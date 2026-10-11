"""Long exit when close breaks below the displaced Ichimoku cloud."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    ParameterSchema,
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
from mike_trade.strategies.indicators.windows import ichimoku_cloud
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "ichimoku_bearish_exit"
TIMEFRAME = CandleTimeframe.M1
TENKAN, KIJUN, SENKOU_B, DISPLACEMENT = 9, 26, 52, 26
BARS_NEEDED = SENKOU_B + DISPLACEMENT + 1


class IchimokuBearishExitSell(SellRule):
    """SELL when close crosses below the lower edge of the point-in-time cloud."""

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version="1.0.0",
        side=StrategyRole.SELL,
        category="technical",
        sub_category="trend_following",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=BARS_NEEDED,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(()),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "SELL when close crosses from at-or-above to below the lower edge of the "
            "displaced Ichimoku cloud (minimum of Senkou Span A and B)."
        ),
        source=(
            "Goichi Hosoda's Ichimoku Kinko Hyo; StockCharts ChartSchool, Ichimoku Cloud "
            "(Tenkan 9, Kijun 26, Senkou B 52, spans plotted 26 periods ahead): "
            "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/ichimoku-cloud"
        ),
        execution_limitations=(
            "Fixed canonical 9/26/52/26 settings adapted to M1; Chikou is "
            "excluded because using its future plot would look ahead.",
            "The cloud at each signal bar uses only observations ending 26 "
            "bars earlier; the crossing bars must be adjacent.",
            "Advisory long exit on completed regular-session bars only.",
        ),
        reason_codes=REASON_CODES,
    )

    def evaluate_trigger(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        self.spec.parameter_schema.validate(params)
        bars = closed_bars(self.spec, context, BARS_NEEDED)
        if bars is None:
            return insufficient(self.spec, context)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        window = bars[-BARS_NEEDED:]
        if any(not adjacent(a, b) for a, b in zip(window, window[1:], strict=False)):
            return insufficient(self.spec, context)
        prior_bottom = min(ichimoku_cloud(window[:-1], TENKAN, KIJUN, SENKOU_B, DISPLACEMENT))
        latest_bottom = min(ichimoku_cloud(window, TENKAN, KIJUN, SENKOU_B, DISPLACEMENT))
        return finish(
            self.spec,
            context,
            bars[-1],
            state,
            bars[-2].close >= prior_bottom and bars[-1].close < latest_bottom,
        )
