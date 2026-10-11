"""Long exit on Wyckoff-style Buying Climax followed by Automatic Reaction confirmation."""

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
)
from mike_trade.strategies.indicators.patterns import contiguous
from mike_trade.strategies.indicators.windows import true_ranges
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_TREND_BARS",
    "DEFAULT_LOOKBACK",
    "DEFAULT_CLIMAX_MULTIPLE",
    "DEFAULT_SPREAD_MULTIPLE",
    "VOLUME_PERIOD",
    "ATR_PERIOD",
    "WARMUP_BARS",
    "VolumeClimaxExitSell",
]

STRATEGY_ID = "volume_climax_exit"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1

DEFAULT_TREND_BARS = 5
DEFAULT_LOOKBACK = 15
DEFAULT_CLIMAX_MULTIPLE = Decimal("2.5")
DEFAULT_SPREAD_MULTIPLE = Decimal("1.25")
VOLUME_PERIOD = 20
ATR_PERIOD = 14


def _required_bars(trend_bars: int, lookback: int) -> int:
    return max(VOLUME_PERIOD + 2, ATR_PERIOD + 3, lookback + 2, trend_bars + 3)


WARMUP_BARS = _required_bars(DEFAULT_TREND_BARS, DEFAULT_LOOKBACK)

_SCHEMA = ParameterSchema(
    (
        ParameterSpec(
            "trend_bars",
            ParameterKind.INT,
            DEFAULT_TREND_BARS,
            2,
            30,
            description="Number of bars preceding climax bar to verify prior advance. Default 5.",
        ),
        ParameterSpec(
            "lookback",
            ParameterKind.INT,
            DEFAULT_LOOKBACK,
            5,
            50,
            description="Lookback window of bars preceding climax bar to verify new swing high. Default 15.",
        ),
        ParameterSpec(
            "climax_multiple",
            ParameterKind.DECIMAL,
            DEFAULT_CLIMAX_MULTIPLE,
            Decimal("1.5"),
            Decimal("10.0"),
            description="Minimum volume multiple over baseline volume SMA on the climax bar. Default 2.5.",
        ),
        ParameterSpec(
            "spread_multiple",
            ParameterKind.DECIMAL,
            DEFAULT_SPREAD_MULTIPLE,
            Decimal("1.0"),
            Decimal("5.0"),
            description="Minimum spread multiple over baseline ATR on the climax bar. Default 1.25.",
        ),
    )
)

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.SELL,
    category="technical",
    sub_category="volume",
    reference_symbols=(),
    required_timeframes=(TIMEFRAME,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=True,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=TIMEFRAME,
    parameter_schema=_SCHEMA,
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "SELL (exit long) when an uptrend terminates in a Wyckoff Buying Climax bar (t-1) that sets a "
        "new swing high on climactic volume (>= climax_multiple * volume SMA) and wide spread (>= spread_multiple * ATR) "
        "failing to close at its high, followed immediately by an Automatic Reaction bar (t) confirming "
        "distribution by closing down below the climax bar's midpoint."
    ),
    source=(
        "Concept: Wyckoff Phase A Buying Climax (BC) and Automatic Reaction (AR) (Richard Wyckoff, "
        "The Richard D. Wyckoff Method of Trading and Investing in Stocks, 1931; Tom Williams, Master "
        "the Markets, 1993; Jim Forte, Anatomy of a Trading Range); Type B. Trigger: Engineering choice "
        "(Type C) combining point-in-time prior advance, new lookback high, surge volume multiple over "
        "preceding volume SMA, spread expansion over ATR, upper rejection shadow, and completed next-bar "
        "reaction breakdown."
    ),
    execution_limitations=(
        "Advisory long exit only; no short positions or orders are owned by this class.",
        "Requires completed adjacent regular-session M1 bars; gaps return INSUFFICIENT_DATA.",
        "Edge-triggered on the Automatic Reaction bar; repeat signals on continued decline are suppressed.",
    ),
    reason_codes=REASON_CODES,
)


class VolumeClimaxExitSell(SellRule):
    """SELL long position on Wyckoff Buying Climax confirmed by Automatic Reaction."""

    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        p = cls.spec.parameter_schema.validate(params)
        trend = p["trend_bars"]
        lookback = p["lookback"]
        assert isinstance(trend, int) and isinstance(lookback, int)
        return _required_bars(trend, lookback)

    def evaluate_trigger(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        p = self.spec.parameter_schema.validate(params)
        trend = p["trend_bars"]
        lookback = p["lookback"]
        climax_mult = p["climax_multiple"]
        spread_mult = p["spread_multiple"]
        assert isinstance(trend, int) and isinstance(lookback, int)
        assert isinstance(climax_mult, Decimal) and isinstance(spread_mult, Decimal)

        needed = _required_bars(trend, lookback)
        bars = closed_bars(self.spec, context, needed)
        if bars is None:
            return insufficient(self.spec, context)

        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)

        if not contiguous(bars[-needed:]):
            return insufficient(self.spec, context)

        pre = bars[-3]
        climax = bars[-2]
        reaction = bars[-1]

        # 1. Prior advance requirement: close at t-2 was higher than close trend_bars earlier
        if pre.close <= bars[-3 - trend].close:
            return finish(self.spec, context, reaction, state, False)

        # 2. Climax bar must set a new swing high over the preceding lookback window
        prior_highs = [b.high for b in bars[-2 - lookback : -2]]
        if climax.high <= max(prior_highs):
            return finish(self.spec, context, reaction, state, False)

        # 3. Climactic volume: volume >= climax_multiple * baseline volume SMA
        vol_baseline_bars = bars[-2 - VOLUME_PERIOD : -2]
        avg_vol = sum((b.volume for b in vol_baseline_bars), Decimal("0")) / Decimal(VOLUME_PERIOD)
        if avg_vol <= 0 or climax.volume < climax_mult * avg_vol:
            return finish(self.spec, context, reaction, state, False)

        # 4. Wide price spread: spread >= spread_multiple * baseline ATR
        atr_bars = bars[-2 - ATR_PERIOD - 1 : -2]
        trs = true_ranges(atr_bars)
        avg_atr = sum(trs, Decimal("0")) / Decimal(ATR_PERIOD)
        climax_spread = climax.high - climax.low
        if avg_atr <= 0 or climax_spread < spread_mult * avg_atr:
            return finish(self.spec, context, reaction, state, False)

        # 5. Upper shadow / distribution: failed to close at the high
        upper_shadow = climax.high - max(climax.open, climax.close)
        failed_to_sustain_high = (
            (upper_shadow >= Decimal("0.35") * climax_spread)
            or ((climax.close - climax.low) <= Decimal("0.55") * climax_spread)
        )
        if not failed_to_sustain_high:
            return finish(self.spec, context, reaction, state, False)

        # 6. Automatic Reaction on latest bar:
        # Bearish candle (close < open and close < climax.close) that closes below climax midpoint
        climax_mid = (climax.high + climax.low) / Decimal("2")
        reaction_confirmed = (
            (reaction.close < reaction.open)
            and (reaction.close < climax.close)
            and (reaction.close < climax_mid)
        )

        return finish(self.spec, context, reaction, state, reaction_confirmed)
