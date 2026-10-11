"""Long entry when the close breaks above the Ichimoku cloud."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterSchema, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, finish, gate, insufficient
from mike_trade.strategies.indicators.windows import ichimoku_cloud

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "ichimoku_cloud_breakout"
TIMEFRAME = CandleTimeframe.M1
TENKAN, KIJUN, SENKOU_B, DISPLACEMENT = 9, 26, 52, 26
#: 52-bar Span B window ending 26 bars back, plus the prior bar for the crossing.
BARS_NEEDED = SENKOU_B + DISPLACEMENT + 1


class IchimokuCloudBreakoutBuy(BuyStrategy):
    """BUY when the close crosses above the top of the Ichimoku cloud.

    Cloud top at a bar is ``max(Span A, Span B)`` *as plotted at that bar*: Hosoda's spans are
    computed from the 9/26/52-bar windows and plotted 26 bars ahead, so the cloud at the latest
    bar uses only data from 26 bars earlier (point-in-time; nothing after that window is read).
    Signal: ``close[t-1] <= top[t-1]`` and ``close[t] > top[t]``.  Fixed canonical settings
    (9, 26, 52, displacement 26); 79 completed bars are needed.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="trend_following", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=BARS_NEEDED,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(()), persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=("BUY when the close crosses from at-or-below to above the upper edge of the "
                     "displaced Ichimoku cloud (max of Senkou Span A and B at the bar)."),
        source=("Goichi Hosoda's Ichimoku Kinko Hyo; StockCharts ChartSchool, Ichimoku Cloud "
                "(Tenkan 9, Kijun 26, Senkou B 52, spans plotted 26 periods ahead): "
                "https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/ichimoku-cloud"),
        execution_limitations=("Fixed canonical 9/26/52/26 settings adapted to M1 bars; the Chikou "
                               "span is not used (it would require looking ahead of its plot).",
                               "Windows span the supplied completed bars like a continuous chart; "
                               "the crossing bars must be adjacent.",
                               "Regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        self.spec.parameter_schema.validate(params)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, BARS_NEEDED)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        window = bars[-BARS_NEEDED:]
        prior_top = max(ichimoku_cloud(window[:-1], TENKAN, KIJUN, SENKOU_B, DISPLACEMENT))
        latest_top = max(ichimoku_cloud(window, TENKAN, KIJUN, SENKOU_B, DISPLACEMENT))
        return finish(self.spec, context, bars[-1], state,
                      bars[-2].close <= prior_top and bars[-1].close > latest_top)
