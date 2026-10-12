"""Deterministic NYSE session calendar.

The calendar is rule based and intentionally performs no I/O. Session dates are
interpreted in New York time; all public instant values are returned in UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from types import MappingProxyType
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
SUPPORTED_YEAR_START = 2000
SUPPORTED_YEAR_END = 2035


class SessionStatus(str, Enum):
    PRE_OPEN = "PRE_OPEN"
    REGULAR = "REGULAR"
    CLOSED = "CLOSED"
    HOLIDAY = "HOLIDAY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Session:
    """An immutable regular NYSE session, including its effective close."""

    session_id: str
    session_date: date
    open_et: datetime
    close_et: datetime
    is_early_close: bool
    open_utc: datetime
    close_utc: datetime


# Override entries augment the published rule calendar.  Values are either
# ``"closed"`` or an early close time expressed as an ET wall-clock time.
SPECIAL_OVERRIDES: MappingProxyType[date, str | time] = MappingProxyType(
    {
        date(2025, 1, 9): "closed",  # National day of mourning for President Carter.
    }
)


def _check_year(day: date) -> None:
    if not SUPPORTED_YEAR_START <= day.year <= SUPPORTED_YEAR_END:
        raise ValueError(
            f"date year {day.year} is outside supported range "
            f"{SUPPORTED_YEAR_START}-{SUPPORTED_YEAR_END}"
        )


def _observed_fixed(day: date, *, new_year: bool = False) -> date | None:
    """Return a fixed-date holiday's observed date under NYSE rules."""
    if day.weekday() == 5:
        return None if new_year else day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _easter_sunday(year: int) -> date:
    """Gregorian Easter computus (Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day_num = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day_num + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last_day = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last_day - timedelta(days=(last_day.weekday() - weekday) % 7)


def _holidays(year: int) -> frozenset[date]:
    holidays: set[date] = {
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _last_weekday(year, 5, 0),  # Memorial Day
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
    }
    for month, day_num, is_new_year in (
        (1, 1, True),
        (7, 4, False),
        (12, 25, False),
    ):
        observed = _observed_fixed(date(year, month, day_num), new_year=is_new_year)
        if observed is not None:
            holidays.add(observed)
    if year >= 2022:
        observed = _observed_fixed(date(year, 6, 19))
        if observed is not None:
            holidays.add(observed)
    return frozenset(holidays)


def _is_trading_day_unchecked(day: date) -> bool:
    if day.weekday() >= 5:
        return False
    if day in _holidays(day.year):
        return False
    return SPECIAL_OVERRIDES.get(day) != "closed"


def is_trading_day(day: date) -> bool:
    """Return whether *day* has a regular NYSE session; unknown years raise."""
    if not isinstance(day, date) or isinstance(day, datetime):
        raise TypeError("day must be a datetime.date")
    _check_year(day)
    return _is_trading_day_unchecked(day)


def session_for(day: date) -> Session:
    """Return the regular session for a trading date."""
    if not is_trading_day(day):
        raise ValueError(f"{day.isoformat()} is not a trading day")
    close_time = time(16, 0)
    early = False
    override = SPECIAL_OVERRIDES.get(day)
    if isinstance(override, time):
        close_time, early = override, True
    else:
        thanksgiving = _nth_weekday(day.year, 11, 3, 4)
        july_fourth = date(day.year, 7, 4)
        if day == thanksgiving + timedelta(days=1):
            close_time, early = time(13, 0), True
        elif day == date(day.year, 12, 24):
            close_time, early = time(13, 0), True
        elif (
            day == date(day.year, 7, 3)
            and july_fourth.weekday() in (1, 2, 3, 4)
        ):
            close_time, early = time(13, 0), True
    open_et = datetime.combine(day, time(9, 30), NEW_YORK)
    close_et = datetime.combine(day, close_time, NEW_YORK)
    return Session(
        session_id=f"XNYS-{day.isoformat()}",
        session_date=day,
        open_et=open_et,
        close_et=close_et,
        is_early_close=early,
        open_utc=open_et.astimezone(UTC),
        close_utc=close_et.astimezone(UTC),
    )


def _normalize_utc(at: datetime) -> datetime:
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at_utc must be a timezone-aware datetime")
    return at.astimezone(UTC)


def session_status(at_utc: datetime) -> SessionStatus:
    """Classify an instant; out-of-range dates are always UNKNOWN."""
    instant = _normalize_utc(at_utc)
    local_day = instant.astimezone(NEW_YORK).date()
    if not SUPPORTED_YEAR_START <= local_day.year <= SUPPORTED_YEAR_END:
        return SessionStatus.UNKNOWN
    if not _is_trading_day_unchecked(local_day):
        return SessionStatus.HOLIDAY if local_day.weekday() < 5 else SessionStatus.CLOSED
    session = session_for(local_day)
    if instant < session.open_utc:
        return SessionStatus.PRE_OPEN
    if instant < session.close_utc:
        return SessionStatus.REGULAR
    return SessionStatus.CLOSED


def is_regular_hours(at_utc: datetime) -> bool:
    return session_status(at_utc) is SessionStatus.REGULAR


def entry_cutoff(session: Session, minutes_before_close: int) -> datetime:
    """Return the UTC instant at which new entries stop for this session."""
    if not isinstance(session, Session):
        raise TypeError("session must be a Session")
    if isinstance(minutes_before_close, bool) or not isinstance(minutes_before_close, int):
        raise TypeError("minutes_before_close must be an integer")
    if minutes_before_close < 0:
        raise ValueError("minutes_before_close must be non-negative")
    return session.close_utc - timedelta(minutes=minutes_before_close)


def eod_close_time(session: Session, hour: int, minute: int) -> datetime:
    """Return the UTC instant at which end-of-day liquidation begins for this session.

    ``hour:minute`` is New York wall-clock time.  When it falls at or after an early
    session close, the same interval before the normal 16:00 close is kept relative to the
    actual close, so early-close sessions still liquidate before the bell.
    """
    if not isinstance(session, Session):
        raise TypeError("session must be a Session")
    configured = datetime.combine(
        session.session_date, time(hour, minute), tzinfo=NEW_YORK
    ).astimezone(UTC)
    if configured >= session.close_utc:
        return session.close_utc - timedelta(minutes=16 * 60 - (hour * 60 + minute))
    return configured


def entries_allowed_by_time(at_utc: datetime, cutoff_minutes: int) -> bool:
    """Return whether entries are allowed at this instant before the close cutoff."""
    instant = _normalize_utc(at_utc)
    if session_status(instant) is not SessionStatus.REGULAR:
        return False
    session = session_for(instant.astimezone(NEW_YORK).date())
    return instant < entry_cutoff(session, cutoff_minutes)


def next_session(after: date) -> Session:
    """Return the first session strictly after *after*."""
    if not isinstance(after, date) or isinstance(after, datetime):
        raise TypeError("after must be a datetime.date")
    _check_year(after)
    day = after + timedelta(days=1)
    while day.year <= SUPPORTED_YEAR_END:
        if _is_trading_day_unchecked(day):
            return session_for(day)
        day += timedelta(days=1)
    raise ValueError("no next session within supported year range")


def previous_session(before: date) -> Session:
    """Return the last session strictly before *before*."""
    if not isinstance(before, date) or isinstance(before, datetime):
        raise TypeError("before must be a datetime.date")
    _check_year(before)
    day = before - timedelta(days=1)
    while day.year >= SUPPORTED_YEAR_START:
        if _is_trading_day_unchecked(day):
            return session_for(day)
        day -= timedelta(days=1)
    raise ValueError("no previous session within supported year range")


__all__ = [
    "NEW_YORK",
    "SUPPORTED_YEAR_START",
    "SUPPORTED_YEAR_END",
    "SPECIAL_OVERRIDES",
    "Session",
    "SessionStatus",
    "is_trading_day",
    "session_for",
    "session_status",
    "next_session",
    "previous_session",
    "is_regular_hours",
    "entry_cutoff",
    "entries_allowed_by_time",
]
