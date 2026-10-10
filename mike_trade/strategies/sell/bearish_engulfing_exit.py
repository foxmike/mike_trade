"""SELL on a bearish real-body engulfing pattern in consecutive M1 bars."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import ParameterSchema, StrategyEvaluation, StrategySpec, TriggerKind
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
from mike_trade.strategies.indicators.finite import engulfing
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "bearish_engulfing_exit"
STRATEGY_VERSION = "1.0.0"
#: Declared trigger and only required timeframe (see `_SPEC`).
TIMEFRAME = CandleTimeframe.M1
WARMUP_BARS = 2

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID, version=STRATEGY_VERSION, side=StrategyRole.SELL,
    category="technical", sub_category="price_action", reference_symbols=(),
    required_timeframes=(TIMEFRAME,), warmup_bars=WARMUP_BARS,
    completed_bars_only=True, requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
    parameter_schema=ParameterSchema(()),
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=("SELL when a bullish candle is followed by a bearish candle whose "
                 "real body fully engulfs the prior real body. This is a pattern trigger, "
                 "not an alpha or profitability claim."),
    source=("StockCharts ChartSchool, Candlestick Bearish Reversal Patterns: "
            "https://chartschool.stockcharts.com/table-of-contents/chart-analysis/candlestick-charts/candlestick-bearish-reversal-patterns"),
    execution_limitations=("Advisory long exit only; no position or order logic is owned "
                           "by this class.",
                           "Pattern bars must be consecutive completed regular-session "
                           "1-minute bars; a missing bar between the two invalidates it."),
    reason_codes=REASON_CODES,
)


class BearishEngulfingExitSell(SellRule):
    spec = _SPEC

    def evaluate_trigger(self, context: MarketContext, params: Mapping[str, object],
                         state: Mapping[str, object]) -> StrategyEvaluation:
        self.spec.parameter_schema.validate(params)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, 2)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        prior, latest = bars[-2:]
        if not adjacent(prior, latest):
            return insufficient(self.spec, context)
        triggered = engulfing(prior, latest, bullish=False)
        return finish(self.spec, context, latest, state, triggered)
