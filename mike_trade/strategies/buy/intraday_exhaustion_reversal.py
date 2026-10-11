"""Long entry on downside exhaustion and bullish reversal."""

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
    blocked,
    closed_bars,
    finish,
    gate,
    insufficient,
)
from mike_trade.strategies.indicators.patterns import contiguous
from mike_trade.strategies.indicators.windows import true_ranges

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_TREND_BARS",
    "DEFAULT_LOOKBACK",
    "DEFAULT_RANGE_MULTIPLE",
    "DEFAULT_VOLUME_MULTIPLE",
    "VOLUME_PERIOD",
    "ATR_PERIOD",
    "WARMUP_BARS",
    "IntradayExhaustionReversalBuy",
]

STRATEGY_ID = "intraday_exhaustion_reversal"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1

DEFAULT_TREND_BARS = 4
DEFAULT_LOOKBACK = 10
DEFAULT_RANGE_MULTIPLE = Decimal("1.5")
DEFAULT_VOLUME_MULTIPLE = Decimal("1.5")
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
            description="Number of bars preceding exhaustion bar to verify prior decline. Default 4.",
        ),
        ParameterSpec(
            "lookback",
            ParameterKind.INT,
            DEFAULT_LOOKBACK,
            5,
            50,
            description="Lookback window of bars preceding exhaustion bar to verify new low. Default 10.",
        ),
        ParameterSpec(
            "range_multiple",
            ParameterKind.DECIMAL,
            DEFAULT_RANGE_MULTIPLE,
            Decimal("1.0"),
            Decimal("5.0"),
            description="Minimum range multiple of baseline ATR required on exhaustion bar. Default 1.5.",
        ),
        ParameterSpec(
            "volume_multiple",
            ParameterKind.DECIMAL,
            DEFAULT_VOLUME_MULTIPLE,
            Decimal("1.0"),
            Decimal("5.0"),
            description="Minimum volume multiple of baseline volume SMA required on exhaustion bar. Default 1.5.",
        ),
    )
)

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="price_action",
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
        "BUY when a prior decline is met by an exhaustion bar (t-1) that sets a new lookback low "
        "on expanded range (>= range_multiple * ATR) and volume (>= volume_multiple * SMA) but "
        "closes off its low (lower shadow/absorption), followed immediately by a completed "
        "bullish confirmation bar (t) closing above the exhaustion midpoint."
    ),
    source=(
        "Concept: Climactic reversal and selling climax exhaustion (Al Brooks, Reading Price Charts "
        "Bar by Bar, 2009; Richard Wyckoff / Tom Williams, Master the Markets, 1993; Thomas Bulkowski, "
        "Encyclopedia of Candlestick Charts, 2008); Type B. Trigger: Engineering choice (Type C) "
        "combining point-in-time prior decline, ATR range expansion, volume surge, lower-wick "
        "absorption, and completed next-bar bullish midpoint confirmation."
    ),
    execution_limitations=(
        "Entry signal only; no stops, sizing, targets, or orders are managed by this class.",
        "Requires completed adjacent regular-session M1 bars; gaps return INSUFFICIENT_DATA.",
        "Edge-triggered on the confirmation bar; replay of the same bar is suppressed.",
    ),
    reason_codes=REASON_CODES,
)


class IntradayExhaustionReversalBuy(BuyStrategy):
    """BUY when downside exhaustion with absorption volume is confirmed by a bullish reversal bar."""

    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        p = cls.spec.parameter_schema.validate(params)
        trend = p["trend_bars"]
        lookback = p["lookback"]
        assert isinstance(trend, int) and isinstance(lookback, int)
        return _required_bars(trend, lookback)

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        p = self.spec.parameter_schema.validate(params)
        trend = p["trend_bars"]
        lookback = p["lookback"]
        range_mult = p["range_multiple"]
        vol_mult = p["volume_multiple"]
        assert isinstance(trend, int) and isinstance(lookback, int)
        assert isinstance(range_mult, Decimal) and isinstance(vol_mult, Decimal)

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
        e = bars[-2]
        c = bars[-1]

        # 1. Prior downtrend check: close before exhaustion was lower than close trend_bars earlier
        if pre.close >= bars[-3 - trend].close:
            return finish(self.spec, context, c, state, False)

        # 2. Lookback channel extreme: exhaustion low was strictly below preceding lookback lows
        prior_lows = [b.low for b in bars[-2 - lookback : -2]]
        if e.low >= min(prior_lows):
            return finish(self.spec, context, c, state, False)

        # 3. Baseline volume and ATR calculated on bars strictly preceding the exhaustion bar
        vol_baseline_bars = bars[-2 - VOLUME_PERIOD : -2]
        avg_vol = sum((b.volume for b in vol_baseline_bars), Decimal("0")) / Decimal(VOLUME_PERIOD)
        if avg_vol <= 0 or e.volume < vol_mult * avg_vol:
            return finish(self.spec, context, c, state, False)

        atr_bars = bars[-2 - ATR_PERIOD - 1 : -2]
        trs = true_ranges(atr_bars)
        avg_tr = sum(trs, Decimal("0")) / Decimal(ATR_PERIOD)
        e_spread = e.high - e.low
        if avg_tr <= 0 or e_spread < range_mult * avg_tr:
            return finish(self.spec, context, c, state, False)

        # 4. Absorption / rejection on exhaustion bar: closed off its lows (lower wick >= 35% of spread)
        if (e.close - e.low) < Decimal("0.35") * e_spread:
            return finish(self.spec, context, c, state, False)

        # 5. Bullish confirmation on bar c (bars[-1]):
        # c is bullish (close > open and close > e.close) and closes above the midpoint of the exhaustion candle
        e_mid = (e.high + e.low) / Decimal("2")
        bullish_confirm = (c.close > c.open) and (c.close > e.close) and (c.close > e_mid)

        return finish(self.spec, context, c, state, bullish_confirm)
