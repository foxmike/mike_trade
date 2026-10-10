"""BUY on a bullish real-body engulfing pattern in two consecutive M1 bars."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.strategies.base import BuyStrategy, ParameterSchema, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, TIMEFRAME, closed_bars, finish, gate, insufficient, same_session
from mike_trade.strategies.indicators.finite import engulfing

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "engulfing_candle_reversal"
STRATEGY_VERSION = "1.0.0"
WARMUP_BARS = 2

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.BUY,
    category="technical", sub_category="price_action", reference_symbols=(),
    required_timeframes=(TIMEFRAME,), warmup_bars=WARMUP_BARS,
    completed_bars_only=True, requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema(()),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=("BUY when a bearish candle is followed by a bullish candle whose "
                 "real body fully engulfs the prior real body. No shadow engulfment "
                 "or separate trend filter is required. This is a pattern trigger, "
                 "not an alpha or profitability claim."),
    source=("StockCharts ChartSchool, Candlestick Bullish Reversal Patterns: "
            "https://chartschool.stockcharts.com/table-of-contents/chart-analysis/candlestick-charts/candlestick-bullish-reversal-patterns"),
    execution_limitations=("Pattern bars must be consecutive completed regular-session "
                           "1-minute bars in the same New York session; gaps invalidate it.",
                           "The pattern alone is not confirmation of a broader reversal."),
    reason_codes=REASON_CODES,
)


class EngulfingCandleReversalBuy(BuyStrategy):
    spec = _SPEC

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        self.spec.parameter_schema.validate(params)
        early = gate(self.spec, context, state)
        if early is not None:
            return early
        bars = closed_bars(context, 2)
        if bars is None:
            return insufficient(self.spec, context)
        prior, latest = bars[-2:]
        consecutive = latest.start - prior.start == timedelta(minutes=1)
        triggered = consecutive and same_session(prior, latest) and engulfing(prior, latest, bullish=True)
        return finish(self.spec, context, latest, state, triggered)
