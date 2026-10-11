"""Shared evaluation for BUY rules that qualify a pattern on completed D1 candles and enter on an
intraday completed-candle breakout of the pattern's pivot.

Signal-time contract (identical for every rule built on this helper)
====================================================================
1. **The pattern is daily and already complete.** It is computed only from the completed D1
   candles through the most recently settled session (``MarketContext.candles[D1]``; the current,
   incomplete day is never part of it).  The result - a breakout *pivot* price, or the reason
   there is no pattern - cannot change during the session, so it is computed once per settled
   session and cached in the strategy's persisted state (``daily_pattern``), keyed by that
   session and the parameter signature.  A restart recomputes the same value.
2. **The trigger is intraday.** The signal is the first completed M1 bar of the current regular
   session whose close is above the pivot: ``close[t] > pivot`` and every earlier close of the
   session ``<= pivot`` (so it fires once per session; a gap-up open above the pivot fires on the
   first bar).  ``bar_time`` is that bar - nothing is back-dated to a daily bar.
3. **No repeat across sessions.** A pattern whose daily closes have already exceeded the pivot is
   not a pattern any more (``already_broken_out``), so a breakout that held cannot re-trigger the
   next day; a failed (reversed) intraday breakout may try again in a later session.
4. **Optional participation test.** The session's volume so far must be at least
   ``breakout_volume_ratio`` times the average daily volume pro-rated by the fraction of the
   session elapsed (0 disables).  Daily and intraday volume come from the same feed.
5. Entries need the engine's ``WATCHING_BUY`` arming like every BUY; the strategy submits no
   order, stop or size.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import TYPE_CHECKING

from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.market_data.session_candles import DEFAULT_SETTLE_DELAY, history_issue
from mike_trade.strategies.base import StrategyEvaluation, StrategySpec
from mike_trade.strategies.buy._session import current_session_bars
from mike_trade.strategies.closed_bar import blocked, finish, gate, insufficient

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

#: Strategy-owned persisted key caching the daily pattern for one settled session.
STATE_DAILY_PATTERN = "daily_pattern"
_MINUTE = 60



def evaluate_daily_breakout(
    spec: StrategySpec,
    context: MarketContext,
    state: Mapping[str, object],
    *,
    signature: str,
    required_sessions: int,
    detect: Callable[..., tuple[Decimal | None, str]],
    breakout_volume_ratio: Decimal,
    volume_sessions: int,
    max_overnight_move: Decimal,
) -> StrategyEvaluation:
    reason = gate(spec, context, state)
    if reason is not None:
        return blocked(spec, context, reason)
    daily = context.candles.get(CandleTimeframe.D1, ())
    if len(daily) < required_sessions:
        return insufficient(spec, context)
    window = daily[-required_sessions:]
    issue = history_issue(
        window, as_of=context.as_of, settle_delay=DEFAULT_SETTLE_DELAY,
        max_overnight_move=max_overnight_move,
    )
    if issue is not None:
        return insufficient(spec, context)

    session_key = window[-1].start.isoformat()
    cached = state.get(STATE_DAILY_PATTERN)
    updates: dict[str, object] | None = None
    if (
        isinstance(cached, Mapping)
        and cached.get("session") == session_key
        and cached.get("signature") == signature
    ):
        pivot_text = cached.get("pivot")
        pivot = Decimal(str(pivot_text)) if pivot_text is not None else None
    else:
        pivot, why = detect(window)
        updates = {
            STATE_DAILY_PATTERN: {
                "session": session_key,
                "signature": signature,
                "pivot": None if pivot is None else str(pivot),
                "reason": why,
            }
        }
    found = current_session_bars(context)
    if found is None:
        return insufficient(spec, context, updates)
    session, bars = found
    latest = bars[-1]
    if pivot is None:
        return finish(spec, context, latest, state, False, updates)
    triggered = latest.close > pivot and all(bar.close <= pivot for bar in bars[:-1])
    if triggered and breakout_volume_ratio > 0:
        average = sum((c.volume for c in window[-volume_sessions:]), Decimal(0)) / Decimal(
            volume_sessions
        )
        if average <= 0:
            return insufficient(spec, context, updates)
        minutes = Decimal(str((session.close_utc - session.open_utc).total_seconds() / _MINUTE))
        expected = average * Decimal(len(bars)) / minutes
        triggered = sum((b.volume for b in bars), Decimal(0)) >= breakout_volume_ratio * expected
    return finish(spec, context, latest, state, triggered, updates)
