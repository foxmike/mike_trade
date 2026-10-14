"""Versioned, strategy-specific decision evidence (the document a reviewer reads).

A strategy that can be reviewed implements the *evidence interface*, version
:data:`EVIDENCE_INTERFACE_VERSION`:

* a class attribute ``EVIDENCE_INTERFACE_VERSION = 1``;
* a method ``build_evidence(context, params, state, evaluation) -> DecisionEvidence``.

``DecisionEvidence`` is a document, not a schema: the *envelope* (schema version, strategy id and
version, action, symbol, timestamp, reason code and reason) is common to every strategy, while
``evidence`` is an arbitrary JSON-serializable mapping chosen by the strategy to describe the
facts that actually caused the signal (model probabilities, opening-range levels, VWAP-cycle
state, ...).  Nothing forces a strategy into a shared set of indicators or probability fields,
and nothing here invents explanations or confidence values: a strategy reports only what it
computed.

``state`` handed to ``build_evidence`` is the strategy's persisted state *including* the
evaluation's own ``state_updates`` (the same view the engine holds right after the evaluation),
so a strategy can read the facts it just recorded.  ``build_evidence`` must be pure: no I/O, no
clock, no mutation.

Strategies that are never reviewed do not implement the interface.  Configuring review for a
strategy that does not is a configuration error (:class:`UnsupportedEvidenceError`).
"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

from mike_trade.domain.errors import ConfigurationError, MikeTradeError

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext
    from mike_trade.strategies.base import StrategyEvaluation, StrategySpec

__all__ = [
    "EVIDENCE_SCHEMA_VERSION",
    "EVIDENCE_INTERFACE_VERSION",
    "EvidenceError",
    "UnsupportedEvidenceError",
    "DecisionEvidence",
    "to_json_value",
    "supports_evidence",
    "require_evidence_support",
    "make_evidence",
    "collect_evidence",
]

#: Version of the serialized evidence document (envelope layout).
EVIDENCE_SCHEMA_VERSION = 1
#: Version of the Python interface a strategy implements to produce evidence.
EVIDENCE_INTERFACE_VERSION = 1

_ACTIONS = ("BUY", "SELL")


class EvidenceError(MikeTradeError):
    """A strategy returned malformed evidence or evidence for a different decision."""


class UnsupportedEvidenceError(ConfigurationError):
    """A strategy selected for review does not implement the evidence interface."""


def to_json_value(value: object, *, path: str = "evidence") -> Any:
    """Return ``value`` as plain JSON data, or raise :class:`EvidenceError`.

    ``Decimal`` becomes its exact string, ``datetime`` becomes a UTC ISO-8601 string with a ``Z``
    suffix, enums become their value, tuples/lists become lists.  Floats must be finite (JSON has
    no NaN/Infinity).  Anything else is rejected rather than stringified so that an evidence
    document never carries an object whose meaning has been silently lost.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EvidenceError(f"{path}: non-finite float {value!r} is not JSON-serializable")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise EvidenceError(f"{path}: non-finite Decimal is not JSON-serializable")
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise EvidenceError(f"{path}: naive datetime; evidence timestamps must be aware")
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return to_json_value(value.value, path=path)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise EvidenceError(f"{path}: mapping keys must be non-empty strings; got {key!r}")
            result[key] = to_json_value(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [to_json_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise EvidenceError(f"{path}: {type(value).__name__} is not JSON-serializable")


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise EvidenceError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DecisionEvidence:
    """Common envelope plus strategy-specific ``evidence``.

    ``evidence`` is normalized to plain JSON data on construction.  ``signal_id`` ties the
    document to the strategy signal it explains.
    """

    strategy_id: str
    strategy_version: str
    action: str
    symbol: str
    timestamp: datetime
    reason_code: str
    reason: str
    evidence: Mapping[str, Any]
    signal_id: str | None = None
    schema_version: int = field(default=EVIDENCE_SCHEMA_VERSION)

    def __post_init__(self) -> None:
        if self.schema_version != EVIDENCE_SCHEMA_VERSION:
            raise EvidenceError(f"unsupported evidence schema_version {self.schema_version!r}")
        for name in ("strategy_id", "strategy_version", "symbol", "reason_code", "reason"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise EvidenceError(f"{name} must be a non-empty str")
        if self.action not in _ACTIONS:
            raise EvidenceError(f"action must be one of {list(_ACTIONS)}; got {self.action!r}")
        object.__setattr__(self, "timestamp", _utc(self.timestamp, "timestamp"))
        if self.signal_id is not None and not (
            isinstance(self.signal_id, str) and self.signal_id
        ):
            raise EvidenceError("signal_id must be a non-empty str or None")
        if not isinstance(self.evidence, Mapping):
            raise EvidenceError("evidence must be a mapping (a JSON object)")
        object.__setattr__(self, "evidence", to_json_value(self.evidence))

    def to_dict(self) -> dict[str, Any]:
        """A deep, JSON-safe copy of the whole document."""
        return {
            "schema_version": self.schema_version,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "action": self.action,
            "symbol": self.symbol,
            "timestamp": to_json_value(self.timestamp),
            "signal_id": self.signal_id,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "evidence": copy.deepcopy(dict(self.evidence)),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DecisionEvidence:
        try:
            return cls(
                strategy_id=data["strategy_id"],
                strategy_version=data["strategy_version"],
                action=data["action"],
                symbol=data["symbol"],
                timestamp=datetime.fromisoformat(str(data["timestamp"]).replace("Z", "+00:00")),
                reason_code=data["reason_code"],
                reason=data["reason"],
                evidence=data["evidence"],
                signal_id=data.get("signal_id"),
                schema_version=int(data.get("schema_version", EVIDENCE_SCHEMA_VERSION)),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise EvidenceError(f"malformed evidence document: {exc}") from exc

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ------------------------------------------------------------------ interface checks


def supports_evidence(strategy: object) -> bool:
    """Whether a strategy class or instance implements evidence interface v1."""
    return getattr(strategy, "EVIDENCE_INTERFACE_VERSION", None) == EVIDENCE_INTERFACE_VERSION and (
        callable(getattr(strategy, "build_evidence", None))
    )


def require_evidence_support(strategy: object, *, where: str = "") -> None:
    """Raise :class:`UnsupportedEvidenceError` naming the strategy when it lacks the interface."""
    if supports_evidence(strategy):
        return
    spec = getattr(strategy, "spec", None)
    strategy_id = getattr(spec, "strategy_id", None) or getattr(strategy, "__name__", repr(strategy))
    side = getattr(getattr(spec, "side", None), "value", "?")
    location = f" ({where})" if where else ""
    raise UnsupportedEvidenceError(
        f"strategy {strategy_id!r} ({side}){location} is selected for decision review but does "
        f"not implement the decision-evidence interface v{EVIDENCE_INTERFACE_VERSION} "
        "(EVIDENCE_INTERFACE_VERSION and build_evidence). Implement it on the strategy, or "
        "exclude the strategy from decision_review.strategies.",
        code="STRATEGY_EVIDENCE_UNSUPPORTED",
        context={"strategy_id": strategy_id, "side": side},
    )


# ------------------------------------------------------------------ construction helpers


def make_evidence(
    spec: StrategySpec,
    evaluation: StrategyEvaluation,
    context: MarketContext,
    *,
    reason_code: str,
    reason: str,
    evidence: Mapping[str, Any],
) -> DecisionEvidence:
    """Build the envelope for ``evaluation`` (a signal) around strategy-specific ``evidence``."""
    if not evaluation.is_signal:
        raise EvidenceError(f"{spec.strategy_id}: evidence can only describe a signal")
    return DecisionEvidence(
        strategy_id=spec.strategy_id,
        strategy_version=spec.version,
        action=spec.side.value,
        symbol=context.symbol,
        timestamp=context.as_of,
        reason_code=reason_code,
        reason=reason,
        evidence=evidence,
        signal_id=evaluation.signal_id,
    )


def merged_state(
    state: Mapping[str, object], evaluation: StrategyEvaluation
) -> dict[str, object]:
    """The persisted state as the engine holds it right after ``evaluation``."""
    return {**state, **evaluation.state_updates}


def collect_evidence(
    strategy: object,
    context: MarketContext,
    params: Mapping[str, object],
    state: Mapping[str, object],
    evaluation: StrategyEvaluation,
) -> DecisionEvidence:
    """Ask ``strategy`` for the evidence of ``evaluation`` and verify it describes that signal.

    ``state`` is the persisted state; the evaluation's own updates are merged in here so every
    caller presents the strategy the same view.  Raises :class:`UnsupportedEvidenceError` for a
    strategy without the interface and :class:`EvidenceError` for a malformed document.
    """
    require_evidence_support(strategy)
    if not evaluation.is_signal:
        raise EvidenceError("evidence is only collected for signals")
    produced = strategy.build_evidence(  # type: ignore[attr-defined]
        context, params, merged_state(state, evaluation), evaluation
    )
    if not isinstance(produced, DecisionEvidence):
        raise EvidenceError(
            f"{evaluation.strategy_id}: build_evidence must return DecisionEvidence; "
            f"got {type(produced).__name__}"
        )
    expected = (
        evaluation.strategy_id,
        evaluation.version,
        evaluation.side.value,
        evaluation.symbol,
        evaluation.signal_id,
    )
    actual = (
        produced.strategy_id,
        produced.strategy_version,
        produced.action,
        produced.symbol,
        produced.signal_id,
    )
    if actual != expected:
        raise EvidenceError(
            f"{evaluation.strategy_id}: evidence describes {actual}, not the evaluated signal "
            f"{expected}"
        )
    return produced


def contributing_ids(items: Sequence[DecisionEvidence]) -> tuple[str, ...]:
    return tuple(sorted({item.strategy_id for item in items}))
