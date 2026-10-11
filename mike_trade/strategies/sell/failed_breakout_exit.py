"""Exit a long when a fresh upside breakout immediately fails."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
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
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


class FailedBreakoutExitSell(SellRule):
    spec = StrategySpec(
        strategy_id="failed_breakout_exit",
        version="1.0.0",
        side=StrategyRole.SELL,
        category="technical",
        sub_category="breakout",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=22,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema((ParameterSpec("period", ParameterKind.INT, 20, 2, 200),)),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "SELL when the prior close broke above the preceding n-bar high but the "
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
            "The breakout and failure bars must be adjacent regular-session M1 bars.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        return period + 2

    def evaluate_trigger(
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
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        level = max(bar.high for bar in bars[-period - 2 : -2])
        return finish(
            self.spec, context, bars[-1], state, bars[-2].close > level and bars[-1].close < level
        )
