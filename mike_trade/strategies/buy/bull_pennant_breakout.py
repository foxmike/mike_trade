"""Bull pennant breakout BUY: a sharp advance, a short converging consolidation, a first close
above the descending upper boundary."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    BuyStrategy,
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

STRATEGY_ID = "bull_pennant_breakout"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
PERCENT = Decimal(100)


class BullPennantBreakoutBuy(BuyStrategy):
    """BUY when the close first rises above the descending upper boundary of a pennant.

    Everything is read from completed bars *before* the signal bar ``t`` (the base window); swing
    points are fractals with ``strength`` bars each side, so each was confirmed by bars that had
    already closed.

    * **Flagpole.** The pole top P is the latest bar with the highest high of the base window. The
      lowest low within ``pole_bars`` bars up to P is at least ``pole_pct`` percent below P's high.
    * **Pennant.** The ``min_pennant_bars``..``max_pennant_bars`` bars after P. Upper boundary: the
      last two swing highs at or after P, strictly *falling*; lower boundary: the last two swing
      lows after P, strictly *rising*. Both lines are extended to the signal bar, where the upper
      line must still be above the lower one (the breakout comes before the apex).
    * **Invalidation.** The lowest low of the pennant retraces no more than ``max_retrace`` of the
      pole height; no close since the first upper pivot was above the upper line; no close since the
      first lower pivot was below the lower line.
    * **Breakout.** ``close[t]`` is above the extended upper line (the first such close).

    A rectangular flag (``bull_flag_breakout``) has no converging boundaries, and a plain
    ``triangle_breakout`` has neither a flagpole nor a duration limit; a valid pennant breakout may
    also satisfy the latter. Finite window, no persisted state beyond ``last_signal_bar``. Needs
    ``lookback + 1`` completed bars (61 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.BUY,
        category="technical",
        sub_category="price_action",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=61,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "strength", ParameterKind.INT, 1, 1, 5,
                    description="Bars on each side of a swing point. Default 1.",
                ),
                ParameterSpec(
                    "lookback", ParameterKind.INT, 60, 20, 300,
                    description="Completed bars before the signal bar searched for the pole top. Default 60.",
                ),
                ParameterSpec(
                    "pole_bars", ParameterKind.INT, 10, 3, 60,
                    description="Maximum bars of the advance up to the pole top. Default 10.",
                ),
                ParameterSpec(
                    "pole_pct", ParameterKind.DECIMAL, Decimal("1.5"), Decimal("0.2"), Decimal(30),
                    description="Minimum advance, pole low to pole-top high, percent of the low. Default 1.5.",
                ),
                ParameterSpec(
                    "min_pennant_bars", ParameterKind.INT, 6, 4, 60,
                    description="Minimum consolidation bars after the pole top. Default 6.",
                ),
                ParameterSpec(
                    "max_pennant_bars", ParameterKind.INT, 20, 6, 120,
                    description="Maximum consolidation bars after the pole top. Default 20.",
                ),
                ParameterSpec(
                    "max_retrace", ParameterKind.DECIMAL, Decimal("0.5"), Decimal("0.1"), Decimal("0.9"),
                    description="Maximum fraction of the pole retraced by the pennant's lowest low. Default 0.5.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "BUY when the close first rises above the falling upper boundary of a bull pennant: a "
            "sharp advance (flagpole), then a short consolidation whose swing highs fall and swing "
            "lows rise (converging boundaries) without retracing more than max_retrace of the pole."
        ),
        source=(
            "Pattern concept (published chart-pattern literature): a pennant is a short, small "
            "symmetrical triangle that follows a sharp move (the flagpole) and resolves in the "
            "direction of that move - Thomas Bulkowski, Encyclopedia of Chart Patterns (Pennants); "
            "John J. Murphy, Technical Analysis of the Financial Markets (flags and pennants); "
            "Edwards and Magee, Technical Analysis of Stock Trends. Published descriptions are "
            "qualitative about size and duration, so pole_pct, the pole and pennant bar limits and "
            "max_retrace are this library's intraday engineering choices, not the authors' "
            "published daily-chart thresholds."
        ),
        execution_limitations=(
            "Entry signal only: no stops, sizing, targets, or orders.",
            "Intraday adaptation of a daily-chart pattern on M1 bars; two-point boundaries through "
            "the last two swing highs and the last two swing lows after the pole top.",
            "Pole and pennant bars must be consecutive completed bars; a hole yields "
            "INSUFFICIENT_DATA. Regular session only; stale data never signals.",
            "Distinct from bull_flag_breakout (no converging lines), triangle_breakout and "
            "ascending_triangle_breakout (no flagpole, no duration limit); a pennant breakout can also "
            "satisfy triangle_breakout.",
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
        v = self.spec.parameter_schema.validate(params)
        strength, lookback, pole_bars = v["strength"], v["lookback"], v["pole_bars"]
        pole_pct, shortest, longest = v["pole_pct"], v["min_pennant_bars"], v["max_pennant_bars"]
        retrace = v["max_retrace"]
        assert isinstance(strength, int) and isinstance(lookback, int) and isinstance(pole_bars, int)
        assert isinstance(shortest, int) and isinstance(longest, int)
        assert isinstance(pole_pct, Decimal) and isinstance(retrace, Decimal)
        require_param(shortest <= longest, "min_pennant_bars <= max_pennant_bars", "min_pennant_bars")
        require_param(lookback > longest + 1, "lookback > max_pennant_bars + 1", "lookback")
        require_param(shortest >= 4 * strength, "min_pennant_bars >= 4 * strength", "min_pennant_bars")
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
        base = window[:last]
        top = max(range(len(base)), key=lambda i: (base[i].high, i))
        pennant = base[top + 1:]
        triggered = False
        if shortest <= len(pennant) <= longest:
            start = max(0, top - pole_bars)
            if not contiguous(window[start:]):
                return insufficient(self.spec, context)
            peak = base[top].high
            floor = min(bar.low for bar in base[start:top + 1])
            height = peak - floor
            if (
                height >= pole_pct / PERCENT * floor
                and peak - min(bar.low for bar in pennant) <= retrace * height
            ):
                highs, lows = swing_pivots(base, strength)
                upper = [(i, p) for i, p in highs if i >= top]
                lower = [(i, p) for i, p in lows if i > top]
                if len(upper) >= 2 and len(lower) >= 2:
                    (hx1, hy1), (hx2, hy2) = upper[-2], upper[-1]
                    (lx1, ly1), (lx2, ly2) = lower[-2], lower[-1]
                    if hy2 < hy1 and ly2 > ly1:
                        def top_line(x: int) -> Decimal:
                            return line_value(hx1, hy1, hx2, hy2, x)

                        def bottom_line(x: int) -> Decimal:
                            return line_value(lx1, ly1, lx2, ly2, x)

                        converging = top_line(last) > bottom_line(last)  # breakout before the apex
                        held = all(window[x].close >= bottom_line(x) for x in range(lx1, last))
                        triggered = (
                            converging and held and first_close_above(window, top_line, hx1)
                        )
        return finish(self.spec, context, bars[-1], state, triggered)
