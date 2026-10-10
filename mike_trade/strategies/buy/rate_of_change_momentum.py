"""Long entry when price rate of change crosses above zero."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, crossed_above, finish, gate, insufficient
from mike_trade.strategies.indicators.finite import rate_of_change

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


def _required(period: int) -> int:
    return period + 2  # two n-bar price comparisons


class RateOfChangeMomentumBuy(BuyStrategy):
    spec = StrategySpec(
        strategy_id="rate_of_change_momentum", version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="momentum", reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,), warmup_bars=_required(12),
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema((ParameterSpec("period", ParameterKind.INT, 12, 2, 100),)),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology="BUY when ROC = 100*(close[t]/close[t-n]-1) crosses above zero on a completed bar.",
        source="StockCharts ChartSchool, Rate of Change (ROC): https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-indicators/rate-of-change-roc",
        execution_limitations=("Observed completed M1 closes only; no missing-bar fill.",
                               "The two crossing bars must be adjacent; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        period = cls.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        return _required(period)

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        period = self.spec.parameter_schema.validate(params)["period"]
        assert isinstance(period, int)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, _required(period))
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        prior = rate_of_change(bars[-2].close, bars[-period - 2].close)
        latest = rate_of_change(bars[-1].close, bars[-period - 1].close)
        return finish(self.spec, context, bars[-1], state,
                      crossed_above(prior, Decimal(0), latest, Decimal(0)))
