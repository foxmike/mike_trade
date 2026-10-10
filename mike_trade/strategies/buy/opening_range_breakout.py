"""Opening-range breakout BUY strategy (classic textbook definition).

Classic opening-range breakout: build the opening range from the first
``opening_range_minutes`` completed regular-session 1-minute bars after the
09:30 America/New_York session open (range high = maximum high, range low =
minimum low).  After the range window has fully completed, an edge-triggered
BUY signal fires when the latest completed bar closes strictly above the range
high while the prior completed bar closed at or below it.

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
  session (range reset).  The strategy rearms only after a close returns to or
  below the opening range high.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, time, timedelta
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
    "DEFAULT_OPENING_RANGE_MINUTES",
    "ELIGIBILITY_KEY",
    "ELIGIBILITY_WATCHING_BUY",
    "STATE_SESSION_ID",
    "STATE_FIRED",
    "STATE_LAST_SIGNAL_BAR",
    "OpeningRangeBreakoutBuy",
]

STRATEGY_ID = "opening_range_breakout"
STRATEGY_VERSION = "1.0.0"

#: Default opening-range length in minutes (also documented in the parameter spec).
DEFAULT_OPENING_RANGE_MINUTES = 30

#: ``spec.warmup_bars`` covers the default range plus one breakout bar.
WARMUP_BARS = DEFAULT_OPENING_RANGE_MINUTES + 1

#: State key the engine sets to the symbol's entry eligibility; only read here.
ELIGIBILITY_KEY = "eligibility"
#: The only eligibility value that permits a BUY evaluation.
ELIGIBILITY_WATCHING_BUY = "WATCHING_BUY"

#: Strategy-owned persistent state keys (also declared in the spec).
STATE_SESSION_ID = "session_id"
STATE_FIRED = "fired"
STATE_LAST_SIGNAL_BAR = "last_signal_bar"

_NEW_YORK = ZoneInfo("America/New_York")
_SESSION_OPEN = time(9, 30)


def _parameter_schema() -> ParameterSchema:
    return ParameterSchema(
        (
            ParameterSpec(
                "opening_range_minutes",
                ParameterKind.INT,
                default=DEFAULT_OPENING_RANGE_MINUTES,
                min_value=1,
                max_value=120,
                description=(
                    "Opening range length in minutes from the 09:30 America/New_York "
                    f"session open (default {DEFAULT_OPENING_RANGE_MINUTES})."
                ),
            ),
            ParameterSpec(
                "min_breakout_volume",
                ParameterKind.DECIMAL,
                default=Decimal("0"),
                min_value=Decimal("0"),
                description=(
                    "Optional minimum volume required on the breakout bar "
                    "(default 0 disables the filter; Decimal only, float is rejected)."
                ),
            ),
        )
    )


_SPEC = StrategySpec(
    strategy_id=STRATEGY_ID,
    version=STRATEGY_VERSION,
    side=StrategyRole.BUY,
    category="technical",
    sub_category="breakout",
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
        "Classic opening-range breakout definition: the opening range high/low are "
        "the maximum high/minimum low of the first `opening_range_minutes` completed "
        "regular-session 1-minute bars from the 09:30 America/New_York open; a BUY "
        "signal fires edge-triggered when the latest completed close crosses strictly "
        "above the range high after the window fully completes. "
        "This restates a textbook definition and makes no alpha or profitability claim."
    ),
    source=(
        "Classic opening-range breakout literature (textbook definition); "
        "not an alpha claim and not backtested here."
    ),
    execution_limitations=(
        "Entry signal only: no sizing, stops, targets, or order submission.",
        "Long entries only; no short signals.",
        "Regular session only; no overnight or pre/post-market handling.",
        "Requires complete 1-minute bars; a missing or invalid bar invalidates "
        "the evaluation instead of signalling.",
        "One signal per crossing: the strategy rearms only after the close "
        "returns to or below the opening range high.",
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


class OpeningRangeBreakoutBuy(BuyStrategy):
    """Edge-triggered opening-range breakout BUY on closed 1-minute bars."""

    spec = _SPEC

    def evaluate(
        self,
        context: MarketContext,
        params: Mapping[str, object],
        state: Mapping[str, object],
    ) -> StrategyEvaluation:
        validated = self.spec.parameter_schema.validate(params)
        range_minutes = validated["opening_range_minutes"]
        min_volume = validated["min_breakout_volume"]
        assert isinstance(range_minutes, int) and isinstance(min_volume, Decimal)

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
        if len(bars) < range_minutes + 1:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )
        if any(not _is_valid_closed_bar(bar, symbol) for bar in bars):
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        session_date = bars[-1].start.astimezone(_NEW_YORK).date()
        window_open = datetime.combine(session_date, _SESSION_OPEN, tzinfo=_NEW_YORK)
        window_end = window_open + timedelta(minutes=range_minutes)

        session_bars = [
            bar
            for bar in bars
            if bar.start.astimezone(_NEW_YORK).date() == session_date
            and bar.start >= window_open
        ]
        if len(session_bars) < range_minutes + 1:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )
        range_bars = [bar for bar in session_bars if bar.start < window_end]
        if len(range_bars) != range_minutes:
            # A missing (or duplicated) minute inside the window: no range, no signal.
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )
        expected = [window_open + timedelta(minutes=k)
                    for k in range(range_minutes)]
        if [bar.start for bar in range_bars] != expected:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        latest = session_bars[-1]
        if latest.start < window_end:
            # The range window has not fully completed yet.
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )
        prior = session_bars[-2]
        if prior.start < window_open:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.INSUFFICIENT_DATA,)
            )

        range_high = max(bar.high for bar in range_bars)

        session_key = context.session_id or session_date.isoformat()
        last_signal_bar = state.get(STATE_LAST_SIGNAL_BAR)
        fired = state.get(STATE_FIRED) is True
        if state.get(STATE_SESSION_ID) != session_key:
            # New session: the range resets and any prior firing is forgotten.
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
        if fired and latest.close <= range_high:
            # Pullback to or below the range high rearms the strategy.
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

        if latest.volume <= 0 or latest.volume < min_volume:
            return StrategyEvaluation.no_signal(
                self.spec, symbol, (ReasonCode.NO_SIGNAL,), bar_time=latest.start,
                state_updates=base_updates,
            )
        if prior.close <= range_high < latest.close:
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
