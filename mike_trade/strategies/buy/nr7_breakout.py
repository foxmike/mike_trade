"""Long entry after a narrow-range bar breaks above its high."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, finish, gate, insufficient

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


class Nr7BreakoutBuy(BuyStrategy):
    spec = StrategySpec(
        strategy_id="nr7_breakout", version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="volatility", reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,), warmup_bars=8,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema((ParameterSpec("period", ParameterKind.INT, 7, 4, 7),)),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology="BUY when the preceding bar has the strictly narrowest high-low range of n bars (NR7 default), and the next close exceeds its high.",
        source="Toby Crabel, Day Trading with Short Term Price Patterns and Opening Range Breakout; StockCharts ChartSchool, Narrow Range Day NR7: https://chartschool.stockcharts.com/table-of-contents/trading-strategies-and-models/trading-strategies/narrow-range-day-nr7",
        execution_limitations=("M1 bar adaptation of the published daily-bar NR7/NR4 setup; n=4 reproduces NR4.",
                               "All setup and breakout bars must be adjacent completed M1 bars in one session."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        return period + 1

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        period = self.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, period + 1)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        window = bars[-period - 1:]
        if any(not adjacent(a, b) for a, b in zip(window, window[1:])):
            return insufficient(self.spec, context)
        setup = window[-2]
        narrow = setup.high - setup.low < min(bar.high - bar.low for bar in window[:-2])
        return finish(self.spec, context, bars[-1], state,
                      narrow and bars[-1].close > setup.high)
