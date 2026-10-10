"""Momentum reversal SELL strategy (lookback rate-of-change loss of momentum).

Precise rule: on completed regular-session 1-minute bars, let
``n = lookback_bars`` and let the evaluation window be the last ``n + 2``
completed bars of the latest session, oldest first
``[c_0, ..., c_n, c_{n+1}]`` where ``c_{n+1}`` is the latest completed close
and ``c_n`` is the prior completed close.  Define two ``n``-bar
rates of change::

    roc_prior  = (c_n - c_0) / c_0
    roc_latest = (c_{n+1} - c_1) / c_1

A SELL trigger fires edge-triggered when momentum was positive on the prior
bar but has reversed to at or below the configured negative threshold on the
latest bar::

    roc_prior > 0 AND roc_latest <= reversal_threshold  ->  SIGNAL

``reversal_threshold`` is a negative Decimal fraction (for example ``-0.02``
means a 2% decline over ``n`` bars).  The boundary is inclusive on the latest
leg (``<=``) and strict on the prior leg (``> 0``): ``roc_latest`` exactly
equal to the threshold signals, while ``roc_prior`` exactly equal to zero
does not.

The rule uses only completed-bar closes from the shared ``MarketContext``; it
never fetches data, never uses developing bars, and performs no I/O of any
kind.

On a trigger the composition layer builds an ``ExitLongRecommendation`` for
100% of the confirmed remaining quantity of the owned managed long position
(see ``mike_trade.strategies.sell.base``); this module itself emits only the
pure ``StrategyEvaluation`` signal with a deterministic ID and never emits a
short, a partial quantity, a confidence score, or an order.

This module states a textbook-style reference definition only; it makes no
alpha, edge, or profitability claim.  The strategy is a pure function of an
immutable ``MarketContext``, validated parameters, and read-only persisted
state: no broker, network, filesystem, order, sizing, confidence-score, or
secrets access (only the standard library and pure domain/market-data value
types are imported).

State convention (read from the ``state`` mapping supplied by the engine):

* ``"session_id"`` and ``"last_signal_bar"`` are owned by this strategy (see
  ``persistent_state_keys``): ``last_signal_bar`` is the ISO start of the bar
  that fired (same-bar replay suppression) and ``session_id`` detects a new
  session.  The strategy carries no fired latch: it rearms naturally, so the
  next valid reversal on a later bar emits a fresh signal with a new
  deterministic ID.  Evaluation windows never span sessions.
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
    ParameterKind,
    ParameterSchema,
    ParameterSpec,
    ReasonCode,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.sell.base import SellRule

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_LOOKBACK_BARS",
    "DEFAULT_REVERSAL_THRESHOLD",
    "WARMUP_BARS",
    "STATE_SESSION_ID",
    "STATE_LAST_SIGNAL_BAR",
    "MomentumReversalSell",
]

STRATEGY_ID = "momentum_reversal"
STRATEGY_VERSION = "1.0.0"

#: Default number of completed bars spanned by each rate-of-change leg.
DEFAULT_LOOKBACK_BARS = 5
#: Default reversal threshold: a 2% decline over ``DEFAULT_LOOKBACK_BARS`` bars.
DEFAULT_REVERSAL_THRESHOLD = Decimal("-0.02")

#: ``spec.warmup_bars`` covers the minimum lookback (2) plus the prior/latest
#: pair; larger configured lookbacks are enforced dynamically in the trigger.
WARMUP_BARS = 4

#: Strategy-owned persistent state keys (also declared in the spec).
STATE_SESSION_ID = "session_id"
STATE_LAST_SIGNAL_BAR = "last_signal_bar"

_NEW_YORK = ZoneInfo("America/New_York")


def _parameter_schema() -> ParameterSchema:
    return ParameterSchema(
        (
            ParameterSpec(
                "lookback_bars",
                ParameterKind.INT,
                default=DEFAULT_LOOKBACK_BARS,
                min_value=2,
                max_value=100,
                description=(
                    "Number of completed 1-minute bars spanned by each "
                    "rate-of-change leg (default "
                    f"{DEFAULT_LOOKBACK_BARS})."
                ),
            ),
            ParameterSpec(
                "reversal_threshold",
                ParameterKind.DECIMAL,
                default=DEFAULT_REVERSAL_THRESHOLD,
                min_value=Decimal("-0.50"),
                max_value=Decimal("-0.0001"),
                description=(
                    "Negative Decimal fraction: the latest n-bar rate of change "
                    "must be at or below this value while the prior n-bar rate "
                    "of change was above zero (default "
                    f"{DEFAULT_REVERSAL_THRESHOLD}). Decimal only, float is rejected."
                ),
            ),
        )
    )


_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.SELL,
    category="technical",
    sub_category="momentum",
    reference_symbols=(),
    required_timeframes=(CandleTimeframe.M1,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=True,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=CandleTimeframe.M1,
    parameter_schema=_parameter_schema(),
    persistent_state_keys=(STATE_SESSION_ID, STATE_LAST_SIGNAL_BAR),
    methodology=(
        "Lookback rate-of-change momentum reversal: with n = `lookback_bars`, "
        "the evaluation window is the last n + 2 completed regular-session "
        "1-minute bars; roc_prior = (prior.close - close n bars before "
        "prior) / close n bars before prior and roc_latest = (latest.close - "
        "close n bars before latest) / close n bars before latest. A SELL "
        "trigger fires edge-triggered when roc_prior > 0 while roc_latest <= "
        "`reversal_threshold` (a negative Decimal fraction; the boundary is "
        "inclusive on the latest leg and strict on the prior leg). Uses only "
        "completed-bar closes from the shared MarketContext. This restates a "
        "textbook-style reference definition and makes no alpha or "
        "profitability claim."
    ),
    source=(
        "Rate-of-change momentum-reversal reference definition (textbook "
        "reference); not an alpha claim, not a profitability claim, and not "
        "backtested here."
    ),
    execution_limitations=(
        "Advisory exit signal only: no order submission and no execution.",
        "Subject to minimum hold and exit arbitration; protective, manual, "
        "hard-risk, and forced-liquidation exits outrank strategy exits.",
        "Long exits only; never emits shorts or opposite-side actions.",
        "Full-remainder exits only; never emits partial-quantity recommendations.",
        "Regular session only; no overnight or pre/post-market handling.",
        "Requires completed 1-minute bars with positive volume; a missing, "
        "stale, invalid, or zero-volume bar invalidates the evaluation "
        "instead of signalling.",
        "Evaluation windows never span sessions.",
        "One signal per completed bar: replaying the same bar never emits "
        "twice; the strategy rearms naturally on the next valid reversal.",
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


class MomentumReversalSell(SellRule):
    """Edge-triggered rate-of-change momentum-reversal SELL on closed bars."""

    spec = _SPEC

    def evaluate_trigger(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        lookback = validated["lookback_bars"]
        threshold = validated["reversal_threshold"]
        assert isinstance(lookback, int) and isinstance(threshold, Decimal)

        symbol = context.symbol

        if context.freshness.stale:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.STALE_DATA,)
            )
        if context.session_status is not SessionStatus.REGULAR:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.NOT_ELIGIBLE,)
            )

        bars = context.candles.get(CandleTimeframe.M1, ())
        if len(bars) < lookback + 2:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )
        if any(not _is_valid_closed_bar(bar, symbol) for bar in bars):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        # Evaluation windows never span sessions: only bars from the latest
        # bar's session participate.
        session_date = bars[-1].start.astimezone(_NEW_YORK).date()
        session_bars = [
            bar
            for bar in bars
            if bar.start.astimezone(_NEW_YORK).date() == session_date
        ]
        if len(session_bars) < lookback + 2:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )
        window = session_bars[-(lookback + 2):]
        for prev, cur in zip(window, window[1:]):
            if cur.start != prev.start + timedelta(minutes=1):
                # A missing minute inside the evaluation window: no signal.
                return StrategyEvaluation.no_signal(
                    self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
                )
        if window[-1].volume <= 0 or window[-2].volume <= 0:
            # Zero-volume evaluated bars carry no momentum information.
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        base_ago_prior = window[0].close
        base_ago_latest = window[1].close
        prior_close = window[-2].close
        latest_close = window[-1].close
        roc_prior = (prior_close - base_ago_prior) / base_ago_prior
        roc_latest = (latest_close - base_ago_latest) / base_ago_latest

        latest = window[-1]
        session_key = getattr(context, "session_id", None) or session_date.isoformat()
        base_updates: dict[str, object] = {STATE_SESSION_ID: session_key}

        if isinstance(state, Mapping) and state.get(STATE_LAST_SIGNAL_BAR) == latest.start.isoformat():
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

        if roc_prior > 0 and roc_latest <= threshold:
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
            self.spec, symbol, (ReasonCode.NO_SIGNAL,), bar_time=latest.start,
            state_updates=base_updates,
        )
