"""Simulated replay clock for the historical replay/backtest subsystem.

``ReplayClock`` conforms to the application's ``mike_trade.runtime.ClockPort``
(``now() -> aware UTC datetime``), so engine/scheduler/strategy code reads time
only through it.  It never reads the wall clock and never sleeps: time moves
only when the replay event loop calls :meth:`ReplayClock.advance_to`, and only
forward.  Naive datetimes and backwards moves raise ``ValueError``.

Design choices (2):
1. Session presentation is a thin adapter over the existing module-level
   NYSE calendar in ``mike_trade.scheduling.calendar`` (open/close, early
   closes, holidays/no-session days, boundaries); no calendar rules are
   duplicated here and the calendar is not modified.  Dates outside the
   calendar's supported years yield ``SessionStatus.UNKNOWN`` / ``None``.
2. Moving to the current instant is a no-op (several events may share one
   timestamp); only a strictly earlier instant is a regression.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from mike_trade.scheduling import calendar as _nyse
from mike_trade.scheduling.calendar import NEW_YORK, Session, SessionStatus

__all__ = ["ReplayClock"]


def _require_aware(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be a datetime; got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


class ReplayClock:
    """Monotonic, explicitly advanced, timezone-aware UTC simulated clock."""

    def __init__(self, start: datetime) -> None:
        self._now = _require_aware(start, "start")
        self._start = self._now

    @property
    def start(self) -> datetime:
        return self._start

    def now(self) -> datetime:
        """Return the current simulated instant (aware UTC)."""
        return self._now

    def advance_to(self, t: datetime) -> datetime:
        """Move forward to ``t`` (no-op if equal); earlier or naive ``t`` raises."""
        target = _require_aware(t, "t")
        if target < self._now:
            raise ValueError(
                f"replay clock cannot move backwards: {target.isoformat()} < "
                f"{self._now.isoformat()}"
            )
        self._now = target
        return self._now

    # ------------------------------------------------------------ presentation

    def local_now(self) -> datetime:
        """Current instant presented in America/New_York."""
        return self._now.astimezone(NEW_YORK)

    def session_date(self) -> date:
        """New York calendar date of the current instant."""
        return self.local_now().date()

    def session_status(self) -> SessionStatus:
        """PRE_OPEN / REGULAR / CLOSED / HOLIDAY / UNKNOWN at the current instant."""
        return _nyse.session_status(self._now)

    def is_regular_hours(self) -> bool:
        return _nyse.is_regular_hours(self._now)

    def current_session(self) -> Session | None:
        """Session of the current New York date, or ``None`` on a no-session day."""
        day = self.session_date()
        if not _nyse.SUPPORTED_YEAR_START <= day.year <= _nyse.SUPPORTED_YEAR_END:
            return None
        if not _nyse.is_trading_day(day):
            return None
        return _nyse.session_for(day)

    def session_open(self) -> datetime | None:
        """UTC open of today's session, or ``None`` on a holiday/weekend."""
        session = self.current_session()
        return None if session is None else session.open_utc

    def session_close(self) -> datetime | None:
        """UTC effective close (early closes included), or ``None`` if no session."""
        session = self.current_session()
        return None if session is None else session.close_utc

    def is_early_close(self) -> bool:
        session = self.current_session()
        return bool(session is not None and session.is_early_close)

    def at_session_open(self) -> bool:
        """True exactly at the session's opening instant."""
        return self._now == self.session_open()

    def at_session_close(self) -> bool:
        """True exactly at the session's closing instant."""
        return self._now == self.session_close()

    def time_to_close(self) -> timedelta | None:
        """Time left in the regular session, or ``None`` outside of it."""
        if not self.is_regular_hours():
            return None
        close = self.session_close()
        return None if close is None else close - self._now

    def next_session(self) -> Session:
        """First session strictly after the current New York date."""
        return _nyse.next_session(self.session_date())
