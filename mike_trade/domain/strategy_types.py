"""Immutable strategy definitions, registrations, and evaluation results.

This module uses only the Python standard library.  In particular, it does
not import market, broker, account, order, or protection types at runtime.

Design choices (2 of 3 for these strategy/signal contracts):
1. Symbols are stripped and uppercased at the contract boundary, matching the
   normalization used by the rest of the domain while keeping strategy keys
   strictly lowercase snake_case.
2. Parameter mappings are defensively shallow-copied into ``MappingProxyType``;
   the contract makes the mapping itself read-only without prescribing or
   transforming strategy-specific parameter values.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mike_trade.domain.signals import Signal

__all__ = [
    "StrategyRole",
    "StrategyDefinition",
    "StrategyInstance",
    "EvaluationStatus",
    "Evaluation",
]

_STRATEGY_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


def _require_non_empty_str(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty str")
    return value.strip()


def _require_strategy_key(value: object, *, field: str) -> str:
    result = _require_non_empty_str(value, field=field)
    if _STRATEGY_KEY_RE.fullmatch(result) is None:
        raise ValueError(f"{field} must be lowercase snake_case; got {value!r}")
    return result


def _require_symbol(value: object, *, field: str) -> str:
    result = _require_non_empty_str(value, field=field).upper()
    if any(character.isspace() for character in result):
        raise ValueError(f"{field} must not contain whitespace; got {value!r}")
    return result


def _require_utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be a datetime; got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _read_only_mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping; got {type(value).__name__}")
    copied: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{field} keys must be non-empty strings")
        copied[key] = item
    return MappingProxyType(copied)


def _string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{field} must be a tuple")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} items must be non-empty strings")
    return tuple(item.strip() for item in value)


class StrategyRole(Enum):
    """A strategy's independently registered entry or exit role."""

    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class StrategyDefinition:
    """Static metadata and defaults published by a strategy plugin."""

    strategy_key: str
    version: str
    role: StrategyRole
    description: str
    required_timeframes: tuple[str, ...]
    warmup_bars: int
    default_parameters: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "strategy_key",
            _require_strategy_key(self.strategy_key, field="strategy_key"),
        )
        object.__setattr__(self, "version", _require_non_empty_str(self.version, field="version"))
        if not isinstance(self.role, StrategyRole):
            raise ValueError(f"role must be StrategyRole; got {type(self.role).__name__}")
        if not isinstance(self.description, str):
            raise ValueError("description must be str")
        object.__setattr__(
            self,
            "required_timeframes",
            _string_tuple(self.required_timeframes, field="required_timeframes"),
        )
        if isinstance(self.warmup_bars, bool) or not isinstance(self.warmup_bars, int):
            raise ValueError("warmup_bars must be int")
        if self.warmup_bars < 0:
            raise ValueError("warmup_bars must be >= 0")
        object.__setattr__(
            self,
            "default_parameters",
            _read_only_mapping(self.default_parameters, field="default_parameters"),
        )


@dataclass(frozen=True, slots=True)
class StrategyInstance:
    """One configured BUY or SELL strategy registration."""

    instance_id: str
    strategy_key: str
    strategy_version: str
    role: StrategyRole
    symbol: str
    account_scope_id: str
    parameters: Mapping[str, object]
    enabled: bool
    pair_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instance_id", _require_non_empty_str(self.instance_id, field="instance_id")
        )
        object.__setattr__(
            self,
            "strategy_key",
            _require_strategy_key(self.strategy_key, field="strategy_key"),
        )
        object.__setattr__(
            self,
            "strategy_version",
            _require_non_empty_str(self.strategy_version, field="strategy_version"),
        )
        if not isinstance(self.role, StrategyRole):
            raise ValueError(f"role must be StrategyRole; got {type(self.role).__name__}")
        object.__setattr__(self, "symbol", _require_symbol(self.symbol, field="symbol"))
        object.__setattr__(
            self,
            "account_scope_id",
            _require_non_empty_str(self.account_scope_id, field="account_scope_id"),
        )
        object.__setattr__(
            self, "parameters", _read_only_mapping(self.parameters, field="parameters")
        )
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be bool")
        if self.pair_key is not None:
            object.__setattr__(
                self, "pair_key", _require_non_empty_str(self.pair_key, field="pair_key")
            )


class EvaluationStatus(Enum):
    """The complete set of outcomes from one strategy evaluation."""

    NO_SIGNAL = "NO_SIGNAL"
    SIGNAL = "SIGNAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    STALE_DATA = "STALE_DATA"
    ERROR = "ERROR"
    NOT_DUE = "NOT_DUE"


@dataclass(frozen=True, slots=True)
class Evaluation:
    """The broker-neutral result of evaluating one strategy instance."""

    instance_id: str
    symbol: str
    evaluated_at: datetime
    bar_time: datetime | None
    status: EvaluationStatus
    signal: Signal | None
    reason: str
    error_detail: str | None = None

    def __post_init__(self) -> None:
        instance_id = _require_non_empty_str(self.instance_id, field="instance_id")
        symbol = _require_symbol(self.symbol, field="symbol")
        object.__setattr__(self, "instance_id", instance_id)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(
            self, "evaluated_at", _require_utc(self.evaluated_at, field="evaluated_at")
        )
        if self.bar_time is not None:
            object.__setattr__(self, "bar_time", _require_utc(self.bar_time, field="bar_time"))
        if not isinstance(self.status, EvaluationStatus):
            raise ValueError(
                f"status must be EvaluationStatus; got {type(self.status).__name__}"
            )
        if not isinstance(self.reason, str):
            raise ValueError("reason must be str")
        if self.error_detail is not None and not isinstance(self.error_detail, str):
            raise ValueError("error_detail must be str or None")

        if self.status is EvaluationStatus.SIGNAL:
            if self.signal is None:
                raise ValueError("SIGNAL evaluations require a signal")
            if getattr(self.signal, "instance_id", None) != instance_id:
                raise ValueError("evaluation and signal instance_id must match")
            if getattr(self.signal, "symbol", None) != symbol:
                raise ValueError("evaluation and signal symbol must match")
        elif self.signal is not None:
            raise ValueError(f"{self.status.value} evaluations must not contain a signal")

        if self.status is EvaluationStatus.ERROR and not self.error_detail:
            raise ValueError("ERROR evaluations require error_detail")
