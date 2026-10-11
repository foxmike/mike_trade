"""Short entry below Chester Keltner's original lower channel."""

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
from mike_trade.strategies.indicators.finite import original_keltner

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


class ShortKeltnerChannelBreakdown(ShortStrategy):
    spec = StrategySpec(
        strategy_id="short_keltner_channel_breakdown",
        version="1.0.0",
        side=StrategyRole.SHORT,
        category="technical",
        sub_category="volatility",
        reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,),
        warmup_bars=11,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "period",
                    ParameterKind.INT,
                    10,
                    2,
                    100,
                    description="Original Keltner SMA and range length. Default 10.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology="SHORT when close crosses below SMA(typical price,n) - SMA(high-low range,n).",
        source=(
            "Chester Keltner, How to Make Money in Commodities; original channel formula "
            "in StockCharts ChartSchool, Keltner Channels."
        ),
        execution_limitations=(
            "Uses Keltner's original finite SMA and range channel, not the later EMA/ATR version.",
            "SHORT entry only; completed adjacent M1 crossing bars.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        return period + 1

    def evaluate(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        period = self.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, period + 1)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        prior_lower = original_keltner(bars[-period - 1 : -1])[0]
        latest_lower = original_keltner(bars[-period:])[0]
        return finish(
            self.spec,
            context,
            bars[-1],
            state,
            bars[-2].close >= prior_lower and bars[-1].close < latest_lower,
        )
