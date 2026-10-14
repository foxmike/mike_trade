"""End-to-end proof of the candle-window training path on a tiny synthetic dataset.

A real XGBoost model is trained in a temporary directory (nothing under ``research/`` is read or
written), then reloaded; training-time and streaming feature vectors are compared.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
from mike_trade.modeling.inference import ModelInferenceService, ModelLoadError
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
    return {"config": config, "outcome": outcome, "lock": lock, "models": models, "data": data}


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


def test_the_serving_stack_refuses_aggregated_models_with_a_clear_message(trained):
    config = trained["config"]
    registry = ModelAdapterRegistry()
    registry.register(XGBoostAdapterFactory())
    reference = ModelReference(config.model_id, config.model_version, "xgboost", "buy")
    with pytest.raises(ModelLoadError, match="5m candles") as caught:
        ModelInferenceService(
            FileModelRepository(trained["models"]), registry, [reference],
            clock=lambda: datetime.now(UTC),
        )
    assert caught.value.code == "PROVENANCE_UNSUPPORTED"
    assert isinstance(caught.value, MikeTradeError)


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
