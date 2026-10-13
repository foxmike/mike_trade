"""Shared bounded training workflow used identically by XGBoost and LightGBM."""

from __future__ import annotations

import hashlib
import json
import platform
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from mike_trade.modeling.adapters.common import FitResult
from mike_trade.modeling.adapters.lightgbm import (
    LightGBMAdapterFactory,
    LightGBMPredictor,
    LightGBMTrainer,
)
from mike_trade.modeling.adapters.xgboost import (
    XGBoostAdapterFactory,
    XGBoostPredictor,
    XGBoostTrainer,
)
from mike_trade.modeling.artifacts import (
    FileModelRepository,
    ModelArtifactManifest,
    StoredModel,
    schema_fingerprint,
)
from mike_trade.modeling.builder import ChronologicalTrainingDatasetBuilder
from mike_trade.modeling.calibration import fit_probability_calibrator
from mike_trade.modeling.config import ModelAlgorithm, ModelExperimentConfig, ModelRole
from mike_trade.modeling.contracts import (
    DatasetSelection,
    FeatureSchema,
    LabelGenerator,
    LabelSchema,
    LearningTask,
    TrainingExample,
    TrainingRequest,
)
from mike_trade.modeling.datasets import ParquetDatasetProvider
from mike_trade.modeling.features import IntradayFeaturePipeline
from mike_trade.modeling.labels import (
    ExecutionCostAssumptions,
    ForwardReturnConfig,
    ForwardReturnLabelGenerator,
    TripleBarrierConfig,
    TripleBarrierLabelGenerator,
)
from mike_trade.modeling.matrix import DiskMatrixWriter, MatrixDataset
from mike_trade.modeling.metrics import (
    PredictiveEvaluation,
    evaluate_classification,
    evaluate_regression,
    permutation_importance,
)
from mike_trade.modeling.objectives import SellPositionDatasetBuilder
from mike_trade.modeling.registry import ModelAdapterRegistry
from mike_trade.modeling.splits import TimeRange

TreeTrainer = XGBoostTrainer | LightGBMTrainer
TreePredictor = XGBoostPredictor | LightGBMPredictor


@dataclass(frozen=True, slots=True)
class PreparedMatrices:
    selection: DatasetSelection
    feature_schema: FeatureSchema
    label_schema: LabelSchema
    fit: MatrixDataset
    early_stopping: MatrixDataset
    evaluation: MatrixDataset
    calibration: MatrixDataset | None
    entry_policy: Mapping[str, str]
    execution_assumptions: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class TrainingOutcome:
    stored: StoredModel
    evaluation: PredictiveEvaluation
    search_trials: tuple[Mapping[str, object], ...]


def _in_range(example: TrainingExample, interval: TimeRange) -> bool:
    return interval.contains(example.features.as_of) and example.label.outcome_time < interval.end


def _slice(dataset: MatrixDataset, count: int) -> MatrixDataset:
    size = min(count, dataset.row_count)
    return MatrixDataset(
        dataset.features[:size],
        dataset.targets[:size],
        dataset.feature_names,
        dataset.symbols[:size],
        dataset.times[:size],
        None if dataset.weights is None else dataset.weights[:size],
    )


def _predictor(result: FitResult) -> TreePredictor:
    if not isinstance(result.predictor, (XGBoostPredictor, LightGBMPredictor)):
        raise TypeError("tree adapter returned an incompatible predictor")
    return result.predictor


def _evaluation(
    config: ModelExperimentConfig,
    predictor: TreePredictor,
    dataset: MatrixDataset,
    training_targets: np.ndarray[Any, Any],
) -> PredictiveEvaluation:
    if config.task is LearningTask.REGRESSION:
        return evaluate_regression(
            dataset,
            predictor.raw_matrix_predictions(dataset),
            training_targets=training_targets,
        )
    return evaluate_classification(
        dataset,
        predictor.probability_matrix(dataset),
        config.class_labels,
        training_targets=training_targets,
    )


class ModelTrainingService:
    def __init__(self, config: ModelExperimentConfig) -> None:
        self.config = config
        self.provider = ParquetDatasetProvider(config.dataset_root)
        self.repository = FileModelRepository(config.model_root)

    def _selection(
        self, dataset_ids: Mapping[str, str] | None = None
    ) -> DatasetSelection:
        primary = self.config.resolved_symbols()
        references = self.config.feature_config.reference_symbols
        symbols = tuple(dict.fromkeys((*primary, *references)))
        return self.provider.select(
            symbols,
            self.config.ranges.overall.start,
            self.config.ranges.overall.end,
            feed=self.config.feed,
            timeframe=self.config.timeframe,
            adjustment=self.config.adjustment,
            universe_id=self.config.universe_id,
            dataset_ids=dataset_ids,
        )

    def _builder(
        self,
    ) -> tuple[
        ChronologicalTrainingDatasetBuilder | SellPositionDatasetBuilder,
        FeatureSchema,
        LabelSchema,
        Mapping[str, str],
        Mapping[str, str],
    ]:
        pipeline = IntradayFeaturePipeline(self.config.feature_config)
        objective = self.config.objective_parameters
        costs = ExecutionCostAssumptions(
            Decimal(str(objective.get("entry_bps", 0))),
            Decimal(str(objective.get("exit_bps", 0))),
        )
        if self.config.role is ModelRole.SELL:
            assert self.config.entry_policy is not None and self.config.sell_config is not None
            builder = SellPositionDatasetBuilder(
                pipeline, self.config.entry_policy, self.config.sell_config
            )
            return (
                builder,
                builder.feature_schema,
                builder.label_schema,
                self.config.entry_policy.as_mapping(),
                costs.parameters(),
            )
        label_type = str(objective.get("type", "triple_barrier"))
        if label_type == "triple_barrier":
            generator: LabelGenerator = TripleBarrierLabelGenerator(
                TripleBarrierConfig(
                    Decimal(str(objective.get("positive_barrier", "0.01"))),
                    Decimal(str(objective.get("negative_barrier", "0.01"))),
                    int(str(objective.get("horizon_bars", 30))),
                    costs,
                )
            )
        elif label_type == "forward_return":
            generator = ForwardReturnLabelGenerator(
                ForwardReturnConfig(
                    int(str(objective.get("horizon_bars", 10))),
                    self.config.task,
                    Decimal(str(objective.get("positive_threshold", 0))),
                    Decimal(str(objective.get("negative_threshold", 0))),
                    costs,
                )
            )
        else:
            raise ValueError(f"unknown BUY label type: {label_type}")
        return (
            ChronologicalTrainingDatasetBuilder(pipeline, generator),
            pipeline.schema,
            generator.schema,
            {},
            costs.parameters(),
        )

    def prepare(
        self,
        root: str | Path,
        *,
        dataset_ids: Mapping[str, str] | None = None,
    ) -> PreparedMatrices:
        selection = self._selection(dataset_ids)
        builder, feature_schema, label_schema, entry_policy, assumptions = self._builder()
        if self.config.selected_feature_names != tuple(
            name
            for name in feature_schema.names
            if name in self.config.selected_feature_names
        ):
            raise ValueError("selected feature order is incompatible with objective schema")
        class_labels = self.config.class_labels
        writers = {
            "fit": DiskMatrixWriter(
                Path(root) / "fit",
                self.config.selected_feature_names,
                task=self.config.task,
                class_labels=class_labels,
            ),
            "early": DiskMatrixWriter(
                Path(root) / "early",
                self.config.selected_feature_names,
                task=self.config.task,
                class_labels=class_labels,
            ),
            "evaluation": DiskMatrixWriter(
                Path(root) / "evaluation",
                self.config.selected_feature_names,
                task=self.config.task,
                class_labels=class_labels,
                retain_metadata=True,
            ),
        }
        if self.config.ranges.calibration is not None:
            writers["calibration"] = DiskMatrixWriter(
                Path(root) / "calibration",
                self.config.selected_feature_names,
                task=self.config.task,
                class_labels=class_labels,
            )
        reference_bars = {
            symbol: tuple(
                self.provider.iter_bars(selection, symbol, batch_size=self.config.batch_size)
            )
            for symbol in feature_schema.required_reference_symbols
        }
        primary_symbols = tuple(
            symbol
            for symbol in self.config.resolved_symbols()
            if symbol not in feature_schema.required_reference_symbols
        )
        for symbol in primary_symbols:
            bars = tuple(
                self.provider.iter_bars(selection, symbol, batch_size=self.config.batch_size)
            )
            examples = tuple(builder.build_symbol(bars, references=reference_bars))
            writers["fit"].append(
                tuple(item for item in examples if _in_range(item, self.config.ranges.fit))
            )
            writers["early"].append(
                tuple(
                    item
                    for item in examples
                    if _in_range(item, self.config.ranges.early_stopping)
                )
            )
            writers["evaluation"].append(
                tuple(
                    item
                    for item in examples
                    if _in_range(item, self.config.ranges.evaluation)
                )
            )
            if self.config.ranges.calibration is not None:
                writers["calibration"].append(
                    tuple(
                        item
                        for item in examples
                        if _in_range(item, self.config.ranges.calibration)
                    )
                )
        return PreparedMatrices(
            selection,
            feature_schema,
            label_schema,
            writers["fit"].finish(),
            writers["early"].finish(),
            writers["evaluation"].finish(),
            writers["calibration"].finish() if "calibration" in writers else None,
            entry_policy,
            assumptions,
        )

    def _trainer(self) -> TreeTrainer:
        adapter = self.config.adapter_config()
        if self.config.algorithm is ModelAlgorithm.XGBOOST:
            from mike_trade.modeling.adapters.common import XGBoostTrainingConfig

            assert isinstance(adapter, XGBoostTrainingConfig)
            return XGBoostTrainer(adapter)
        from mike_trade.modeling.adapters.common import LightGBMTrainingConfig

        assert isinstance(adapter, LightGBMTrainingConfig)
        return LightGBMTrainer(adapter)

    def train(self) -> TrainingOutcome:
        with tempfile.TemporaryDirectory(prefix="mike-trade-model-") as directory:
            prepared = self.prepare(directory)
            request = TrainingRequest(
                self.config.model_id,
                self.config.model_version,
                self.config.seed,
                prepared.feature_schema,
                prepared.label_schema,
                self.config.hyperparameters,
            )
            base_trainer = self._trainer()
            candidates: tuple[Mapping[str, object], ...] = (
                self.config.search.candidates or ({},)
            )
            trials: list[Mapping[str, object]] = []
            best_result: FitResult | None = None
            best_score = float("inf")
            for number, candidate in enumerate(candidates, start=1):
                trainer = (
                    base_trainer.with_parameters(candidate) if candidate else base_trainer
                )
                result = trainer.fit(request, prepared.fit, prepared.early_stopping)
                predictor = _predictor(result)
                early_evaluation = _evaluation(
                    self.config, predictor, prepared.early_stopping, prepared.fit.targets
                )
                score_name = (
                    "rmse"
                    if self.config.task is LearningTask.REGRESSION
                    else "log_loss"
                )
                score = float(early_evaluation.metrics[score_name])
                trials.append(
                    {
                        "trial": number,
                        "parameters": dict(candidate),
                        "selection_metric": score_name,
                        "selection_score": score,
                        "best_iteration": result.best_iteration,
                    }
                )
                if score < best_score:
                    best_score, best_result = score, result
            assert best_result is not None
            predictor = _predictor(best_result)
            if self.config.probability_calibration:
                assert prepared.calibration is not None
                calibrator = fit_probability_calibrator(
                    predictor.raw_matrix_predictions(prepared.calibration),
                    prepared.calibration.targets,
                )
                predictor = predictor.with_calibrator(calibrator)
                best_result = replace(
                    best_result, predictor=predictor, calibrator=calibrator
                )
            evaluation = _evaluation(
                self.config, predictor, prepared.evaluation, prepared.fit.targets
            )
            importance = dict(best_result.feature_importance)
            if self.config.permutation_importance:
                sample = _slice(prepared.evaluation, self.config.permutation_max_rows)
                if self.config.task is LearningTask.REGRESSION:
                    predict = predictor.raw_matrix_predictions
                else:
                    predict = predictor.probability_matrix
                for name, value in permutation_importance(
                    sample, predict, task=self.config.task, seed=self.config.seed
                ).items():
                    importance[f"{name}.permutation"] = repr(value)
            evaluation_payload = json.loads(evaluation.to_json_bytes())
            evaluation_payload["search_trials"] = trials
            evaluation_payload["feature_importance_disclaimer"] = (
                "Importance is predictive attribution, not evidence of causation."
            )
            evaluation_bytes = (
                json.dumps(
                    evaluation_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode()
            artifact = best_result.artifact
            hyperparameters = dict(best_result.hyperparameters)
            hyperparameters["best_iteration"] = str(best_result.best_iteration)
            validation_ranges = [self.config.ranges.early_stopping]
            if self.config.ranges.calibration is not None:
                validation_ranges.append(self.config.ranges.calibration)
            validation_ranges.append(self.config.ranges.evaluation)
            scalar_metrics = {
                key: str(value)
                for key, value in evaluation.metrics.items()
                if isinstance(value, (str, int, float, Decimal))
            }
            manifest = ModelArtifactManifest(
                manifest_schema_version=2,
                model_id=self.config.model_id,
                model_version=self.config.model_version,
                created_at=datetime.now(UTC),
                adapter_id=self.config.algorithm.value,
                adapter_version="1.0.0",
                artifact_format=best_result.artifact_format,
                artifact_sha256=hashlib.sha256(artifact).hexdigest(),
                artifact_size=len(artifact),
                feature_schema=prepared.feature_schema,
                feature_schema_sha256=schema_fingerprint(prepared.feature_schema),
                label_schema=prepared.label_schema,
                label_schema_sha256=schema_fingerprint(prepared.label_schema),
                dataset_sources=prepared.selection.sources,
                feed=prepared.selection.feed,
                timeframe=prepared.selection.timeframe,
                adjustment=prepared.selection.adjustment,
                universe_id=prepared.selection.universe_id,
                training_ranges=(self.config.ranges.fit,),
                validation_ranges=tuple(validation_ranges),
                validation_method=(
                    "chronological_early_stop_then_calibration_then_evaluation;"
                    f"embargo_minutes={int(self.config.ranges.embargo.total_seconds() // 60)};"
                    "half_open=true;global_cross_symbol=true"
                ),
                execution_assumptions=prepared.execution_assumptions,
                random_seed=self.config.seed,
                software_versions={
                    "python": platform.python_version(),
                    self.config.algorithm.value: best_result.library_version,
                    "numpy": np.__version__,
                },
                training_config_sha256=self.config.config_sha256,
                validation_metrics=scalar_metrics,
                holdout_evaluated=False,
                algorithm=self.config.algorithm.value,
                learning_objective=(
                    f"{self.config.role.value}_{prepared.label_schema.label_id}"
                ),
                selected_feature_names=self.config.selected_feature_names,
                class_labels=self.config.class_labels,
                hyperparameters=hyperparameters,
                calibration=best_result.calibrator.as_mapping(),
                entry_policy=prepared.entry_policy,
                feature_importance=importance,
                evaluation_sha256=hashlib.sha256(evaluation_bytes).hexdigest(),
                evaluation_size=len(evaluation_bytes),
            )
            stored = self.repository.put(manifest, artifact, evaluation_bytes)
            return TrainingOutcome(stored, evaluation, tuple(trials))


def load_model_predictor(
    repository: FileModelRepository, model_id: str, version: str
) -> tuple[ModelArtifactManifest, TreePredictor]:
    manifest, artifact = repository.get(model_id, version)
    registry = ModelAdapterRegistry()
    registry.register(XGBoostAdapterFactory())
    registry.register(LightGBMAdapterFactory())
    factory = registry.get(manifest.adapter_id, manifest.adapter_version)
    loaded = factory.predictor(artifact, manifest)
    if not isinstance(loaded, (XGBoostPredictor, LightGBMPredictor)):
        raise TypeError("trusted tree adapter returned an incompatible predictor")
    predictor: TreePredictor = loaded
    return manifest, predictor


def evaluate_saved_model(config: ModelExperimentConfig) -> PredictiveEvaluation:
    service = ModelTrainingService(config)
    pinned_manifest, predictor = load_model_predictor(
        service.repository, config.model_id, config.model_version
    )
    dataset_ids = {
        source.symbol: source.dataset_id for source in pinned_manifest.dataset_sources
    }
    with tempfile.TemporaryDirectory(prefix="mike-trade-model-eval-") as directory:
        prepared = service.prepare(directory, dataset_ids=dataset_ids)
        manifest = pinned_manifest
        selected_sources = tuple(
            (source.dataset_id, source.sha256) for source in prepared.selection.sources
        )
        manifest_sources = tuple(
            (source.dataset_id, source.sha256) for source in manifest.dataset_sources
        )
        if selected_sources != manifest_sources:
            raise ValueError(
                "evaluation dataset identities differ from the pinned model manifest"
            )
        if schema_fingerprint(prepared.feature_schema) != manifest.feature_schema_sha256:
            raise ValueError("evaluation feature schema is incompatible with the model")
        if prepared.evaluation.feature_names != manifest.selected_feature_names:
            raise ValueError("evaluation feature ordering is incompatible with the model")
        return _evaluation(config, predictor, prepared.evaluation, prepared.fit.targets)
