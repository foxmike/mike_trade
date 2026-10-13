"""Immutable, strategy-facing model-prediction values.

This module is pure: it imports only the standard library and has no model-library,
broker, persistence, or network dependency.  The composition root builds these values
(from ``mike_trade.modeling``) and attaches them to ``MarketContext.model_inputs``; a pure
strategy only ever reads them.

Design choices:
1. A prediction names exactly what was knowable: the model identity and artifact checksum,
   the completed bar it describes (``observation_end``), and when it was produced
   (``predicted_at``).  A prediction for an earlier bar is therefore never mistakable for a
   prediction for a later one.
2. Failures are first-class values (:class:`ModelPredictionResult`) so a strategy can report
   *why* it did not trade and the audit trail can show it, but an unavailable prediction can
   never be turned into a signal.
3. SELL predictions carry the fingerprint of the actual position they were computed for.
   Position facts are the real fill price/time of the real position; they are never inferred.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

__all__ = [
    "PredictionFailure",
    "ModelReference",
    "PositionFacts",
    "ServedPrediction",
    "ModelPredictionResult",
    "ModelInputs",
    "ModelDriven",
]

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,62}")
_SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ALGORITHMS = frozenset({"xgboost", "lightgbm"})
_ROLES = frozenset({"buy", "sell"})


class PredictionFailure(StrEnum):
    """Why a model could not supply a usable prediction (all fail closed)."""

    MODEL_NOT_LOADED = "MODEL_NOT_LOADED"
    ROLE_MISMATCH = "ROLE_MISMATCH"
    ALGORITHM_MISMATCH = "ALGORITHM_MISMATCH"
    SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
    SYMBOL_NOT_IN_TRAINING_UNIVERSE = "SYMBOL_NOT_IN_TRAINING_UNIVERSE"
    PRIMARY_IS_REFERENCE_SYMBOL = "PRIMARY_IS_REFERENCE_SYMBOL"
    FEED_MISMATCH = "FEED_MISMATCH"
    TIMEFRAME_MISMATCH = "TIMEFRAME_MISMATCH"
    ADJUSTMENT_MISMATCH = "ADJUSTMENT_MISMATCH"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    PROVENANCE_MISMATCH = "PROVENANCE_MISMATCH"
    NOT_REGULAR_SESSION = "NOT_REGULAR_SESSION"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    FEATURES_UNAVAILABLE = "FEATURES_UNAVAILABLE"
    REFERENCE_BAR_MISSING = "REFERENCE_BAR_MISSING"
    VWAP_UNAVAILABLE = "VWAP_UNAVAILABLE"
    FUTURE_OBSERVATION = "FUTURE_OBSERVATION"
    STALE_OBSERVATION = "STALE_OBSERVATION"
    NONFINITE_PREDICTION = "NONFINITE_PREDICTION"
    INVALID_PROBABILITIES = "INVALID_PROBABILITIES"
    INFERENCE_ERROR = "INFERENCE_ERROR"
    INFERENCE_TIMEOUT = "INFERENCE_TIMEOUT"
    POSITION_REQUIRED = "POSITION_REQUIRED"
    POSITION_INVALID = "POSITION_INVALID"
    POSITION_ENTRY_BAR_UNAVAILABLE = "POSITION_ENTRY_BAR_UNAVAILABLE"
    POSITION_OUTSIDE_TRAINING_SUPPORT = "POSITION_OUTSIDE_TRAINING_SUPPORT"


def _require_utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase snake_case identifier")
    return value


def _semver(value: object, name: str) -> str:
    if not isinstance(value, str) or _SEMVER.fullmatch(value) is None:
        raise ValueError(f"{name} must be MAJOR.MINOR.PATCH")
    return value


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _symbol(value: object) -> str:
    text = value.strip().upper() if isinstance(value, str) else ""
    if not text:
        raise ValueError("symbol must be a non-empty str")
    return text


def _positive_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise ValueError(f"{name} must be a Decimal")
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a positive finite Decimal")
    return value


@dataclass(frozen=True, slots=True)
class ModelReference:
    """An explicit, immutable model pin: identity, version, algorithm, role (never ``latest``)."""

    model_id: str
    model_version: str
    algorithm: str
    role: str
    #: Optional operator-supplied checksum the loaded artifact must match exactly.
    artifact_sha256: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.model_id, "model_id")
        _semver(self.model_version, "model_version")
        if self.algorithm not in _ALGORITHMS:
            raise ValueError(f"algorithm must be one of {sorted(_ALGORITHMS)}")
        if self.role not in _ROLES:
            raise ValueError(f"role must be one of {sorted(_ROLES)}")
        if self.artifact_sha256 is not None:
            _sha256(self.artifact_sha256, "artifact_sha256")

    @property
    def key(self) -> str:
        return f"{self.model_id}@{self.model_version}"


@dataclass(frozen=True, slots=True)
class PositionFacts:
    """The real, confirmed long position a SELL prediction is about.

    ``entry_price`` is the actual average entry fill price and ``entry_time`` the actual
    entry fill time of the BACKTEST/SHADOW/PAPER/LIVE position.  They are never taken from a
    training-time hypothetical entry.
    """

    symbol: str
    cycle_id: str
    quantity: Decimal
    entry_price: Decimal
    entry_time: datetime
    direction: str = "LONG"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        if not isinstance(self.cycle_id, str) or not self.cycle_id:
            raise ValueError("cycle_id must be a non-empty str")
        _positive_decimal(self.quantity, "quantity")
        _positive_decimal(self.entry_price, "entry_price")
        object.__setattr__(self, "entry_time", _require_utc(self.entry_time, "entry_time"))
        if self.direction != "LONG":
            raise ValueError("model SELL positions must be LONG")

    @property
    def fingerprint(self) -> str:
        """Stable identity of this position's inputs (symbol, cycle, entry price and time)."""
        canonical = "|".join(
            (
                self.symbol,
                self.cycle_id,
                str(self.entry_price),
                self.entry_time.isoformat(timespec="microseconds"),
            )
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _frozen_floats(values: Mapping[str, float], name: str) -> Mapping[str, float]:
    copied: dict[str, float] = {}
    for key, item in values.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{name} keys must be non-empty strings")
        number = float(item)
        if not math.isfinite(number):
            raise ValueError(f"{name}[{key}] must be finite")
        copied[key] = number
    return MappingProxyType(copied)


@dataclass(frozen=True, slots=True)
class ServedPrediction:
    """One validated model output with full provenance.

    ``observation_end`` is the end of the last completed bar the features describe;
    ``predicted_at`` is when the prediction was produced.  ``probabilities`` follows the
    manifest's class order; ``value`` is set instead for regression models.
    """

    model_id: str
    model_version: str
    artifact_sha256: str
    algorithm: str
    role: str
    task: str
    symbol: str
    feature_schema_id: str
    feature_schema_version: str
    feature_schema_sha256: str
    feed: str
    timeframe: str
    adjustment: str
    observation_start: datetime
    observation_end: datetime
    predicted_at: datetime
    horizon_bars: int
    label_id: str
    class_labels: tuple[str, ...] = ()
    probabilities: Mapping[str, float] = field(default_factory=dict)
    value: float | None = None
    #: Modeled net return of each class (decimal fraction) when the label schema defines it.
    class_net_returns: Mapping[str, Decimal] = field(default_factory=dict)
    class_return_basis: str = ""
    symbol_in_training_universe: bool = False
    position_fingerprint: str | None = None
    position_features: Mapping[str, float] = field(default_factory=dict)
    inference_ms: float = 0.0

    def __post_init__(self) -> None:
        _identifier(self.model_id, "model_id")
        _semver(self.model_version, "model_version")
        _sha256(self.artifact_sha256, "artifact_sha256")
        _sha256(self.feature_schema_sha256, "feature_schema_sha256")
        if self.algorithm not in _ALGORITHMS or self.role not in _ROLES:
            raise ValueError("algorithm/role are not supported")
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        for name in ("feed", "timeframe", "adjustment"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty str")
        start = _require_utc(self.observation_start, "observation_start")
        end = _require_utc(self.observation_end, "observation_end")
        made = _require_utc(self.predicted_at, "predicted_at")
        if end <= start:
            raise ValueError("observation_end must be after observation_start")
        if made < end:
            raise ValueError("a prediction cannot be made before its observation completed")
        object.__setattr__(self, "observation_start", start)
        object.__setattr__(self, "observation_end", end)
        object.__setattr__(self, "predicted_at", made)
        if isinstance(self.horizon_bars, bool) or self.horizon_bars < 1:
            raise ValueError("horizon_bars must be a positive int")
        labels = tuple(str(item) for item in self.class_labels)
        object.__setattr__(self, "class_labels", labels)
        probabilities = _frozen_floats(self.probabilities, "probabilities")
        object.__setattr__(self, "probabilities", probabilities)
        if probabilities:
            if set(probabilities) != set(labels):
                raise ValueError("probabilities must cover exactly the class labels")
            if any(p < 0.0 or p > 1.0 for p in probabilities.values()):
                raise ValueError("probabilities must lie in [0, 1]")
            if abs(sum(probabilities.values()) - 1.0) > 1e-6:
                raise ValueError("probabilities must sum to 1")
            if self.value is not None:
                raise ValueError("class predictions cannot carry a regression value")
        elif self.value is None or not math.isfinite(self.value):
            raise ValueError("regression predictions require a finite value")
        returns: dict[str, Decimal] = {}
        for key, item in self.class_net_returns.items():
            if not isinstance(item, Decimal) or not item.is_finite():
                raise ValueError("class_net_returns must be finite Decimals")
            returns[str(key)] = item
        object.__setattr__(self, "class_net_returns", MappingProxyType(returns))
        object.__setattr__(
            self, "position_features", _frozen_floats(self.position_features, "position_features")
        )
        if (self.role == "sell") != (self.position_fingerprint is not None):
            raise ValueError("SELL predictions (and only SELL predictions) carry a position")
        if not math.isfinite(self.inference_ms) or self.inference_ms < 0:
            raise ValueError("inference_ms must be finite and non-negative")

    @property
    def reference_key(self) -> str:
        return f"{self.model_id}@{self.model_version}"

    def to_audit(self) -> dict[str, object]:
        """Compact, JSON-safe provenance for audit sinks (no feature vector)."""
        record: dict[str, object] = {
            "model": self.reference_key,
            "artifact_sha256": self.artifact_sha256,
            "algorithm": self.algorithm,
            "role": self.role,
            "task": self.task,
            "symbol": self.symbol,
            "feature_schema": f"{self.feature_schema_id}@{self.feature_schema_version}",
            "feature_schema_sha256": self.feature_schema_sha256,
            "feed": self.feed,
            "timeframe": self.timeframe,
            "adjustment": self.adjustment,
            "observation_end": self.observation_end.isoformat(),
            "predicted_at": self.predicted_at.isoformat(),
            "horizon_bars": self.horizon_bars,
        }
        if self.probabilities:
            record["probabilities"] = {k: round(v, 6) for k, v in self.probabilities.items()}
        else:
            record["value"] = self.value
        if self.position_fingerprint is not None:
            record["position_fingerprint"] = self.position_fingerprint
            record["position_features"] = {
                k: round(v, 8) for k, v in self.position_features.items()
            }
        return record


@dataclass(frozen=True, slots=True)
class ModelPredictionResult:
    """Either a usable prediction or the explicit reason there is none."""

    reference: ModelReference
    prediction: ServedPrediction | None = None
    failure: PredictionFailure | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if (self.prediction is None) == (self.failure is None):
            raise ValueError("a result carries exactly one of prediction or failure")
        if self.failure is not None and not isinstance(self.failure, PredictionFailure):
            raise ValueError("failure must be PredictionFailure")

    @property
    def ok(self) -> bool:
        return self.prediction is not None


@dataclass(frozen=True, slots=True)
class ModelInputs:
    """Immutable model inputs for one symbol at one evaluation, keyed by model reference."""

    symbol: str
    evaluated_at: datetime
    results: Mapping[str, ModelPredictionResult] = field(default_factory=dict)
    position: PositionFacts | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "evaluated_at", _require_utc(self.evaluated_at, "evaluated_at"))
        frozen: dict[str, ModelPredictionResult] = {}
        for key, result in self.results.items():
            if not isinstance(result, ModelPredictionResult) or key != result.reference.key:
                raise ValueError("results must be keyed by their model reference key")
            if result.prediction is not None and result.prediction.symbol != self.symbol:
                raise ValueError("prediction symbol differs from the model inputs symbol")
            frozen[key] = result
        object.__setattr__(self, "results", MappingProxyType(frozen))
        if self.position is not None and self.position.symbol != self.symbol:
            raise ValueError("position symbol differs from the model inputs symbol")

    def result_for(self, reference_key: str) -> ModelPredictionResult | None:
        return self.results.get(reference_key)


@runtime_checkable
class ModelDriven(Protocol):
    """A strategy class that declares the one pinned model it consumes (pure, from params)."""

    @classmethod
    def model_reference(cls, params: Mapping[str, object]) -> ModelReference: ...
