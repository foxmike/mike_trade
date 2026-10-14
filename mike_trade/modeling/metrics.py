"""Predictive model evaluation; no metric here is a profitability claim."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC
from typing import Any

import numpy as np

from mike_trade.modeling.contracts import LearningTask
from mike_trade.modeling.matrix import MatrixDataset


def _float(value: float) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("evaluation metrics must be finite")
    return result


@dataclass(frozen=True, slots=True)
class PredictiveEvaluation:
    task: LearningTask
    sample_count: int
    metrics: Mapping[str, Any]
    per_symbol: Mapping[str, Any]
    per_period: Mapping[str, Any]
    prediction_distribution: Mapping[str, Any]
    baseline: Mapping[str, Any]
    calibration: tuple[Mapping[str, Any], ...] = ()
    confusion_matrix: tuple[tuple[int, ...], ...] = ()
    class_labels: tuple[str, ...] = ()
    #: One-vs-rest reliability per class: predicted probability bins vs observed frequency.
    class_calibration: Mapping[str, Any] | None = None
    #: Outcome mix by decile of the favorable-class probability (only when requested).
    favorable_deciles: Mapping[str, Any] | None = None
    disclaimer: str = (
        "Predictive metrics describe held-out labels; they do not establish trading profitability."
    )

    def to_json_bytes(self) -> bytes:
        payload = {
            "task": self.task.value,
            "sample_count": self.sample_count,
            "metrics": self.metrics,
            "per_symbol": self.per_symbol,
            "per_period": self.per_period,
            "prediction_distribution": self.prediction_distribution,
            "baseline": self.baseline,
            "calibration": self.calibration,
            "confusion_matrix": self.confusion_matrix,
            "class_labels": self.class_labels,
            "disclaimer": self.disclaimer,
        }
        if self.class_calibration is not None:
            payload["class_calibration"] = self.class_calibration
        if self.favorable_deciles is not None:
            payload["favorable_deciles"] = self.favorable_deciles
        return (
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode("utf-8")


def _classification_core(
    targets: np.ndarray[Any, Any], probabilities: np.ndarray[Any, Any]
) -> dict[str, Any]:
    labels = np.asarray(targets, dtype=np.int64)
    probs = np.asarray(probabilities, dtype=np.float64)
    if probs.ndim != 2 or len(labels) != len(probs) or probs.shape[1] < 2:
        raise ValueError("classification evaluation requires probability rows")
    if not np.isfinite(probs).all() or np.any(probs < 0) or np.any(probs > 1):
        raise ValueError("classification probabilities must be finite and in [0, 1]")
    if not np.allclose(np.sum(probs, axis=1), 1.0, rtol=0, atol=1e-8):
        raise ValueError("classification probability rows must sum to one")
    classes = probs.shape[1]
    if np.any(labels < 0) or np.any(labels >= classes):
        raise ValueError("classification target is outside probability columns")
    predicted = np.argmax(probs, axis=1)
    selected = np.clip(probs[np.arange(len(labels)), labels], 1e-15, 1.0)
    log_loss = -float(np.mean(np.log(selected)))
    one_hot = np.eye(classes)[labels]
    brier = float(np.mean(np.sum((probs - one_hot) ** 2, axis=1)))
    confusion = np.zeros((classes, classes), dtype=np.int64)
    for actual, guess in zip(labels, predicted, strict=True):
        confusion[int(actual), int(guess)] += 1
    precision: list[float] = []
    recall: list[float] = []
    per_class: dict[str, Any] = {}
    for index in range(classes):
        true_positive = int(confusion[index, index])
        predicted_positive = int(np.sum(confusion[:, index]))
        actual_positive = int(np.sum(confusion[index, :]))
        p = true_positive / predicted_positive if predicted_positive else 0.0
        r = true_positive / actual_positive if actual_positive else 0.0
        precision.append(p)
        recall.append(r)
        per_class[str(index)] = {
            "precision": p,
            "recall": r,
            "support": actual_positive,
        }
    result: dict[str, Any] = {
        "log_loss": log_loss,
        "brier_score": brier,
        "accuracy": float(np.mean(predicted == labels)),
        "macro_precision": float(np.mean(precision)),
        "macro_recall": float(np.mean(recall)),
        "per_class": per_class,
        "confusion": confusion,
        "predicted": predicted,
    }
    if classes == 2 and len(np.unique(labels)) == 2:
        result["roc_auc"] = _binary_auc(labels, probs[:, 1])
    return result


def _binary_auc(labels: np.ndarray[Any, Any], scores: np.ndarray[Any, Any]) -> float:
    order = np.argsort(scores, kind="mergesort")
    sorted_scores = scores[order]
    ranks = np.empty(len(scores), dtype=np.float64)
    left = 0
    while left < len(scores):
        right = left + 1
        while right < len(scores) and sorted_scores[right] == sorted_scores[left]:
            right += 1
        ranks[order[left:right]] = (left + 1 + right) / 2.0
        left = right
    positives = labels == 1
    positive_count = int(np.sum(positives))
    negative_count = len(labels) - positive_count
    return float(
        (np.sum(ranks[positives]) - positive_count * (positive_count + 1) / 2)
        / (positive_count * negative_count)
    )


def _calibration_bins(
    targets: np.ndarray[Any, Any], probabilities: np.ndarray[Any, Any], bins: int = 10
) -> tuple[tuple[dict[str, Any], ...], float]:
    confidence = np.max(probabilities, axis=1)
    correct = np.argmax(probabilities, axis=1) == targets
    rows = []
    expected_error = 0.0
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        mask = (confidence >= low) & (
            confidence <= high if index == bins - 1 else confidence < high
        )
        count = int(np.sum(mask))
        if not count:
            continue
        mean_confidence = float(np.mean(confidence[mask]))
        accuracy = float(np.mean(correct[mask]))
        expected_error += count / len(targets) * abs(mean_confidence - accuracy)
        rows.append(
            {
                "lower": low,
                "upper": high,
                "count": count,
                "mean_probability": mean_confidence,
                "observed_accuracy": accuracy,
            }
        )
    return tuple(rows), expected_error


def _class_calibration(
    targets: np.ndarray[Any, Any],
    probabilities: np.ndarray[Any, Any],
    class_labels: tuple[str, ...],
    bins: int = 10,
) -> dict[str, Any]:
    """One-vs-rest reliability: does a predicted P(class) match how often the class occurs?"""
    result: dict[str, Any] = {}
    labels = np.asarray(targets, dtype=np.int64)
    for index, label in enumerate(class_labels):
        predicted = probabilities[:, index]
        observed = labels == index
        rows = []
        error = 0.0
        for number in range(bins):
            low, high = number / bins, (number + 1) / bins
            mask = (predicted >= low) & (
                predicted <= high if number == bins - 1 else predicted < high
            )
            count = int(np.sum(mask))
            if not count:
                continue
            mean_predicted = float(np.mean(predicted[mask]))
            frequency = float(np.mean(observed[mask]))
            error += count / len(labels) * abs(mean_predicted - frequency)
            rows.append(
                {
                    "lower": low,
                    "upper": high,
                    "count": count,
                    "mean_predicted": mean_predicted,
                    "observed_frequency": frequency,
                }
            )
        result[label] = {"bins": rows, "expected_calibration_error": error}
    return result


def favorable_decile_analysis(
    targets: np.ndarray[Any, Any],
    probabilities: np.ndarray[Any, Any],
    class_labels: tuple[str, ...],
    favorable_class: str,
    deciles: int = 10,
    barrier_returns: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Do higher predicted P(favorable) identify genuinely higher rates of favorable outcomes?

    Rows are ranked by P(favorable) (ties broken by row order, so groups are equal-sized) and cut into
    ``deciles`` groups; decile ``deciles`` holds the HIGHEST probabilities.  For each group the
    observed rate of every class is reported next to the mean predicted probability.  The optional
    ``barrier_returns`` (class label -> return) yield a barrier-implied mean return per prediction
    BEFORE costs and ignoring where timeouts end; it is a descriptive label statistic, not a
    profitability claim.
    """
    labels = np.asarray(targets, dtype=np.int64)
    probs = np.asarray(probabilities, dtype=np.float64)
    column = class_labels.index(favorable_class)
    score = probs[:, column]
    order = np.argsort(score, kind="mergesort")
    groups = np.array_split(order, deciles)
    base_rate = float(np.mean(labels == column))
    rows: list[dict[str, Any]] = []
    for number, members in enumerate(groups, start=1):
        if len(members) == 0:
            continue
        subset = labels[members]
        rates = {
            label: float(np.mean(subset == index)) for index, label in enumerate(class_labels)
        }
        row: dict[str, Any] = {
            "decile": number,
            "count": int(len(members)),
            "min_probability": float(score[members].min()),
            "max_probability": float(score[members].max()),
            "mean_probability": float(score[members].mean()),
            "observed_rates": rates,
            "favorable_rate": rates[favorable_class],
            "favorable_lift_vs_base_rate": (
                rates[favorable_class] / base_rate if base_rate > 0 else None
            ),
        }
        if barrier_returns is not None:
            row["barrier_implied_mean_return_before_costs"] = float(
                sum(rates[label] * value for label, value in barrier_returns.items())
            )
        rows.append(row)
    rates_by_decile = np.asarray([row["favorable_rate"] for row in rows])
    positions = np.arange(len(rows), dtype=np.float64)
    if len(rows) > 2 and np.std(rates_by_decile) > 0:
        monotonic = float(np.corrcoef(positions, rates_by_decile)[0, 1])
    else:
        monotonic = None
    auc = None
    binary = (labels == column).astype(np.int64)
    if 0 < int(binary.sum()) < len(binary):
        auc = _binary_auc(binary, score)
    return {
        "favorable_class": favorable_class,
        "deciles": rows,
        "base_favorable_rate": base_rate,
        "ordering": "decile 1 = lowest predicted P(favorable); the last decile = highest",
        "favorable_probability_quantiles": {
            str(q): float(np.quantile(score, q)) for q in (0, 0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99, 1)
        },
        "correlation_decile_vs_favorable_rate": monotonic,
        "favorable_vs_rest_roc_auc": auc,
        "top_decile_lift_vs_base_rate": rows[-1]["favorable_lift_vs_base_rate"] if rows else None,
        "barrier_returns": None if barrier_returns is None else dict(barrier_returns),
        "disclaimer": (
            "Outcome rates are label statistics on held-out rows; they are not trading results."
        ),
    }


def evaluate_classification(
    dataset: MatrixDataset,
    probabilities: np.ndarray[Any, Any],
    class_labels: tuple[str, ...],
    *,
    training_targets: np.ndarray[Any, Any] | None = None,
    favorable_class: str | None = None,
    deciles: int = 10,
    barrier_returns: Mapping[str, float] | None = None,
) -> PredictiveEvaluation:
    if len(class_labels) != probabilities.shape[1]:
        raise ValueError("class labels must match probability columns")
    core = _classification_core(dataset.targets, probabilities)
    calibration, ece = _calibration_bins(dataset.targets, probabilities)
    metrics = {key: value for key, value in core.items() if key not in {"confusion", "predicted"}}
    per_class = metrics.pop("per_class")
    metrics["per_class"] = {class_labels[int(index)]: values for index, values in per_class.items()}
    metrics["expected_calibration_error"] = ece
    per_symbol = _grouped_classification(dataset, probabilities, dataset.symbols)
    periods = tuple(value.astimezone(UTC).strftime("%Y-%m") for value in dataset.times)
    per_period = _grouped_classification(dataset, probabilities, periods)
    predicted = np.asarray(core["predicted"])
    predicted_counts = Counter(map(int, predicted))
    max_probability = np.max(probabilities, axis=1)
    distribution = {
        "predicted_class_counts": {
            class_labels[index]: int(predicted_counts.get(index, 0))
            for index in range(len(class_labels))
        },
        "max_probability_quantiles": {
            str(q): float(np.quantile(max_probability, q)) for q in (0, 0.25, 0.5, 0.75, 1)
        },
        "mean_class_probability": {
            label: float(np.mean(probabilities[:, index]))
            for index, label in enumerate(class_labels)
        },
    }
    baseline_targets = dataset.targets if training_targets is None else training_targets
    counts = np.bincount(np.asarray(baseline_targets, dtype=np.int64), minlength=len(class_labels))
    frequencies = counts / np.sum(counts)
    baseline_probabilities = np.tile(frequencies, (dataset.row_count, 1))
    baseline_core = _classification_core(dataset.targets, baseline_probabilities)
    baseline = {
        "kind": "training_class_frequency",
        "probabilities": {
            label: float(frequencies[index]) for index, label in enumerate(class_labels)
        },
        "log_loss": baseline_core["log_loss"],
        "brier_score": baseline_core["brier_score"],
        "model_log_loss_improvement": baseline_core["log_loss"] - core["log_loss"],
    }
    confusion = tuple(tuple(map(int, row)) for row in np.asarray(core["confusion"]))
    return PredictiveEvaluation(
        LearningTask.BINARY_CLASSIFICATION
        if len(class_labels) == 2
        else LearningTask.MULTICLASS_CLASSIFICATION,
        dataset.row_count,
        metrics,
        per_symbol,
        per_period,
        distribution,
        baseline,
        calibration,
        confusion,
        class_labels,
        _class_calibration(dataset.targets, probabilities, class_labels),
        None
        if favorable_class is None
        else favorable_decile_analysis(
            dataset.targets, probabilities, class_labels, favorable_class, deciles, barrier_returns
        ),
    )


def _grouped_classification(
    dataset: MatrixDataset,
    probabilities: np.ndarray[Any, Any],
    groups: tuple[str, ...],
) -> dict[str, Any]:
    if not groups:
        return {}
    indexes: dict[str, list[int]] = defaultdict(list)
    for index, group in enumerate(groups):
        indexes[group].append(index)
    result = {}
    for group, positions in sorted(indexes.items()):
        selected = np.asarray(positions)
        core = _classification_core(dataset.targets[selected], probabilities[selected])
        result[group] = {
            "sample_count": len(positions),
            "log_loss": core["log_loss"],
            "brier_score": core["brier_score"],
            "accuracy": core["accuracy"],
        }
    return result


def evaluate_regression(
    dataset: MatrixDataset,
    predictions: np.ndarray[Any, Any],
    *,
    training_targets: np.ndarray[Any, Any] | None = None,
) -> PredictiveEvaluation:
    values = np.asarray(predictions, dtype=np.float64).reshape(-1)
    if len(values) != dataset.row_count or not np.isfinite(values).all():
        raise ValueError("regression predictions must be finite and match rows")
    targets = np.asarray(dataset.targets, dtype=np.float64)
    errors = values - targets
    metrics = _regression_core(targets, values)
    per_symbol = _grouped_regression(dataset, values, dataset.symbols)
    periods = tuple(value.astimezone(UTC).strftime("%Y-%m") for value in dataset.times)
    per_period = _grouped_regression(dataset, values, periods)
    baseline_source = targets if training_targets is None else training_targets
    baseline_value = float(np.mean(baseline_source))
    baseline_predictions = np.full(len(targets), baseline_value)
    baseline_metrics = _regression_core(targets, baseline_predictions)
    baseline = {
        "kind": "training_mean",
        "value": baseline_value,
        **baseline_metrics,
        "model_rmse_improvement": baseline_metrics["rmse"] - metrics["rmse"],
    }
    distribution = {
        "prediction_quantiles": {
            str(q): float(np.quantile(values, q)) for q in (0, 0.25, 0.5, 0.75, 1)
        },
        "mean": float(np.mean(values)),
        "standard_deviation": float(np.std(values)),
        "mean_error": float(np.mean(errors)),
    }
    return PredictiveEvaluation(
        LearningTask.REGRESSION,
        dataset.row_count,
        metrics,
        per_symbol,
        per_period,
        distribution,
        baseline,
    )


def _regression_core(
    targets: np.ndarray[Any, Any], predictions: np.ndarray[Any, Any]
) -> dict[str, float]:
    errors = predictions - targets
    denominator = float(np.sum((targets - np.mean(targets)) ** 2))
    return {
        "mae": _float(np.mean(np.abs(errors))),
        "rmse": _float(np.sqrt(np.mean(errors**2))),
        "r_squared": _float(1 - float(np.sum(errors**2)) / denominator) if denominator > 0 else 0.0,
    }


def _grouped_regression(
    dataset: MatrixDataset, predictions: np.ndarray[Any, Any], groups: tuple[str, ...]
) -> dict[str, Any]:
    if not groups:
        return {}
    indexes: dict[str, list[int]] = defaultdict(list)
    for index, group in enumerate(groups):
        indexes[group].append(index)
    return {
        group: {
            "sample_count": len(positions),
            **_regression_core(
                dataset.targets[np.asarray(positions)], predictions[np.asarray(positions)]
            ),
        }
        for group, positions in sorted(indexes.items())
    }


def permutation_importance(
    dataset: MatrixDataset,
    predict: Callable[[MatrixDataset], np.ndarray[Any, Any]],
    *,
    task: LearningTask,
    seed: int,
) -> dict[str, float]:
    """Held-out permutation impact on log loss or MSE; descriptive, never causal."""
    rng = np.random.default_rng(seed)
    baseline_predictions = predict(dataset)
    if task is LearningTask.REGRESSION:
        baseline = float(np.mean((baseline_predictions.reshape(-1) - dataset.targets) ** 2))
    else:
        baseline = float(_classification_core(dataset.targets, baseline_predictions)["log_loss"])
    result = {}
    for index, name in enumerate(dataset.feature_names):
        copied = np.array(dataset.features, copy=True)
        copied[:, index] = copied[rng.permutation(dataset.row_count), index]
        permuted = MatrixDataset(
            copied,
            dataset.targets,
            dataset.feature_names,
            dataset.symbols,
            dataset.times,
            dataset.weights,
        )
        predictions = predict(permuted)
        if task is LearningTask.REGRESSION:
            score = float(np.mean((predictions.reshape(-1) - dataset.targets) ** 2))
        else:
            score = float(_classification_core(dataset.targets, predictions)["log_loss"])
        result[name] = score - baseline
    return result
