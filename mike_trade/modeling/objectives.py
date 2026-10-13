"""Initial BUY and SELL objective construction on shared completed-bar features."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from mike_trade.modeling.contracts import (
    FeatureSchema,
    FeatureVector,
    LabelSchema,
    LearningTask,
    ModelBar,
    SupervisedLabel,
    TrainingExample,
    identifier,
    semver,
)
from mike_trade.modeling.features import IntradayFeaturePipeline
from mike_trade.modeling.labels import ExecutionCostAssumptions


@dataclass(frozen=True, slots=True)
class ScheduledEntryPolicy:
    """Point-in-time, non-overlapping position generator with no outcome-based selection."""

    policy_id: str = "scheduled_flat_entry"
    version: str = "1.0.0"
    warmup_bars: int = 30
    entry_interval_bars: int = 60
    maximum_holding_bars: int = 45
    cooldown_bars: int = 15

    def __post_init__(self) -> None:
        identifier(self.policy_id, "policy_id")
        semver(self.version, "entry policy version")
        for name in (
            "warmup_bars",
            "entry_interval_bars",
            "maximum_holding_bars",
            "cooldown_bars",
        ):
            value = getattr(self, name)
            minimum = 0 if name == "cooldown_bars" else 1
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an int >= {minimum}")

    def as_mapping(self) -> dict[str, str]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "warmup_bars": str(self.warmup_bars),
            "entry_interval_bars": str(self.entry_interval_bars),
            "maximum_holding_bars": str(self.maximum_holding_bars),
            "cooldown_bars": str(self.cooldown_bars),
            "selection": "time_schedule_only_no_future_outcome_filter",
            "entry_execution": "next_contiguous_positive_volume_bar_open",
            "overlap_rule": "max(entry_interval,holding_plus_cooldown)",
        }


@dataclass(frozen=True, slots=True)
class SellContinuationConfig:
    horizon_bars: int = 10
    risk_barrier: Decimal = Decimal("0.01")
    minimum_hold_advantage: Decimal = Decimal("0")
    costs: ExecutionCostAssumptions = ExecutionCostAssumptions()
    label_id: str = "sell_continuation"
    version: str = "1.0.0"

    def __post_init__(self) -> None:
        if isinstance(self.horizon_bars, bool) or self.horizon_bars < 1:
            raise ValueError("horizon_bars must be a positive int")
        if (
            not isinstance(self.risk_barrier, Decimal)
            or not self.risk_barrier.is_finite()
            or not Decimal("0") < self.risk_barrier < Decimal("1")
        ):
            raise ValueError("risk_barrier must be a finite Decimal in (0, 1)")
        if (
            not isinstance(self.minimum_hold_advantage, Decimal)
            or not self.minimum_hold_advantage.is_finite()
            or self.minimum_hold_advantage < 0
        ):
            raise ValueError("minimum_hold_advantage must be a finite non-negative Decimal")


class SellPositionDatasetBuilder:
    """Create realistic held-position samples independently of future winners/losers.

    Positions are entered solely by the scheduled policy and remain in the sample population
    for a fixed maximum holding period. Labels compare a permitted next-bar exit with continued
    holding; outcomes never decide which positions are admitted.
    """

    CLASS_LABELS = ("-1", "0", "1")
    POSITION_FEATURES = (
        "position_unrealized_return",
        "position_holding_bars",
        "position_max_favorable_return",
        "position_max_adverse_return",
    )

    def __init__(
        self,
        feature_pipeline: IntradayFeaturePipeline,
        entry_policy: ScheduledEntryPolicy,
        config: SellContinuationConfig,
    ) -> None:
        self.feature_pipeline = feature_pipeline
        self.entry_policy = entry_policy
        self.config = config
        if config.horizon_bars >= entry_policy.maximum_holding_bars:
            raise ValueError("SELL horizon must be shorter than maximum_holding_bars")
        base = feature_pipeline.schema
        self.feature_schema = FeatureSchema(
            "intraday_position",
            "1.0.0",
            (*base.names, *self.POSITION_FEATURES),
            base.required_reference_symbols,
            base.timeframe,
        )
        self.label_schema = LabelSchema(
            config.label_id,
            config.version,
            LearningTask.MULTICLASS_CLASSIFICATION,
            config.horizon_bars,
            {
                "risk_barrier": str(config.risk_barrier),
                "minimum_hold_advantage": str(config.minimum_hold_advantage),
                "classes": "-1=RISK_EXIT,0=EXIT_FAVORABLE,1=HOLD_FAVORABLE",
                "entry_policy_id": entry_policy.policy_id,
                "entry_policy_version": entry_policy.version,
                **config.costs.parameters(),
            },
        )

    def build_symbol(
        self,
        bars: Sequence[ModelBar],
        *,
        references: Mapping[str, Sequence[ModelBar]] | None = None,
    ) -> Iterator[TrainingExample]:
        if len(bars) < self.entry_policy.warmup_bars + self.config.horizon_bars + 2:
            return
        base_features = {
            item.bar_start: item
            for item in self.feature_pipeline.transform(bars, references=references)
        }
        index = 0
        while index < len(bars) - 1:
            session_start = index
            session_id = bars[index].session_id
            while index < len(bars) and bars[index].session_id == session_id:
                index += 1
            session_end = index
            yield from self._session_examples(bars, base_features, session_start, session_end)

    def _session_examples(
        self,
        bars: Sequence[ModelBar],
        base_features: Mapping[datetime, FeatureVector],
        session_start: int,
        session_end: int,
    ) -> Iterator[TrainingExample]:
        policy, cfg = self.entry_policy, self.config
        signal_index = session_start + policy.warmup_bars - 1
        while signal_index + 1 < session_end:
            signal = bars[signal_index]
            entry_bar = bars[signal_index + 1]
            if entry_bar.start != signal.end or entry_bar.volume <= 0:
                signal_index += policy.entry_interval_bars
                continue
            entry_price = cfg.costs.entry_fill(entry_bar.open)
            position_end = min(
                signal_index + 1 + policy.maximum_holding_bars,
                session_end,
            )
            maximum_high = entry_bar.high
            minimum_low = entry_bar.low
            for held_index in range(signal_index + 1, position_end):
                current = bars[held_index]
                maximum_high = max(maximum_high, current.high)
                minimum_low = min(minimum_low, current.low)
                base = base_features.get(current.start)
                outcome_index = held_index + cfg.horizon_bars
                if (
                    base is None
                    or held_index + 1 >= session_end
                    or outcome_index >= session_end
                    or outcome_index >= position_end
                ):
                    continue
                future = bars[held_index + 1 : outcome_index + 1]
                if any(
                    right.start != left.end
                    for left, right in zip(bars[held_index:outcome_index], future, strict=True)
                ):
                    continue
                values = dict(base.values)
                values.update(
                    {
                        "position_unrealized_return": float(current.close / entry_price - 1),
                        "position_holding_bars": float(held_index - signal_index),
                        "position_max_favorable_return": float(maximum_high / entry_price - 1),
                        "position_max_adverse_return": float(minimum_low / entry_price - 1),
                    }
                )
                features = FeatureVector(
                    current.symbol,
                    current.start,
                    current.end,
                    current.session_id,
                    current.feed,
                    self.feature_schema.feature_id,
                    self.feature_schema.version,
                    values,
                )
                next_bar = bars[held_index + 1]
                immediate_exit = cfg.costs.exit_fill(next_bar.open)
                hold_bar = bars[outcome_index]
                hold_exit = cfg.costs.exit_fill(hold_bar.close)
                future_low = min(item.low for item in future)
                risk_price = entry_price * (Decimal("1") - cfg.risk_barrier)
                if future_low <= risk_price:
                    label_value, outcome = -1, "RISK_EXIT"
                    risk_bar = next(item for item in future if item.low <= risk_price)
                    outcome_price = cfg.costs.exit_fill(min(risk_price, risk_bar.open))
                    outcome_time = (
                        risk_bar.start if risk_bar.open <= risk_price else risk_bar.end
                    )
                elif hold_exit - immediate_exit > entry_price * cfg.minimum_hold_advantage:
                    label_value, outcome = 1, "HOLD_FAVORABLE"
                    outcome_price, outcome_time = hold_exit, hold_bar.end
                else:
                    label_value, outcome = 0, "EXIT_FAVORABLE"
                    outcome_price, outcome_time = immediate_exit, next_bar.start
                label = SupervisedLabel(
                    current.symbol,
                    current.start,
                    next_bar.start,
                    immediate_exit,
                    outcome_time,
                    outcome_price,
                    label_value,
                    outcome_price / entry_price - Decimal("1"),
                    self.label_schema.label_id,
                    self.label_schema.version,
                    outcome,
                )
                yield TrainingExample(
                    features,
                    label,
                    {
                        "position_entry_time": entry_bar.start.isoformat(),
                        "position_entry_price": str(entry_price),
                        "position_side": "LONG",
                        "position_quantity_basis": "one_share",
                        "entry_policy_id": policy.policy_id,
                        "entry_policy_version": policy.version,
                    },
                )
            signal_index += max(
                policy.entry_interval_bars,
                policy.maximum_holding_bars + 1 + policy.cooldown_bars,
            )

    @property
    def maximum_label_duration(self) -> timedelta:
        return timedelta(minutes=self.config.horizon_bars)
