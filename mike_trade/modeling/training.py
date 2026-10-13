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
from mike_trade.modeling.config import (
    ExperimentRanges,
    ModelAlgorithm,
    ModelExperimentConfig,
    ModelRole,
)
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
class FittedModel:
    result: FitResult
    predictor: TreePredictor
    evaluation: PredictiveEvaluation
    trials: tuple[Mapping[str, object], ...]
    importance: Mapping[str, str]


def _target_distribution(targets: np.ndarray[Any, Any], labels: tuple[str, ...]) -> dict[str, int]:
    values, counts = np.unique(np.asarray(targets), return_counts=True)
    if not labels:
        return {"rows": int(len(targets))}
    return {
        labels[int(value)]: int(count)
        for value, count in zip(values, counts, strict=True)
        if 0 <= int(value) < len(labels)
    }


def prepared_counts(
    prepared: PreparedMatrices, class_labels: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Real, recorded observation counts for every partition (and the label mix of fit)."""
    return {
        "fit": prepared.fit.row_count,
        "early_stopping": prepared.early_stopping.row_count,
        "calibration": 0 if prepared.calibration is None else prepared.calibration.row_count,
        "evaluation": prepared.evaluation.row_count,
        "features": len(prepared.fit.feature_names),
        "primary_symbols_in_selection": len(prepared.selection.sources),
        "fit_target_counts": _target_distribution(prepared.fit.targets, class_labels),
    }


def _range_mapping(ranges: ExperimentRanges) -> dict[str, Any]:
    def span(item: TimeRange | None) -> dict[str, str] | None:
        return (
            None if item is None else {"start": item.start.isoformat(), "end": item.end.isoformat()}
        )

    return {
        "fit": span(ranges.fit),
        "early_stopping": span(ranges.early_stopping),
        "calibration": span(ranges.calibration),
        "evaluation": span(ranges.evaluation),
        "embargo_minutes": int(ranges.embargo.total_seconds() // 60),
    }


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
        self,
        dataset_ids: Mapping[str, str] | None = None,
        window: TimeRange | None = None,
    ) -> DatasetSelection:
        primary = self.config.resolved_symbols()
        references = self.config.feature_config.reference_symbols
        symbols = tuple(dict.fromkeys((*primary, *references)))
        window = window or self.config.ranges.overall
        return self.provider.select(
            symbols,
            window.start,
            window.end,
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
        return self.prepare_many(root, {"main": self.config.ranges}, dataset_ids=dataset_ids)[
            "main"
        ]

    def prepare_many(
        self,
        root: str | Path,
        range_sets: Mapping[str, ExperimentRanges],
        *,
        dataset_ids: Mapping[str, str] | None = None,
    ) -> dict[str, PreparedMatrices]:
        """Build features and labels once per symbol and route them into every range set.

        Each range set (the main experiment, or one walk-forward fold) receives only examples
        whose feature time is inside a partition and whose label outcome resolves strictly
        before that partition ends, so no label ever peeks beyond its own partition.
        """
        if not range_sets:
            raise ValueError("at least one range set is required")
        window = TimeRange(
            min(item.fit.start for item in range_sets.values()),
            max(item.evaluation.end for item in range_sets.values()),
        )
        selection = self._selection(dataset_ids, window)
        builder, feature_schema, label_schema, entry_policy, assumptions = self._builder()
        if self.config.selected_feature_names != tuple(
            name for name in feature_schema.names if name in self.config.selected_feature_names
        ):
            raise ValueError("selected feature order is incompatible with objective schema")
        class_labels = self.config.class_labels

        def writer(name: str, part: str, *, metadata: bool = False) -> DiskMatrixWriter:
            return DiskMatrixWriter(
                Path(root) / name / part,
                self.config.selected_feature_names,
                task=self.config.task,
                class_labels=class_labels,
                retain_metadata=metadata,
            )

        writers: dict[str, dict[str, DiskMatrixWriter]] = {}
        for name, ranges in range_sets.items():
            parts = {
                "fit": writer(name, "fit"),
                "early": writer(name, "early"),
                "evaluation": writer(name, "evaluation", metadata=True),
            }
            if ranges.calibration is not None:
                parts["calibration"] = writer(name, "calibration")
            writers[name] = parts
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
            for name, ranges in range_sets.items():
                targets = {
                    "fit": ranges.fit,
                    "early": ranges.early_stopping,
                    "evaluation": ranges.evaluation,
                }
                if ranges.calibration is not None:
                    targets["calibration"] = ranges.calibration
                for part, interval in targets.items():
                    writers[name][part].append(
                        tuple(item for item in examples if _in_range(item, interval))
                    )
        return {
            name: PreparedMatrices(
                selection,
                feature_schema,
                label_schema,
                parts["fit"].finish(),
                parts["early"].finish(),
                parts["evaluation"].finish(),
                parts["calibration"].finish() if "calibration" in parts else None,
                entry_policy,
                assumptions,
            )
            for name, parts in writers.items()
        }

    def _trainer(self, config: ModelExperimentConfig | None = None) -> TreeTrainer:
        config = config or self.config
        adapter = config.adapter_config()
        if config.algorithm is ModelAlgorithm.XGBOOST:
            from mike_trade.modeling.adapters.common import XGBoostTrainingConfig

            assert isinstance(adapter, XGBoostTrainingConfig)
            return XGBoostTrainer(adapter)
        from mike_trade.modeling.adapters.common import LightGBMTrainingConfig

        assert isinstance(adapter, LightGBMTrainingConfig)
        return LightGBMTrainer(adapter)

    def fit(self, config: ModelExperimentConfig, prepared: PreparedMatrices) -> FittedModel:
        """Search, early-stop, calibrate (separate partition) and evaluate; stores nothing.

        Shared by the final training run and every walk-forward fold, so a fold is trained
        exactly like the model that is eventually saved.
        """
        request = TrainingRequest(
            config.model_id,
            config.model_version,
            config.seed,
            prepared.feature_schema,
            prepared.label_schema,
            config.hyperparameters,
        )
        base_trainer = self._trainer(config)
        candidates: tuple[Mapping[str, object], ...] = config.search.candidates or ({},)
        trials: list[Mapping[str, object]] = []
        best_result: FitResult | None = None
        best_score = float("inf")
        for number, candidate in enumerate(candidates, start=1):
            trainer = base_trainer.with_parameters(candidate) if candidate else base_trainer
            result = trainer.fit(request, prepared.fit, prepared.early_stopping)
            predictor = _predictor(result)
            early_evaluation = _evaluation(
                config, predictor, prepared.early_stopping, prepared.fit.targets
            )
            score_name = "rmse" if config.task is LearningTask.REGRESSION else "log_loss"
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
        if config.probability_calibration:
            assert prepared.calibration is not None
            calibrator = fit_probability_calibrator(
                predictor.raw_matrix_predictions(prepared.calibration),
                prepared.calibration.targets,
            )
            predictor = predictor.with_calibrator(calibrator)
            best_result = replace(best_result, predictor=predictor, calibrator=calibrator)
        evaluation = _evaluation(config, predictor, prepared.evaluation, prepared.fit.targets)
        importance = dict(best_result.feature_importance)
        if config.permutation_importance:
            sample = _slice(prepared.evaluation, config.permutation_max_rows)
            predict = (
                predictor.raw_matrix_predictions
                if config.task is LearningTask.REGRESSION
                else predictor.probability_matrix
            )
            for name, value in permutation_importance(
                sample, predict, task=config.task, seed=config.seed
            ).items():
                importance[f"{name}.permutation"] = repr(value)
        return FittedModel(best_result, predictor, evaluation, tuple(trials), importance)

    def train(
        self,
        *,
        dataset_ids: Mapping[str, str] | None = None,
        expected_sha256: Mapping[str, str] | None = None,
    ) -> TrainingOutcome:
        """Train and store one model.

        ``dataset_ids``/``expected_sha256`` come from a ``model-prepare`` lock: the exact
        snapshots are used, and training stops before anything is stored if the bytes the
        provider resolved are not the ones that were locked.
        """
        with tempfile.TemporaryDirectory(prefix="mike-trade-model-") as directory:
            prepared = self.prepare(directory, dataset_ids=dataset_ids)
            if expected_sha256 is not None:
                resolved = {item.symbol: item.sha256 for item in prepared.selection.sources}
                if resolved != dict(expected_sha256):
                    raise ValueError("resolved datasets differ from the locked preparation")
            fitted = self.fit(self.config, prepared)
            best_result, evaluation = fitted.result, fitted.evaluation
            trials = list(fitted.trials)
            importance = fitted.importance
            evaluation_payload = json.loads(evaluation.to_json_bytes())
            evaluation_payload["search_trials"] = trials
            evaluation_payload["dataset_counts"] = prepared_counts(
                prepared, self.config.class_labels
            )
            evaluation_payload["training_ranges"] = _range_mapping(self.config.ranges)
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
                learning_objective=(f"{self.config.role.value}_{prepared.label_schema.label_id}"),
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
    dataset_ids = {source.symbol: source.dataset_id for source in pinned_manifest.dataset_sources}
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
            raise ValueError("evaluation dataset identities differ from the pinned model manifest")
        if schema_fingerprint(prepared.feature_schema) != manifest.feature_schema_sha256:
            raise ValueError("evaluation feature schema is incompatible with the model")
        if prepared.evaluation.feature_names != manifest.selected_feature_names:
            raise ValueError("evaluation feature ordering is incompatible with the model")
        return _evaluation(config, predictor, prepared.evaluation, prepared.fit.targets)
