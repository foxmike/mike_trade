"""Long entry on a bullish pullback with volume dry-up followed by volume-backed resumption."""

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

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_TREND_BARS",
    "DEFAULT_PULLBACK_BARS",
    "DEFAULT_VOLUME_PERIOD",
    "DEFAULT_DRY_UP_FACTOR",
    "WARMUP_BARS",
    "VolumeDryUpPullbackBuy",
]

STRATEGY_ID = "volume_dry_up_pullback"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1

DEFAULT_TREND_BARS = 5
DEFAULT_PULLBACK_BARS = 3
DEFAULT_VOLUME_PERIOD = 20
DEFAULT_DRY_UP_FACTOR = Decimal("0.70")


def _required_bars(trend_bars: int, pullback_bars: int, volume_period: int) -> int:
    return pullback_bars + max(volume_period, trend_bars) + 1


WARMUP_BARS = _required_bars(DEFAULT_TREND_BARS, DEFAULT_PULLBACK_BARS, DEFAULT_VOLUME_PERIOD)

_SCHEMA = ParameterSchema(
    (
        ParameterSpec(
            "trend_bars",
            ParameterKind.INT,
            DEFAULT_TREND_BARS,
            2,
            30,
            description="Number of bars prior to pullback verifying established uptrend. Default 5.",
        ),
        ParameterSpec(
            "pullback_bars",
            ParameterKind.INT,
            DEFAULT_PULLBACK_BARS,
            2,
            10,
            description="Number of corrective bars during which volume dry-up is evaluated. Default 3.",
        ),
        ParameterSpec(
            "volume_period",
            ParameterKind.INT,
            DEFAULT_VOLUME_PERIOD,
            5,
            100,
            description="Lookback window for baseline volume SMA. Default 20.",
        ),
        ParameterSpec(
            "dry_up_factor",
            ParameterKind.DECIMAL,
            DEFAULT_DRY_UP_FACTOR,
            Decimal("0.20"),
            Decimal("0.95"),
            description="Maximum ratio of average pullback volume to baseline volume SMA. Default 0.70.",
        ),
    )
)

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
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
        "BUY when an established uptrend undergoes a corrective pullback of pullback_bars where "
        "average volume contracts significantly (<= dry_up_factor * baseline volume SMA), and price "
        "subsequently resumes upward on the latest bar with a close above the prior pullback bar "
        "on expanding volume."
    ),
    source=(
        "Concept: Volume dry-up during constructive consolidations and pullbacks (William O'Neil, "
        "How to Make Money in Stocks; Gil Morales & Chris Kacher, Trade Like an O'Neil Disciple; "
        "Mark Minervini, Trade Like a Stock Market Wizard); Type B. Trigger: Engineering choice "
        "(Type C) evaluating rolling point-in-time trend advance, multi-bar volume contraction "
        "relative to preceding volume SMA, and completed bullish resumption breakout with expanding volume."
    ),
    execution_limitations=(
        "Advisory long entry only; no stops, sizing, targets, or orders are owned by this class.",
        "Requires completed adjacent regular-session M1 bars; gaps return INSUFFICIENT_DATA.",
        "Edge-triggered on the resumption bar; repeat signals on continued rise are suppressed.",
    ),
    reason_codes=REASON_CODES,
)


class VolumeDryUpPullbackBuy(BuyStrategy):
    """BUY when an uptrend pullback on contracted volume resumes upward on expanding volume."""

    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        p = cls.spec.parameter_schema.validate(params)
        trend = p["trend_bars"]
        pullback = p["pullback_bars"]
        vol_p = p["volume_period"]
        assert isinstance(trend, int) and isinstance(pullback, int) and isinstance(vol_p, int)
        return _required_bars(trend, pullback, vol_p)

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        p = self.spec.parameter_schema.validate(params)
        trend = p["trend_bars"]
        pullback = p["pullback_bars"]
        vol_p = p["volume_period"]
        dry_factor = p["dry_up_factor"]
        assert isinstance(trend, int) and isinstance(pullback, int) and isinstance(vol_p, int)
        assert isinstance(dry_factor, Decimal)

        needed = _required_bars(trend, pullback, vol_p)
        bars = closed_bars(self.spec, context, needed)
        if bars is None:
            return insufficient(self.spec, context)

        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)

        if not contiguous(bars[-needed:]):
            return insufficient(self.spec, context)

        latest = bars[-1]
        pullback_bars_list = bars[-1 - pullback : -1]
        trend_peak = bars[-1 - pullback]
        trend_start = bars[-1 - pullback - trend]

        # 1. Prior uptrend requirement: close at trend peak was higher than trend start
        if trend_peak.close <= trend_start.close:
            return finish(self.spec, context, latest, state, False)

        # 2. Corrective pullback structure:
        # Price actually pulled back (min close during pullback is below trend_peak close)
        # but did not violate the trend start low (constructive, shallow correction)
        min_pullback_close = min(b.close for b in pullback_bars_list)
        min_pullback_low = min(b.low for b in pullback_bars_list)
        if min_pullback_close >= trend_peak.close or min_pullback_low <= trend_start.low:
            return finish(self.spec, context, latest, state, False)

        # 3. Volume dry-up evaluation:
        # Baseline volume SMA measured over volume_period bars prior to the pullback
        baseline_bars = bars[-1 - pullback - vol_p : -1 - pullback]
        sum_baseline_vol = sum((b.volume for b in baseline_bars), Decimal("0"))
        avg_baseline_vol = sum_baseline_vol / Decimal(vol_p)
        if avg_baseline_vol <= 0:
            return finish(self.spec, context, latest, state, False)

        # Average volume during pullback must be at or below dry_up_factor of baseline
        pullback_sum_vol = sum((b.volume for b in pullback_bars_list), Decimal("0"))
        pullback_avg_vol = pullback_sum_vol / Decimal(pullback)
        volume_dried_up = pullback_avg_vol <= dry_factor * avg_baseline_vol
        final_pullback_bar_dry = pullback_bars_list[-1].volume < avg_baseline_vol

        if not (volume_dried_up and final_pullback_bar_dry):
            return finish(self.spec, context, latest, state, False)

        # 4. Bullish resumption on latest bar:
        # Latest bar is bullish (close > open and close > prior bar close),
        # breaks above the prior pullback bar high, and volume expands
        bullish_candle = (latest.close > latest.open) and (latest.close > pullback_bars_list[-1].close)
        breaks_pullback_high = latest.close > pullback_bars_list[-1].high
        volume_expanding = (latest.volume > pullback_bars_list[-1].volume) and (
            latest.volume >= avg_baseline_vol * Decimal("0.85")
        )

        resumed = bullish_candle and breaks_pullback_high and volume_expanding
        return finish(self.spec, context, latest, state, resumed)
