"""COVER on the confirmation bar after a hammer that ends a decline."""

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
)
from mike_trade.strategies.indicators.windows import is_hammer

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_hammer_reversal"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1


class CoverHammerReversal(CoverStrategy):
    """COVER when a bar closes above the high of the immediately preceding confirmed-shape hammer.

    Hammer (bar ``h = t-1``): lower shadow ``>= shadow_ratio`` x real body (Nison: at least twice),
    upper shadow ``<= max_upper_shadow`` x range, non-zero range (shared ``is_hammer``).  Prior
    decline: ``close[h-1] < close[h-1-trend_bars]``.  Nison requires bullish confirmation, so the
    signal is the *next* bar, never the hammer itself: ``close[t] > high[h]``.  Before that close
    the hammer is unconfirmed and nothing fires.  Unlike the hammer_reversal BUY (which checks only
    the three pattern bars), the whole ``trend_bars`` decline window must also be consecutive
    bars.  Finite window, no persisted state.  Needs ``trend_bars + 3`` completed bars (8 by
    default).
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="price_action",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=8,
        completed_bars_only=True,
        requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(
            (
                ParameterSpec(
                    "trend_bars", ParameterKind.INT, 5, 2, 50,
                    description="Bars over which the pre-hammer close must have fallen. Default 5.",
                ),
                ParameterSpec(
                    "shadow_ratio", ParameterKind.DECIMAL, Decimal(2), Decimal(1), Decimal(10),
                    description="Minimum lower shadow as a multiple of the real body. Default 2.",
                ),
                ParameterSpec(
                    "max_upper_shadow", ParameterKind.DECIMAL, Decimal("0.1"), Decimal(0),
                    Decimal("0.5"),
                    description="Maximum upper shadow as a fraction of the bar's range. Default 0.1.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "COVER on the bar after a hammer (long lower shadow, small body at the top) that "
            "follows a decline, when that bar closes above the hammer's high."
        ),
        source=(
            "Steve Nison, Japanese Candlestick Charting Techniques (2nd ed., 2001): hammer (lower "
            "shadow at least twice the body, little or no upper shadow, after a decline) and the "
            "need for bullish confirmation; shadow thresholds quantified as in TA-Lib's "
            "CDLHAMMER. Same pattern and confirmation as the hammer_reversal BUY."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Nison's daily-bar candle adapted to M1 bars; confirmation is the close above the "
            "hammer's high (an objective reading of 'higher').",
            "The decline window, the hammer and the confirmation bar must be consecutive "
            "completed bars; regular session only; stale data never signals.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        trend = cls.spec.parameter_schema.validate(params)["trend_bars"]
        assert isinstance(trend, int)
        return trend + 3

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        trend, ratio, upper = (
            validated["trend_bars"], validated["shadow_ratio"], validated["max_upper_shadow"],
        )
        assert isinstance(trend, int) and isinstance(ratio, Decimal) and isinstance(upper, Decimal)
        reason = gate(self.spec, context, state)
        bars = closed_bars(self.spec, context, trend + 3)
        if bars is None:
            return insufficient(self.spec, context)
        if reason is not None:
            return blocked(self.spec, context, reason)
        window = bars[-(trend + 3):]
        if any(not adjacent(a, b) for a, b in zip(window, window[1:], strict=False)):
            return insufficient(self.spec, context)
        before, hammer, latest = window[-3], window[-2], window[-1]
        triggered = (
            is_hammer(hammer, ratio, upper)
            and before.close < window[-3 - trend].close
            and latest.close > hammer.high
        )
        return finish(self.spec, context, latest, state, triggered)
