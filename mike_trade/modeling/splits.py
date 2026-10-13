"""Chronological and purged walk-forward validation with a locked final holdout."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from mike_trade.modeling.contracts import TrainingExample, sha256, utc

DEVELOPMENT_START = datetime(2025, 10, 9, tzinfo=UTC)
FINAL_HOLDOUT_START = datetime(2026, 9, 21, tzinfo=UTC)
FINAL_HOLDOUT_END = datetime(2026, 10, 9, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class TimeRange:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", utc(self.start, "range start"))
        object.__setattr__(self, "end", utc(self.end, "range end"))
        if self.end <= self.start:
            raise ValueError("time range must be non-empty")

    def contains(self, value: datetime) -> bool:
        moment = utc(value, "moment")
        return self.start <= moment < self.end

    def overlaps(self, other: TimeRange) -> bool:
        return self.start < other.end and self.end > other.start


FINAL_HOLDOUT = TimeRange(FINAL_HOLDOUT_START, FINAL_HOLDOUT_END)


class ExperimentPurpose(StrEnum):
    DEVELOPMENT = "DEVELOPMENT"
    FINAL_HOLDOUT = "FINAL_HOLDOUT"


class FoldPartition(StrEnum):
    TRAIN = "TRAIN"
    VALIDATION = "VALIDATION"


@dataclass(frozen=True, slots=True)
class FinalHoldoutAuthorization:
    """Explicit operator authorization locked to an already-frozen experiment."""

    token: str
    experiment_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token.strip():
            raise ValueError("holdout authorization token is required")
        sha256(self.experiment_hash, "experiment_hash")


class HoldoutAccessError(ValueError):
    pass


def authorize_range(
    requested: TimeRange,
    purpose: ExperimentPurpose,
    *,
    experiment_hash: str,
    authorization: FinalHoldoutAuthorization | None = None,
) -> None:
    """Guard every modeling phase; only an exact, authorized final evaluation may use holdout."""
    if requested.start < DEVELOPMENT_START:
        raise HoldoutAccessError("model-development data starts at 2025-10-09")
    overlap = requested.overlaps(FINAL_HOLDOUT)
    if purpose is ExperimentPurpose.DEVELOPMENT:
        if overlap:
            raise HoldoutAccessError(
                "development, tuning, fitting, and calibration exclude holdout"
            )
        return
    if requested != FINAL_HOLDOUT:
        raise HoldoutAccessError("final holdout evaluation must use the exact reserved window")
    if authorization is None:
        raise HoldoutAccessError("final holdout evaluation requires explicit authorization")
    if authorization.experiment_hash != experiment_hash:
        raise HoldoutAccessError("holdout authorization does not match the frozen experiment")


@dataclass(frozen=True, slots=True)
class ValidationFold:
    fold_id: str
    train: tuple[TrainingExample, ...]
    validation: tuple[TrainingExample, ...]
    train_range: TimeRange
    validation_range: TimeRange
    embargo: timedelta


@dataclass(frozen=True, slots=True)
class FoldAssignment:
    """One streaming assignment; examples may train multiple later expanding folds."""

    fold_id: str
    partition: FoldPartition
    example: TrainingExample


class PurgedWalkForwardSplitter:
    """Expanding-window folds purged globally by label interval, not independently by symbol."""

    def __init__(
        self,
        validation_ranges: Sequence[TimeRange],
        *,
        embargo: timedelta = timedelta(0),
        development_start: datetime = DEVELOPMENT_START,
    ) -> None:
        if not validation_ranges:
            raise ValueError("at least one validation range is required")
        if embargo < timedelta(0):
            raise ValueError("embargo must be non-negative")
        ordered = tuple(sorted(validation_ranges, key=lambda value: value.start))
        if any(right.start < left.end for left, right in zip(ordered, ordered[1:], strict=False)):
            raise ValueError("validation ranges must not overlap")
        if any(item.overlaps(FINAL_HOLDOUT) for item in ordered):
            raise HoldoutAccessError("walk-forward validation cannot inspect the final holdout")
        self.validation_ranges = ordered
        self.embargo = embargo
        self.development_start = utc(development_start, "development_start")

    def assign(self, examples: Iterable[TrainingExample]) -> Iterator[FoldAssignment]:
        """Assign a chronological stream without materializing the pooled universe."""
        previous: tuple[datetime, str] | None = None
        for item in examples:
            key = (item.features.as_of, item.features.symbol)
            if previous is not None and key < previous:
                raise ValueError("examples must be chronological for streaming fold assignment")
            previous = key
            for number, validation_range in enumerate(self.validation_ranges, start=1):
                fold_id = f"fold_{number}"
                purge_boundary = validation_range.start - self.embargo
                if (
                    self.development_start <= item.features.as_of < purge_boundary
                    and item.label.outcome_time <= purge_boundary
                ):
                    yield FoldAssignment(fold_id, FoldPartition.TRAIN, item)
                elif (
                    validation_range.contains(item.features.as_of)
                    and item.label.outcome_time <= validation_range.end
                ):
                    yield FoldAssignment(fold_id, FoldPartition.VALIDATION, item)

    def split(self, examples: Iterable[TrainingExample]) -> Iterator[ValidationFold]:
        """Materialized convenience for small studies/tests; production should use ``assign``."""
        ordered = tuple(
            sorted(examples, key=lambda item: (item.features.as_of, item.features.symbol))
        )
        if not ordered:
            return
        for number, validation_range in enumerate(self.validation_ranges, start=1):
            purge_boundary = validation_range.start - self.embargo
            # Global temporal purge: a label from any symbol must be resolved before the boundary.
            train = tuple(
                item
                for item in ordered
                if self.development_start <= item.features.as_of < purge_boundary
                and item.label.outcome_time <= purge_boundary
            )
            validation = tuple(
                item
                for item in ordered
                if validation_range.contains(item.features.as_of)
                and item.label.outcome_time <= validation_range.end
            )
            if not train or not validation:
                raise ValueError(f"fold {number} has an empty train or validation partition")
            train_range = TimeRange(
                min(item.features.as_of for item in train),
                max(item.label.outcome_time for item in train) + timedelta(microseconds=1),
            )
            yield ValidationFold(
                f"fold_{number}",
                train,
                validation,
                train_range,
                validation_range,
                self.embargo,
            )
