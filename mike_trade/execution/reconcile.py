"""Fail-closed startup reconciliation for one broker account or scenario.

The operational schema predates :mod:`mike_trade.portfolio.cycles`: its
``position_cycles.state`` column cannot represent ``RECONCILING`` or
``UNMANAGED``, and it has no first-class cancel/replacement-intent tables.
Consequently this module exposes those states in :class:`ReconciliationReport`
and durably records discrepancies in the existing reconciliation journal.  It
derives pending cancels/replacements from broker-order status.  Callers needing
provider-specific intent queues can supply them through the broker port without
changing the database schema.

One instance is permanently bound to one ``ScopedWriter`` and one broker
environment.  Every query is qualified by that writer's scope; snapshots from
another account, mode, or scenario are rejected before they can be persisted.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol

from mike_trade.brokers.ports import BrokerPort
from mike_trade.domain.errors import ModeIsolationError
from mike_trade.domain.protection import ProtectionStatus
from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    Fill,
    OrderSide,
    Position,
)
from mike_trade.persistence.journal import DomainEventJournal, NewEvent, canonical_payload_json
from mike_trade.persistence.repositories import (
    BrokerOrderRepository,
    CashEquityRepository,
    FillRepository,
    ReconciliationRepository,
)
from mike_trade.persistence.schema import (
    decimal_from_db,
    decimal_to_db,
    from_db_timestamp,
    to_db_timestamp,
)
from mike_trade.persistence.transaction import ScopedWriter
from mike_trade.portfolio.cycles import CycleState

__all__ = [
    "AccountReconciler",
    "StartupReconciler",
    "Reconciler",
    "ReconciliationReport",
    "SymbolReconciliationStatus",
    "ReconciliationHealth",
    "ReconciliationDriftItem",
    "UnmanagedHolding",
    "ReconciliationInventory",
]

_ZERO = Decimal("0")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TERMINAL = frozenset(
    {
        BrokerOrderStatus.FILLED,
        BrokerOrderStatus.CANCELED,
        BrokerOrderStatus.EXPIRED,
        BrokerOrderStatus.REJECTED,
        BrokerOrderStatus.REPLACED,
    }
)


class _Clock(Protocol):
    def now(self) -> datetime: ...


Clock = _Clock | Callable[[], datetime]


def _now(clock: Clock) -> datetime:
    method = getattr(clock, "now", None)
    value = method() if callable(method) else clock()  # type: ignore[operator]
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _symbol(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("symbol must be a non-empty string")
    result = value.strip().upper()
    if any(character.isspace() for character in result):
        raise ValueError("symbol must not contain whitespace")
    return result


class ReconciliationHealth(Enum):
    CLEAN = "CLEAN"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True, slots=True)
class ReconciliationDriftItem:
    drift_type: str
    symbol: str | None
    entity_ref: str | None
    expected: Mapping[str, Any] = field(default_factory=dict)
    actual: Mapping[str, Any] = field(default_factory=dict)
    persisted_drift_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.drift_type, str) or not self.drift_type.strip():
            raise ValueError("drift_type must be a non-empty string")
        if self.symbol is not None:
            object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "expected", MappingProxyType(dict(self.expected)))
        object.__setattr__(self, "actual", MappingProxyType(dict(self.actual)))


@dataclass(frozen=True, slots=True)
class UnmanagedHolding:
    symbol: str
    quantity: Decimal
    average_entry_price: Decimal
    reason: str = "no_matching_managed_cycle"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))


@dataclass(frozen=True, slots=True)
class SymbolReconciliationStatus:
    symbol: str
    state: CycleState
    health: ReconciliationHealth
    reasons: tuple[str, ...] = ()
    cycle_id: str | None = None
    local_quantity: Decimal = _ZERO
    broker_quantity: Decimal = _ZERO

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        if not isinstance(self.state, CycleState):
            raise TypeError("state must be CycleState")
        if not isinstance(self.health, ReconciliationHealth):
            raise TypeError("health must be ReconciliationHealth")

    @property
    def degraded(self) -> bool:
        return self.health is ReconciliationHealth.DEGRADED


@dataclass(frozen=True, slots=True)
class ReconciliationInventory:
    """Scoped local state consulted during one run."""

    managed_cycle_ids: tuple[str, ...] = ()
    pending_intent_ids: tuple[str, ...] = ()
    pending_cancel_order_ids: tuple[str, ...] = ()
    replacement_order_ids: tuple[str, ...] = ()
    active_reservation_ids: tuple[str, ...] = ()
    linked_order_ids: tuple[str, ...] = ()
    strategy_state_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    account_scope_id: str
    run_id: str
    per_symbol_status: Mapping[str, SymbolReconciliationStatus]
    drift_items: tuple[ReconciliationDriftItem, ...]
    unmanaged_holdings: tuple[UnmanagedHolding, ...]
    unknown_orders: tuple[BrokerOrder, ...]
    applied_fill_ids: tuple[str, ...]
    entries_permitted: bool
    account_block_reasons: tuple[str, ...] = ()
    inventory: ReconciliationInventory = field(default_factory=ReconciliationInventory)
    activities_since: datetime = _EPOCH

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "per_symbol_status", MappingProxyType(dict(self.per_symbol_status))
        )

    @property
    def clean(self) -> bool:
        """Engine-compatible alias for ``entries_permitted``."""

        return self.entries_permitted

    @property
    def status(self) -> str:
        return "CLEAN" if self.entries_permitted else "DRIFTED"

    @property
    def reasons(self) -> tuple[str, ...]:
        reasons = list(self.account_block_reasons)
        reasons.extend(item.drift_type for item in self.drift_items)
        reasons.extend(f"UNMANAGED_HOLDING:{item.symbol}" for item in self.unmanaged_holdings)
        reasons.extend(f"UNKNOWN_ORDER:{item.symbol}" for item in self.unknown_orders)
        for item in self.per_symbol_status.values():
            reasons.extend(item.reasons)
        return tuple(dict.fromkeys(reasons))


@dataclass(frozen=True, slots=True)
class _CycleRow:
    cycle_id: str
    symbol: str
    state: str
    remaining: Decimal


@dataclass(frozen=True, slots=True)
class _LocalSnapshot:
    cycles: tuple[_CycleRow, ...]
    intent_by_client: Mapping[str, tuple[str, str, str, str]]
    order_by_id: Mapping[str, tuple[str | None, str, str, str]]
    protection_by_cycle: Mapping[str, tuple[str, Decimal, str | None]]
    protection_order_ids: frozenset[str]
    inventory: ReconciliationInventory


class AccountReconciler:
    """Reconcile exactly one account/scenario against one broker port."""

    def __init__(
        self,
        *,
        broker: BrokerPort,
        writer: ScopedWriter,
        clock: Clock,
        minimum_buying_power: Decimal = _ZERO,
    ) -> None:
        if broker.environment is not writer.environment:
            raise ModeIsolationError(
                "broker and SQLite writer environments differ",
                code="reconciliation_environment_mismatch",
                context={
                    "broker": broker.environment.value,
                    "writer": writer.environment.value,
                },
            )
        if (
            not isinstance(minimum_buying_power, Decimal)
            or not minimum_buying_power.is_finite()
            or minimum_buying_power < _ZERO
        ):
            raise ValueError("minimum_buying_power must be a finite non-negative Decimal")
        self._broker = broker
        self._writer = writer
        self._clock = clock
        self._minimum_buying_power = minimum_buying_power
        self._orders = BrokerOrderRepository(writer, clock)
        self._fills = FillRepository(writer, clock)
        self._cash = CashEquityRepository(writer, clock)
        self._runs = ReconciliationRepository(writer, clock)
        self._journal = DomainEventJournal(writer.connection, clock=clock)

    @property
    def account_scope_id(self) -> str:
        return self._writer.account_scope_id

    async def run(self) -> ReconciliationReport:
        return await self.reconcile()

    async def reconcile(self) -> ReconciliationReport:
        run_id = self._runs.start_run()
        initial = self._load_local_snapshot()
        activities_since = self._activity_cursor()
        prior_unresolved = self._runs.has_unresolved_drift()
        drifts: list[ReconciliationDriftItem] = []
        applied: list[str] = []
        external_fills: list[Fill] = []
        account_reasons: list[str] = []

        try:
            account = await self._broker.get_account()
            self._require_scope(account.scope.scope_id, "account snapshot")
            self._cash.append_equity_snapshot(account)
            if account.unsafe_flags:
                account_reasons.extend(f"ACCOUNT_FLAG:{flag}" for flag in account.unsafe_flags)
            if account.buying_power <= self._minimum_buying_power:
                account_reasons.append("INSUFFICIENT_BUYING_POWER")
            if prior_unresolved:
                account_reasons.append("UNRESOLVED_RECONCILIATION_DRIFT")

            positions = tuple(await self._broker.list_positions())
            open_orders = tuple(await self._broker.list_open_orders())
            self._validate_positions(positions)
            self._validate_orders(open_orders)

            # Pending local intents and all known linked orders are looked up even
            # when absent from the broker's open-order response.
            observed: dict[str, BrokerOrder] = {
                order.broker_order_id: order for order in open_orders
            }
            for order_id in initial.inventory.linked_order_ids:
                if order_id not in observed:
                    found = await self._broker.get_order(order_id)
                    if found is not None:
                        self._require_order_scope(found)
                        observed[found.broker_order_id] = found
            for client_id in initial.intent_by_client:
                if not any(order.client_order_id == client_id for order in observed.values()):
                    found = await self._broker.get_order_by_client_id(client_id)
                    if found is not None:
                        self._require_order_scope(found)
                        observed[found.broker_order_id] = found

            linked_broker_ids: set[str] = set(initial.order_by_id)
            linked_broker_ids.update(initial.protection_order_ids)
            for order in observed.values():
                linked = (
                    order.broker_order_id in linked_broker_ids
                    or order.client_order_id in initial.intent_by_client
                )
                if linked:
                    intent = initial.intent_by_client.get(order.client_order_id)
                    self._orders.upsert_broker_order(
                        order, intent_id=None if intent is None else intent[0]
                    )

            activities = tuple(await self._broker.list_fills(activities_since))
            for fill in sorted(activities, key=lambda item: (item.filled_at, item.fill_id)):
                self._require_fill_scope(fill)
                local = self._load_local_snapshot()
                if fill.broker_order_id not in local.order_by_id:
                    found = observed.get(fill.broker_order_id)
                    if found is None:
                        found = await self._broker.get_order(fill.broker_order_id)
                        if found is not None:
                            self._require_order_scope(found)
                            observed[found.broker_order_id] = found
                    if found is not None and found.client_order_id in local.intent_by_client:
                        intent_id = local.intent_by_client[found.client_order_id][0]
                        self._orders.upsert_broker_order(found, intent_id=intent_id)
                        local = self._load_local_snapshot()
                order_row = local.order_by_id.get(fill.broker_order_id)
                if order_row is not None and order_row[0] is not None:
                    result = self._fills.apply_fill(fill)
                    if result.applied:
                        applied.append(fill.fill_id)
                else:
                    external_fills.append(fill)

            # A broker-confirmed flat snapshot plus exact external sell fills can
            # close a managed cycle without inventing a strategy intent or P&L win.
            broker_positions = {position.symbol: position for position in positions}
            open_ids = {order.broker_order_id for order in open_orders}
            refreshed = self._load_local_snapshot()
            for cycle in refreshed.cycles:
                if cycle.remaining <= _ZERO or cycle.symbol in broker_positions:
                    continue
                candidates = tuple(
                    fill
                    for fill in external_fills
                    if fill.symbol == cycle.symbol and fill.side is OrderSide.SELL
                )
                linked_open = self._cycle_has_linked_open_order(cycle.cycle_id, open_ids)
                if (
                    candidates
                    and sum((fill.quantity for fill in candidates), start=_ZERO)
                    == cycle.remaining
                    and not linked_open
                    and await self._persist_external_orders(candidates, observed)
                ):
                    changed = self._apply_external_manual_exit(cycle, candidates)
                    if changed:
                        applied.extend(fill.fill_id for fill in candidates)
                        drifts.append(
                            ReconciliationDriftItem(
                                "EXTERNAL_MANUAL_EXIT",
                                cycle.symbol,
                                cycle.cycle_id,
                                {"local_remaining": cycle.remaining},
                                {"broker_quantity": _ZERO},
                            )
                        )

            local = self._load_local_snapshot()
            unknown_orders = tuple(
                order
                for order in open_orders
                if order.broker_order_id not in local.order_by_id
                and order.broker_order_id not in local.protection_order_ids
                and order.client_order_id not in local.intent_by_client
            )
            for order in unknown_orders:
                drifts.append(
                    ReconciliationDriftItem(
                        "UNKNOWN_BROKER_ORDER",
                        order.symbol,
                        order.broker_order_id,
                        {"linked_local_order": True},
                        {"client_order_id": order.client_order_id},
                    )
                )

            self._append_pending_local_drifts(local, observed, drifts)
            statuses, unmanaged = self._classify_symbols(
                local, positions, open_orders, drifts
            )
            persisted = self._persist_drifts(run_id, drifts)
            drifts = persisted

            clean = not (
                account_reasons
                or drifts
                or unmanaged
                or unknown_orders
                or any(status.degraded for status in statuses.values())
            )
            report = ReconciliationReport(
                account_scope_id=self.account_scope_id,
                run_id=run_id,
                per_symbol_status=statuses,
                drift_items=tuple(drifts),
                unmanaged_holdings=unmanaged,
                unknown_orders=unknown_orders,
                applied_fill_ids=tuple(dict.fromkeys(applied)),
                entries_permitted=clean,
                account_block_reasons=tuple(dict.fromkeys(account_reasons)),
                inventory=local.inventory,
                activities_since=activities_since,
            )
            self._runs.complete_run(
                run_id,
                "CLEAN" if clean else "DRIFTED",
                {
                    "entries_permitted": clean,
                    "drift_count": len(drifts),
                    "unmanaged_count": len(unmanaged),
                    "unknown_order_count": len(unknown_orders),
                    "applied_fill_ids": report.applied_fill_ids,
                },
            )
            return report
        except BaseException as error:
            self._runs.complete_run(
                run_id,
                "ERROR",
                {"error_type": type(error).__name__, "message": str(error)},
            )
            raise

    # ---------------------------------------------------------------- scope checks

    def _require_scope(self, actual: str, source: str) -> None:
        if actual != self.account_scope_id:
            raise ModeIsolationError(
                f"{source} belongs to {actual!r}, not {self.account_scope_id!r}",
                code="cross_scope_reconciliation",
                context={"expected": self.account_scope_id, "actual": actual},
            )

    def _require_order_scope(self, order: BrokerOrder) -> None:
        self._require_scope(order.account_scope_id, "broker order")

    def _require_fill_scope(self, fill: Fill) -> None:
        self._require_scope(fill.account_scope_id, "broker fill")

    def _validate_orders(self, orders: Sequence[BrokerOrder]) -> None:
        for order in orders:
            self._require_order_scope(order)

    def _validate_positions(self, positions: Sequence[Position]) -> None:
        for position in positions:
            self._require_scope(position.account_scope_id, "broker position")

    # ---------------------------------------------------------------- local reads

    def _activity_cursor(self) -> datetime:
        row = self._writer.connection.execute(
            "SELECT MAX(occurred_at) FROM ("
            "SELECT occurred_at FROM broker_activities WHERE account_scope_id=? "
            "UNION ALL SELECT filled_at FROM fills WHERE account_scope_id=?"
            ")",
            (self.account_scope_id, self.account_scope_id),
        ).fetchone()
        if row is None or row[0] is None:
            return _EPOCH
        # Inclusive provider APIs and the fill-id uniqueness constraint make a
        # one-microsecond overlap safe while avoiding a same-timestamp gap.
        return max(_EPOCH, from_db_timestamp(row[0]) - timedelta(microseconds=1))

    def _load_local_snapshot(self) -> _LocalSnapshot:
        conn, scope = self._writer.connection, self.account_scope_id
        cycle_rows = conn.execute(
            "SELECT cycle_id,symbol,state FROM position_cycles "
            "WHERE account_scope_id=? ORDER BY created_at,rowid",
            (scope,),
        ).fetchall()
        cycles: list[_CycleRow] = []
        for cycle_id, symbol, state in cycle_rows:
            quantities = conn.execute(
                "SELECT side,quantity FROM fills WHERE account_scope_id=? AND cycle_id=?",
                (scope, cycle_id),
            ).fetchall()
            bought = sum(
                (decimal_from_db(qty) for side, qty in quantities if side == "BUY"),
                start=_ZERO,
            )
            sold = sum(
                (decimal_from_db(qty) for side, qty in quantities if side == "SELL"),
                start=_ZERO,
            )
            cycles.append(_CycleRow(cycle_id, symbol, state, bought - sold))

        intent_rows = conn.execute(
            "SELECT client_order_id,intent_id,cycle_id,symbol,status FROM order_intents "
            "WHERE account_scope_id=?",
            (scope,),
        ).fetchall()
        intent_by_client = {
            row[0]: (row[1], row[2], row[3], row[4]) for row in intent_rows
        }
        order_rows = conn.execute(
            "SELECT broker_order_id,intent_id,client_order_id,symbol,status FROM broker_orders "
            "WHERE account_scope_id=?",
            (scope,),
        ).fetchall()
        order_by_id = {row[0]: (row[1], row[2], row[3], row[4]) for row in order_rows}

        coverage: dict[str, tuple[str, Decimal, str | None]] = {}
        protection_ids: set[str] = set()
        for cycle_id, status, quantity, broker_id in conn.execute(
            "SELECT cycle_id,status,covered_quantity,broker_order_id FROM protection_orders "
            "WHERE account_scope_id=? ORDER BY updated_at",
            (scope,),
        ).fetchall():
            coverage[cycle_id] = (status, decimal_from_db(quantity), broker_id)
            if broker_id:
                protection_ids.add(broker_id)
        for cycle_id, status, quantity, broker_id in conn.execute(
            "SELECT cycle_id,status,covered_quantity,broker_order_id FROM trailing_stop_states "
            "WHERE account_scope_id=? ORDER BY updated_at",
            (scope,),
        ).fetchall():
            coverage[cycle_id] = (status, decimal_from_db(quantity), broker_id)
            if broker_id:
                protection_ids.add(broker_id)

        pending_intents = tuple(
            sorted(row[1] for row in intent_rows if row[4] in {"RECORDED", "SUBMITTED", "UNKNOWN"})
        )
        pending_cancels = tuple(sorted(row[0] for row in order_rows if row[4] == "PENDING_CANCEL"))
        replacements = tuple(sorted(row[0] for row in order_rows if row[4] == "REPLACED"))
        reservations = tuple(
            row[0]
            for row in conn.execute(
                "SELECT reservation_id FROM budget_reservations "
                "WHERE account_scope_id=? AND state='ACTIVE' ORDER BY created_at",
                (scope,),
            ).fetchall()
        )
        strategies = tuple(
            f"{row[0]}:{row[1]}"
            for row in conn.execute(
                "SELECT instance_id,symbol FROM strategy_state "
                "WHERE account_scope_id=? ORDER BY instance_id,symbol",
                (scope,),
            ).fetchall()
        )
        inventory = ReconciliationInventory(
            managed_cycle_ids=tuple(row.cycle_id for row in cycles),
            pending_intent_ids=pending_intents,
            pending_cancel_order_ids=pending_cancels,
            replacement_order_ids=replacements,
            active_reservation_ids=reservations,
            linked_order_ids=tuple(sorted(order_by_id)),
            strategy_state_keys=strategies,
        )
        return _LocalSnapshot(
            tuple(cycles),
            MappingProxyType(intent_by_client),
            MappingProxyType(order_by_id),
            MappingProxyType(coverage),
            frozenset(protection_ids),
            inventory,
        )

    # --------------------------------------------------------------- classification

    def _classify_symbols(
        self,
        local: _LocalSnapshot,
        positions: Sequence[Position],
        open_orders: Sequence[BrokerOrder],
        drifts: list[ReconciliationDriftItem],
    ) -> tuple[dict[str, SymbolReconciliationStatus], tuple[UnmanagedHolding, ...]]:
        position_by_symbol = {position.symbol: position for position in positions}
        all_cycles_by_symbol: dict[str, list[_CycleRow]] = {}
        cycles_by_symbol: dict[str, list[_CycleRow]] = {}
        for cycle in local.cycles:
            all_cycles_by_symbol.setdefault(cycle.symbol, []).append(cycle)
            if cycle.state != "CLOSED":
                cycles_by_symbol.setdefault(cycle.symbol, []).append(cycle)

        unmanaged: list[UnmanagedHolding] = []
        reasons: dict[str, list[str]] = {}
        for position in positions:
            managed = [
                cycle
                for cycle in cycles_by_symbol.get(position.symbol, ())
                if cycle.remaining > 0
            ]
            if not managed:
                unmanaged.append(
                    UnmanagedHolding(
                        position.symbol, position.quantity, position.avg_entry_price
                    )
                )
                reasons.setdefault(position.symbol, []).append("UNMANAGED_HOLDING")

        for symbol, cycles in cycles_by_symbol.items():
            if len([cycle for cycle in cycles if cycle.remaining > 0]) > 1:
                drifts.append(
                    ReconciliationDriftItem(
                        "MULTIPLE_MANAGED_CYCLES",
                        symbol,
                        symbol,
                        {"maximum": 1},
                        {"cycle_ids": [cycle.cycle_id for cycle in cycles]},
                    )
                )
                reasons.setdefault(symbol, []).append("MULTIPLE_MANAGED_CYCLES")
            for cycle in cycles:
                broker_qty = position_by_symbol.get(symbol)
                actual = _ZERO if broker_qty is None else broker_qty.quantity
                if cycle.remaining != actual:
                    item = ReconciliationDriftItem(
                        "POSITION_QUANTITY_MISMATCH",
                        symbol,
                        cycle.cycle_id,
                        {"local_fill_ledger_quantity": cycle.remaining},
                        {"broker_position_quantity": actual},
                    )
                    drifts.append(item)
                    reasons.setdefault(symbol, []).append(item.drift_type)
                if cycle.remaining > _ZERO and self._provider_supports_protection():
                    coverage = local.protection_by_cycle.get(cycle.cycle_id)
                    if coverage is None:
                        kind = "PROTECTION_COVERAGE_MISSING"
                        actual_coverage: Mapping[str, Any] = {}
                    else:
                        status, quantity, broker_id = coverage
                        open_by_id = {
                            order.broker_order_id: order for order in open_orders
                        }
                        protective_order = (
                            None if broker_id is None else open_by_id.get(broker_id)
                        )
                        if status == ProtectionStatus.UNKNOWN.value:
                            kind = "PROTECTION_COVERAGE_UNKNOWN"
                        elif (
                            status != ProtectionStatus.ACTIVE.value
                            or quantity != actual
                            or broker_id is None
                            or protective_order is None
                            or protective_order.status
                            in {
                                BrokerOrderStatus.UNKNOWN,
                                BrokerOrderStatus.PENDING_CANCEL,
                            }
                            or protective_order.side is not OrderSide.SELL
                            or protective_order.quantity
                            - protective_order.filled_quantity
                            != actual
                        ):
                            kind = "PROTECTION_COVERAGE_MISMATCH"
                        else:
                            kind = ""
                        actual_coverage = {
                            "status": status,
                            "covered_quantity": quantity,
                            "broker_order_id": broker_id,
                        }
                    if kind:
                        drifts.append(
                            ReconciliationDriftItem(
                                kind,
                                symbol,
                                cycle.cycle_id,
                                {"required_quantity": actual},
                                actual_coverage,
                            )
                        )
                        reasons.setdefault(symbol, []).append(kind)

        for order in open_orders:
            if (
                order.broker_order_id not in local.order_by_id
                and order.broker_order_id not in local.protection_order_ids
                and order.client_order_id not in local.intent_by_client
            ):
                reasons.setdefault(order.symbol, []).append("UNKNOWN_BROKER_ORDER")

        for drift in drifts:
            if drift.symbol is not None:
                reasons.setdefault(drift.symbol, []).append(drift.drift_type)

        all_symbols = set(all_cycles_by_symbol) | set(position_by_symbol) | set(reasons)
        statuses: dict[str, SymbolReconciliationStatus] = {}
        unmanaged_symbols = {item.symbol for item in unmanaged}
        for symbol in sorted(all_symbols):
            symbol_cycles = cycles_by_symbol.get(symbol, [])
            persisted_cycles = all_cycles_by_symbol.get(symbol, [])
            selected_cycle = (
                symbol_cycles[-1]
                if symbol_cycles
                else persisted_cycles[-1]
                if persisted_cycles
                else None
            )
            local_qty = _ZERO if selected_cycle is None else selected_cycle.remaining
            broker_quantity = (
                _ZERO if symbol not in position_by_symbol else position_by_symbol[symbol].quantity
            )
            problem_reasons = tuple(dict.fromkeys(reasons.get(symbol, ())))
            if symbol in unmanaged_symbols:
                state = CycleState.UNMANAGED
            elif problem_reasons:
                state = CycleState.RECONCILING
            elif selected_cycle is None or selected_cycle.state == "CLOSED":
                state = CycleState.CLOSED
            elif selected_cycle.state == "PENDING_ENTRY":
                state = CycleState.WATCHING_BUY
            elif selected_cycle.state == "EXITING":
                state = CycleState.SELL_PENDING
            else:
                state = CycleState.WATCHING_SELL
            statuses[symbol] = SymbolReconciliationStatus(
                symbol=symbol,
                cycle_id=(
                    None
                    if selected_cycle is None or symbol in unmanaged_symbols
                    else selected_cycle.cycle_id
                ),
                state=state,
                health=(
                    ReconciliationHealth.DEGRADED
                    if problem_reasons or symbol in unmanaged_symbols
                    else ReconciliationHealth.CLEAN
                ),
                reasons=problem_reasons,
                local_quantity=local_qty,
                broker_quantity=broker_quantity,
            )
        return statuses, tuple(unmanaged)

    def _provider_supports_protection(self) -> bool:
        capabilities = self._broker.capabilities()
        order_types = {str(value).upper() for value in capabilities.supported_order_types}
        return capabilities.supports_native_trailing_stop or bool(
            order_types & {"STOP", "STOP_LIMIT", "TRAILING_STOP"}
        )

    def _append_pending_local_drifts(
        self,
        local: _LocalSnapshot,
        observed: Mapping[str, BrokerOrder],
        drifts: list[ReconciliationDriftItem],
    ) -> None:
        observed_clients = {order.client_order_id for order in observed.values()}
        for client_id, (intent_id, cycle_id, symbol, status) in local.intent_by_client.items():
            has_local_order = any(row[1] == client_id for row in local.order_by_id.values())
            if status == "UNKNOWN" or (
                status in {"RECORDED", "SUBMITTED"}
                and not has_local_order
                and client_id not in observed_clients
            ):
                drifts.append(
                    ReconciliationDriftItem(
                        "LOCAL_INTENT_UNRESOLVED",
                        symbol,
                        intent_id,
                        {"broker_order": "present_or_confirmed_absent"},
                        {"intent_status": status, "cycle_id": cycle_id},
                    )
                )
        for order_id in local.inventory.pending_cancel_order_ids:
            row = local.order_by_id[order_id]
            observed_order = observed.get(order_id)
            if observed_order is None or observed_order.status is BrokerOrderStatus.PENDING_CANCEL:
                drifts.append(
                    ReconciliationDriftItem(
                        "CANCEL_PENDING",
                        row[2],
                        order_id,
                        {"status": "terminal"},
                        {"status": row[3]},
                    )
                )
        for order_id in local.inventory.replacement_order_ids:
            client_id = local.order_by_id[order_id][1]
            successor = [
                order
                for order in observed.values()
                if order.client_order_id == client_id and order.broker_order_id != order_id
            ]
            if not successor:
                drifts.append(
                    ReconciliationDriftItem(
                        "REPLACEMENT_UNRESOLVED",
                        local.order_by_id[order_id][2],
                        order_id,
                        {"replacement_order": "present"},
                        {},
                    )
                )

        conn = self._writer.connection
        for reservation_id in local.inventory.active_reservation_ids:
            row = conn.execute(
                "SELECT r.cycle_id,c.symbol FROM budget_reservations r "
                "LEFT JOIN position_cycles c ON c.account_scope_id=r.account_scope_id "
                "AND c.cycle_id=r.cycle_id WHERE r.account_scope_id=? AND r.reservation_id=?",
                (self.account_scope_id, reservation_id),
            ).fetchone()
            if row is None:
                continue
            has_pending_intent = conn.execute(
                "SELECT 1 FROM order_intents WHERE account_scope_id=? AND cycle_id=? "
                "AND status IN ('RECORDED','SUBMITTED','UNKNOWN') LIMIT 1",
                (self.account_scope_id, row[0]),
            ).fetchone()
            held = any(
                cycle.cycle_id == row[0] and cycle.remaining > _ZERO
                for cycle in local.cycles
            )
            if has_pending_intent is None and not held:
                drifts.append(
                    ReconciliationDriftItem(
                        "ORPHAN_BUDGET_RESERVATION",
                        row[1],
                        reservation_id,
                        {"active_exposure": True},
                        {"cycle_id": row[0]},
                    )
                )

    # ------------------------------------------------------- external/manual exits

    def _cycle_has_linked_open_order(self, cycle_id: str, open_ids: set[str]) -> bool:
        rows = self._writer.connection.execute(
            "SELECT b.broker_order_id FROM broker_orders b JOIN order_intents i "
            "ON i.account_scope_id=b.account_scope_id AND i.intent_id=b.intent_id "
            "WHERE b.account_scope_id=? AND i.cycle_id=?",
            (self.account_scope_id, cycle_id),
        ).fetchall()
        protected = self._writer.connection.execute(
            "SELECT broker_order_id FROM protection_orders WHERE account_scope_id=? "
            "AND cycle_id=? UNION SELECT broker_order_id FROM trailing_stop_states "
            "WHERE account_scope_id=? AND cycle_id=?",
            (self.account_scope_id, cycle_id, self.account_scope_id, cycle_id),
        ).fetchall()
        linked = {row[0] for row in (*rows, *protected) if row[0] is not None}
        return bool(linked & open_ids)

    async def _persist_external_orders(
        self, fills: Sequence[Fill], observed: dict[str, BrokerOrder]
    ) -> bool:
        for fill in fills:
            order = observed.get(fill.broker_order_id)
            if order is None:
                order = await self._broker.get_order(fill.broker_order_id)
            if order is None:
                return False
            self._require_order_scope(order)
            if order.status not in _TERMINAL or order.side is not OrderSide.SELL:
                return False
            observed[order.broker_order_id] = order
            self._orders.upsert_broker_order(order, intent_id=None)
        return True

    def _apply_external_manual_exit(
        self, cycle: _CycleRow, fills: Sequence[Fill]
    ) -> bool:
        """Record exact external fills and close once; never create a local intent."""

        now, scope = _now(self._clock), self.account_scope_id
        with self._writer.unit_of_work() as conn:
            if all(
                conn.execute(
                    "SELECT 1 FROM fills WHERE account_scope_id=? AND external_activity_id=?",
                    (scope, fill.fill_id),
                ).fetchone()
                for fill in fills
            ):
                return False
            current = conn.execute(
                "SELECT state FROM position_cycles WHERE account_scope_id=? AND cycle_id=?",
                (scope, cycle.cycle_id),
            ).fetchone()
            if current is None or current[0] == "CLOSED":
                return False
            for fill in fills:
                exists = conn.execute(
                    "SELECT 1 FROM fills WHERE account_scope_id=? AND external_activity_id=?",
                    (scope, fill.fill_id),
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    "INSERT INTO fills(fill_id,account_scope_id,external_activity_id,"
                    "broker_order_id,cycle_id,symbol,side,quantity,price,fee,filled_at,"
                    "recorded_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"{scope}:{fill.fill_id}",
                        scope,
                        fill.fill_id,
                        fill.broker_order_id,
                        cycle.cycle_id,
                        fill.symbol,
                        fill.side.value,
                        decimal_to_db(fill.quantity),
                        decimal_to_db(fill.price),
                        decimal_to_db(fill.fee),
                        to_db_timestamp(fill.filled_at),
                        to_db_timestamp(now),
                    ),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO broker_activities(broker_activity_id,account_scope_id,"
                    "external_activity_id,activity_type,payload_json,occurred_at,recorded_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        f"external:{scope}:{fill.fill_id}",
                        scope,
                        fill.fill_id,
                        "EXTERNAL_MANUAL_EXIT",
                        canonical_payload_json(
                            {
                                "cycle_id": cycle.cycle_id,
                                "broker_order_id": fill.broker_order_id,
                                "classification": "external_manual_exit",
                            }
                        ),
                        to_db_timestamp(fill.filled_at),
                        to_db_timestamp(now),
                    ),
                )
            closed_at = max(fill.filled_at for fill in fills)
            conn.execute(
                "UPDATE position_cycles SET state='CLOSED',closed_at=?,updated_at=? "
                "WHERE account_scope_id=? AND cycle_id=?",
                (to_db_timestamp(closed_at), to_db_timestamp(now), scope, cycle.cycle_id),
            )
            conn.execute(
                "DELETE FROM positions WHERE account_scope_id=? AND symbol=? AND cycle_id=?",
                (scope, cycle.symbol, cycle.cycle_id),
            )
            conn.execute(
                "UPDATE budget_reservations SET state='RELEASED',amount='0',resolved_at=? "
                "WHERE account_scope_id=? AND cycle_id=? AND state='ACTIVE'",
                (to_db_timestamp(now), scope, cycle.cycle_id),
            )
            conn.execute(
                "UPDATE protection_orders SET status='CANCELED',updated_at=? "
                "WHERE account_scope_id=? AND cycle_id=? "
                "AND status IN ('PENDING','ACTIVE','REPLACING','UNKNOWN')",
                (to_db_timestamp(now), scope, cycle.cycle_id),
            )
            conn.execute(
                "UPDATE trailing_stop_states SET status='CANCELED',updated_at=? "
                "WHERE account_scope_id=? AND cycle_id=? "
                "AND status IN ('PENDING','ACTIVE','REPLACING','UNKNOWN')",
                (to_db_timestamp(now), scope, cycle.cycle_id),
            )
            self._journal.append_in_transaction(
                [
                    NewEvent(
                        scope,
                        "position_cycle",
                        cycle.cycle_id,
                        "ExternalManualExitDetected",
                        {
                            "classification": "external_manual_exit",
                            "strategy_exit": False,
                            "strategy_win": False,
                            "symbol": cycle.symbol,
                            "fill_ids": tuple(fill.fill_id for fill in fills),
                        },
                        closed_at,
                        idempotency_key="external_manual_exit:"
                        + ":".join(sorted(fill.fill_id for fill in fills)),
                    )
                ]
            )
        return True

    # --------------------------------------------------------------- drift output

    def _persist_drifts(
        self, run_id: str, drifts: Sequence[ReconciliationDriftItem]
    ) -> list[ReconciliationDriftItem]:
        result: list[ReconciliationDriftItem] = []
        seen: set[tuple[str, str | None, str | None]] = set()
        for item in drifts:
            key = (item.drift_type, item.symbol, item.entity_ref)
            if key in seen:
                continue
            seen.add(key)
            drift_id = self._runs.record_drift(
                run_id,
                item.drift_type,
                entity_ref=item.entity_ref,
                expected=item.expected,
                actual=item.actual,
            )
            result.append(
                ReconciliationDriftItem(
                    item.drift_type,
                    item.symbol,
                    item.entity_ref,
                    item.expected,
                    item.actual,
                    drift_id,
                )
            )
        return result


StartupReconciler = AccountReconciler
Reconciler = AccountReconciler
