from datetime import UTC, date, datetime, time

import pytest

from mike_trade.scheduling.calendar import (
    NEW_YORK,
    SessionStatus,
    entries_allowed_by_time,
    entry_cutoff,
    is_regular_hours,
    is_trading_day,
    next_session,
    previous_session,
    session_for,
    session_status,
)


@pytest.mark.parametrize(
    "day",
    [
        date(2026, 4, 3),  # Good Friday
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),  # Independence Day observed
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),  # Christmas
        date(2025, 1, 9),  # National day of mourning override
    ],
)
def test_full_closures_are_not_trading_days(day: date) -> None:
    assert is_trading_day(day) is False


def test_new_years_day_saturday_is_not_observed_on_prior_friday() -> None:
    assert is_trading_day(date(2027, 12, 31)) is True


@pytest.mark.parametrize("day", [date(2026, 11, 27), date(2026, 12, 24)])
def test_early_closes_are_13_et(day: date) -> None:
    session = session_for(day)
    assert session.close_et.timetz().replace(tzinfo=None) == time(13, 0)
    assert session.close_utc == datetime(day.year, day.month, day.day, 18, tzinfo=UTC)
    assert session.is_early_close is True


def test_july_second_is_full_session() -> None:
    session = session_for(date(2026, 7, 2))
    assert session.close_et.timetz().replace(tzinfo=None) == time(16, 0)
    assert session.is_early_close is False


def test_session_utc_times_follow_dst() -> None:
    march_6 = session_for(date(2026, 3, 6))
    march_9 = session_for(date(2026, 3, 9))
    november_2 = session_for(date(2026, 11, 2))
    assert march_6.open_utc == datetime(2026, 3, 6, 14, 30, tzinfo=UTC)
    assert march_9.open_utc == datetime(2026, 3, 9, 13, 30, tzinfo=UTC)
    assert november_2.close_utc == datetime(2026, 11, 2, 21, 0, tzinfo=UTC)


def test_regular_hours_use_half_open_effective_session_interval() -> None:
    session = session_for(date(2026, 11, 27))
    at_open = datetime(2026, 11, 27, 9, 30, tzinfo=NEW_YORK)
    just_before_open = datetime(2026, 11, 27, 9, 29, 59, 999999, tzinfo=NEW_YORK)
    at_close = session.close_et
    assert is_regular_hours(at_open) is True
    assert is_regular_hours(just_before_open) is False
    assert is_regular_hours(at_close) is False


def test_entry_cutoff_uses_early_close() -> None:
    session = session_for(date(2026, 11, 27))
    assert entry_cutoff(session, 15) == datetime(2026, 11, 27, 17, 45, tzinfo=UTC)
    assert entries_allowed_by_time(datetime(2026, 11, 27, 12, 45, tzinfo=NEW_YORK), 15) is False
    assert entries_allowed_by_time(datetime(2026, 11, 27, 12, 44, tzinfo=NEW_YORK), 15) is True


def test_out_of_range_dates_fail_closed() -> None:
    with pytest.raises(ValueError):
        is_trading_day(date(2036, 1, 2))
    assert session_status(datetime(2036, 1, 2, 15, tzinfo=UTC)) is SessionStatus.UNKNOWN
    assert is_regular_hours(datetime(2036, 1, 2, 15, tzinfo=UTC)) is False


def test_session_id_is_deterministic() -> None:
    day = date(2026, 11, 27)
    assert session_for(day).session_id == session_for(day).session_id == "XNYS-2026-11-27"


def test_session_status_and_navigation() -> None:
    assert session_status(datetime(2026, 3, 6, 14, 0, tzinfo=UTC)) is SessionStatus.PRE_OPEN
    assert session_status(datetime(2026, 3, 6, 15, 0, tzinfo=UTC)) is SessionStatus.REGULAR
    assert session_status(datetime(2026, 3, 6, 22, 0, tzinfo=UTC)) is SessionStatus.CLOSED
    assert session_status(datetime(2026, 11, 26, 15, 0, tzinfo=UTC)) is SessionStatus.HOLIDAY
    assert next_session(date(2026, 11, 26)).session_date == date(2026, 11, 27)
    assert previous_session(date(2026, 11, 27)).session_date == date(2026, 11, 25)
