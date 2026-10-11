"""COVER when the close crosses above the most recent confirmed swing high."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    CoverStrategy,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    adjacent,
    blocked,
    closed_bars,
    finish,
    gate,
    insufficient,
    require_param,
)
from mike_trade.strategies.indicators.windows import swing_pivots

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_previous_swing_high_breakout"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class CoverPreviousSwingHighBreakout(CoverStrategy):
    """COVER when the close crosses from at-or-below to above the latest confirmed swing high.

    Resistance ``R`` is the latest fractal swing high (strictly above the highs of the ``strength``
    bars on each side) found in the ``lookback`` completed bars *before* the signal bar, so ``R``
    is fixed by bars that had already closed and a pivot is used only once its right-hand bars have
    closed.  Signal (edge)::

        close[t-1] <= R  AND  close[t] > R  ->  COVER

    The level is a structural pivot, not the highest high of a window: it is generally lower than
    the donchian_channel_breakout / cover_donchian_breakout level whenever a higher peak lies
    further back, and a pivot confirmed after a higher peak replaces it.  Finite window, no
    persisted state.  Needs ``lookback + 1`` completed bars (51 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="price_action",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=51,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "strength", ParameterKind.INT, 2, 1, 10,
                    description="Bars on each side of a swing high. Default 2.",
                ),
                ParameterSpec(
                    "lookback", ParameterKind.INT, 50, 10, 300,
                    description="Completed bars before the signal bar searched for the pivot. Default 50.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "COVER when the close crosses from at-or-below to above the latest confirmed swing "
            "high."
        ),
        source=(
            "Robert Edwards and John Magee, Technical Analysis of Stock Trends (resistance and "
            "breakout); John Murphy, Technical Analysis of the Financial Markets (1999), ch. 4; "
            "swing highs as Bill Williams fractals (Trading Chaos). Same level as the "
            "resistance_breakout BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "The most recent pivot is used even if an older, higher peak exists; no pivot in the "
            "lookback means no signal.",
            "Pivot bars and the latest two bars must be adjacent; regular session only; stale "
            "data never signals.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        lookback = cls.spec.parameter_schema.validate(params)["lookback"]
        assert isinstance(lookback, int)
        return lookback + 1

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        strength, lookback = validated["strength"], validated["lookback"]
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
        triggered = (
            resistance is not None
            and bars[-2].close <= resistance
            and bars[-1].close > resistance
        )
        return finish(self.spec, context, bars[-1], state, triggered)
