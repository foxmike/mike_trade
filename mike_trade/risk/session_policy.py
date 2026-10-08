"""Pure NYSE session entry and discretionary minimum-hold policies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from mike_trade.scheduling import calendar

__all__ = [
    "SessionPolicy", "SessionDecision", "EntryCutoffPolicy",
    "MinimumHoldPolicy", "ExitKind",
]


@dataclass(frozen=True, slots=True)
class SessionDecision:
    allowed: bool
    reason: str
    evaluated_at: datetime
    cutoff_at: datetime | None = None
    hold_until: datetime | None = None

    @property
    def blocked(self) -> bool:
        return not self.allowed


@dataclass(frozen=True, slots=True)
class EntryCutoffPolicy:
    """Stop accepting entries this many minutes before the actual session close."""

    minutes_before_close: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.minutes_before_close, bool) or not isinstance(self.minutes_before_close, int):
            raise TypeError("minutes_before_close must be an integer")
        if self.minutes_before_close < 0:
            raise ValueError("minutes_before_close must be non-negative")


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """Regular-hours-only entry gate; end-of-day position closing is opt-in metadata."""

    entry_cutoff: EntryCutoffPolicy | None = None
    regular_hours_only: bool = True
    end_of_day_close_enabled: bool = False

    def __post_init__(self) -> None:
        if self.entry_cutoff is not None and not isinstance(self.entry_cutoff, EntryCutoffPolicy):
            raise TypeError("entry_cutoff must be EntryCutoffPolicy or None")
        if not isinstance(self.regular_hours_only, bool):
            raise TypeError("regular_hours_only must be bool")
        if not isinstance(self.end_of_day_close_enabled, bool):
            raise TypeError("end_of_day_close_enabled must be bool")

    def evaluate(self, now: datetime) -> SessionDecision:
        instant = _utc(now, "now")
        status = calendar.session_status(instant)
        if self.regular_hours_only and status is not calendar.SessionStatus.REGULAR:
            return SessionDecision(False, "OUTSIDE_REGULAR_HOURS", instant)
        cutoff_at = None
        if self.entry_cutoff is not None:
            local_day = instant.astimezone(calendar.NEW_YORK).date()
            if status is calendar.SessionStatus.REGULAR:
                session = calendar.session_for(local_day)
                cutoff_at = calendar.entry_cutoff(session, self.entry_cutoff.minutes_before_close)
                if instant >= cutoff_at:
                    return SessionDecision(False, "ENTRY_CUTOFF_REACHED", instant, cutoff_at)
        return SessionDecision(True, "ALLOWED", instant, cutoff_at)


class ExitKind(str, Enum):
    STRATEGY = "strategy"
    PROTECTIVE_STOP = "protective_stop"
    TRAILING_STOP = "trailing_stop"
    RISK_SAFETY = "risk_safety"
    DAILY_LOSS = "daily_loss"
    MANUAL_CLOSE = "manual_close"
    CLOSE_ALL = "close_all"
    FLATTEN = "flatten"
    RECONCILIATION = "reconciliation"
    END_OF_DAY = "end_of_day"


@dataclass(frozen=True, slots=True)
class MinimumHoldPolicy:
    """Hold duration applies only to ordinary strategy SELL exits."""

    minimum_hold: timedelta = timedelta(0)

    def __post_init__(self) -> None:
        if not isinstance(self.minimum_hold, timedelta) or self.minimum_hold < timedelta(0):
            raise ValueError("minimum_hold must be a non-negative timedelta")

    def evaluate(
        self, *, now: datetime, first_confirmed_entry_fill_at: datetime | None,
        exit_kind: ExitKind,
    ) -> SessionDecision:
        instant = _utc(now, "now")
        if not isinstance(exit_kind, ExitKind):
            raise TypeError("exit_kind must be ExitKind")
        # All non-strategy exits are explicitly exempt from this discretionary policy.
        if exit_kind is not ExitKind.STRATEGY or self.minimum_hold == timedelta(0):
            return SessionDecision(True, "ALLOWED", instant)
        if first_confirmed_entry_fill_at is None:
            return SessionDecision(False, "NO_CONFIRMED_ENTRY_FILL", instant)
        filled_at = _utc(first_confirmed_entry_fill_at, "first_confirmed_entry_fill_at")
        hold_until = filled_at + self.minimum_hold
        if instant < hold_until:
            return SessionDecision(False, "MINIMUM_HOLD_NOT_ELAPSED", instant, hold_until=hold_until)
        return SessionDecision(True, "ALLOWED", instant, hold_until=hold_until)


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC")
    return value.astimezone(timezone.utc)
