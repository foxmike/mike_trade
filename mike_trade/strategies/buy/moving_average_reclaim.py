"""Long entry when price reclaims its simple moving average."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, finish, gate, insufficient
from mike_trade.strategies.indicators.core import sma

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


class MovingAverageReclaimBuy(BuyStrategy):
    spec = StrategySpec(
        strategy_id="moving_average_reclaim", version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="moving_average", reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,), warmup_bars=21,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema((ParameterSpec("period", ParameterKind.INT, 20, 2, 100),)),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology="BUY when a completed close crosses from at-or-below to above its n-bar SMA.",
        source="StockCharts ChartSchool, Moving Averages - Simple and Exponential: https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/moving-averages-simple-and-exponential",
        execution_limitations=("Price-to-SMA crossover, distinct from a two-average crossover.",
                               "Completed adjacent M1 crossing bars; regular session and BUY eligibility apply."),
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
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        prior_sma = sma((bar.close for bar in bars[-period - 1:-1]), period)[-1]
        latest_sma = sma((bar.close for bar in bars[-period:]), period)[-1]
        assert prior_sma is not None and latest_sma is not None
        return finish(self.spec, context, bars[-1], state,
                      bars[-2].close <= prior_sma and bars[-1].close > latest_sma)
