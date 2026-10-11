"""Long entry when a higher swing low is confirmed inside a higher-high uptrend."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, finish, gate, insufficient, require_param
from mike_trade.strategies.indicators.windows import swing_pivots

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "higher_high_higher_low"
TIMEFRAME = CandleTimeframe.M1


class HigherHighHigherLowBuy(BuyStrategy):
    """BUY on the bar that confirms a swing low higher than the previous swing low while the last
    two swing highs are also rising (Dow-theory uptrend structure).

    Swing points are fractals with `strength` bars on each side (strict extremes).  A swing low at
    bar ``c`` is only knowable at bar ``c + strength``, so the signal bar is exactly
    ``t = c + strength``: the entry is the first completed bar that proves the higher low.
    Requires ``HH`` (latest swing high > previous swing high) and ``HL`` (this swing low >
    previous swing low) among pivots inside the last `lookback` bars.  Needs ``lookback + 1``
    completed bars (51 by default).  Finite windows only.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="price_action", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=51,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("strength", ParameterKind.INT, 2, 1, 10,
                          description="Bars on each side of a swing point. Default 2."),
            ParameterSpec("lookback", ParameterKind.INT, 50, 10, 300,
                          description="Completed bars searched for swing points. Default 50."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=("BUY when a newly confirmed swing low is above the previous swing low and the "
                     "latest swing high is above the previous swing high."),
        source=("Dow Theory as presented in John Murphy, Technical Analysis of the Financial "
                "Markets (1999), ch. 4 (an uptrend is a series of rising peaks and troughs); swing "
                "points as Bill Williams fractals (Trading Chaos)."),
        execution_limitations=("Entry is delayed by `strength` bars by design: a pivot is not "
                               "known earlier without look-ahead.",
                               "Every pivot's 2*strength+1 bars and the latest two bars must be "
                               "adjacent; regular session and BUY eligibility apply."),
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
        require_param(lookback >= 4 * strength + 2, "lookback >= 4 * strength + 2", "lookback")
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, lookback + 1)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        window = bars[-lookback - 1:]
        highs, lows = swing_pivots(window, strength)
        triggered = (len(highs) >= 2 and len(lows) >= 2
                     and lows[-1][0] == len(window) - 1 - strength
                     and lows[-1][1] > lows[-2][1] and highs[-1][1] > highs[-2][1])
        return finish(self.spec, context, bars[-1], state, triggered)
