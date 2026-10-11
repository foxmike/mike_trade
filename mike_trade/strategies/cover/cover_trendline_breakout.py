"""COVER when the close breaks above a falling resistance trendline."""

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
from mike_trade.strategies.indicators.patterns import contiguous, first_close_above, line_value
from mike_trade.strategies.indicators.windows import swing_pivots

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_trendline_breakout"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class CoverTrendlineBreakout(CoverStrategy):
    """COVER when the close first rises above the line through the last two falling swing highs.

    Swing points are fractals with ``strength`` bars each side computed on the completed bars
    *before* the signal bar, so both pivots H1 (earlier) and H2 were confirmed by bars that had
    already closed.  H2 must be strictly lower than H1.  The trendline is the straight line through
    ``(x1, H1)`` and ``(x2, H2)`` (bar indexes), extended to the signal bar.  Signal: the close is
    above the line and no close from H1 to the previous bar was (the line was never closed through).
    Finite window, no persisted state.  Needs ``lookback + 1`` completed bars (101 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="price_action",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=101,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "strength", ParameterKind.INT, 2, 1, 10,
                    description="Bars on each side of a swing point. Default 2.",
                ),
                ParameterSpec(
                    "lookback", ParameterKind.INT, 100, 20, 400,
                    description="Completed bars searched before the signal bar. Default 100.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "COVER when the close crosses above the descending trendline through the last two "
            "swing highs."
        ),
        source=(
            "Robert Edwards and John Magee, Technical Analysis of Stock Trends (a downtrend line "
            "is drawn through descending peaks; a close through it is the signal); John Murphy, "
            "Technical Analysis of the Financial Markets (1999), ch. 4; StockCharts ChartSchool, "
            "Trendlines. Same line as the trendline_breakout BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Only two-point trendlines through consecutive swing highs; touches are not counted "
            "beyond that, and a close through the line before the signal bar disqualifies it.",
            "Bars from H1 to the signal bar must be consecutive completed bars; regular session "
            "only; stale data never signals.",
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
        highs, _ = swing_pivots(window[:-1], strength)
        triggered = False
        if len(highs) >= 2:
            (x1, y1), (x2, y2) = highs[-2], highs[-1]
            if not contiguous(window[x1:]):
                return insufficient(self.spec, context)
            if y2 < y1:
                triggered = first_close_above(window, lambda x: line_value(x1, y1, x2, y2, x), x1)
        return finish(self.spec, context, bars[-1], state, triggered)
