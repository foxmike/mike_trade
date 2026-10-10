"""Long entry on an upward turn in Alan Hull's moving average."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, finish, gate, insufficient
from mike_trade.strategies.indicators.finite import hull_moving_average, hull_smoothing_period

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


def _window(period: int) -> int:
    return period + hull_smoothing_period(period) - 1


class HullMovingAverageBuy(BuyStrategy):
    spec = StrategySpec(
        strategy_id="hull_moving_average", version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="moving_average", reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,), warmup_bars=_window(20) + 2,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema((ParameterSpec("period", ParameterKind.INT, 20, 4, 100),)),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology="BUY when HMA[n] turns upward: HMA[t-2] >= HMA[t-1] and HMA[t] > HMA[t-1].",
        source="StockCharts ChartSchool, Hull Moving Average: https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/hull-moving-average-hma",
        execution_limitations=("HMA is WMA(rounded sqrt(n)) of 2*WMA(rounded n/2)-WMA(n); halves round up.",
                               "Three turning bars must be adjacent completed M1 bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        return _window(period) + 2

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        period = self.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, _window(period) + 2)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not (adjacent(bars[-3], bars[-2]) and adjacent(bars[-2], bars[-1])):
            return insufficient(self.spec, context)
        size = _window(period)
        closes = [bar.close for bar in bars]
        older = hull_moving_average(closes[-size - 2:-2], period)
        prior = hull_moving_average(closes[-size - 1:-1], period)
        latest = hull_moving_average(closes[-size:], period)
        return finish(self.spec, context, bars[-1], state, older >= prior and latest > prior)
