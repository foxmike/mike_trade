"""Long exit on bearish key reversal with volatility expansion."""

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
    "DEFAULT_ATR_PERIOD",
    "DEFAULT_EXPANSION_MULTIPLE",
    "WARMUP_BARS",
    "VolatilityExpansionReversalSell",
]

STRATEGY_ID = "volatility_expansion_reversal"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1

DEFAULT_TREND_BARS = 5
DEFAULT_ATR_PERIOD = 14
DEFAULT_EXPANSION_MULTIPLE = Decimal("1.5")


def _required_bars(trend_bars: int, atr_period: int) -> int:
    return max(atr_period + 1, trend_bars) + 2


WARMUP_BARS = _required_bars(DEFAULT_TREND_BARS, DEFAULT_ATR_PERIOD)

_SCHEMA = ParameterSchema(
    (
        ParameterSpec(
            "trend_bars",
            ParameterKind.INT,
            DEFAULT_TREND_BARS,
            2,
            30,
            description="Number of bars preceding reversal bar to verify prior uptrend. Default 5.",
        ),
        ParameterSpec(
            "atr_period",
            ParameterKind.INT,
            DEFAULT_ATR_PERIOD,
            2,
            50,
            description="Lookback window for baseline finite ATR. Default 14.",
        ),
        ParameterSpec(
            "expansion_multiple",
            ParameterKind.DECIMAL,
            DEFAULT_EXPANSION_MULTIPLE,
            Decimal("1.0"),
            Decimal("5.0"),
            description="Minimum True Range expansion multiple over baseline ATR. Default 1.5.",
        ),
    )
)

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.SELL,
    category="technical",
    sub_category="volatility",
    reference_symbols=(),
    required_timeframes=(TIMEFRAME,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=False,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=TIMEFRAME,
    parameter_schema=_SCHEMA,
    persistent_state_keys=(STATE_LAST_SIGNAL_BAR,),
    methodology=(
        "SELL (exit long) when an uptrend (close[t-1] > close[t-1-trend_bars]) produces an expanded "
        "volatility key reversal bar at t whose True Range is >= expansion_multiple * ATR(14) of "
        "prior bars, trades above prior high (high[t] > high[t-1]), but is rejected to close "
        "strictly below prior low (close[t] < low[t-1]) in the lower 35% of its range."
    ),
    source=(
        "Concept: Outside key reversals on volatility expansion (Toby Crabel, Day Trading with Short "
        "Term Price Patterns, 1990; Robert D. Edwards & John Magee, Technical Analysis of Stock Trends; "
        "Perry Kaufman, Trading Systems and Methods, 5th ed.; John J. Murphy, Technical Analysis of the "
        "Financial Markets); Type B. Trigger: Engineering choice (Type C) measuring point-in-time prior "
        "uptrend, True Range expansion relative to preceding finite ATR, and full outside-bar breakdown."
    ),
    execution_limitations=(
        "Advisory long exit only; no short positions or orders are owned by this class.",
        "Requires completed adjacent regular-session M1 bars; gaps return INSUFFICIENT_DATA.",
        "Edge-triggered on the reversal bar; does not signal repeatedly on subsequent continuation.",
    ),
    reason_codes=REASON_CODES,
)


class VolatilityExpansionReversalSell(SellRule):
    """SELL long position on a bearish outside key reversal with True Range volatility expansion."""

    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        p = cls.spec.parameter_schema.validate(params)
        trend = p["trend_bars"]
        atr_p = p["atr_period"]
        assert isinstance(trend, int) and isinstance(atr_p, int)
        return _required_bars(trend, atr_p)

    def evaluate_trigger(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        p = self.spec.parameter_schema.validate(params)
        trend = p["trend_bars"]
        atr_p = p["atr_period"]
        mult = p["expansion_multiple"]
        assert isinstance(trend, int) and isinstance(atr_p, int)
        assert isinstance(mult, Decimal)

        needed = _required_bars(trend, atr_p)
        bars = closed_bars(self.spec, context, needed)
        if bars is None:
            return insufficient(self.spec, context)

        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)

        if not contiguous(bars[-needed:]):
            return insufficient(self.spec, context)

        latest = bars[-1]
        prior = bars[-2]

        # 1. Prior uptrend: close at t-1 was higher than close trend_bars earlier
        if prior.close <= bars[-2 - trend].close:
            return finish(self.spec, context, latest, state, False)

        # 2. Baseline ATR(atr_period) calculated up to prior bar (bars[-2])
        atr_bars = bars[-1 - atr_p - 1 : -1]
        trs = true_ranges(atr_bars)
        avg_atr = sum(trs, Decimal("0")) / Decimal(atr_p)
        if avg_atr <= 0:
            return finish(self.spec, context, latest, state, False)

        # 3. Volatility expansion on latest bar: True Range >= mult * avg_atr
        curr_tr = max(
            latest.high - latest.low,
            abs(latest.high - prior.close),
            abs(latest.low - prior.close),
        )
        if curr_tr < mult * avg_atr:
            return finish(self.spec, context, latest, state, False)

        # 4. Bearish Outside Key Reversal structure:
        # Tested higher (high > prior.high), but rejected completely below prior low (close < prior.low),
        # closed bearish (close < open), and closed in the lower 35% of its range
        spread = latest.high - latest.low
        tested_higher = latest.high > prior.high
        rejected_lower = latest.close < prior.low
        bearish_candle = latest.close < latest.open
        settled_at_low = (
            (latest.close - latest.low) <= Decimal("0.35") * spread if spread > 0 else True
        )

        triggered = tested_higher and rejected_lower and bearish_candle and settled_at_low
        return finish(self.spec, context, latest, state, triggered)
