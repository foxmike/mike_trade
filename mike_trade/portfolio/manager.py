"""Serialized portfolio coordinator for managed one-entry / full-exit position cycles.

A cycle is LONG (BUY -> SELL) or SHORT (SELL_SHORT -> BUY_TO_COVER); the roles are independent,
there is no automatic reversal and never a simultaneous long and short.  Short entries are
opt-in (``PortfolioManagerConfig.short_authorized``) and fail closed.

The coordinator owns one :class:`PositionCycle` per account/scenario and symbol and
routes strategy signals, fills, protection updates, operator commands and
risk/safety exits through a single mutation path: one ``asyncio.Lock`` per
account scope, held for the whole handler.  Two concurrent signals for one
symbol therefore cannot both reserve an entry.

Design choices:
1. Every collaborator is an injected async port (entry gate, budget, orders,
   protection, position verification, clock).  The coordinator never talks to a
   broker, and strategies never talk to the coordinator's ports.
2. Overlapping sells are prevented structurally: an exit first cancels any open
   entry remainder, then cancels/confirms protection, then (for a priority exit)
   cancels/confirms a working strategy sell, and finally caps the quantity at the
   broker-confirmed position.  Any unconfirmed step defers the exit; priority
   exits are remembered and retried on the next event for the symbol.
3. The cycle FSM has no "sell canceled" event.  A superseded or terminal-without-
   fill sell is therefore recorded through the FSM's reconciliation events
   (``RECONCILIATION_REQUIRED``/``RESOLVED``) and full broker reconciliation
   remains the job of the reconciler task, not this module.
4. Operator pause and the entry kill switch are coordinator flags consulted by
   the entry path alone; they are in-memory and are not restored on restart.
5. Budget release, the post-close cooldown and the eligibility close record run
   only after the cycle has reached ``CLOSED`` (zero position verified, no open
   sell or protective order).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Protocol
from uuid import uuid4

from mike_trade.controls.commands import ControlCommandType
from mike_trade.domain.protection import ProtectionStatus
from mike_trade.domain.signals import Signal, SignalAction
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import OrderSide, PositionDirection
from mike_trade.execution.intents import OrderPurpose
from mike_trade.execution.order_fsm import TERMINAL_STATUSES, OrderStatus
from mike_trade.portfolio.cycles import (
    CycleEvent,
    CycleState,
    CycleTransitionError,
    PositionCycle,
    apply_cycle_event,
)
from mike_trade.risk.session_policy import ExitKind, MinimumHoldPolicy

__all__ = [
    "PortfolioManager",
    "PortfolioManagerConfig",
    "PortfolioResult",
    "PortfolioSnapshot",
    "Disposition",
    "FillReport",
    "ProtectionUpdate",
    "OperatorCommand",
    "RiskExit",
    "EntryGateDecision",
    "EntryRequest",
    "EntryReservation",
    "OrderRequest",
    "OrderAck",
    "EntryGate",
    "EntryBudgetPort",
    "OrderPort",
    "ProtectionPort",
    "PositionPort",
]

_ZERO = Decimal("0")
_HELD_STATES = frozenset(
    {
        CycleState.PARTIALLY_OWNED,
        CycleState.PROTECTIVE_PENDING,
        CycleState.WATCHING_SELL,
        CycleState.SELL_SUBMITTING,
        CycleState.SELL_PENDING,
    }
)
_UNSAFE_STATES = frozenset({CycleState.RECONCILING, CycleState.ERROR, CycleState.UNMANAGED})
_ENTRY_TERMINAL = frozenset({OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED})
_UNRESOLVED_STATUSES = frozenset({OrderStatus.UNKNOWN, OrderStatus.SUSPENDED})
_SELL_STATES = frozenset({CycleState.SELL_SUBMITTING, CycleState.SELL_PENDING})
_PROTECTION_LIVE = frozenset(
    {
        ProtectionStatus.PENDING,
        ProtectionStatus.ACTIVE,
        ProtectionStatus.REPLACING,
        ProtectionStatus.UNKNOWN,
    }
)
_RISK_EXIT_KINDS = frozenset(
    {
        ExitKind.RISK_SAFETY,
        ExitKind.DAILY_LOSS,
        ExitKind.RECONCILIATION,
        ExitKind.END_OF_DAY,
        ExitKind.MANUAL_CLOSE,
        ExitKind.CLOSE_ALL,
        ExitKind.FLATTEN,
    }
)
_OPERATOR_EXITS = {
    ControlCommandType.CLOSE_POSITION: ExitKind.MANUAL_CLOSE,
    ControlCommandType.CLOSE_ALL: ExitKind.CLOSE_ALL,
    ControlCommandType.FLATTEN: ExitKind.FLATTEN,
}
_MANUAL_KINDS = frozenset({ExitKind.MANUAL_CLOSE, ExitKind.CLOSE_ALL, ExitKind.FLATTEN})


class Disposition(Enum):
    ACCEPTED = "ACCEPTED"
    IGNORED = "IGNORED"
    BLOCKED = "BLOCKED"
    DEFERRED = "DEFERRED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"
    RECONCILING = "RECONCILING"


# ---------------------------------------------------------------- value objects


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty str")
    return value.strip()


def _symbol(value: object) -> str:
    result = _text(value, "symbol").upper()
    if any(character.isspace() for character in result):
        raise ValueError("symbol must not contain whitespace")
    return result


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class PortfolioManagerConfig:
    """Coordinator policy; automatic end-of-day liquidation is off unless enabled."""

    minimum_hold: MinimumHoldPolicy = field(default_factory=MinimumHoldPolicy)
    post_close_cooldown: timedelta = timedelta(0)
    auto_eod_liquidation: bool = False
    #: Opt-in switch for ENTER_SHORT.  Off by default: a short entry is BLOCKED, never queued.
    short_authorized: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.short_authorized, bool):
            raise TypeError("short_authorized must be bool")
        if not isinstance(self.minimum_hold, MinimumHoldPolicy):
            raise TypeError("minimum_hold must be MinimumHoldPolicy")
        if not isinstance(self.post_close_cooldown, timedelta) or self.post_close_cooldown < (
            timedelta(0)
        ):
            raise ValueError("post_close_cooldown must be a non-negative timedelta")
        if not isinstance(self.auto_eod_liquidation, bool):
            raise TypeError("auto_eod_liquidation must be bool")


@dataclass(frozen=True, slots=True)
class FillReport:
    """A broker fill, or (without fill fields) a terminal order-status report."""

    symbol: str
    order_id: str
    side: OrderSide
    occurred_at: datetime
    fill_id: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    order_status: OrderStatus | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "order_id", _text(self.order_id, "order_id"))
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))
        if not isinstance(self.side, OrderSide):
            raise TypeError("side must be OrderSide")
        fill_fields = (self.fill_id, self.quantity, self.price)
        if any(item is None for item in fill_fields) and any(
            item is not None for item in fill_fields
        ):
            raise ValueError("fill_id, quantity and price are required together")
        if self.fill_id is None and self.order_status is None:
            raise ValueError("a report needs either fill fields or an order_status")

    @property
    def is_fill(self) -> bool:
        return self.fill_id is not None


@dataclass(frozen=True, slots=True)
class ProtectionUpdate:
    """Latest protective-order status as confirmed by the protection coordinator."""

    symbol: str
    status: ProtectionStatus
    occurred_at: datetime
    order_id: str | None = None
    quantity: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))
        if not isinstance(self.status, ProtectionStatus):
            raise TypeError("status must be ProtectionStatus")


@dataclass(frozen=True, slots=True)
class OperatorCommand:
    command_type: ControlCommandType
    account_scope_id: str
    symbol: str | None = None
    reason: str = "operator"

    def __post_init__(self) -> None:
        if not isinstance(self.command_type, ControlCommandType):
            raise TypeError("command_type must be ControlCommandType")
        object.__setattr__(self, "account_scope_id", _text(self.account_scope_id, "scope"))
        if self.symbol is not None:
            object.__setattr__(self, "symbol", _symbol(self.symbol))


@dataclass(frozen=True, slots=True)
class RiskExit:
    """A risk/safety handler's request to fully close one symbol."""

    symbol: str
    kind: ExitKind = ExitKind.RISK_SAFETY
    reason: str = "risk_exit"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        if self.kind not in _RISK_EXIT_KINDS:
            raise ValueError(f"{self.kind!r} is not a risk/safety exit kind")


@dataclass(frozen=True, slots=True)
class EntryGateDecision:
    """Eligibility/risk verdict; stale data and unresolved orders always block."""

    allowed: bool
    reasons: tuple[str, ...] = ()
    stale_data: bool = False
    unresolved_orders: bool = False
    protection_unknown: bool = False

    @property
    def blocked(self) -> bool:
        return (
            not self.allowed or self.stale_data or self.unresolved_orders or self.protection_unknown
        )

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        if not self.blocked:
            return ()
        flags = (
            (self.stale_data, "STALE_DATA"),
            (self.unresolved_orders, "UNRESOLVED_ORDERS"),
            (self.protection_unknown, "PROTECTION_UNKNOWN"),
        )
        reasons = (*self.reasons, *(name for active, name in flags if active))
        return reasons or ("ENTRY_NOT_ELIGIBLE",)


@dataclass(frozen=True, slots=True)
class EntryRequest:
    account_scope_id: str
    cycle_id: str
    signal: Signal


@dataclass(frozen=True, slots=True)
class EntryReservation:
    """Budget reservation and sizing result for one accepted entry."""

    intent_id: str
    client_order_id: str
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class OrderRequest:
    account_scope_id: str
    symbol: str
    cycle_id: str
    purpose: OrderPurpose
    side: OrderSide
    quantity: Decimal
    client_order_id: str
    intent_id: str | None = None
    strategy_id: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class OrderAck:
    order_id: str
    status: OrderStatus


@dataclass(frozen=True, slots=True)
class PortfolioResult:
    disposition: Disposition
    reason: str
    account_scope_id: str
    symbol: str | None
    cycle: PositionCycle | None
    audits: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    account_scope_id: str
    cycles: Mapping[str, PositionCycle]
    entries_paused: bool
    entries_disabled: bool
    pending_exits: Mapping[str, ExitKind]
    short_entries_authorized: bool = False

    def consulted_roles(self, symbol: str) -> frozenset[StrategyRole]:
        """Which strategy roles should be evaluated for ``symbol`` right now.

        Once any quantity is held only the exit role of the held direction is consulted
        (SELL for a long, COVER for a short).  BUY (and, when short selling is authorized,
        SHORT) strategies are consulted only for a flat, armed, entry-eligible cycle.
        """
        cycle = self.cycles.get(_symbol(symbol))
        if cycle is None or cycle.state is CycleState.WATCHING_BUY:
            if self.entries_paused or self.entries_disabled:
                return frozenset()
            roles = {StrategyRole.BUY}
            if self.short_entries_authorized:
                roles.add(StrategyRole.SHORT)
            return frozenset(roles)
        if cycle.remaining_qty > _ZERO:
            return frozenset(
                {StrategyRole.SELL if cycle.direction is PositionDirection.LONG
                 else StrategyRole.COVER}
            )
        return frozenset()


# ------------------------------------------------------------------------ ports


class EntryGate(Protocol):
    """Injected eligibility/risk gate (daily slots, cooldown, lockouts, cutoff, freshness)."""

    async def evaluate_entry(
        self, account_scope_id: str, symbol: str, strategy_id: str, now: datetime
    ) -> EntryGateDecision: ...

    async def record_entry_fill(self, cycle: PositionCycle) -> None: ...

    async def record_cycle_closed(self, cycle: PositionCycle) -> None: ...


class EntryBudgetPort(Protocol):
    async def reserve(self, request: EntryRequest) -> EntryReservation | None:
        """Reserve and size an entry, or return ``None`` when no budget is available."""
        ...

    async def apply_entry_fill(self, cycle: PositionCycle, report: FillReport) -> None: ...

    async def release_pending(self, cycle: PositionCycle) -> None:
        """Release the unfilled part of the reservation (entry canceled/rejected)."""
        ...

    async def close_cycle(self, cycle: PositionCycle) -> None:
        """Release the whole reservation; called only once the cycle is CLOSED."""
        ...


class OrderPort(Protocol):
    async def submit(self, request: OrderRequest) -> OrderAck: ...

    async def cancel(self, order_id: str) -> bool:
        """``True`` only when the order is confirmed terminal."""
        ...


class ProtectionPort(Protocol):
    async def cancel_and_confirm(self, cycle: PositionCycle) -> bool:
        """``True`` only when no protective order (or pending placement) remains."""
        ...

    async def ensure_protection(self, cycle: PositionCycle, committed_sell_qty: Decimal) -> None:
        """Request protection covering ``remaining_qty - committed_sell_qty``."""
        ...


class PositionPort(Protocol):
    async def confirmed_quantity(self, account_scope_id: str, symbol: str) -> Decimal | None:
        """Broker-confirmed long quantity, or ``None`` when it cannot be verified."""
        ...

    # Optional (only SHORT cycles need it; a port without it makes a short unverifiable and
    # therefore fails closed):
    #
    #     async def confirmed_short_quantity(self, account_scope_id, symbol) -> Decimal | None
    #
    # the broker-confirmed SHORT magnitude, ``0`` when not short, ``None`` when unverifiable.


class _Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class _OpenSell:
    order_id: str
    kind: ExitKind


@dataclass(frozen=True, slots=True)
class _PendingExit:
    kind: ExitKind
    reason: str


_Key = tuple[str, str]
_Outcome = tuple[Disposition, str]


class PortfolioManager:
    """Single serialized mutation path for all position cycles of each account."""

    def __init__(
        self,
        *,
        gate: EntryGate,
        budget: EntryBudgetPort,
        orders: OrderPort,
        protection: ProtectionPort,
        positions: PositionPort,
        clock: _Clock,
        config: PortfolioManagerConfig | None = None,
        audit_sink: Callable[[Mapping[str, object]], None] | None = None,
        id_factory: Callable[[], str] | None = None,
        cycles: Iterable[PositionCycle] = (),
    ) -> None:
        self._gate = gate
        self._budget = budget
        self._orders = orders
        self._protection = protection
        self._positions = positions
        self._clock = clock
        self._config = config or PortfolioManagerConfig()
        self._audit_sink = audit_sink
        self._new_id = id_factory or (lambda: uuid4().hex)
        self._locks: dict[str, asyncio.Lock] = {}
        self._cycles: dict[_Key, PositionCycle] = {
            (cycle.account_or_scenario_id, cycle.symbol): cycle for cycle in cycles
        }
        self._paused: set[str] = set()
        self._disabled: set[str] = set()
        self._open_sell: dict[_Key, _OpenSell] = {}
        self._dead_sells: set[str] = set()
        self._pending_exits: dict[_Key, _PendingExit] = {}
        self._unreleased: set[_Key] = set()

    # ---------------------------------------------------------------- public API

    async def handle_signal(self, account_scope_id: str, signal: Signal) -> PortfolioResult:
        key = self._key(account_scope_id, signal.symbol)
        async with self._lock(key[0]):
            now, audits, stale = self._now(), [], self._pending_exits.get(key)
            event_type = "PortfolioDecision"
            if signal.action in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT):
                direction = (
                    PositionDirection.LONG
                    if signal.action is SignalAction.ENTER_LONG
                    else PositionDirection.SHORT
                )
                outcome = await self._enter(key, signal, now, audits, direction)
                if outcome[0] is Disposition.IGNORED:
                    event_type = "BuySignalIgnored"
            elif signal.action in (SignalAction.EXIT_LONG, SignalAction.COVER_SHORT):
                wanted = (
                    PositionDirection.LONG
                    if signal.action is SignalAction.EXIT_LONG
                    else PositionDirection.SHORT
                )
                held = self._cycles.get(key)
                if held is not None and held.remaining_qty > _ZERO and held.direction is not wanted:
                    # a SELL never closes a short and a COVER never closes a long
                    outcome = (Disposition.IGNORED, "wrong_direction")
                else:
                    outcome = await self._exit(
                        key, ExitKind.STRATEGY, signal.reason, signal.instance_id, now, audits
                    )
            else:  # pragma: no cover - SignalAction is a closed set
                outcome = (Disposition.IGNORED, "signal_action_not_enabled")
            await self._settle(key, now, audits, stale)
            return self._finish(key, outcome, audits, event_type)

    async def handle_fill(self, account_scope_id: str, report: FillReport) -> PortfolioResult:
        key = self._key(account_scope_id, report.symbol)
        async with self._lock(key[0]):
            audits, stale = [], self._pending_exits.get(key)
            handler = self._on_fill if report.is_fill else self._on_order_status
            outcome = await handler(key, report, report.occurred_at, audits)
            await self._settle(key, self._now(), audits, stale)
            return self._finish(key, outcome, audits)

    async def handle_protection_update(
        self, account_scope_id: str, update: ProtectionUpdate
    ) -> PortfolioResult:
        key = self._key(account_scope_id, update.symbol)
        async with self._lock(key[0]):
            audits, stale = [], self._pending_exits.get(key)
            outcome = self._on_protection(key, update, audits)
            await self._settle(key, self._now(), audits, stale)
            return self._finish(key, outcome, audits)

    async def handle_operator_command(
        self, command: OperatorCommand
    ) -> tuple[PortfolioResult, ...]:
        scope = command.account_scope_id
        async with self._lock(scope):
            now, kind = self._now(), _OPERATOR_EXITS.get(command.command_type)
            if kind is None:
                audits: list[Mapping[str, object]] = []
                outcome = self._operator_flag(scope, command.command_type)
                return (self._finish((scope, None), outcome, audits),)
            if command.command_type is ControlCommandType.CLOSE_POSITION:
                if command.symbol is None:
                    raise ValueError("CLOSE_POSITION requires a symbol")
                symbols = [command.symbol]
            else:
                symbols = sorted(
                    symbol
                    for (cycle_scope, symbol), cycle in self._cycles.items()
                    if cycle_scope == scope and cycle.remaining_qty > _ZERO
                )
            results = []
            for symbol in symbols:
                key = (scope, symbol)
                audits, stale = [], self._pending_exits.get(key)
                outcome = await self._exit(key, kind, command.reason, None, now, audits)
                await self._settle(key, now, audits, stale)
                results.append(self._finish(key, outcome, audits))
            if not results:
                outcome = (Disposition.IGNORED, "no_open_positions")
                results.append(self._finish((scope, None), outcome, []))
            return tuple(results)

    async def handle_risk_exit(self, account_scope_id: str, request: RiskExit) -> PortfolioResult:
        key = self._key(account_scope_id, request.symbol)
        async with self._lock(key[0]):
            now, audits, stale = self._now(), [], self._pending_exits.get(key)
            if request.kind is ExitKind.END_OF_DAY and not self._config.auto_eod_liquidation:
                outcome = (Disposition.IGNORED, "eod_liquidation_disabled")
            else:
                outcome = await self._exit(key, request.kind, request.reason, None, now, audits)
            await self._settle(key, now, audits, stale)
            return self._finish(key, outcome, audits)

    def snapshot(self, account_scope_id: str) -> PortfolioSnapshot:
        scope = _text(account_scope_id, "account_scope_id")
        return PortfolioSnapshot(
            account_scope_id=scope,
            cycles=MappingProxyType(
                {sym: cycle for (cs, sym), cycle in self._cycles.items() if cs == scope}
            ),
            entries_paused=scope in self._paused,
            entries_disabled=scope in self._disabled,
            pending_exits=MappingProxyType(
                {
                    sym: pending.kind
                    for (cs, sym), pending in self._pending_exits.items()
                    if cs == scope
                }
            ),
            short_entries_authorized=self._config.short_authorized,
        )

    # ------------------------------------------------------------------ plumbing

    @staticmethod
    def _key(account_scope_id: str, symbol: str) -> _Key:
        return _text(account_scope_id, "account_scope_id"), _symbol(symbol)

    def _lock(self, scope: str) -> asyncio.Lock:
        return self._locks.setdefault(scope, asyncio.Lock())

    def _now(self) -> datetime:
        return _utc(self._clock.now(), "clock")

    def _apply(
        self,
        key: _Key,
        event_type: str,
        now: datetime,
        audits: list[Mapping[str, object]],
        **fields: object,
    ) -> PositionCycle:
        event = CycleEvent(event_type=event_type, occurred_at=now, **fields)  # type: ignore[arg-type]
        updated, cycle_audits = apply_cycle_event(self._cycles[key], event)
        self._cycles[key] = updated
        audits.extend(cycle_audits)
        return updated

    def _note(
        self, audits: list[Mapping[str, object]], key: _Key, event_type: str, reason: str
    ) -> None:
        audits.append(
            {
                "event_type": event_type,
                "account_or_scenario_id": key[0],
                "symbol": key[1],
                "reason": reason,
            }
        )

    def _finish(
        self,
        key: tuple[str, str | None],
        outcome: _Outcome,
        audits: list[Mapping[str, object]],
        event_type: str = "PortfolioDecision",
    ) -> PortfolioResult:
        disposition, reason = outcome
        scope, symbol = key
        cycle = None if symbol is None else self._cycles.get((scope, symbol))
        audits.append(
            {
                "event_type": event_type,
                "outcome": disposition.value.lower(),
                "reason": reason,
                "account_or_scenario_id": scope,
                "symbol": symbol,
                "cycle_id": None if cycle is None else cycle.cycle_id,
                "cycle_state": None if cycle is None else cycle.state.value,
            }
        )
        if self._audit_sink is not None:
            for audit in audits:
                self._audit_sink(audit)
        return PortfolioResult(disposition, reason, scope, symbol, cycle, tuple(audits))

    def _operator_flag(self, scope: str, command_type: ControlCommandType) -> _Outcome:
        flags = {
            ControlCommandType.PAUSE: (self._paused, True),
            ControlCommandType.RESUME: (self._paused, False),
            ControlCommandType.DISABLE_ENTRIES: (self._disabled, True),
            ControlCommandType.ENABLE_ENTRIES: (self._disabled, False),
        }
        if command_type not in flags:
            return Disposition.IGNORED, "unsupported_command"
        target, on = flags[command_type]
        target.add(scope) if on else target.discard(scope)
        return Disposition.ACCEPTED, command_type.value.lower()

    async def _confirmed(
        self, awaitable: object, key: _Key, audits: list[Mapping[str, object]], label: str
    ) -> bool:
        try:
            return bool(await awaitable)  # type: ignore[misc]
        except Exception as exc:  # port failure is "not confirmed", never "confirmed"
            self._note(audits, key, "PortCallFailed", f"{label}:{type(exc).__name__}")
            return False

    async def _confirmed_quantity(
        self, key: _Key, direction: PositionDirection = PositionDirection.LONG
    ) -> Decimal | None:
        """Broker-confirmed magnitude of the position in ``direction``; ``None`` if unverifiable.

        A SHORT is verified only through ``confirmed_short_quantity``; a position port that
        cannot report shorts leaves the quantity unverified, which defers/blocks (fail closed).
        """
        if direction is PositionDirection.LONG:
            call = self._positions.confirmed_quantity
        else:
            call = getattr(self._positions, "confirmed_short_quantity", None)
            if call is None:
                return None
        try:
            quantity = await call(*key)
        except Exception:
            return None
        return quantity if isinstance(quantity, Decimal) else None

    def _require_reconciliation(
        self, key: _Key, now: datetime, audits: list[Mapping[str, object]], reason: str
    ) -> None:
        try:
            self._apply(key, CycleEvent.RECONCILIATION_REQUIRED, now, audits, reason=reason)
        except CycleTransitionError as exc:
            self._note(audits, key, "ReconciliationNotApplicable", exc.reason)

    def _rearm(self, key: _Key, now: datetime, audits: list[Mapping[str, object]]) -> PositionCycle:
        updated = self._apply(
            key,
            CycleEvent.REARM,
            now,
            audits,
            new_cycle_id=self._new_id(),
            reason="rearm_after_confirmed_flat",
        )
        self._open_sell.pop(key, None)
        self._pending_exits.pop(key, None)
        return updated

    def _open_sell_for(self, key: _Key, cycle: PositionCycle) -> _OpenSell | None:
        tracked = self._open_sell.get(key)
        if tracked is not None:
            return tracked
        if cycle.has_open_sell_order and cycle.sell_order_ids[-1] not in self._dead_sells:
            return _OpenSell(cycle.sell_order_ids[-1], ExitKind.STRATEGY)
        return None

    # --------------------------------------------------------------------- entry

    async def _enter(
        self,
        key: _Key,
        signal: Signal,
        now: datetime,
        audits: list[Mapping[str, object]],
        direction: PositionDirection = PositionDirection.LONG,
    ) -> _Outcome:
        scope, symbol = key
        short = direction is PositionDirection.SHORT
        if short and not self._config.short_authorized:
            return Disposition.BLOCKED, "short_selling_not_authorized"
        cycle = self._cycles.get(key)
        if cycle is None:
            cycle = self._cycles[key] = PositionCycle(scope, symbol, self._new_id())
        if cycle.remaining_qty > _ZERO or cycle.state in _HELD_STATES:
            return Disposition.IGNORED, "position_held"
        if cycle.state in _UNSAFE_STATES or cycle.state is CycleState.DISABLED:
            return Disposition.BLOCKED, f"cycle_{cycle.state.value.lower()}"
        if cycle.state is CycleState.CLOSED:
            if key in self._unreleased:
                return Disposition.BLOCKED, "close_release_pending"
            if cycle.cooldown_until is not None and now < cycle.cooldown_until:
                return Disposition.IGNORED, "cooldown_active"
            cycle = self._rearm(key, now, audits)
        if cycle.state is not CycleState.WATCHING_BUY:
            return Disposition.IGNORED, f"cycle_{cycle.state.value.lower()}"
        if scope in self._disabled:
            return Disposition.BLOCKED, "entry_kill_switch"
        if scope in self._paused:
            return Disposition.BLOCKED, "operator_paused"
        if cycle.protection_status is ProtectionStatus.UNKNOWN:
            return Disposition.BLOCKED, "protection_unknown"
        decision = await self._gate.evaluate_entry(scope, symbol, signal.instance_id, now)
        if decision.blocked:
            return Disposition.BLOCKED, "gate:" + ",".join(decision.blocking_reasons)
        reservation = await self._budget.reserve(EntryRequest(scope, cycle.cycle_id, signal))
        if reservation is None:
            return Disposition.BLOCKED, "budget_unavailable"

        cycle = self._apply(
            key,
            CycleEvent.BUY_SIGNAL_ACCEPTED,
            now,
            audits,
            strategy_id=signal.instance_id,
            entry_intent_id=reservation.intent_id,
            target_entry_qty=reservation.quantity,
            side=OrderSide.SELL_SHORT if short else OrderSide.BUY,
            reason=signal.reason,
        )
        self._apply(
            key, CycleEvent.BUY_SUBMITTED, now, audits, order_id=reservation.client_order_id
        )
        request = OrderRequest(
            account_scope_id=scope,
            symbol=symbol,
            cycle_id=cycle.cycle_id,
            purpose=OrderPurpose.ENTRY_SHORT if short else OrderPurpose.ENTRY_LONG,
            side=OrderSide.SELL_SHORT if short else OrderSide.BUY,
            quantity=reservation.quantity,
            client_order_id=reservation.client_order_id,
            intent_id=reservation.intent_id,
            strategy_id=signal.instance_id,
            reason=signal.reason,
        )
        try:
            ack = await self._orders.submit(request)
        except Exception as exc:  # outcome unknown: keep the reservation, reconcile
            self._require_reconciliation(
                key, now, audits, f"entry_submit_unresolved:{type(exc).__name__}"
            )
            return Disposition.RECONCILING, "entry_submit_unresolved"
        if ack.status in _ENTRY_TERMINAL:
            disposition, reason = await self._entry_ended(key, ack.status, now, audits)
            if disposition is Disposition.ACCEPTED:
                return Disposition.REJECTED, "entry_rejected"
            return disposition, reason
        if ack.status in _UNRESOLVED_STATUSES:
            self._require_reconciliation(key, now, audits, "entry_ack_status_unresolved")
            return Disposition.RECONCILING, "entry_ack_unresolved"
        if ack.status not in {OrderStatus.SUBMITTING, OrderStatus.INTENT_PERSISTED}:
            acknowledged = (
                OrderStatus.PENDING if ack.status is OrderStatus.PENDING else (OrderStatus.ACCEPTED)
            )
            self._apply(
                key,
                CycleEvent.BUY_ACKNOWLEDGED,
                now,
                audits,
                order_id=ack.order_id,
                order_status=acknowledged,
            )
        return Disposition.ACCEPTED, "entry_submitted"

    async def _entry_ended(
        self,
        key: _Key,
        status: OrderStatus,
        now: datetime,
        audits: list[Mapping[str, object]],
    ) -> _Outcome:
        """Entry order ended without (more) fills: release budget, rearm if flat."""
        cycle = self._cycles[key]
        if (
            cycle.remaining_qty <= _ZERO
            and await self._confirmed_quantity(key, cycle.direction) != _ZERO
        ):
            self._require_reconciliation(key, now, audits, "entry_ended_position_unverified")
            return Disposition.RECONCILING, "entry_ended_position_unverified"
        event_type = (
            CycleEvent.ENTRY_EXPIRED if status is OrderStatus.EXPIRED else CycleEvent.ENTRY_CANCELED
        )
        try:
            updated = self._apply(key, event_type, now, audits, reason=f"entry_{status.value}")
        except CycleTransitionError as exc:
            self._require_reconciliation(key, now, audits, f"entry_end_rejected:{exc.reason}")
            return Disposition.RECONCILING, "entry_end_rejected_by_cycle"
        await self._budget.release_pending(updated)
        if updated.state is CycleState.CLOSED:
            self._rearm(key, now, audits)
            return Disposition.ACCEPTED, "entry_ended_flat_rearmed"
        return Disposition.ACCEPTED, "entry_remainder_released"

    # ---------------------------------------------------------------------- exit

    async def _exit(
        self,
        key: _Key,
        kind: ExitKind,
        reason: str,
        strategy_id: str | None,
        now: datetime,
        audits: list[Mapping[str, object]],
    ) -> _Outcome:
        """Submit one full-remaining exit.  Manual/risk exits bypass the minimum hold."""
        priority = kind is not ExitKind.STRATEGY
        cycle = self._cycles.get(key)
        if cycle is None or cycle.remaining_qty <= _ZERO:
            return Disposition.IGNORED, "no_position"

        def defer(code: str) -> _Outcome:
            if priority:
                self._pending_exits[key] = _PendingExit(kind, reason)
            return Disposition.DEFERRED, code

        if cycle.state in _UNSAFE_STATES:
            if priority and cycle.state is CycleState.RECONCILING:
                return defer("cycle_reconciling")
            return Disposition.BLOCKED, f"cycle_{cycle.state.value.lower()}"
        if not priority:
            if key in self._pending_exits:
                return Disposition.IGNORED, "priority_exit_pending"
            hold = self._config.minimum_hold.evaluate(
                now=now,
                first_confirmed_entry_fill_at=cycle.first_positive_fill_at,
                exit_kind=ExitKind.STRATEGY,
            )
            if hold.blocked:
                return Disposition.IGNORED, hold.reason.lower()
        open_sell = self._open_sell_for(key, cycle)
        if open_sell is not None and (not priority or open_sell.kind is not ExitKind.STRATEGY):
            return Disposition.IGNORED, "exit_already_in_flight"
        if open_sell is None and cycle.state in _SELL_STATES:
            self._require_reconciliation(key, now, audits, "exit_state_without_sell_order")
            return Disposition.RECONCILING, "exit_state_without_sell_order"

        # 1. No entry remainder may fill after the exit starts.
        if (
            cycle.entry_order_ids
            and cycle.entry_order_status is not None
            and cycle.entry_order_status not in TERMINAL_STATUSES
        ):
            canceled = await self._confirmed(
                self._orders.cancel(cycle.entry_order_ids[-1]), key, audits, "entry_cancel"
            )
            if not canceled:
                return defer("entry_cancel_unconfirmed")
            cycle = self._apply(
                key, CycleEvent.ENTRY_CANCELED, now, audits, reason="exit_cancels_entry_remainder"
            )
            await self._budget.release_pending(cycle)

        # 2. Protection must be gone (confirmed) before an overlapping sell exists.
        if cycle.has_open_protective_order or cycle.protection_status in _PROTECTION_LIVE:
            if not await self._confirmed(
                self._protection.cancel_and_confirm(cycle), key, audits, "protection_cancel"
            ):
                return defer("protection_cancel_unconfirmed")
            cycle = self._apply(
                key,
                CycleEvent.PROTECTION_CONFIRMED,
                now,
                audits,
                protection_status=ProtectionStatus.CANCELED,
                reason="exit_cancels_protection",
            )

        # 3. A priority exit supersedes a working strategy sell (cancel, then confirm).
        if open_sell is not None:
            if not await self._confirmed(
                self._orders.cancel(open_sell.order_id), key, audits, "strategy_sell_cancel"
            ):
                return defer("strategy_sell_cancel_unconfirmed")
            self._dead_sells.add(open_sell.order_id)
            self._open_sell.pop(key, None)
            self._apply(
                key,
                CycleEvent.RECONCILIATION_REQUIRED,
                now,
                audits,
                reason="priority_exit_supersedes_strategy_sell",
            )
            cycle = self._apply(
                key,
                CycleEvent.RECONCILIATION_RESOLVED,
                now,
                audits,
                resolved_state=CycleState.PROTECTIVE_PENDING,
                reason="strategy_sell_canceled_for_priority_exit",
            )

        # 4. Cap the quantity at the broker-confirmed position of the cycle's own direction:
        #    a close can never exceed it, so it can never open the opposite position.
        confirmed = await self._confirmed_quantity(key, cycle.direction)
        if confirmed is None:
            await self._reprotect(cycle, key, audits)
            return defer("position_unconfirmed")
        if confirmed <= _ZERO:
            self._require_reconciliation(key, now, audits, "broker_flat_while_cycle_open")
            return Disposition.RECONCILING, "broker_flat_while_cycle_open"
        quantity = min(cycle.remaining_qty, confirmed)
        if quantity != cycle.remaining_qty:
            self._note(audits, key, "ExitQuantityCapped", f"{quantity} of {cycle.remaining_qty}")

        self._apply(
            key,
            CycleEvent.SELL_SIGNAL_ACCEPTED,
            now,
            audits,
            strategy_id=strategy_id,
            reason=reason or kind.value,
        )
        client_order_id = self._new_id()
        self._apply(key, CycleEvent.SELL_SUBMITTED, now, audits, order_id=client_order_id)
        self._open_sell[key] = _OpenSell(client_order_id, kind)
        self._pending_exits.pop(key, None)
        request = OrderRequest(
            account_scope_id=key[0],
            symbol=key[1],
            cycle_id=cycle.cycle_id,
            purpose=self._purpose(kind, cycle.direction),
            side=cycle.exit_side,
            quantity=quantity,
            client_order_id=client_order_id,
            strategy_id=strategy_id,
            reason=reason,
        )
        try:
            ack = await self._orders.submit(request)
        except Exception as exc:
            self._require_reconciliation(
                key, now, audits, f"exit_submit_unresolved:{type(exc).__name__}"
            )
            return Disposition.RECONCILING, "exit_submit_unresolved"
        if ack.status in _ENTRY_TERMINAL or ack.status in _UNRESOLVED_STATUSES:
            self._require_reconciliation(key, now, audits, f"exit_ack_{ack.status.value.lower()}")
            return Disposition.RECONCILING, "exit_ack_unresolved"
        return Disposition.ACCEPTED, "exit_submitted"

    @staticmethod
    def _purpose(
        kind: ExitKind, direction: PositionDirection = PositionDirection.LONG
    ) -> OrderPurpose:
        if kind is ExitKind.STRATEGY:
            return (
                OrderPurpose.EXIT_LONG
                if direction is PositionDirection.LONG
                else OrderPurpose.EXIT_SHORT
            )
        if kind in _MANUAL_KINDS:
            return OrderPurpose.MANUAL_CLOSE
        return OrderPurpose.SAFETY_EXIT

    async def _reprotect(
        self, cycle: PositionCycle, key: _Key, audits: list[Mapping[str, object]]
    ) -> None:
        try:
            await self._protection.ensure_protection(cycle, _ZERO)
        except Exception as exc:
            self._note(audits, key, "PortCallFailed", f"ensure_protection:{type(exc).__name__}")

    # ------------------------------------------------------------ fills & status

    async def _on_fill(
        self, key: _Key, report: FillReport, now: datetime, audits: list[Mapping[str, object]]
    ) -> _Outcome:
        before = self._cycles.get(key)
        if before is None:
            return Disposition.REJECTED, "fill_without_cycle"
        try:
            updated = self._apply(
                key,
                CycleEvent.FILL_APPLIED,
                now,
                audits,
                fill_id=report.fill_id,
                order_id=report.order_id,
                side=report.side,
                quantity=report.quantity,
                price=report.price,
                reason="fill",
            )
        except CycleTransitionError as exc:
            self._require_reconciliation(key, now, audits, f"fill_rejected:{exc.reason}")
            return Disposition.RECONCILING, "fill_rejected_by_cycle"
        if updated is before:
            return Disposition.DUPLICATE, "duplicate_fill"
        if report.side.is_entry:
            await self._budget.apply_entry_fill(updated, report)
            if not before.entry_slot_consumed:
                await self._gate.record_entry_fill(updated)
            await self._protection.ensure_protection(updated, _ZERO)
        else:
            if not updated.has_open_sell_order:
                self._open_sell.pop(key, None)
            if updated.remaining_qty > _ZERO:
                working = self._open_sell_for(key, updated) is not None
                await self._protection.ensure_protection(
                    updated, updated.remaining_qty if working else _ZERO
                )
        return Disposition.ACCEPTED, "fill_applied"

    async def _on_order_status(
        self, key: _Key, report: FillReport, now: datetime, audits: list[Mapping[str, object]]
    ) -> _Outcome:
        cycle = self._cycles.get(key)
        if cycle is None:
            return Disposition.REJECTED, "status_without_cycle"
        status = report.order_status
        if status not in _ENTRY_TERMINAL:
            return Disposition.IGNORED, "non_terminal_status"
        assert status is not None
        if report.side.is_entry:
            if (
                report.order_id not in cycle.entry_order_ids
                or cycle.entry_order_status in TERMINAL_STATUSES
            ):
                return Disposition.IGNORED, "entry_order_not_open"
            return await self._entry_ended(key, status, now, audits)
        if (
            report.order_id in self._dead_sells
            or report.order_id not in cycle.sell_order_ids
            or cycle.remaining_qty <= _ZERO
        ):
            return Disposition.IGNORED, "sell_order_not_open"
        if key in self._pending_exits:
            return Disposition.ACCEPTED, "sell_ended_priority_exit_pending"
        self._dead_sells.add(report.order_id)
        self._open_sell.pop(key, None)
        self._require_reconciliation(key, now, audits, f"sell_{status.value.lower()}_without_fill")
        return Disposition.RECONCILING, "sell_ended_without_fill"

    def _on_protection(
        self, key: _Key, update: ProtectionUpdate, audits: list[Mapping[str, object]]
    ) -> _Outcome:
        cycle = self._cycles.get(key)
        if cycle is None:
            return Disposition.REJECTED, "protection_without_cycle"
        lost = update.status is ProtectionStatus.UNKNOWN and cycle.remaining_qty > _ZERO
        try:
            self._apply(
                key,
                CycleEvent.PROTECTION_LOST if lost else CycleEvent.PROTECTION_CONFIRMED,
                update.occurred_at,
                audits,
                order_id=update.order_id,
                protection_status=update.status,
                protection_qty=update.quantity,
                reason=f"protection_{update.status.value.lower()}",
            )
        except CycleTransitionError as exc:
            self._note(audits, key, "ProtectionUpdateRejected", exc.reason)
            return Disposition.REJECTED, "protection_update_rejected_by_cycle"
        return Disposition.ACCEPTED, "protection_updated"

    # -------------------------------------------------------------------- settle

    async def _settle(
        self,
        key: _Key,
        now: datetime,
        audits: list[Mapping[str, object]],
        stale_pending: _PendingExit | None,
    ) -> None:
        """Tail of every handler: finish a flat close, release budget, retry a priority exit."""
        cycle = self._cycles.get(key)
        if cycle is None:
            return
        if cycle.state is CycleState.CLOSING:
            await self._try_close(key, now, audits)
            cycle = self._cycles[key]
        if cycle.state is CycleState.CLOSED and key in self._unreleased:
            await self._release(key, cycle)
        pending = self._pending_exits.get(key)
        if pending is None:
            return
        if cycle.remaining_qty <= _ZERO:
            self._pending_exits.pop(key, None)
        elif pending is stale_pending:  # deferred by an earlier event, not by this one
            self._pending_exits.pop(key, None)
            await self._exit(key, pending.kind, pending.reason, None, now, audits)

    async def _try_close(
        self, key: _Key, now: datetime, audits: list[Mapping[str, object]]
    ) -> None:
        """CLOSING -> CLOSED only with zero verified position and no stale order."""
        cycle = self._cycles[key]
        try:
            if cycle.has_open_protective_order or cycle.has_open_sell_order:
                cleared = True
                if cycle.has_open_protective_order:
                    cleared = await self._confirmed(
                        self._protection.cancel_and_confirm(cycle), key, audits, "close_protection"
                    )
                if cleared and cycle.has_open_sell_order:
                    cleared = await self._confirmed(
                        self._orders.cancel(cycle.sell_order_ids[-1]), key, audits, "close_sell"
                    )
                if not cleared:
                    self._note(audits, key, "ClosePending", "stale_order_unconfirmed")
                    return
                self._apply(
                    key,
                    CycleEvent.RECONCILIATION_REQUIRED,
                    now,
                    audits,
                    reason="close_stale_orders",
                )
                self._apply(
                    key,
                    CycleEvent.RECONCILIATION_RESOLVED,
                    now,
                    audits,
                    resolved_state=CycleState.CLOSING,
                    reason="stale_orders_canceled_at_close",
                )
            confirmed = await self._confirmed_quantity(key, cycle.direction)
            if confirmed is None:
                self._note(audits, key, "ClosePending", "flat_unverified")
                return
            if confirmed > _ZERO:
                self._require_reconciliation(key, now, audits, "closed_cycle_broker_not_flat")
                return
            closed = self._apply(
                key,
                CycleEvent.CLOSE_CONFIRMED_FLAT,
                now,
                audits,
                cooldown_until=now + self._config.post_close_cooldown,
                reason="verified_flat",
            )
        except CycleTransitionError as exc:
            self._note(audits, key, "ClosePending", exc.reason)
            return
        self._open_sell.pop(key, None)
        self._pending_exits.pop(key, None)
        self._unreleased.add(key)
        await self._release(key, closed)

    async def _release(self, key: _Key, cycle: PositionCycle) -> None:
        """Budget release and eligibility close record, strictly after CLOSED."""
        await self._budget.close_cycle(cycle)
        await self._gate.record_cycle_closed(cycle)
        self._unreleased.discard(key)
