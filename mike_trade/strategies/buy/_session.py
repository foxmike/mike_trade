"""Small fail-closed helpers for regular-session BUY strategies."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from mike_trade.market_data.candles import Candle, CandleTimeframe
from mike_trade.scheduling.calendar import NEW_YORK, Session, session_for

if TYPE_CHECKING:
    from mike_trade.market_data.context import MarketContext

MINUTE = timedelta(minutes=1)
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)


def current_session_bars(context: MarketContext) -> tuple[Session, tuple[Candle, ...]] | None:
    """Return a complete-from-open M1 prefix for the latest bar's NYSE session.

    A bounded history whose first current-session observation is not the exact
    session open is rejected. Missing or duplicated minutes are also rejected.
    """
    supplied = context.candles.get(CandleTimeframe.M1, ())
    if not supplied:
        return None
    day = supplied[-1].start.astimezone(NEW_YORK).date()
    try:
        session = session_for(day)
    except (TypeError, ValueError):
        return None
    bars = tuple(
        bar for bar in supplied
        if session.open_utc <= bar.start and bar.end <= session.close_utc
    )
    if not bars or bars[-1] is not supplied[-1] or bars[0].start != session.open_utc:
        return None
    expected = session.open_utc
    for bar in bars:
        if bar.start != expected or bar.end != bar.start + MINUTE:
            return None
        expected = bar.end
    return session, bars


def at(day: date, wall_time: time) -> datetime:
    """Convert a naive New York wall-clock time on *day* to an aware instant."""
    return datetime.combine(day, wall_time, tzinfo=NEW_YORK)


def complete_window(
    bars: tuple[Candle, ...], start: datetime, end: datetime
) -> tuple[Candle, ...] | None:
    """Return every exact M1 bar in ``[start, end)`` or fail closed."""
    if end <= start or start.second or start.microsecond or end.second or end.microsecond:
        return None
    selected = tuple(bar for bar in bars if start <= bar.start < end)
    expected_count = int((end - start) / MINUTE)
    if len(selected) != expected_count:
        return None
    if any(bar.start != start + index * MINUTE for index, bar in enumerate(selected)):
        return None
    return selected


def minutes_from_open(wall_time: time) -> int:
    """Return whole minutes after 09:30 ET for a validated session time."""
    return wall_time.hour * 60 + wall_time.minute - (9 * 60 + 30)
