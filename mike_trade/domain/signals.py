"""Immutable, broker-neutral signal, provenance, and decision contracts.

Only standard-library modules are imported at runtime.  Signals deliberately
contain no quantity, order, fill, broker, or position-cycle state.

Design choice (3 of 3 for these strategy/signal contracts):
3. Signal IDs use the full 64-character SHA-256 hex digest of a compact JSON
   array containing trimmed instance ID, normalized symbol, action value, and
   the bar instant normalized to a microsecond-precision UTC ``Z`` timestamp.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType

__all__ = [
    "SignalAction",
    "ENABLED_SIGNAL_ACTIONS",
    "SignalProvenance",
    "make_signal_id",
    "Signal",
    "DecisionOutcome",
    "SignalDecision",
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


def _to_decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field} must be Decimal, int, or str; got {type(value).__name__}")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field} must be a non-empty numeric string")
        try:
            result = Decimal(text)
        except InvalidOperation:
            raise ValueError(f"{field} is not a valid decimal string: {value!r}") from None
    else:
        raise ValueError(f"{field} must be Decimal, int, or str; got {type(value).__name__}")
    if not result.is_finite():
        raise ValueError(f"{field} must be finite; got {result!s}")
    return result


class SignalAction(Enum):
    """Signal actions: ENTER_LONG/EXIT_LONG (BUY/SELL) and ENTER_SHORT/COVER_SHORT (SHORT/COVER).

    All four are valid signals.  Whether a short action may actually reach an order is decided
    downstream and fails closed: the portfolio manager's ``short_authorized`` switch, the
    broker adapter's short authorization and its shortability/margin/protection checks.
    """

    ENTER_LONG = "ENTER_LONG"
    EXIT_LONG = "EXIT_LONG"
    ENTER_SHORT = "ENTER_SHORT"
    COVER_SHORT = "COVER_SHORT"


ENABLED_SIGNAL_ACTIONS = frozenset(SignalAction)


@dataclass(frozen=True, slots=True)
class SignalProvenance:
    """The market-data and optional research evidence behind a signal."""

    data_provider: str
    data_feed: str
    is_consolidated: bool
    timeframe: str
    bar_time: datetime
    indicator_snapshot: Mapping[str, Decimal | None]
    research_source: str | None = None
    research_as_of: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "data_provider",
            _require_non_empty_str(self.data_provider, field="data_provider"),
        )
        object.__setattr__(
            self, "data_feed", _require_non_empty_str(self.data_feed, field="data_feed")
        )
        if not isinstance(self.is_consolidated, bool):
            raise ValueError("is_consolidated must be bool")
        object.__setattr__(
            self, "timeframe", _require_non_empty_str(self.timeframe, field="timeframe")
        )
        object.__setattr__(self, "bar_time", _require_utc(self.bar_time, field="bar_time"))
        if not isinstance(self.indicator_snapshot, Mapping):
            raise ValueError("indicator_snapshot must be a mapping")
        snapshot: dict[str, Decimal | None] = {}
        for key, value in self.indicator_snapshot.items():
            if not isinstance(key, str) or not key:
                raise ValueError("indicator_snapshot keys must be non-empty strings")
            snapshot[key] = None if value is None else _to_decimal(value, field=f"indicator[{key}]")
        object.__setattr__(self, "indicator_snapshot", MappingProxyType(snapshot))
        if self.research_source is not None:
            object.__setattr__(
                self,
                "research_source",
                _require_non_empty_str(self.research_source, field="research_source"),
            )
        if self.research_as_of is not None:
            object.__setattr__(
                self,
                "research_as_of",
                _require_utc(self.research_as_of, field="research_as_of"),
            )


def make_signal_id(
    instance_id: str,
    symbol: str,
    action: SignalAction,
    bar_time: datetime,
) -> str:
    """Return a stable SHA-256 identifier for one instance/action/bar."""
    normalized_instance = _require_non_empty_str(instance_id, field="instance_id")
    normalized_symbol = _require_symbol(symbol, field="symbol")
    if not isinstance(action, SignalAction):
        raise ValueError(f"action must be SignalAction; got {type(action).__name__}")
    normalized_bar_time = _require_utc(bar_time, field="bar_time")
    timestamp = normalized_bar_time.isoformat(timespec="microseconds").replace("+00:00", "Z")
    canonical = json.dumps(
        [normalized_instance, normalized_symbol, action.value, timestamp],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Signal:
    """A strategy's immutable actionable result (long or short entry/exit)."""

    signal_id: str
    strategy_key: str
    strategy_version: str
    instance_id: str
    symbol: str
    action: SignalAction
    bar_time: datetime
    generated_at: datetime
    reference_price: Decimal | None
    reason: str
    provenance: SignalProvenance
    confidence: Decimal | None = None

    def __post_init__(self) -> None:
        strategy_key = _require_strategy_key(self.strategy_key, field="strategy_key")
        strategy_version = _require_non_empty_str(
            self.strategy_version, field="strategy_version"
        )
        instance_id = _require_non_empty_str(self.instance_id, field="instance_id")
        symbol = _require_symbol(self.symbol, field="symbol")
        if not isinstance(self.action, SignalAction):
            raise ValueError(f"action must be SignalAction; got {type(self.action).__name__}")
        if self.action not in ENABLED_SIGNAL_ACTIONS:
            raise ValueError(f"signal action {self.action.value} is not enabled")
        bar_time = _require_utc(self.bar_time, field="bar_time")
        generated_at = _require_utc(self.generated_at, field="generated_at")
        if generated_at < bar_time:
            raise ValueError("generated_at must be >= bar_time")
        if not isinstance(self.provenance, SignalProvenance):
            raise ValueError("provenance must be SignalProvenance")
        reference_price = None
        if self.reference_price is not None:
            reference_price = _to_decimal(self.reference_price, field="reference_price")
            if reference_price <= 0:
                raise ValueError("reference_price must be > 0")
        confidence = None
        if self.confidence is not None:
            confidence = _to_decimal(self.confidence, field="confidence")
            if confidence < 0 or confidence > 1:
                raise ValueError("confidence must be between 0 and 1 inclusive")
        reason = _require_non_empty_str(self.reason, field="reason")

        expected_signal_id = make_signal_id(instance_id, symbol, self.action, bar_time)
        if self.signal_id != expected_signal_id:
            raise ValueError("signal_id does not match the signal identity fields")

        object.__setattr__(self, "strategy_key", strategy_key)
        object.__setattr__(self, "strategy_version", strategy_version)
        object.__setattr__(self, "instance_id", instance_id)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "bar_time", bar_time)
        object.__setattr__(self, "generated_at", generated_at)
        object.__setattr__(self, "reference_price", reference_price)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "confidence", confidence)

    @classmethod
    def create(
        cls,
        *,
        strategy_key: str,
        strategy_version: str,
        instance_id: str,
        symbol: str,
        action: SignalAction,
        bar_time: datetime,
        generated_at: datetime,
        reference_price: Decimal | None,
        reason: str,
        provenance: SignalProvenance,
        confidence: Decimal | None = None,
    ) -> Signal:
        """Construct a signal with its deterministic identifier."""
        signal_id = make_signal_id(instance_id, symbol, action, bar_time)
        return cls(
            signal_id=signal_id,
            strategy_key=strategy_key,
            strategy_version=strategy_version,
            instance_id=instance_id,
            symbol=symbol,
            action=action,
            bar_time=bar_time,
            generated_at=generated_at,
            reference_price=reference_price,
            reason=reason,
            provenance=provenance,
            confidence=confidence,
        )


class DecisionOutcome(Enum):
    """Routing or policy disposition of a signal."""

    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    IGNORE = "IGNORE"
    DEFER = "DEFER"


@dataclass(frozen=True, slots=True)
class SignalDecision:
    """A signal disposition, kept separate from all execution state."""

    decision_id: str
    signal_id: str
    outcome: DecisionOutcome
    decided_at: datetime
    reason_codes: tuple[str, ...]
    detail: str
    defer_until: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "decision_id", _require_non_empty_str(self.decision_id, field="decision_id")
        )
        object.__setattr__(
            self, "signal_id", _require_non_empty_str(self.signal_id, field="signal_id")
        )
        if not isinstance(self.outcome, DecisionOutcome):
            raise ValueError(
                f"outcome must be DecisionOutcome; got {type(self.outcome).__name__}"
            )
        object.__setattr__(self, "decided_at", _require_utc(self.decided_at, field="decided_at"))
        if not isinstance(self.reason_codes, tuple):
            raise ValueError("reason_codes must be a tuple")
        if any(not isinstance(code, str) or not code.strip() for code in self.reason_codes):
            raise ValueError("reason_codes items must be non-empty strings")
        object.__setattr__(self, "reason_codes", tuple(code.strip() for code in self.reason_codes))
        if not isinstance(self.detail, str):
            raise ValueError("detail must be str")
        if self.defer_until is not None:
            object.__setattr__(
                self, "defer_until", _require_utc(self.defer_until, field="defer_until")
            )
        needs_reasons = self.outcome in {DecisionOutcome.REJECT, DecisionOutcome.DEFER}
        if needs_reasons and not self.reason_codes:
            raise ValueError(f"{self.outcome.value} decisions require reason_codes")
        if self.outcome is DecisionOutcome.DEFER and self.defer_until is None:
            raise ValueError("DEFER decisions require defer_until")
