"""Pure, restart-safe lifecycle for one managed long position cycle.

The composite state in this module deliberately does not replace the order
and protection state machines.  A cycle records their latest statuses and
uses them to make fail-closed lifecycle decisions (most notably closing).
Persistence adapters can append the returned audit mappings to the existing
domain-event journal; this module itself performs no I/O.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar

from mike_trade.domain.protection import ProtectionStatus
from mike_trade.domain.trading import OrderSide
from mike_trade.execution.order_fsm import TERMINAL_STATUSES, OrderStatus

__all__ = [
    "CycleState",
    "PositionCycle",
    "CycleEvent",
    "CycleTransitionError",
    "apply_cycle_event",
    "serialize_cycle",
    "deserialize_cycle",
]

_ZERO = Decimal("0")
_SCHEMA_VERSION = 1


class CycleState(Enum):
    DISABLED = "DISABLED"
    WATCHING_BUY = "WATCHING_BUY"
    ENTRY_RESERVED = "ENTRY_RESERVED"
    BUY_SUBMITTING = "BUY_SUBMITTING"
    BUY_PENDING = "BUY_PENDING"
    PARTIALLY_OWNED = "PARTIALLY_OWNED"
    PROTECTIVE_PENDING = "PROTECTIVE_PENDING"
    WATCHING_SELL = "WATCHING_SELL"
    SELL_SUBMITTING = "SELL_SUBMITTING"
    SELL_PENDING = "SELL_PENDING"
    CLOSING = "CLOSING"
    RECONCILING = "RECONCILING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"
    UNMANAGED = "UNMANAGED"
    CLOSED = "CLOSED"


def _text(value: object, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty str")
    return value.strip()


def _symbol(value: object) -> str:
    symbol = _text(value, "symbol")
    assert symbol is not None
    normalized = symbol.upper()
    if any(character.isspace() for character in normalized):
        raise ValueError("symbol must not contain whitespace")
    return normalized


def _decimal(
    value: object,
    field: str,
    *,
    optional: bool = False,
    positive: bool = False,
) -> Decimal | None:
    if value is None and optional:
        return None
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    if value < _ZERO or (positive and value == _ZERO):
        operator = "> 0" if positive else ">= 0"
        raise ValueError(f"{field} must be {operator}")
    return value


def _utc(value: object, field: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC")
    return value.astimezone(UTC)


def _ids(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{field} must be a tuple")
    result = tuple(_text(item, f"{field} item") for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{field} must not contain duplicates")
    return result  # type: ignore[return-value]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _open_order(order_ids: tuple[str, ...], status: OrderStatus | None) -> bool:
    return bool(order_ids) and (status is None or status not in TERMINAL_STATUSES)


def _open_protection(cycle: PositionCycle) -> bool:
    return bool(cycle.protective_order_ids) and cycle.protection_status not in {
        ProtectionStatus.CANCELED,
        ProtectionStatus.TRIGGERED,
        ProtectionStatus.UNSUPPORTED,
    }


@dataclass(frozen=True, slots=True)
class PositionCycle:
    """Immutable snapshot for one account/scenario and symbol lifecycle."""

    account_or_scenario_id: str
    symbol: str
    cycle_id: str
    state: CycleState = CycleState.WATCHING_BUY
    originating_buy_strategy_id: str | None = None
    entry_intent_id: str | None = None
    entry_order_ids: tuple[str, ...] = ()
    sell_order_ids: tuple[str, ...] = ()
    protective_order_ids: tuple[str, ...] = ()
    entry_order_status: OrderStatus | None = None
    sell_order_status: OrderStatus | None = None
    filled_entry_qty: Decimal = _ZERO
    remaining_qty: Decimal = _ZERO
    average_entry_price: Decimal | None = None
    filled_exit_qty: Decimal = _ZERO
    target_entry_qty: Decimal | None = None
    first_positive_fill_at: datetime | None = None
    entry_slot_consumed: bool = False
    protection_status: ProtectionStatus | None = None
    protection_qty: Decimal = _ZERO
    applied_fill_ids: tuple[str, ...] = ()
    paused_state: CycleState | None = None
    reconciliation_state: CycleState | None = None
    cooldown_until: datetime | None = None
    last_transition_at: datetime | None = None
    last_transition_reason: str = "initialized"
    version: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "account_or_scenario_id",
            _text(self.account_or_scenario_id, "account_or_scenario_id"),
        )
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "cycle_id", _text(self.cycle_id, "cycle_id"))
        if not isinstance(self.state, CycleState):
            raise TypeError("state must be CycleState")
        for field in ("originating_buy_strategy_id", "entry_intent_id"):
            object.__setattr__(self, field, _text(getattr(self, field), field, optional=True))
        for field in ("entry_order_ids", "sell_order_ids", "protective_order_ids"):
            object.__setattr__(self, field, _ids(getattr(self, field), field))
        all_order_ids = self.entry_order_ids + self.sell_order_ids + self.protective_order_ids
        if len(all_order_ids) != len(set(all_order_ids)):
            raise ValueError("linked order ids must be unique across purposes")

        for field in ("entry_order_status", "sell_order_status"):
            status = getattr(self, field)
            if status is not None and not isinstance(status, OrderStatus):
                raise TypeError(f"{field} must be OrderStatus or None")
        if self.protection_status is not None and not isinstance(
            self.protection_status, ProtectionStatus
        ):
            raise TypeError("protection_status must be ProtectionStatus or None")

        for field in (
            "filled_entry_qty",
            "remaining_qty",
            "filled_exit_qty",
            "protection_qty",
        ):
            object.__setattr__(self, field, _decimal(getattr(self, field), field))
        object.__setattr__(
            self,
            "average_entry_price",
            _decimal(
                self.average_entry_price,
                "average_entry_price",
                optional=True,
                positive=True,
            ),
        )
        object.__setattr__(
            self,
            "target_entry_qty",
            _decimal(self.target_entry_qty, "target_entry_qty", optional=True, positive=True),
        )
        if self.filled_entry_qty != self.filled_exit_qty + self.remaining_qty:
            raise ValueError("filled_entry_qty must equal filled_exit_qty plus remaining_qty")
        if (self.average_entry_price is None) != (self.filled_entry_qty == _ZERO):
            raise ValueError("average_entry_price is set exactly when entry quantity is positive")
        if self.target_entry_qty is not None and self.filled_entry_qty > self.target_entry_qty:
            raise ValueError("filled_entry_qty must not exceed target_entry_qty")

        object.__setattr__(
            self,
            "first_positive_fill_at",
            _utc(self.first_positive_fill_at, "first_positive_fill_at", optional=True),
        )
        object.__setattr__(
            self,
            "cooldown_until",
            _utc(self.cooldown_until, "cooldown_until", optional=True),
        )
        object.__setattr__(
            self,
            "last_transition_at",
            _utc(self.last_transition_at, "last_transition_at", optional=True),
        )
        if (self.first_positive_fill_at is None) != (self.filled_entry_qty == _ZERO):
            raise ValueError("first_positive_fill_at is set exactly once quantity is owned")
        if not isinstance(self.entry_slot_consumed, bool):
            raise TypeError("entry_slot_consumed must be bool")
        if self.entry_slot_consumed != (self.filled_entry_qty > _ZERO):
            raise ValueError("entry_slot_consumed is true exactly after a positive buy fill")

        object.__setattr__(
            self, "applied_fill_ids", _ids(self.applied_fill_ids, "applied_fill_ids")
        )
        for field in ("paused_state", "reconciliation_state"):
            value = getattr(self, field)
            if value is not None and not isinstance(value, CycleState):
                raise TypeError(f"{field} must be CycleState or None")
        if self.state is CycleState.PAUSED:
            if self.paused_state in {None, CycleState.PAUSED}:
                raise ValueError("PAUSED requires a non-PAUSED paused_state")
        elif self.paused_state is not None:
            raise ValueError("paused_state is valid only while PAUSED")
        if self.state is CycleState.RECONCILING and self.reconciliation_state in {
            None,
            CycleState.RECONCILING,
            CycleState.PAUSED,
        }:
            raise ValueError("RECONCILING requires its prior lifecycle state")
        if self.state is CycleState.PAUSED and self.paused_state is CycleState.RECONCILING:
            if self.reconciliation_state is None:
                raise ValueError("paused reconciliation requires reconciliation_state")
        elif self.state is not CycleState.RECONCILING and self.reconciliation_state is not None:
            raise ValueError("reconciliation_state is valid only while reconciling")

        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise ValueError("version must be an int >= 0")
        object.__setattr__(
            self,
            "last_transition_reason",
            _text(self.last_transition_reason, "last_transition_reason"),
        )
        if self.state is CycleState.CLOSED:
            if self.remaining_qty != _ZERO:
                raise ValueError("CLOSED requires remaining_qty == 0")
            if self.has_open_sell_order or self.has_open_protective_order:
                raise ValueError("CLOSED cannot retain an open sell or protective order")
        effective_state = self.paused_state if self.state is CycleState.PAUSED else self.state
        if self.remaining_qty > _ZERO and effective_state in {
            CycleState.DISABLED,
            CycleState.WATCHING_BUY,
            CycleState.ENTRY_RESERVED,
            CycleState.BUY_SUBMITTING,
            CycleState.BUY_PENDING,
            CycleState.CLOSED,
        }:
            raise ValueError(f"{effective_state.value} cannot hold a positive position")
        if (
            self.remaining_qty == _ZERO
            and self.filled_entry_qty > _ZERO
            and effective_state
            in {
                CycleState.PARTIALLY_OWNED,
                CycleState.PROTECTIVE_PENDING,
                CycleState.WATCHING_SELL,
                CycleState.SELL_SUBMITTING,
                CycleState.SELL_PENDING,
            }
        ):
            raise ValueError(f"{effective_state.value} requires remaining_qty > 0")

    @property
    def account_scope_id(self) -> str:
        """Compatibility alias for the repository's established scope naming."""
        return self.account_or_scenario_id

    @property
    def linked_order_ids(self) -> tuple[str, ...]:
        return self.entry_order_ids + self.sell_order_ids + self.protective_order_ids

    @property
    def has_open_sell_order(self) -> bool:
        return _open_order(self.sell_order_ids, self.sell_order_status)

    @property
    def has_open_protective_order(self) -> bool:
        return _open_protection(self)


_EVENT_TYPES = frozenset(
    {
        "BUY_SIGNAL_ACCEPTED",
        "ENTRY_RESERVED",
        "BUY_SUBMITTED",
        "BUY_ACKNOWLEDGED",
        "FILL_APPLIED",
        "ENTRY_CANCELED",
        "ENTRY_EXPIRED",
        "PROTECTION_CONFIRMED",
        "PROTECTION_LOST",
        "SELL_SIGNAL_ACCEPTED",
        "SELL_SUBMITTED",
        "CLOSE_CONFIRMED_FLAT",
        "OPERATOR_PAUSE",
        "OPERATOR_RESUME",
        "RECONCILIATION_REQUIRED",
        "RECONCILIATION_RESOLVED",
        "ERROR",
        "EXTERNAL_MANUAL_CLOSE_DETECTED",
        "REARM",
    }
)


@dataclass(frozen=True, slots=True)
class CycleEvent:
    """A validated member of the cycle FSM's closed input event set."""

    BUY_SIGNAL_ACCEPTED: ClassVar[str] = "BUY_SIGNAL_ACCEPTED"
    ENTRY_RESERVED: ClassVar[str] = "ENTRY_RESERVED"
    BUY_SUBMITTED: ClassVar[str] = "BUY_SUBMITTED"
    BUY_ACKNOWLEDGED: ClassVar[str] = "BUY_ACKNOWLEDGED"
    FILL_APPLIED: ClassVar[str] = "FILL_APPLIED"
    ENTRY_CANCELED: ClassVar[str] = "ENTRY_CANCELED"
    ENTRY_EXPIRED: ClassVar[str] = "ENTRY_EXPIRED"
    PROTECTION_CONFIRMED: ClassVar[str] = "PROTECTION_CONFIRMED"
    PROTECTION_LOST: ClassVar[str] = "PROTECTION_LOST"
    SELL_SIGNAL_ACCEPTED: ClassVar[str] = "SELL_SIGNAL_ACCEPTED"
    SELL_SUBMITTED: ClassVar[str] = "SELL_SUBMITTED"
    CLOSE_CONFIRMED_FLAT: ClassVar[str] = "CLOSE_CONFIRMED_FLAT"
    OPERATOR_PAUSE: ClassVar[str] = "OPERATOR_PAUSE"
    OPERATOR_RESUME: ClassVar[str] = "OPERATOR_RESUME"
    RECONCILIATION_REQUIRED: ClassVar[str] = "RECONCILIATION_REQUIRED"
    RECONCILIATION_RESOLVED: ClassVar[str] = "RECONCILIATION_RESOLVED"
    ERROR: ClassVar[str] = "ERROR"
    EXTERNAL_MANUAL_CLOSE_DETECTED: ClassVar[str] = "EXTERNAL_MANUAL_CLOSE_DETECTED"
    REARM: ClassVar[str] = "REARM"

    event_type: str
    occurred_at: datetime
    reason: str = ""
    strategy_id: str | None = None
    entry_intent_id: str | None = None
    order_id: str | None = None
    side: OrderSide | None = None
    fill_id: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    order_status: OrderStatus | None = None
    protection_status: ProtectionStatus | None = None
    protection_qty: Decimal | None = None
    target_entry_qty: Decimal | None = None
    cooldown_until: datetime | None = None
    resolved_state: CycleState | None = None
    new_cycle_id: str | None = None

    def __post_init__(self) -> None:
        raw_type = _text(self.event_type, "event_type")
        assert raw_type is not None
        event_type = raw_type.upper()
        if event_type not in _EVENT_TYPES:
            raise ValueError(f"unsupported cycle event type: {event_type}")
        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))
        if not isinstance(self.reason, str):
            raise TypeError("reason must be str")
        object.__setattr__(self, "reason", self.reason.strip())
        for field in (
            "strategy_id",
            "entry_intent_id",
            "order_id",
            "fill_id",
            "new_cycle_id",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field, optional=True))
        if self.side is not None and not isinstance(self.side, OrderSide):
            raise TypeError("side must be OrderSide or None")
        for field in ("quantity", "price", "protection_qty", "target_entry_qty"):
            object.__setattr__(
                self,
                field,
                _decimal(
                    getattr(self, field),
                    field,
                    optional=True,
                    positive=field in {"quantity", "price", "target_entry_qty"},
                ),
            )
        if self.order_status is not None and not isinstance(self.order_status, OrderStatus):
            raise TypeError("order_status must be OrderStatus or None")
        if self.protection_status is not None and not isinstance(
            self.protection_status, ProtectionStatus
        ):
            raise TypeError("protection_status must be ProtectionStatus or None")
        object.__setattr__(
            self,
            "cooldown_until",
            _utc(self.cooldown_until, "cooldown_until", optional=True),
        )
        if self.resolved_state is not None and not isinstance(self.resolved_state, CycleState):
            raise TypeError("resolved_state must be CycleState or None")
        if event_type == self.FILL_APPLIED:
            if None in (self.fill_id, self.side, self.quantity, self.price):
                raise ValueError("FILL_APPLIED requires fill_id, side, quantity, and price")


class CycleTransitionError(ValueError):
    """Typed evidence that a cycle event is illegal for the current snapshot."""

    def __init__(self, cycle: PositionCycle, event: CycleEvent, reason: str) -> None:
        self.cycle = cycle
        self.event = event
        self.current_state = cycle.state
        self.reason = reason
        super().__init__(
            f"illegal cycle transition {cycle.state.value} + {event.event_type}: {reason}"
        )


def _illegal(cycle: PositionCycle, event: CycleEvent, reason: str) -> None:
    raise CycleTransitionError(cycle, event, reason)


def _add_id(values: tuple[str, ...], value: str | None) -> tuple[str, ...]:
    if value is None or value in values:
        return values
    return (*values, value)


def _reason(event: CycleEvent) -> str:
    return event.reason or event.event_type.lower()


def _audit(
    before: PositionCycle,
    after: PositionCycle,
    event: CycleEvent,
    *,
    outcome: str = "applied",
) -> dict[str, object]:
    return {
        "event_type": "CycleTransitioned" if outcome == "applied" else "BuySignalIgnored",
        "input_event": event.event_type,
        "outcome": outcome,
        "account_or_scenario_id": before.account_or_scenario_id,
        "symbol": before.symbol,
        "cycle_id": before.cycle_id,
        "from_state": before.state.value,
        "to_state": after.state.value,
        "reason": _reason(event),
        "occurred_at": _iso(event.occurred_at),
        "version": after.version,
    }


def _advance(cycle: PositionCycle, event: CycleEvent, **changes: object) -> PositionCycle:
    return replace(
        cycle,
        **changes,
        last_transition_at=event.occurred_at,
        last_transition_reason=_reason(event),
        version=cycle.version + 1,
    )


def _owned(cycle: PositionCycle, event: CycleEvent) -> None:
    if cycle.remaining_qty <= _ZERO:
        _illegal(cycle, event, "event requires a positive managed long position")


def _derived_after_reconciliation(cycle: PositionCycle) -> CycleState:
    if cycle.remaining_qty == _ZERO:
        return CycleState.CLOSING if cycle.filled_entry_qty > _ZERO else CycleState.CLOSED
    if cycle.has_open_sell_order:
        return CycleState.SELL_PENDING
    if (
        cycle.protection_status is ProtectionStatus.ACTIVE
        and cycle.protection_qty == cycle.remaining_qty
    ):
        return CycleState.WATCHING_SELL
    return CycleState.PROTECTIVE_PENDING


def _apply_unpaused(
    cycle: PositionCycle, event: CycleEvent
) -> tuple[PositionCycle, tuple[dict[str, object], ...]]:
    kind = event.event_type

    if kind == CycleEvent.BUY_SIGNAL_ACCEPTED:
        if cycle.state is not CycleState.WATCHING_BUY:
            return cycle, (_audit(cycle, cycle, event, outcome="ignored"),)
        if event.strategy_id is None:
            _illegal(cycle, event, "buy signal requires strategy_id")
        updated = _advance(
            cycle,
            event,
            state=CycleState.ENTRY_RESERVED,
            originating_buy_strategy_id=event.strategy_id,
            entry_intent_id=event.entry_intent_id,
            target_entry_qty=event.target_entry_qty,
            entry_order_status=OrderStatus.INTENT_PERSISTED
            if event.entry_intent_id is not None
            else None,
        )

    elif kind == CycleEvent.ENTRY_RESERVED:
        if cycle.state is not CycleState.ENTRY_RESERVED:
            _illegal(cycle, event, "entry reservation follows one accepted buy signal")
        intent_id = event.entry_intent_id or cycle.entry_intent_id
        if intent_id is None:
            _illegal(cycle, event, "entry reservation requires entry_intent_id")
        if cycle.entry_intent_id is not None and cycle.entry_intent_id != intent_id:
            _illegal(cycle, event, "entry intent cannot be replaced")
        updated = _advance(
            cycle,
            event,
            entry_intent_id=intent_id,
            target_entry_qty=event.target_entry_qty or cycle.target_entry_qty,
            entry_order_status=OrderStatus.INTENT_PERSISTED,
        )

    elif kind == CycleEvent.BUY_SUBMITTED:
        if cycle.state is not CycleState.ENTRY_RESERVED:
            _illegal(cycle, event, "buy submit requires an entry reservation")
        if event.order_id is None:
            _illegal(cycle, event, "buy submit requires order_id")
        updated = _advance(
            cycle,
            event,
            state=CycleState.BUY_SUBMITTING,
            entry_order_ids=_add_id(cycle.entry_order_ids, event.order_id),
            entry_order_status=OrderStatus.SUBMITTING,
            target_entry_qty=event.target_entry_qty or cycle.target_entry_qty,
        )

    elif kind == CycleEvent.BUY_ACKNOWLEDGED:
        if cycle.state not in {CycleState.BUY_SUBMITTING, CycleState.BUY_PENDING}:
            _illegal(cycle, event, "buy acknowledgement requires a submitted buy")
        status = event.order_status or OrderStatus.ACCEPTED
        if status not in {OrderStatus.ACCEPTED, OrderStatus.PENDING}:
            _illegal(cycle, event, "buy acknowledgement status must be ACCEPTED or PENDING")
        updated = _advance(
            cycle,
            event,
            state=CycleState.BUY_PENDING,
            entry_order_ids=_add_id(cycle.entry_order_ids, event.order_id),
            entry_order_status=status,
        )

    elif kind == CycleEvent.FILL_APPLIED:
        assert event.fill_id is not None
        assert event.side is not None
        assert event.quantity is not None
        assert event.price is not None
        if event.fill_id in cycle.applied_fill_ids:
            return cycle, ()
        if event.side is OrderSide.BUY:
            if cycle.state not in {
                CycleState.ENTRY_RESERVED,
                CycleState.BUY_SUBMITTING,
                CycleState.BUY_PENDING,
                CycleState.PARTIALLY_OWNED,
                CycleState.PROTECTIVE_PENDING,
            }:
                _illegal(cycle, event, "buy fill is not associated with an open entry")
            if cycle.filled_exit_qty > _ZERO:
                _illegal(cycle, event, "buy fills are forbidden after exit execution begins")
            total = cycle.filled_entry_qty + event.quantity
            if cycle.target_entry_qty is not None and total > cycle.target_entry_qty:
                _illegal(cycle, event, "buy fill exceeds target_entry_qty")
            old_cost = cycle.filled_entry_qty * (cycle.average_entry_price or _ZERO)
            average = (old_cost + event.quantity * event.price) / total
            status = (
                OrderStatus.FILLED
                if cycle.target_entry_qty is not None and total == cycle.target_entry_qty
                else OrderStatus.PARTIALLY_FILLED
            )
            updated = _advance(
                cycle,
                event,
                state=CycleState.PROTECTIVE_PENDING,
                entry_order_ids=_add_id(cycle.entry_order_ids, event.order_id),
                entry_order_status=status,
                filled_entry_qty=total,
                remaining_qty=cycle.remaining_qty + event.quantity,
                average_entry_price=average,
                first_positive_fill_at=cycle.first_positive_fill_at or event.occurred_at,
                entry_slot_consumed=True,
                protection_status=ProtectionStatus.PENDING,
                applied_fill_ids=(*cycle.applied_fill_ids, event.fill_id),
            )
        else:
            _owned(cycle, event)
            if cycle.state not in {
                CycleState.PARTIALLY_OWNED,
                CycleState.PROTECTIVE_PENDING,
                CycleState.WATCHING_SELL,
                CycleState.SELL_SUBMITTING,
                CycleState.SELL_PENDING,
                CycleState.CLOSING,
            }:
                _illegal(cycle, event, "sell fill requires an owned position")
            if event.quantity > cycle.remaining_qty:
                _illegal(cycle, event, "sell fill exceeds remaining_qty (shorting is forbidden)")
            remaining = cycle.remaining_qty - event.quantity
            protective_fill = event.order_id in cycle.protective_order_ids
            updated = _advance(
                cycle,
                event,
                state=CycleState.CLOSING if remaining == _ZERO else CycleState.PROTECTIVE_PENDING,
                sell_order_ids=(
                    cycle.sell_order_ids
                    if protective_fill
                    else _add_id(cycle.sell_order_ids, event.order_id)
                ),
                sell_order_status=(
                    cycle.sell_order_status
                    if protective_fill
                    else (
                        OrderStatus.FILLED if remaining == _ZERO else OrderStatus.PARTIALLY_FILLED
                    )
                ),
                remaining_qty=remaining,
                filled_exit_qty=cycle.filled_exit_qty + event.quantity,
                protection_status=(
                    ProtectionStatus.TRIGGERED
                    if protective_fill
                    else (
                        cycle.protection_status if remaining == _ZERO else ProtectionStatus.PENDING
                    )
                ),
                protection_qty=_ZERO if protective_fill else cycle.protection_qty,
                applied_fill_ids=(*cycle.applied_fill_ids, event.fill_id),
            )

    elif kind in {CycleEvent.ENTRY_CANCELED, CycleEvent.ENTRY_EXPIRED}:
        if cycle.state not in {
            CycleState.ENTRY_RESERVED,
            CycleState.BUY_SUBMITTING,
            CycleState.BUY_PENDING,
            CycleState.PARTIALLY_OWNED,
            CycleState.PROTECTIVE_PENDING,
        }:
            _illegal(cycle, event, "entry cancellation requires an open entry lifecycle")
        terminal = (
            OrderStatus.CANCELED if kind == CycleEvent.ENTRY_CANCELED else OrderStatus.EXPIRED
        )
        if cycle.remaining_qty > _ZERO:
            state = CycleState.PROTECTIVE_PENDING
            protection = cycle.protection_status or ProtectionStatus.PENDING
        else:
            state = CycleState.CLOSED
            protection = cycle.protection_status
        updated = _advance(
            cycle,
            event,
            state=state,
            entry_order_status=terminal,
            protection_status=protection,
            cooldown_until=event.cooldown_until or cycle.cooldown_until,
        )

    elif kind == CycleEvent.PROTECTION_CONFIRMED:
        if cycle.state not in {
            CycleState.PARTIALLY_OWNED,
            CycleState.PROTECTIVE_PENDING,
            CycleState.WATCHING_SELL,
            CycleState.SELL_SUBMITTING,
            CycleState.SELL_PENDING,
            CycleState.CLOSING,
        }:
            _illegal(cycle, event, "protection confirmation requires an owned position")
        status = event.protection_status or ProtectionStatus.ACTIVE
        if cycle.remaining_qty == _ZERO and status not in {
            ProtectionStatus.CANCELED,
            ProtectionStatus.TRIGGERED,
            ProtectionStatus.UNSUPPORTED,
        }:
            _illegal(cycle, event, "flat positions accept only terminal protection status")
        if cycle.remaining_qty > _ZERO:
            _owned(cycle, event)
        quantity = event.protection_qty
        if status is ProtectionStatus.ACTIVE:
            if quantity is None:
                quantity = cycle.remaining_qty
            if quantity != cycle.remaining_qty:
                _illegal(cycle, event, "active protection must exactly cover remaining_qty")
        elif status in {ProtectionStatus.CANCELED, ProtectionStatus.TRIGGERED}:
            quantity = _ZERO
        elif quantity is None:
            quantity = cycle.protection_qty
        if cycle.remaining_qty == _ZERO:
            state = CycleState.CLOSING
        elif cycle.has_open_sell_order:
            state = CycleState.SELL_PENDING
        elif status is ProtectionStatus.ACTIVE:
            state = CycleState.WATCHING_SELL
        else:
            state = CycleState.PROTECTIVE_PENDING
        updated = _advance(
            cycle,
            event,
            state=state,
            protective_order_ids=_add_id(cycle.protective_order_ids, event.order_id),
            protection_status=status,
            protection_qty=quantity,
        )

    elif kind == CycleEvent.PROTECTION_LOST:
        _owned(cycle, event)
        if cycle.state not in {
            CycleState.PARTIALLY_OWNED,
            CycleState.PROTECTIVE_PENDING,
            CycleState.WATCHING_SELL,
            CycleState.SELL_SUBMITTING,
            CycleState.SELL_PENDING,
            CycleState.CLOSING,
        }:
            _illegal(cycle, event, "protection loss requires an owned position")
        updated = _advance(
            cycle,
            event,
            state=CycleState.PROTECTIVE_PENDING,
            protective_order_ids=_add_id(cycle.protective_order_ids, event.order_id),
            protection_status=event.protection_status or ProtectionStatus.UNKNOWN,
            protection_qty=event.protection_qty or _ZERO,
        )

    elif kind == CycleEvent.SELL_SIGNAL_ACCEPTED:
        _owned(cycle, event)
        if cycle.state not in {
            CycleState.PARTIALLY_OWNED,
            CycleState.PROTECTIVE_PENDING,
            CycleState.WATCHING_SELL,
        }:
            _illegal(cycle, event, "sell signal requires a position not already exiting")
        updated = _advance(cycle, event, state=CycleState.SELL_SUBMITTING)

    elif kind == CycleEvent.SELL_SUBMITTED:
        _owned(cycle, event)
        if cycle.state is not CycleState.SELL_SUBMITTING:
            _illegal(cycle, event, "sell submit requires an accepted sell signal")
        if event.order_id is None:
            _illegal(cycle, event, "sell submit requires order_id")
        status = event.order_status or OrderStatus.SUBMITTING
        if status not in {OrderStatus.SUBMITTING, OrderStatus.ACCEPTED, OrderStatus.PENDING}:
            _illegal(cycle, event, "sell submit status must be an open order status")
        updated = _advance(
            cycle,
            event,
            state=CycleState.SELL_PENDING,
            sell_order_ids=_add_id(cycle.sell_order_ids, event.order_id),
            sell_order_status=status,
        )

    elif kind == CycleEvent.CLOSE_CONFIRMED_FLAT:
        if cycle.remaining_qty != _ZERO:
            _illegal(cycle, event, "close requires remaining_qty == 0")
        if cycle.has_open_sell_order:
            _illegal(cycle, event, "close refused while a linked sell order is open")
        if cycle.has_open_protective_order:
            _illegal(cycle, event, "close refused while a linked protective order is open")
        updated = _advance(
            cycle,
            event,
            state=CycleState.CLOSED,
            cooldown_until=event.cooldown_until or cycle.cooldown_until,
            reconciliation_state=None,
        )

    elif kind == CycleEvent.OPERATOR_PAUSE:
        if cycle.state in {CycleState.PAUSED, CycleState.CLOSED}:
            _illegal(cycle, event, "cycle cannot be paused from its current state")
        updated = _advance(cycle, event, state=CycleState.PAUSED, paused_state=cycle.state)

    elif kind == CycleEvent.OPERATOR_RESUME:
        _illegal(cycle, event, "operator resume requires PAUSED")

    elif kind == CycleEvent.RECONCILIATION_REQUIRED:
        if cycle.state in {CycleState.CLOSED, CycleState.RECONCILING}:
            _illegal(cycle, event, "cycle cannot enter reconciliation from its current state")
        updated = _advance(
            cycle,
            event,
            state=CycleState.RECONCILING,
            reconciliation_state=cycle.state,
        )

    elif kind == CycleEvent.RECONCILIATION_RESOLVED:
        if cycle.state is not CycleState.RECONCILING:
            _illegal(cycle, event, "reconciliation resolution requires RECONCILING")
        state = event.resolved_state or _derived_after_reconciliation(cycle)
        if state in {CycleState.PAUSED, CycleState.RECONCILING}:
            _illegal(cycle, event, "invalid resolved lifecycle state")
        if cycle.remaining_qty > _ZERO and state not in {
            CycleState.PARTIALLY_OWNED,
            CycleState.PROTECTIVE_PENDING,
            CycleState.WATCHING_SELL,
            CycleState.SELL_SUBMITTING,
            CycleState.SELL_PENDING,
            CycleState.ERROR,
            CycleState.UNMANAGED,
        }:
            _illegal(cycle, event, "resolved state is incompatible with an owned position")
        if (
            cycle.remaining_qty == _ZERO
            and cycle.filled_entry_qty > _ZERO
            and state
            not in {
                CycleState.CLOSING,
                CycleState.CLOSED,
                CycleState.ERROR,
                CycleState.UNMANAGED,
            }
        ):
            _illegal(cycle, event, "resolved state is incompatible with a flat cycle")
        if state is CycleState.CLOSED and (
            cycle.has_open_sell_order or cycle.has_open_protective_order
        ):
            _illegal(cycle, event, "CLOSED requires all linked sell and protection orders terminal")
        changes: dict[str, object] = {"state": state, "reconciliation_state": None}
        if cycle.remaining_qty == _ZERO:
            if cycle.sell_order_ids and cycle.sell_order_status not in TERMINAL_STATUSES:
                changes["sell_order_status"] = OrderStatus.CANCELED
            if cycle.protective_order_ids:
                changes["protection_status"] = ProtectionStatus.CANCELED
                changes["protection_qty"] = _ZERO
        updated = _advance(cycle, event, **changes)

    elif kind == CycleEvent.ERROR:
        if cycle.state is CycleState.CLOSED:
            _illegal(cycle, event, "closed cycles are immutable until explicit rearm")
        updated = _advance(cycle, event, state=CycleState.ERROR)

    elif kind == CycleEvent.EXTERNAL_MANUAL_CLOSE_DETECTED:
        _owned(cycle, event)
        updated = _advance(
            cycle,
            event,
            state=CycleState.RECONCILING,
            remaining_qty=_ZERO,
            filled_exit_qty=cycle.filled_entry_qty,
            sell_order_status=(
                OrderStatus.UNKNOWN if cycle.sell_order_ids else cycle.sell_order_status
            ),
            protection_status=(
                ProtectionStatus.UNKNOWN if cycle.protective_order_ids else cycle.protection_status
            ),
            reconciliation_state=cycle.state,
        )

    elif kind == CycleEvent.REARM:
        if cycle.state is not CycleState.CLOSED:
            _illegal(cycle, event, "only CLOSED can be explicitly rearmed")
        if cycle.cooldown_until is not None and event.occurred_at < cycle.cooldown_until:
            _illegal(cycle, event, "post-close cooldown has not elapsed")
        updated = _advance(
            cycle,
            event,
            cycle_id=event.new_cycle_id or cycle.cycle_id,
            state=CycleState.WATCHING_BUY,
            originating_buy_strategy_id=None,
            entry_intent_id=None,
            entry_order_ids=(),
            sell_order_ids=(),
            protective_order_ids=(),
            entry_order_status=None,
            sell_order_status=None,
            filled_entry_qty=_ZERO,
            remaining_qty=_ZERO,
            average_entry_price=None,
            filled_exit_qty=_ZERO,
            target_entry_qty=None,
            first_positive_fill_at=None,
            entry_slot_consumed=False,
            protection_status=None,
            protection_qty=_ZERO,
            applied_fill_ids=(),
            cooldown_until=None,
        )

    else:  # pragma: no cover - CycleEvent validates the closed set
        _illegal(cycle, event, "unsupported event")

    return updated, (_audit(cycle, updated, event),)


_PAUSED_ENTRY_EVENTS = frozenset(
    {
        CycleEvent.BUY_SIGNAL_ACCEPTED,
        CycleEvent.ENTRY_RESERVED,
        CycleEvent.BUY_SUBMITTED,
        CycleEvent.BUY_ACKNOWLEDGED,
        CycleEvent.REARM,
    }
)


def apply_cycle_event(
    cycle: PositionCycle, event: CycleEvent
) -> tuple[PositionCycle, tuple[dict[str, object], ...]]:
    """Apply one event, returning the new immutable snapshot and audit mappings.

    Illegal events raise :class:`CycleTransitionError`; because the transition
    is pure, the caller's snapshot is always unchanged.  Duplicate fill ids
    return the identical object and no audit event.
    """
    if not isinstance(cycle, PositionCycle):
        raise TypeError("cycle must be PositionCycle")
    if not isinstance(event, CycleEvent):
        raise TypeError("event must be CycleEvent")

    if cycle.state is not CycleState.PAUSED:
        return _apply_unpaused(cycle, event)
    if event.event_type == CycleEvent.OPERATOR_RESUME:
        assert cycle.paused_state is not None
        updated = _advance(
            cycle,
            event,
            state=cycle.paused_state,
            paused_state=None,
        )
        return updated, (_audit(cycle, updated, event),)
    if event.event_type == CycleEvent.OPERATOR_PAUSE:
        _illegal(cycle, event, "cycle is already paused")
    if event.event_type in _PAUSED_ENTRY_EVENTS:
        _illegal(cycle, event, "entry transitions are blocked while paused")

    assert cycle.paused_state is not None
    working = replace(cycle, state=cycle.paused_state, paused_state=None)
    updated_working, audits = _apply_unpaused(working, event)
    updated = replace(
        updated_working,
        state=CycleState.PAUSED,
        paused_state=updated_working.state,
    )
    paused_audits = tuple(
        {
            **audit,
            "from_state": CycleState.PAUSED.value,
            "to_state": CycleState.PAUSED.value,
            "underlying_state": updated_working.state.value,
        }
        for audit in audits
    )
    return updated, paused_audits


def serialize_cycle(cycle: PositionCycle) -> dict[str, object]:
    """Return a deterministic JSON-safe snapshot with Decimal values as strings."""
    if not isinstance(cycle, PositionCycle):
        raise TypeError("cycle must be PositionCycle")
    return {
        "schema_version": _SCHEMA_VERSION,
        "account_or_scenario_id": cycle.account_or_scenario_id,
        "symbol": cycle.symbol,
        "cycle_id": cycle.cycle_id,
        "state": cycle.state.value,
        "originating_buy_strategy_id": cycle.originating_buy_strategy_id,
        "entry_intent_id": cycle.entry_intent_id,
        "entry_order_ids": list(cycle.entry_order_ids),
        "sell_order_ids": list(cycle.sell_order_ids),
        "protective_order_ids": list(cycle.protective_order_ids),
        "entry_order_status": (
            None if cycle.entry_order_status is None else cycle.entry_order_status.value
        ),
        "sell_order_status": (
            None if cycle.sell_order_status is None else cycle.sell_order_status.value
        ),
        "filled_entry_qty": str(cycle.filled_entry_qty),
        "remaining_qty": str(cycle.remaining_qty),
        "average_entry_price": (
            None if cycle.average_entry_price is None else str(cycle.average_entry_price)
        ),
        "filled_exit_qty": str(cycle.filled_exit_qty),
        "target_entry_qty": (
            None if cycle.target_entry_qty is None else str(cycle.target_entry_qty)
        ),
        "first_positive_fill_at": _iso(cycle.first_positive_fill_at),
        "entry_slot_consumed": cycle.entry_slot_consumed,
        "protection_status": (
            None if cycle.protection_status is None else cycle.protection_status.value
        ),
        "protection_qty": str(cycle.protection_qty),
        "applied_fill_ids": list(cycle.applied_fill_ids),
        "paused_state": None if cycle.paused_state is None else cycle.paused_state.value,
        "reconciliation_state": (
            None if cycle.reconciliation_state is None else cycle.reconciliation_state.value
        ),
        "cooldown_until": _iso(cycle.cooldown_until),
        "last_transition_at": _iso(cycle.last_transition_at),
        "last_transition_reason": cycle.last_transition_reason,
        "version": cycle.version,
    }


def _parse_time(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp or None")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc


def _optional_enum(enum_type: type[Enum], value: object, field: str) -> Enum | None:
    if value is None:
        return None
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc


def deserialize_cycle(data: Mapping[str, Any] | str) -> PositionCycle:
    """Restore a snapshot produced by :func:`serialize_cycle`."""
    if isinstance(data, str):
        try:
            loaded = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError("cycle snapshot is not valid JSON") from exc
        data = loaded
    if not isinstance(data, Mapping):
        raise TypeError("cycle snapshot must be a mapping or JSON object string")
    if data.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError(f"unsupported cycle schema_version: {data.get('schema_version')!r}")

    def ids(field: str) -> tuple[str, ...]:
        value = data.get(field, [])
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{field} must be a list")
        return tuple(value)

    def decimal(field: str, *, optional: bool = False) -> Decimal | None:
        value = data.get(field)
        if value is None and optional:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a Decimal string")
        try:
            return Decimal(value)
        except Exception as exc:
            raise ValueError(f"invalid Decimal for {field}") from exc

    try:
        return PositionCycle(
            account_or_scenario_id=data.get("account_or_scenario_id"),  # type: ignore[arg-type]
            symbol=data.get("symbol"),  # type: ignore[arg-type]
            cycle_id=data.get("cycle_id"),  # type: ignore[arg-type]
            state=CycleState(data.get("state")),
            originating_buy_strategy_id=data.get("originating_buy_strategy_id"),  # type: ignore[arg-type]
            entry_intent_id=data.get("entry_intent_id"),  # type: ignore[arg-type]
            entry_order_ids=ids("entry_order_ids"),
            sell_order_ids=ids("sell_order_ids"),
            protective_order_ids=ids("protective_order_ids"),
            entry_order_status=_optional_enum(
                OrderStatus, data.get("entry_order_status"), "entry_order_status"
            ),  # type: ignore[arg-type]
            sell_order_status=_optional_enum(
                OrderStatus, data.get("sell_order_status"), "sell_order_status"
            ),  # type: ignore[arg-type]
            filled_entry_qty=decimal("filled_entry_qty"),  # type: ignore[arg-type]
            remaining_qty=decimal("remaining_qty"),  # type: ignore[arg-type]
            average_entry_price=decimal("average_entry_price", optional=True),
            filled_exit_qty=decimal("filled_exit_qty"),  # type: ignore[arg-type]
            target_entry_qty=decimal("target_entry_qty", optional=True),
            first_positive_fill_at=_parse_time(
                data.get("first_positive_fill_at"), "first_positive_fill_at"
            ),
            entry_slot_consumed=data.get("entry_slot_consumed"),  # type: ignore[arg-type]
            protection_status=_optional_enum(
                ProtectionStatus, data.get("protection_status"), "protection_status"
            ),  # type: ignore[arg-type]
            protection_qty=decimal("protection_qty"),  # type: ignore[arg-type]
            applied_fill_ids=ids("applied_fill_ids"),
            paused_state=_optional_enum(CycleState, data.get("paused_state"), "paused_state"),  # type: ignore[arg-type]
            reconciliation_state=_optional_enum(
                CycleState, data.get("reconciliation_state"), "reconciliation_state"
            ),  # type: ignore[arg-type]
            cooldown_until=_parse_time(data.get("cooldown_until"), "cooldown_until"),
            last_transition_at=_parse_time(data.get("last_transition_at"), "last_transition_at"),
            last_transition_reason=data.get("last_transition_reason"),  # type: ignore[arg-type]
            version=data.get("version"),  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid cycle snapshot: {exc}") from exc
