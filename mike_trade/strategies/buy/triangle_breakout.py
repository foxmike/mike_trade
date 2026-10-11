"""Long entry when the close breaks above a descending resistance trendline of a symmetrical triangle."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, finish, gate, insufficient, require_param
from mike_trade.strategies.indicators.patterns import contiguous, first_close_above, line_value
from mike_trade.strategies.indicators.windows import swing_pivots

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "triangle_breakout"
TIMEFRAME = CandleTimeframe.M1


class TriangleBreakoutBuy(BuyStrategy):
    """BUY when the close first rises above the line through the last two falling swing highs while swing lows rise.

    Swing points are fractals with `strength` bars each side, confirmed by bars up to the one
    before the signal bar.  With the last two swing highs H1 (earlier) and H2: H2 is strictly lower
    than H1 and the last two swing lows since H1 are strictly rising (converging boundaries).  The trendline is the straight line through them extended to the signal
    bar.  Signal: the close is above the line and no close from H1 to the previous bar was (the line
    was never closed through).  Needs ``lookback + 1`` completed bars (101 by default).
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
            ParameterSpec("lookback", ParameterKind.INT, 100, 20, 400,
                          description="Completed bars searched before the signal bar. Default 100."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology="BUY when the close crosses above the falling upper boundary of a symmetrical triangle (lower highs, higher lows).",
        source=("Thomas Bulkowski, Encyclopedia of Chart Patterns, Symmetrical Triangle (lower highs and higher "
                "lows converging; the trade is a close outside the boundary, here upward); Edwards and "
                "Magee, Technical Analysis of Stock Trends; John Murphy, ch. 7."),
        execution_limitations=("Only two-point trendlines through consecutive swing highs; touches are "
                               "not counted beyond that.",
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
        strength, lookback = v["strength"], v["lookback"]
        assert isinstance(strength, int) and isinstance(lookback, int)
        require_param(lookback >= 8 * strength, "lookback >= 8 * strength", "lookback")
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
        if len(highs) >= 2:
            (x1, y1), (x2, y2) = highs[-2], highs[-1]
            if not contiguous(window[x1:]):
                return insufficient(self.spec, context)
            if y2 < y1 and len(rising := [p for i, p in lows if i > x1]) >= 2 and rising[-2] < rising[-1]:
                triggered = first_close_above(window, lambda x: line_value(x1, y1, x2, y2, x), x1)
        return finish(self.spec, context, bars[-1], state, triggered)
