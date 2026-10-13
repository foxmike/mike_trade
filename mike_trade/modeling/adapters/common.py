"""Typed configuration and results shared only by the two tree adapters."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from mike_trade.modeling.calibration import ProbabilityCalibrator
from mike_trade.modeling.contracts import LearningTask, ModelPredictor


class ComputeDevice(StrEnum):
    CPU = "cpu"
    GPU = "gpu"


class ClassBalance(StrEnum):
    NONE = "none"
    BALANCED = "balanced"


def _positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive int")


def _fraction(value: float, name: str, *, allow_one: bool = True) -> None:
    upper = value <= 1 if allow_one else value < 1
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 < value or not upper:
        raise ValueError(f"{name} must be in (0, {'1]' if allow_one else '1)'})")


@dataclass(frozen=True, slots=True)
class XGBoostTrainingConfig:
    task: LearningTask
    selected_feature_names: tuple[str, ...]
    class_labels: tuple[str, ...] = ()
    device: ComputeDevice = ComputeDevice.CPU
    threads: int = 4
    num_boost_round: int = 300
    early_stopping_rounds: int = 25
    max_depth: int = 6
    learning_rate: float = 0.05
    min_child_weight: float = 1.0
    subsample: float = 0.9
    colsample_bytree: float = 0.9
    reg_alpha: float = 0.0
    reg_lambda: float = 1.0
    class_balance: ClassBalance = ClassBalance.BALANCED
    extra_parameters: Mapping[str, str | int | float | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_common(self.task, self.selected_feature_names, self.class_labels)
        for name in ("threads", "num_boost_round", "early_stopping_rounds", "max_depth"):
            _positive_int(getattr(self, name), name)
        _fraction(self.learning_rate, "learning_rate")
        _fraction(self.subsample, "subsample")
        _fraction(self.colsample_bytree, "colsample_bytree")
        for name in ("min_child_weight", "reg_alpha", "reg_lambda"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        _reject_reserved(
            self.extra_parameters,
            {
                "objective",
                "eval_metric",
                "seed",
                "random_state",
                "nthread",
                "device",
                "tree_method",
                "max_depth",
                "eta",
                "learning_rate",
                "min_child_weight",
                "subsample",
                "colsample_bytree",
                "alpha",
                "lambda",
                "num_class",
                "scale_pos_weight",
            },
        )


@dataclass(frozen=True, slots=True)
class LightGBMTrainingConfig:
    task: LearningTask
    selected_feature_names: tuple[str, ...]
    class_labels: tuple[str, ...] = ()
    device: ComputeDevice = ComputeDevice.CPU
    threads: int = 4
    num_boost_round: int = 300
    early_stopping_rounds: int = 25
    num_leaves: int = 31
    max_depth: int = -1
    learning_rate: float = 0.05
    min_data_in_leaf: int = 20
    feature_fraction: float = 0.9
    bagging_fraction: float = 0.9
    bagging_freq: int = 1
    reg_alpha: float = 0.0
    reg_lambda: float = 1.0
    class_balance: ClassBalance = ClassBalance.BALANCED
    extra_parameters: Mapping[str, str | int | float | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_common(self.task, self.selected_feature_names, self.class_labels)
        for name in (
            "threads",
            "num_boost_round",
            "early_stopping_rounds",
            "num_leaves",
            "min_data_in_leaf",
            "bagging_freq",
        ):
            _positive_int(getattr(self, name), name)
        if (
            isinstance(self.max_depth, bool)
            or not isinstance(self.max_depth, int)
            or self.max_depth == 0
            or self.max_depth < -1
        ):
            raise ValueError("max_depth must be -1 or a positive int")
        _fraction(self.learning_rate, "learning_rate")
        _fraction(self.feature_fraction, "feature_fraction")
        _fraction(self.bagging_fraction, "bagging_fraction")
        for name in ("reg_alpha", "reg_lambda"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        _reject_reserved(
            self.extra_parameters,
            {
                "objective",
                "metric",
                "seed",
                "num_threads",
                "device",
                "device_type",
                "feature_fraction_seed",
                "bagging_seed",
                "data_random_seed",
                "learning_rate",
                "num_leaves",
                "max_depth",
                "min_data_in_leaf",
                "feature_fraction",
                "bagging_fraction",
                "bagging_freq",
                "lambda_l1",
                "lambda_l2",
                "num_class",
                "is_unbalance",
                "scale_pos_weight",
            },
        )


def _validate_common(
    task: LearningTask, selected: tuple[str, ...], class_labels: tuple[str, ...]
) -> None:
    if not isinstance(task, LearningTask) or not selected or len(selected) != len(set(selected)):
        raise ValueError("task and unique selected_feature_names are required")
    if task is LearningTask.REGRESSION and class_labels:
        raise ValueError("regression configuration cannot contain class labels")
    if task is LearningTask.BINARY_CLASSIFICATION and len(class_labels) != 2:
        raise ValueError("binary classification requires exactly two class labels")
    if task is LearningTask.MULTICLASS_CLASSIFICATION and len(class_labels) < 3:
        raise ValueError("multiclass classification requires at least three class labels")
    if len(class_labels) != len(set(class_labels)):
        raise ValueError("class labels must be unique")


def _reject_reserved(parameters: Mapping[str, object], reserved: set[str]) -> None:
    overlap = set(parameters) & reserved
    if overlap:
        raise ValueError(f"extra_parameters cannot override controlled values: {sorted(overlap)}")
    for key, value in parameters.items():
        if not isinstance(key, str) or not key:
            raise ValueError("extra_parameters keys must be non-empty strings")
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError("extra_parameters values must be scalar")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("extra_parameters values must be finite")


@dataclass(frozen=True, slots=True)
class FitResult:
    artifact: bytes
    artifact_format: str
    predictor: ModelPredictor
    hyperparameters: Mapping[str, str]
    feature_importance: Mapping[str, str]
    best_iteration: int
    calibrator: ProbabilityCalibrator
    library_version: str
