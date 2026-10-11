"""COVER when the close first breaks above a bull flag that follows a sharp advance."""

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
from mike_trade.strategies.indicators.patterns import contiguous

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_bull_flag_breakout"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
PERCENT = Decimal(100)


class CoverBullFlagBreakout(CoverStrategy):
    """COVER when the close first exceeds the flag high after a flagpole and a shallow, short flag.

    All structure is read from completed bars *before* the signal bar ``t``:

    * Pole top P: the latest bar with the highest high among the ``lookback`` bars before ``t``.
    * Flagpole: the lowest low within ``pole_bars`` bars up to P is at least ``pole_pct`` percent
      below P's high (height = P.high - that low).
    * Flag: the ``min_flag_bars``..``max_flag_bars`` bars strictly between P and ``t``; its lowest
      low retraces no more than ``max_retrace`` of the pole height.
    * Breakout: ``close[t]`` exceeds the flag's highest high (the upper boundary).

    This is the bull_flag_breakout BUY pattern with one added edge condition: the previous bar
    must not itself have closed above the high of the flag bars before it, so a continuation after
    a breakout does not signal again (the BUY can fire on consecutive bars).  The consolidation is
    fixed by bars that had already closed; the signal bar is the breakout bar.  Finite window, no
    persisted state.  Needs ``lookback + 1`` completed bars (61 by default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
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
                    "lookback", ParameterKind.INT, 60, 20, 300,
                    description="Completed bars searched for the pole top. Default 60.",
                ),
                ParameterSpec(
                    "pole_bars", ParameterKind.INT, 15, 3, 100,
                    description="Maximum bars of the advance up to the pole top. Default 15.",
                ),
                ParameterSpec(
                    "pole_pct", ParameterKind.DECIMAL, Decimal(1), Decimal("0.1"), Decimal(30),
                    description="Minimum advance low-to-top, percent. Default 1.",
                ),
                ParameterSpec(
                    "min_flag_bars", ParameterKind.INT, 3, 2, 50,
                    description="Minimum consolidation bars before a breakout counts. Default 3.",
                ),
                ParameterSpec(
                    "max_flag_bars", ParameterKind.INT, 20, 3, 100,
                    description="Maximum consolidation bars. Default 20.",
                ),
                ParameterSpec(
                    "max_retrace", ParameterKind.DECIMAL, Decimal("0.5"), Decimal("0.1"),
                    Decimal("0.9"),
                    description="Maximum fraction of the pole retraced. Default 0.5.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "COVER when the close first exceeds the high of a flag that follows a steep advance, "
            "lasts a limited time and retraces no more than the allowed share of the pole."
        ),
        source=(
            "Thomas Bulkowski, Encyclopedia of Chart Patterns, Flags (steep pole, short flag, "
            "upward breakout); Murphy, Technical Analysis of the Financial Markets, ch. 7 (flags "
            "retrace no more than about half the pole). Same pattern as the bull_flag_breakout BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Published descriptions give no minimum pole height, so pole_pct and the bar limits "
            "are parameters. Pennants and volume contraction are not used.",
            "Pole and flag bars must be consecutive completed bars; regular session only; stale "
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
        lookback, pole_bars, pole_pct = (
            validated["lookback"], validated["pole_bars"], validated["pole_pct"],
        )
        min_flag, max_flag, retrace = (
            validated["min_flag_bars"], validated["max_flag_bars"], validated["max_retrace"],
        )
        assert isinstance(lookback, int) and isinstance(pole_bars, int)
        assert isinstance(pole_pct, Decimal) and isinstance(retrace, Decimal)
        assert isinstance(min_flag, int) and isinstance(max_flag, int)
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
            if (
                height >= pole_pct / PERCENT * floor
                and peak - min(bar.low for bar in flag) <= retrace * height
            ):
                # first breakout only: the previous bar did not already close above the flag
                # high formed before it
                first = window[last - 1].close <= max(bar.high for bar in flag[:-1])
                triggered = first and window[last].close > max(bar.high for bar in flag)
        return finish(self.spec, context, bars[-1], state, triggered)
