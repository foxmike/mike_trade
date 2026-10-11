"""Long entry when the close breaks above the flat top of an ascending triangle."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, finish, gate, insufficient, require_param
from mike_trade.strategies.indicators.patterns import contiguous, first_close_above
from mike_trade.strategies.indicators.windows import swing_pivots

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "ascending_triangle_breakout"
TIMEFRAME = CandleTimeframe.M1
PERCENT = Decimal(100)


class AscendingTriangleBreakoutBuy(BuyStrategy):
    """BUY when the close first rises above a flat resistance touched three times above rising lows.

    Swing points are fractals with `strength` bars each side, confirmed by bars up to the one
    before the signal bar.  The last three swing highs lie within `flat_tolerance_pct` of each other
    (resistance R = the highest of them) and at least two swing lows since the first of those highs
    are strictly rising.  Signal: the close crosses above R and no close since the third touch had.
    Needs ``lookback + 1`` completed bars (101 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="breakout", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=101,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("strength", ParameterKind.INT, 2, 1, 10,
                          description="Bars on each side of a swing point. Default 2."),
            ParameterSpec("lookback", ParameterKind.INT, 100, 30, 400,
                          description="Completed bars searched before the signal bar. Default 100."),
            ParameterSpec("flat_tolerance_pct", ParameterKind.DECIMAL, Decimal("0.2"), Decimal("0.02"), Decimal(5),
                          description="Maximum spread of the three tops, percent. Default 0.2."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=("BUY when the close crosses above the flat top formed by three swing highs while "
                     "the swing lows are rising."),
        source=("Thomas Bulkowski, Encyclopedia of Chart Patterns, Ascending Triangle (flat top with "
                "three or more touches, rising bottom with two or more; an upward breakout is the "
                "trade); Edwards and Magee, Technical Analysis of Stock Trends."),
        execution_limitations=("The flatness tolerance is a parameter (published descriptions are "
                               "qualitative). Volume contraction is not required.",
                               "Pattern bars must be consecutive completed bars; regular session and BUY "
                               "eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        lookback = cls.spec.parameter_schema.validate(params)["lookback"]
        assert isinstance(lookback, int)
        return lookback + 1

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        v = self.spec.parameter_schema.validate(params)
        strength, lookback, flat = v["strength"], v["lookback"], v["flat_tolerance_pct"]
        assert isinstance(strength, int) and isinstance(lookback, int) and isinstance(flat, Decimal)
        require_param(lookback >= 10 * strength, "lookback >= 10 * strength", "lookback")
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, lookback + 1)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        window = list(bars[-lookback - 1:])
        highs, lows = swing_pivots(window[:-1], strength)
        triggered = False
        if len(highs) >= 3:
            tops = highs[-3:]
            if not contiguous(window[tops[0][0]:]):
                return insufficient(self.spec, context)
            resistance = max(p for _, p in tops)
            rising = [p for i, p in lows if i > tops[0][0]]
            if (resistance - min(p for _, p in tops) <= flat / PERCENT * resistance
                    and len(rising) >= 2 and rising[-2] < rising[-1]):
                triggered = first_close_above(window, lambda _x: resistance, tops[-1][0] + 1)
        return finish(self.spec, context, bars[-1], state, triggered)
