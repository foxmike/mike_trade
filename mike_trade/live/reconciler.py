"""Continuous reconciliation of broker truth against the engine's ledger and lifecycle.

Alpaca's positions, open orders and executions are authoritative.  This module runs on engine
start, on a short periodic loop, whenever the ``trade_updates`` stream reports activity, and
immediately before the lifecycle submits any order, so a manual action (Alpaca website or
mobile app, DAS Trader on the same account, ...) is noticed in seconds, never at end of day.

Per pass it:

1. resolves intents whose submit outcome was unknown (lookup by ``client_order_id``);
2. refreshes the ledger's view of every order the engine owns;
3. ingests broker executions idempotently by activity id - the engine's own orders as
   ordinary fills, anything else as an *external* execution that may only reduce the managed
   cycle it belongs to (a purchase is journaled and never adopted);
4. compares the managed quantity with the broker position and restores coverage:
   a full external sale closes the cycle, a partial one resizes the stop, a human-cancelled
   stop is re-placed, a duplicate protective order is cancelled, and a symbol that holds shares
   the engine does not manage is *unmanaged* (no entry, no adoption).

Nothing is invented: when a broker read fails the pass reports failure and changes nothing,
and the lifecycle refuses to act on holdings it could not verify.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from mike_trade.domain.errors import BrokerError
from mike_trade.domain.protection import ProtectionPolicy
from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    Fill,
    OrderSide,
    PositionCycle,
    PositionCycleState,
)
from mike_trade.live.executor import LiveExecutor, ProtectionFailure
from mike_trade.live.gateway import BrokerSnapshot, Execution, LiveGateway
from mike_trade.live.ledger import LiveLedger, OrderRow

__all__ = ["LiveReconciler", "ReconcileReport", "StaleHoldings", "SymbolState"]

ZERO = Decimal("0")
_EXIT_PURPOSES = frozenset(
    {"STRATEGY_EXIT", "MANUAL_CLOSE", "SAFETY_EXIT", "EOD_CLOSE"}
)
_WORKING = frozenset(
    {
        BrokerOrderStatus.NEW,
        BrokerOrderStatus.PENDING_NEW,
        BrokerOrderStatus.ACCEPTED,
        BrokerOrderStatus.PARTIALLY_FILLED,
        BrokerOrderStatus.PENDING_CANCEL,
    }
)
Escalate = Callable[[PositionCycle, str], Awaitable[None]]
PolicyFor = Callable[[PositionCycle], ProtectionPolicy | None]


class StaleHoldings(RuntimeError):
    """Broker holdings could not be verified, so no order may be decided from them."""


@dataclass(slots=True)
class SymbolState:
    symbol: str
    cycle_id: str | None = None
    managed_quantity: Decimal = ZERO
    broker_quantity: Decimal = ZERO
    unmanaged_quantity: Decimal = ZERO
    entry_pending: bool = False
    exit_pending: bool = False
    protected: bool = False
    #: Entries (not exits, not protection) are blocked for this symbol.
    frozen: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def holding(self) -> bool:
        return self.managed_quantity > ZERO


@dataclass(slots=True)
class ReconcileReport:
    ok: bool = True
    consistent: bool = True
    at: datetime | None = None
    states: dict[str, SymbolState] = field(default_factory=dict)
    applied_fills: list[str] = field(default_factory=list)
    external_sales: list[str] = field(default_factory=list)
    external_buys: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    drifts: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.ok and not self.failures


class LiveReconciler:
    """Reconcile one PAPER/LIVE account against its broker."""

    def __init__(
        self,
        gateway: LiveGateway,
        ledger: LiveLedger,
        executor: LiveExecutor,
        clock: Any,
        *,
        symbols: Iterable[str],
        policy_for: PolicyFor,
        escalate: Escalate | None = None,
        activity_overlap: timedelta = timedelta(minutes=2),
        lag_grace: timedelta = timedelta(seconds=120),
        unknown_intent_grace: timedelta = timedelta(seconds=30),
        interval: float = 10.0,
        on_cycle_closed: Callable[[PositionCycle, bool], None] | None = None,
    ) -> None:
        self.gateway = gateway
        self.ledger = ledger
        self.executor = executor
        self.clock = clock
        self.symbols = tuple(symbols)
        self.policy_for = policy_for
        self.escalate = escalate
        self.activity_overlap = activity_overlap
        self.lag_grace = lag_grace
        self.unknown_intent_grace = unknown_intent_grace
        self.interval = interval
        self.on_cycle_closed = on_cycle_closed
        self.lock = asyncio.Lock()
        self.last: ReconcileReport | None = None
        self.last_success_at: datetime | None = None
        self.states: dict[str, SymbolState] = {}
        self.entries_blocked_reasons: list[str] = []
        self._kick = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._deficit_since: dict[str, datetime] = {}
        self._reported: set[tuple[str, str]] = set()

    # ============================================================= public API

    async def reconcile(self, reason: str = "periodic") -> ReconcileReport:
        async with self.lock:
            return await self.reconcile_locked(reason)

    async def ensure_fresh(self, symbol: str | None = None) -> ReconcileReport:
        """Reconcile now; raise :class:`StaleHoldings` if the broker could not be read."""
        report = await self.reconcile("pre-decision")
        if not report.ok:
            raise StaleHoldings("; ".join(report.failures) or "broker read failed")
        return report

    def state(self, symbol: str) -> SymbolState:
        return self.states.get(symbol) or SymbolState(symbol)

    def kick(self) -> None:
        """Wake the loop now (e.g. a trade update arrived)."""
        self._kick.set()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop(), name="live-reconcile")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._kick.wait(), timeout=self.interval)
            except TimeoutError:
                pass
            self._kick.clear()
            try:
                await self.reconcile("periodic")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a failed pass must never kill the loop
                self.entries_blocked_reasons = [f"RECONCILE_ERROR:{type(exc).__name__}"]

    # ============================================================== the pass

    async def reconcile_locked(self, reason: str) -> ReconcileReport:
        report = ReconcileReport(at=self.clock.now())
        try:
            snapshot = self.gateway.snapshot()
            report.consistent = snapshot.consistent
            self._resolve_unknown_intents(snapshot, report)
            self._refresh_orders(snapshot, report)
            executions = self._executions(report)
            self._ingest(executions, snapshot, report)
        except Exception as exc:  # broker/API/DB failure: invent nothing
            report.ok = False
            report.failures.append(f"{type(exc).__name__}: {exc}")
            self.last = report
            self.entries_blocked_reasons = ["BROKER_READ_FAILED"]
            return report
        # The broker may have moved while executions were being applied: take the picture the
        # symbol decisions are made from AFTER ingestion.
        try:
            snapshot = self.gateway.snapshot()
            report.consistent = report.consistent and snapshot.consistent
            self._refresh_orders(snapshot, report)
        except Exception as exc:
            report.ok = False
            report.failures.append(f"{type(exc).__name__}: {exc}")
            self.last = report
            self.entries_blocked_reasons = ["BROKER_READ_FAILED"]
            return report
        symbols = self._universe(snapshot)
        for symbol in sorted(symbols):
            try:
                report.states[symbol] = await self._reconcile_symbol(symbol, snapshot, report)
            except ProtectionFailure as exc:
                state = report.states.setdefault(symbol, SymbolState(symbol))
                state.reasons.append(f"PROTECTION_FAILED:{exc}")
                state.frozen = True
                report.failures.append(f"{symbol}: {exc}")
            except Exception as exc:
                state = report.states.setdefault(symbol, SymbolState(symbol))
                state.frozen = True
                state.reasons.append(f"RECONCILE_ERROR:{type(exc).__name__}")
                report.failures.append(f"{symbol}: {type(exc).__name__}: {exc}")
        self.states = report.states
        self.last = report
        self.last_success_at = report.at
        self.entries_blocked_reasons = [] if report.consistent else ["INCONSISTENT_SNAPSHOT"]
        return report

    def _universe(self, snapshot: BrokerSnapshot) -> set[str]:
        symbols = {cycle.symbol for cycle in self.ledger.open_cycles()}
        symbols |= set(snapshot.positions)
        symbols |= {order.symbol for order in snapshot.open_orders}
        symbols |= {row.symbol for row in self.ledger.working_order_rows()}
        symbols |= set(self.symbols)
        return symbols

    # ------------------------------------------------- 1. unknown intents

    def _resolve_unknown_intents(self, snapshot: BrokerSnapshot, report: ReconcileReport) -> None:
        now = self.clock.now()
        for intent in self.ledger.unresolved_intents():
            order = self.gateway.get_order_by_client_id(intent.client_order_id)
            if order is not None:
                self.ledger.upsert_order(order, intent_id=intent.intent_id)
                self.ledger.mark_submitted_by_id(intent.intent_id, order.broker_order_id)
                report.actions.append(f"INTENT_RESOLVED_FOUND:{intent.client_order_id}")
            else:
                created = self._intent_age(intent.intent_id, now)
                if created is None or created >= self.unknown_intent_grace:
                    self.ledger.mark_not_submitted(intent.intent_id, "broker has no such order")
                    report.actions.append(f"INTENT_RESOLVED_ABSENT:{intent.client_order_id}")

    def _intent_age(self, intent_id: str, now: datetime) -> timedelta | None:
        row = self.ledger.writer.connection.execute(
            "SELECT created_at FROM order_intents WHERE account_scope_id = ? AND intent_id = ?",
            (self.ledger.scope_id, intent_id),
        ).fetchone()
        if row is None:
            return None
        from mike_trade.persistence.schema import from_db_timestamp

        return now - from_db_timestamp(row[0])

    # --------------------------------------------------- 2. order refresh

    def _refresh_orders(self, snapshot: BrokerSnapshot, report: ReconcileReport) -> None:
        open_ids = {order.broker_order_id for order in snapshot.open_orders}
        for order in snapshot.open_orders:
            if self.ledger.order_row(order.broker_order_id) is not None:
                self.ledger.upsert_order(order)
        for row in self.ledger.working_order_rows():
            if row.broker_order_id in open_ids:
                continue
            latest = self.gateway.get_order(row.broker_order_id)
            if latest is not None:
                self.ledger.upsert_order(latest)
                report.actions.append(f"ORDER_REFRESHED:{row.broker_order_id}:{latest.status.value}")

    # ---------------------------------------------------- 3. executions

    def _executions(self, report: ReconcileReport) -> tuple[Execution, ...]:
        cursor = self.ledger.activity_cursor()
        since = (
            self.clock.now() - timedelta(days=2)
            if cursor is None else cursor - self.activity_overlap
        )
        return self.gateway.executions(since)

    def _ingest(
        self, executions: tuple[Execution, ...], snapshot: BrokerSnapshot, report: ReconcileReport
    ) -> None:
        for execution in executions:
            if self.ledger.has_activity(execution.activity_id):
                continue
            row = self.ledger.order_row(execution.order_id)
            order: BrokerOrder | None = None
            if row is None:
                order = self.gateway.get_order(execution.order_id)
                if order is None:
                    report.drifts.append(f"EXECUTION_ORDER_UNKNOWN:{execution.activity_id}")
                    continue
                self.ledger.upsert_order(order)
                row = self.ledger.order_row(execution.order_id)
            assert row is not None
            fill = Fill(
                fill_id=execution.activity_id, broker_order_id=execution.order_id,
                account_scope_id=self.ledger.scope_id, symbol=execution.symbol,
                side=execution.side, quantity=execution.quantity, price=execution.price,
                fee=ZERO, filled_at=execution.occurred_at,
            )
            if row.intent_id is not None:
                try:
                    result = self.ledger.apply_fill(fill)
                except ValueError as exc:
                    report.drifts.append(f"FILL_REJECTED:{execution.activity_id}:{exc}")
                    continue
                if result.applied:
                    report.applied_fills.append(execution.activity_id)
                    self._closed(result.cycle, external=False)
                continue
            self._ingest_external(execution, fill, report)

    def _closed(self, cycle: PositionCycle | None, *, external: bool) -> None:
        if (
            cycle is not None and cycle.state is PositionCycleState.CLOSED
            and self.on_cycle_closed is not None
        ):
            self.on_cycle_closed(cycle, external)

    def _ingest_external(self, execution: Execution, fill: Fill, report: ReconcileReport) -> None:
        cycle = self.ledger.active_cycle(execution.symbol)
        payload = {
            "symbol": execution.symbol, "side": execution.side.value,
            "quantity": execution.quantity, "price": execution.price,
            "order_id": execution.order_id,
        }
        if (
            execution.side is OrderSide.SELL and cycle is not None
            and cycle.state in (PositionCycleState.OPEN, PositionCycleState.EXITING)
            and cycle.remaining_quantity > ZERO
        ):
            portion = min(execution.quantity, cycle.remaining_quantity)
            managed = Fill(
                fill.fill_id, fill.broker_order_id, fill.account_scope_id, fill.symbol,
                fill.side, portion, fill.price, fill.fee, fill.filled_at,
            )
            result = self.ledger.apply_external_fill(
                managed, cycle.cycle_id, "external_full_exit"
                if portion == cycle.remaining_quantity else "external_partial_exit",
            )
            if result.applied:
                self._closed(result.cycle, external=True)
                report.external_sales.append(execution.activity_id)
                report.actions.append(
                    f"EXTERNAL_SALE:{execution.symbol}:{portion}"
                    + ("" if portion == execution.quantity else f":unmanaged_part={execution.quantity - portion}")
                )
            return
        kind = "EXTERNAL_BUY" if execution.side is OrderSide.BUY else "EXTERNAL_SELL_UNMANAGED"
        if self.ledger.record_activity(execution.activity_id, kind, execution.occurred_at, payload):
            self.ledger.event("external_execution", execution.symbol, kind, payload,
                              idempotency_key=f"external:{execution.activity_id}")
            (report.external_buys if kind == "EXTERNAL_BUY" else report.actions).append(
                execution.activity_id if kind == "EXTERNAL_BUY" else f"{kind}:{execution.symbol}"
            )

    # ================================================ 4. per-symbol decisions

    async def _reconcile_symbol(
        self, symbol: str, snapshot: BrokerSnapshot, report: ReconcileReport
    ) -> SymbolState:
        state = SymbolState(symbol)
        cycle = self.ledger.active_cycle(symbol)
        position = snapshot.positions.get(symbol)
        broker_qty = ZERO if position is None else position.quantity
        state.broker_quantity = broker_qty
        if position is not None and position.side != "long":
            state.frozen = True
            state.unmanaged_quantity = broker_qty
            state.reasons.append("UNSUPPORTED_SHORT_POSITION")
            return state
        orders = snapshot.orders_for(symbol)
        rows = {r.broker_order_id: r for r in self.ledger.working_order_rows(symbol)}

        if cycle is None:
            await self._reap_orphans(symbol, orders, rows, report)
            if broker_qty > ZERO:
                state.unmanaged_quantity = broker_qty
                state.frozen = True
                state.reasons.append("UNMANAGED_HOLDING")
            foreign = [o for o in orders if o.broker_order_id not in rows]
            if foreign:
                state.frozen = True
                state.reasons.append("FOREIGN_OPEN_ORDER")
            return state

        state.cycle_id = cycle.cycle_id
        cycle_rows = self.ledger.order_rows_for_cycle(cycle.cycle_id)
        working = [r for r in cycle_rows if r.status in _WORKING_NAMES]
        entry_working = [r for r in working if r.side == "BUY"]
        exit_working = [r for r in working if r.side == "SELL" and r.purpose in _EXIT_PURPOSES]
        stops_working = [
            r for r in working if r.side == "SELL" and r.purpose == "PROTECTIVE_STOP"
        ]
        state.entry_pending = bool(entry_working)
        state.exit_pending = bool(exit_working)

        if cycle.state is PositionCycleState.PENDING_ENTRY:
            if not entry_working and not any(r.side == "BUY" and r.filled_quantity > ZERO for r in cycle_rows):
                self.ledger.abandon_cycle(cycle.cycle_id, "entry order ended without a fill")
                report.actions.append(f"CYCLE_ABANDONED:{cycle.cycle_id}")
                return state
            if broker_qty > ZERO and not entry_working:
                # Filled at the broker but the execution is not in the ledger yet (lag).
                state.managed_quantity = ZERO
                state.reasons.append("FILL_ACTIVITY_PENDING")
            return state

        # ----- an open (OPEN / EXITING) cycle: derive the managed quantity.
        pending_entry = self._unledgered(cycle_rows, "BUY")
        pending_exit = self._unledgered(cycle_rows, "SELL", exits_only=True)
        expected = cycle.remaining_quantity + pending_entry - pending_exit
        if expected < ZERO:
            expected = ZERO
        if broker_qty >= expected:
            managed = expected
            state.unmanaged_quantity = broker_qty - expected
            self._deficit_since.pop(symbol, None)
            if state.unmanaged_quantity > ZERO:
                state.reasons.append("UNMANAGED_EXCESS")
                self._report_once(report, symbol, "UNMANAGED_EXCESS", f"{state.unmanaged_quantity}")
        else:
            managed = broker_qty
            since = self._deficit_since.setdefault(symbol, self.clock.now())
            state.reasons.append("EXTERNAL_REDUCTION_PENDING")
            if self.clock.now() - since > self.lag_grace:
                state.frozen = True
                state.reasons.append("POSITION_MISMATCH_UNEXPLAINED")
                self._report_once(
                    report, symbol, "POSITION_MISMATCH_UNEXPLAINED",
                    f"ledger={expected} broker={broker_qty}",
                )
        state.managed_quantity = managed

        foreign = [o for o in orders if o.broker_order_id not in rows]
        if foreign:
            state.reasons.append("FOREIGN_OPEN_ORDER")

        if managed <= ZERO:
            # Flat at the broker.  The cycle closes only through recorded executions (never
            # fabricated); until then it stays open and the symbol is not re-entered.
            for stop in stops_working:
                await self._cancel_row(stop, report, "POSITION_FLAT")
            self.executor.retire(cycle.cycle_id)
            if cycle.remaining_quantity > ZERO:
                state.frozen = True
                state.reasons.append("AWAITING_EXECUTION_RECORDS")
            return state

        if exit_working:
            return state  # an exit is in flight; the close path owns the stop
        policy = self.policy_for(cycle)
        if policy is None:
            state.reasons.append("NO_PROTECTION_POLICY")
            return state
        await self._maintain_protection(cycle, managed, policy, stops_working, orders, state, report)
        return state

    def _unledgered(self, rows: list[OrderRow], side: str, *, exits_only: bool = False) -> Decimal:
        """Quantity the broker says an order of ours filled that the ledger has not recorded."""
        total = ZERO
        for row in rows:
            if row.side != side or row.filled_quantity <= ZERO:
                continue
            if exits_only and row.purpose not in _EXIT_PURPOSES and row.purpose != "PROTECTIVE_STOP":
                continue
            total += max(ZERO, row.filled_quantity - self.ledger.ledger_fill_quantity(row.broker_order_id))
        return total

    async def _maintain_protection(
        self, cycle: PositionCycle, managed: Decimal, policy: ProtectionPolicy,
        stops_working: list[OrderRow], orders: tuple[BrokerOrder, ...], state: SymbolState,
        report: ReconcileReport,
    ) -> None:
        runtime = self.executor.runtime(cycle, policy)
        by_id = {o.broker_order_id: o for o in orders}
        live = [by_id[r.broker_order_id] for r in stops_working if r.broker_order_id in by_id]
        reference = cycle.avg_entry_price
        assert reference is not None
        try:
            if runtime is None:
                self.executor.establish(cycle, managed, reference, policy)
                state.protected = True
                report.actions.append(f"PROTECTION_ESTABLISHED:{cycle.symbol}:{managed}")
                return
            if len(live) > 1:
                keep = next((o for o in live if o.broker_order_id == runtime.order_id), live[0])
                for extra in live:
                    if extra.broker_order_id != keep.broker_order_id:
                        await self._cancel_order(extra.broker_order_id, report, "DUPLICATE_PROTECTION")
                live = [keep]
            if not live:
                self.executor.coordinator.forget(cycle.cycle_id)
                self.executor.restore(runtime, managed, "protective stop missing at broker")
                state.protected = True
                report.actions.append(f"PROTECTION_RESTORED:{cycle.symbol}:{managed}")
                return
            stop = live[0]
            if runtime.order_id != stop.broker_order_id:
                runtime.order_id = stop.broker_order_id
                self.executor.coordinator.register_protective_stop(
                    position_id=cycle.cycle_id, symbol=cycle.symbol,
                    order_id=stop.broker_order_id, stop_price=runtime.stop_price,
                )
            covered = stop.quantity - stop.filled_quantity
            if covered != runtime.covered:
                runtime.covered = covered
            if stop.stop_price is not None and stop.stop_price < runtime.stop_price:
                # Someone loosened our stop at the broker: tighten it back, never lower it.
                await self.executor.reassert_price(runtime, managed)
                report.actions.append(f"STOP_RESTORED_PRICE:{cycle.symbol}")
            elif stop.stop_price is not None and stop.stop_price > runtime.stop_price:
                runtime.stop_price = stop.stop_price
                self.executor._persist_quietly(runtime)
            if covered != managed:
                await self.executor.resize(runtime, managed, "managed quantity changed")
                report.actions.append(f"PROTECTION_RESIZED:{cycle.symbol}:{covered}->{managed}")
            state.protected = True
        except ProtectionFailure as exc:
            state.protected = False
            state.frozen = True
            state.reasons.append(f"UNPROTECTED:{exc}")
            report.failures.append(f"{cycle.symbol}: {exc}")
            if self.escalate is not None:
                await self.escalate(cycle, f"protection could not be maintained: {exc}")

    async def _reap_orphans(
        self, symbol: str, orders: tuple[BrokerOrder, ...], rows: dict[str, OrderRow],
        report: ReconcileReport,
    ) -> None:
        """Cancel the engine's OWN protective orders left behind by a closed cycle."""
        for order in orders:
            row = rows.get(order.broker_order_id)
            if row is not None and row.purpose == "PROTECTIVE_STOP" and order.status in _WORKING:
                await self._cancel_order(order.broker_order_id, report, "CYCLE_CLOSED")

    async def _cancel_row(self, row: OrderRow, report: ReconcileReport, reason: str) -> None:
        await self._cancel_order(row.broker_order_id, report, reason)

    async def _cancel_order(self, order_id: str, report: ReconcileReport, reason: str) -> None:
        try:
            await self.executor.cancel_order(order_id)
            report.actions.append(f"ORDER_CANCELED:{order_id}:{reason}")
        except BrokerError as exc:
            report.failures.append(f"cancel {order_id}: {exc}")

    def _report_once(self, report: ReconcileReport, symbol: str, kind: str, detail: str) -> None:
        key = (symbol, kind)
        report.drifts.append(f"{kind}:{symbol}:{detail}")
        if key not in self._reported:
            self._reported.add(key)
            self.ledger.event("reconciliation", symbol, f"Drift{kind.title().replace('_', '')}",
                              {"symbol": symbol, "detail": detail})


_WORKING_NAMES = frozenset(status.value for status in _WORKING) | {"UNKNOWN"}
