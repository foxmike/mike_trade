"""Long entry when the close breaks above a shallow, short consolidation after a sharp advance."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import BuyStrategy, ParameterKind, ParameterSchema, ParameterSpec, StrategyEvaluation, StrategySpec, TriggerKind
from mike_trade.strategies.closed_bar import REASON_CODES, STATE_LAST_SIGNAL_BAR, adjacent, blocked, closed_bars, finish, gate, insufficient, require_param
from mike_trade.strategies.indicators.patterns import contiguous

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "bull_flag_breakout"
TIMEFRAME = CandleTimeframe.M1
PERCENT = Decimal(100)


class BullFlagBreakoutBuy(BuyStrategy):
    """BUY when the close first exceeds the flag high after a flagpole and a shallow, short flag.

    Pole top P is the latest bar with the highest high among the `lookback` bars before the signal
    bar.  Flagpole: the lowest low within `pole_bars` bars up to P is at least `pole_pct` percent
    below P's high.  Flag: the `min_flag_bars`..`max_flag_bars` bars after P; the lowest low in the
    flag retraces no more than `max_retrace` of the pole's height.  Signal: the close exceeds the
    flag's highest high (the upper boundary).  Needs ``lookback + 1`` completed bars (61 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="price_action", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=61,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("lookback", ParameterKind.INT, 60, 20, 300,
                          description="Completed bars searched for the pole top. Default 60."),
            ParameterSpec("pole_bars", ParameterKind.INT, 15, 3, 100,
                          description="Maximum bars of the advance up to the pole top. Default 15."),
            ParameterSpec("pole_pct", ParameterKind.DECIMAL, Decimal(1), Decimal("0.1"), Decimal(30),
                          description="Minimum advance low-to-top, percent. Default 1."),
            ParameterSpec("min_flag_bars", ParameterKind.INT, 3, 2, 50,
                          description="Minimum consolidation bars before a breakout counts. Default 3."),
            ParameterSpec("max_flag_bars", ParameterKind.INT, 20, 3, 100,
                          description="Maximum consolidation bars. Default 20."),
            ParameterSpec("max_retrace", ParameterKind.DECIMAL, Decimal("0.5"), Decimal("0.1"), Decimal("0.9"),
                          description="Maximum fraction of the pole retraced. Default 0.5."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=("BUY when the close exceeds the high of a flag that follows a steep advance, lasts a "
                     "limited time and retraces no more than the allowed share of the pole."),
        source=("Thomas Bulkowski, Encyclopedia of Chart Patterns, Flags (steep pole, short flag of up "
                "to roughly three to four weeks on daily bars, upward breakout); Murphy, Technical "
                "Analysis of the Financial Markets, ch. 7 (flags retrace no more than about half the pole)."),
        execution_limitations=("Published descriptions give no minimum pole height, so pole_pct and the "
                               "bar limits are parameters. Pennants and volume contraction are not used.",
                               "Pole and flag bars must be consecutive completed bars; regular session and "
                               "BUY eligibility apply."),
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
        lookback, pole_bars, pole_pct = v["lookback"], v["pole_bars"], v["pole_pct"]
        min_flag, max_flag, retrace = v["min_flag_bars"], v["max_flag_bars"], v["max_retrace"]
        assert isinstance(lookback, int) and isinstance(pole_bars, int) and isinstance(pole_pct, Decimal)
        assert isinstance(min_flag, int) and isinstance(max_flag, int) and isinstance(retrace, Decimal)
        require_param(min_flag <= max_flag, "min_flag_bars <= max_flag_bars", "min_flag_bars")
        require_param(lookback > max_flag + 1, "lookback > max_flag_bars + 1", "lookback")
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, lookback + 1)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        window = list(bars[-lookback - 1:])
        last = len(window) - 1
        top = max(range(last), key=lambda i: (window[i].high, i))
        flag = window[top + 1:last]
        triggered = False
        if min_flag <= len(flag) <= max_flag:
            start = max(0, top - pole_bars)
            if not contiguous(window[start:]):
                return insufficient(self.spec, context)
            peak = window[top].high
            floor = min(bar.low for bar in window[start:top + 1])
            height = peak - floor
            if (height >= pole_pct / PERCENT * floor
                    and peak - min(bar.low for bar in flag) <= retrace * height):
                triggered = window[last].close > max(bar.high for bar in flag)
        return finish(self.spec, context, bars[-1], state, triggered)
