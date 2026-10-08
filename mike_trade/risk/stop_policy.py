"""Provider-neutral protective-order policy and capability evaluation.

Capability declarations are intentionally explicit and default to unsupported.
An adapter must advertise both support and, for LIVE execution, the combinations
that have already been demonstrated in PAPER.  This keeps an unknown provider
combination from becoming an implicit live-trading experiment.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from mike_trade.domain.trading import OrderType, TimeInForce
from mike_trade.runtime import ExecutionMode

__all__ = [
    "ProtectionMode",
    "ProtectionPolicy",
    "BrokerProtectionCapabilities",
    "ProtectionEligibility",
    "evaluate_policy",
]

_ZERO = Decimal("0")
_STOP_MARKET = "STOP_MARKET"
_STOP_LIMIT = "STOP_LIMIT"
_TRAILING_STOP = "TRAILING_STOP"
_TRAIL_PRICE = "TRAIL_PRICE"
_TRAIL_PERCENT = "TRAIL_PERCENT"
_ALLOWED = "ALLOWED"
_REJECTED = "REJECTED"

_RISK_NOTES = (
    "A stop trigger price is not a guaranteed execution price or maximum loss.",
    "A stop-limit order may not fill.",
    "DAY TIF expires and leaves an overnight residual unless protection is renewed.",
)


class ProtectionMode(StrEnum):
    FIXED = "FIXED"
    TRAILING_NATIVE = "TRAILING_NATIVE"
    TRAILING_STEPPED = "TRAILING_STEPPED"


def _positive_decimal(value: object, name: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite() or value <= _ZERO:
        raise ValueError(f"{name} must be a finite positive Decimal")
    return value


def _order_semantics(value: object) -> str:
    raw = value.value if isinstance(value, OrderType) else value
    if not isinstance(raw, str):
        raise TypeError("order_semantics must be STOP_MARKET or STOP_LIMIT")
    normalized = raw.strip().upper()
    if normalized == OrderType.STOP.value:
        normalized = _STOP_MARKET
    if normalized not in {_STOP_MARKET, _STOP_LIMIT}:
        raise ValueError("order_semantics must be STOP_MARKET or STOP_LIMIT")
    return normalized


def _tif(value: object) -> TimeInForce:
    if isinstance(value, TimeInForce):
        return value
    if isinstance(value, str):
        try:
            return TimeInForce(value.strip().upper())
        except ValueError:
            pass
    raise ValueError("time_in_force must be DAY or GTC")


def _mode_value(value: object) -> str:
    raw = getattr(value, "value", value)
    if not isinstance(raw, str) or raw.upper() not in {item.value for item in ExecutionMode}:
        raise ValueError("execution_mode must be BACKTEST, SHADOW, PAPER, or LIVE")
    return raw.upper()


def _normalize_order_types(values: Iterable[object], name: str) -> frozenset[str]:
    try:
        normalized: set[str] = set()
        for value in values:
            raw = value.value if isinstance(value, OrderType) else value
            if isinstance(raw, str) and raw.strip().upper() == _TRAILING_STOP:
                normalized.add(_TRAILING_STOP)
            else:
                normalized.add(_order_semantics(value))
        return frozenset(normalized)
    except TypeError as exc:
        raise TypeError(f"{name} must be an iterable of order types") from exc


def _normalize_tifs(values: Iterable[object], name: str) -> frozenset[TimeInForce]:
    try:
        return frozenset(_tif(value) for value in values)
    except TypeError as exc:
        raise TypeError(f"{name} must be an iterable of TIF values") from exc


def _normalize_combinations(
    values: Iterable[tuple[object, object]], name: str
) -> frozenset[tuple[str, TimeInForce]]:
    result: set[tuple[str, TimeInForce]] = set()
    try:
        for order_type, tif in values:
            normalized = _normalize_order_types((order_type,), name)
            result.add((next(iter(normalized)), _tif(tif)))
    except (TypeError, ValueError) as exc:
        raise type(exc)(f"invalid {name}: {exc}") from exc
    return frozenset(result)


@dataclass(frozen=True, slots=True)
class ProtectionPolicy:
    """One long-position protection rule.

    ``fixed_reference`` is a provider-neutral reference name.  The default is
    the broker-confirmed average fill; configured systems may use another
    documented reference and resolve it before broker submission.
    """

    mode: ProtectionMode
    required: bool = True
    order_semantics: str | OrderType = _STOP_MARKET
    limit_offset: Decimal | None = None
    fixed_reference: str = "CONFIRMED_FILL_PRICE"
    distance_percent: Decimal | None = None
    distance_usd: Decimal | None = None
    atr_multiple: Decimal | None = None
    support_level: Decimal | None = None
    trail_percent: Decimal | None = None
    trail_price: Decimal | None = None
    time_in_force: TimeInForce | str = TimeInForce.GTC
    allow_fractional_protection: bool = False
    documented_fractional_risk_ack: str | None = None
    linked: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ProtectionMode):
            try:
                object.__setattr__(self, "mode", ProtectionMode(str(self.mode).upper()))
            except (TypeError, ValueError) as exc:
                raise ValueError("mode must be a ProtectionMode") from exc
        for name in ("required", "allow_fractional_protection", "linked"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")
        object.__setattr__(self, "order_semantics", _order_semantics(self.order_semantics))
        object.__setattr__(self, "time_in_force", _tif(self.time_in_force))
        if not isinstance(self.fixed_reference, str) or not self.fixed_reference.strip():
            raise ValueError("fixed_reference must be a non-empty string")
        object.__setattr__(self, "fixed_reference", self.fixed_reference.strip().upper())

        for name in (
            "limit_offset",
            "distance_percent",
            "distance_usd",
            "atr_multiple",
            "support_level",
            "trail_percent",
            "trail_price",
        ):
            object.__setattr__(self, name, _positive_decimal(getattr(self, name), name))

        if self.order_semantics == _STOP_LIMIT and self.limit_offset is None:
            raise ValueError("STOP_LIMIT requires limit_offset")
        if self.order_semantics == _STOP_MARKET and self.limit_offset is not None:
            raise ValueError("STOP_MARKET does not accept limit_offset")

        fixed_count = sum(
            value is not None
            for value in (
                self.distance_percent,
                self.distance_usd,
                self.atr_multiple,
                self.support_level,
            )
        )
        trail_count = int(self.trail_percent is not None) + int(self.trail_price is not None)
        if self.mode is ProtectionMode.FIXED:
            if fixed_count != 1:
                raise ValueError("FIXED requires exactly one distance definition")
            if trail_count:
                raise ValueError("FIXED does not accept native trailing distances")
        elif self.mode is ProtectionMode.TRAILING_NATIVE:
            if trail_count != 1:
                raise ValueError(
                    "TRAILING_NATIVE requires exactly one of trail_price or trail_percent"
                )
            if fixed_count:
                raise ValueError("TRAILING_NATIVE does not accept fixed distances")

        ack = self.documented_fractional_risk_ack
        if ack is not None:
            if not isinstance(ack, str) or not ack.strip():
                raise ValueError("documented_fractional_risk_ack must be non-empty when set")
            object.__setattr__(self, "documented_fractional_risk_ack", ack.strip())


@dataclass(frozen=True, slots=True)
class BrokerProtectionCapabilities:
    """Explicit supported and paper-proven protective-order combinations."""

    supported_order_types: frozenset[str | OrderType] = field(default_factory=frozenset)
    paper_proven_order_types: frozenset[str | OrderType] = field(default_factory=frozenset)
    standalone_trailing_parameters: frozenset[str] = field(default_factory=frozenset)
    paper_proven_standalone_trailing_parameters: frozenset[str] = field(
        default_factory=frozenset
    )
    fractional_combinations: frozenset[tuple[str | OrderType, TimeInForce | str]] = field(
        default_factory=frozenset
    )
    paper_proven_fractional_combinations: frozenset[
        tuple[str | OrderType, TimeInForce | str]
    ] = field(default_factory=frozenset)
    supported_tifs: frozenset[TimeInForce | str] = field(default_factory=frozenset)
    paper_proven_tifs: frozenset[TimeInForce | str] = field(default_factory=frozenset)
    linked_order_types: frozenset[str | OrderType] = field(default_factory=frozenset)
    paper_proven_linked_order_types: frozenset[str | OrderType] = field(
        default_factory=frozenset
    )
    linked_trailing_parameters: frozenset[str] = field(default_factory=frozenset)
    paper_proven_linked_trailing_parameters: frozenset[str] = field(
        default_factory=frozenset
    )

    def __post_init__(self) -> None:
        for name in (
            "supported_order_types",
            "paper_proven_order_types",
            "fractional_combinations",
            "paper_proven_fractional_combinations",
            "supported_tifs",
            "paper_proven_tifs",
            "linked_order_types",
            "paper_proven_linked_order_types",
            "standalone_trailing_parameters",
            "paper_proven_standalone_trailing_parameters",
            "linked_trailing_parameters",
            "paper_proven_linked_trailing_parameters",
        ):
            if not isinstance(getattr(self, name), frozenset):
                raise TypeError(f"{name} must be a frozenset")

        for name in (
            "supported_order_types",
            "paper_proven_order_types",
            "linked_order_types",
            "paper_proven_linked_order_types",
        ):
            object.__setattr__(self, name, _normalize_order_types(getattr(self, name), name))
        for name in ("supported_tifs", "paper_proven_tifs"):
            object.__setattr__(self, name, _normalize_tifs(getattr(self, name), name))
        for name in ("fractional_combinations", "paper_proven_fractional_combinations"):
            object.__setattr__(self, name, _normalize_combinations(getattr(self, name), name))
        for name in (
            "standalone_trailing_parameters",
            "paper_proven_standalone_trailing_parameters",
            "linked_trailing_parameters",
            "paper_proven_linked_trailing_parameters",
        ):
            normalized = frozenset(str(value).strip().upper() for value in getattr(self, name))
            if not normalized <= {_TRAIL_PRICE, _TRAIL_PERCENT}:
                raise ValueError(f"{name} contains an unsupported trailing parameter")
            object.__setattr__(self, name, normalized)


@dataclass(frozen=True, slots=True)
class ProtectionEligibility:
    status: str
    reason_code: str | None
    risk_notes: tuple[str, ...]
    entry_allowed: bool

    def __post_init__(self) -> None:
        if self.status not in {_ALLOWED, _REJECTED}:
            raise ValueError("status must be ALLOWED or REJECTED")
        if (self.reason_code is None) != (self.status == _ALLOWED):
            raise ValueError("REJECTED requires a reason code; ALLOWED must not have one")

    @property
    def allowed(self) -> bool:
        return self.status == _ALLOWED

    @property
    def rejected(self) -> bool:
        return self.status == _REJECTED


def _result(policy: ProtectionPolicy, reason: str | None = None) -> ProtectionEligibility:
    allowed = reason is None
    return ProtectionEligibility(
        status=_ALLOWED if allowed else _REJECTED,
        reason_code=reason,
        risk_notes=_RISK_NOTES,
        entry_allowed=allowed or not policy.required,
    )


def evaluate_policy(
    policy: ProtectionPolicy,
    capabilities: BrokerProtectionCapabilities,
    execution_mode: ExecutionMode | object,
    quantity: Decimal,
) -> ProtectionEligibility:
    """Return a deterministic, fail-closed capability decision."""

    if not isinstance(policy, ProtectionPolicy):
        raise TypeError("policy must be ProtectionPolicy")
    if not isinstance(capabilities, BrokerProtectionCapabilities):
        raise TypeError("capabilities must be BrokerProtectionCapabilities")
    if not isinstance(quantity, Decimal):
        raise TypeError("quantity must be Decimal")
    if not quantity.is_finite() or quantity <= _ZERO:
        raise ValueError("quantity must be a finite positive Decimal")
    live = _mode_value(execution_mode) == ExecutionMode.LIVE.value

    if policy.mode is ProtectionMode.TRAILING_STEPPED:
        return _result(policy, "STEPPED_NOT_AVAILABLE")
    if policy.mode not in {ProtectionMode.FIXED, ProtectionMode.TRAILING_NATIVE}:
        return _result(policy, "UNSUPPORTED_MODE")

    order_type = (
        _TRAILING_STOP
        if policy.mode is ProtectionMode.TRAILING_NATIVE
        else str(policy.order_semantics)
    )
    tif = policy.time_in_force
    assert isinstance(tif, TimeInForce)

    if order_type not in capabilities.supported_order_types:
        return _result(policy, "UNSUPPORTED_ORDER_TYPE")
    if live and order_type not in capabilities.paper_proven_order_types:
        return _result(policy, "UNSUPPORTED_ORDER_TYPE")

    if tif not in capabilities.supported_tifs:
        return _result(policy, "TIF_NOT_SUPPORTED")
    if live and tif not in capabilities.paper_proven_tifs:
        return _result(policy, "TIF_NOT_SUPPORTED")

    trail_parameter: str | None = None
    if policy.mode is ProtectionMode.TRAILING_NATIVE:
        trail_parameter = _TRAIL_PRICE if policy.trail_price is not None else _TRAIL_PERCENT
        if not policy.linked:
            if trail_parameter not in capabilities.standalone_trailing_parameters:
                return _result(policy, "UNSUPPORTED_ORDER_TYPE")
            if (
                live
                and trail_parameter
                not in capabilities.paper_proven_standalone_trailing_parameters
            ):
                return _result(policy, "UNSUPPORTED_ORDER_TYPE")

    if policy.linked:
        if policy.mode is ProtectionMode.TRAILING_NATIVE:
            if trail_parameter not in capabilities.linked_trailing_parameters:
                return _result(policy, "LINKED_TRAILING_UNSUPPORTED")
            if live and trail_parameter not in capabilities.paper_proven_linked_trailing_parameters:
                return _result(policy, "LINKED_TRAILING_UNSUPPORTED")
        else:
            if order_type not in capabilities.linked_order_types:
                return _result(policy, "UNSUPPORTED_ORDER_TYPE")
            if live and order_type not in capabilities.paper_proven_linked_order_types:
                return _result(policy, "UNSUPPORTED_ORDER_TYPE")

    fractional = quantity != quantity.to_integral_value()
    if fractional:
        if (
            not policy.allow_fractional_protection
            or policy.documented_fractional_risk_ack is None
        ):
            return _result(policy, "FRACTIONAL_NOT_SUPPORTED")
        combination = (order_type, tif)
        if combination not in capabilities.fractional_combinations:
            return _result(policy, "FRACTIONAL_NOT_SUPPORTED")
        if live and combination not in capabilities.paper_proven_fractional_combinations:
            return _result(policy, "FRACTIONAL_NOT_PAPER_PROVEN")

    return _result(policy)
