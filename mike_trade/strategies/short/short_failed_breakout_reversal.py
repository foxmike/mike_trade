"""SHORT when a fresh upside breakout immediately fails."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    ShortStrategy,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    adjacent,
    blocked,
    closed_bars,
    finish,
    gate,
    insufficient,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


class ShortFailedBreakoutReversal(ShortStrategy):
    spec = StrategySpec(
        strategy_id="short_failed_breakout_reversal",
        version="1.0.0",
        side=StrategyRole.SHORT,
        category="technical",
        sub_category="breakout",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=22,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "period",
                    ParameterKind.INT,
                    20,
                    2,
                    200,
                    description="Reference bars immediately before the breakout. Default 20.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "SHORT when the prior close broke above the immediately preceding n-bar high but the "
            "latest close falls back below that same breakout level."
        ),
        source=(
            "Thomas N. Bulkowski, Encyclopedia of Chart Patterns, busted/failed "
            "breakouts; breakout level uses Richard Donchian's prior-period price "
            "channel convention."
        ),
        execution_limitations=(
            "The failure must occur on the immediately following completed bar; "
            "equality is not a breakout or failure.",
            "The reference, breakout, and failure bars must be adjacent M1 bars; "
            "the reference excludes the breakout candle.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        return period + 2

    def evaluate(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        period = self.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        bars = closed_bars(self.spec, context, period + 2)
        if bars is None:
            return insufficient(self.spec, context)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        window = bars[-period - 2 :]
        if any(not adjacent(a, b) for a, b in zip(window, window[1:], strict=False)):
            return insufficient(self.spec, context)
        prior_level = max(bar.high for bar in window[:-2])
        was_breakout = window[-2].close > prior_level
        return finish(
            self.spec, context, bars[-1], state, was_breakout and bars[-1].close < prior_level
        )
