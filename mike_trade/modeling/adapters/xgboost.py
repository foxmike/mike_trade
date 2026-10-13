"""Native XGBoost classification/regression adapter (no pickle)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

import numpy as np
import xgboost as xgb

from mike_trade.modeling.adapters.common import (
    ClassBalance,
    ComputeDevice,
    FitResult,
    XGBoostTrainingConfig,
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


def _parameters(config: XGBoostTrainingConfig, seed: int) -> dict[str, Any]:
    if config.task is LearningTask.REGRESSION:
        objective, metric = "reg:squarederror", "rmse"
    elif config.task is LearningTask.BINARY_CLASSIFICATION:
        objective, metric = "binary:logistic", "logloss"
    else:
        objective, metric = "multi:softprob", "mlogloss"
    values: dict[str, Any] = {
        "objective": objective,
        "eval_metric": metric,
        "seed": seed,
        "nthread": config.threads,
        "device": "cuda" if config.device is ComputeDevice.GPU else "cpu",
        "tree_method": "hist",
        "max_depth": config.max_depth,
        "eta": config.learning_rate,
        "min_child_weight": config.min_child_weight,
        "subsample": config.subsample,
        "colsample_bytree": config.colsample_bytree,
        "alpha": config.reg_alpha,
        "lambda": config.reg_lambda,
    }
    if config.task is LearningTask.MULTICLASS_CLASSIFICATION:
        values["num_class"] = len(config.class_labels)
    values.update(config.extra_parameters)
    return values


def _string_parameters(values: Mapping[str, object]) -> dict[str, str]:
    return {key: str(value) for key, value in sorted(values.items())}


def _importance_value(value: float | list[float]) -> float:
    return float(sum(value) if isinstance(value, list) else value)


class XGBoostPredictor:
    def __init__(
        self,
        booster: xgb.Booster,
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

    def _matrix(self, features: FeatureVector) -> xgb.DMatrix:
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
        values = np.asarray(
            [[features.values[name] for name in self.selected_feature_names]], dtype=np.float32
        )
        return xgb.DMatrix(values, feature_names=list(self.selected_feature_names))

    def raw_matrix_predictions(self, dataset: MatrixDataset) -> np.ndarray[Any, Any]:
        if dataset.feature_names != self.selected_feature_names:
            raise FeatureSchemaMismatch("matrix feature order is incompatible with model")
        matrix = xgb.DMatrix(dataset.features, feature_names=list(dataset.feature_names))
        iteration_range = (
            (0, self.best_iteration + 1) if self.best_iteration is not None else (0, 0)
        )
        values = np.asarray(self.booster.predict(matrix, iteration_range=iteration_range))
        if self.task is LearningTask.REGRESSION:
            return values.reshape(-1)
        if self.task is LearningTask.BINARY_CLASSIFICATION:
            values = values.reshape(-1)
            return np.column_stack((1.0 - values, values))
        return values.reshape((-1, len(self.class_labels)))

    def probability_matrix(self, dataset: MatrixDataset) -> np.ndarray[Any, Any]:
        if self.task is LearningTask.REGRESSION:
            raise ValueError("regression models do not produce probabilities")
        return self.calibrator.transform(self.raw_matrix_predictions(dataset))

    def with_calibrator(self, calibrator: ProbabilityCalibrator) -> XGBoostPredictor:
        return XGBoostPredictor(
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
        matrix = self._matrix(features)
        iteration_range = (
            (0, self.best_iteration + 1) if self.best_iteration is not None else (0, 0)
        )
        raw = np.asarray(self.booster.predict(matrix, iteration_range=iteration_range))
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
        probabilities = self.calibrator.transform(probabilities)[0]
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


class XGBoostTrainer:
    adapter_id = "xgboost"
    version = ADAPTER_VERSION
    artifact_formats: tuple[str, ...] = ("xgboost_ubj",)

    def __init__(self, config: XGBoostTrainingConfig) -> None:
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
        parameters = _parameters(self.config, request.seed)
        weights = (
            balanced_weights(train.targets)
            if self.config.task is not LearningTask.REGRESSION
            and self.config.class_balance is ClassBalance.BALANCED
            else train.weights
        )
        dtrain = xgb.DMatrix(
            train.features,
            label=train.targets,
            weight=weights,
            feature_names=list(train.feature_names),
        )
        evals: list[tuple[xgb.DMatrix, str]] = []
        if validation is not None:
            evals.append(
                (
                    xgb.DMatrix(
                        validation.features,
                        label=validation.targets,
                        feature_names=list(validation.feature_names),
                    ),
                    "validation",
                )
            )
        booster = xgb.train(
            parameters,
            dtrain,
            num_boost_round=self.config.num_boost_round,
            evals=evals,
            early_stopping_rounds=(self.config.early_stopping_rounds if evals else None),
            verbose_eval=False,
        )
        best_iteration = int(getattr(booster, "best_iteration", self.config.num_boost_round - 1))
        predictor = XGBoostPredictor(
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
        gain = booster.get_score(importance_type="gain")
        weight = booster.get_score(importance_type="weight")
        importance = {
            f"{name}.gain": repr(_importance_value(gain.get(name, 0.0)))
            for name in train.feature_names
        }
        importance.update(
            {
                f"{name}.weight": repr(_importance_value(weight.get(name, 0.0)))
                for name in train.feature_names
            }
        )
        recorded_parameters = {
            **parameters,
            "num_boost_round": self.config.num_boost_round,
            "early_stopping_rounds": self.config.early_stopping_rounds,
            "class_balance": self.config.class_balance.value,
        }
        return FitResult(
            bytes(booster.save_raw(raw_format="ubj")),
            "xgboost_ubj",
            predictor,
            _string_parameters(recorded_parameters),
            importance,
            best_iteration,
            calibrator,
            xgb.__version__,
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

    def with_parameters(self, values: Mapping[str, object]) -> XGBoostTrainer:
        allowed = {
            "num_boost_round",
            "early_stopping_rounds",
            "max_depth",
            "learning_rate",
            "min_child_weight",
            "subsample",
            "colsample_bytree",
            "reg_alpha",
            "reg_lambda",
            "class_balance",
            "extra_parameters",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unknown XGBoost configuration keys: {sorted(unknown)}")
        replacements = dict(values)
        if "class_balance" in replacements:
            replacements["class_balance"] = ClassBalance(str(replacements["class_balance"]))
        return XGBoostTrainer(replace(self.config, **replacements))  # type: ignore[arg-type]


class XGBoostAdapterFactory:
    adapter_id = "xgboost"
    version = ADAPTER_VERSION
    artifact_formats: tuple[str, ...] = ("xgboost_ubj",)

    def __init__(self, config: XGBoostTrainingConfig | None = None) -> None:
        self.config = config

    def trainer(self) -> XGBoostTrainer:
        if self.config is None:
            raise ValueError("XGBoost factory requires training configuration")
        return XGBoostTrainer(self.config)

    def predictor(
        self, artifact: bytes, manifest: ModelArtifactManifest | None = None
    ) -> XGBoostPredictor:
        if manifest is None:
            raise ValueError("loading a predictor requires its pinned manifest")
        if manifest.adapter_id != self.adapter_id or manifest.artifact_format != "xgboost_ubj":
            raise ValueError("manifest is not an XGBoost native artifact")
        booster = xgb.Booster()
        booster.load_model(bytearray(artifact))
        if tuple(booster.feature_names or ()) != manifest.selected_feature_names:
            raise FeatureSchemaMismatch(
                "native XGBoost feature ordering differs from the pinned manifest"
            )
        metadata = manifest.calibration
        return XGBoostPredictor(
            booster,
            model_id=manifest.model_id,
            model_version=manifest.model_version,
            feature_schema=manifest.feature_schema,
            selected_feature_names=manifest.selected_feature_names,
            task=manifest.label_schema.task,
            class_labels=manifest.class_labels,
            calibrator=ProbabilityCalibrator.from_mapping(
                {key: str(value) for key, value in metadata.items()}
            ),
            best_iteration=int(str(manifest.hyperparameters.get("best_iteration", "0"))),
        )
