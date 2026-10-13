from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from textwrap import dedent

import numpy as np
import pytest

from mike_trade.market_data.alpaca_history import Adjustment, HistoricalBar
from mike_trade.modeling.artifacts import FileModelRepository
from mike_trade.modeling.calibration import fit_probability_calibrator
from mike_trade.modeling.config import ExperimentRanges, load_experiment_config
from mike_trade.modeling.contracts import (
    FeatureVector,
    LearningTask,
    ModelBar,
    SupervisedLabel,
    TrainingExample,
)
from mike_trade.modeling.features import IntradayFeaturePipeline
from mike_trade.modeling.matrix import MatrixDataset
from mike_trade.modeling.metrics import evaluate_classification
from mike_trade.modeling.objectives import (
    ScheduledEntryPolicy,
    SellContinuationConfig,
    SellPositionDatasetBuilder,
)
from mike_trade.modeling.splits import FINAL_HOLDOUT, TimeRange
from mike_trade.modeling.training import (
    ModelTrainingService,
    _in_range,
    evaluate_saved_model,
    load_model_predictor,
)
from mike_trade.research.datasets import HistoricalDatasetRepository

START = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def _bars(*, adverse: bool) -> tuple[ModelBar, ...]:
    result = []
    for minute in range(70):
        price = D("100") + D(minute) / D("20")
        if adverse and minute == 25:
            price = D("98")
        moment = START + minute * MINUTE
        result.append(
            ModelBar(
                "AAA",
                moment,
                moment + MINUTE,
                price,
                price + D("0.2"),
                price - D("0.2"),
                price,
                D("1000"),
                "iex",
                "1m",
                "raw",
                "XNYS-2026-03-02",
                price,
            )
        )
    return tuple(result)


def test_sell_positions_are_scheduled_without_hindsight_selection() -> None:
    policy = ScheduledEntryPolicy(
        warmup_bars=21,
        entry_interval_bars=30,
        maximum_holding_bars=12,
        cooldown_bars=2,
    )
    builder = SellPositionDatasetBuilder(
        IntradayFeaturePipeline(),
        policy,
        SellContinuationConfig(horizon_bars=3, risk_barrier=D("0.01")),
    )
    adverse = tuple(builder.build_symbol(_bars(adverse=True)))
    favorable = tuple(builder.build_symbol(_bars(adverse=False)))
    assert adverse and len(adverse) == len(favorable)
    assert [item.features.bar_start for item in adverse] == [
        item.features.bar_start for item in favorable
    ]
    assert any(item.label.value == -1 for item in adverse)
    first = adverse[0]
    assert first.label.entry_time == first.features.as_of
    assert first.metadata["position_entry_time"] == (START + 21 * MINUTE).isoformat()
    assert D(first.metadata["position_entry_price"]) > 0
    assert first.metadata["entry_policy_id"] == policy.policy_id
    assert all(item.label.outcome_time < START + timedelta(days=1) for item in adverse)


def test_partition_membership_excludes_targets_on_the_next_boundary() -> None:
    boundary = START + timedelta(hours=1)
    feature = FeatureVector(
        "AAA",
        START,
        START + MINUTE,
        "XNYS-2026-03-02",
        "iex",
        "synthetic",
        "1.0.0",
        {"value": 1.0},
    )
    label = SupervisedLabel(
        "AAA",
        START,
        START + MINUTE,
        D("100"),
        boundary,
        D("101"),
        1,
        D("0.01"),
        "synthetic_label",
        "1.0.0",
        "WIN",
    )
    assert not _in_range(TrainingExample(feature, label), TimeRange(START, boundary))


def test_example_configs_are_versioned_and_holdout_is_rejected() -> None:
    paths = sorted(Path("config/modeling").glob("*.yaml"))
    configs = [load_experiment_config(path) for path in paths]
    assert len(configs) == 4
    assert {(item.algorithm.value, item.role.value) for item in configs} == {
        ("xgboost", "buy"),
        ("xgboost", "sell"),
        ("lightgbm", "buy"),
        ("lightgbm", "sell"),
    }
    assert all(item.feed == "iex" and len(item.resolved_symbols()) == 100 for item in configs)
    with pytest.raises(ValueError, match="final holdout"):
        ExperimentRanges(
            TimeRange(datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC)),
            TimeRange(datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 10, tzinfo=UTC)),
            FINAL_HOLDOUT,
        )


def test_multiclass_metrics_include_groups_calibration_and_frequency_baseline() -> None:
    features = np.asarray([[0.0], [1.0], [2.0], [3.0]], dtype=np.float32)
    targets = np.asarray([0, 1, 2, 2], dtype=np.float64)
    dataset = MatrixDataset(
        features,
        targets,
        ("value",),
        ("AAA", "AAA", "BBB", "BBB"),
        tuple(START + index * MINUTE for index in range(4)),
    )
    probabilities = np.asarray(
        [
            [0.8, 0.1, 0.1],
            [0.1, 0.7, 0.2],
            [0.1, 0.2, 0.7],
            [0.2, 0.2, 0.6],
        ]
    )
    evaluation = evaluate_classification(
        dataset,
        probabilities,
        ("LOSS", "TIMEOUT", "WIN"),
        training_targets=np.asarray([0, 0, 1, 2, 2, 2]),
    )
    assert evaluation.task is LearningTask.MULTICLASS_CLASSIFICATION
    assert len(evaluation.confusion_matrix) == 3
    assert "roc_auc" not in evaluation.metrics
    assert set(evaluation.per_symbol) == {"AAA", "BBB"}
    assert evaluation.calibration
    assert evaluation.baseline["kind"] == "training_class_frequency"

    calibrator = fit_probability_calibrator(probabilities, targets)
    calibrated = calibrator.transform(probabilities)
    assert calibrator.method == "temperature"
    np.testing.assert_allclose(calibrated.sum(axis=1), 1.0, atol=1e-12)


@pytest.mark.parametrize("algorithm", ["xgboost", "lightgbm"])
def test_shared_service_trains_native_regression_from_parquet(
    algorithm: str, tmp_path: Path
) -> None:
    data_root = tmp_path / "data"
    model_root = tmp_path / "models"
    repository = HistoricalDatasetRepository(data_root)
    bars: list[HistoricalBar] = []
    session_starts = (
        datetime(2026, 1, 5, 14, 30, tzinfo=UTC),
        datetime(2026, 1, 6, 14, 30, tzinfo=UTC),
        datetime(2026, 1, 7, 14, 30, tzinfo=UTC),
    )
    for day, session_start in enumerate(session_starts):
        for minute in range(80):
            wave = D((minute % 10) - 5) / D("20")
            price = D("100") + D(day) / D("5") + wave
            close = price + (D("0.03") if minute % 2 else D("-0.02"))
            bars.append(
                HistoricalBar(
                    "AAA",
                    session_start + minute * MINUTE,
                    price,
                    max(price, close) + D("0.1"),
                    min(price, close) - D("0.1"),
                    close,
                    1000 + minute,
                    close,
                    10,
                    "iex",
                    Adjustment.RAW,
                )
            )
    repository.persist(
        "AAA",
        bars,
        feed="iex",
        coverage_start=session_starts[0],
        coverage_end=session_starts[-1] + timedelta(minutes=90),
    )
    algorithm_parameters = (
        "max_depth: 3\n"
        "              subsample: 1.0\n"
        "              colsample_bytree: 1.0"
        if algorithm == "xgboost"
        else "num_leaves: 15\n"
        "              min_data_in_leaf: 3\n"
        "              feature_fraction: 1.0\n"
        "              bagging_fraction: 1.0"
    )
    config_path = tmp_path / f"{algorithm}.yaml"
    config_path.write_text(
        dedent(
            f"""
            schema_version: 1
            experiment_id: {algorithm}_synthetic_regression
            algorithm: {algorithm}
            role: buy
            model_id: {algorithm}_synthetic_regression
            model_version: 1.0.0
            dataset_root: {data_root}
            model_root: {model_root}
            universe_id: synthetic_v1
            symbols: [AAA]
            feed: iex
            timeframe: 1m
            adjustment: raw
            seed: 17
            ranges:
              fit_start: 2026-01-05T14:30:00Z
              fit_end: 2026-01-05T16:00:00Z
              early_stopping_start: 2026-01-06T14:30:00Z
              early_stopping_end: 2026-01-06T16:00:00Z
              evaluation_start: 2026-01-07T14:30:00Z
              evaluation_end: 2026-01-07T16:00:00Z
              embargo_minutes: 0
            features:
              feature_id: intraday_core
              version: 1.0.0
              include_vwap: true
            objective:
              type: forward_return
              task: REGRESSION
              horizon_bars: 2
              entry_bps: "1"
              exit_bps: "1"
            resources:
              device: cpu
              threads: 1
            hyperparameters:
              class_balance: none
              num_boost_round: 12
              early_stopping_rounds: 3
              learning_rate: 0.1
              {algorithm_parameters}
            search:
              max_trials: 1
            probability_calibration: false
            permutation_importance: false
            batch_size: 17
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_experiment_config(config_path)
    outcome = ModelTrainingService(config).train()
    assert outcome.stored.artifact_path.exists()
    assert outcome.stored.manifest.algorithm == algorithm
    assert outcome.stored.manifest.feed == "iex"
    assert outcome.stored.manifest.label_schema.task is LearningTask.REGRESSION
    assert outcome.stored.manifest.hyperparameters["num_boost_round"] == "12"
    assert outcome.evaluation.sample_count > 0
    assert "rmse" in outcome.evaluation.metrics
    loaded_manifest, predictor = load_model_predictor(
        FileModelRepository(model_root), config.model_id, config.model_version
    )
    assert loaded_manifest.artifact_sha256 == outcome.stored.manifest.artifact_sha256
    assert predictor.selected_feature_names == config.selected_feature_names
    repeated_evaluation = evaluate_saved_model(config)
    assert repeated_evaluation.metrics == outcome.evaluation.metrics
