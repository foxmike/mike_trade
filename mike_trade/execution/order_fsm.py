"""Provider-neutral order status transitions.

Status changes deliberately do not own executed quantity.  An event can carry
``filled_qty`` only when it represents a fill-bearing update, leaving the fill
ledger as the authoritative owner of quantity changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Literal

__all__ = [
    "OrderStatus",
    "OrderEvent",
    "TransitionResult",
    "IllegalOrderTransition",
    "apply_transition",
    "TERMINAL_STATUSES",
]


class OrderStatus(Enum):
    INTENT_PERSISTED = "INTENT_PERSISTED"
    SUBMITTING = "SUBMITTING"
    ACCEPTED = "ACCEPTED"
    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REPLACED = "REPLACED"
    SUSPENDED = "SUSPENDED"
    UNKNOWN = "UNKNOWN"


TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REPLACED,
    }
)


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """One normalized broker/order-lifecycle observation.

    ``broker_lookup`` is required when resolving ``UNKNOWN``.  ``broker_confirmed``
    distinguishes final broker state from a local cancel/replace request or an
    acknowledgement that has not yet confirmed the final state.
    """

    status: OrderStatus
    filled_qty: Decimal | None = None
    broker_lookup: bool = False
    broker_confirmed: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.status, OrderStatus):
            raise TypeError("status must be an OrderStatus")
        if not isinstance(self.broker_lookup, bool):
            raise TypeError("broker_lookup must be bool")
        if not isinstance(self.broker_confirmed, bool):
            raise TypeError("broker_confirmed must be bool")
        is_fill_status = self.status in {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
        }
        if self.filled_qty is None:
            return
        if not isinstance(self.filled_qty, Decimal):
            raise TypeError("filled_qty must be Decimal")
        if not self.filled_qty.is_finite() or self.filled_qty <= 0:
            raise ValueError("filled_qty must be a finite positive Decimal")
        if not is_fill_status:
            raise ValueError("filled_qty may change only through fill-bearing events")


class IllegalOrderTransition(ValueError):
    """Typed evidence that an event cannot change the current order state."""

    def __init__(self, current: OrderStatus, event: OrderEvent, reason: str) -> None:
        self.current = current
        self.event = event
        self.reason = reason
        super().__init__(
            f"illegal order transition {current.value} -> {event.status.value}: {reason}"
        )


@dataclass(frozen=True, slots=True)
class TransitionResult:
    previous_status: OrderStatus
    status: OrderStatus
    event: OrderEvent
    outcome: Literal["accepted", "ignored", "illegal"]
    error: IllegalOrderTransition | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome == "accepted"

    @property
    def ignored(self) -> bool:
        return self.outcome == "ignored"

    @property
    def illegal(self) -> bool:
        return self.outcome == "illegal"

    @property
    def filled_qty(self) -> Decimal | None:
        """Return fill data only when its fill-bearing transition was accepted."""
        return self.event.filled_qty if self.accepted else None

    def raise_for_illegal(self) -> None:
        if self.error is not None:
            raise self.error


# The table is the complete transition policy.  Request acknowledgements are
# represented by CANCEL_PENDING; REPLACED appears only as broker-confirmed state.
_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.INTENT_PERSISTED: frozenset({OrderStatus.SUBMITTING}),
    OrderStatus.SUBMITTING: frozenset(
        {
            OrderStatus.ACCEPTED,
            OrderStatus.PENDING,
            OrderStatus.REJECTED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.ACCEPTED: frozenset(
        {
            OrderStatus.PENDING,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.REPLACED,
            OrderStatus.SUSPENDED,
        }
    ),
    OrderStatus.PENDING: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.REPLACED,
            OrderStatus.SUSPENDED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.REPLACED,
            OrderStatus.SUSPENDED,
        }
    ),
    OrderStatus.CANCEL_PENDING: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.REPLACED,
            OrderStatus.SUSPENDED,
        }
    ),
    OrderStatus.SUSPENDED: frozenset(
        {
            OrderStatus.ACCEPTED,
            OrderStatus.PENDING,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.REPLACED,
        }
    ),
    OrderStatus.UNKNOWN: frozenset(
        {
            OrderStatus.ACCEPTED,
            OrderStatus.PENDING,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.REPLACED,
            OrderStatus.SUSPENDED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
    OrderStatus.REPLACED: frozenset(),
}

_PROGRESS_RANK = {
    OrderStatus.INTENT_PERSISTED: 0,
    OrderStatus.SUBMITTING: 1,
    OrderStatus.ACCEPTED: 2,
    OrderStatus.PENDING: 3,
    OrderStatus.PARTIALLY_FILLED: 4,
    OrderStatus.CANCEL_PENDING: 5,
}


def _illegal(current: OrderStatus, event: OrderEvent, reason: str) -> TransitionResult:
    error = IllegalOrderTransition(current, event, reason)
    return TransitionResult(current, current, event, "illegal", error)


def apply_transition(current: OrderStatus, event: OrderEvent) -> TransitionResult:
    """Apply one event without side effects, returning explicit disposition evidence."""
    if not isinstance(current, OrderStatus):
        raise TypeError("current must be an OrderStatus")
    if not isinstance(event, OrderEvent):
        raise TypeError("event must be an OrderEvent")

    target = event.status
    if target is current:
        return TransitionResult(current, current, event, "ignored")
    if current in TERMINAL_STATUSES:
        return _illegal(current, event, "terminal states are immutable")
    if current is OrderStatus.UNKNOWN and not event.broker_lookup:
        return _illegal(current, event, "UNKNOWN may be resolved only by broker lookup")
    if target in {OrderStatus.CANCELED, OrderStatus.REPLACED} and not event.broker_confirmed:
        return _illegal(current, event, f"{target.value} requires broker confirmation")
    if (
        current is OrderStatus.CANCEL_PENDING
        and target in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}
        and event.filled_qty is None
    ):
        return TransitionResult(current, current, event, "ignored")

    if target in _TRANSITIONS[current]:
        return TransitionResult(current, target, event, "accepted")

    current_rank = _PROGRESS_RANK.get(current)
    target_rank = _PROGRESS_RANK.get(target)
    if current_rank is not None and target_rank is not None and target_rank < current_rank:
        return TransitionResult(current, current, event, "ignored")

    return _illegal(current, event, "edge is absent from the transition table")
