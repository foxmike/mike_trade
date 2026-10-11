"""Long entry on Larry Williams' bullish Ultimate Oscillator divergence breakout."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, finish, gate, insufficient, require_param
from mike_trade.strategies.indicators.patterns import ultimate_oscillator
from mike_trade.strategies.indicators.windows import swing_pivots

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "ultimate_oscillator"
TIMEFRAME = CandleTimeframe.M1
#: Longest Ultimate Oscillator window; the first value needs this many bars plus a prior close.
LONG = 28


class UltimateOscillatorBuy(BuyStrategy):
    """BUY when UO rises above the high reached between a bullish-divergence pair of lows.

    Williams' three steps, all on completed bars: (1) price makes a lower swing low (fractal with
    `strength` bars each side) while UO(7,14,28) makes a higher low at the same two bars;
    (2) the divergence's low UO is below `oversold` (30); (3) UO rises above the highest UO between
    the two lows (exclusive) - the first bar to do so after the second low is the signal.  The second low is
    only usable `strength` bars after it printed, so no later bar is read.  Needs
    ``lookback + 28`` completed bars (78 by default).  Finite windows only.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="oscillator", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=78,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("strength", ParameterKind.INT, 2, 1, 10,
                          description="Bars on each side of a swing low. Default 2."),
            ParameterSpec("lookback", ParameterKind.INT, 50, 20, 300,
                          description="Bars searched for the divergence pair. Default 50."),
            ParameterSpec("oversold", ParameterKind.DECIMAL, Decimal(30), Decimal(5), Decimal(50),
                          description="The divergence's UO low must be below this. Default 30."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=("BUY on the first bar where UO exceeds the highest UO between two price swing lows "
                     "that form a bullish divergence whose UO low is below the oversold level."),
        source=("Larry Williams, 'The Ultimate Oscillator', Technical Analysis of Stocks & Commodities "
                "(1985); StockCharts ChartSchool, Ultimate Oscillator (bullish divergence, low below "
                "30, buy when UO breaks above the divergence high)."),
        execution_limitations=("Only the buy rule is implemented; Williams' sell rules belong to SELL strategies.",
                               "Swing lows use fractals; they and the latest two bars must be adjacent. "
                               "Regular session and BUY eligibility apply."),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        lookback = cls.spec.parameter_schema.validate(params)["lookback"]
        assert isinstance(lookback, int)
        return lookback + LONG

    def evaluate(self, context: MarketContext, params: Mapping[str, object],
                 state: Mapping[str, object]) -> StrategyEvaluation:
        v = self.spec.parameter_schema.validate(params)
        strength, lookback, oversold = v["strength"], v["lookback"], v["oversold"]
        assert isinstance(strength, int) and isinstance(lookback, int) and isinstance(oversold, Decimal)
        require_param(lookback >= 4 * strength + 2, "lookback >= 4 * strength + 2", "lookback")
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, lookback + LONG)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        window = list(bars[-lookback - LONG:])
        uo = ultimate_oscillator(window)
        last = len(window) - 1
        lows = [(i, p) for i, p in swing_pivots(window, strength)[1] if i >= LONG]
        triggered = False
        if len(lows) >= 2 and uo[last] is not None and uo[last - 1] is not None:
            (first, first_price), (second, second_price) = lows[-2], lows[-1]
            between, after = uo[first + 1:second], uo[second + 1:last]
            if (between and None not in between and None not in after and second_price < first_price
                    and uo[second] > uo[first] and uo[first] < oversold):
                peak = max(between)
                triggered = (uo[last - 1] <= peak < uo[last]
                             and all(value <= peak for value in after))
        return finish(self.spec, context, bars[-1], state, triggered)
