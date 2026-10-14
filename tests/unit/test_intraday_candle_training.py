"""End-to-end proof of the candle-window training path on a tiny synthetic dataset.

A real XGBoost model is trained in a temporary directory (nothing under ``research/`` is read or
written), then reloaded; training-time and streaming feature vectors are compared.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import yaml
from test_model_workflow import build_dataset

from mike_trade.domain.errors import MikeTradeError
from mike_trade.domain.model_signals import ModelReference
from mike_trade.modeling.aggregation import AggregationPolicy, CandleAggregator
from mike_trade.modeling.artifacts import FileModelRepository
from mike_trade.modeling.builder import AggregatedTrainingDatasetBuilder
from mike_trade.modeling.candle_features import CandleWindowFeaturePipeline
from mike_trade.modeling.config import load_experiment_config
from mike_trade.modeling.contracts import FeatureVector
from mike_trade.modeling.datasets import ParquetDatasetProvider
from mike_trade.modeling.adapters import XGBoostAdapterFactory
from mike_trade.modeling.inference import InferenceRequest, ModelInferenceService, ModelLoadError
from mike_trade.modeling.labels import (
    AggregatedTripleBarrierConfig,
    AggregatedTripleBarrierLabelGenerator,
)
from mike_trade.modeling.matrix import matrix_from_examples
from mike_trade.modeling.model_reports import write_model_report
from mike_trade.modeling.operations import prepare_training_lock
from mike_trade.modeling.registry import ModelAdapterRegistry
from mike_trade.modeling.training import ModelTrainingService, load_model_predictor


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    base = tmp_path_factory.mktemp("candle-training")
    data, models = base / "data", base / "models"
    build_dataset(data, crash=False)
    document = yaml.safe_load(Path("config/modeling/xgboost_buy_5m_tb100_050.yaml").read_text())
    document.update(
        dataset_root=str(data), model_root=str(models), symbols=["AAA", "BBB"], seed=7,
        resources={"device": "cpu", "threads": 1}, batch_size=64,
    )
    document.pop("universe_file")
    document["universe_id"] = "synthetic_v1"
    document["ranges"] = {
        "fit_start": "2026-03-10T00:00:00Z", "fit_end": "2026-03-18T00:00:00Z",
        "early_stopping_start": "2026-03-18T00:00:00Z", "early_stopping_end": "2026-03-20T00:00:00Z",
        "calibration_start": "2026-03-20T00:00:00Z", "calibration_end": "2026-03-21T00:00:00Z",
        "evaluation_start": "2026-03-23T00:00:00Z", "evaluation_end": "2026-03-26T00:00:00Z",
        "embargo_minutes": 0,
    }
    document["features"].update(lookback_bars=4, recency_decay={"half_life_bars": 2.0})
    document["objective"].update(
        horizon_bars=4, positive_barrier="0.004", negative_barrier="0.002"
    )
    document["hyperparameters"].update(
        class_balance="none", num_boost_round=15, early_stopping_rounds=3, learning_rate=0.2,
        max_depth=3, subsample=1.0, colsample_bytree=1.0,
    )
    path = base / "candle.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    config = load_experiment_config(path)
    lock_path, lock = prepare_training_lock(config)
    outcome = ModelTrainingService(config).train(
        dataset_ids={s["symbol"]: s["dataset_id"] for s in lock["dataset_sources"]},
        expected_sha256={s["symbol"]: s["sha256"] for s in lock["dataset_sources"]},
    )
    return {
        "config": config, "outcome": outcome, "lock": lock, "models": models, "data": data,
        "config_path": path, "config_dir": base,
    }


def test_the_stored_model_records_candle_timeframe_horizon_and_feature_definitions(trained):
    manifest = trained["outcome"].stored.manifest
    config = trained["config"]
    assert manifest.timeframe == "1m"  # the stored source series
    assert manifest.feature_schema.feature_id == "intraday_candle_window"
    assert manifest.feature_schema.timeframe == "5m"
    assert manifest.feature_schema.names == config.selected_feature_names
    assert len(manifest.feature_schema.names) == 4 * 5 + 3 + 2 + 7 + 5 + 1 + 2 + 1
    label = manifest.label_schema
    assert label.horizon_bars == 4 and label.parameters["bar_minutes"] == "5"
    assert label.parameters["horizon_minutes"] == "20"
    assert manifest.algorithm == "xgboost" and manifest.class_labels == ("-1", "0", "1")
    assert manifest.holdout_evaluated is False
    assert [r.start.isoformat() for r in manifest.training_ranges] == ["2026-03-10T00:00:00+00:00"]


def test_class_prevalence_counts_and_build_counters_are_recorded(trained):
    lock_counts = trained["lock"]["counts"]
    evaluation = json.loads(trained["outcome"].stored.evaluation_path.read_text())
    counts = evaluation["dataset_counts"]
    assert counts == lock_counts  # the locked preparation is exactly what was trained on
    for partition, rows in (
        ("fit", counts["fit"]), ("early_stopping", counts["early_stopping"]),
        ("calibration", counts["calibration"]), ("evaluation", counts["evaluation"]),
    ):
        mix = counts["target_counts"][partition]
        assert sum(mix.values()) == rows > 0
    stats = counts["build_stats"]
    assert stats["aggregation"]["candles"] > 0 and stats["aggregation"]["minute_bars"] > 0
    labels = stats["labels"]
    assert labels["labels"] == labels["win"] + labels["loss"] + labels["timeout"] > 0
    assert labels["same_candle_both_barriers"] == (
        labels["resolved_by_one_minute_order"]
        + labels["ambiguous_after_one_minute_treated_as_loss"]
    )


def test_evaluation_reports_the_outcome_mix_by_favorable_probability_decile(trained):
    evaluation = json.loads(trained["outcome"].stored.evaluation_path.read_text())
    block = evaluation["favorable_deciles"]
    assert block["favorable_class"] == "1" and len(block["deciles"]) == 10
    assert sum(row["count"] for row in block["deciles"]) == evaluation["sample_count"]
    previous = -1.0
    for row in block["deciles"]:
        assert row["min_probability"] >= previous - 1e-12  # ascending by predicted P(favorable)
        previous = row["max_probability"]
        assert sum(row["observed_rates"].values()) == pytest.approx(1.0)
        assert 0.0 <= row["favorable_rate"] <= 1.0
    assert block["ordering"].startswith("decile 1 = lowest")
    assert "not trading results" in block["disclaimer"]
    # the standard evaluation outputs are all still present
    assert len(evaluation["confusion_matrix"]) == 3 and evaluation["calibration"]
    assert evaluation["class_calibration"] and evaluation["prediction_distribution"]
    assert evaluation["metrics"]["log_loss"] > 0


def test_model_report_includes_prevalence_and_decile_tables(trained, tmp_path):
    paths = write_model_report(
        trained["models"], "xgboost_buy_5m_tb100_050", "1.0.0", output_dir=tmp_path / "report"
    )
    text = paths["markdown"].read_text()
    assert "Class prevalence by partition" in text
    assert "Outcome mix by decile of predicted P(class 1)" in text
    assert "not trading results" in text.lower() or "not trading" in text.lower()
    assert (tmp_path / "report" / "favorable_deciles.csv").read_text().startswith("decile,count")


def test_training_and_streaming_feature_vectors_are_identical_and_drive_the_stored_model(trained):
    config = trained["config"]
    repository = FileModelRepository(trained["models"])
    manifest, predictor = load_model_predictor(repository, config.model_id, config.model_version)
    provider = ParquetDatasetProvider(trained["data"])
    window = config.ranges.evaluation
    selection = provider.select(
        ("AAA", "SPY"), window.start, window.end, feed="iex", timeframe="1m",
        adjustment="raw", universe_id="synthetic_v1",
    )
    minutes = tuple(provider.iter_bars(selection, "AAA"))
    spy = tuple(provider.iter_bars(selection, "SPY"))

    builder = AggregatedTrainingDatasetBuilder(
        CandleWindowFeaturePipeline(config.feature_config),
        AggregatedTripleBarrierLabelGenerator(
            AggregatedTripleBarrierConfig(
                Decimal(str(config.objective_parameters["positive_barrier"])),
                Decimal(str(config.objective_parameters["negative_barrier"])),
                int(config.objective_parameters["horizon_bars"]), 5,
            )
        ),
        config.candles,
    )
    trained_rows = list(builder.build_symbol(minutes, references={"SPY": spy}))
    assert trained_rows

    # the live/streaming path: minute -> candle (aggregator) -> features (pipeline.update)
    primary, reference = CandleAggregator(config.candles), CandleAggregator(config.candles)
    pipeline = CandleWindowFeaturePipeline(config.feature_config)
    spy_by_start = {bar.start: bar for bar in spy}
    streamed: dict = {}
    for bar in minutes:
        candles = primary.update(bar)
        spy_minute = spy_by_start.get(bar.start)
        spy_candles = reference.update(spy_minute) if spy_minute is not None else []
        for candle, ref_candle in zip(candles, spy_candles, strict=False):
            vector = pipeline.update(candle, {"SPY": ref_candle})
            if vector is not None:
                streamed[vector.bar_start] = vector
    matched = 0
    for row in trained_rows:
        vector = streamed.get(row.features.bar_start)
        if vector is not None:
            assert dict(vector.values) == dict(row.features.values)
            matched += 1
    assert matched > 0

    matrix = matrix_from_examples(
        trained_rows[:50], source_feature_names=manifest.feature_schema.names,
        selected_feature_names=manifest.selected_feature_names,
        task=config.task, class_labels=config.class_labels,
    )
    batch_probabilities = predictor.probability_matrix(matrix)
    for index, row in enumerate(trained_rows[:50]):
        prediction = predictor.predict(
            FeatureVector(
                row.features.symbol, row.features.bar_start, row.features.as_of,
                row.features.session_id, row.features.feed, row.features.schema_id,
                row.features.schema_version, row.features.values,
            )
        )
        got = np.array([prediction.probabilities[label] for label in config.class_labels])
        assert got == pytest.approx(batch_probabilities[index], abs=1e-6)
        assert sum(prediction.probabilities.values()) == pytest.approx(1.0)


def _service(trained, clock, directories=None):
    registry = ModelAdapterRegistry()
    registry.register(XGBoostAdapterFactory())
    config = trained["config"]
    reference = ModelReference(config.model_id, config.model_version, "xgboost", "buy")
    directories = [trained["config_dir"]] if directories is None else directories
    return reference, ModelInferenceService(
        FileModelRepository(trained["models"]), registry, [reference], clock=clock,
        candle_config_directories=directories,
    )


def test_serving_refuses_a_candle_model_whose_training_config_cannot_be_found(trained, tmp_path):
    with pytest.raises(ModelLoadError, match="cannot be reproduced") as caught:
        _service(trained, lambda: datetime.now(UTC), directories=[tmp_path])
    assert caught.value.code == "SCHEMA_MISMATCH"
    assert isinstance(caught.value, MikeTradeError)


def test_serving_refuses_a_config_that_does_not_reproduce_the_recorded_schema(trained, tmp_path):
    other = yaml.safe_load(trained["config_path"].read_text())
    other["features"]["lookback_bars"] = 5  # a different window than the one that was trained
    (tmp_path / "other.yaml").write_text(yaml.safe_dump(other))
    manifest = trained["outcome"].stored.manifest
    from mike_trade.modeling.candle_features import CandleDefinitionError, resolve_candle_definition

    with pytest.raises(CandleDefinitionError, match="no training configuration"):
        resolve_candle_definition(
            feature_schema=manifest.feature_schema,
            training_config_sha256=manifest.training_config_sha256,
            config_directories=[tmp_path],
        )
    # a config that does hash-match but cannot reproduce the recorded schema is rejected too
    wrong = replace(manifest.feature_schema, names=manifest.feature_schema.names[:-1])
    with pytest.raises(CandleDefinitionError, match="does not reproduce"):
        resolve_candle_definition(
            feature_schema=wrong,
            training_config_sha256=manifest.training_config_sha256,
            config_directories=[trained["config_dir"]],
        )


def test_served_candle_predictions_equal_the_training_features_and_stored_model(trained):
    """Minute-by-minute serving reproduces the training rows exactly, only at candle closes."""
    config = trained["config"]
    now = [datetime(2026, 3, 24, tzinfo=UTC)]
    reference, service = _service(trained, lambda: now[0])
    loaded = service.loaded(reference)
    assert loaded is not None and loaded.feature_window == 4 * 5
    assert loaded.candle.features.half_life_bars == 2.0  # recovered from the hashed training config

    provider = ParquetDatasetProvider(trained["data"])
    window = config.ranges.evaluation
    selection = provider.select(
        ("AAA", "SPY"), window.start, window.end, feed="iex", timeframe="1m",
        adjustment="raw", universe_id="synthetic_v1",
    )
    minutes = tuple(provider.iter_bars(selection, "AAA"))
    spy = tuple(provider.iter_bars(selection, "SPY"))
    builder = AggregatedTrainingDatasetBuilder(
        CandleWindowFeaturePipeline(config.feature_config),
        AggregatedTripleBarrierLabelGenerator(
            AggregatedTripleBarrierConfig(
                Decimal(str(config.objective_parameters["positive_barrier"])),
                Decimal(str(config.objective_parameters["negative_barrier"])),
                int(config.objective_parameters["horizon_bars"]), 5,
            )
        ),
        config.candles,
    )
    trained_rows = {
        row.features.as_of: row for row in builder.build_symbol(minutes, references={"SPY": spy})
    }
    assert trained_rows
    _manifest, predictor = load_model_predictor(
        FileModelRepository(trained["models"]), config.model_id, config.model_version
    )

    served = failures = compared = 0
    for index, bar in enumerate(minutes):
        now[0] = bar.end + timedelta(seconds=1)
        # only data that existed at bar.end is handed over; the future is not even present
        request = InferenceRequest(
            reference=reference, symbol="AAA", bars=minutes[max(0, index - 80) : index + 1],
            references={"SPY": tuple(b for b in spy if b.end <= bar.end)[-82:]},
        )
        result = service.predict(request)
        if result.prediction is None:
            failures += 1
            continue
        served += 1
        prediction = result.prediction
        assert (bar.end - bar.start) == timedelta(minutes=1)
        assert prediction.observation_end == bar.end
        assert prediction.observation_end - prediction.observation_start == timedelta(minutes=5)
        assert prediction.timeframe == "1m" and prediction.horizon_bars == 4
        assert prediction.feature_schema_id == "intraday_candle_window"
        row = trained_rows.get(bar.end)
        if row is not None:
            assert dict(prediction.feature_values) == dict(row.features.values)
            expected = predictor.predict(row.features)
            for label in config.class_labels:
                assert prediction.probabilities[label] == pytest.approx(
                    expected.probabilities[label], abs=1e-9
                )
            compared += 1
    assert served > 0 and compared > 0 and failures > served
    # the first prediction of a session needs a full lookback: four 5m candles = 20 minutes
    first = min(
        r.prediction.observation_end
        for b in minutes[:30]
        if (r := _predict_at(service, reference, minutes, spy, b, now)).prediction is not None
    )
    assert first == minutes[0].start + timedelta(minutes=20)


def _predict_at(service, reference, minutes, spy, bar, now):
    now[0] = bar.end + timedelta(seconds=1)
    index = minutes.index(bar)
    return service.predict(
        InferenceRequest(
            reference=reference, symbol="AAA", bars=minutes[max(0, index - 80) : index + 1],
            references={"SPY": tuple(b for b in spy if b.end <= bar.end)[-82:]},
        )
    )


def test_a_developing_candle_is_never_scored_and_later_data_cannot_change_a_prediction(trained):
    config = trained["config"]
    now = [datetime(2026, 3, 24, tzinfo=UTC)]
    reference, service = _service(trained, lambda: now[0])
    provider = ParquetDatasetProvider(trained["data"])
    window = config.ranges.evaluation
    selection = provider.select(
        ("AAA", "SPY"), window.start, window.end, feed="iex", timeframe="1m",
        adjustment="raw", universe_id="synthetic_v1",
    )
    minutes = tuple(provider.iter_bars(selection, "AAA"))
    spy = tuple(provider.iter_bars(selection, "SPY"))
    boundary = 29  # the 30th minute of the session closes a 5m candle with a full lookback
    assert minutes[boundary].end - minutes[0].start == timedelta(minutes=30)
    for offset in (-1, 1, 2, 3, 4):  # every minute that is not the end of a 5m candle
        bar = minutes[boundary + offset]
        result = _predict_at(service, reference, minutes, spy, bar, now)
        assert result.prediction is None
        assert result.failure.value == "NOT_CANDLE_BOUNDARY"
    base = _predict_at(service, reference, minutes, spy, minutes[boundary], now)
    assert base.prediction is not None
    # mutate every bar after the observation: the prediction must not change
    tampered = tuple(
        replace(b, close=b.close * 3, high=b.high * 3) if b.start > minutes[boundary].start else b
        for b in minutes
    )
    again = _predict_at(
        _service(trained, lambda: now[0])[1], reference, tampered, spy, tampered[boundary], now
    )
    assert again.prediction is not None
    assert dict(again.prediction.feature_values) == dict(base.prediction.feature_values)
    assert again.prediction.probabilities == base.prediction.probabilities


def test_one_minute_models_keep_their_original_one_minute_behavior(tmp_path):
    from model_fixtures import build_model

    built = build_model(tmp_path, algorithm="xgboost", role="buy", model_id="xgb_one_minute")
    registry = ModelAdapterRegistry()
    registry.register(XGBoostAdapterFactory())
    service = ModelInferenceService(
        FileModelRepository(tmp_path), registry, [built.reference],
        clock=lambda: datetime.now(UTC),
    )
    loaded = service.loaded(built.reference)
    assert loaded is not None and loaded.candle is None
    assert loaded.base_config.timeframe == "1m"
    assert loaded.feature_window == loaded.base_config.maximum_history


def test_stored_models_are_immutable(trained):
    config = trained["config"]
    repository = FileModelRepository(trained["models"])
    manifest, artifact = repository.get(config.model_id, config.model_version)
    with pytest.raises(Exception):  # noqa: B017 - a second write of id@version is refused
        repository.put(manifest, artifact, b"{}\n")
    assert hashlib.sha256(artifact).hexdigest() == manifest.artifact_sha256
    # re-reading still yields the original bytes
    assert repository.get(config.model_id, config.model_version)[1] == artifact


def test_lookback_and_horizon_are_expressed_in_candles_in_the_experiment_budget(trained):
    config = trained["config"]
    # 4 candles of history x 5 minutes and a 4-candle (20 minute) horizon: the first possible signal
    # of a session ends 20 minutes after the open and the last allowed one ends 20 minutes before
    # the data ends; nothing is labelled across the end of the day.
    builder_stats = trained["lock"]["counts"]["build_stats"]["labels"]
    assert builder_stats["skipped_incomplete_horizon"] > 0
    assert config.feature_config.maximum_history == 4
    assert timedelta(minutes=config.candles.minutes * 4) == timedelta(minutes=20)


def test_summary_reports_outcome_frequencies_partition_metrics_and_deciles(trained):
    from mike_trade.modeling.experiment_summary import summarize_experiment

    evaluation = json.loads(trained["outcome"].stored.evaluation_path.read_text())
    assert set(evaluation["partition_metrics"]) == {
        "fit", "early_stopping", "calibration", "evaluation"
    }
    text = summarize_experiment(trained["models"], "xgboost_buy_5m_tb100_050", "1.0.0")
    assert "Outcome frequency by partition" in text and "favorable (+ barrier first)" in text
    assert "adverse (- barrier first)" in text and "neither (timeout)" in text
    assert "ROC AUC" in text and "decile 1 = lowest" in text and "NOT trading results" in text


def _candle_backtest(trained, tmp_path, tape, *, trail="0.5", **overrides):
    """Run the real BacktestRunner (strategy, lifecycle, simulator, native trailing stop)."""
    import test_model_backtest as harness

    config = trained["config"]
    registry = ModelAdapterRegistry()
    registry.register(XGBoostAdapterFactory())
    reference = ModelReference(config.model_id, config.model_version, "xgboost", "buy")
    params = {
        "xgboost_probability_buy": {
            "model_id": config.model_id, "model_version": config.model_version,
            "decision_mode": "MODEL_DIRECTED", **overrides,
        }
    }

    def build(_model_root, _references, clock):
        return ModelInferenceService(
            FileModelRepository(trained["models"]), registry, [reference], clock=clock,
            candle_config_directories=[trained["config_dir"]],
        )

    original = harness.build_inference_service
    harness.build_inference_service = build
    try:
        return harness.run(
            tmp_path, trained["models"],
            {"AAA": harness.events("AAA", tape), "SPY": harness.spy_flat(len(tape))},
            ("xgboost_probability_buy",), params,
            config={"protective_stop": {"type": "native_trailing", "trail_percent": trail}},
        )
    finally:
        harness.build_inference_service = original


def _rally_then_reversal():
    quiet = [str(Decimal("100") + Decimal("0.01") * i) for i in range(30)]
    rally = [str(Decimal("100.3") + Decimal("0.2") * i) for i in range(1, 21)]  # about +4%
    return [*quiet, *rally, *["104.2", "103.6", "103.0", "102.4", "102.4", "102.4"]]


def test_the_real_engine_scores_a_candle_model_only_at_candle_closes(trained, tmp_path):
    result = _candle_backtest(trained, tmp_path, _rally_then_reversal())
    audits = [
        json.loads(r["model_audit"])
        for r in result.evaluations
        if r["strategy"] == "xgboost_probability_buy" and r.get("model_audit")
    ]
    scored = [a for a in audits if "prediction" in a]
    idle = [a for a in audits if "prediction" not in a]
    assert scored, "the model must have been consulted"
    session_open = datetime(2026, 3, 10, 13, 30, tzinfo=UTC)
    for audit in scored:
        end = datetime.fromisoformat(audit["bar_end"])
        assert (end - session_open) % timedelta(minutes=5) == timedelta(0)
        prediction = audit["prediction"]
        assert prediction["feature_schema"] == "intraday_candle_window@1.0.0"
        assert prediction["observation_end"] == audit["bar_end"]
        assert prediction["horizon_bars"] == 4
        assert audit["decision_mode"] == "MODEL_DIRECTED"
        favorable, adverse = audit["favorable_probability_bps"], audit["adverse_probability_bps"]
        assert (audit["decision"] == "SIGNAL") == (favorable > adverse)  # favorable vs adverse only
    # every other minute is explicitly "no candle has just closed": never a prediction, never a signal
    assert idle
    for audit in idle:
        end = datetime.fromisoformat(audit["bar_end"])
        assert audit["decision"] == "NO_SIGNAL"
        assert audit["reason"].endswith("NOT_CANDLE_BOUNDARY") or (
            (end - session_open) % timedelta(minutes=5) == timedelta(0)
        )


def test_candle_signals_enter_on_the_next_open_and_only_the_trailing_stop_exits_a_winner(
    trained, tmp_path
):
    # THRESHOLD with open limits only forces the tiny synthetic model to fire so the lifecycle can
    # be exercised; the production backtests use MODEL_DIRECTED.
    result = _candle_backtest(
        trained, tmp_path, _rally_then_reversal(), decision_mode="THRESHOLD",
        minimum_favorable_probability_bps=1, maximum_adverse_probability_bps=10000,
    )
    ordered = list(result.fills)
    buys = [f for f in ordered if f["side"] == "BUY"]
    sells = [f for f in ordered if f["side"] == "SELL"]
    # no pyramiding: every BUY is preceded by the complete SELL of the earlier position
    sides = [f["side"] for f in ordered]
    assert sides[0] == "BUY" and all(a != b for a, b in zip(sides, sides[1:], strict=False))
    assert all(f["quantity"] == buys[0]["quantity"] for f in ordered)
    (signal, *_later) = sorted(result.signals, key=lambda s_: s_["bar_time"])
    assert not any(f["purpose"] == "STRATEGY_EXIT" for f in sells), "no SELL strategy exists"
    entry, exit_ = buys[0], sells[0]
    assert exit_["purpose"] == "PROTECTIVE_STOP"
    peak = max(Decimal(t) for t in _rally_then_reversal())
    # the +1% training target is not a take-profit: the position rode well past +1% ...
    assert exit_["price"] > entry["price"] * Decimal("1.03")
    # ... and left through the 0.5% trailing stop measured from the high
    assert abs(exit_["price"] - peak * Decimal("0.995")) <= Decimal("0.35")
    assert exit_["time"] > entry["time"]
