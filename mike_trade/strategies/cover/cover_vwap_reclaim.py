"""COVER when the close reclaims the session VWAP from below."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import Candle, CandleStatus, CandleTimeframe
from mike_trade.strategies.base import (
    CoverStrategy,
    ParameterSchema,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.closed_bar import (
    REASON_CODES,
    STATE_LAST_SIGNAL_BAR,
    blocked,
    finish,
    gate,
    insufficient,
)

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

STRATEGY_ID = "cover_vwap_reclaim"
STRATEGY_VERSION = "1.0.0"
TIMEFRAME = CandleTimeframe.M1
STATE_SESSION_ID = "session_id"
#: Indicator-service keys: session VWAP on the latest completed bar and on the bar before it.
VWAP_LATEST_KEY = "vwap"
VWAP_PRIOR_KEY = "vwap_prev"

_NEW_YORK = ZoneInfo("America/New_York")


def _is_valid_closed_bar(candle: object, symbol: str) -> bool:
    if not isinstance(candle, Candle):
        return False
    if candle.symbol != symbol or candle.timeframe is not TIMEFRAME:
        return False
    if candle.status is not CandleStatus.CLOSED:
        return False
    for price in (candle.open, candle.high, candle.low, candle.close):
        if isinstance(price, bool) or not isinstance(price, Decimal):
            return False
        if not price.is_finite() or price <= 0:
            return False
    if isinstance(candle.volume, bool) or not isinstance(candle.volume, Decimal):
        return False
    if not candle.volume.is_finite() or candle.volume < 0:
        return False
    return candle.low <= candle.open <= candle.high and candle.low <= candle.close <= candle.high


def _is_valid_vwap(value: object) -> bool:
    return (
        isinstance(value, Decimal) and not isinstance(value, bool)
        and value.is_finite() and value > 0
    )


class CoverVwapReclaim(CoverStrategy):
    """COVER on the completed bar whose close moves from strictly below to strictly above the
    session VWAP.

    Both VWAP readings come from the shared, session-reset ``MarketContext`` indicator service
    (``vwap`` on the latest completed bar, ``vwap_prev`` on the bar before it); the strategy never
    computes or fetches VWAP.  Signal (edge, prior bar ``t-1`` and latest completed bar ``t`` of
    the same session)::

        close[t-1] < vwap[t-1]  AND  close[t] > vwap[t]  ->  COVER

    This is a *reclaim* of VWAP from beneath: unlike the vwap_bounce BUY (``close[t-1] <=
    vwap[t-1]``), a prior close exactly on VWAP is not "below" it and does not qualify.  Only
    bars of the latest bar's New York session are used, so a prior-session bar is never the prior
    observation, and the two bars must be consecutive minutes.  No indicator state is persisted
    beyond the session id and the last-signal bar.  Needs 2 completed bars.
    """

    spec = StrategySpec(
        strategy_id=STRATEGY_ID,
        version=STRATEGY_VERSION,
        side=StrategyRole.COVER,
        category="technical",
        sub_category="vwap",
        reference_symbols=(),
        required_timeframes=(TIMEFRAME,),
        warmup_bars=2,
        completed_bars_only=True,
        requires_volume=True,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
        trigger_timeframe=TIMEFRAME,
        parameter_schema=ParameterSchema(()),
        persistent_state_keys=(STATE_SESSION_ID, STATE_LAST_SIGNAL_BAR),
        methodology=(
            "COVER on the completed regular-session bar where the close moves from strictly "
            "below the prior bar's session VWAP to strictly above the latest bar's session "
            "VWAP (VWAP reclaim). Rule definition only; no alpha or profitability claim."
        ),
        source=(
            "Session-VWAP reclaim rule built on the shared session-VWAP indicator service "
            "(volume-weighted average of typical price since the session open); same data "
            "path as the vwap_bounce BUY and vwap_breakdown SELL. No external publication "
            "defines this exact trigger."
        ),
        execution_limitations=(
            "Exit signal only: the engine must evaluate it only while a managed short exists and decides the cover order, quantity and any follow-on action; it never implies a BUY.",
            "Requires warmed per-bar session VWAP (`vwap`, `vwap_prev`) and positive-volume "
            "consecutive M1 bars of one session; a missing, invalid or zero-volume bar or an "
            "unwarmed VWAP yields INSUFFICIENT_DATA.",
            "VWAP resets each session; regular session only; stale data never signals.",
        ),
        reason_codes=REASON_CODES,
    )

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        self.spec.parameter_schema.validate(params)
        reason = gate(self.spec, context, state)
        if reason is not None:
            return blocked(self.spec, context, reason)
        bars = context.candles.get(TIMEFRAME, ())
        if len(bars) < 2 or any(not _is_valid_closed_bar(bar, context.symbol) for bar in bars):
            return insufficient(self.spec, context)
        session_date = bars[-1].start.astimezone(_NEW_YORK).date()
        session_bars = [b for b in bars if b.start.astimezone(_NEW_YORK).date() == session_date]
        if len(session_bars) < 2:
            return insufficient(self.spec, context)
        prior, latest = session_bars[-2], session_bars[-1]
        if latest.start != prior.start + timedelta(minutes=1):
            return insufficient(self.spec, context)
        if prior.volume <= 0 or latest.volume <= 0:
            return insufficient(self.spec, context)
        indicators = context.indicators
        vwap_latest = indicators.get(VWAP_LATEST_KEY) if isinstance(indicators, Mapping) else None
        vwap_prior = indicators.get(VWAP_PRIOR_KEY) if isinstance(indicators, Mapping) else None
        if not _is_valid_vwap(vwap_latest) or not _is_valid_vwap(vwap_prior):
            return insufficient(self.spec, context)
        assert isinstance(vwap_latest, Decimal) and isinstance(vwap_prior, Decimal)
        updates = {STATE_SESSION_ID: context.session_id or session_date.isoformat()}
        triggered = prior.close < vwap_prior and latest.close > vwap_latest
        return finish(self.spec, context, latest, state, triggered, updates)
