"""ModelInferenceService: pinning, verification, provenance, caching and fail-closed validation.

Real native XGBoost/LightGBM artifacts (tiny, deterministic) go through the production
repository and manifest path; stub adapters are used only to force specific failure modes.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from model_fixtures import (
    BuiltModel,
    build_model,
    falling,
    flat,
    model_bars,
    rising,
    sell_builder,
)

from mike_trade.domain.model_signals import ModelReference, PositionFacts, PredictionFailure
from mike_trade.modeling.artifacts import FileModelRepository
from mike_trade.modeling.contracts import (
    FeatureVector,
    ModelPrediction,
    ModelPredictor,
    PredictionKind,
)
from mike_trade.modeling.inference import (
    InferenceRequest,
    ModelInferenceService,
    ModelLoadError,
)
from mike_trade.modeling.matrix import FeatureSchemaMismatch
from mike_trade.modeling.registry import ModelAdapterRegistry
from mike_trade.modeling.serving import build_inference_service

ALGORITHMS = ("xgboost", "lightgbm")


def _service(root: Path, references: list[ModelReference], at) -> ModelInferenceService:
    return build_inference_service(root, references, lambda: at)


def _registry() -> ModelAdapterRegistry:
    from mike_trade.modeling.adapters import LightGBMAdapterFactory, XGBoostAdapterFactory

    registry = ModelAdapterRegistry()
    registry.register(XGBoostAdapterFactory())
    registry.register(LightGBMAdapterFactory())
    return registry


def _inputs(count: int = 40, closes=None, symbol: str = "AAA", **bar_kwargs):
    primary = model_bars(symbol, closes or rising(count, step="0.30"), **bar_kwargs)
    spy = model_bars("SPY", flat(len(primary)), **bar_kwargs)
    return primary, {"SPY": spy}


@pytest.fixture(scope="module")
def models(tmp_path_factory) -> dict[tuple[str, str], BuiltModel]:
    root = tmp_path_factory.mktemp("models")
    return {
        (algorithm, role): build_model(root, algorithm=algorithm, role=role)
        for algorithm in ALGORITHMS
        for role in ("buy", "sell")
    }


def _root(models) -> Path:
    return next(iter(models.values())).root


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_buy_prediction_carries_full_provenance(models, algorithm):
    built = models[(algorithm, "buy")]
    bars, refs = _inputs()
    service = _service(_root(models), [built.reference], bars[-1].end + timedelta(seconds=2))
    result = service.predict(InferenceRequest(built.reference, "AAA", bars, refs))
    prediction = result.prediction
    assert prediction is not None, result.detail
    manifest = built.manifest
    assert prediction.model_id == manifest.model_id and prediction.model_version == "1.0.0"
    assert prediction.artifact_sha256 == manifest.artifact_sha256
    assert prediction.algorithm == algorithm and prediction.role == "buy"
    assert prediction.feature_schema_sha256 == manifest.feature_schema_sha256
    assert (prediction.feed, prediction.timeframe, prediction.adjustment) == ("iex", "1m", "raw")
    assert prediction.symbol == "AAA" and prediction.symbol_in_training_universe
    assert prediction.observation_end == bars[-1].end
    assert prediction.observation_start == bars[-1].start
    assert prediction.predicted_at >= prediction.observation_end
    assert prediction.class_labels == ("-1", "0", "1")
    assert sum(prediction.probabilities.values()) == pytest.approx(1.0)
    assert prediction.probabilities["1"] > 0.5  # a rising tape favors the take-profit class
    assert set(prediction.class_net_returns) == {"-1", "0", "1"}
    assert prediction.position_fingerprint is None


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_sell_prediction_uses_the_actual_entry_fill_price(models, algorithm):
    built = models[(algorithm, "sell")]
    bars, refs = _inputs(closes=flat(40))
    service = _service(_root(models), [built.reference], bars[-1].end + timedelta(seconds=2))

    def predict(entry: str):
        position = PositionFacts("AAA", "cycle-1", D("10"), D(entry), bars[-3].start)
        result = service.predict(InferenceRequest(built.reference, "AAA", bars, refs, position))
        assert result.prediction is not None, result.detail
        return result.prediction

    hold, exit_now, risk = predict("100"), predict("99"), predict("101")
    assert hold.probabilities["1"] > 0.5  # flat P&L: keep holding
    assert exit_now.probabilities["0"] > 0.5  # +1.0% on the actual fill: exit now
    assert risk.probabilities["-1"] > 0.5  # -1.0% on the actual fill: risk exit
    assert exit_now.position_features["position_unrealized_return"] == pytest.approx(1 / 99)
    assert exit_now.position_features["position_holding_bars"] == 3.0
    assert len({hold.position_fingerprint, exit_now.position_fingerprint}) == 2


def test_position_features_equal_the_training_builder_features(models):
    """Inference and the SELL training builder produce identical vectors for the same position."""
    built = models[("xgboost", "sell")]
    closes = [D("100") + D("0.03") * ((i * 7) % 11) - D("0.1") * (i % 3) for i in range(110)]
    bars = model_bars("AAA", closes)
    spy = model_bars("SPY", [D("200") + D("0.02") * (i % 9) for i in range(110)])
    builder = sell_builder()
    examples = list(builder.build_symbol(bars, references={"SPY": spy}))
    assert examples
    captured: list[FeatureVector] = []

    class Capturing:
        def __init__(self, inner: ModelPredictor) -> None:
            self.inner = inner

        @property
        def feature_schema(self):
            return self.inner.feature_schema

        def predict(self, features: FeatureVector) -> ModelPrediction:
            captured.append(features)
            return self.inner.predict(features)

    for example in (examples[0], examples[len(examples) // 2], examples[-1]):
        end = example.features.as_of
        window = [bar for bar in bars if bar.end <= end]
        position = PositionFacts(
            "AAA",
            "cycle-parity",
            D("1"),
            D(example.metadata["position_entry_price"]),
            __import__("datetime").datetime.fromisoformat(example.metadata["position_entry_time"]),
        )
        service = _service(_root(models), [built.reference], end + timedelta(seconds=1))
        loaded = service.loaded(built.reference)
        assert loaded is not None
        object.__setattr__(loaded, "predictor", Capturing(loaded.predictor))
        result = service.predict(
            InferenceRequest(
                built.reference,
                "AAA",
                window,
                {"SPY": [bar for bar in spy if bar.end <= end]},
                position,
            )
        )
        assert result.prediction is not None, result.detail
        vector = captured[-1]
        assert tuple(vector.values) == tuple(example.features.values)
        assert dict(vector.values) == dict(example.features.values)
        assert vector.schema_id == example.features.schema_id
        assert vector.as_of == example.features.as_of


def test_buy_features_equal_batch_transform(models):
    """The replayed window equals the continuous batch/streaming pipeline value for value."""
    from model_fixtures import feature_config

    from mike_trade.modeling.features import IntradayFeaturePipeline

    closes = [D("100") + D("0.07") * ((i * 5) % 13) for i in range(60)]
    bars = model_bars("AAA", closes)
    spy = model_bars("SPY", [D("300") + D("0.05") * (i % 7) for i in range(60)])
    batch = list(IntradayFeaturePipeline(feature_config()).transform(bars, references={"SPY": spy}))
    built = models[("lightgbm", "buy")]
    captured: list[FeatureVector] = []
    service = _service(_root(models), [built.reference], bars[-1].end + timedelta(seconds=1))
    loaded = service.loaded(built.reference)
    assert loaded is not None
    inner = loaded.predictor

    class Capturing:
        feature_schema = inner.feature_schema

        def predict(self, features: FeatureVector) -> ModelPrediction:
            captured.append(features)
            return inner.predict(features)

    object.__setattr__(loaded, "predictor", Capturing())
    result = service.predict(InferenceRequest(built.reference, "AAA", bars, {"SPY": spy}))
    assert result.prediction is not None
    assert dict(captured[-1].values) == dict(batch[-1].values)


# ------------------------------------------------------------------ pinning and artifacts


def test_exact_version_pinning_never_resolves_latest(models):
    built = models[("xgboost", "buy")]
    wrong = replace(built.reference, model_version="1.0.1")
    with pytest.raises(ModelLoadError) as raised:
        _service(_root(models), [wrong], built.manifest.created_at)
    assert raised.value.code == "MODEL_MISSING"


def test_algorithm_role_and_checksum_pins_are_enforced(models):
    xgb_buy = models[("xgboost", "buy")]
    at = xgb_buy.manifest.created_at
    root = _root(models)
    with pytest.raises(ModelLoadError) as algorithm:
        _service(root, [replace(xgb_buy.reference, algorithm="lightgbm")], at)
    assert algorithm.value.code == "ALGORITHM_MISMATCH"
    with pytest.raises(ModelLoadError) as role:
        _service(root, [replace(xgb_buy.reference, role="sell")], at)
    assert role.value.code == "ROLE_MISMATCH"
    with pytest.raises(ModelLoadError) as checksum:
        _service(root, [replace(xgb_buy.reference, artifact_sha256="0" * 64)], at)
    assert checksum.value.code == "CHECKSUM_MISMATCH"
    ok = replace(xgb_buy.reference, artifact_sha256=xgb_buy.manifest.artifact_sha256)
    assert _service(root, [ok], at).loaded(ok) is not None


def test_missing_and_corrupt_artifacts_fail_at_startup(tmp_path):
    built = build_model(tmp_path, algorithm="lightgbm", role="buy")
    at = built.manifest.created_at
    original = built.artifact_path.read_bytes()
    built.artifact_path.write_bytes(original[:-5] + b"XXXXX")
    with pytest.raises(ModelLoadError) as corrupt:
        _service(tmp_path, [built.reference], at)
    assert corrupt.value.code == "CHECKSUM_MISMATCH"
    built.artifact_path.unlink()
    with pytest.raises(ModelLoadError) as missing:
        _service(tmp_path, [built.reference], at)
    assert missing.value.code == "MODEL_MISSING"
    empty = tmp_path / "empty"
    with pytest.raises(ModelLoadError) as nothing:
        _service(empty, [built.reference], at)
    assert nothing.value.code == "MODEL_MISSING"


def test_feature_schema_that_the_pipeline_cannot_reproduce_is_refused(tmp_path):
    from mike_trade.modeling.artifacts import ModelArtifactManifest, schema_fingerprint
    from mike_trade.modeling.contracts import FeatureSchema

    built = build_model(tmp_path / "base", algorithm="xgboost", role="buy")
    schema = built.manifest.feature_schema
    drifted = FeatureSchema(
        schema.feature_id,
        schema.version,
        (*schema.names, "mystery_feature"),
        schema.required_reference_symbols,
        schema.timeframe,
    )
    manifest = replace(
        built.manifest, feature_schema=drifted, feature_schema_sha256=schema_fingerprint(drifted)
    )
    assert isinstance(manifest, ModelArtifactManifest)
    path = tmp_path / "drift" / "manifests" / manifest.model_id / "1.0.0.json"
    path.parent.mkdir(parents=True)
    path.write_text(manifest.canonical() + "\n")
    artifact_dir = tmp_path / "drift" / "artifacts" / manifest.artifact_sha256
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "model.xgboost_ubj").write_bytes(built.artifact_path.read_bytes())
    with pytest.raises(ModelLoadError) as raised:
        _service(tmp_path / "drift", [built.reference], built.manifest.created_at)
    assert raised.value.code == "SCHEMA_MISMATCH"


def test_sell_model_requires_recorded_continuation_class_semantics(tmp_path):
    from mike_trade.modeling.artifacts import schema_fingerprint

    built = build_model(tmp_path, algorithm="xgboost", role="sell")
    wrong_label = replace(
        built.manifest.label_schema,
        parameters={**built.manifest.label_schema.parameters, "classes": "-1=HOLD,0=EXIT,1=RISK"},
    )
    manifest = replace(
        built.manifest,
        label_schema=wrong_label,
        label_schema_sha256=schema_fingerprint(wrong_label),
    )
    manifest_path = (
        tmp_path / "manifests" / manifest.model_id / f"{manifest.model_version}.json"
    )
    manifest_path.write_text(manifest.canonical() + "\n")

    with pytest.raises(ModelLoadError) as raised:
        _service(tmp_path, [built.reference], built.manifest.created_at)
    assert raised.value.code == "SCHEMA_MISMATCH"


def test_model_is_loaded_once_and_reused(models):
    built = models[("xgboost", "buy")]
    registry = _registry()
    loads = {"count": 0}
    factory = registry.get("xgboost", "1.0.0")
    original = factory.predictor

    def counting(artifact: bytes, manifest=None):
        loads["count"] += 1
        return original(artifact, manifest)

    factory.predictor = counting  # type: ignore[method-assign]
    bars, refs = _inputs()
    service = ModelInferenceService(
        FileModelRepository(_root(models)),
        registry,
        [built.reference],
        clock=lambda: bars[-1].end + timedelta(seconds=1),
    )
    assert loads["count"] == 1
    for end in range(25, 41):
        window = bars[:end]
        service.predict(
            InferenceRequest(built.reference, "AAA", window, {"SPY": refs["SPY"][:end]})
        )
    assert loads["count"] == 1  # never reloaded per candle


# ------------------------------------------------------------------ provenance validation


def _failure(models, **overrides):
    built = models[("lightgbm", "buy")]
    closes = overrides.pop("closes", None)
    bars, refs = _inputs(closes=closes, **overrides.pop("bar_kwargs", {}))
    now = overrides.pop("now", bars[-1].end + timedelta(seconds=1))
    service = _service(_root(models), [built.reference], now)
    return service.predict(InferenceRequest(built.reference, "AAA", bars, refs))


def test_feed_mismatch_is_refused_not_substituted(models):
    result = _failure(models, bar_kwargs={"feed": "sip"})
    assert result.failure is PredictionFailure.FEED_MISMATCH and result.prediction is None


def test_missing_vwap_makes_features_unavailable_not_fabricated(models):
    result = _failure(models, bar_kwargs={"with_vwap": False})
    assert result.failure is PredictionFailure.VWAP_UNAVAILABLE


def test_stale_and_future_observations_are_refused(models):
    bars, _ = _inputs()
    stale = _failure(models, now=bars[-1].end + timedelta(minutes=11))
    assert stale.failure is PredictionFailure.STALE_OBSERVATION
    future = _failure(models, now=bars[-1].end - timedelta(seconds=1))
    assert future.failure is PredictionFailure.FUTURE_OBSERVATION


def test_insufficient_history_and_missing_reference_fail_closed(models):
    built = models[("lightgbm", "buy")]
    bars, refs = _inputs()
    service = _service(_root(models), [built.reference], bars[-1].end + timedelta(seconds=1))
    short = service.predict(InferenceRequest(built.reference, "AAA", bars[-5:], refs))
    assert short.failure is PredictionFailure.INSUFFICIENT_HISTORY
    no_spy = service.predict(InferenceRequest(built.reference, "AAA", bars, {}))
    assert no_spy.failure is PredictionFailure.REFERENCE_BAR_MISSING
    lagging = service.predict(
        InferenceRequest(built.reference, "AAA", bars, {"SPY": refs["SPY"][:-1]})
    )
    assert lagging.failure is PredictionFailure.REFERENCE_BAR_MISSING


def test_gap_in_recent_bars_resets_features(models):
    built = models[("lightgbm", "buy")]
    bars, refs = _inputs()
    gapped = bars[:30] + bars[31:]  # one missing minute inside the feature window
    service = _service(_root(models), [built.reference], bars[-1].end + timedelta(seconds=1))
    result = service.predict(InferenceRequest(built.reference, "AAA", gapped, refs))
    assert result.failure is PredictionFailure.FEATURES_UNAVAILABLE


def test_primary_symbol_equal_to_reference_is_refused(models):
    built = models[("lightgbm", "buy")]
    spy = model_bars("SPY", rising(40, step="0.30"))
    service = _service(_root(models), [built.reference], spy[-1].end + timedelta(seconds=1))
    result = service.predict(InferenceRequest(built.reference, "SPY", spy, {"SPY": spy}))
    assert result.failure is PredictionFailure.PRIMARY_IS_REFERENCE_SYMBOL


def test_sell_requires_a_valid_position_inside_training_support(models):
    built = models[("xgboost", "sell")]
    bars, refs = _inputs(closes=flat(80))
    service = _service(_root(models), [built.reference], bars[-1].end + timedelta(seconds=1))

    def run(position):
        return service.predict(InferenceRequest(built.reference, "AAA", bars, refs, position))

    assert run(None).failure is PredictionFailure.POSITION_REQUIRED
    other = PositionFacts("BBB", "c", D("1"), D("100"), bars[-3].start)
    assert run(other).failure is PredictionFailure.POSITION_INVALID
    too_old = PositionFacts("AAA", "c", D("1"), D("100"), bars[-60].start)
    assert run(too_old).failure is PredictionFailure.POSITION_OUTSIDE_TRAINING_SUPPORT
    missing_bar = PositionFacts("AAA", "c", D("1"), D("100"), bars[0].start - timedelta(hours=1))
    assert run(missing_bar).failure is PredictionFailure.POSITION_ENTRY_BAR_UNAVAILABLE
    after = PositionFacts("AAA", "c", D("1"), D("100"), bars[-1].end + timedelta(minutes=5))
    assert run(after).failure is PredictionFailure.POSITION_INVALID


def test_buy_model_rejects_a_position_and_untrained_symbols_are_flagged(models):
    built = models[("xgboost", "buy")]
    bars, refs = _inputs(symbol="QQQ")
    service = _service(_root(models), [built.reference], bars[-1].end + timedelta(seconds=1))
    result = service.predict(InferenceRequest(built.reference, "QQQ", bars, refs))
    assert result.prediction is not None and not result.prediction.symbol_in_training_universe
    position = PositionFacts("QQQ", "c", D("1"), D("100"), bars[-3].start)
    bad = service.predict(InferenceRequest(built.reference, "QQQ", bars, refs, position))
    assert bad.failure is PredictionFailure.POSITION_INVALID


# ------------------------------------------------------------------ forced predictor failures


class _StubPredictor:
    def __init__(self, real: ModelPredictor, behavior: str) -> None:
        self.real, self.behavior = real, behavior

    @property
    def feature_schema(self):
        return self.real.feature_schema

    def predict(self, features: FeatureVector) -> ModelPrediction:
        if self.behavior == "schema":
            raise FeatureSchemaMismatch("prediction feature ordering is incompatible")
        if self.behavior == "crash":
            raise RuntimeError("native library failure")
        real = self.real.predict(features)
        base: dict[str, Any] = {
            "model_id": real.model_id,
            "model_version": real.model_version,
            "symbol": real.symbol,
            "as_of": real.as_of,
            "feature_schema_id": real.feature_schema_id,
            "feature_schema_version": real.feature_schema_version,
            "kind": PredictionKind.CLASS,
            "predicted_class": "1",
        }
        if self.behavior == "wrong_model":
            return ModelPrediction(
                **{**base, "model_version": "9.9.9"}, probabilities=dict(real.probabilities)
            )
        if self.behavior == "wrong_symbol":
            return ModelPrediction(
                **{**base, "symbol": "BBB"}, probabilities=dict(real.probabilities)
            )
        if self.behavior == "wrong_bar":
            return ModelPrediction(
                **{**base, "as_of": real.as_of + timedelta(minutes=1)},
                probabilities=dict(real.probabilities),
            )
        if self.behavior == "wrong_classes":
            return ModelPrediction(**base, probabilities={"1": 0.5, "0": 0.3, "-1": 0.2})
        if self.behavior == "nan":
            object.__setattr__(real, "probabilities", {"-1": float("nan"), "0": 0.5, "1": 0.5})
            return real
        raise AssertionError(self.behavior)


@pytest.mark.parametrize(
    ("behavior", "expected"),
    [
        ("schema", PredictionFailure.SCHEMA_MISMATCH),
        ("crash", PredictionFailure.INFERENCE_ERROR),
        ("wrong_model", PredictionFailure.PROVENANCE_MISMATCH),
        ("wrong_symbol", PredictionFailure.SYMBOL_MISMATCH),
        ("wrong_bar", PredictionFailure.PROVENANCE_MISMATCH),
        ("wrong_classes", PredictionFailure.SCHEMA_MISMATCH),
    ],
)
def test_untrustworthy_predictor_output_is_rejected(models, behavior, expected):
    built = models[("xgboost", "buy")]
    bars, refs = _inputs()
    service = _service(_root(models), [built.reference], bars[-1].end + timedelta(seconds=1))
    loaded = service.loaded(built.reference)
    assert loaded is not None
    object.__setattr__(loaded, "predictor", _StubPredictor(loaded.predictor, behavior))
    result = service.predict(InferenceRequest(built.reference, "AAA", bars, refs))
    assert result.failure is expected and result.prediction is None
    assert service.diagnostics()["failures"] == {expected.value: 1}


def test_invalid_probability_distribution_is_rejected(models):
    built = models[("xgboost", "buy")]
    bars, refs = _inputs()
    service = _service(_root(models), [built.reference], bars[-1].end + timedelta(seconds=1))
    loaded = service.loaded(built.reference)
    assert loaded is not None

    class Skewed:
        feature_schema = loaded.predictor.feature_schema

        def predict(self, features: FeatureVector) -> ModelPrediction:
            return ModelPrediction(
                loaded.manifest.model_id,
                "1.0.0",
                features.symbol,
                features.as_of,
                features.schema_id,
                features.schema_version,
                PredictionKind.CLASS,
                "1",
                {"-1": 0.4, "0": 0.4, "1": 0.4},
            )

    object.__setattr__(loaded, "predictor", Skewed())
    result = service.predict(InferenceRequest(built.reference, "AAA", bars, refs))
    assert result.failure is PredictionFailure.INVALID_PROBABILITIES


def test_slow_inference_is_refused(models):
    built = models[("xgboost", "buy")]
    bars, refs = _inputs()
    service = ModelInferenceService(
        FileModelRepository(_root(models)),
        _registry(),
        [built.reference],
        clock=lambda: bars[-1].end + timedelta(seconds=1),
        max_inference_seconds=1e-9,
    )
    result = service.predict(InferenceRequest(built.reference, "AAA", bars, refs))
    assert result.failure is PredictionFailure.INFERENCE_TIMEOUT
    assert service.diagnostics()["slow_predictions"] == 1


# ------------------------------------------------------------------ cache behavior


def test_cache_reuses_per_model_symbol_and_bar_but_never_across_them(models):
    built = models[("xgboost", "buy")]
    bars, refs = _inputs()
    service = _service(_root(models), [built.reference], bars[-1].end + timedelta(seconds=1))
    first = service.predict(InferenceRequest(built.reference, "AAA", bars, refs))
    again = service.predict(InferenceRequest(built.reference, "AAA", bars, refs))
    assert first.prediction is again.prediction  # the identical immutable object
    diagnostics = service.diagnostics()
    assert diagnostics["cache_hits"] == 1 and diagnostics["predictions"] == 1

    earlier = service.predict(
        InferenceRequest(built.reference, "AAA", bars[:-1], {"SPY": refs["SPY"][:-1]})
    )
    assert earlier.prediction is not first.prediction
    assert earlier.prediction is not None and first.prediction is not None
    assert earlier.prediction.observation_end == bars[-2].end  # never mislabeled as later
    other_symbol_bars = model_bars("BBB", falling(40, step="0.30"))
    other = service.predict(InferenceRequest(built.reference, "BBB", other_symbol_bars, refs))
    assert other.prediction is not None
    assert other.prediction.symbol == "BBB"
    assert other.prediction.probabilities["-1"] > other.prediction.probabilities["1"]
    # a revised bar with the same timestamp must not hit the stale cached prediction
    revised = model_bars("AAA", falling(40, step="0.30"))
    changed = service.predict(InferenceRequest(built.reference, "AAA", revised, refs))
    assert changed.prediction is not None and changed.prediction is not first.prediction
    assert changed.prediction.probabilities["-1"] > changed.prediction.probabilities["1"]


def test_cache_is_bounded(models):
    built = models[("lightgbm", "buy")]
    bars, refs = _inputs(60)
    service = ModelInferenceService(
        FileModelRepository(_root(models)),
        _registry(),
        [built.reference],
        clock=lambda: bars[-1].end + timedelta(seconds=1),
        cache_size=4,
        feature_cache_size=4,
    )
    for end in range(25, 55):
        service.predict(
            InferenceRequest(built.reference, "AAA", bars[:end], {"SPY": refs["SPY"][:end]})
        )
    assert service.diagnostics()["cache_entries"] <= 4


def test_identical_features_are_reused_across_models_and_requests_are_thread_safe(models):
    xgb_buy, lgb_buy = models[("xgboost", "buy")], models[("lightgbm", "buy")]
    bars, refs = _inputs()
    service = _service(
        _root(models), [xgb_buy.reference, lgb_buy.reference], bars[-1].end + timedelta(seconds=1)
    )
    first = service.predict(InferenceRequest(xgb_buy.reference, "AAA", bars, refs))
    second = service.predict(InferenceRequest(lgb_buy.reference, "AAA", bars, refs))
    assert first.prediction and second.prediction
    assert first.prediction.algorithm == "xgboost" and second.prediction.algorithm == "lightgbm"
    assert service.diagnostics()["feature_reuse"] == 1

    barrier = threading.Barrier(8)

    def work(index: int):
        barrier.wait()
        reference = (xgb_buy, lgb_buy)[index % 2].reference
        return service.predict(InferenceRequest(reference, "AAA", bars, refs))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(8)))
    assert all(item.prediction is not None for item in results)
    assert {item.prediction.algorithm for item in results if item.prediction} == {
        "xgboost",
        "lightgbm",
    }
    for item in results:
        assert item.prediction is not None
        expected = (first, second)[item.prediction.algorithm == "lightgbm"].prediction
        assert expected is not None
        assert dict(item.prediction.probabilities) == dict(expected.probabilities)


def test_several_models_are_served_simultaneously(models):
    references = [built.reference for built in models.values()]
    created = models[("xgboost", "buy")].manifest.created_at
    service = _service(_root(models), references, created)
    assert len(service.references) == 4
    assert {(ref.algorithm, ref.role) for ref in service.references} == {
        (algorithm, role) for algorithm in ALGORITHMS for role in ("buy", "sell")
    }
    assert service.required_reference_symbols() == ("SPY",)
