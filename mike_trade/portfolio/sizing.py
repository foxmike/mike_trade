"""Pure, fail-closed position sizing for protected long and short entries."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from enum import Enum

from mike_trade.domain.protection import ProtectionKind

__all__ = [
    "SizingInput",
    "SizingResult",
    "SizingRejection",
    "PositionSizer",
    "size_long_entry",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")


class SizingRejection(Enum):
    """Explicit reasons that a valid sizing request produced no order."""

    SHORT_NOT_SUPPORTED = "SHORT_NOT_SUPPORTED"
    MARGIN_NOT_ENABLED = "MARGIN_NOT_ENABLED"
    MAX_PLANNED_LOSS_REQUIRED = "MAX_PLANNED_LOSS_REQUIRED"
    INVALID_PROTECTIVE_STOP = "INVALID_PROTECTIVE_STOP"
    PROTECTION_CANNOT_BE_ESTABLISHED = "PROTECTION_CANNOT_BE_ESTABLISHED"
    FRACTIONAL_PROTECTION_UNSUPPORTED = "FRACTIONAL_PROTECTION_UNSUPPORTED"
    INSUFFICIENT_BUYING_POWER = "INSUFFICIENT_BUYING_POWER"
    QUANTITY_ROUNDS_TO_ZERO = "QUANTITY_ROUNDS_TO_ZERO"
    BELOW_MINIMUM_ORDER_VALUE = "BELOW_MINIMUM_ORDER_VALUE"


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if value < _ZERO or (positive and value == _ZERO):
        operator = "> 0" if positive else ">= 0"
        raise ValueError(f"{name} must be {operator}")
    return value


def _optional_decimal(
    value: object, name: str, *, positive: bool = False
) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, name, positive=positive)


def _optional_finite_decimal(value: object, name: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


@dataclass(frozen=True, slots=True)
class SizingInput:
    """Inputs needed to size one protected entry without external state.

    ``supported_protection_combinations`` is deliberately explicit and
    fail-closed. Each tuple contains the selected protection kind and whether
    the order uses fractional-share precision. A missing tuple is unsupported.
    """

    entry_price: Decimal
    remaining_symbol_budget_usd: Decimal
    remaining_total_budget_usd: Decimal
    broker_buying_power_usd: Decimal
    reserve_usd: Decimal
    slippage_cushion_usd: Decimal
    quantity_increment: Decimal
    minimum_order_value_usd: Decimal
    protection_kind: ProtectionKind
    supported_protection_combinations: frozenset[tuple[ProtectionKind, bool]]
    max_planned_loss_usd: Decimal | None = None
    protective_stop_price: Decimal | None = None
    protective_stop_distance: Decimal | None = None
    require_max_planned_loss: bool = False
    margin_required: bool = False
    margin_enabled: bool = False
    side: str = "long"
    #: Short selling is opt-in: a short request is rejected (``SHORT_NOT_SUPPORTED``) unless it
    #: is explicitly authorized, and it always needs a margin account.
    short_selling_enabled: bool = False
    #: Own-cash requirement of a short as a multiple of its notional MINUS the locked proceeds:
    #: ``(multiplier - 1) x notional`` must fit in available buying power.  The sale proceeds are
    #: restricted collateral and are never counted as buying power.
    short_margin_multiplier: Decimal = Decimal("1.5")

    def __post_init__(self) -> None:
        for name in (
            "entry_price",
            "remaining_symbol_budget_usd",
            "remaining_total_budget_usd",
            "broker_buying_power_usd",
            "reserve_usd",
            "slippage_cushion_usd",
            "quantity_increment",
            "minimum_order_value_usd",
        ):
            positive = name in {"entry_price", "quantity_increment"}
            object.__setattr__(
                self, name, _decimal(getattr(self, name), name, positive=positive)
            )
        if self.quantity_increment > _ONE:
            raise ValueError("quantity_increment must be <= 1")

        object.__setattr__(
            self,
            "max_planned_loss_usd",
            _optional_decimal(
                self.max_planned_loss_usd,
                "max_planned_loss_usd",
                positive=True,
            ),
        )
        object.__setattr__(
            self,
            "protective_stop_price",
            _optional_finite_decimal(
                self.protective_stop_price, "protective_stop_price"
            ),
        )
        object.__setattr__(
            self,
            "protective_stop_distance",
            _optional_finite_decimal(
                self.protective_stop_distance, "protective_stop_distance"
            ),
        )
        if not isinstance(self.protection_kind, ProtectionKind):
            raise TypeError("protection_kind must be ProtectionKind")
        if not isinstance(self.supported_protection_combinations, frozenset):
            raise TypeError("supported_protection_combinations must be a frozenset")
        for combination in self.supported_protection_combinations:
            if (
                not isinstance(combination, tuple)
                or len(combination) != 2
                or not isinstance(combination[0], ProtectionKind)
                or not isinstance(combination[1], bool)
            ):
                raise TypeError(
                    "supported_protection_combinations entries must be "
                    "(ProtectionKind, bool) tuples"
                )
        for name in ("require_max_planned_loss", "margin_required", "margin_enabled"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")
        if not isinstance(self.side, str) or not self.side.strip():
            raise TypeError("side must be a non-empty string")
        object.__setattr__(self, "side", self.side.strip().lower())
        if not isinstance(self.short_selling_enabled, bool):
            raise TypeError("short_selling_enabled must be bool")
        object.__setattr__(
            self,
            "short_margin_multiplier",
            _decimal(self.short_margin_multiplier, "short_margin_multiplier", positive=True),
        )
        if self.short_margin_multiplier < _ONE:
            raise ValueError("short_margin_multiplier must be >= 1")


@dataclass(frozen=True, slots=True)
class SizingResult:
    """A sized order, or an explicit reason why no order can be placed."""

    quantity: Decimal
    notional: Decimal
    planned_loss: Decimal | None
    rejection: SizingRejection | None = None

    @property
    def accepted(self) -> bool:
        return self.rejection is None


def _rejected(
    reason: SizingRejection, *, planned_loss: Decimal | None = None
) -> SizingResult:
    return SizingResult(
        quantity=_ZERO,
        notional=_ZERO,
        planned_loss=planned_loss,
        rejection=reason,
    )


def _stop_distance(request: SizingInput) -> tuple[Decimal | None, bool]:
    """Return the validated stop distance and whether it is valid.

    A long's stop sits below the entry (the distance can never reach the entry price); a
    short's stop sits above it.
    """

    stop_price = request.protective_stop_price
    distance = request.protective_stop_distance
    short = request.side == "short"
    if stop_price is None and distance is None:
        return None, False
    if stop_price is not None:
        if stop_price <= _ZERO or (
            stop_price <= request.entry_price if short else stop_price >= request.entry_price
        ):
            return None, False
        price_distance = (
            stop_price - request.entry_price if short else request.entry_price - stop_price
        )
        if distance is not None and distance != price_distance:
            return None, False
        distance = price_distance
    elif distance is not None and not short and distance >= request.entry_price:
        return None, False
    if distance is None or distance <= _ZERO:
        return None, False
    return distance, True


class PositionSizer:
    """Stateless implementation of protected long- and short-entry sizing."""

    @staticmethod
    def size(request: SizingInput) -> SizingResult:
        if not isinstance(request, SizingInput):
            raise TypeError("request must be SizingInput")
        if request.side not in ("long", "short"):
            return _rejected(SizingRejection.SHORT_NOT_SUPPORTED)
        short = request.side == "short"
        if short and not request.short_selling_enabled:
            return _rejected(SizingRejection.SHORT_NOT_SUPPORTED)
        if (request.margin_required or short) and not request.margin_enabled:
            return _rejected(SizingRejection.MARGIN_NOT_ENABLED)
        if request.require_max_planned_loss and request.max_planned_loss_usd is None:
            return _rejected(SizingRejection.MAX_PLANNED_LOSS_REQUIRED)

        stop_distance, stop_is_valid = _stop_distance(request)
        if not stop_is_valid:
            reason = (
                SizingRejection.INVALID_PROTECTIVE_STOP
                if request.max_planned_loss_usd is not None
                else SizingRejection.PROTECTION_CANNOT_BE_ESTABLISHED
            )
            return _rejected(reason)

        fractional = request.quantity_increment < _ONE
        capability = (request.protection_kind, fractional)
        if capability not in request.supported_protection_combinations:
            reason = (
                SizingRejection.FRACTIONAL_PROTECTION_UNSUPPORTED
                if fractional
                else SizingRejection.PROTECTION_CANNOT_BE_ESTABLISHED
            )
            return _rejected(reason)

        broker_available = request.broker_buying_power_usd - request.reserve_usd
        if broker_available <= _ZERO:
            return _rejected(SizingRejection.INSUFFICIENT_BUYING_POWER)

        # Budgets are ceilings on GROSS exposure (a short counts like a long).  Buying power
        # funds a long's whole notional but only a short's own-cash margin (multiplier - 1).
        if short:
            margin_ratio = request.short_margin_multiplier - _ONE
            funded_notional = (
                broker_available / margin_ratio if margin_ratio > _ZERO else broker_available
            )
        else:
            funded_notional = broker_available
        spendable_notional = min(
            request.remaining_symbol_budget_usd,
            request.remaining_total_budget_usd,
            funded_notional,
        )
        spendable_notional = max(
            _ZERO, spendable_notional - request.slippage_cushion_usd
        )
        raw_quantity = spendable_notional / request.entry_price
        if request.max_planned_loss_usd is not None:
            # A configured loss cap can reach here only with a valid distance.
            assert stop_distance is not None
            raw_quantity = min(
                raw_quantity, request.max_planned_loss_usd / stop_distance
            )

        units = (raw_quantity / request.quantity_increment).to_integral_value(
            rounding=ROUND_DOWN
        )
        quantity = units * request.quantity_increment
        if quantity <= _ZERO:
            return _rejected(SizingRejection.QUANTITY_ROUNDS_TO_ZERO)

        notional = quantity * request.entry_price
        planned_loss = quantity * stop_distance if stop_distance is not None else None
        if notional < request.minimum_order_value_usd:
            return _rejected(
                SizingRejection.BELOW_MINIMUM_ORDER_VALUE,
                planned_loss=planned_loss,
            )
        return SizingResult(
            quantity=quantity,
            notional=notional,
            planned_loss=planned_loss,
        )


def size_long_entry(request: SizingInput) -> SizingResult:
    """Size a long entry using only values captured in ``request``."""

    return PositionSizer.size(request)
