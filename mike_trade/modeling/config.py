"""Strict, versioned configuration for reproducible model experiments."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml  # type: ignore[import-untyped]

from mike_trade.host_resources import default_native_threads
from mike_trade.modeling.adapters.common import (
    ClassBalance,
    ComputeDevice,
    LightGBMTrainingConfig,
    XGBoostTrainingConfig,
)
from mike_trade.modeling.contracts import LearningTask, identifier, semver
from mike_trade.modeling.features import IntradayFeatureConfig
from mike_trade.modeling.labels import ExecutionCostAssumptions
from mike_trade.modeling.objectives import (
    ScheduledEntryPolicy,
    SellContinuationConfig,
    SellPositionDatasetBuilder,
)
from mike_trade.modeling.splits import DEVELOPMENT_START, FINAL_HOLDOUT, TimeRange


class ModelAlgorithm(StrEnum):
    XGBOOST = "xgboost"
    LIGHTGBM = "lightgbm"


class ModelRole(StrEnum):
    BUY = "buy"
    SELL = "sell"


def _datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(f"{name} must be an ISO timestamp string")
    if result.tzinfo is None:
        raise ValueError(f"{name} must include timezone")
    return result


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception:
        raise ValueError(f"{name} must be decimal-compatible") from None
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class ExperimentRanges:
    fit: TimeRange
    early_stopping: TimeRange
    evaluation: TimeRange
    calibration: TimeRange | None = None
    embargo: timedelta = timedelta(0)

    def __post_init__(self) -> None:
        if self.embargo < timedelta(0):
            raise ValueError("embargo must be non-negative")
        ordered = [self.fit, self.early_stopping]
        if self.calibration is not None:
            ordered.append(self.calibration)
        ordered.append(self.evaluation)
        if any(item.start < DEVELOPMENT_START for item in ordered):
            raise ValueError("model experiment ranges cannot precede the development period")
        if any(item.overlaps(FINAL_HOLDOUT) for item in ordered):
            raise ValueError("model experiment ranges cannot overlap the final holdout")
        adjacent = zip(ordered, ordered[1:], strict=False)
        if any(right.start < left.end + self.embargo for left, right in adjacent):
            raise ValueError("model ranges must be chronological, disjoint, and embargoed")

    @property
    def overall(self) -> TimeRange:
        return TimeRange(self.fit.start, self.evaluation.end)


@dataclass(frozen=True, slots=True)
class WalkForwardConfig:
    """Expanding purged walk-forward folds ending at the experiment's evaluation end.

    Fold ``i`` of ``N`` validates on the ``validation_days``-long window that is ``N - i``
    windows before the evaluation end; it trains on everything from ``fit_start`` up to the
    validation start minus the embargo, split chronologically into fit, early-stopping and
    (optionally) calibration partitions separated by the embargo.
    """

    folds: int = 3
    validation_days: int = 7
    early_stopping_fraction: float = 0.15
    calibration_fraction: float = 0.15

    def __post_init__(self) -> None:
        if isinstance(self.folds, bool) or not 2 <= self.folds <= 12:
            raise ValueError("walk_forward.folds must be an int in [2, 12]")
        if isinstance(self.validation_days, bool) or not 1 <= self.validation_days <= 120:
            raise ValueError("walk_forward.validation_days must be an int in [1, 120]")
        for name in ("early_stopping_fraction", "calibration_fraction"):
            value = getattr(self, name)
            if not 0.05 <= value <= 0.4:
                raise ValueError(f"walk_forward.{name} must be within [0.05, 0.4]")

    def fold_ranges(self, config: ModelExperimentConfig) -> dict[str, ExperimentRanges]:
        """Concrete, holdout-safe ranges for every fold (validated by ``ExperimentRanges``)."""
        embargo = config.ranges.embargo
        window = timedelta(days=self.validation_days)
        end = config.ranges.evaluation.end
        calibrating = config.probability_calibration
        result: dict[str, ExperimentRanges] = {}
        for number in range(1, self.folds + 1):
            validation = TimeRange(
                end - window * (self.folds - number + 1), end - window * (self.folds - number)
            )
            boundary = validation.start - embargo
            total = boundary - config.ranges.fit.start
            early_length = total * self.early_stopping_fraction
            calibration_length = total * self.calibration_fraction if calibrating else timedelta(0)
            calibration = (
                TimeRange(boundary - calibration_length, boundary) if calibrating else None
            )
            early_end = (calibration.start - embargo) if calibration else boundary
            early = TimeRange(early_end - early_length, early_end)
            fit = TimeRange(config.ranges.fit.start, early.start - embargo)
            result[f"fold_{number}"] = ExperimentRanges(
                fit, early, validation, calibration, embargo
            )
        return result


@dataclass(frozen=True, slots=True)
class SearchConfig:
    candidates: tuple[Mapping[str, object], ...] = ()
    max_trials: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.max_trials, bool) or not 1 <= self.max_trials <= 32:
            raise ValueError("search max_trials must be in [1, 32]")
        if len(self.candidates) > self.max_trials:
            raise ValueError("search candidates exceed max_trials")
        object.__setattr__(
            self, "candidates", tuple(MappingProxyType(dict(item)) for item in self.candidates)
        )


@dataclass(frozen=True, slots=True)
class ModelExperimentConfig:
    schema_version: int
    experiment_id: str
    algorithm: ModelAlgorithm
    role: ModelRole
    model_id: str
    model_version: str
    dataset_root: Path
    model_root: Path
    universe_id: str
    symbols: tuple[str, ...]
    universe_file: Path | None
    feed: str
    timeframe: str
    adjustment: str
    ranges: ExperimentRanges
    seed: int
    feature_config: IntradayFeatureConfig
    selected_feature_names: tuple[str, ...]
    task: LearningTask
    class_labels: tuple[str, ...]
    objective_parameters: Mapping[str, object]
    resources: Mapping[str, object]
    hyperparameters: Mapping[str, object]
    search: SearchConfig
    entry_policy: ScheduledEntryPolicy | None = None
    sell_config: SellContinuationConfig | None = None
    probability_calibration: bool = False
    permutation_importance: bool = False
    permutation_max_rows: int = 5000
    batch_size: int = 4096
    config_sha256: str = ""
    walk_forward: WalkForwardConfig | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported model experiment schema_version")
        identifier(self.experiment_id, "experiment_id")
        identifier(self.model_id, "model_id")
        semver(self.model_version, "model_version")
        if self.feed not in {"iex", "sip"}:
            raise ValueError("feed must be explicitly 'iex' or 'sip'")
        if self.timeframe != "1m" or self.adjustment != "raw":
            raise ValueError("initial model training supports only 1m RAW observations")
        if not self.symbols and self.universe_file is None:
            raise ValueError("symbols or universe_file is required")
        if self.role is ModelRole.SELL and (self.entry_policy is None or self.sell_config is None):
            raise ValueError("SELL experiments require entry_policy and sell configuration")
        if self.role is ModelRole.BUY and self.entry_policy is not None:
            raise ValueError("BUY experiments do not use a historical position entry policy")
        objective_type = str(self.objective_parameters.get("type", "triple_barrier"))
        if self.role is ModelRole.SELL and (
            self.task is not LearningTask.MULTICLASS_CLASSIFICATION
            or self.class_labels != ("-1", "0", "1")
        ):
            raise ValueError("SELL continuation requires multiclass labels -1, 0, 1")
        if (
            self.role is ModelRole.BUY
            and objective_type == "triple_barrier"
            and (
                self.task is not LearningTask.MULTICLASS_CLASSIFICATION
                or self.class_labels != ("-1", "0", "1")
            )
        ):
            raise ValueError("triple-barrier BUY requires multiclass labels -1, 0, 1")
        expected = tuple(
            name for name in self.feature_config.names() if name in self.selected_feature_names
        )
        if self.role is ModelRole.BUY and expected != self.selected_feature_names:
            raise ValueError("selected features must preserve the versioned feature schema order")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be int")
        for name in ("permutation_max_rows", "batch_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive int")
        object.__setattr__(
            self,
            "objective_parameters",
            MappingProxyType(dict(self.objective_parameters)),
        )
        object.__setattr__(self, "resources", MappingProxyType(dict(self.resources)))
        object.__setattr__(self, "hyperparameters", MappingProxyType(dict(self.hyperparameters)))

    def resolved_symbols(self) -> tuple[str, ...]:
        if self.symbols:
            return tuple(dict.fromkeys(symbol.upper() for symbol in self.symbols))
        assert self.universe_file is not None
        raw = json.loads(self.universe_file.read_text(encoding="utf-8"))
        if raw.get("universe_id") != self.universe_id:
            raise ValueError("universe file identity does not match experiment universe_id")
        return tuple(dict.fromkeys(str(item["symbol"]).upper() for item in raw.get("symbols", ())))

    def resolved_threads(self) -> int:
        """``resources.threads``: an explicit count is kept; absent or ``auto`` uses the host."""
        value = self.resources.get("threads", "auto")
        if str(value).strip().lower() == "auto":
            return default_native_threads()
        return int(str(value))

    def adapter_config(self) -> XGBoostTrainingConfig | LightGBMTrainingConfig:
        threads = self.resolved_threads()
        device = ComputeDevice(str(self.resources.get("device", "cpu")))
        balance = ClassBalance(str(self.hyperparameters.get("class_balance", "balanced")))
        common = {
            "task": self.task,
            "selected_feature_names": self.selected_feature_names,
            "class_labels": self.class_labels,
            "threads": threads,
            "device": device,
            "class_balance": balance,
        }
        values = {
            key: value for key, value in self.hyperparameters.items() if key != "class_balance"
        }
        if self.algorithm is ModelAlgorithm.XGBOOST:
            return XGBoostTrainingConfig(**common, **values)  # type: ignore[arg-type]
        return LightGBMTrainingConfig(**common, **values)  # type: ignore[arg-type]


def _walk_forward(value: object) -> WalkForwardConfig | None:
    if value is None:
        return None
    raw = _mapping(value, "walk_forward")
    unknown = set(raw) - {
        "folds",
        "validation_days",
        "early_stopping_fraction",
        "calibration_fraction",
    }
    if unknown:
        raise ValueError(f"unknown walk_forward keys: {sorted(unknown)}")
    return WalkForwardConfig(
        folds=int(raw.get("folds", 3)),
        validation_days=int(raw.get("validation_days", 7)),
        early_stopping_fraction=float(raw.get("early_stopping_fraction", 0.15)),
        calibration_fraction=float(raw.get("calibration_fraction", 0.15)),
    )


def load_experiment_config(path: str | Path) -> ModelExperimentConfig:
    config_path = Path(path)
    payload = config_path.read_bytes()
    raw_value = yaml.safe_load(payload)
    raw = _mapping(raw_value, "configuration")
    allowed = {
        "schema_version",
        "experiment_id",
        "algorithm",
        "role",
        "model_id",
        "model_version",
        "dataset_root",
        "model_root",
        "universe_id",
        "symbols",
        "universe_file",
        "feed",
        "timeframe",
        "adjustment",
        "ranges",
        "seed",
        "features",
        "objective",
        "resources",
        "hyperparameters",
        "search",
        "probability_calibration",
        "permutation_importance",
        "permutation_max_rows",
        "batch_size",
        "entry_policy",
        "walk_forward",
    }
    if set(raw) - allowed:
        raise ValueError(f"unknown experiment keys: {sorted(set(raw) - allowed)}")
    ranges_raw = _mapping(raw["ranges"], "ranges")
    ranges = ExperimentRanges(
        TimeRange(
            _datetime(ranges_raw["fit_start"], "fit_start"),
            _datetime(ranges_raw["fit_end"], "fit_end"),
        ),
        TimeRange(
            _datetime(ranges_raw["early_stopping_start"], "early_stopping_start"),
            _datetime(ranges_raw["early_stopping_end"], "early_stopping_end"),
        ),
        TimeRange(
            _datetime(ranges_raw["evaluation_start"], "evaluation_start"),
            _datetime(ranges_raw["evaluation_end"], "evaluation_end"),
        ),
        (
            TimeRange(
                _datetime(ranges_raw["calibration_start"], "calibration_start"),
                _datetime(ranges_raw["calibration_end"], "calibration_end"),
            )
            if ranges_raw.get("calibration_start") and ranges_raw.get("calibration_end")
            else None
        ),
        timedelta(minutes=int(ranges_raw.get("embargo_minutes", 0))),
    )
    feature_raw = _mapping(raw.get("features", {}), "features")
    feature_config = IntradayFeatureConfig(
        feature_id=str(feature_raw.get("feature_id", "intraday_core")),
        version=str(feature_raw.get("version", "1.0.0")),
        reference_symbols=tuple(str(value) for value in feature_raw.get("reference_symbols", ())),
        include_vwap=bool(feature_raw.get("include_vwap", True)),
    )
    objective = _mapping(raw["objective"], "objective")
    task = LearningTask(str(objective.get("task", "MULTICLASS_CLASSIFICATION")))
    role = ModelRole(str(raw["role"]))
    base_selected = tuple(
        str(value) for value in feature_raw.get("selected_feature_names", feature_config.names())
    )
    entry_policy = None
    sell_config = None
    selected = base_selected
    if role is ModelRole.SELL:
        entry_raw = _mapping(raw.get("entry_policy", {}), "entry_policy")
        entry_policy = ScheduledEntryPolicy(
            warmup_bars=int(entry_raw.get("warmup_bars", 30)),
            entry_interval_bars=int(entry_raw.get("entry_interval_bars", 60)),
            maximum_holding_bars=int(entry_raw.get("maximum_holding_bars", 45)),
            cooldown_bars=int(entry_raw.get("cooldown_bars", 15)),
        )
        costs = ExecutionCostAssumptions(
            _decimal(objective.get("entry_bps", 0), "entry_bps"),
            _decimal(objective.get("exit_bps", 0), "exit_bps"),
        )
        sell_config = SellContinuationConfig(
            horizon_bars=int(objective.get("horizon_bars", 10)),
            risk_barrier=_decimal(objective.get("risk_barrier", "0.01"), "risk_barrier"),
            minimum_hold_advantage=_decimal(
                objective.get("minimum_hold_advantage", 0), "minimum_hold_advantage"
            ),
            costs=costs,
        )
        position_names = SellPositionDatasetBuilder.POSITION_FEATURES
        selected = tuple((*base_selected, *position_names))
    if task is LearningTask.REGRESSION:
        labels_default: tuple[str, ...] = ()
    elif task is LearningTask.BINARY_CLASSIFICATION:
        labels_default = ("0", "1")
    else:
        labels_default = ("-1", "0", "1")
    search_raw = _mapping(raw.get("search", {}), "search")
    candidates = tuple(
        _mapping(item, "search candidate") for item in search_raw.get("candidates", ())
    )
    symbols = tuple(str(value).upper() for value in raw.get("symbols", ()))
    universe_file = raw.get("universe_file")
    result = ModelExperimentConfig(
        int(raw["schema_version"]),
        str(raw["experiment_id"]),
        ModelAlgorithm(str(raw["algorithm"])),
        role,
        str(raw["model_id"]),
        str(raw["model_version"]),
        Path(str(raw.get("dataset_root", "research/data"))),
        Path(str(raw.get("model_root", "research/models"))),
        str(raw["universe_id"]),
        symbols,
        Path(str(universe_file)) if universe_file else None,
        str(raw["feed"]).lower(),
        str(raw.get("timeframe", "1m")),
        str(raw.get("adjustment", "raw")),
        ranges,
        int(raw.get("seed", 271828)),
        feature_config,
        selected,
        task,
        tuple(str(value) for value in objective.get("class_labels", labels_default)),
        objective,
        _mapping(raw.get("resources", {}), "resources"),
        _mapping(raw.get("hyperparameters", {}), "hyperparameters"),
        SearchConfig(candidates, int(search_raw.get("max_trials", max(1, len(candidates))))),
        entry_policy,
        sell_config,
        bool(raw.get("probability_calibration", False)),
        bool(raw.get("permutation_importance", False)),
        int(raw.get("permutation_max_rows", 5000)),
        int(raw.get("batch_size", 4096)),
        hashlib.sha256(payload).hexdigest(),
        walk_forward=_walk_forward(raw.get("walk_forward")),
    )
    if result.walk_forward is not None:
        result.walk_forward.fold_ranges(result)  # fail at load, not mid-run
    if result.probability_calibration and result.ranges.calibration is None:
        raise ValueError("probability_calibration requires a separate calibration range")
    if result.task is LearningTask.REGRESSION and result.probability_calibration:
        raise ValueError("probability calibration is not available for regression")
    return result
