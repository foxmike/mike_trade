"""Broker-neutral, model-library-neutral contracts for training and inference."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,62}")
_SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase snake_case identifier")
    return value


def semver(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SEMVER.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be MAJOR.MINOR.PATCH")
    return value


def sha256(value: str, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def symbol(value: str) -> str:
    result = value.strip().upper() if isinstance(value, str) else ""
    if not result:
        raise ValueError("symbol must be non-empty")
    return result


class LearningTask(StrEnum):
    BINARY_CLASSIFICATION = "BINARY_CLASSIFICATION"
    MULTICLASS_CLASSIFICATION = "MULTICLASS_CLASSIFICATION"
    REGRESSION = "REGRESSION"


@dataclass(frozen=True, slots=True)
class DatasetSource:
    """One immutable raw snapshot selected for an experiment."""

    dataset_id: str
    sha256: str
    symbol: str
    feed: str
    timeframe: str
    adjustment: str
    coverage_start: datetime
    coverage_end: datetime

    def __post_init__(self) -> None:
        if not self.dataset_id:
            raise ValueError("dataset_id is required")
        object.__setattr__(self, "sha256", sha256(self.sha256, "dataset sha256"))
        object.__setattr__(self, "symbol", symbol(self.symbol))
        for name in ("feed", "timeframe", "adjustment"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
            object.__setattr__(self, name, value.strip().lower())
        object.__setattr__(self, "coverage_start", utc(self.coverage_start, "coverage_start"))
        object.__setattr__(self, "coverage_end", utc(self.coverage_end, "coverage_end"))
        if self.coverage_end <= self.coverage_start:
            raise ValueError("dataset coverage must be non-empty")


@dataclass(frozen=True, slots=True)
class DatasetSelection:
    """Resolved, immutable input selection; all sources share one market-data identity."""

    universe_id: str
    start: datetime
    end: datetime
    sources: tuple[DatasetSource, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.universe_id, str) or not self.universe_id.strip():
            raise ValueError("universe_id is required")
        object.__setattr__(self, "start", utc(self.start, "start"))
        object.__setattr__(self, "end", utc(self.end, "end"))
        if self.end <= self.start or not self.sources:
            raise ValueError("selection must have a non-empty range and sources")
        identities = {(s.feed, s.timeframe, s.adjustment) for s in self.sources}
        if len(identities) != 1:
            raise ValueError("a dataset selection cannot mix feed, timeframe, or adjustment")
        symbols = [s.symbol for s in self.sources]
        if len(symbols) != len(set(symbols)):
            raise ValueError("a dataset selection requires exactly one source per symbol")
        if any(s.coverage_start > self.start or s.coverage_end < self.end for s in self.sources):
            raise ValueError("every source must cover the selected range")

    @property
    def feed(self) -> str:
        return self.sources[0].feed

    @property
    def timeframe(self) -> str:
        return self.sources[0].timeframe

    @property
    def adjustment(self) -> str:
        return self.sources[0].adjustment


@dataclass(frozen=True, slots=True)
class ModelBar:
    """A completed, point-in-time OHLCV input shared by batch and streaming features."""

    symbol: str
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    feed: str
    timeframe: str
    adjustment: str
    session_id: str
    vwap: Decimal | None = None
    trade_count: int | None = None
    completed: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", symbol(self.symbol))
        object.__setattr__(self, "start", utc(self.start, "start"))
        object.__setattr__(self, "end", utc(self.end, "end"))
        if self.end <= self.start:
            raise ValueError("bar end must be after start")
        if not self.completed:
            raise ValueError("model inputs must be completed bars")
        for name in ("open", "high", "low", "close", "volume"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
            if name == "volume" and value < 0:
                raise ValueError("volume must be non-negative")
            if name != "volume" and value <= 0:
                raise ValueError(f"{name} must be positive")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("invalid OHLC ordering")
        if self.vwap is not None and (
            not isinstance(self.vwap, Decimal) or not self.vwap.is_finite() or self.vwap <= 0
        ):
            raise ValueError("vwap must be a positive finite Decimal or None")
        if self.trade_count is not None and (
            isinstance(self.trade_count, bool) or self.trade_count < 0
        ):
            raise ValueError("trade_count must be non-negative or None")
        for name in ("feed", "timeframe", "adjustment", "session_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        object.__setattr__(self, "feed", self.feed.strip().lower())
        object.__setattr__(self, "timeframe", self.timeframe.strip().lower())
        object.__setattr__(self, "adjustment", self.adjustment.strip().lower())


@dataclass(frozen=True, slots=True)
class FeatureSchema:
    feature_id: str
    version: str
    names: tuple[str, ...]
    required_reference_symbols: tuple[str, ...] = ()
    timeframe: str = "1m"

    def __post_init__(self) -> None:
        object.__setattr__(self, "feature_id", identifier(self.feature_id, "feature_id"))
        object.__setattr__(self, "version", semver(self.version, "feature version"))
        if not self.names or len(self.names) != len(set(self.names)):
            raise ValueError("feature names must be non-empty and unique")
        if any(_IDENTIFIER.fullmatch(name) is None for name in self.names):
            raise ValueError("feature names must be lowercase snake_case identifiers")
        refs = tuple(symbol(item) for item in self.required_reference_symbols)
        if len(refs) != len(set(refs)):
            raise ValueError("required reference symbols must be unique")
        object.__setattr__(self, "required_reference_symbols", refs)


@dataclass(frozen=True, slots=True)
class FeatureVector:
    symbol: str
    bar_start: datetime
    as_of: datetime
    session_id: str
    feed: str
    schema_id: str
    schema_version: str
    values: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", symbol(self.symbol))
        object.__setattr__(self, "bar_start", utc(self.bar_start, "bar_start"))
        object.__setattr__(self, "as_of", utc(self.as_of, "as_of"))
        if self.as_of <= self.bar_start:
            raise ValueError("feature vector is unavailable until its bar is completed")
        identifier(self.schema_id, "schema_id")
        semver(self.schema_version, "schema_version")
        copied: dict[str, float] = {}
        for name, value in self.values.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"feature {name} must be numeric")
            normalized = float(value)
            if not math.isfinite(normalized):
                raise ValueError(f"feature {name} must be finite")
            copied[name] = normalized
        object.__setattr__(self, "values", MappingProxyType(copied))


@dataclass(frozen=True, slots=True)
class LabelSchema:
    label_id: str
    version: str
    task: LearningTask
    horizon_bars: int
    parameters: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "label_id", identifier(self.label_id, "label_id"))
        object.__setattr__(self, "version", semver(self.version, "label version"))
        if not isinstance(self.task, LearningTask):
            raise ValueError("task must be LearningTask")
        if isinstance(self.horizon_bars, bool) or self.horizon_bars < 1:
            raise ValueError("horizon_bars must be a positive int")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class SupervisedLabel:
    symbol: str
    signal_bar_start: datetime
    entry_time: datetime
    entry_price: Decimal
    outcome_time: datetime
    outcome_price: Decimal
    value: Decimal | int | str
    net_return: Decimal
    schema_id: str
    schema_version: str
    outcome: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", symbol(self.symbol))
        for name in ("signal_bar_start", "entry_time", "outcome_time"):
            object.__setattr__(self, name, utc(getattr(self, name), name))
        if self.entry_time <= self.signal_bar_start:
            raise ValueError("label entry must occur after the completed signal bar begins")
        if self.outcome_time < self.entry_time:
            raise ValueError("outcome cannot precede entry")
        for name in ("entry_price", "outcome_price"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
        if not isinstance(self.net_return, Decimal) or not self.net_return.is_finite():
            raise ValueError("net_return must be a finite Decimal")
        identifier(self.schema_id, "schema_id")
        semver(self.schema_version, "schema_version")


@dataclass(frozen=True, slots=True)
class TrainingExample:
    features: FeatureVector
    label: SupervisedLabel

    def __post_init__(self) -> None:
        if self.features.symbol != self.label.symbol:
            raise ValueError("feature and label symbols differ")
        if self.features.bar_start != self.label.signal_bar_start:
            raise ValueError("feature and label anchors differ")
        if self.features.as_of > self.label.entry_time:
            raise ValueError("features contain data unavailable at label entry")


class PredictionKind(StrEnum):
    CLASS = "CLASS"
    REGRESSION = "REGRESSION"


@dataclass(frozen=True, slots=True)
class ModelPrediction:
    """Stable strategy-facing output; never carries orders or broker state."""

    model_id: str
    model_version: str
    symbol: str
    as_of: datetime
    feature_schema_id: str
    feature_schema_version: str
    kind: PredictionKind
    predicted_class: str | None = None
    probabilities: Mapping[str, float] = field(default_factory=dict)
    value: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_id", identifier(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", semver(self.model_version, "model_version"))
        object.__setattr__(self, "symbol", symbol(self.symbol))
        object.__setattr__(self, "as_of", utc(self.as_of, "as_of"))
        identifier(self.feature_schema_id, "feature_schema_id")
        semver(self.feature_schema_version, "feature_schema_version")
        probs = {str(k): float(v) for k, v in self.probabilities.items()}
        if any(not math.isfinite(v) or v < 0 or v > 1 for v in probs.values()):
            raise ValueError("probabilities must be finite values in [0, 1]")
        if probs and abs(sum(probs.values()) - 1.0) > 1e-9:
            raise ValueError("probabilities must sum to 1")
        object.__setattr__(self, "probabilities", MappingProxyType(probs))
        if self.kind is PredictionKind.CLASS:
            if self.predicted_class is None or self.value is not None:
                raise ValueError("class predictions require predicted_class and no value")
            if probs and self.predicted_class not in probs:
                raise ValueError("predicted_class must be present in probabilities")
        elif self.kind is PredictionKind.REGRESSION:
            if self.value is None or not math.isfinite(self.value):
                raise ValueError("regression predictions require a finite value")
            if self.predicted_class is not None or probs:
                raise ValueError("regression predictions cannot carry class output")


@dataclass(frozen=True, slots=True)
class TrainingRequest:
    model_id: str
    model_version: str
    seed: int
    feature_schema: FeatureSchema
    label_schema: LabelSchema
    hyperparameters: Mapping[str, object]

    def __post_init__(self) -> None:
        identifier(self.model_id, "model_id")
        semver(self.model_version, "model_version")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be int")
        object.__setattr__(self, "hyperparameters", MappingProxyType(dict(self.hyperparameters)))


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    metrics: Mapping[str, Decimal]
    sample_count: int

    def __post_init__(self) -> None:
        if self.sample_count < 0:
            raise ValueError("sample_count must be non-negative")
        if any(not value.is_finite() for value in self.metrics.values()):
            raise ValueError("evaluation metrics must be finite")
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))


@runtime_checkable
class DatasetProvider(Protocol):
    def select(
        self,
        symbols: Sequence[str],
        start: datetime,
        end: datetime,
        *,
        feed: str,
        timeframe: str,
        adjustment: str,
        universe_id: str,
        dataset_ids: Mapping[str, str] | None = None,
    ) -> DatasetSelection: ...

    def iter_bars(
        self, selection: DatasetSelection, symbol: str, *, batch_size: int = 4096
    ) -> Iterator[ModelBar]: ...


@runtime_checkable
class FeaturePipeline(Protocol):
    @property
    def schema(self) -> FeatureSchema: ...

    def update(
        self, bar: ModelBar, references: Mapping[str, ModelBar] | None = None
    ) -> FeatureVector | None: ...


@runtime_checkable
class LabelGenerator(Protocol):
    @property
    def schema(self) -> LabelSchema: ...

    def generate(self, bars: Sequence[ModelBar]) -> Iterator[SupervisedLabel]: ...


@runtime_checkable
class TrainingDatasetBuilder(Protocol):
    def build_symbol(
        self,
        bars: Sequence[ModelBar],
        *,
        references: Mapping[str, Sequence[ModelBar]] | None = None,
    ) -> Iterator[TrainingExample]: ...


@runtime_checkable
class ModelTrainer(Protocol):
    def train(self, request: TrainingRequest, examples: Iterable[TrainingExample]) -> bytes: ...


@runtime_checkable
class ModelPredictor(Protocol):
    @property
    def feature_schema(self) -> FeatureSchema: ...

    def predict(self, features: FeatureVector) -> ModelPrediction: ...


@runtime_checkable
class ModelEvaluator(Protocol):
    def evaluate(
        self,
        predictions: Iterable[ModelPrediction],
        labels: Iterable[SupervisedLabel],
    ) -> EvaluationReport: ...


@runtime_checkable
class ModelRepository(Protocol):
    def put(self, manifest: object, artifact: bytes) -> object: ...

    def get(self, model_id: str, version: str) -> tuple[object, bytes]: ...
