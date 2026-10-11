"""VWAP breakdown reference SHORT strategy (session-VWAP downward crossing).

Reference definition: on completed regular-session 1-minute bars, a SHORT
trigger fires edge-triggered when the prior completed close was at or above
the prior completed-bar session VWAP while the latest completed close is
strictly below the latest completed-bar session VWAP::

    prior.close >= vwap_prev AND latest.close < vwap_latest  ->  SIGNAL

Both VWAP values are read from the ``MarketContext`` indicator service under
the ``"vwap"`` (latest completed bar) and ``"vwap_prev"`` (prior completed
bar) keys.  Those shared values are warmed, fresh, and reset each session by
the indicator service.  This strategy never computes VWAP itself, never fetches
data, and performs no I/O of any kind.

On a trigger this module emits only a SHORT ``StrategyEvaluation`` with a
deterministic ID. The engine independently owns entry arbitration, borrow and
margin checks, sizing, protection, and order submission.

This module states a widely known textbook reference definition only; it makes
no alpha, edge, or profitability claim.  The strategy is a pure function of an
immutable ``MarketContext``, validated parameters, and read-only persisted
state: no broker, network, filesystem, order, sizing, confidence-score, or
secrets access (only the standard library and pure domain/market-data value
types are imported).

State convention (read from the ``state`` mapping supplied by the engine):

* ``"session_id"`` and ``"last_signal_bar"`` are owned by this strategy (see
  ``persistent_state_keys``): ``last_signal_bar`` is the ISO start of the bar
  that fired (same-bar replay suppression) and ``session_id`` detects a new
  session.  The strategy carries no fired latch: it rearms naturally, so the
  next valid downward crossing on a later bar emits a fresh signal with a new
  deterministic ID.  Bars and VWAP windows never span sessions.
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
    ParameterSchema,
    ReasonCode,
    ShortStrategy,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.closed_bar import ELIGIBILITY_KEY, ELIGIBILITY_WATCHING_SHORT

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "WARMUP_BARS",
    "STATE_SESSION_ID",
    "STATE_LAST_SIGNAL_BAR",
    "VWAP_LATEST_KEY",
    "VWAP_PRIOR_KEY",
    "ShortVwapBreakdown",
]

STRATEGY_ID = "short_vwap_breakdown"
STRATEGY_VERSION = "1.0.0"

#: ``spec.warmup_bars``: two completed session bars plus a warmed session VWAP.
WARMUP_BARS = 2

#: Strategy-owned persistent state keys (also declared in the spec).
STATE_SESSION_ID = "session_id"
STATE_LAST_SIGNAL_BAR = "last_signal_bar"

#: Indicator-service key holding the latest completed-bar session VWAP.
VWAP_LATEST_KEY = "vwap"
#: Indicator-service key holding the prior completed-bar session VWAP.
VWAP_PRIOR_KEY = "vwap_prev"

_NEW_YORK = ZoneInfo("America/New_York")


_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.SHORT,
    category="technical",
    sub_category="vwap",
    reference_symbols=(),
    required_timeframes=(CandleTimeframe.M1,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=True,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=CandleTimeframe.M1,
    parameter_schema=ParameterSchema(()),
    persistent_state_keys=(STATE_SESSION_ID, STATE_LAST_SIGNAL_BAR),
    methodology=(
        "Session-VWAP downward-crossing reference: on completed regular-session "
        "1-minute bars, a SHORT trigger fires edge-triggered when the prior "
        "completed close was at or above the prior completed-bar session VWAP "
        "(`vwap_prev`) while the latest completed close is strictly below the "
        "latest completed-bar session VWAP (`vwap`). Both VWAP values are the "
        "warmed, fresh, session-reset shared values supplied by the "
        "MarketContext/indicator service; the strategy never computes VWAP "
        "and never fetches data. This restates a textbook reference "
        "definition and makes no alpha or profitability claim."
    ),
    source=(
        "Session-VWAP breakdown reference definition (textbook reference); "
        "not an alpha claim, not a profitability claim, and not backtested here."
    ),
    execution_limitations=(
        "SHORT entry signal only: no order submission, sizing, margin, borrow, or "
        "protection logic.",
        "The engine owns SHORT eligibility and BUY-versus-SHORT entry arbitration.",
        "Regular session only; no overnight or pre/post-market handling.",
        "Requires completed 1-minute bars with positive volume; a missing, "
        "stale, invalid, or zero-volume bar invalidates the evaluation "
        "instead of signalling.",
        "Requires warmed per-bar session VWAP (`vwap` and `vwap_prev`); "
        "a missing or not-warmed VWAP invalidates the evaluation.",
        "Bars and VWAP windows never span sessions; VWAP resets each session.",
        "One signal per completed bar: replaying the same bar never emits "
        "twice; the strategy rearms naturally on the next valid crossing.",
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
    """Return True when ``candle`` is a usable completed 1-minute bar."""
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
    if isinstance(candle.volume, bool) or not isinstance(candle.volume, Decimal):
        return False
    if not candle.volume.is_finite() or candle.volume < 0:
        return False
    if candle.high < candle.low:
        return False
    if not candle.low <= candle.open <= candle.high:
        return False
    return candle.low <= candle.close <= candle.high


def _is_valid_vwap(value: object) -> bool:
    """Return True when ``value`` is a usable warmed session-VWAP reading."""
    return (
        isinstance(value, Decimal)
        and not isinstance(value, bool)
        and value.is_finite()
        and value > 0
    )


class ShortVwapBreakdown(ShortStrategy):
    """Edge-triggered session-VWAP downward-crossing SHORT on closed 1-minute bars."""

    spec = _SPEC

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        self.spec.parameter_schema.validate(params)

        symbol = context.symbol

        if context.freshness.stale:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.STALE_DATA,))
        if context.session_status is not SessionStatus.REGULAR:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.NOT_ELIGIBLE,))
        if state.get(ELIGIBILITY_KEY) != ELIGIBILITY_WATCHING_SHORT:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.NOT_ELIGIBLE,))

        bars = context.candles.get(CandleTimeframe.M1, ())
        if len(bars) < WARMUP_BARS:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,))
        if any(not _is_valid_closed_bar(bar, symbol) for bar in bars):
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,))

        # VWAP resets each session: only bars from the latest bar's session
        # participate, so a prior-session bar is never used as the prior bar.
        session_date = bars[-1].start.astimezone(_NEW_YORK).date()
        session_bars = [
            bar for bar in bars if bar.start.astimezone(_NEW_YORK).date() == session_date
        ]
        if len(session_bars) < WARMUP_BARS:
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,))
        prior = session_bars[-2]
        latest = session_bars[-1]
        if latest.start != prior.start + timedelta(minutes=1):
            # A missing minute between the evaluated bars: no signal.
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,))
        if prior.volume <= 0 or latest.volume <= 0:
            # Zero-volume bars carry no VWAP information: no signal.
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,))

        indicators = context.indicators
        vwap_latest = indicators.get(VWAP_LATEST_KEY) if isinstance(indicators, Mapping) else None
        vwap_prior = indicators.get(VWAP_PRIOR_KEY) if isinstance(indicators, Mapping) else None
        if not _is_valid_vwap(vwap_latest) or not _is_valid_vwap(vwap_prior):
            # Missing, not-warmed (None), or invalid shared VWAP: no signal.
            return StrategyEvaluation.no_signal(self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,))
        assert isinstance(vwap_latest, Decimal) and isinstance(vwap_prior, Decimal)

        session_key = getattr(context, "session_id", None) or session_date.isoformat()
        base_updates: dict[str, object] = {STATE_SESSION_ID: session_key}

        if (
            isinstance(state, Mapping)
            and state.get(STATE_LAST_SIGNAL_BAR) == latest.start.isoformat()
        ):
            # The same completed bar must never emit twice.
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

        if prior.close >= vwap_prior and latest.close < vwap_latest:
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
