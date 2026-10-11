"""Point-in-time regular-session reference aggregation tests."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.session_reference import SessionReferenceTracker
from mike_trade.scheduling.calendar import NEW_YORK, session_for

D = Decimal


def candle(
    start: datetime,
    *,
    open_: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
    feed_id: str = "sip",
) -> Candle:
    high_value = max(D(high), D(open_), D(close))
    low_value = min(D(low), D(open_), D(close))
    return Candle(
        symbol="SPY",
        timeframe=CandleTimeframe.M1,
        start=start,
        end=start + timedelta(minutes=1),
        open=D(open_),
        high=high_value,
        low=low_value,
        close=D(close),
        volume=D("1000"),
        feed_id=feed_id,
        consolidated=feed_id == "sip",
        origin=CandleOrigin.REPLAY,
        price_basis=PriceBasis.RAW,
        status=CandleStatus.CLOSED,
    )


def add_session(
    tracker: SessionReferenceTracker,
    day: date,
    *,
    skip: int | None = None,
    open_: str = "100",
    close: str = "100",
) -> None:
    session = session_for(day)
    count = int((session.close_utc - session.open_utc) / timedelta(minutes=1))
    for index in range(count):
        if index == skip:
            continue
        tracker.add_bar(
            candle(
                session.open_utc + timedelta(minutes=index),
                open_=open_ if index == 0 else "100",
                high="110" if index == 75 else "101",
                low="90" if index == 125 else "99",
                close=close if index == count - 1 else "100",
            )
        )


def open_current(tracker: SessionReferenceTracker, day: date, price: str = "103") -> datetime:
    session = session_for(day)
    tracker.add_bar(
        candle(
            session.open_utc,
            open_=price,
            high=str(D(price) + 1),
            low=str(D(price) - 1),
            close=price,
        )
    )
    return session.open_utc + timedelta(minutes=1)


def test_complete_previous_session_ohlc_and_current_open_are_stable() -> None:
    tracker = SessionReferenceTracker()
    add_session(tracker, date(2026, 10, 8), open_="98", close="104")
    as_of = open_current(tracker, date(2026, 10, 9), "106")

    first = tracker.reference("SPY", as_of)
    assert first is not None and first.previous is not None
    assert (
        first.previous.open,
        first.previous.high,
        first.previous.low,
        first.previous.close,
        first.current_open,
    ) == (D("98"), D("110"), D("90"), D("104"), D("106"))

    tracker.add_bar(candle(session_for(date(2026, 10, 9)).open_utc + timedelta(minutes=1)))
    later = tracker.reference("SPY", as_of + timedelta(minutes=1))
    assert later is not None
    assert later.previous == first.previous
    assert later.current_open == first.current_open


def test_previous_session_crosses_holiday_and_dst_weekend() -> None:
    holiday = SessionReferenceTracker()
    add_session(holiday, date(2026, 7, 2), close="102")
    holiday_ref = holiday.reference(
        "SPY", open_current(holiday, date(2026, 7, 6))
    )
    assert holiday_ref is not None and holiday_ref.previous is not None
    assert holiday_ref.previous_session_id == "XNYS-2026-07-02"

    dst = SessionReferenceTracker()
    add_session(dst, date(2026, 3, 6), close="101")
    dst_ref = dst.reference("SPY", open_current(dst, date(2026, 3, 9)))
    assert dst_ref is not None and dst_ref.previous is not None
    assert dst_ref.previous.close_time.hour == 21
    assert session_for(date(2026, 3, 9)).open_utc.hour == 13


def test_half_day_is_complete_at_early_close() -> None:
    tracker = SessionReferenceTracker()
    early_day = date(2026, 11, 27)
    assert session_for(early_day).is_early_close
    add_session(tracker, early_day, close="103")
    reference = tracker.reference("SPY", open_current(tracker, date(2026, 11, 30)))
    assert reference is not None and reference.previous is not None
    assert reference.previous.close == D("103")
    assert reference.previous.close_time == session_for(early_day).close_utc


def test_missing_minute_makes_previous_prices_unavailable() -> None:
    tracker = SessionReferenceTracker()
    add_session(tracker, date(2026, 10, 8), skip=200)
    reference = tracker.reference("SPY", open_current(tracker, date(2026, 10, 9)))
    assert reference is not None
    assert reference.previous_session_id == "XNYS-2026-10-08"
    assert reference.previous is None


def test_extended_hours_are_excluded_and_overlap_is_idempotent() -> None:
    tracker = SessionReferenceTracker()
    day = date(2026, 10, 8)
    premarket = datetime.combine(day, time(8), NEW_YORK)
    tracker.add_bar(candle(premarket, open_="1", high="2", low="1", close="2"))
    add_session(tracker, day, open_="99", close="105")
    # A warmup/stream overlap must not corrupt the already observed session.
    tracker.add_bar(candle(session_for(day).open_utc, open_="99"))
    after_hours = datetime.combine(day, time(18), NEW_YORK)
    tracker.add_bar(candle(after_hours, open_="500", high="501", low="499", close="500"))
    reference = tracker.reference("SPY", open_current(tracker, date(2026, 10, 9)))
    assert reference is not None and reference.previous is not None
    assert reference.previous.open == D("99")
    assert reference.previous.close == D("105")


def test_reference_fails_closed_when_tracker_contains_later_data() -> None:
    tracker = SessionReferenceTracker()
    add_session(tracker, date(2026, 10, 8))
    current = session_for(date(2026, 10, 9))
    tracker.add_bar(candle(current.open_utc, open_="102", close="102"))
    reference = tracker.reference("SPY", current.open_utc + timedelta(seconds=30))
    assert reference is not None
    assert reference.current_open is None
    assert reference.previous is None
