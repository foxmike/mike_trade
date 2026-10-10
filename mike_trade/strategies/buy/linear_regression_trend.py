"""Long entry when rolling linear-regression slope turns positive."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, crossed_above, finish, gate, insufficient
from mike_trade.strategies.indicators.finite import linear_regression_slope

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


class LinearRegressionTrendBuy(BuyStrategy):
    spec = StrategySpec(
        strategy_id="linear_regression_trend", version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="trend_following", reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,), warmup_bars=21,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema((ParameterSpec("period", ParameterKind.INT, 20, 2, 100),)),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology="BUY when the n-bar least-squares close-price slope crosses above zero.",
        source="StockCharts ChartSchool, Slope: https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/slope",
        execution_limitations=("Slope is price change per observed bar, using x=0..n-1.",
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
        prior = linear_regression_slope([bar.close for bar in bars[-period - 1:-1]])
        latest = linear_regression_slope([bar.close for bar in bars[-period:]])
        return finish(self.spec, context, bars[-1], state,
                      crossed_above(prior, Decimal(0), latest, Decimal(0)))
