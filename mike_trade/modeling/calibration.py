"""Deterministic probability calibration fitted only on a separated partition."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import numpy as np


@dataclass(frozen=True, slots=True)
class ProbabilityCalibrator:
    method: str
    parameters: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.method not in {"none", "platt", "temperature"}:
            raise ValueError("unknown calibration method")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    def transform(self, probabilities: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
        values = np.asarray(probabilities, dtype=np.float64)
        if self.method == "none":
            return values
        if self.method == "platt":
            if values.ndim != 2 or values.shape[1] != 2:
                raise ValueError("Platt calibration requires binary probability rows")
            a = float(self.parameters["a"])
            b = float(self.parameters["b"])
            logits = np.log(np.clip(values[:, 1], 1e-12, 1 - 1e-12)) - np.log(
                np.clip(values[:, 0], 1e-12, 1 - 1e-12)
            )
            calibrated = 1.0 / (1.0 + np.exp(-np.clip(a * logits + b, -40, 40)))
            return np.column_stack((1.0 - calibrated, calibrated))
        temperature = float(self.parameters["temperature"])
        logits = np.log(np.clip(values, 1e-12, 1.0)) / temperature
        logits -= np.max(logits, axis=1, keepdims=True)
        exp = np.exp(logits)
        return exp / np.sum(exp, axis=1, keepdims=True)

    def as_mapping(self) -> dict[str, str]:
        return {"method": self.method, **dict(self.parameters)}

    @classmethod
    def from_mapping(cls, value: Mapping[str, str]) -> ProbabilityCalibrator:
        method = value.get("method", "none")
        return cls(method, {key: item for key, item in value.items() if key != "method"})


NO_CALIBRATION = ProbabilityCalibrator("none", {})


def fit_probability_calibrator(
    probabilities: np.ndarray[Any, Any],
    targets: np.ndarray[Any, Any],
) -> ProbabilityCalibrator:
    """Fit Platt scaling for binary output or scalar temperature for multiclass."""
    values = np.asarray(probabilities, dtype=np.float64)
    labels = np.asarray(targets, dtype=np.int32)
    if values.ndim != 2 or values.shape[0] != len(labels) or values.shape[1] < 2:
        raise ValueError("calibration requires class-probability rows and matching labels")
    if len(np.unique(labels)) < 2:
        raise ValueError("calibration partition must contain at least two classes")
    if values.shape[1] == 2:
        logits = np.log(np.clip(values[:, 1], 1e-12, 1 - 1e-12)) - np.log(
            np.clip(values[:, 0], 1e-12, 1 - 1e-12)
        )
        a, b = 1.0, 0.0
        for _ in range(1000):
            predicted = 1.0 / (1.0 + np.exp(-np.clip(a * logits + b, -40, 40)))
            error = predicted - labels
            grad_a = float(np.mean(error * logits))
            grad_b = float(np.mean(error))
            a -= 0.05 * grad_a
            b -= 0.05 * grad_b
            if max(abs(grad_a), abs(grad_b)) < 1e-9:
                break
        return ProbabilityCalibrator("platt", {"a": repr(a), "b": repr(b)})
    best_temperature, best_loss = 1.0, math.inf
    for temperature in np.geomspace(0.25, 4.0, num=121):
        logits = np.log(np.clip(values, 1e-12, 1.0)) / temperature
        logits -= np.max(logits, axis=1, keepdims=True)
        exp = np.exp(logits)
        calibrated = exp / np.sum(exp, axis=1, keepdims=True)
        selected = calibrated[np.arange(len(labels)), labels]
        loss = -float(np.mean(np.log(np.clip(selected, 1e-15, 1))))
        if loss < best_loss:
            best_temperature, best_loss = float(temperature), loss
    return ProbabilityCalibrator("temperature", {"temperature": repr(best_temperature)})
