"""Immutable protective-stop policy and trailing-state contracts.

This module imports the trading contract only for static type checking; at
runtime, ``start_protection`` uses the small structural surface supplied by a
position cycle.

Design choice (the task's final design choice): ``with_covered_quantity``
treats the state's current coverage as the last confirmed maximum remaining
position.  It can resize downward to a newly confirmed remainder, while an
increase requires rebuilding protection from a freshly observed cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from mike_trade.domain.trading import PositionDirection

if TYPE_CHECKING:
    from mike_trade.domain.trading import PositionCycle

__all__ = [
    "ProtectionKind",
    "ProtectionStatus",
    "ProtectionPolicy",
    "TrailingStopState",
    "start_protection",
    "combined_sell_exposure",
    "combined_exit_exposure",
]

_ZERO = Decimal("0")


def _require_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty str")
    return value.strip()


def _require_symbol(value: object) -> str:
    symbol = _require_text(value, field="symbol").upper()
    if any(character.isspace() for character in symbol):
        raise ValueError("symbol must not contain whitespace")
    return symbol


def _require_decimal(
    value: object,
    *,
    field: str,
    minimum: Decimal = _ZERO,
    inclusive: bool = True,
) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError(f"{field} must be Decimal; got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    if value < minimum or (not inclusive and value == minimum):
        operator = ">=" if inclusive else ">"
        raise ValueError(f"{field} must be {operator} {minimum}")
    return value


def _optional_decimal(
    value: object, *, field: str, positive: bool = True
) -> Decimal | None:
    if value is None:
        return None
    return _require_decimal(value, field=field, minimum=_ZERO, inclusive=not positive)


def _optional_utc(value: object, *, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC")
    return value.astimezone(UTC)


class ProtectionKind(Enum):
    FIXED_STOP = "FIXED_STOP"
    BROKER_TRAILING_STOP = "BROKER_TRAILING_STOP"
    STEPPED_TRAILING_STOP = "STEPPED_TRAILING_STOP"
    FIXED_THEN_TRAILING_STOP = "FIXED_THEN_TRAILING_STOP"
    #: Michelle (corrected): fixed initial stop, then a monotonic move to breakeven once the
    #: market reaches ``activation_reference``; the EMA9 exit is application-managed.
    FIXED_THEN_BREAKEVEN_STOP = "FIXED_THEN_BREAKEVEN_STOP"


class ProtectionStatus(Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    REPLACING = "REPLACING"
    UNKNOWN = "UNKNOWN"
    CANCELED = "CANCELED"
    TRIGGERED = "TRIGGERED"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class ProtectionPolicy:
    kind: ProtectionKind
    stop_offset_percent: Decimal | None = None
    stop_offset_amount: Decimal | None = None
    trail_percent: Decimal | None = None
    trail_amount: Decimal | None = None
    step_size: Decimal | None = None
    activation_gain: Decimal | None = None
    require_broker_held: bool = True
    initial_stop_price: Decimal | None = None
    activation_reference: Decimal | None = None
    breakeven_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ProtectionKind):
            raise ValueError("kind must be ProtectionKind")
        for field in (
            "stop_offset_percent",
            "stop_offset_amount",
            "trail_percent",
            "trail_amount",
            "step_size",
            "initial_stop_price",
            "activation_reference",
            "breakeven_price",
        ):
            object.__setattr__(
                self, field, _optional_decimal(getattr(self, field), field=field)
            )
        object.__setattr__(
            self,
            "activation_gain",
            _optional_decimal(self.activation_gain, field="activation_gain", positive=False),
        )
        if not isinstance(self.require_broker_held, bool):
            raise ValueError("require_broker_held must be bool")

        stop_count = int(self.stop_offset_percent is not None) + int(
            self.stop_offset_amount is not None
        )
        trail_count = int(self.trail_percent is not None) + int(
            self.trail_amount is not None
        )
        if self.breakeven_price is not None and self.kind is not ProtectionKind.FIXED_THEN_BREAKEVEN_STOP:
            raise ValueError("breakeven_price is only valid for FIXED_THEN_BREAKEVEN_STOP")
        if self.kind is ProtectionKind.FIXED_THEN_BREAKEVEN_STOP:
            if (
                self.initial_stop_price is None
                or self.activation_reference is None
                or self.breakeven_price is None
            ):
                raise ValueError(
                    "FIXED_THEN_BREAKEVEN_STOP requires initial_stop_price, "
                    "activation_reference and breakeven_price"
                )
            if (
                stop_count or trail_count or self.step_size is not None
                or self.activation_gain is not None
            ):
                raise ValueError("FIXED_THEN_BREAKEVEN_STOP does not accept offset/trail/step fields")
            if not self.initial_stop_price < self.breakeven_price < self.activation_reference:
                raise ValueError(
                    "FIXED_THEN_BREAKEVEN_STOP requires initial_stop < breakeven < activation"
                )
        elif self.kind is ProtectionKind.FIXED_STOP:
            if stop_count != 1:
                raise ValueError("FIXED_STOP requires exactly one stop offset")
            if (
                trail_count or self.step_size is not None or self.activation_gain is not None
                or self.initial_stop_price is not None or self.activation_reference is not None
            ):
                raise ValueError("FIXED_STOP does not accept trailing or stepped fields")
        elif self.kind is ProtectionKind.BROKER_TRAILING_STOP:
            if trail_count != 1:
                raise ValueError("BROKER_TRAILING_STOP requires exactly one trail value")
            if (
                stop_count or self.step_size is not None or self.activation_gain is not None
                or self.initial_stop_price is not None or self.activation_reference is not None
            ):
                raise ValueError("BROKER_TRAILING_STOP accepts only a trail value")
        elif self.kind is ProtectionKind.STEPPED_TRAILING_STOP:
            if trail_count != 1:
                raise ValueError("STEPPED_TRAILING_STOP requires exactly one trail value")
            if self.step_size is None or self.activation_gain is None:
                raise ValueError("STEPPED_TRAILING_STOP requires step_size and activation_gain")
            if stop_count or self.initial_stop_price is not None or self.activation_reference is not None:
                raise ValueError("STEPPED_TRAILING_STOP does not accept fixed-stop fields")
        else:
            if self.trail_percent is None or self.trail_amount is not None:
                raise ValueError("FIXED_THEN_TRAILING_STOP requires trail_percent")
            if self.initial_stop_price is None or self.activation_reference is None:
                raise ValueError(
                    "FIXED_THEN_TRAILING_STOP requires initial_stop_price and activation_reference"
                )
            if stop_count or self.step_size is not None or self.activation_gain is not None:
                raise ValueError("FIXED_THEN_TRAILING_STOP does not accept offset/step fields")


@dataclass(frozen=True, slots=True)
class TrailingStopState:
    cycle_id: str
    account_scope_id: str
    symbol: str
    kind: ProtectionKind
    status: ProtectionStatus
    covered_quantity: Decimal
    stop_price: Decimal
    high_water_mark: Decimal
    direction: PositionDirection = PositionDirection.LONG
    broker_order_id: str | None = None
    last_confirmed_at: datetime | None = None

    def __post_init__(self) -> None:
        for field in ("cycle_id", "account_scope_id"):
            object.__setattr__(self, field, _require_text(getattr(self, field), field=field))
        object.__setattr__(self, "symbol", _require_symbol(self.symbol))
        if not isinstance(self.kind, ProtectionKind):
            raise ValueError("kind must be ProtectionKind")
        if not isinstance(self.status, ProtectionStatus):
            raise ValueError("status must be ProtectionStatus")
        if not isinstance(self.direction, PositionDirection):
            raise ValueError("direction must be PositionDirection")
        object.__setattr__(
            self,
            "covered_quantity",
            _require_decimal(
                self.covered_quantity,
                field="covered_quantity",
                minimum=_ZERO,
                inclusive=False,
            ),
        )
        object.__setattr__(
            self,
            "stop_price",
            _require_decimal(
                self.stop_price, field="stop_price", minimum=_ZERO, inclusive=False
            ),
        )
        object.__setattr__(
            self,
            "high_water_mark",
            _require_decimal(
                self.high_water_mark,
                field="high_water_mark",
                minimum=_ZERO,
                inclusive=False,
            ),
        )
        if (
            self.direction is PositionDirection.LONG
            and self.stop_price > self.high_water_mark
        ):
            raise ValueError("long stop_price must not exceed its favorable watermark")
        if (
            self.direction is PositionDirection.SHORT
            and self.stop_price < self.high_water_mark
        ):
            raise ValueError("short stop_price must not be below its favorable watermark")
        if self.broker_order_id is not None:
            object.__setattr__(
                self,
                "broker_order_id",
                _require_text(self.broker_order_id, field="broker_order_id"),
            )
        object.__setattr__(
            self,
            "last_confirmed_at",
            _optional_utc(self.last_confirmed_at, field="last_confirmed_at"),
        )

    def ratchet(self, new_stop_price: Decimal, observed_high: Decimal) -> TrailingStopState:
        new_stop = _require_decimal(
            new_stop_price, field="new_stop_price", minimum=_ZERO, inclusive=False
        )
        new_high = _require_decimal(
            observed_high, field="observed_high", minimum=_ZERO, inclusive=False
        )
        if self.direction is PositionDirection.LONG:
            if new_stop < self.stop_price:
                raise ValueError("a long protective stop must never move downward")
            if new_high < self.high_water_mark:
                raise ValueError("a long favorable watermark must never move downward")
            if new_stop > new_high:
                raise ValueError("long new_stop_price must not exceed the watermark")
        else:
            if new_stop > self.stop_price:
                raise ValueError("a short protective stop must never move upward")
            if new_high > self.high_water_mark:
                raise ValueError("a short favorable watermark must never move upward")
            if new_stop < new_high:
                raise ValueError("short new_stop_price must not be below the watermark")
        return replace(self, stop_price=new_stop, high_water_mark=new_high)

    def with_covered_quantity(self, remaining: Decimal) -> TrailingStopState:
        quantity = _require_decimal(
            remaining, field="remaining", minimum=_ZERO, inclusive=False
        )
        if quantity > self.covered_quantity:
            raise ValueError("covered quantity must not exceed confirmed remaining quantity")
        return replace(self, covered_quantity=quantity)


def _offset_price(
    fill_price: Decimal,
    *,
    percent: Decimal | None,
    amount: Decimal | None,
    direction: PositionDirection = PositionDirection.LONG,
) -> Decimal:
    sign = Decimal("-1") if direction is PositionDirection.LONG else Decimal("1")
    if amount is not None:
        return fill_price + sign * amount
    if percent is None:  # Protected by ProtectionPolicy validation.
        raise ValueError("a percent or amount offset is required")
    return fill_price * (Decimal("1") + sign * percent / Decimal("100"))


def start_protection(
    policy: ProtectionPolicy, cycle: "PositionCycle", fill_price: Decimal  # noqa: UP037
) -> TrailingStopState:
    """Create pending protection after a confirmed positive entry fill."""

    if not isinstance(policy, ProtectionPolicy):
        raise ValueError("policy must be ProtectionPolicy")
    price = _require_decimal(
        fill_price, field="fill_price", minimum=_ZERO, inclusive=False
    )
    entry_quantity = getattr(cycle, "entry_quantity", None)
    remaining_quantity = getattr(cycle, "remaining_quantity", None)
    if not isinstance(entry_quantity, Decimal) or entry_quantity <= _ZERO:
        raise ValueError("protection requires a positive filled entry quantity")
    if not isinstance(remaining_quantity, Decimal) or remaining_quantity <= _ZERO:
        raise ValueError("protection requires a positive remaining quantity")
    direction = getattr(cycle, "direction", PositionDirection.LONG)
    if not isinstance(direction, PositionDirection):
        raise ValueError("cycle direction must be PositionDirection")

    if policy.kind in (
        ProtectionKind.FIXED_THEN_TRAILING_STOP, ProtectionKind.FIXED_THEN_BREAKEVEN_STOP
    ):
        assert policy.initial_stop_price is not None
        stop = policy.initial_stop_price
    elif policy.kind is ProtectionKind.FIXED_STOP:
        stop = _offset_price(
            price,
            percent=policy.stop_offset_percent,
            amount=policy.stop_offset_amount,
            direction=direction,
        )
    else:
        stop = _offset_price(
            price,
            percent=policy.trail_percent,
            amount=policy.trail_amount,
            direction=direction,
        )
    stop = _require_decimal(stop, field="stop_price", minimum=_ZERO, inclusive=False)
    return TrailingStopState(
        cycle_id=_require_text(getattr(cycle, "cycle_id", None), field="cycle_id"),
        account_scope_id=_require_text(
            getattr(cycle, "account_scope_id", None), field="account_scope_id"
        ),
        symbol=_require_symbol(getattr(cycle, "symbol", None)),
        kind=policy.kind,
        status=ProtectionStatus.PENDING,
        covered_quantity=remaining_quantity,
        stop_price=stop,
        high_water_mark=price,
        direction=direction,
    )


def combined_sell_exposure(
    protective_qty: Decimal, pending_exit_qty: Decimal, position_qty: Decimal
) -> bool:
    """Report whether overlapping sell orders could create a short position."""

    protective = _require_decimal(protective_qty, field="protective_qty")
    pending = _require_decimal(pending_exit_qty, field="pending_exit_qty")
    position = _require_decimal(position_qty, field="position_qty")
    return protective + pending > position


def combined_exit_exposure(
    protective_qty: Decimal, pending_exit_qty: Decimal, position_qty: Decimal
) -> bool:
    """Direction-neutral alias: exits may be SELL or BUY_TO_COVER."""

    return combined_sell_exposure(protective_qty, pending_exit_qty, position_qty)
