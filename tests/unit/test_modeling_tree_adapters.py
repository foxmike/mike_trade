from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from mike_trade.modeling.adapters.common import (
    LightGBMTrainingConfig,
    XGBoostTrainingConfig,
)
from mike_trade.modeling.adapters.lightgbm import (
    LightGBMAdapterFactory,
    LightGBMTrainer,
)
from mike_trade.modeling.adapters.xgboost import XGBoostAdapterFactory, XGBoostTrainer
from mike_trade.modeling.artifacts import (
    FileModelRepository,
    ModelArtifactManifest,
    schema_fingerprint,
)
from mike_trade.modeling.contracts import (
    DatasetSource,
    FeatureSchema,
    FeatureVector,
    LabelSchema,
    LearningTask,
    TrainingRequest,
)
from mike_trade.modeling.matrix import FeatureSchemaMismatch, MatrixDataset
from mike_trade.modeling.splits import TimeRange

START = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
FEATURES = FeatureSchema("synthetic_tree", "1.0.0", ("trend", "volume_signal"))


def _matrix(*, task: LearningTask, rows: int = 72) -> MatrixDataset:
    index = np.arange(rows, dtype=np.float32)
    features = np.column_stack((np.sin(index / 5), np.cos(index / 7))).astype(np.float32)
    if task is LearningTask.REGRESSION:
        targets = (features[:, 0] * 0.7 - features[:, 1] * 0.2).astype(np.float64)
    elif task is LearningTask.BINARY_CLASSIFICATION:
        targets = (features[:, 0] + features[:, 1] * 0.5 > 0).astype(np.float64)
    else:
        score = features[:, 0] + features[:, 1] * 0.5
        targets = np.where(score < -0.25, 0, np.where(score > 0.55, 2, 1)).astype(
            np.float64
        )
    return MatrixDataset(features, targets, FEATURES.names)


def _request(task: LearningTask) -> TrainingRequest:
    labels = LabelSchema("synthetic_target", "1.0.0", task, 5)
    return TrainingRequest("synthetic_model", "1.0.0", 90210, FEATURES, labels, {})


def _trainer(
    algorithm: str, task: LearningTask
) -> XGBoostTrainer | LightGBMTrainer:
    if task is LearningTask.REGRESSION:
        labels: tuple[str, ...] = ()
    elif task is LearningTask.BINARY_CLASSIFICATION:
        labels = ("0", "1")
    else:
        labels = ("LOSS", "TIMEOUT", "WIN")
    if algorithm == "xgboost":
        return XGBoostTrainer(
            XGBoostTrainingConfig(
                task,
                FEATURES.names,
                labels,
                threads=1,
                num_boost_round=24,
                early_stopping_rounds=5,
                max_depth=3,
                subsample=1.0,
                colsample_bytree=1.0,
            )
        )
    return LightGBMTrainer(
        LightGBMTrainingConfig(
            task,
            FEATURES.names,
            labels,
            threads=1,
            num_boost_round=24,
            early_stopping_rounds=5,
            min_data_in_leaf=3,
            feature_fraction=1.0,
            bagging_fraction=1.0,
        )
    )


def _manifest(result: Any, task: LearningTask, evaluation: bytes) -> ModelArtifactManifest:
    request = _request(task)
    algorithm = "xgboost" if result.artifact_format == "xgboost_ubj" else "lightgbm"
    source = DatasetSource(
        "synthetic-dataset",
        "d" * 64,
        "AAA",
        "iex",
        "1m",
        "raw",
        datetime(2025, 10, 9, tzinfo=UTC),
        datetime(2026, 9, 21, tzinfo=UTC),
    )
    labels = () if task is LearningTask.REGRESSION else ("LOSS", "TIMEOUT", "WIN")
    return ModelArtifactManifest(
        2,
        request.model_id,
        request.model_version,
        datetime(2026, 8, 31, tzinfo=UTC),
        algorithm,
        "1.0.0",
        result.artifact_format,
        hashlib.sha256(result.artifact).hexdigest(),
        len(result.artifact),
        FEATURES,
        schema_fingerprint(FEATURES),
        request.label_schema,
        schema_fingerprint(request.label_schema),
        (source,),
        "iex",
        "1m",
        "raw",
        "synthetic_universe",
        (
            TimeRange(
                datetime(2025, 10, 9, tzinfo=UTC),
                datetime(2026, 6, 1, tzinfo=UTC),
            ),
        ),
        (
            TimeRange(
                datetime(2026, 6, 2, tzinfo=UTC),
                datetime(2026, 8, 31, tzinfo=UTC),
            ),
        ),
        "chronological",
        {"entry_bps": "1", "exit_bps": "1"},
        request.seed,
        {"library": result.library_version},
        "c" * 64,
        {"test_metric": "0.5"},
        False,
        algorithm,
        "buy_synthetic_target",
        FEATURES.names,
        labels,
        {**result.hyperparameters, "best_iteration": result.best_iteration},
        result.calibrator.as_mapping(),
        {},
        result.feature_importance,
        hashlib.sha256(evaluation).hexdigest(),
        len(evaluation),
    )


@pytest.mark.parametrize("algorithm", ["xgboost", "lightgbm"])
def test_classification_native_round_trip_is_reproducible(
    algorithm: str, tmp_path: Path
) -> None:
    task = LearningTask.MULTICLASS_CLASSIFICATION
    full = _matrix(task=task)
    train = MatrixDataset(full.features[:54], full.targets[:54], full.feature_names)
    validation = MatrixDataset(full.features[54:], full.targets[54:], full.feature_names)
    trainer = _trainer(algorithm, task)
    first = trainer.fit(_request(task), train, validation)
    second = trainer.fit(_request(task), train, validation)
    assert first.feature_importance
    assert first.hyperparameters["num_boost_round"] == "24"
    assert first.hyperparameters["class_balance"] == "balanced"
    first_probabilities = first.predictor.probability_matrix(validation)
    second_probabilities = second.predictor.probability_matrix(validation)
    np.testing.assert_allclose(first_probabilities, second_probabilities, rtol=0, atol=1e-8)
    np.testing.assert_allclose(first_probabilities.sum(axis=1), 1.0, atol=1e-7)
    assert np.all((first_probabilities >= 0) & (first_probabilities <= 1))

    evaluation = b'{"metrics":{"log_loss":0.5}}\n'
    manifest = _manifest(first, task, evaluation)
    repository = FileModelRepository(tmp_path)
    stored = repository.put(manifest, first.artifact, evaluation)
    loaded_manifest, artifact = repository.get("synthetic_model", "1.0.0")
    factory = (
        XGBoostAdapterFactory() if algorithm == "xgboost" else LightGBMAdapterFactory()
    )
    loaded = factory.predictor(artifact, loaded_manifest)
    np.testing.assert_allclose(
        first_probabilities,
        loaded.probability_matrix(validation),
        rtol=0,
        atol=1e-8,
    )
    assert stored.evaluation_path is not None and stored.evaluation_path.exists()
    assert repository.get_evaluation("synthetic_model", "1.0.0") == evaluation

    incompatible = replace(loaded_manifest, selected_feature_names=("trend",))
    with pytest.raises(FeatureSchemaMismatch, match="native .* feature ordering"):
        factory.predictor(artifact, incompatible)

    stored.evaluation_path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="evaluation size mismatch|evaluation checksum mismatch"):
        repository.get_evaluation("synthetic_model", "1.0.0")
    stored.evaluation_path.write_bytes(evaluation)

    stored.artifact_path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="size mismatch|checksum mismatch"):
        repository.get("synthetic_model", "1.0.0")


@pytest.mark.parametrize("algorithm", ["xgboost", "lightgbm"])
def test_regression_outputs_are_finite(algorithm: str) -> None:
    task = LearningTask.REGRESSION
    full = _matrix(task=task)
    train = MatrixDataset(full.features[:54], full.targets[:54], full.feature_names)
    validation = MatrixDataset(full.features[54:], full.targets[54:], full.feature_names)
    result = _trainer(algorithm, task).fit(_request(task), train, validation)
    prediction = result.predictor.raw_matrix_predictions(validation)
    assert prediction.shape == (validation.row_count,)
    assert np.isfinite(prediction).all()


@pytest.mark.parametrize("algorithm", ["xgboost", "lightgbm"])
def test_binary_classification_returns_two_valid_probabilities(algorithm: str) -> None:
    task = LearningTask.BINARY_CLASSIFICATION
    full = _matrix(task=task)
    train = MatrixDataset(full.features[:54], full.targets[:54], full.feature_names)
    validation = MatrixDataset(full.features[54:], full.targets[54:], full.feature_names)
    result = _trainer(algorithm, task).fit(_request(task), train, validation)
    probabilities = result.predictor.probability_matrix(validation)
    assert probabilities.shape == (validation.row_count, 2)
    np.testing.assert_allclose(probabilities.sum(axis=1), 1.0, atol=1e-7)


@pytest.mark.parametrize("algorithm", ["xgboost", "lightgbm"])
def test_prediction_rejects_missing_and_reordered_features(algorithm: str) -> None:
    task = LearningTask.REGRESSION
    matrix = _matrix(task=task)
    result = _trainer(algorithm, task).fit(_request(task), matrix)
    at = START + timedelta(minutes=2)
    missing = FeatureVector(
        "AAA",
        at - timedelta(minutes=1),
        at,
        "XNYS-2026-01-05",
        "iex",
        FEATURES.feature_id,
        FEATURES.version,
        {"trend": 0.1},
    )
    with pytest.raises(FeatureSchemaMismatch, match="missing"):
        result.predictor.predict(missing)
    reordered = FeatureVector(
        "AAA",
        at - timedelta(minutes=1),
        at,
        "XNYS-2026-01-05",
        "iex",
        FEATURES.feature_id,
        FEATURES.version,
        {"volume_signal": 0.2, "trend": 0.1},
    )
    with pytest.raises(FeatureSchemaMismatch, match="ordering"):
        result.predictor.predict(reordered)
