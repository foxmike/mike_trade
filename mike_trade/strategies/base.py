"""Versioned Python strategy SDK: specs, validated parameters, and BUY/SELL bases.

Strategies are pure functions of an immutable ``MarketContext``, validated
parameters, and read-only persisted state.  They receive no broker, secrets,
filesystem handle, or network/session client, and this module imports none of
those (only the standard library and pure domain/market-data value types).

Design choices:
1. ``StrategyRole`` (BUY/SELL/SHORT/COVER) from ``domain.strategy_types`` is the side type;
   ``CandleTimeframe`` from ``market_data.candles`` is the timeframe type.  The
   strategy-facing ``MarketContext`` is ``market_data.context.MarketContext``,
   imported for type checking only.
2. ``make_signal_id`` here is the strategy-level identity (strategy id, version,
   symbol, side, triggering bar).  It is distinct from
   ``domain.signals.make_signal_id``, which is keyed by a configured instance
   id and action; the domain ``Signal`` is built later by the engine.
3. The triggering bar timestamp is the closed bar's start (``Candle.start``),
   normalized to UTC; the same bar therefore always yields the same ID.
4. Evaluations carry reason codes and an optional signal ID only.  They have no
   confidence score, quantity, or order data.
5. Parameters without a default are required.  ``Decimal`` parameters accept
   ``Decimal`` or ``int`` only; ``float`` is rejected for every parameter kind.
"""

from __future__ import annotations

import hashlib
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, ClassVar

from mike_trade.domain.errors import MikeTradeError
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "ParameterKind",
    "ParameterValidationError",
    "ParameterSpec",
    "ParameterSchema",
    "ReasonCode",
    "TriggerKind",
    "StrategySpec",
    "STRATEGY_CATEGORIES",
    "TECHNICAL_SUB_CATEGORIES",
    "StrategyEvaluation",
    "make_signal_id",
    "BuyStrategy",
    "SellStrategy",
    "ShortStrategy",
    "CoverStrategy",
]

#: Stable ``StrategySpec.category`` identifiers.
STRATEGY_CATEGORIES: tuple[str, ...] = (
    "technical",
    "statistical",
    "machine_learning",
    "deep_learning",
    "reinforcement_learning",
    "llm_ai",
    "microstructure",
    "event_fundamental",
    "relative_value",
    "alternative_data",
)

#: Stable ``StrategySpec.sub_category`` identifiers for ``category == "technical"``.
TECHNICAL_SUB_CATEGORIES: tuple[str, ...] = (
    "momentum",
    "breakout",
    "vwap",
    "moving_average",
    "mean_reversion",
    "trend_following",
    "volatility",
    "price_action",
    "gap_trading",
    "intraday_session",
    "oscillator",
    "volume",
)

_STRATEGY_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def _require_non_empty_str(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty str")
    return value.strip()


def _require_identifier(value: object, *, field: str) -> str:
    result = _require_non_empty_str(value, field=field)
    if _STRATEGY_ID_RE.fullmatch(result) is None:
        raise ValueError(f"{field} must be lowercase snake_case; got {value!r}")
    return result


def _require_symbol(value: object, *, field: str) -> str:
    result = _require_non_empty_str(value, field=field).upper()
    if _SYMBOL_RE.fullmatch(result) is None:
        raise ValueError(f"{field} is invalid: {value!r}")
    return result


def _require_utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be a datetime; got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _require_semver(value: object) -> str:
    version = _require_non_empty_str(value, field="version")
    if _SEMVER_RE.fullmatch(version) is None:
        raise ValueError(f"version must be MAJOR.MINOR.PATCH; got {value!r}")
    return version


def _require_str_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{field} must be a tuple")
    return tuple(_require_non_empty_str(item, field=f"{field} item") for item in value)


def _read_only_mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping; got {type(value).__name__}")
    copied: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{field} keys must be non-empty strings")
        copied[key] = item
    return MappingProxyType(copied)


# ------------------------------------------------------------------ parameters


class ParameterKind(Enum):
    """The parameter types a strategy may declare."""

    INT = "INT"
    DECIMAL = "DECIMAL"
    BOOL = "BOOL"
    ENUM = "ENUM"
    TIME_OF_DAY = "TIME_OF_DAY"


_RANGED_KINDS = frozenset({ParameterKind.INT, ParameterKind.DECIMAL, ParameterKind.TIME_OF_DAY})


class ParameterValidationError(MikeTradeError, ValueError):
    """A strategy parameter is unknown, missing, mistyped, or out of range.

    ``code`` is one of ``unknown_parameter``, ``missing_parameter``,
    ``wrong_type``, ``out_of_range``, ``invalid_choice``, or ``invalid_spec``.
    """

    def __init__(self, message: str, *, code: str, parameter: str) -> None:
        super().__init__(message, code=code, context={"parameter": parameter})
        self.parameter = parameter


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """One named, typed parameter with optional default, inclusive range, and choices.

    A parameter with ``default=None`` is required.  Bounds apply to INT, DECIMAL
    and TIME_OF_DAY; ``choices`` apply to (and are required for) ENUM only.
    """

    name: str
    kind: ParameterKind
    default: object | None = None
    min_value: object | None = None
    max_value: object | None = None
    choices: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        try:
            object.__setattr__(
                self, "name", _require_identifier(self.name, field="parameter name")
            )
        except ValueError as exc:
            raise ParameterValidationError(
                str(exc), code="invalid_spec", parameter=str(self.name)
            ) from None
        if not isinstance(self.kind, ParameterKind):
            raise self._spec_error("kind must be ParameterKind")
        if not isinstance(self.description, str):
            raise self._spec_error("description must be str")

        if self.kind is ParameterKind.ENUM:
            if not isinstance(self.choices, tuple) or not self.choices:
                raise self._spec_error("ENUM parameters require a non-empty choices tuple")
            if any(not isinstance(c, str) or not c for c in self.choices):
                raise self._spec_error("choices must be non-empty strings")
            if len(set(self.choices)) != len(self.choices):
                raise self._spec_error("choices must be unique")
        elif self.choices:
            raise self._spec_error("choices are only allowed for ENUM parameters")

        if self.kind not in _RANGED_KINDS and (
            self.min_value is not None or self.max_value is not None
        ):
            raise self._spec_error(f"{self.kind.value} parameters do not support ranges")
        lower = None if self.min_value is None else self._check_type(self.min_value)
        upper = None if self.max_value is None else self._check_type(self.max_value)
        if lower is not None and upper is not None and lower > upper:  # type: ignore[operator]
            raise self._spec_error("min_value must be <= max_value")
        object.__setattr__(self, "min_value", lower)
        object.__setattr__(self, "max_value", upper)

        if self.default is not None:
            object.__setattr__(self, "default", self.validate_value(self.default))

    @property
    def required(self) -> bool:
        return self.default is None

    def _spec_error(self, message: str) -> ParameterValidationError:
        return ParameterValidationError(
            f"parameter {self.name}: {message}", code="invalid_spec", parameter=self.name
        )

    def _wrong_type(self, value: object, expected: str) -> ParameterValidationError:
        return ParameterValidationError(
            f"parameter {self.name} must be {expected}; got {type(value).__name__}",
            code="wrong_type",
            parameter=self.name,
        )

    def _check_type(self, value: object) -> object:
        """Return ``value`` normalized to this kind, or raise ``wrong_type``."""
        kind = self.kind
        if kind is ParameterKind.BOOL:
            if not isinstance(value, bool):
                raise self._wrong_type(value, "bool")
            return value
        if kind is ParameterKind.INT:
            if isinstance(value, bool) or not isinstance(value, int):
                raise self._wrong_type(value, "int")
            return value
        if kind is ParameterKind.DECIMAL:
            if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
                raise self._wrong_type(value, "Decimal (float is not allowed)")
            result = value if isinstance(value, Decimal) else Decimal(value)
            if not result.is_finite():
                raise ParameterValidationError(
                    f"parameter {self.name} must be finite; got {result!s}",
                    code="out_of_range",
                    parameter=self.name,
                )
            return result
        if kind is ParameterKind.TIME_OF_DAY:
            if not isinstance(value, time) or value.tzinfo is not None:
                raise self._wrong_type(value, "naive datetime.time")
            return value
        if not isinstance(value, str):
            raise self._wrong_type(value, "str")
        return value

    def validate_value(self, value: object) -> object:
        """Return the normalized value, or raise ``ParameterValidationError``."""
        result = self._check_type(value)
        if self.kind is ParameterKind.ENUM and result not in self.choices:
            raise ParameterValidationError(
                f"parameter {self.name} must be one of {list(self.choices)!r}; got {result!r}",
                code="invalid_choice",
                parameter=self.name,
            )
        below = self.min_value is not None and result < self.min_value  # type: ignore[operator]
        above = self.max_value is not None and result > self.max_value  # type: ignore[operator]
        if below or above:
            raise ParameterValidationError(
                f"parameter {self.name}={result!s} is outside "
                f"[{self.min_value!s}, {self.max_value!s}]",
                code="out_of_range",
                parameter=self.name,
            )
        return result


@dataclass(frozen=True, slots=True)
class ParameterSchema:
    """The complete, ordered set of parameters a strategy accepts."""

    parameters: tuple[ParameterSpec, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.parameters, tuple):
            raise ValueError("parameters must be a tuple")
        if any(not isinstance(p, ParameterSpec) for p in self.parameters):
            raise ValueError("parameters must contain ParameterSpec objects")
        names = [p.name for p in self.parameters]
        if len(set(names)) != len(names):
            raise ValueError("parameter names must be unique")

    def validate(self, values: Mapping[str, object]) -> Mapping[str, object]:
        """Return a read-only mapping of validated values with defaults applied."""
        if not isinstance(values, Mapping):
            raise ParameterValidationError(
                f"parameters must be a mapping; got {type(values).__name__}",
                code="wrong_type",
                parameter="<parameters>",
            )
        known = {p.name for p in self.parameters}
        for name in sorted(str(key) for key in values):
            if name not in known:
                raise ParameterValidationError(
                    f"unknown parameter {name!r}", code="unknown_parameter", parameter=name
                )
        validated: dict[str, object] = {}
        for spec in self.parameters:
            if spec.name in values:
                validated[spec.name] = spec.validate_value(values[spec.name])
            elif spec.required:
                raise ParameterValidationError(
                    f"missing required parameter {spec.name!r}",
                    code="missing_parameter",
                    parameter=spec.name,
                )
            else:
                validated[spec.name] = spec.default
        return MappingProxyType(validated)


# ----------------------------------------------------------------------- spec


class ReasonCode(Enum):
    """Diagnostic reason codes a strategy evaluation may carry."""

    SIGNAL = "SIGNAL"
    NO_SIGNAL = "NO_SIGNAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    STALE_DATA = "STALE_DATA"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


class TriggerKind(Enum):
    """When, relative to completed bars, a strategy may fire."""

    EDGE_ON_CLOSED_BAR = "EDGE_ON_CLOSED_BAR"
    LEVEL_ON_CLOSED_BAR = "LEVEL_ON_CLOSED_BAR"


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """Immutable, versioned description of one strategy's contract.

    ``category`` is one of :data:`STRATEGY_CATEGORIES`.  ``sub_category`` is one of
    :data:`TECHNICAL_SUB_CATEGORIES` when the category is ``technical``; the other
    categories have no sub-category vocabulary yet, so any snake_case identifier is
    accepted for them.  Both are plain, stable strings.
    """

    strategy_id: str
    version: str
    side: StrategyRole
    category: str
    sub_category: str
    reference_symbols: tuple[str, ...]
    required_timeframes: tuple[CandleTimeframe, ...]
    warmup_bars: int
    completed_bars_only: bool
    requires_volume: bool
    trigger_kind: TriggerKind
    trigger_timeframe: CandleTimeframe
    parameter_schema: ParameterSchema
    persistent_state_keys: tuple[str, ...]
    methodology: str
    source: str
    execution_limitations: tuple[str, ...]
    reason_codes: tuple[ReasonCode, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "strategy_id", _require_identifier(self.strategy_id, field="strategy_id")
        )
        object.__setattr__(self, "version", _require_semver(self.version))
        if not isinstance(self.side, StrategyRole):
            raise ValueError(f"side must be StrategyRole; got {type(self.side).__name__}")
        category = _require_identifier(self.category, field="category")
        if category not in STRATEGY_CATEGORIES:
            raise ValueError(
                f"category must be one of {list(STRATEGY_CATEGORIES)!r}; got {category!r}"
            )
        sub_category = _require_identifier(self.sub_category, field="sub_category")
        if category == "technical" and sub_category not in TECHNICAL_SUB_CATEGORIES:
            raise ValueError(
                f"technical sub_category must be one of {list(TECHNICAL_SUB_CATEGORIES)!r}; "
                f"got {sub_category!r}"
            )
        object.__setattr__(self, "category", category)
        object.__setattr__(self, "sub_category", sub_category)

        if not isinstance(self.reference_symbols, tuple):
            raise ValueError("reference_symbols must be a tuple")
        symbols = tuple(
            _require_symbol(s, field="reference_symbols item") for s in self.reference_symbols
        )
        if len(set(symbols)) != len(symbols):
            raise ValueError("reference_symbols must be unique")
        object.__setattr__(self, "reference_symbols", symbols)

        if not isinstance(self.required_timeframes, tuple) or not self.required_timeframes:
            raise ValueError("required_timeframes must be a non-empty tuple")
        if any(not isinstance(tf, CandleTimeframe) for tf in self.required_timeframes):
            raise ValueError("required_timeframes must contain CandleTimeframe values")
        if len(set(self.required_timeframes)) != len(self.required_timeframes):
            raise ValueError("required_timeframes must be unique")
        if self.trigger_timeframe not in self.required_timeframes:
            raise ValueError("trigger_timeframe must be one of required_timeframes")

        if isinstance(self.warmup_bars, bool) or not isinstance(self.warmup_bars, int):
            raise ValueError("warmup_bars must be int")
        if self.warmup_bars < 0:
            raise ValueError("warmup_bars must be >= 0")
        for name in ("completed_bars_only", "requires_volume"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")
        if not isinstance(self.trigger_kind, TriggerKind):
            raise ValueError("trigger_kind must be TriggerKind")
        if not self.completed_bars_only:
            raise ValueError(f"{self.trigger_kind.value} requires completed_bars_only=True")
        if not isinstance(self.parameter_schema, ParameterSchema):
            raise ValueError("parameter_schema must be ParameterSchema")

        keys = _require_str_tuple(self.persistent_state_keys, field="persistent_state_keys")
        if len(set(keys)) != len(keys):
            raise ValueError("persistent_state_keys must be unique")
        object.__setattr__(self, "persistent_state_keys", keys)
        object.__setattr__(
            self, "methodology", _require_non_empty_str(self.methodology, field="methodology")
        )
        object.__setattr__(self, "source", _require_non_empty_str(self.source, field="source"))
        object.__setattr__(
            self,
            "execution_limitations",
            _require_str_tuple(self.execution_limitations, field="execution_limitations"),
        )

        if not isinstance(self.reason_codes, tuple) or any(
            not isinstance(code, ReasonCode) for code in self.reason_codes
        ):
            raise ValueError("reason_codes must be a tuple of ReasonCode")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("reason_codes must be unique")
        if not {ReasonCode.SIGNAL, ReasonCode.NO_SIGNAL} <= set(self.reason_codes):
            raise ValueError("reason_codes must include SIGNAL and NO_SIGNAL")

    @property
    def version_key(self) -> tuple[int, int, int]:
        """The numeric ``(major, minor, patch)`` used to order versions."""
        major, minor, patch = self.version.split(".")
        return (int(major), int(minor), int(patch))


# ------------------------------------------------------------------ signal id


def make_signal_id(
    strategy_id: str,
    version: str,
    symbol: str,
    side: StrategyRole,
    bar_time: datetime,
) -> str:
    """Return the stable SHA-256 ID for one strategy/version/symbol/side/bar.

    ``bar_time`` is the triggering completed bar's start, normalized to UTC.
    """
    if not isinstance(side, StrategyRole):
        raise ValueError(f"side must be StrategyRole; got {type(side).__name__}")
    timestamp = _require_utc(bar_time, field="bar_time").isoformat(timespec="microseconds")
    canonical = json.dumps(
        [
            _require_identifier(strategy_id, field="strategy_id"),
            _require_semver(version),
            _require_symbol(symbol, field="symbol"),
            side.value,
            timestamp.replace("+00:00", "Z"),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- evaluation


@dataclass(frozen=True, slots=True)
class StrategyEvaluation:
    """Either a signal (with deterministic ``signal_id``) or a no-signal result.

    Build with :meth:`for_signal` or :meth:`no_signal`, which check reason codes
    against the strategy's declared ``spec.reason_codes``.  Carries no
    confidence score.  ``state_updates`` are values for the strategy's declared
    ``persistent_state_keys``; the engine, not the strategy, persists them.
    """

    strategy_id: str
    version: str
    symbol: str
    side: StrategyRole
    reason_codes: tuple[ReasonCode, ...]
    bar_time: datetime | None = None
    signal_id: str | None = None
    reference_price: Decimal | None = None
    state_updates: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_identifier(self.strategy_id, field="strategy_id")
        _require_semver(self.version)
        object.__setattr__(self, "symbol", _require_symbol(self.symbol, field="symbol"))
        if not isinstance(self.side, StrategyRole):
            raise ValueError("side must be StrategyRole")
        if (
            not isinstance(self.reason_codes, tuple)
            or not self.reason_codes
            or any(not isinstance(code, ReasonCode) for code in self.reason_codes)
        ):
            raise ValueError("reason_codes must be a non-empty tuple of ReasonCode")
        if self.bar_time is not None:
            object.__setattr__(self, "bar_time", _require_utc(self.bar_time, field="bar_time"))
        if self.reference_price is not None and (
            isinstance(self.reference_price, bool)
            or not isinstance(self.reference_price, Decimal)
            or not self.reference_price.is_finite()
            or self.reference_price <= 0
        ):
            raise ValueError("reference_price must be a positive finite Decimal")
        object.__setattr__(
            self, "state_updates", _read_only_mapping(self.state_updates, field="state_updates")
        )

        if self.signal_id is None:
            if ReasonCode.SIGNAL in self.reason_codes:
                raise ValueError("SIGNAL reason code requires a signal_id")
            if self.reference_price is not None:
                raise ValueError("no-signal evaluations must not carry reference_price")
        else:
            if self.reason_codes != (ReasonCode.SIGNAL,):
                raise ValueError("signal evaluations must carry exactly (SIGNAL,)")
            if self.bar_time is None:
                raise ValueError("signal evaluations require bar_time")
            expected = make_signal_id(
                self.strategy_id, self.version, self.symbol, self.side, self.bar_time
            )
            if self.signal_id != expected:
                raise ValueError("signal_id does not match the signal identity fields")

    @property
    def is_signal(self) -> bool:
        return self.signal_id is not None

    @classmethod
    def for_signal(
        cls,
        spec: StrategySpec,
        symbol: str,
        bar_time: datetime,
        *,
        reference_price: Decimal | None = None,
        state_updates: Mapping[str, object] | None = None,
    ) -> StrategyEvaluation:
        """Build a signal result whose ID is derived from the triggering bar."""
        return cls(
            strategy_id=spec.strategy_id,
            version=spec.version,
            symbol=symbol,
            side=spec.side,
            reason_codes=(ReasonCode.SIGNAL,),
            bar_time=bar_time,
            signal_id=make_signal_id(spec.strategy_id, spec.version, symbol, spec.side, bar_time),
            reference_price=reference_price,
            state_updates=state_updates or {},
        )

    @classmethod
    def no_signal(
        cls,
        spec: StrategySpec,
        symbol: str,
        reason_codes: tuple[ReasonCode, ...] = (ReasonCode.NO_SIGNAL,),
        *,
        bar_time: datetime | None = None,
        state_updates: Mapping[str, object] | None = None,
    ) -> StrategyEvaluation:
        """Build a no-signal result; codes must be declared by ``spec``."""
        if not isinstance(reason_codes, tuple):
            raise ValueError("reason_codes must be a tuple")
        undeclared = [c for c in reason_codes if c not in spec.reason_codes]
        if undeclared:
            raise ValueError(
                f"reason codes not declared by {spec.strategy_id}: "
                f"{[c.value for c in undeclared]!r}"
            )
        return cls(
            strategy_id=spec.strategy_id,
            version=spec.version,
            symbol=symbol,
            side=spec.side,
            reason_codes=reason_codes,
            bar_time=bar_time,
            state_updates=state_updates or {},
        )


# ----------------------------------------------------------------- base classes


class _Strategy(ABC):
    """Shared contract: a class-level ``spec`` and a pure ``evaluate``."""

    spec: ClassVar[StrategySpec]
    _side: ClassVar[StrategyRole]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        declared = cls.__dict__.get("spec")
        if declared is None:
            return
        if not isinstance(declared, StrategySpec):
            raise TypeError(f"{cls.__name__}.spec must be a StrategySpec")
        if declared.side is not cls._side:
            raise TypeError(
                f"{cls.__name__}.spec.side is {declared.side.value}; expected {cls._side.value}"
            )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        """Completed trigger-timeframe bars needed before ``params`` can evaluate.

        The default is the static ``spec.warmup_bars``.  A strategy whose lookback depends
        on its parameters overrides this so that ``spec.warmup_bars`` can state the
        requirement at the default parameters while engines still gate a configured
        (shorter or longer) lookback exactly.
        """
        return cls.spec.warmup_bars

    @abstractmethod
    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        """Evaluate one closed-bar context.

        Must be pure: no I/O, no clock, no mutation of ``params`` or ``state``.
        ``params`` are already validated by ``spec.parameter_schema``.
        """


class BuyStrategy(_Strategy):
    """Base class for entry (BUY) strategies."""

    _side: ClassVar[StrategyRole] = StrategyRole.BUY


class SellStrategy(_Strategy):
    """Base class for exit (SELL) strategies."""

    _side: ClassVar[StrategyRole] = StrategyRole.SELL


class ShortStrategy(_Strategy):
    """Base class for short-entry (SHORT) strategies.

    Like BUY, a SHORT strategy only reports that a short entry is warranted; sizing, order
    submission, borrow checks and protective stops belong to the engine.
    """

    _side: ClassVar[StrategyRole] = StrategyRole.SHORT


class CoverStrategy(_Strategy):
    """Base class for short-exit (COVER) strategies.

    Like SELL, a COVER strategy is only evaluated by the engine while a managed short exists and
    never opens a long position.
    """

    _side: ClassVar[StrategyRole] = StrategyRole.COVER
