"""Relative volume momentum BUY strategy (volume expansion with confirmed bullish price momentum).

A BUY signal fires edge-triggered on completed regular-session 1-minute bars when:
1. Relative volume on the latest completed bar (current volume divided by the average
   volume of the preceding ``volume_period`` completed bars) crosses at or above
   ``rvol_threshold`` while the prior completed bar was strictly below it.
2. The latest completed bar is a bullish candle: closing strictly higher than its
   own open (green body) and strictly higher than the prior close.
3. The latest completed close confirms positive price momentum over the trailing
   ``momentum_bars`` window, gaining at least ``min_return_pct`` percent.

This combines an objective relative-volume expansion event with confirmed bullish price
action, ensuring the volume surge reflects buying pressure rather than distribution.
"""

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
    require_param,
)
from mike_trade.strategies.indicators.patterns import contiguous

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_VOLUME_PERIOD",
    "DEFAULT_RVOL_THRESHOLD",
    "DEFAULT_MOMENTUM_BARS",
    "DEFAULT_MIN_RETURN_PCT",
    "WARMUP_BARS",
    "RelativeVolumeMomentumBuy",
]

STRATEGY_ID = "relative_volume_momentum"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1

DEFAULT_VOLUME_PERIOD = 20
DEFAULT_RVOL_THRESHOLD = Decimal("2.0")
DEFAULT_MOMENTUM_BARS = 3
DEFAULT_MIN_RETURN_PCT = Decimal("0.1")


def _required_bars(volume_period: int, momentum_bars: int) -> int:
    return max(volume_period, momentum_bars) + 2


WARMUP_BARS = _required_bars(DEFAULT_VOLUME_PERIOD, DEFAULT_MOMENTUM_BARS)

_SCHEMA = ParameterSchema(
    (
        ParameterSpec(
            "volume_period",
            ParameterKind.INT,
            DEFAULT_VOLUME_PERIOD,
            5,
            100,
            description=(
                "Lookback window of completed bars used for baseline volume SMA. Default 20."
            ),
        ),
        ParameterSpec(
            "rvol_threshold",
            ParameterKind.DECIMAL,
            DEFAULT_RVOL_THRESHOLD,
            Decimal("1.1"),
            Decimal("10.0"),
            description=(
                "Minimum relative volume multiple required on the trigger bar. Default 2.0."
            ),
        ),
        ParameterSpec(
            "momentum_bars",
            ParameterKind.INT,
            DEFAULT_MOMENTUM_BARS,
            1,
            20,
            description="Number of bars over which price momentum return is measured. Default 3.",
        ),
        ParameterSpec(
            "min_return_pct",
            ParameterKind.DECIMAL,
            DEFAULT_MIN_RETURN_PCT,
            Decimal("0.0"),
            Decimal("5.0"),
            description="Minimum percentage return required over momentum_bars. Default 0.1%.",
        ),
    )
)

_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="momentum",
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
        "BUY when relative volume (bar volume / preceding volume_period volume SMA) "
        "crosses at or above rvol_threshold from below on a bullish candle (close > open and "
        "close > prior close) with positive return over momentum_bars "
        "(100 * (close[t] - close[t-momentum_bars]) / close[t-momentum_bars] >= min_return_pct)."
    ),
    source=(
        "Indicator: Relative Volume (RVOL) ratio (current volume / preceding volume SMA); Type A. "
        "Trigger: Engineering choice (Type C) requiring an edge-triggered RVOL expansion threshold "
        "coupled with confirmed bullish candlestick advance and positive multi-bar momentum."
    ),
    execution_limitations=(
        "Entry signal only; no stops, sizing, targets, or orders.",
        "Requires completed adjacent regular-session M1 bars; gaps invalidate the evaluation.",
        "Edge-triggered on the volume surge bar; does not re-signal on continuously high volume.",
    ),
    reason_codes=REASON_CODES,
)


class RelativeVolumeMomentumBuy(BuyStrategy):
    """BUY on relative volume surge with confirmed bullish price momentum."""

    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        values = cls.spec.parameter_schema.validate(params)
        vol_p = values["volume_period"]
        mom_p = values["momentum_bars"]
        assert isinstance(vol_p, int) and isinstance(mom_p, int)
        require_param(
            mom_p < vol_p,
            "momentum_bars must be less than volume_period",
            "momentum_bars",
        )
        return _required_bars(vol_p, mom_p)

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        vol_p = values["volume_period"]
        rvol_thresh = values["rvol_threshold"]
        mom_p = values["momentum_bars"]
        min_ret = values["min_return_pct"]
        assert isinstance(vol_p, int) and isinstance(mom_p, int)
        assert isinstance(rvol_thresh, Decimal) and isinstance(min_ret, Decimal)
        require_param(
            mom_p < vol_p,
            "momentum_bars must be less than volume_period",
            "momentum_bars",
        )

        needed = _required_bars(vol_p, mom_p)
        bars = closed_bars(self.spec, context, needed)
        if bars is None:
            return insufficient(self.spec, context)

        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)

        # Ensure no gaps exist across the evaluation window
        if not contiguous(bars[-needed:]):
            return insufficient(self.spec, context)

        # Baseline volume for latest bar (t): preceding vol_p bars
        vol_window_latest = bars[-1 - vol_p : -1]
        sum_vol_latest = sum((b.volume for b in vol_window_latest), Decimal("0"))
        avg_vol_latest = sum_vol_latest / Decimal(vol_p)
        if avg_vol_latest <= 0:
            return finish(self.spec, context, bars[-1], state, False)

        rvol_latest = bars[-1].volume / avg_vol_latest

        # Baseline volume for prior bar (t-1): preceding vol_p bars
        vol_window_prior = bars[-2 - vol_p : -2]
        sum_vol_prior = sum((b.volume for b in vol_window_prior), Decimal("0"))
        avg_vol_prior = sum_vol_prior / Decimal(vol_p)
        rvol_prior = (bars[-2].volume / avg_vol_prior) if avg_vol_prior > 0 else Decimal("0")

        # Edge trigger: RVOL crosses at or above threshold
        volume_surged = (rvol_prior < rvol_thresh) and (rvol_latest >= rvol_thresh)

        # Bullish candle: green body and close above prior close
        bullish_candle = (bars[-1].close > bars[-1].open) and (bars[-1].close > bars[-2].close)

        # Price momentum: return over momentum_bars
        base_price = bars[-1 - mom_p].close
        if base_price <= 0:
            return finish(self.spec, context, bars[-1], state, False)

        ret_pct = ((bars[-1].close - base_price) / base_price) * Decimal("100")
        momentum_confirmed = ret_pct >= min_ret

        triggered = volume_surged and bullish_candle and momentum_confirmed
        return finish(self.spec, context, bars[-1], state, triggered)
