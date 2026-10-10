"""Long entry when the short SMA has crossed above both medium and long SMAs."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, finish, gate, insufficient, require_param
from mike_trade.strategies.indicators.core import sma

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext


def _sma(bars: tuple, period: int) -> Decimal:
    value = sma((bar.close for bar in bars[-period:]), period)[-1]
    assert value is not None
    return value


class TripleMovingAverageBuy(BuyStrategy):
    spec = StrategySpec(
        strategy_id="triple_moving_average", version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="moving_average", reference_symbols=(),
        required_timeframes=(CandleTimeframe.M1,), warmup_bars=21,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=CandleTimeframe.M1,
        parameter_schema=ParameterSchema((
            ParameterSpec("fast_period", ParameterKind.INT, 5, 2, 50),
            ParameterSpec("medium_period", ParameterKind.INT, 10, 3, 100),
            ParameterSpec("slow_period", ParameterKind.INT, 20, 4, 150),
        )), persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology="BUY when the fast SMA first exceeds both medium and slow SMAs, after being at-or-below at least one.",
        source="StockCharts ChartSchool, Moving Averages - Simple and Exponential, triple crossover: https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/moving-averages-simple-and-exponential",
        execution_limitations=("Requires fast < medium < slow; the final crossover completes the triple alignment.",
                               "Completed adjacent M1 crossing bars; regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        p = cls.spec.parameter_schema.validate(params)
        fast, medium, slow = p["fast_period"], p["medium_period"], p["slow_period"]
        assert isinstance(fast, int) and isinstance(medium, int) and isinstance(slow, int)
        require_param(fast < medium < slow, "fast_period < medium_period < slow_period", "fast_period")
        return slow + 1

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        p = self.spec.parameter_schema.validate(params)
        fast, medium, slow = p["fast_period"], p["medium_period"], p["slow_period"]
        assert isinstance(fast, int) and isinstance(medium, int) and isinstance(slow, int)
        require_param(fast < medium < slow, "fast_period < medium_period < slow_period", "fast_period")
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, slow + 1)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        previous = bars[:-1]
        f0, m0, s0 = (_sma(previous, n) for n in (fast, medium, slow))
        f1, m1, s1 = (_sma(bars, n) for n in (fast, medium, slow))
        triggered = f1 > m1 and f1 > s1 and (f0 <= m0 or f0 <= s0)
        return finish(self.spec, context, bars[-1], state, triggered)
