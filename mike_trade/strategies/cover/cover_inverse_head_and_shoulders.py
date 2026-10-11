"""COVER when the close breaks above the neckline of an inverse head and shoulders."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
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

STRATEGY_ID = "cover_inverse_head_and_shoulders"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
PERCENT = Decimal(100)


class CoverInverseHeadAndShoulders(CoverStrategy):
    """COVER when the close first rises above the neckline of the last three swing lows.

    Swing points are fractals with ``strength`` bars each side computed on the completed bars
    *before* the signal bar, so the left shoulder LS, head H and right shoulder RS (the last three
    swing lows) were all confirmed by bars that had already closed.  H must be below both shoulders
    by at least ``min_head_depth_pct`` of the lower shoulder and the shoulders may differ by at most
    ``shoulder_tolerance_pct``.  The neckline is the straight line through the highest swing high
    between LS and H and the highest between H and RS, extended to the signal bar.  Signal: the
    close is above the neckline and no close since RS had been.  The signal bar is the
    neckline-breaking bar; nothing is back-dated.  Finite window, no persisted state.  Needs
    ``lookback + 1`` completed bars (151 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="price_action",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=151,
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
                    "lookback", ParameterKind.INT, 150, 30, 500,
                    description="Completed bars searched before the signal bar. Default 150.",
                ),
                ParameterSpec(
                    "shoulder_tolerance_pct", ParameterKind.DECIMAL, Decimal(1), Decimal("0.1"),
                    Decimal(20),
                    description="Maximum shoulder-low difference, percent. Default 1.",
                ),
                ParameterSpec(
                    "min_head_depth_pct", ParameterKind.DECIMAL, Decimal("0.3"), Decimal("0.05"),
                    Decimal(20),
                    description="Minimum depth of the head below the lower shoulder, percent. Default 0.3.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "COVER when the close crosses above the neckline through the two swing highs of a "
            "formed inverse head and shoulders (head lowest, shoulders alike)."
        ),
        source=(
            "Edwards and Magee, Technical Analysis of Stock Trends; Thomas Bulkowski, "
            "Encyclopedia of Chart Patterns, Head-and-Shoulders Bottom (three lows with the "
            "middle lowest, neckline through the two peaks, close above it confirms); John "
            "Murphy, Technical Analysis of the Financial Markets, ch. 7. Same pattern as the "
            "inverse_head_and_shoulders BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Shoulder and head thresholds are explicit parameters because published descriptions "
            "are qualitative. A prior downtrend and a volume pattern are not required.",
            "Pattern bars from LS to the signal bar must be consecutive completed bars; regular "
            "session only; stale data never signals.",
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
        tolerance, depth = validated["shoulder_tolerance_pct"], validated["min_head_depth_pct"]
        assert isinstance(strength, int) and isinstance(lookback, int)
        assert isinstance(tolerance, Decimal) and isinstance(depth, Decimal)
        require_param(lookback >= 12 * strength, "lookback >= 12 * strength", "lookback")
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
        if len(lows) >= 3:
            (il, left), (ih, head), (ir, right) = lows[-3:]
            if not contiguous(window[il:]):
                return insufficient(self.spec, context)
            shoulder = min(left, right)
            left_peaks = [(i, p) for i, p in highs if il < i < ih]
            right_peaks = [(i, p) for i, p in highs if ih < i < ir]
            if (
                left_peaks and right_peaks
                and head <= shoulder * (1 - depth / PERCENT)
                and abs(left - right) <= tolerance / PERCENT * shoulder
            ):
                x1, y1 = max(left_peaks, key=lambda item: item[1])
                x2, y2 = max(right_peaks, key=lambda item: item[1])
                triggered = first_close_above(
                    window, lambda x: line_value(x1, y1, x2, y2, x), ir + 1
                )
        return finish(self.spec, context, bars[-1], state, triggered)
