"""Strict matrix boundary between shared examples and algorithm adapters."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from mike_trade.modeling.contracts import LearningTask, TrainingExample


class FeatureSchemaMismatch(ValueError):
    """Raised before inference/training when exact feature order is incompatible."""


def label_text(value: object) -> str:
    if isinstance(value, bool):
        return str(int(value))
    return str(value)


@dataclass(frozen=True, slots=True)
class MatrixDataset:
    features: np.ndarray[Any, Any]
    targets: np.ndarray[Any, Any]
    feature_names: tuple[str, ...]
    symbols: tuple[str, ...] = ()
    times: tuple[datetime, ...] = ()
    weights: np.ndarray[Any, Any] | None = None

    def __post_init__(self) -> None:
        if self.features.ndim != 2 or self.targets.ndim != 1:
            raise ValueError("features must be 2D and targets must be 1D")
        rows, columns = self.features.shape
        if rows < 1 or columns < 1 or len(self.targets) != rows:
            raise ValueError("matrix must be non-empty with one target per row")
        if len(self.feature_names) != columns or len(set(self.feature_names)) != columns:
            raise ValueError("feature_names must uniquely match matrix columns")
        if not np.isfinite(self.features).all() or not np.isfinite(self.targets).all():
            raise ValueError("training matrices must contain only finite values")
        if self.symbols and len(self.symbols) != rows:
            raise ValueError("symbols must be absent or match matrix rows")
        if self.times and len(self.times) != rows:
            raise ValueError("times must be absent or match matrix rows")
        if self.weights is not None:
            if self.weights.ndim != 1 or len(self.weights) != rows:
                raise ValueError("weights must contain one value per row")
            if not np.isfinite(self.weights).all() or np.any(self.weights <= 0):
                raise ValueError("weights must be positive finite values")

    @property
    def row_count(self) -> int:
        return int(self.features.shape[0])


def encode_targets(
    examples: Sequence[TrainingExample],
    task: LearningTask,
    class_labels: tuple[str, ...],
) -> np.ndarray[Any, Any]:
    if task is LearningTask.REGRESSION:
        return np.asarray([float(item.label.value) for item in examples], dtype=np.float64)
    lookup = {value: index for index, value in enumerate(class_labels)}
    try:
        return np.asarray(
            [lookup[label_text(item.label.value)] for item in examples], dtype=np.int32
        )
    except KeyError as exc:
        raise ValueError(f"label {exc.args[0]!r} is absent from class_labels") from None


def matrix_from_examples(
    examples: Iterable[TrainingExample],
    *,
    source_feature_names: tuple[str, ...],
    selected_feature_names: tuple[str, ...],
    task: LearningTask,
    class_labels: tuple[str, ...] = (),
) -> MatrixDataset:
    rows = tuple(examples)
    if not rows:
        raise ValueError("cannot build an empty training matrix")
    expected = tuple(name for name in source_feature_names if name in selected_feature_names)
    if expected != selected_feature_names or len(expected) != len(set(expected)):
        raise FeatureSchemaMismatch(
            "selected features must be unique and preserve the source schema order"
        )
    values: list[list[float]] = []
    for item in rows:
        if tuple(item.features.values) != source_feature_names:
            raise FeatureSchemaMismatch("training feature vector order does not match schema")
        values.append([item.features.values[name] for name in selected_feature_names])
    matrix = np.asarray(values, dtype=np.float32)
    return MatrixDataset(
        matrix,
        encode_targets(rows, task, class_labels),
        selected_feature_names,
        tuple(item.features.symbol for item in rows),
        tuple(item.features.as_of for item in rows),
    )


def balanced_weights(targets: np.ndarray[Any, Any]) -> np.ndarray[Any, Any]:
    labels, counts = np.unique(targets, return_counts=True)
    total = len(targets)
    by_label = {
        int(label): total / (len(labels) * int(count))
        for label, count in zip(labels, counts, strict=True)
    }
    return np.asarray([by_label[int(value)] for value in targets], dtype=np.float32)


class DiskMatrixWriter:
    """Append examples in bounded symbol batches to disk-backed numeric arrays.

    Metadata is optional and normally retained only for validation/calibration matrices.
    """

    def __init__(
        self,
        root: str | Path,
        feature_names: tuple[str, ...],
        *,
        task: LearningTask,
        class_labels: tuple[str, ...] = (),
        retain_metadata: bool = False,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.feature_names = feature_names
        self.task = task
        self.class_labels = class_labels
        self.retain_metadata = retain_metadata
        self.feature_path = self.root / "features.f32"
        self.target_path = self.root / "targets.f64"
        self.metadata_path = self.root / "metadata.jsonl"
        self._rows = 0

    def append(self, examples: Sequence[TrainingExample]) -> None:
        if not examples:
            return
        values = []
        targets = []
        lookup = {value: index for index, value in enumerate(self.class_labels)}
        for item in examples:
            missing = [name for name in self.feature_names if name not in item.features.values]
            if missing:
                raise FeatureSchemaMismatch(f"missing selected features: {missing}")
            values.append([item.features.values[name] for name in self.feature_names])
            targets.append(
                float(item.label.value)
                if self.task is LearningTask.REGRESSION
                else float(lookup[label_text(item.label.value)])
            )
        with self.feature_path.open("ab") as stream:
            np.asarray(values, dtype=np.float32).tofile(stream)
        with self.target_path.open("ab") as stream:
            np.asarray(targets, dtype=np.float64).tofile(stream)
        if self.retain_metadata:
            with self.metadata_path.open("a", encoding="utf-8") as stream:
                for item in examples:
                    stream.write(
                        json.dumps(
                            {
                                "symbol": item.features.symbol,
                                "as_of": item.features.as_of.isoformat(),
                                "example_metadata": dict(item.metadata),
                            },
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
        self._rows += len(examples)

    def finish(self) -> MatrixDataset:
        if self._rows < 1:
            raise ValueError("cannot finish an empty disk matrix")
        expected_feature_bytes = self._rows * len(self.feature_names) * 4
        expected_target_bytes = self._rows * 8
        if self.feature_path.stat().st_size != expected_feature_bytes:
            raise ValueError("disk feature matrix size mismatch")
        if self.target_path.stat().st_size != expected_target_bytes:
            raise ValueError("disk target matrix size mismatch")
        features = np.memmap(
            self.feature_path,
            dtype=np.float32,
            mode="r",
            shape=(self._rows, len(self.feature_names)),
        )
        targets = np.memmap(self.target_path, dtype=np.float64, mode="r", shape=(self._rows,))
        symbols: tuple[str, ...] = ()
        times: tuple[datetime, ...] = ()
        if self.retain_metadata:
            records = [json.loads(line) for line in self.metadata_path.read_text().splitlines()]
            symbols = tuple(str(item["symbol"]) for item in records)
            times = tuple(datetime.fromisoformat(item["as_of"]) for item in records)
        return MatrixDataset(features, targets, self.feature_names, symbols, times)
