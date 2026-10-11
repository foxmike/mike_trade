"""Long entry when the close breaks above the latest confirmed swing-high resistance."""

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

STRATEGY_ID = "resistance_breakout"
TIMEFRAME = CandleTimeframe.M1


class ResistanceBreakoutBuy(BuyStrategy):
    """BUY when the close crosses above the most recent confirmed swing high.

    Resistance ``R`` is the latest fractal swing high (strict extreme over `strength` bars each
    side) inside the `lookback` completed bars *before* the signal bar, so ``R`` is fixed by bars
    that were already closed.  Signal: ``close[t-1] <= R`` and ``close[t] > R``.  Unlike
    ``donchian_channel_breakout`` the level is a structural pivot, not the highest high of a
    window.  Needs ``lookback + 1`` completed bars (51 by default).  Finite windows only.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID, version="1.0.0", side=StrategyRole.BUY,
        category="technical", sub_category="price_action", reference_symbols=(),
        required_timeframes=(TIMEFRAME,), warmup_bars=51,
        completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema((
            ParameterSpec("strength", ParameterKind.INT, 2, 1, 10,
                          description="Bars on each side of a swing high. Default 2."),
            ParameterSpec("lookback", ParameterKind.INT, 50, 10, 300,
                          description="Completed bars before the signal bar searched for the pivot. Default 50."),
        )),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology="BUY when the close crosses from at-or-below to above the latest confirmed swing high.",
        source=("Robert Edwards and John Magee, Technical Analysis of Stock Trends (resistance "
                "and breakout); John Murphy, Technical Analysis of the Financial Markets (1999), "
                "ch. 4; swing highs as Bill Williams fractals (Trading Chaos)."),
        execution_limitations=("The most recent pivot is used even if an older, higher peak exists.",
                               "Pivot bars and the latest two bars must be adjacent; regular "
                               "session and BUY eligibility apply."),
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
        require_param(lookback >= 2 * strength + 2, "lookback >= 2 * strength + 2", "lookback")
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, lookback + 1)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not adjacent(bars[-2], bars[-1]):
            return insufficient(self.spec, context)
        highs, _ = swing_pivots(bars[-lookback - 1:-1], strength)
        resistance = highs[-1][1] if highs else None
        triggered = (resistance is not None and bars[-2].close <= resistance
                     and bars[-1].close > resistance)
        return finish(self.spec, context, bars[-1], state, triggered)
