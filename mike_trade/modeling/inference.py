"""Shared, model-agnostic inference service for pinned XGBoost/LightGBM artifacts.

The service is the *only* place a trained artifact is loaded for trading.  It is created by
the composition root with an explicit list of :class:`ModelReference` pins, loads each
checksum-verified artifact exactly once at construction, and afterwards serves predictions
from completed bars.  It knows nothing about brokers, orders, positions' lifecycle, or
strategies, and it never trains, reloads, or substitutes a model.

Guarantees (each failure is returned as a typed :class:`PredictionFailure`, never raised into
a strategy, and never produces a prediction):

* the artifact and its manifest are verified (size + SHA-256, optional operator checksum pin),
  the adapter/algorithm/role/feature-schema identity is exact, and the feature order that the
  native model carries equals the manifest (checked by the trusted adapter on load);
* every bar used has the model's feed, timeframe and adjustment — IEX and SIP are never mixed;
* features are produced by the same :class:`IntradayFeaturePipeline` used for training, from a
  window of completed bars only, so the result equals the streaming/batch computation;
* SELL position features use :func:`position_feature_values`, the function training uses,
  with the *actual* entry fill price and time;
* predictions are bounded-LRU cached by (model, symbol, observation end, position
  fingerprint); identical feature inputs are reused across models.
"""

from __future__ import annotations

import math
import threading
import time as _time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import NamedTuple

from mike_trade.domain.errors import MikeTradeError
from mike_trade.domain.model_signals import (
    ModelPredictionResult,
    ModelReference,
    PositionFacts,
    PredictionFailure,
    ServedPrediction,
)
from mike_trade.modeling.artifacts import FileModelRepository, ModelArtifactManifest
from mike_trade.modeling.contracts import (
    FeatureSchema,
    FeatureVector,
    LearningTask,
    ModelBar,
    ModelPrediction,
    ModelPredictor,
    PredictionKind,
)
from mike_trade.modeling.features import IntradayFeatureConfig, IntradayFeaturePipeline
from mike_trade.modeling.matrix import FeatureSchemaMismatch
from mike_trade.modeling.objectives import POSITION_FEATURE_NAMES, position_feature_values
from mike_trade.modeling.registry import ModelAdapterRegistry

__all__ = [
    "ModelLoadError",
    "InferenceRequest",
    "LoadedModel",
    "InferenceDiagnostics",
    "ModelInferenceService",
    "EXPECTED_SELL_CLASSES",
]

EXPECTED_SELL_CLASSES = ("-1", "0", "1")
EXPECTED_SELL_CLASS_SEMANTICS = "-1=RISK_EXIT,0=EXIT_FAVORABLE,1=HOLD_FAVORABLE"
_SELL_FEATURE_ID = "intraday_position"
_BPS = Decimal("10000")


class ModelLoadError(MikeTradeError):
    """A pinned model cannot be used; raised at startup, never during evaluation.

    ``code`` is one of ``MODEL_MISSING``, ``CHECKSUM_MISMATCH``, ``MANIFEST_INVALID``,
    ``ALGORITHM_MISMATCH``, ``ROLE_MISMATCH``, ``SCHEMA_MISMATCH``, ``PROVENANCE_UNSUPPORTED``
    or ``ADAPTER_UNAVAILABLE``.
    """


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    """Completed, point-in-time inputs for one prediction (no I/O is performed with them)."""

    reference: ModelReference
    symbol: str
    #: Completed primary bars in ascending order; the last one is the observation.
    bars: Sequence[ModelBar]
    #: Completed reference-symbol bars (ascending), keyed by symbol.
    references: Mapping[str, Sequence[ModelBar]]
    #: The real position (SELL models only).
    position: PositionFacts | None = None


@dataclass(frozen=True, slots=True)
class LoadedModel:
    """One verified artifact held in memory for the life of the service."""

    reference: ModelReference
    manifest: ModelArtifactManifest
    predictor: ModelPredictor
    base_config: IntradayFeatureConfig
    base_schema: FeatureSchema
    trained_symbols: frozenset[str]
    class_returns: Mapping[str, Decimal]
    class_return_basis: str
    #: Largest ``position_holding_bars`` the SELL model saw during training (None for BUY).
    max_supported_holding_bars: int | None
    predict_lock: threading.Lock

    @property
    def feature_window(self) -> int:
        return self.base_config.maximum_history


@dataclass(slots=True)
class InferenceDiagnostics:
    """Thread-safe counters surfaced through existing health/diagnostic channels."""

    requests: int = 0
    cache_hits: int = 0
    feature_reuse: int = 0
    predictions: int = 0
    slow_predictions: int = 0
    max_inference_ms: float = 0.0
    total_inference_ms: float = 0.0
    failures: dict[str, int] = field(default_factory=dict)

    def snapshot(self) -> dict[str, object]:
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "feature_reuse": self.feature_reuse,
            "predictions": self.predictions,
            "slow_predictions": self.slow_predictions,
            "max_inference_ms": round(self.max_inference_ms, 3),
            "mean_inference_ms": round(
                self.total_inference_ms / self.predictions if self.predictions else 0.0, 3
            ),
            "failures": dict(self.failures),
        }


class _Failure(NamedTuple):
    """Internal failure carrier (distinct from the plain tuples successful helpers return)."""

    code: PredictionFailure
    detail: str


def _fail(reference: ModelReference, code: PredictionFailure, detail: str) -> ModelPredictionResult:
    return ModelPredictionResult(reference, failure=code, detail=detail)


def _derive_role(manifest: ModelArtifactManifest) -> str:
    return "sell" if manifest.feature_schema.feature_id == _SELL_FEATURE_ID else "buy"


def _class_returns(manifest: ModelArtifactManifest) -> tuple[Mapping[str, Decimal], str]:
    """Modeled net return per class for triple-barrier BUY labels; empty when undefined."""
    label = manifest.label_schema
    params = label.parameters
    needed = ("positive_barrier", "negative_barrier", "entry_bps", "exit_bps")
    if (
        label.task is not LearningTask.MULTICLASS_CLASSIFICATION
        or tuple(manifest.class_labels) != EXPECTED_SELL_CLASSES
        or not label.label_id.startswith("triple_barrier")
        or any(key not in params for key in needed)
    ):
        return MappingProxyType({}), ""
    try:
        positive = Decimal(str(params["positive_barrier"]))
        negative = Decimal(str(params["negative_barrier"]))
        exit_bps = Decimal(str(params["exit_bps"]))
    except Exception:
        return MappingProxyType({}), ""
    exit_factor = Decimal(1) - exit_bps / _BPS
    returns = {
        "1": (Decimal(1) + positive) * exit_factor - 1,
        "-1": (Decimal(1) - negative) * exit_factor - 1,
        "0": exit_factor - 1,
    }
    basis = "triple_barrier:win=barrier,loss=barrier_without_gap,timeout=gross_zero"
    return MappingProxyType(returns), basis


class ModelInferenceService:
    """Loads pinned models once; serves cached, validated, point-in-time predictions."""

    def __init__(
        self,
        repository: FileModelRepository,
        adapters: ModelAdapterRegistry,
        references: Sequence[ModelReference],
        *,
        clock: Callable[[], datetime],
        max_observation_age: timedelta = timedelta(seconds=600),
        max_inference_seconds: float = 2.0,
        cache_size: int = 4096,
        feature_cache_size: int = 2048,
    ) -> None:
        if not callable(clock):
            raise ValueError("clock must be callable")
        if max_observation_age <= timedelta(0):
            raise ValueError("max_observation_age must be positive")
        if not math.isfinite(max_inference_seconds) or max_inference_seconds <= 0:
            raise ValueError("max_inference_seconds must be positive")
        if cache_size < 1 or feature_cache_size < 1:
            raise ValueError("cache sizes must be positive")
        self._repository = repository
        self._adapters = adapters
        self._clock = clock
        self._max_age = max_observation_age
        self._max_inference = max_inference_seconds
        self._cache_size = cache_size
        self._feature_cache_size = feature_cache_size
        self._lock = threading.RLock()
        self._predictions: OrderedDict[tuple[str, str, datetime, str, int], ServedPrediction] = (
            OrderedDict()
        )
        self._features: OrderedDict[tuple[str, str, datetime, int], FeatureVector] = OrderedDict()
        self._diagnostics = InferenceDiagnostics()
        self._status: dict[str, dict[str, object]] = {}
        self._models: dict[str, LoadedModel] = {}
        seen: set[str] = set()
        for reference in references:
            if not isinstance(reference, ModelReference):
                raise ValueError("references must be ModelReference pins")
            if reference.key in seen:
                continue
            seen.add(reference.key)
            self._models[reference.key] = self._load(reference)

    # ------------------------------------------------------------------ loading

    def _load(self, reference: ModelReference) -> LoadedModel:
        context = {"model": reference.key}
        try:
            manifest, artifact = self._repository.get(reference.model_id, reference.model_version)
        except FileNotFoundError:
            raise ModelLoadError(
                f"pinned model {reference.key} has no manifest/artifact in the repository",
                code="MODEL_MISSING",
                context=context,
            ) from None
        except ValueError as exc:
            text = str(exc)
            code = (
                "CHECKSUM_MISMATCH" if "checksum" in text or "size" in text else "MANIFEST_INVALID"
            )
            raise ModelLoadError(
                f"pinned model {reference.key} failed verification: {text}",
                code=code,
                context=context,
            ) from exc
        if (
            manifest.model_id != reference.model_id
            or manifest.model_version != reference.model_version
        ):
            raise ModelLoadError(
                f"repository returned {manifest.model_id}@{manifest.model_version} "
                f"for pin {reference.key}",
                code="MANIFEST_INVALID",
                context=context,
            )
        if manifest.manifest_schema_version != 2 or manifest.algorithm is None:
            raise ModelLoadError(
                f"pinned model {reference.key} has a schema-v1 manifest; tree models require v2",
                code="MANIFEST_INVALID",
                context=context,
            )
        if reference.artifact_sha256 is not None and reference.artifact_sha256 != (
            manifest.artifact_sha256
        ):
            raise ModelLoadError(
                f"pinned model {reference.key} artifact checksum differs from the configured pin",
                code="CHECKSUM_MISMATCH",
                context=context,
            )
        if manifest.algorithm != reference.algorithm or manifest.adapter_id != reference.algorithm:
            raise ModelLoadError(
                f"pinned model {reference.key} is a {manifest.algorithm!r} model but the "
                f"strategy requires {reference.algorithm!r}",
                code="ALGORITHM_MISMATCH",
                context=context,
            )
        role = _derive_role(manifest)
        if role != reference.role:
            raise ModelLoadError(
                f"pinned model {reference.key} is a {role} model but a {reference.role} "
                "strategy references it",
                code="ROLE_MISMATCH",
                context=context,
            )
        if manifest.timeframe != "1m" or manifest.adjustment != "raw":
            raise ModelLoadError(
                f"pinned model {reference.key} was trained on {manifest.timeframe}/"
                f"{manifest.adjustment}; only 1m raw inference is supported",
                code="PROVENANCE_UNSUPPORTED",
                context=context,
            )
        base_config, base_schema = self._base_pipeline(reference, manifest)
        max_support: int | None = None
        if role == "sell":
            if (
                manifest.label_schema.task is not LearningTask.MULTICLASS_CLASSIFICATION
                or tuple(manifest.class_labels) != EXPECTED_SELL_CLASSES
                or manifest.label_schema.label_id != "sell_continuation"
                or manifest.label_schema.parameters.get("classes")
                != EXPECTED_SELL_CLASS_SEMANTICS
            ):
                raise ModelLoadError(
                    f"SELL model {reference.key} lacks the required recorded continuation "
                    "class semantics",
                    code="SCHEMA_MISMATCH",
                    context=context,
                )
            try:
                maximum_holding = int(str(manifest.entry_policy["maximum_holding_bars"]))
            except (KeyError, ValueError):
                raise ModelLoadError(
                    f"SELL model {reference.key} does not record its training holding limit",
                    code="MANIFEST_INVALID",
                    context=context,
                ) from None
            max_support = maximum_holding - manifest.label_schema.horizon_bars
            if max_support < 1:
                raise ModelLoadError(
                    f"SELL model {reference.key} has no supported holding range",
                    code="MANIFEST_INVALID",
                    context=context,
                )
        try:
            factory = self._adapters.get(manifest.adapter_id, manifest.adapter_version)
            predictor = factory.predictor(artifact, manifest)
        except Exception as exc:
            raise ModelLoadError(
                f"pinned model {reference.key} could not be loaded by adapter "
                f"{manifest.adapter_id}@{manifest.adapter_version}: {exc}",
                code="ADAPTER_UNAVAILABLE",
                context=context,
            ) from exc
        if predictor.feature_schema != manifest.feature_schema:
            raise ModelLoadError(
                f"pinned model {reference.key} predictor schema differs from its manifest",
                code="SCHEMA_MISMATCH",
                context=context,
            )
        self._bound_threads(predictor)
        returns, basis = _class_returns(manifest)
        return LoadedModel(
            reference=reference,
            manifest=manifest,
            predictor=predictor,
            base_config=base_config,
            base_schema=base_schema,
            trained_symbols=frozenset(source.symbol for source in manifest.dataset_sources),
            class_returns=returns,
            class_return_basis=basis,
            max_supported_holding_bars=max_support,
            predict_lock=threading.Lock(),
        )

    @staticmethod
    def _base_pipeline(
        reference: ModelReference, manifest: ModelArtifactManifest
    ) -> tuple[IntradayFeatureConfig, FeatureSchema]:
        schema = manifest.feature_schema
        names = schema.names
        if manifest.feature_schema.feature_id == _SELL_FEATURE_ID:
            if names[-len(POSITION_FEATURE_NAMES) :] != POSITION_FEATURE_NAMES:
                raise ModelLoadError(
                    f"SELL model {reference.key} schema does not end with the four position "
                    "features",
                    code="SCHEMA_MISMATCH",
                    context={"model": reference.key},
                )
            base_names = names[: -len(POSITION_FEATURE_NAMES)]
            base_id, base_version = "intraday_core", "1.0.0"
        else:
            base_names = names
            base_id, base_version = schema.feature_id, schema.version
        config = IntradayFeatureConfig(
            feature_id=base_id,
            version=base_version,
            reference_symbols=tuple(schema.required_reference_symbols),
            include_vwap="vwap_distance" in base_names,
            timeframe=schema.timeframe,
        )
        pipeline = IntradayFeaturePipeline(config)
        if pipeline.schema.names != tuple(base_names):
            raise ModelLoadError(
                f"model {reference.key} feature order is not reproducible by feature pipeline "
                f"{base_id}@{base_version}",
                code="SCHEMA_MISMATCH",
                context={"model": reference.key},
            )
        return config, pipeline.schema

    @staticmethod
    def _bound_threads(predictor: ModelPredictor) -> None:
        """Keep inference single-threaded: one row per call gains nothing from vendor pools."""
        booster = getattr(predictor, "booster", None)
        if booster is None:
            return
        set_param = getattr(booster, "set_param", None)
        if callable(set_param):  # XGBoost
            set_param({"nthread": 1})

    # ------------------------------------------------------------------ queries

    @property
    def references(self) -> tuple[ModelReference, ...]:
        return tuple(model.reference for model in self._models.values())

    def loaded(self, reference: ModelReference) -> LoadedModel | None:
        return self._models.get(reference.key)

    def manifest(self, reference: ModelReference) -> ModelArtifactManifest | None:
        model = self._models.get(reference.key)
        return None if model is None else model.manifest

    def required_reference_symbols(self) -> tuple[str, ...]:
        names: dict[str, None] = {}
        for model in self._models.values():
            for symbol in model.manifest.feature_schema.required_reference_symbols:
                names.setdefault(symbol, None)
        return tuple(names)

    def feature_window(self, reference: ModelReference) -> int:
        model = self._models.get(reference.key)
        return 0 if model is None else model.feature_window

    def health(self) -> list[dict[str, object]]:
        """Per pinned model: identity, loaded state, last prediction and last failure."""
        with self._lock:
            status = {key: dict(value) for key, value in self._status.items()}
        rows: list[dict[str, object]] = []
        for key, model in sorted(self._models.items()):
            seen = status.get(
                key,
                {"predictions": 0, "failures": 0, "last_prediction": None, "last_failure": None},
            )
            rows.append(
                {
                    "model": key,
                    "algorithm": model.reference.algorithm,
                    "role": model.reference.role,
                    "feed": model.manifest.feed,
                    "artifact_sha256": model.manifest.artifact_sha256,
                    "state": "LOADED",
                    **seen,
                }
            )
        return rows

    def diagnostics(self) -> dict[str, object]:
        with self._lock:
            snapshot = self._diagnostics.snapshot()
        snapshot["models"] = sorted(self._models)
        snapshot["cache_entries"] = len(self._predictions)
        return snapshot

    # ------------------------------------------------------------------ inference

    def predict(self, request: InferenceRequest) -> ModelPredictionResult:
        """Return a validated prediction for the latest completed bar, or the reason why not."""
        started = _time.perf_counter()
        with self._lock:
            self._diagnostics.requests += 1
        result = self._predict(request, started)
        with self._lock:
            status = self._status.setdefault(
                request.reference.key,
                {"predictions": 0, "failures": 0, "last_prediction": None, "last_failure": None},
            )
            if result.failure is not None:
                failures = self._diagnostics.failures
                failures[result.failure.value] = failures.get(result.failure.value, 0) + 1
                status["failures"] = int(str(status["failures"])) + 1
                status["last_failure"] = {
                    "code": result.failure.value,
                    "detail": result.detail[:200],
                    "symbol": request.symbol.strip().upper(),
                    "at": self._clock().isoformat(),
                }
            elif result.prediction is not None:
                status["predictions"] = int(str(status["predictions"])) + 1
                status["last_prediction"] = {
                    "symbol": result.prediction.symbol,
                    "observation_end": result.prediction.observation_end.isoformat(),
                    "predicted_at": result.prediction.predicted_at.isoformat(),
                }
        return result

    def _predict(self, request: InferenceRequest, started: float) -> ModelPredictionResult:
        reference = request.reference
        model = self._models.get(reference.key)
        if model is None:
            return _fail(
                reference, PredictionFailure.MODEL_NOT_LOADED, f"{reference.key} is not loaded"
            )
        if model.reference.algorithm != reference.algorithm or model.reference.role != (
            reference.role
        ):
            return _fail(
                reference,
                PredictionFailure.ALGORITHM_MISMATCH,
                f"{reference.key} is loaded as {model.reference.algorithm}/{model.reference.role}",
            )
        if (
            reference.artifact_sha256 is not None
            and reference.artifact_sha256 != model.manifest.artifact_sha256
        ):
            return _fail(
                reference, PredictionFailure.PROVENANCE_MISMATCH, "artifact checksum pin differs"
            )
        manifest = model.manifest
        symbol = request.symbol.strip().upper()
        bars = request.bars
        if not bars:
            return _fail(reference, PredictionFailure.INSUFFICIENT_HISTORY, "no completed bars")
        last = bars[-1]
        if last.symbol != symbol:
            return _fail(
                reference,
                PredictionFailure.SYMBOL_MISMATCH,
                f"bars are for {last.symbol}, request is for {symbol}",
            )
        if symbol in manifest.feature_schema.required_reference_symbols:
            return _fail(
                reference,
                PredictionFailure.PRIMARY_IS_REFERENCE_SYMBOL,
                f"{symbol} is a declared reference series of {reference.key}",
            )
        # Not a data error: strategies decide whether an instrument the model never saw may trade.
        symbol_trained = symbol in model.trained_symbols

        now = self._clock()
        if last.end > now:
            return _fail(
                reference,
                PredictionFailure.FUTURE_OBSERVATION,
                f"observation ends {last.end.isoformat()} after {now.isoformat()}",
            )
        if now - last.end > self._max_age:
            return _fail(
                reference,
                PredictionFailure.STALE_OBSERVATION,
                f"observation ended {(now - last.end).total_seconds():.0f}s ago",
            )

        position = request.position
        fingerprint = ""
        if model.reference.role == "sell":
            if position is None:
                return _fail(
                    reference, PredictionFailure.POSITION_REQUIRED, "SELL needs a position"
                )
            if position.symbol != symbol:
                return _fail(
                    reference, PredictionFailure.POSITION_INVALID, "position is for another symbol"
                )
            fingerprint = position.fingerprint
        elif position is not None:
            return _fail(
                reference, PredictionFailure.POSITION_INVALID, "BUY models take no position"
            )

        content = self._content_fingerprint(model, bars, request.references)
        key = (reference.key, symbol, last.end, fingerprint, content)
        with self._lock:
            cached = self._predictions.get(key)
            if cached is not None:
                self._predictions.move_to_end(key)
                self._diagnostics.cache_hits += 1
                return ModelPredictionResult(reference, prediction=cached)

        for bar in bars[-model.feature_window :]:
            mismatch = self._provenance_failure(manifest, bar)
            if mismatch is not None:
                return _fail(reference, mismatch.code, mismatch.detail)
        if len(bars) < model.feature_window:
            return _fail(
                reference,
                PredictionFailure.INSUFFICIENT_HISTORY,
                f"{len(bars)} bars < required window {model.feature_window}",
            )
        for ref_symbol in manifest.feature_schema.required_reference_symbols:
            ref_bars = request.references.get(ref_symbol, ())
            if not ref_bars:
                return _fail(
                    reference,
                    PredictionFailure.REFERENCE_BAR_MISSING,
                    f"no completed {ref_symbol} bars",
                )
            if ref_bars[-1].start != last.start:
                return _fail(
                    reference,
                    PredictionFailure.REFERENCE_BAR_MISSING,
                    f"{ref_symbol} bar for {last.start.isoformat()} is not available",
                )
            for bar in ref_bars[-model.feature_window :]:
                mismatch = self._provenance_failure(manifest, bar)
                if mismatch is not None:
                    return _fail(reference, mismatch.code, f"{ref_symbol}: {mismatch.detail}")

        vector_or_failure = self._base_features(model, symbol, bars, request.references, content)
        if isinstance(vector_or_failure, _Failure):
            return _fail(reference, vector_or_failure.code, vector_or_failure.detail)
        base_vector = vector_or_failure
        if base_vector.as_of != last.end or base_vector.bar_start != last.start:
            return _fail(
                reference,
                PredictionFailure.PROVENANCE_MISMATCH,
                "feature vector does not describe the latest completed bar",
            )

        position_features: Mapping[str, float] = {}
        vector = base_vector
        if model.reference.role == "sell":
            assert position is not None
            built = self._position_vector(model, base_vector, bars, position)
            if isinstance(built, _Failure):
                return _fail(reference, built.code, built.detail)
            vector, position_features = built

        begun = _time.perf_counter()
        try:
            with model.predict_lock:
                prediction = model.predictor.predict(vector)
        except FeatureSchemaMismatch as exc:
            return _fail(reference, PredictionFailure.SCHEMA_MISMATCH, str(exc))
        except ValueError as exc:
            return _fail(reference, PredictionFailure.INVALID_PROBABILITIES, str(exc))
        except Exception as exc:  # a native-library failure must never escape into trading
            return _fail(
                reference, PredictionFailure.INFERENCE_ERROR, f"{type(exc).__name__}: {exc}"
            )
        elapsed = _time.perf_counter() - begun
        if elapsed > self._max_inference:
            with self._lock:
                self._diagnostics.slow_predictions += 1
            return _fail(
                reference,
                PredictionFailure.INFERENCE_TIMEOUT,
                f"inference took {elapsed * 1000:.0f}ms > {self._max_inference * 1000:.0f}ms",
            )

        checked = self._check_prediction(model, symbol, vector, prediction)
        if isinstance(checked, _Failure):
            return _fail(reference, checked.code, checked.detail)
        predicted_at = max(self._clock(), last.end)
        served = self._served(
            model,
            symbol,
            symbol_trained,
            vector,
            checked,
            last,
            predicted_at,
            fingerprint or None,
            position_features,
            (_time.perf_counter() - started) * 1000.0,
        )
        if isinstance(served, _Failure):
            return _fail(reference, served.code, served.detail)
        with self._lock:
            self._predictions[key] = served
            self._predictions.move_to_end(key)
            while len(self._predictions) > self._cache_size:
                self._predictions.popitem(last=False)
            self._diagnostics.predictions += 1
            self._diagnostics.total_inference_ms += served.inference_ms
            self._diagnostics.max_inference_ms = max(
                self._diagnostics.max_inference_ms, served.inference_ms
            )
        return ModelPredictionResult(reference, prediction=served)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _content_fingerprint(
        model: LoadedModel,
        bars: Sequence[ModelBar],
        references: Mapping[str, Sequence[ModelBar]],
    ) -> int:
        """Process-local digest of the exact bars used, so a revised bar never hits the cache."""

        def row(bar: ModelBar) -> tuple[object, ...]:
            return (
                bar.start,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.vwap,
                bar.feed,
                bar.session_id,
            )

        window = model.feature_window
        parts: list[object] = [tuple(row(bar) for bar in bars)]
        for name in model.manifest.feature_schema.required_reference_symbols:
            parts.append((name, tuple(row(bar) for bar in references.get(name, ())[-window:])))
        return hash(tuple(parts))

    @staticmethod
    def _provenance_failure(manifest: ModelArtifactManifest, bar: ModelBar) -> _Failure | None:
        if bar.feed != manifest.feed:
            return _Failure(
                PredictionFailure.FEED_MISMATCH,
                f"bar feed {bar.feed!r} differs from model feed {manifest.feed!r}",
            )
        if bar.timeframe != manifest.timeframe:
            return _Failure(
                PredictionFailure.TIMEFRAME_MISMATCH,
                f"bar timeframe {bar.timeframe!r} differs from {manifest.timeframe!r}",
            )
        if bar.adjustment != manifest.adjustment:
            return _Failure(
                PredictionFailure.ADJUSTMENT_MISMATCH,
                f"bar adjustment {bar.adjustment!r} differs from {manifest.adjustment!r}",
            )
        return None

    def _base_features(
        self,
        model: LoadedModel,
        symbol: str,
        bars: Sequence[ModelBar],
        references: Mapping[str, Sequence[ModelBar]],
        content: int,
    ) -> FeatureVector | _Failure:
        last = bars[-1]
        feature_key = (model.manifest.feature_schema_sha256, symbol, last.end, content)
        with self._lock:
            reusable = self._features.get(feature_key)
            if reusable is not None:
                self._features.move_to_end(feature_key)
                self._diagnostics.feature_reuse += 1
                return reusable
        window = model.feature_window
        recent = tuple(bars[-window:])
        starts = {bar.start for bar in recent}
        pipeline = IntradayFeaturePipeline(model.base_config)
        aligned_refs: dict[str, dict[datetime, ModelBar]] = {
            name: {bar.start: bar for bar in series if bar.start in starts}
            for name, series in references.items()
            if name in model.base_config.reference_symbols
        }
        produced: FeatureVector | None = None
        try:
            for bar in recent:
                aligned = {
                    name: index[bar.start]
                    for name, index in aligned_refs.items()
                    if bar.start in index
                }
                produced = pipeline.update(bar, aligned)
        except ValueError as exc:
            return _Failure(PredictionFailure.FEATURES_UNAVAILABLE, str(exc))
        if produced is None or produced.bar_start != last.start:
            reason = (
                "VWAP missing for the latest bar"
                if model.base_config.include_vwap and last.vwap is None
                else "history is not contiguous within one session or a reference bar is missing"
            )
            code = (
                PredictionFailure.VWAP_UNAVAILABLE
                if model.base_config.include_vwap and last.vwap is None
                else PredictionFailure.FEATURES_UNAVAILABLE
            )
            return _Failure(code, reason)
        if tuple(produced.values) != model.base_schema.names:
            return _Failure(
                PredictionFailure.SCHEMA_MISMATCH, "feature order drifted from the schema"
            )
        with self._lock:
            self._features[feature_key] = produced
            self._features.move_to_end(feature_key)
            while len(self._features) > self._feature_cache_size:
                self._features.popitem(last=False)
        return produced

    def _position_vector(
        self,
        model: LoadedModel,
        base: FeatureVector,
        bars: Sequence[ModelBar],
        position: PositionFacts,
    ) -> tuple[FeatureVector, Mapping[str, float]] | _Failure:
        last = bars[-1]
        entry_bar_start = position.entry_time.replace(second=0, microsecond=0)
        if entry_bar_start > last.start:
            return _Failure(
                PredictionFailure.POSITION_INVALID,
                "position entry is after the latest completed bar",
            )
        held = [bar for bar in bars if bar.start >= entry_bar_start]
        if not held or held[0].start != entry_bar_start:
            return _Failure(
                PredictionFailure.POSITION_ENTRY_BAR_UNAVAILABLE,
                f"entry bar {entry_bar_start.isoformat()} is not in the completed-bar history",
            )
        if any(bar.session_id != last.session_id for bar in held):
            return _Failure(
                PredictionFailure.POSITION_OUTSIDE_TRAINING_SUPPORT,
                "position was opened in an earlier session",
            )
        holding_bars = len(held)
        limit = model.max_supported_holding_bars
        assert limit is not None
        if holding_bars > limit:
            return _Failure(
                PredictionFailure.POSITION_OUTSIDE_TRAINING_SUPPORT,
                f"holding {holding_bars} bars exceeds the model's supported {limit}",
            )
        features = position_feature_values(
            entry_price=position.entry_price,
            close=last.close,
            maximum_high=max(bar.high for bar in held),
            minimum_low=min(bar.low for bar in held),
            holding_bars=holding_bars,
        )
        values = dict(base.values)
        values.update(features)
        schema = model.manifest.feature_schema
        vector = FeatureVector(
            base.symbol,
            base.bar_start,
            base.as_of,
            base.session_id,
            base.feed,
            schema.feature_id,
            schema.version,
            values,
        )
        return vector, MappingProxyType(features)

    @staticmethod
    def _check_prediction(
        model: LoadedModel, symbol: str, vector: FeatureVector, prediction: object
    ) -> ModelPrediction | _Failure:
        manifest = model.manifest
        if not isinstance(prediction, ModelPrediction):
            return _Failure(
                PredictionFailure.INFERENCE_ERROR, "predictor returned a foreign object"
            )
        if (
            prediction.model_id != manifest.model_id
            or prediction.model_version != manifest.model_version
        ):
            return _Failure(
                PredictionFailure.PROVENANCE_MISMATCH,
                f"prediction is for {prediction.model_id}@{prediction.model_version}",
            )
        if prediction.symbol != symbol:
            return _Failure(
                PredictionFailure.SYMBOL_MISMATCH,
                f"prediction is for {prediction.symbol}, request is for {symbol}",
            )
        if (
            prediction.feature_schema_id != manifest.feature_schema.feature_id
            or prediction.feature_schema_version != manifest.feature_schema.version
        ):
            return _Failure(PredictionFailure.SCHEMA_MISMATCH, "prediction feature schema differs")
        if prediction.as_of != vector.as_of:
            return _Failure(
                PredictionFailure.PROVENANCE_MISMATCH,
                "prediction is not for the observed bar",
            )
        if manifest.label_schema.task is LearningTask.REGRESSION:
            if prediction.kind is not PredictionKind.REGRESSION or prediction.value is None:
                return _Failure(
                    PredictionFailure.INVALID_PROBABILITIES, "expected a regression value"
                )
            if not math.isfinite(prediction.value):
                return _Failure(
                    PredictionFailure.NONFINITE_PREDICTION, "regression value is not finite"
                )
            return prediction
        if prediction.kind is not PredictionKind.CLASS:
            return _Failure(PredictionFailure.INVALID_PROBABILITIES, "expected class probabilities")
        if tuple(prediction.probabilities) != tuple(manifest.class_labels):
            return _Failure(
                PredictionFailure.SCHEMA_MISMATCH,
                "probability classes/order differ from the manifest",
            )
        if any(not math.isfinite(p) for p in prediction.probabilities.values()):
            return _Failure(PredictionFailure.NONFINITE_PREDICTION, "a probability is not finite")
        return prediction

    @staticmethod
    def _served(
        model: LoadedModel,
        symbol: str,
        symbol_trained: bool,
        vector: FeatureVector,
        prediction: object,
        last: ModelBar,
        predicted_at: datetime,
        fingerprint: str | None,
        position_features: Mapping[str, float],
        inference_ms: float,
    ) -> ServedPrediction | _Failure:
        assert isinstance(prediction, ModelPrediction)
        manifest = model.manifest
        schema = manifest.feature_schema
        try:
            return ServedPrediction(
                model_id=manifest.model_id,
                model_version=manifest.model_version,
                artifact_sha256=manifest.artifact_sha256,
                algorithm=model.reference.algorithm,
                role=model.reference.role,
                task=manifest.label_schema.task.value,
                symbol=symbol,
                feature_schema_id=schema.feature_id,
                feature_schema_version=schema.version,
                feature_schema_sha256=manifest.feature_schema_sha256,
                feed=manifest.feed,
                timeframe=manifest.timeframe,
                adjustment=manifest.adjustment,
                observation_start=last.start,
                observation_end=last.end,
                predicted_at=predicted_at,
                horizon_bars=manifest.label_schema.horizon_bars,
                label_id=manifest.label_schema.label_id,
                class_labels=tuple(manifest.class_labels),
                probabilities=dict(prediction.probabilities),
                value=prediction.value,
                class_net_returns=dict(model.class_returns),
                class_return_basis=model.class_return_basis,
                symbol_in_training_universe=symbol_trained,
                position_fingerprint=fingerprint,
                position_features=position_features,
                inference_ms=inference_ms,
            )
        except ValueError as exc:
            return _Failure(PredictionFailure.INVALID_PROBABILITIES, str(exc))
