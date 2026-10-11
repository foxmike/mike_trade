"""BUY on a completed close crossing back above the lower session VWAP deviation band.

Captures an intraday mean-reversion recovery when price dips below the lower
volume-weighted standard deviation band around session VWAP and subsequently
reclaims the band:

1. Setup: prior completed close is strictly below the lower VWAP band:
   `close[t-1] < lower_band[t-1]`.
2. Trigger: latest completed close crosses back strictly above the lower VWAP band:
   `close[t] > lower_band[t]`.

Point-in-time volume-weighted standard deviation bands are computed strictly from
completed bars of the current regular session (resetting at session open):
    TP_i = (High_i + Low_i + Close_i) / 3
    VWAP_k = sum(TP_i * V_i) / sum(V_i)
    Variance_k = (sum(TP_i^2 * V_i) / sum(V_i)) - VWAP_k^2
    StdDev_k = sqrt(max(0, Variance_k))
    LowerBand_k = VWAP_k - multiplier * StdDev_k

Materially distinct from:
- `vwap_bounce`: `vwap_bounce` detects crossing the central VWAP line from below
  (`prior.close <= vwap_prev` and `latest.close > vwap_latest`), whereas
  `vwap_band_reversion` detects an oversold rebound at the lower deviation band
  (typically 2 standard deviations below session VWAP).
- `bollinger_band_mean_reversion`: Bollinger bands use an unweighted rolling SMA
  and rolling unweighted standard deviation over a fixed finite window (e.g. 20 bars).
  In contrast, `vwap_band_reversion` uses cumulative volume-weighted statistics
  that reset at each regular session open.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import Candle, CandleStatus, CandleTimeframe
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import (
    BuyStrategy,
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    ReasonCode,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.indicators.vwap_bands import (
    VWAPBands,
    session_vwap_bands,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_MULTIPLIER",
    "DEFAULT_MIN_SESSION_BARS",
    "WARMUP_BARS",
    "ELIGIBILITY_KEY",
    "ELIGIBILITY_WATCHING_BUY",
    "STATE_SESSION_ID",
    "STATE_LAST_SIGNAL_BAR",
    "VWAPBandReversionBuy",
]

STRATEGY_ID = "vwap_band_reversion"
STRATEGY_VERSION = "1.0.0"

DEFAULT_MULTIPLIER = Decimal("2.0")
DEFAULT_MIN_SESSION_BARS = 5
WARMUP_BARS = DEFAULT_MIN_SESSION_BARS

ELIGIBILITY_KEY = "eligibility"
ELIGIBILITY_WATCHING_BUY = "WATCHING_BUY"

STATE_SESSION_ID = "session_id"
STATE_LAST_SIGNAL_BAR = "last_signal_bar"

_NEW_YORK = ZoneInfo("America/New_York")


def _required_bars(min_session_bars: int) -> int:
    return max(2, min_session_bars)


_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="vwap",
    reference_symbols=(),
    required_timeframes=(CandleTimeframe.M1,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=True,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=CandleTimeframe.M1,
    parameter_schema=ParameterSchema(
        (
            ParameterSpec(
                "multiplier",
                ParameterKind.DECIMAL,
                DEFAULT_MULTIPLIER,
                Decimal("0.5"),
                Decimal("4.0"),
                description="Standard deviation multiplier for lower VWAP band. Default 2.0.",
            ),
            ParameterSpec(
                "min_session_bars",
                ParameterKind.INT,
                DEFAULT_MIN_SESSION_BARS,
                2,
                60,
                description="Minimum completed session bars before evaluating band reversion. Default 5.",
            ),
        )
    ),
    persistent_state_keys=(STATE_SESSION_ID, STATE_LAST_SIGNAL_BAR),
    methodology=(
        "BUY when price trades strictly below the session lower VWAP deviation band "
        "(close[t-1] < lower_band[t-1]) and the latest completed bar crosses back strictly "
        "above the lower band (close[t] > lower_band[t]). Bands use point-in-time cumulative "
        "volume-weighted standard deviation resetting each session."
    ),
    source=(
        "Brian Shannon, CMT, Technical Analysis Using Multiple Timeframes (2008) and "
        "Maximum Trading Gains With Anchored VWAP (2023) (VWAP standard deviation bands); "
        "StockCharts / TradingView canonical session VWAP deviation band calculations."
    ),
    execution_limitations=(
        "Entry signal only: no stops, sizing, targets, or orders.",
        "Long entries only; regular session only; completed 1-minute bars with positive volume.",
        "Bars and VWAP windows never span sessions; accumulators reset at session open.",
        "Materially distinct from vwap_bounce: vwap_bounce triggers on crossing central VWAP, "
        "whereas vwap_band_reversion triggers on recovering above the lower deviation band.",
        "One signal per completed bar: replaying the same bar never emits twice.",
    ),
    reason_codes=(
        ReasonCode.SIGNAL,
        ReasonCode.NO_SIGNAL,
        ReasonCode.INSUFFICIENT_DATA,
        ReasonCode.STALE_DATA,
        ReasonCode.NOT_ELIGIBLE,
    ),
)


def _is_valid_closed_bar(candle: object, symbol: str) -> bool:
    if not isinstance(candle, Candle):
        return False
    if candle.symbol != symbol or candle.timeframe is not CandleTimeframe.M1:
        return False
    if candle.status is not CandleStatus.CLOSED:
        return False
    for price in (candle.open, candle.high, candle.low, candle.close):
        if (
            isinstance(price, bool)
            or not isinstance(price, Decimal)
            or not price.is_finite()
            or price <= 0
        ):
            return False
    if (
        isinstance(candle.volume, bool)
        or not isinstance(candle.volume, Decimal)
        or not candle.volume.is_finite()
        or candle.volume < 0
    ):
        return False
    return True


class VWAPBandReversionBuy(BuyStrategy):
    """BUY when close crosses back above the lower session VWAP deviation band."""

    spec = _SPEC

    @classmethod
    def required_bars(cls, params: Mapping[str, object]) -> int:
        min_bars = cls.spec.parameter_schema.validate(params)["min_session_bars"]
        assert isinstance(min_bars, int)
        return _required_bars(min_bars)

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        values = self.spec.parameter_schema.validate(params)
        multiplier = values["multiplier"]
        min_session_bars = values["min_session_bars"]
        assert isinstance(multiplier, Decimal) and isinstance(min_session_bars, int)

        symbol = context.symbol

        if context.freshness.stale:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.STALE_DATA,)
            )
        if context.session_status is not SessionStatus.REGULAR:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.NOT_ELIGIBLE,)
            )
        if not isinstance(state, Mapping) or state.get(ELIGIBILITY_KEY) != (
            ELIGIBILITY_WATCHING_BUY
        ):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.NOT_ELIGIBLE,)
            )

        need_bars = _required_bars(min_session_bars)
        bars = context.candles.get(CandleTimeframe.M1, ())
        if len(bars) < need_bars:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )
        if any(not _is_valid_closed_bar(bar, symbol) for bar in bars):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        session_date = bars[-1].start.astimezone(_NEW_YORK).date()
        session_bars = [
            bar
            for bar in bars
            if bar.start.astimezone(_NEW_YORK).date() == session_date
        ]
        if len(session_bars) < need_bars:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        # Adjacency check on latest two bars
        if session_bars[-1].start != session_bars[-2].end:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        # Ensure bars in session have positive volume
        if any(bar.volume <= 0 for bar in session_bars[-need_bars:]):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        bands = session_vwap_bands(session_bars, multiplier=multiplier)
        prior_band = bands[-2]
        latest_band = bands[-1]
        if prior_band is None or latest_band is None:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        session_key = context.session_id or session_date.isoformat()
        base_updates: dict[str, object] = {STATE_SESSION_ID: session_key}

        latest = session_bars[-1]
        prior = session_bars[-2]

        if state.get(STATE_LAST_SIGNAL_BAR) == latest.start.isoformat():
            return StrategyEvaluation.no_signal(
                self.spec,
                symbol,
                (ReasonCode.NO_SIGNAL,),
                bar_time=latest.start,
                state_updates={
                    **base_updates,
                    STATE_LAST_SIGNAL_BAR: latest.start.isoformat(),
                },
            )

        # Signal condition: prior closed below lower band, latest closed back above lower band
        triggered = prior.close < prior_band.lower and latest.close > latest_band.lower

        if triggered:
            return StrategyEvaluation.for_signal(
                self.spec,
                symbol,
                latest.start,
                reference_price=latest.close,
                state_updates={
                    **base_updates,
                    STATE_LAST_SIGNAL_BAR: latest.start.isoformat(),
                },
            )

        return StrategyEvaluation.no_signal(
            self.spec,
            symbol,
            (ReasonCode.NO_SIGNAL,),
            bar_time=latest.start,
            state_updates=base_updates,
        )
