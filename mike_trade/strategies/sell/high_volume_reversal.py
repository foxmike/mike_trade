"""High-volume reversal SELL strategy (long exit on high-volume bearish rejection).

A SELL signal fires edge-triggered on completed regular-session 1-minute bars when:
1. Prior Trend: Over the ``trend_bars`` completed bars preceding the reversal bar
   (ending at bar ``t - 1``), price was in an established uptrend:
       close[t - 1] > close[t - 1 - trend_bars]
2. Volume Expansion: Volume on the latest completed bar (``t``) expands to at least
   ``volume_multiple`` times the average volume of the preceding ``volume_period`` completed bars:
       volume[t] >= volume_multiple * SMA(volume, volume_period)[t - 1]
3. Bearish Rejection Candle: The latest completed bar (``t``) forms a decisive bearish candle:
       close[t] < open[t]
       close[t] < close[t - 1]
       (close[t] - low[t]) <= 0.5 * (high[t] - low[t])  (closed in the lower half of its range)
4. Edge Trigger: The prior bar (``t - 1``) did not itself show the high-volume bearish rejection
   (its own volume multiple against its own preceding baseline plus the same candle conditions).
   The prior bar's uptrend precondition is deliberately not re-tested, so a run of consecutive
   high-volume bearish bars in an extended decline yields at most one signal.

Distinct from:
- ``bearish_engulfing_exit``: 2-bar engulfing candlestick pattern; does not evaluate volume.
- ``shooting_star_reversal``: shooting star candle (small lower body, long upper shadow >= 2x body)
  confirmed by next-bar breakdown; does not evaluate volume.
- ``momentum_reversal``: multi-bar ROC reversing from positive to <= -2%; does not evaluate volume
  or single-bar candlestick structure.
- ``obv_breakdown``: cumulative On-Balance Volume channel breakdown.
"""

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
from mike_trade.strategies.buy._session import latest_session_bars
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    blocked,
    closed_bars,
    finish,
    gate,
    insufficient,
)
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_TREND_BARS",
    "DEFAULT_VOLUME_PERIOD",
    "DEFAULT_VOLUME_MULTIPLE",
    "WARMUP_BARS",
    "HighVolumeReversalSell",
]

STRATEGY_ID = "high_volume_reversal"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1

DEFAULT_TREND_BARS = 5
DEFAULT_VOLUME_PERIOD = 20
DEFAULT_VOLUME_MULTIPLE = Decimal("2.0")


def _required_bars(volume_period: int, trend_bars: int) -> int:
    return max(volume_period, trend_bars) + 2


WARMUP_BARS = _required_bars(DEFAULT_VOLUME_PERIOD, DEFAULT_TREND_BARS)

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
            "volume_period",
            ParameterKind.INT,
            DEFAULT_VOLUME_PERIOD,
            5,
            100,
            description="Lookback window of completed bars for baseline volume SMA. Default 20.",
        ),
        ParameterSpec(
            "volume_multiple",
            ParameterKind.DECIMAL,
            DEFAULT_VOLUME_MULTIPLE,
            Decimal("1.2"),
            Decimal("10.0"),
            description="Minimum volume multiple required on reversal bar. Default 2.0.",
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
        "SELL (exit long) when an uptrend (close[t-1] > close[t-1-trend_bars]) is met with a "
        "bearish reversal candle (close[t] < open[t], close[t] < close[t-1], and "
        "(close[t] - low[t]) <= 0.5 * (high[t] - low[t])) on elevated volume at least "
        "volume_multiple times the preceding volume_period volume SMA."
    ),
    source=(
        "Concept only: high-volume churn / climactic reversal after an advance (Wyckoff-style "
        "volume analysis); no published mechanical rule exists. Trigger: engineering choice "
        "(Type C) combining a prior-trend lookback, a volume multiple over the preceding volume "
        "SMA, and a lower-half bearish candle; every threshold is an engineering default."
    ),
    execution_limitations=(
        "Advisory long exit only; no short positions or orders are owned by this class.",
        "Requires completed adjacent regular-session M1 bars; gaps invalidate the evaluation.",
        "Edge-triggered on the reversal bar; does not re-signal repeatedly on continuing volume.",
    ),
    reason_codes=REASON_CODES,
)


class HighVolumeReversalSell(SellRule):
    """SELL long position on high-volume bearish reversal following an uptrend."""

    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        p = cls.spec.parameter_schema.validate(params)
        trend = p["trend_bars"]
        vol_p = p["volume_period"]
        assert isinstance(trend, int) and isinstance(vol_p, int)
        return _required_bars(vol_p, trend)

    def evaluate_trigger(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        p = self.spec.parameter_schema.validate(params)
        trend = p["trend_bars"]
        vol_p = p["volume_period"]
        vol_mult = p["volume_multiple"]
        assert isinstance(trend, int) and isinstance(vol_p, int)
        assert isinstance(vol_mult, Decimal)

        needed = _required_bars(vol_p, trend)
        bars = closed_bars(self.spec, context, needed)
        if bars is None:
            return insufficient(self.spec, context)

        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)

        if latest_session_bars(context, needed) is None:
            return insufficient(self.spec, context)

        # 1. Prior trend qualification: close at t-1 was higher than close at t - 1 - trend
        prior_uptrend = bars[-2].close > bars[-2 - trend].close
        if not prior_uptrend:
            return finish(self.spec, context, bars[-1], state, False)

        # 2. Volume expansion at latest bar (t)
        vol_window_latest = bars[-1 - vol_p : -1]
        sum_vol_latest = sum((b.volume for b in vol_window_latest), Decimal("0"))
        avg_vol_latest = sum_vol_latest / Decimal(vol_p)
        if avg_vol_latest <= 0:
            return finish(self.spec, context, bars[-1], state, False)

        volume_surged = bars[-1].volume >= vol_mult * avg_vol_latest

        # 3. Bearish rejection candle at latest bar (t)
        range_latest = bars[-1].high - bars[-1].low
        bearish_candle = (bars[-1].close < bars[-1].open) and (bars[-1].close < bars[-2].close)
        lower_half = (
            (bars[-1].close - bars[-1].low) <= range_latest * Decimal("0.5")
            if range_latest > 0
            else True
        )
        bearish_rejection = bearish_candle and lower_half

        # 4. Edge trigger: prior bar (t-1) was not already a qualifying high-volume reversal
        vol_window_prior = bars[-2 - vol_p : -2]
        sum_vol_prior = sum((b.volume for b in vol_window_prior), Decimal("0"))
        avg_vol_prior = sum_vol_prior / Decimal(vol_p)
        prior_surged = (bars[-2].volume >= vol_mult * avg_vol_prior) if avg_vol_prior > 0 else False
        prior_range = bars[-2].high - bars[-2].low
        prior_bearish = (
            (bars[-2].close < bars[-2].open)
            and (len(bars) >= 3 and bars[-2].close < bars[-3].close)
            and (
                (bars[-2].close - bars[-2].low) <= prior_range * Decimal("0.5")
                if prior_range > 0
                else True
            )
        )
        prior_active = prior_surged and prior_bearish

        triggered = volume_surged and bearish_rejection and (not prior_active)
        return finish(self.spec, context, bars[-1], state, triggered)
