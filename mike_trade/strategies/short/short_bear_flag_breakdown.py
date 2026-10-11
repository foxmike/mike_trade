"""SHORT when a shallow upward flag after a steep decline breaks below flag support."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.base import (
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    ShortStrategy,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
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

STRATEGY_ID = "short_bear_flag_breakdown"
TIMEFRAME = CandleTimeframe.M1
PERCENT = Decimal(100)


class ShortBearFlagBreakdown(ShortStrategy):
    """A falling pole, a limited upward retracement, then a close below flag lows."""

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version="1.0.0",
        side=StrategyRole.SHORT,
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
                    "lookback",
                    ParameterKind.INT,
                    60,
                    20,
                    300,
                    description="Prior bars searched for the pole low. Default 60.",
                ),
                ParameterSpec(
                    "pole_bars",
                    ParameterKind.INT,
                    15,
                    3,
                    100,
                    description="Maximum decline bars to pole low. Default 15.",
                ),
                ParameterSpec(
                    "pole_pct",
                    ParameterKind.DECIMAL,
                    Decimal(1),
                    Decimal("0.1"),
                    Decimal(30),
                    description="Minimum pole decline percentage. Default 1.",
                ),
                ParameterSpec(
                    "min_flag_bars",
                    ParameterKind.INT,
                    3,
                    2,
                    50,
                    description="Minimum corrective bars before breakdown. Default 3.",
                ),
                ParameterSpec(
                    "max_flag_bars",
                    ParameterKind.INT,
                    20,
                    3,
                    100,
                    description="Maximum corrective bars. Default 20.",
                ),
                ParameterSpec(
                    "max_retrace",
                    ParameterKind.DECIMAL,
                    Decimal("0.5"),
                    Decimal("0.1"),
                    Decimal("0.9"),
                    description="Maximum fraction of the pole retraced. Default 0.5.",
                ),
            )
        ),
        persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
        methodology=(
            "SHORT when a decline of at least pole_pct is followed by a limited upward "
            "corrective flag of min_flag_bars..max_flag_bars, retracing no more than "
            "max_retrace of the pole, and the completed close breaks below flag support."
        ),
        source=(
            "Thomas Bulkowski, Encyclopedia of Chart Patterns, Flags (steep pole and brief "
            "countertrend flag); John J. Murphy, Technical Analysis of the Financial Markets, "
            "chapter 7 (flags often retrace no more than half the pole). Bearish geometry "
            "adapted from the repository's bull_flag_breakout implementation."
        ),
        execution_limitations=(
            "Pole percentage and bar lengths are explicit parameters because the sources "
            "do not prescribe an M1 threshold; no volume filter or target is imposed.",
            "The flag must end above its first close, stay above the pole low, and remain "
            "below the permitted retracement; the full pole and flag must be consecutive.",
            "SHORT entry signal only; risk and execution belong to the engine.",
        ),
        reason_codes=REASON_CODES,
    )

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        lookback = cls.spec.parameter_schema.validate(params)["lookback"]
        assert isinstance(lookback, int)
        return lookback + 1

    def evaluate(
        self, context: MarketContext, params: Mapping[str, object], state: Mapping[str, object]
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        lookback, pole_bars, pole_pct = (
            values["lookback"],
            values["pole_bars"],
            values["pole_pct"],
        )
        min_flag, max_flag, retrace = (
            values["min_flag_bars"],
            values["max_flag_bars"],
            values["max_retrace"],
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
        window = bars[-lookback - 1 :]
        if reason is not None:
            return blocked(self.spec, context, reason)
        if not contiguous(window):
            return insufficient(self.spec, context)
        last = len(window) - 1
        bottom = min(range(last), key=lambda i: (window[i].low, -i))
        flag = window[bottom + 1 : last]
        triggered = False
        if min_flag <= len(flag) <= max_flag:
            start = max(0, bottom - pole_bars)
            if bottom - start < 2:
                return finish(self.spec, context, bars[-1], state, False)
            pole_high = max(bar.high for bar in window[start:bottom])
            pole_low = window[bottom].low
            height = pole_high - pole_low
            flag_high = max(bar.high for bar in flag)
            flag_low = min(bar.low for bar in flag)
            qualifies = (
                height >= pole_pct / PERCENT * pole_high
                and window[start].close > window[bottom].close
                and flag[-1].close > flag[0].close
                and flag_low > pole_low
                and flag_high - pole_low <= retrace * height
            )
            triggered = qualifies and window[last].close < flag_low
        return finish(self.spec, context, bars[-1], state, triggered)
