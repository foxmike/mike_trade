"""COVER on the bar that confirms a higher swing low inside a higher-high structure."""

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

STRATEGY_ID = "cover_higher_high_higher_low"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class CoverHigherHighHigherLow(CoverStrategy):
    """COVER on the bar that confirms a swing low above the previous swing low while the last two
    swing highs are also rising (Dow-theory uptrend structure).

    Swing points are fractals with ``strength`` bars on each side (strict extremes) over the last
    ``lookback + 1`` completed bars.  A swing low at bar ``c`` is only knowable once ``strength``
    bars have closed after it, so the signal bar is exactly ``t = c + strength``: the first
    completed bar that proves the higher low.  Required::

        HL: low pivot[-1] > low pivot[-2]     HH: high pivot[-1] > high pivot[-2]
        low pivot[-1] confirmed on the latest bar (index t - strength)

    Both the higher low and the higher high are therefore already confirmed when the signal fires,
    and the signal fires once, on the confirming bar.  Finite window, no persisted state.  Needs
    ``lookback + 1`` completed bars (51 by default).
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
                    description="Bars on each side of a swing point. Default 2.",
                ),
                ParameterSpec(
                    "lookback", ParameterKind.INT, 50, 10, 300,
                    description="Completed bars searched for swing points. Default 50.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "COVER when a newly confirmed swing low is above the previous swing low and the "
            "latest swing high is above the previous swing high."
        ),
        source=(
            "Dow Theory as presented in John Murphy, Technical Analysis of the Financial Markets "
            "(1999), ch. 4 (an uptrend is a series of rising peaks and troughs); swing points as "
            "Bill Williams fractals (Trading Chaos). Same structure as the higher_high_higher_low "
            "BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "The signal is delayed by `strength` bars by design: a pivot is not known earlier "
            "without look-ahead.",
            "Every pivot's 2*strength+1 bars and the latest two bars must be adjacent; regular "
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
        triggered = (
            len(highs) >= 2 and len(lows) >= 2
            and lows[-1][0] == len(window) - 1 - strength
            and lows[-1][1] > lows[-2][1]
            and highs[-1][1] > highs[-2][1]
        )
        return finish(self.spec, context, bars[-1], state, triggered)
