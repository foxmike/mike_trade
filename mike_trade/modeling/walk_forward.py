"""Purged, embargoed walk-forward validation and out-of-fold calibration evaluation.

Every fold is trained exactly like the model that would be saved (``ModelTrainingService.fit``),
on data that ends one embargo before the fold's validation window, with labels required to
resolve before their own partition ends — so neither features nor labels can cross into the
validation window, and the final holdout is rejected while the ranges are built.  Features and
labels for all folds are produced in one pass over the data.  Nothing is written to the model
repository except one immutable JSON summary; no fold model is ever eligible for trading.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from mike_trade.modeling.config import ModelExperimentConfig
from mike_trade.modeling.training import (
    ModelTrainingService,
    _range_mapping,
    prepared_counts,
)

__all__ = ["run_walk_forward", "walk_forward_path"]

#: Metrics summarized across folds when present (all derived from held-out fold labels).
_SUMMARY_METRICS = (
    "log_loss",
    "brier_score",
    "accuracy",
    "macro_precision",
    "macro_recall",
    "calibration_error",
    "expected_calibration_error",
    "roc_auc",
    "rmse",
    "mae",
    "r2",
)


def walk_forward_path(config: ModelExperimentConfig) -> Path:
    return (
        Path(config.model_root)
        / "walk_forward"
        / config.experiment_id
        / f"{config.config_sha256}.json"
    )


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _summary(folds: list[dict[str, Any]], key_path: str) -> dict[str, float] | None:
    values = [
        number for fold in folds if (number := _number(fold["metrics"].get(key_path))) is not None
    ]
    if not values:
        return None
    return {
        "mean": statistics.fmean(values),
        "min": min(values),
        "max": max(values),
        "stdev": statistics.pstdev(values),
        "folds": float(len(values)),
    }


def run_walk_forward(
    config: ModelExperimentConfig,
    *,
    dataset_ids: Mapping[str, str] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Run all configured folds and persist an immutable summary; return (path, payload)."""
    if config.walk_forward is None:
        raise ValueError("the experiment configuration has no walk_forward section")
    fold_ranges = config.walk_forward.fold_ranges(config)
    service = ModelTrainingService(config)
    folds: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="mike-trade-walk-forward-") as directory:
        prepared_sets = service.prepare_many(directory, fold_ranges, dataset_ids=dataset_ids)
        selection = next(iter(prepared_sets.values())).selection
        for name, ranges in fold_ranges.items():
            prepared = prepared_sets[name]
            fitted = service.fit(replace(config, ranges=ranges), prepared)
            evaluation = json.loads(fitted.evaluation.to_json_bytes())
            folds.append(
                {
                    "fold": name,
                    "ranges": _range_mapping(ranges),
                    "counts": prepared_counts(prepared, config.class_labels),
                    "best_iteration": fitted.result.best_iteration,
                    "search_trials": [dict(item) for item in fitted.trials],
                    "metrics": evaluation["metrics"],
                    "baseline": evaluation.get("baseline"),
                    "calibration": evaluation.get("calibration"),
                    "per_symbol_samples": len(evaluation.get("per_symbol", {})),
                }
            )
    payload: dict[str, Any] = {
        "schema": "mike_trade_walk_forward",
        "schema_version": 1,
        "experiment_id": config.experiment_id,
        "config_sha256": config.config_sha256,
        "algorithm": config.algorithm.value,
        "role": config.role.value,
        "feed": selection.feed,
        "timeframe": selection.timeframe,
        "universe_id": selection.universe_id,
        "dataset_sources": [
            {"symbol": item.symbol, "dataset_id": item.dataset_id, "sha256": item.sha256}
            for item in selection.sources
        ],
        "method": (
            "expanding purged walk-forward; fit/early-stopping/calibration/validation are "
            "disjoint, separated by the embargo, every label resolves inside its own "
            "partition, and the reserved final holdout is rejected"
        ),
        "embargo_minutes": int(config.ranges.embargo.total_seconds() // 60),
        "folds": folds,
        "summary": {
            name: summary
            for name in _SUMMARY_METRICS
            if (summary := _summary(folds, name)) is not None
        },
        "disclaimer": ("Held-out predictive metrics; they do not establish trading profitability."),
    }
    text = json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n"
    path = walk_forward_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise FileExistsError(f"immutable walk-forward summary already exists: {path}")
    else:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    payload["_sha256"] = hashlib.sha256(text.encode()).hexdigest()
    return path, payload
