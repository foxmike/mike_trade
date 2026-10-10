"""Momentum breakout BUY strategy (classic N-bar high with volume confirmation).

Classic N-bar high breakout with volume confirmation: on completed regular-session
1-minute bars, the lookback high is the highest high of the ``lookback_bars``
completed bars that precede the latest completed bar.  An edge-triggered BUY
signal fires when the latest completed close crosses strictly above that lookback
high while the prior completed close was at or below its own corresponding
lookback high, and the breakout bar's volume is at least ``volume_multiple``
times the average volume of the lookback bars.

This module states a widely known textbook definition only; it makes no alpha,
edge, or profitability claim.  The strategy is a pure function of an immutable
``MarketContext``, validated parameters, and read-only persisted state: no
broker, network, filesystem, order, sizing, confidence-score, or secrets access
(only the standard library and pure domain/market-data value types are
imported).

State convention (read from the ``state`` mapping supplied by the engine):

* ``"eligibility"`` must equal ``"WATCHING_BUY"`` or the evaluation returns
  ``NOT_ELIGIBLE``.  This key is owned by the engine; the strategy only reads
  it and never writes it.
* ``"session_id"``, ``"fired"``, and ``"last_signal_bar"`` are owned by this
  strategy (see ``persistent_state_keys``): ``fired`` records that a breakout
  already fired this session, ``last_signal_bar`` is the ISO start of the bar
  that fired (same-bar replay suppression), and ``session_id`` detects a new
  session (window reset).  The strategy rearms only after a close returns to or
  below the lookback high.  Lookback windows never span sessions.
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

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

__all__ = [
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "DEFAULT_LOOKBACK_BARS",
    "DEFAULT_VOLUME_MULTIPLE",
    "WARMUP_BARS",
    "ELIGIBILITY_KEY",
    "ELIGIBILITY_WATCHING_BUY",
    "STATE_SESSION_ID",
    "STATE_FIRED",
    "STATE_LAST_SIGNAL_BAR",
    "MomentumBreakoutBuy",
]

STRATEGY_ID = "momentum_breakout"
STRATEGY_VERSION = "1.0.0"

#: Default number of completed bars in the lookback window.
DEFAULT_LOOKBACK_BARS = 20
#: Default volume confirmation multiple (Decimal only, float is rejected).
DEFAULT_VOLUME_MULTIPLE = Decimal("1.5")

#: ``spec.warmup_bars`` covers the default lookback plus prior-bar and breakout bar.
WARMUP_BARS = DEFAULT_LOOKBACK_BARS + 2

#: State key the engine sets to the symbol's entry eligibility; only read here.
ELIGIBILITY_KEY = "eligibility"
#: The only eligibility value that permits a BUY evaluation.
ELIGIBILITY_WATCHING_BUY = "WATCHING_BUY"

#: Strategy-owned persistent state keys (also declared in the spec).
STATE_SESSION_ID = "session_id"
STATE_FIRED = "fired"
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
                    "Number of completed 1-minute bars preceding the latest completed "
                    f"bar used for the highest-high lookback (default {DEFAULT_LOOKBACK_BARS})."
                ),
            ),
            ParameterSpec(
                "volume_multiple",
                ParameterKind.DECIMAL,
                default=DEFAULT_VOLUME_MULTIPLE,
                min_value=Decimal("1"),
                max_value=Decimal("10"),
                description=(
                    "Required breakout-bar volume as a multiple of the average lookback "
                    f"volume (default {DEFAULT_VOLUME_MULTIPLE}). Decimal only, "
                    "float is rejected."
                ),
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
    required_timeframes=(CandleTimeframe.M1,),
    warmup_bars=WARMUP_BARS,
    completed_bars_only=True,
    requires_volume=True,
    trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR,
    trigger_timeframe=CandleTimeframe.M1,
    parameter_schema=_parameter_schema(),
    persistent_state_keys=(STATE_SESSION_ID, STATE_FIRED, STATE_LAST_SIGNAL_BAR),
    methodology=(
        "Classic N-bar high breakout with volume confirmation: the lookback high is "
        "the maximum high of the `lookback_bars` completed regular-session 1-minute "
        "bars preceding the latest completed bar; a BUY signal fires edge-triggered "
        "when the latest completed close crosses strictly above that high while the "
        "prior completed close was at or below its own corresponding lookback high, "
        "with breakout volume at least `volume_multiple` times the average lookback "
        "volume. This restates a textbook definition and makes no alpha or "
        "profitability claim."
    ),
    source=(
        "Classic N-bar high breakout literature (textbook definition with volume "
        "confirmation); not an alpha claim and not backtested here."
    ),
    execution_limitations=(
        "Entry signal only: no sizing, stops, targets, or order submission.",
        "Long entries only; no short signals.",
        "Regular session only; no overnight or pre/post-market handling.",
        "Requires complete 1-minute bars; a missing or invalid bar invalidates "
        "the evaluation instead of signalling.",
        "Lookback windows never span sessions.",
        "One signal per crossing: the strategy rearms only after the close "
        "returns to or below the lookback high.",
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


class MomentumBreakoutBuy(BuyStrategy):
    """Edge-triggered N-bar high breakout BUY on closed 1-minute bars."""

    spec = _SPEC

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        lookback = validated["lookback_bars"]
        multiple = validated["volume_multiple"]
        assert isinstance(lookback, int) and isinstance(multiple, Decimal)

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

        bars = context.candles.get(CandleTimeframe.M1, ())
        if len(bars) < lookback + 2:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )
        if any(not _is_valid_closed_bar(bar, symbol) for bar in bars):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        # Lookback windows never span sessions: only bars from the latest bar's
        # session participate.
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

        latest = window[-1]
        prior = window[-2]
        lookback_bars = window[-(lookback + 1):-1]
        prior_lookback_bars = window[-(lookback + 2):-2]
        lookback_high = max(bar.high for bar in lookback_bars)
        prior_lookback_high = max(bar.high for bar in prior_lookback_bars)

        session_key = context.session_id or session_date.isoformat()
        last_signal_bar = state.get(STATE_LAST_SIGNAL_BAR)
        fired = state.get(STATE_FIRED) is True
        if state.get(STATE_SESSION_ID) != session_key:
            # New session: windows reset and any prior firing is forgotten.
            fired = False
            last_signal_bar = None
        base_updates: dict[str, object] = {STATE_SESSION_ID: session_key}

        if last_signal_bar == latest.start.isoformat():
            # The same completed bar must never emit twice.
            updates = dict(base_updates)
            updates[STATE_FIRED] = True
            updates[STATE_LAST_SIGNAL_BAR] = last_signal_bar
            return StrategyEvaluation.no_signal(
                self.spec,
                symbol,
                (ReasonCode.NO_SIGNAL,),
                bar_time=latest.start,
                state_updates=updates,
            )
        if fired and latest.close <= lookback_high:
            # Pullback to or below the lookback high rearms the strategy.
            updates = dict(base_updates)
            updates[STATE_FIRED] = False
            return StrategyEvaluation.no_signal(
                self.spec,
                symbol,
                (ReasonCode.NO_SIGNAL,),
                bar_time=latest.start,
                state_updates=updates,
            )
        if fired:
            return StrategyEvaluation.no_signal(
                self.spec,
                symbol,
                (ReasonCode.NO_SIGNAL,),
                bar_time=latest.start,
                state_updates={**base_updates, STATE_FIRED: True,
                               STATE_LAST_SIGNAL_BAR: last_signal_bar},
            )

        # Volume confirmation uses its own INSUFFICIENT_DATA reason, distinct
        # from the NO_SIGNAL returned for an exact-boundary non-breakout.
        average_volume = sum((bar.volume for bar in lookback_bars), Decimal("0")) / Decimal(
            lookback
        )
        if latest.volume <= 0 or average_volume <= 0:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,),
                bar_time=latest.start, state_updates=base_updates,
            )
        if latest.volume < multiple * average_volume:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,),
                bar_time=latest.start, state_updates=base_updates,
            )
        if prior.close <= prior_lookback_high and latest.close > lookback_high:
            return StrategyEvaluation.for_signal(
                self.spec,
                symbol,
                latest.start,
                reference_price=latest.close,
                state_updates={
                    **base_updates,
                    STATE_FIRED: True,
                    STATE_LAST_SIGNAL_BAR: latest.start.isoformat(),
                },
            )
        return StrategyEvaluation.no_signal(
            self.spec, symbol, (ReasonCode.NO_SIGNAL,), bar_time=latest.start,
            state_updates=base_updates,
        )
