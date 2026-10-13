"""Point-in-time supervised labels aligned to the backtester's next-bar execution rule."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from mike_trade.modeling.contracts import (
    LabelSchema,
    LearningTask,
    ModelBar,
    SupervisedLabel,
)

_ZERO = Decimal("0")
_BPS = Decimal("10000")


class TripleBarrierOutcome(StrEnum):
    WIN = "WIN"
    LOSS = "LOSS"
    TIMEOUT = "TIMEOUT"


class IntrabarAmbiguityPolicy(StrEnum):
    LOSS = "LOSS"
    SKIP = "SKIP"


@dataclass(frozen=True, slots=True)
class ExecutionCostAssumptions:
    """Deterministic adverse costs applied to hypothetical entry and exit prices."""

    entry_bps: Decimal = _ZERO
    exit_bps: Decimal = _ZERO

    def __post_init__(self) -> None:
        for name in ("entry_bps", "exit_bps"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite non-negative Decimal")

    def entry_fill(self, price: Decimal) -> Decimal:
        return price * (Decimal("1") + self.entry_bps / _BPS)

    def exit_fill(self, price: Decimal) -> Decimal:
        return price * (Decimal("1") - self.exit_bps / _BPS)

    def parameters(self) -> dict[str, str]:
        return {"entry_bps": str(self.entry_bps), "exit_bps": str(self.exit_bps)}


def _same_series(previous: ModelBar, current: ModelBar) -> bool:
    return (
        current.start == previous.end
        and current.session_id == previous.session_id
        and current.symbol == previous.symbol
        and current.feed == previous.feed
        and current.timeframe == previous.timeframe
        and current.adjustment == previous.adjustment
    )


def _entry_bar(signal: ModelBar, candidate: ModelBar) -> bool:
    """A missing, zero-volume, or next-session bar is not treated as a guaranteed fill."""
    return _same_series(signal, candidate) and candidate.volume > 0


@dataclass(frozen=True, slots=True)
class ForwardReturnConfig:
    horizon_bars: int
    task: LearningTask = LearningTask.REGRESSION
    positive_threshold: Decimal = _ZERO
    negative_threshold: Decimal = _ZERO
    costs: ExecutionCostAssumptions = ExecutionCostAssumptions()
    label_id: str = "forward_return"
    version: str = "1.0.0"

    def __post_init__(self) -> None:
        if isinstance(self.horizon_bars, bool) or self.horizon_bars < 1:
            raise ValueError("horizon_bars must be a positive int")
        if self.task not in {
            LearningTask.REGRESSION,
            LearningTask.BINARY_CLASSIFICATION,
            LearningTask.MULTICLASS_CLASSIFICATION,
        }:
            raise ValueError("unsupported forward-return task")
        for name in ("positive_threshold", "negative_threshold"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be a finite non-negative Decimal")


class ForwardReturnLabelGenerator:
    def __init__(self, config: ForwardReturnConfig) -> None:
        self.config = config
        params = {
            "positive_threshold": str(config.positive_threshold),
            "negative_threshold": str(config.negative_threshold),
            "entry_convention": "next_contiguous_session_bar_open",
            **config.costs.parameters(),
        }
        self._schema = LabelSchema(
            config.label_id, config.version, config.task, config.horizon_bars, params
        )

    @property
    def schema(self) -> LabelSchema:
        return self._schema

    def generate(self, bars: Sequence[ModelBar]) -> Iterator[SupervisedLabel]:
        horizon = self.config.horizon_bars
        for index in range(0, len(bars) - horizon):
            signal = bars[index]
            entry = bars[index + 1]
            outcome_bar = bars[index + horizon]
            path = bars[index + 1 : index + horizon + 1]
            if not _entry_bar(signal, entry):
                continue
            if any(
                not _same_series(left, right) for left, right in zip(path, path[1:], strict=False)
            ):
                continue
            entry_price = self.config.costs.entry_fill(entry.open)
            outcome_price = self.config.costs.exit_fill(outcome_bar.close)
            net_return = outcome_price / entry_price - Decimal("1")
            value: Decimal | int
            if self.config.task is LearningTask.REGRESSION:
                value = net_return
            elif self.config.task is LearningTask.BINARY_CLASSIFICATION:
                value = int(net_return > self.config.positive_threshold)
            elif net_return > self.config.positive_threshold:
                value = 1
            elif net_return < -self.config.negative_threshold:
                value = -1
            else:
                value = 0
            yield SupervisedLabel(
                signal.symbol,
                signal.start,
                entry.start,
                entry_price,
                outcome_bar.end,
                outcome_price,
                value,
                net_return,
                self.schema.label_id,
                self.schema.version,
                "FORWARD_RETURN",
            )


@dataclass(frozen=True, slots=True)
class TripleBarrierConfig:
    positive_barrier: Decimal
    negative_barrier: Decimal
    horizon_bars: int
    costs: ExecutionCostAssumptions = ExecutionCostAssumptions()
    ambiguity_policy: IntrabarAmbiguityPolicy = IntrabarAmbiguityPolicy.LOSS
    label_id: str = "triple_barrier"
    version: str = "1.0.0"

    def __post_init__(self) -> None:
        for name in ("positive_barrier", "negative_barrier"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
        if isinstance(self.horizon_bars, bool) or self.horizon_bars < 1:
            raise ValueError("horizon_bars must be a positive int")
        if not isinstance(self.ambiguity_policy, IntrabarAmbiguityPolicy):
            raise ValueError("ambiguity_policy must be IntrabarAmbiguityPolicy")


class TripleBarrierLabelGenerator:
    """Long-side triple barrier labels with pessimistic ambiguous-bar handling."""

    def __init__(self, config: TripleBarrierConfig) -> None:
        self.config = config
        params = {
            "positive_barrier": str(config.positive_barrier),
            "negative_barrier": str(config.negative_barrier),
            "entry_convention": "next_contiguous_session_bar_open",
            "ambiguity_policy": config.ambiguity_policy.value,
            **config.costs.parameters(),
        }
        self._schema = LabelSchema(
            config.label_id,
            config.version,
            LearningTask.MULTICLASS_CLASSIFICATION,
            config.horizon_bars,
            params,
        )

    @property
    def schema(self) -> LabelSchema:
        return self._schema

    def generate(self, bars: Sequence[ModelBar]) -> Iterator[SupervisedLabel]:
        cfg = self.config
        for index in range(0, len(bars) - cfg.horizon_bars):
            signal = bars[index]
            entry = bars[index + 1]
            if not _entry_bar(signal, entry):
                continue
            entry_price = cfg.costs.entry_fill(entry.open)
            upper = entry_price * (Decimal("1") + cfg.positive_barrier)
            lower = entry_price * (Decimal("1") - cfg.negative_barrier)
            previous = signal
            result: tuple[TripleBarrierOutcome, ModelBar, Decimal, bool] | None = None
            invalid = False
            for candidate in bars[index + 1 : index + cfg.horizon_bars + 1]:
                if not _same_series(previous, candidate):
                    invalid = True
                    break
                hit_upper = candidate.high >= upper
                hit_lower = candidate.low <= lower
                if hit_upper and hit_lower:
                    if cfg.ambiguity_policy is IntrabarAmbiguityPolicy.SKIP:
                        invalid = True
                        break
                    # The unfavorable event wins; a downward opening gap is worse than the stop.
                    result = (
                        TripleBarrierOutcome.LOSS,
                        candidate,
                        min(candidate.open, lower),
                        candidate.open <= lower,
                    )
                    break
                if hit_lower:
                    result = (
                        TripleBarrierOutcome.LOSS,
                        candidate,
                        min(candidate.open, lower),
                        candidate.open <= lower,
                    )
                    break
                if hit_upper:
                    # Never assume price improvement on a favorable gap.
                    result = (TripleBarrierOutcome.WIN, candidate, upper, candidate.open >= upper)
                    break
                previous = candidate
            if invalid:
                continue
            if result is None:
                timeout = bars[index + cfg.horizon_bars]
                result = (TripleBarrierOutcome.TIMEOUT, timeout, timeout.close, False)
            outcome, outcome_bar, raw_outcome_price, at_open = result
            outcome_price = cfg.costs.exit_fill(raw_outcome_price)
            net_return = outcome_price / entry_price - Decimal("1")
            value = {
                TripleBarrierOutcome.LOSS: -1,
                TripleBarrierOutcome.TIMEOUT: 0,
                TripleBarrierOutcome.WIN: 1,
            }[outcome]
            yield SupervisedLabel(
                signal.symbol,
                signal.start,
                entry.start,
                entry_price,
                outcome_bar.start if at_open else outcome_bar.end,
                outcome_price,
                value,
                net_return,
                self.schema.label_id,
                self.schema.version,
                outcome.value,
            )
