"""Long entry when the close confirms a double bottom by breaking the interim peak."""

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

STRATEGY_ID = "double_bottom_reversal"
TIMEFRAME = CandleTimeframe.M1
PERCENT = Decimal(100)


class DoubleBottomReversalBuy(BuyStrategy):
    """BUY when the close first rises above the peak between two similar swing lows.

    Swing points are fractals with `strength` bars each side, confirmed by bars up to the one
    before the signal bar.  With the last two swing lows B1 (earlier) and B2: (1) they differ by at most
    `tolerance_pct` of the lower one; (2) the highest swing high P between them is at least
    `min_rise_pct` above the higher low; (3) no bar since B1 broke below the lower low by more than
    the tolerance; (4) the close crosses above P and no close since B2 had.  Needs
    ``lookback + 1`` completed bars (101 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="price_action", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=101,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("strength", ParameterKind.INT, 2, 1, 10,
                          description="Bars on each side of a swing point. Default 2."),
            ParameterSpec("lookback", ParameterKind.INT, 100, 20, 400,
                          description="Completed bars searched before the signal bar. Default 100."),
            ParameterSpec("tolerance_pct", ParameterKind.DECIMAL, Decimal("0.5"), Decimal("0.05"), Decimal(10),
                          description="Maximum difference between the two lows, percent. Default 0.5."),
            ParameterSpec("min_rise_pct", ParameterKind.DECIMAL, Decimal(1), Decimal("0.1"), Decimal(30),
                          description="Minimum rise between the lows to the interim peak, percent. Default 1."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=("BUY when the close crosses above the highest swing high between the last two "
                     "approximately equal swing lows (confirmed double bottom)."),
        source=("Robert Edwards and John Magee, Technical Analysis of Stock Trends; Thomas Bulkowski, "
                "Encyclopedia of Chart Patterns, Double Bottom (two nearly equal bottoms, a rise of at "
                "least 10% between them on daily charts, confirmed by a close above the interim peak)."),
        execution_limitations=("Bulkowski's daily-chart percentages (about 6% and 10%) are replaced by "
                               "intraday parameter defaults; he calls his guidelines flexible. A prior "
                               "downtrend is not required.",
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
        tolerance, rise = v["tolerance_pct"], v["min_rise_pct"]
        assert isinstance(strength, int) and isinstance(lookback, int)
        assert isinstance(tolerance, Decimal) and isinstance(rise, Decimal)
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
        if len(lows) >= 2:
            (i1, b1), (i2, b2) = lows[-2], lows[-1]
            if not contiguous(window[i1:]):
                return insufficient(self.spec, context)
            peaks = [(i, p) for i, p in highs if i1 < i < i2]
            floor, ceiling = min(b1, b2), max(b1, b2)
            if peaks and (ceiling - floor) <= tolerance / PERCENT * floor:
                peak = max(p for _, p in peaks)
                held = min(bar.low for bar in window[i1:-1]) >= floor * (1 - tolerance / PERCENT)
                triggered = (held and peak >= ceiling * (1 + rise / PERCENT)
                             and first_close_above(window, lambda _x: peak, i2 + 1))
        return finish(self.spec, context, bars[-1], state, triggered)
