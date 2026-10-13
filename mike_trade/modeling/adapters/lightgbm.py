"""Native LightGBM classification/regression adapter (no pickle)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

import lightgbm as lgb
import numpy as np

from mike_trade.modeling.adapters.common import (
    ClassBalance,
    ComputeDevice,
    FitResult,
    LightGBMTrainingConfig,
    normalize_probability_matrix,
    normalize_probability_row,
)
from mike_trade.modeling.artifacts import ModelArtifactManifest
from mike_trade.modeling.calibration import NO_CALIBRATION, ProbabilityCalibrator
from mike_trade.modeling.contracts import (
    FeatureSchema,
    FeatureVector,
    LearningTask,
    ModelPrediction,
    PredictionKind,
    TrainingExample,
    TrainingRequest,
)
from mike_trade.modeling.matrix import (
    FeatureSchemaMismatch,
    MatrixDataset,
    balanced_weights,
    matrix_from_examples,
)

ADAPTER_VERSION = "1.0.0"


def _parameters(config: LightGBMTrainingConfig, seed: int) -> dict[str, Any]:
    if config.task is LearningTask.REGRESSION:
        objective, metric = "regression", "rmse"
    elif config.task is LearningTask.BINARY_CLASSIFICATION:
        objective, metric = "binary", "binary_logloss"
    else:
        objective, metric = "multiclass", "multi_logloss"
    values: dict[str, Any] = {
        "objective": objective,
        "metric": metric,
        "seed": seed,
        "feature_fraction_seed": seed,
        "bagging_seed": seed,
        "data_random_seed": seed,
        "num_threads": config.threads,
        "device_type": "gpu" if config.device is ComputeDevice.GPU else "cpu",
        "verbosity": -1,
        "learning_rate": config.learning_rate,
        "num_leaves": config.num_leaves,
        "max_depth": config.max_depth,
        "min_data_in_leaf": config.min_data_in_leaf,
        "feature_fraction": config.feature_fraction,
        "bagging_fraction": config.bagging_fraction,
        "bagging_freq": config.bagging_freq,
        "lambda_l1": config.reg_alpha,
        "lambda_l2": config.reg_lambda,
    }
    if config.device is ComputeDevice.CPU:
        values.update({"deterministic": True, "force_col_wise": True})
    if config.task is LearningTask.MULTICLASS_CLASSIFICATION:
        values["num_class"] = len(config.class_labels)
    values.update(config.extra_parameters)
    return values


class LightGBMPredictor:
    def __init__(
        self,
        booster: lgb.Booster,
        *,
        model_id: str,
        model_version: str,
        feature_schema: FeatureSchema,
        selected_feature_names: tuple[str, ...],
        task: LearningTask,
        class_labels: tuple[str, ...],
        calibrator: ProbabilityCalibrator = NO_CALIBRATION,
        best_iteration: int | None = None,
    ) -> None:
        self.booster = booster
        self.model_id = model_id
        self.model_version = model_version
        self._feature_schema = feature_schema
        self.selected_feature_names = selected_feature_names
        self.task = task
        self.class_labels = class_labels
        self.calibrator = calibrator
        self.best_iteration = best_iteration

    @property
    def feature_schema(self) -> FeatureSchema:
        return self._feature_schema

    def _array(self, features: FeatureVector) -> np.ndarray[Any, Any]:
        if (
            features.schema_id != self.feature_schema.feature_id
            or features.schema_version != self.feature_schema.version
        ):
            raise FeatureSchemaMismatch(
                "prediction feature schema identity is incompatible with the trained model"
            )
        missing = [name for name in self.feature_schema.names if name not in features.values]
        if missing:
            raise FeatureSchemaMismatch(f"prediction is missing schema features: {missing}")
        extra = [name for name in features.values if name not in self.feature_schema.names]
        if extra:
            raise FeatureSchemaMismatch(f"prediction has unknown schema features: {extra}")
        if tuple(features.values) != self.feature_schema.names:
            raise FeatureSchemaMismatch(
                "prediction feature ordering is incompatible with the trained model"
            )
        return np.asarray(
            [[features.values[name] for name in self.selected_feature_names]], dtype=np.float32
        )

    def raw_matrix_predictions(self, dataset: MatrixDataset) -> np.ndarray[Any, Any]:
        if dataset.feature_names != self.selected_feature_names:
            raise FeatureSchemaMismatch("matrix feature order is incompatible with model")
        values = np.asarray(
            self.booster.predict(dataset.features, num_iteration=self.best_iteration)
        )
        if self.task is LearningTask.REGRESSION:
            return values.reshape(-1)
        if self.task is LearningTask.BINARY_CLASSIFICATION:
            values = values.reshape(-1)
            return np.column_stack((1.0 - values, values))
        return values.reshape((-1, len(self.class_labels)))

    def probability_matrix(self, dataset: MatrixDataset) -> np.ndarray[Any, Any]:
        if self.task is LearningTask.REGRESSION:
            raise ValueError("regression models do not produce probabilities")
        return normalize_probability_matrix(
            self.calibrator.transform(self.raw_matrix_predictions(dataset))
        )

    def with_calibrator(self, calibrator: ProbabilityCalibrator) -> LightGBMPredictor:
        return LightGBMPredictor(
            self.booster,
            model_id=self.model_id,
            model_version=self.model_version,
            feature_schema=self.feature_schema,
            selected_feature_names=self.selected_feature_names,
            task=self.task,
            class_labels=self.class_labels,
            calibrator=calibrator,
            best_iteration=self.best_iteration,
        )

    def predict(self, features: FeatureVector) -> ModelPrediction:
        # One row never benefits from an OpenMP pool; keep serving single-threaded.
        raw = np.asarray(
            self.booster.predict(
                self._array(features), num_iteration=self.best_iteration, num_threads=1
            )
        )
        if self.task is LearningTask.REGRESSION:
            return ModelPrediction(
                self.model_id,
                self.model_version,
                features.symbol,
                features.as_of,
                self.feature_schema.feature_id,
                self.feature_schema.version,
                PredictionKind.REGRESSION,
                value=float(raw.reshape(-1)[0]),
            )
        probabilities = (
            np.column_stack((1.0 - raw.reshape(-1), raw.reshape(-1)))
            if self.task is LearningTask.BINARY_CLASSIFICATION
            else raw.reshape((1, len(self.class_labels)))
        )
        probabilities = normalize_probability_row(self.calibrator.transform(probabilities)[0])
        index = int(np.argmax(probabilities))
        return ModelPrediction(
            self.model_id,
            self.model_version,
            features.symbol,
            features.as_of,
            self.feature_schema.feature_id,
            self.feature_schema.version,
            PredictionKind.CLASS,
            self.class_labels[index],
            dict(zip(self.class_labels, map(float, probabilities), strict=True)),
        )


class LightGBMTrainer:
    adapter_id = "lightgbm"
    version = ADAPTER_VERSION
    artifact_formats: tuple[str, ...] = ("lightgbm_text",)

    def __init__(self, config: LightGBMTrainingConfig) -> None:
        self.config = config

    def fit(
        self,
        request: TrainingRequest,
        train: MatrixDataset,
        validation: MatrixDataset | None = None,
        *,
        calibrator: ProbabilityCalibrator = NO_CALIBRATION,
    ) -> FitResult:
        if train.feature_names != self.config.selected_feature_names:
            raise FeatureSchemaMismatch("training matrix feature order differs from configuration")
        if validation is not None and validation.feature_names != train.feature_names:
            raise FeatureSchemaMismatch("validation feature order differs from training")
        params = _parameters(self.config, request.seed)
        weights = (
            balanced_weights(train.targets)
            if self.config.task is not LearningTask.REGRESSION
            and self.config.class_balance is ClassBalance.BALANCED
            else train.weights
        )
        training = lgb.Dataset(
            train.features,
            label=train.targets,
            weight=weights,
            feature_name=list(train.feature_names),
            free_raw_data=False,
        )
        valid_sets: list[lgb.Dataset] = []
        callbacks: list[Any] = []
        if validation is not None:
            valid_sets.append(
                lgb.Dataset(
                    validation.features,
                    label=validation.targets,
                    feature_name=list(validation.feature_names),
                    reference=training,
                    free_raw_data=False,
                )
            )
            callbacks.append(
                lgb.early_stopping(
                    self.config.early_stopping_rounds, first_metric_only=True, verbose=False
                )
            )
        booster = lgb.train(
            params,
            training,
            num_boost_round=self.config.num_boost_round,
            valid_sets=valid_sets or None,
            valid_names=["validation"] if valid_sets else None,
            callbacks=callbacks or None,
        )
        best_iteration = int(booster.best_iteration or self.config.num_boost_round)
        predictor = LightGBMPredictor(
            booster,
            model_id=request.model_id,
            model_version=request.model_version,
            feature_schema=request.feature_schema,
            selected_feature_names=self.config.selected_feature_names,
            task=self.config.task,
            class_labels=self.config.class_labels,
            calibrator=calibrator,
            best_iteration=best_iteration,
        )
        split = booster.feature_importance(importance_type="split", iteration=best_iteration)
        gain = booster.feature_importance(importance_type="gain", iteration=best_iteration)
        importance = {
            f"{name}.gain": repr(float(value))
            for name, value in zip(train.feature_names, gain, strict=True)
        }
        importance.update(
            {
                f"{name}.split": repr(float(value))
                for name, value in zip(train.feature_names, split, strict=True)
            }
        )
        recorded_params = {
            **params,
            "num_boost_round": self.config.num_boost_round,
            "early_stopping_rounds": self.config.early_stopping_rounds,
            "class_balance": self.config.class_balance.value,
        }
        string_params = {key: str(value) for key, value in sorted(recorded_params.items())}
        return FitResult(
            booster.model_to_string(num_iteration=best_iteration).encode("utf-8"),
            "lightgbm_text",
            predictor,
            string_params,
            importance,
            best_iteration,
            calibrator,
            lgb.__version__,
        )

    def train(self, request: TrainingRequest, examples: Iterable[TrainingExample]) -> bytes:
        matrix = matrix_from_examples(
            examples,
            source_feature_names=request.feature_schema.names,
            selected_feature_names=self.config.selected_feature_names,
            task=self.config.task,
            class_labels=self.config.class_labels,
        )
        return self.fit(request, matrix).artifact

    def with_parameters(self, values: Mapping[str, object]) -> LightGBMTrainer:
        allowed = {
            "num_boost_round",
            "early_stopping_rounds",
            "num_leaves",
            "max_depth",
            "learning_rate",
            "min_data_in_leaf",
            "feature_fraction",
            "bagging_fraction",
            "bagging_freq",
            "reg_alpha",
            "reg_lambda",
            "class_balance",
            "extra_parameters",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unknown LightGBM configuration keys: {sorted(unknown)}")
        replacements = dict(values)
        if "class_balance" in replacements:
            replacements["class_balance"] = ClassBalance(str(replacements["class_balance"]))
        return LightGBMTrainer(replace(self.config, **replacements))  # type: ignore[arg-type]


class LightGBMAdapterFactory:
    adapter_id = "lightgbm"
    version = ADAPTER_VERSION
    artifact_formats: tuple[str, ...] = ("lightgbm_text",)

    def __init__(self, config: LightGBMTrainingConfig | None = None) -> None:
        self.config = config

    def trainer(self) -> LightGBMTrainer:
        if self.config is None:
            raise ValueError("LightGBM factory requires training configuration")
        return LightGBMTrainer(self.config)

    def predictor(
        self, artifact: bytes, manifest: ModelArtifactManifest | None = None
    ) -> LightGBMPredictor:
        if manifest is None:
            raise ValueError("loading a predictor requires its pinned manifest")
        if manifest.adapter_id != self.adapter_id or manifest.artifact_format != "lightgbm_text":
            raise ValueError("manifest is not a LightGBM native artifact")
        booster = lgb.Booster(model_str=artifact.decode("utf-8"))
        if tuple(booster.feature_name()) != manifest.selected_feature_names:
            raise FeatureSchemaMismatch(
                "native LightGBM feature ordering differs from the pinned manifest"
            )
        return LightGBMPredictor(
            booster,
            model_id=manifest.model_id,
            model_version=manifest.model_version,
            feature_schema=manifest.feature_schema,
            selected_feature_names=manifest.selected_feature_names,
            task=manifest.label_schema.task,
            class_labels=manifest.class_labels,
            calibrator=ProbabilityCalibrator.from_mapping(
                {key: str(value) for key, value in manifest.calibration.items()}
            ),
            best_iteration=int(str(manifest.hyperparameters.get("best_iteration", "0"))),
        )
