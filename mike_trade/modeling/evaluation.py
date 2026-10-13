"""Small deterministic evaluators shared by future model-library adapters."""

from __future__ import annotations

import math
from collections.abc import Iterable
from decimal import Decimal

from mike_trade.modeling.contracts import (
    EvaluationReport,
    ModelPrediction,
    PredictionKind,
    SupervisedLabel,
)


def _pairs(
    predictions: Iterable[ModelPrediction], labels: Iterable[SupervisedLabel]
) -> tuple[tuple[ModelPrediction, SupervisedLabel], ...]:
    result = tuple(zip(predictions, labels, strict=True))
    for prediction, label in result:
        if prediction.symbol != label.symbol:
            raise ValueError("prediction and label symbols differ")
        if prediction.as_of > label.entry_time:
            raise ValueError("prediction was not available by label entry")
    return result


class ClassificationEvaluator:
    def evaluate(
        self,
        predictions: Iterable[ModelPrediction],
        labels: Iterable[SupervisedLabel],
    ) -> EvaluationReport:
        pairs = _pairs(predictions, labels)
        if not pairs:
            return EvaluationReport({}, 0)
        correct = 0
        losses: list[float] = []
        for prediction, label in pairs:
            if prediction.kind is not PredictionKind.CLASS:
                raise ValueError("classification evaluator requires class predictions")
            target = str(label.value)
            correct += int(prediction.predicted_class == target)
            if prediction.probabilities:
                probability = prediction.probabilities.get(target)
                if probability is None:
                    raise ValueError("target class is absent from probabilities")
                losses.append(-math.log(max(probability, 1e-15)))
        metrics = {"accuracy": Decimal(correct) / Decimal(len(pairs))}
        if losses:
            metrics["log_loss"] = Decimal(str(math.fsum(losses) / len(losses)))
        return EvaluationReport(metrics, len(pairs))


class RegressionEvaluator:
    def evaluate(
        self,
        predictions: Iterable[ModelPrediction],
        labels: Iterable[SupervisedLabel],
    ) -> EvaluationReport:
        pairs = _pairs(predictions, labels)
        if not pairs:
            return EvaluationReport({}, 0)
        errors: list[float] = []
        for prediction, label in pairs:
            if prediction.kind is not PredictionKind.REGRESSION or prediction.value is None:
                raise ValueError("regression evaluator requires regression predictions")
            errors.append(prediction.value - float(label.value))
        mae = math.fsum(abs(value) for value in errors) / len(errors)
        rmse = math.sqrt(math.fsum(value * value for value in errors) / len(errors))
        return EvaluationReport({"mae": Decimal(str(mae)), "rmse": Decimal(str(rmse))}, len(pairs))
