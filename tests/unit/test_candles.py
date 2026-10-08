"""Unit tests for deterministic multi-timeframe candle builders."""

import dataclasses
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from mike_trade.market_data.candles import (
    BucketConflictError,
    Candle,
    CandleBuilder,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    InvalidCandleInputError,
    MinuteBar,
    PriceBasis,
    SeriesMismatchError,
    TradeTick,
)

NY = ZoneInfo("America/New_York")
M1, M5, M15, M30, M60 = (
    CandleTimeframe.M1,
    CandleTimeframe.M5,
    CandleTimeframe.M15,
    CandleTimeframe.M30,
    CandleTimeframe.M60,
)
D = Decimal


def ny(h: int, m: int = 0, s: int = 0, *, day: int = 8) -> datetime:
    """2026-10-08 (EDT) wall time in New York, as an aware datetime."""
    return datetime(2026, 10, day, h, m, s, tzinfo=NY)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def tick(ts, price="100", size="10", **kw) -> TradeTick:
    kw.setdefault("symbol", "AAPL")
    kw.setdefault("feed_id", "iex")
    kw.setdefault("consolidated", False)
    return TradeTick(ts=ts, price=price, size=size, **kw)


def bar(start, o="100", h="101", lo="99", c="100.5", v="1000", **kw) -> MinuteBar:
    kw.setdefault("symbol", "AAPL")
    kw.setdefault("feed_id", "iex")
    kw.setdefault("consolidated", False)
    return MinuteBar(start=start, open=o, high=h, low=lo, close=c, volume=v, **kw)


def minute_bars(builder: CandleBuilder, first: datetime, count: int, **kw) -> None:
    for i in range(count):
        px = D(100 + i)
        builder.on_minute_bar(
            bar(first + timedelta(minutes=i), o=px, h=px + 1, lo=px - 1, c=px + D("0.5"), **kw)
        )


# --------------------------------------------------------------------- Candle value type


def _candle(**over) -> Candle:
    base = dict(
        symbol="AAPL", timeframe=M1, start=ny(9, 30), end=ny(9, 31), open=D("10"), high=D("11"),
        low=D("9"), close=D("10.5"), volume=D("5"), feed_id="iex", consolidated=False,
        origin=CandleOrigin.REPLAY, price_basis=PriceBasis.RAW, status=CandleStatus.CLOSED,
    )
    base.update(over)
    return Candle(**base)


def test_candle_is_frozen_and_utc_normalized():
    c = _candle()
    assert c.start == ny(9, 30) and c.start.utcoffset() == timedelta(0)
    assert c.is_closed
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.close = D("1")  # type: ignore[misc]


@pytest.mark.parametrize(
    "over",
    [
        {"open": D("0"), "low": D("0")},
        {"low": D("-1")},
        {"volume": D("-1")},
        {"high": D("8")},  # high < low
        {"open": D("12")},  # open above high
        {"close": D("8")},  # close below low
        {"start": datetime(2026, 10, 8, 9, 30), "end": datetime(2026, 10, 8, 9, 31)},
        {"end": ny(9, 32)},  # not start + duration
        {"open": 10.0},  # float
        {"consolidated": True, "feed_id": "iex"},
    ],
)
def test_candle_rejects_invalid(over):
    with pytest.raises(InvalidCandleInputError):
        _candle(**over)


# --------------------------------------------------------------------- input validation


@pytest.mark.parametrize(
    "factory",
    [
        lambda: tick(ny(9, 30), price="0"),
        lambda: tick(ny(9, 30), price="-1"),
        lambda: tick(ny(9, 30), size="-1"),
        lambda: tick(datetime(2026, 10, 8, 9, 30)),
        lambda: tick(ny(9, 30), price=1.5),
        lambda: tick(ny(9, 30), origin=CandleOrigin.STREAM_BAR),
        lambda: bar(ny(9, 30), h="98", lo="99"),
        lambda: bar(ny(9, 30), o="102"),
        lambda: bar(ny(9, 30), c="98"),
        lambda: bar(ny(9, 30), v="-1"),
        lambda: bar(ny(9, 30), lo="0"),
        lambda: bar(datetime(2026, 10, 8, 9, 30)),
        lambda: bar(ny(9, 30, 15)),
        lambda: bar(ny(9, 30), trade_count=-1),
    ],
)
def test_invalid_inputs_raise_typed_error(factory):
    with pytest.raises(InvalidCandleInputError):
        factory()


def test_builder_rejects_wrong_input_type_without_state_change():
    b = CandleBuilder()
    with pytest.raises(InvalidCandleInputError):
        b.on_trade("not a tick")  # type: ignore[arg-type]
    with pytest.raises(InvalidCandleInputError):
        b.on_minute_bar(tick(ny(9, 30)))  # type: ignore[arg-type]
    assert b.get_developing_candle("AAPL", M1) is None
    assert b.late_counts() == {}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeframes": ()},
        {"max_history": 0},
        {"grace": timedelta(seconds=-1)},
        {"session_open": time(9, 30, tzinfo=NY)},
        {"session_open": "09:30"},
    ],
)
def test_builder_rejects_bad_config(kwargs):
    with pytest.raises(InvalidCandleInputError):
        CandleBuilder(**kwargs)


# --------------------------------------------------------------------- tick -> 1m


def test_ticks_build_one_minute_candle_and_close_on_next_minute():
    b = CandleBuilder()
    assert b.on_trade(tick(ny(9, 30, 5), "100", "10")).closed == ()
    b.on_trade(tick(ny(9, 30, 20), "103", "5"))
    b.on_trade(tick(ny(9, 30, 40), "99", "7"))
    b.on_trade(tick(ny(9, 30, 59), "101", "3"))
    dev = b.get_developing_candle("AAPL", M1)
    assert dev is not None and dev.status is CandleStatus.DEVELOPING
    assert (dev.open, dev.high, dev.low, dev.close) == (D(100), D(103), D(99), D(101))
    assert dev.volume == D(25) and dev.trade_count == 4
    assert dev.origin is CandleOrigin.STREAM_TRADE
    assert b.get_completed_candles("AAPL", M1) == ()

    result = b.on_trade(tick(ny(9, 31), "102", "1"))  # ts == end closes the previous bucket
    assert not result.late
    (closed,) = result.closed
    assert closed.status is CandleStatus.CLOSED
    assert (closed.start, closed.end) == (ny(9, 30), ny(9, 31))
    assert (closed.open, closed.high, closed.low, closed.close) == (D(100), D(103), D(99), D(101))
    assert b.get_completed_candles("AAPL", M1) == (closed,)
    assert b.get_developing_candle("AAPL", M1).start == ny(9, 31)


def test_out_of_order_ticks_within_open_bucket_are_deterministic():
    b = CandleBuilder()
    b.on_trade(tick(ny(9, 30, 30), "105"))
    b.on_trade(tick(ny(9, 30, 10), "100"))  # earlier: becomes open
    b.on_trade(tick(ny(9, 30, 50), "102"))
    b.on_trade(tick(ny(9, 30, 40), "999"))  # earlier than close: must not become close
    dev = b.get_developing_candle("AAPL", M1)
    assert (dev.open, dev.close) == (D(100), D(102))


def test_completed_accessor_never_returns_developing():
    b = CandleBuilder()
    b.on_trade(tick(ny(9, 30, 1)))
    b.on_trade(tick(ny(9, 31, 1)))
    assert all(c.status is CandleStatus.CLOSED for c in b.get_completed_candles("AAPL", M1))
    assert b.get_developing_candle("AAPL", M1) is not None
    assert len(b.get_completed_candles("AAPL", M1)) == 1


# --------------------------------------------------------------------- bars -> higher TFs


def test_minute_bars_aggregate_to_all_higher_timeframes():
    b = CandleBuilder()
    minute_bars(b, ny(9, 30), 61)  # 09:30..10:30; the last bar only closes 10:29
    (c5,) = b.get_completed_candles("AAPL", M5, limit=1)
    assert c5.start == ny(10, 25) and c5.end == ny(10, 30)  # the 10:30 bar start closes it
    first5 = b.get_completed_candles("AAPL", M5)[0]
    assert (first5.start, first5.end) == (ny(9, 30), ny(9, 35))
    assert first5.open == D(100) and first5.close == D(104) + D("0.5")
    assert first5.high == D(105) and first5.low == D(99)
    assert first5.volume == D(5000)
    assert first5.origin is CandleOrigin.STREAM_BAR
    assert first5.status is CandleStatus.CLOSED
    assert len(b.get_completed_candles("AAPL", M15)) == 4  # 09:30 .. 10:15, all end <= 10:30
    assert [c.start for c in b.get_completed_candles("AAPL", M30)] == [ny(9, 30), ny(10, 0)]


def test_hourly_bucket_aligns_to_session_open_anchor():
    b = CandleBuilder(session_open=time(9, 30))
    minute_bars(b, ny(9, 30), 61)
    (h,) = b.get_completed_candles("AAPL", M60)
    assert (h.start, h.end) == (ny(9, 30), ny(10, 30))
    assert h.volume == D(60000)
    assert h.trade_count is None  # bars carried no trade_count


def test_hourly_bucket_defaults_to_clock_hour_without_anchor():
    b = CandleBuilder()
    assert b.bucket_start(ny(10, 47), M60) == ny(10, 0)
    assert b.bucket_start(ny(10, 47), M30) == ny(10, 30)
    assert b.bucket_start(ny(10, 47), M15) == ny(10, 45)
    assert b.bucket_start(ny(10, 47), M5) == ny(10, 45)


def test_anchor_callable_and_none_fallback():
    seen: list[date] = []

    def anchor(d: date) -> time | None:
        seen.append(d)
        return time(9, 30) if d == date(2026, 10, 8) else None

    b = CandleBuilder(session_open=anchor)
    assert b.bucket_start(ny(10, 29), M60) == ny(9, 30)
    assert b.bucket_start(ny(10, 29, day=9), M60) == ny(10, 0, day=9)
    assert seen == [date(2026, 10, 8), date(2026, 10, 9)]


def test_bucket_alignment_survives_dst_boundary():
    b = CandleBuilder(session_open=time(9, 30))
    # 2026-11-02 is EST (DST ended 2026-11-01); anchor is 09:30 ET both days.
    ts = datetime(2026, 11, 2, 10, 15, tzinfo=NY)
    assert b.bucket_start(ts, M60) == datetime(2026, 11, 2, 9, 30, tzinfo=NY)
    assert b.bucket_start(ts, M60).utcoffset() == timedelta(0)


def test_ticks_cascade_into_higher_timeframes_with_trade_counts():
    b = CandleBuilder()
    for minute in range(5):
        b.on_trade(tick(ny(9, 30 + minute, 10), 100 + minute, 2))
        b.on_trade(tick(ny(9, 30 + minute, 50), 100 + minute, 3))
    result = b.on_trade(tick(ny(9, 35, 0), "50"))
    five = [c for c in result.closed if c.timeframe is M5]
    assert len(five) == 1
    c = five[0]
    assert c.trade_count == 10 and c.volume == D(25)
    assert (c.open, c.close, c.high, c.low) == (D(100), D(104), D(104), D(100))
    assert c.origin is CandleOrigin.STREAM_TRADE


def test_mixed_origins_marked_mixed_on_aggregate():
    b = CandleBuilder()
    b.on_minute_bar(bar(ny(9, 30), origin=CandleOrigin.HISTORICAL_REST))
    b.on_minute_bar(bar(ny(9, 31), origin=CandleOrigin.STREAM_BAR))
    b.on_minute_bar(bar(ny(9, 35)))
    (c5,) = b.get_completed_candles("AAPL", M5)
    assert c5.origin is CandleOrigin.MIXED


def test_subset_of_timeframes_still_builds_one_minute():
    b = CandleBuilder(timeframes=(M5,))
    minute_bars(b, ny(9, 30), 6)
    assert len(b.get_completed_candles("AAPL", M1)) == 5
    assert len(b.get_completed_candles("AAPL", M5)) == 1
    assert b.get_completed_candles("AAPL", M15) == ()


# --------------------------------------------------------------------- finalization / late


def test_closed_candle_is_never_mutated_and_late_event_is_counted():
    b = CandleBuilder()
    b.on_trade(tick(ny(9, 30, 10), "100"))
    b.on_trade(tick(ny(9, 31, 10), "110"))
    (closed,) = b.get_completed_candles("AAPL", M1)
    result = b.on_trade(tick(ny(9, 30, 55), "500", "99"))
    assert result.late and result.closed == ()
    assert b.get_completed_candles("AAPL", M1) == (closed,)
    assert closed.high == D(100) and closed.volume == D(10)
    assert b.late_count("AAPL") == 1
    assert dict(b.late_counts()) == {("AAPL", M1): 1}
    # the developing candle is untouched too
    assert b.get_developing_candle("AAPL", M1).high == D(110)


def test_late_bar_for_closed_bucket_is_dropped():
    b = CandleBuilder()
    minute_bars(b, ny(9, 30), 7)
    before = b.get_completed_candles("AAPL", M5)
    assert b.on_minute_bar(bar(ny(9, 31), h="900")).late
    assert b.get_completed_candles("AAPL", M5) == before
    assert b.late_count("AAPL") == 1


def test_clock_closes_bucket_after_grace():
    clock = FakeClock(ny(9, 30, 30))
    b = CandleBuilder(clock=clock, grace=timedelta(seconds=5))
    b.on_trade(tick(ny(9, 30, 10)))
    clock._now = ny(9, 31, 4)  # end + grace is 09:31:05
    assert b.close_due() == ()
    assert b.get_completed_candles("AAPL", M1) == ()
    clock._now = ny(9, 31, 5)
    (closed,) = b.close_due()
    assert closed.status is CandleStatus.CLOSED and closed.end == ny(9, 31)
    assert b.get_developing_candle("AAPL", M1) is None
    assert b.close_due() == ()  # idempotent


def test_clock_close_makes_later_event_late_and_cascades_higher_timeframe():
    clock = FakeClock(ny(9, 30))
    b = CandleBuilder(clock=clock)
    b.on_trade(tick(ny(9, 34, 30)))
    clock._now = ny(9, 35, 0)
    closed = b.close_due()
    assert {c.timeframe for c in closed} == {M1, M5}
    assert b.on_trade(tick(ny(9, 34, 40), "7")).late
    assert b.late_count("AAPL") == 1


def test_clock_is_consulted_on_ingest():
    clock = FakeClock(ny(9, 36))
    b = CandleBuilder(clock=clock)
    assert b.on_trade(tick(ny(9, 30, 10))).late  # clock already past its end


def test_grace_does_not_delay_timestamp_driven_close():
    b = CandleBuilder(grace=timedelta(minutes=10), clock=FakeClock(ny(9, 0)))
    b.on_trade(tick(ny(9, 30, 10)))
    assert len(b.on_trade(tick(ny(9, 31, 0))).closed) == 1


# --------------------------------------------------------------------- provenance


@pytest.mark.parametrize(
    "kw",
    [
        {"feed_id": "sip", "consolidated": True},
        {"price_basis": PriceBasis.SPLIT_ADJUSTED},
        {"feed_id": "otc", "consolidated": False},
    ],
)
def test_provenance_mixing_raises_and_changes_nothing(kw):
    b = CandleBuilder()
    b.on_trade(tick(ny(9, 30, 10), "100"))
    snapshot = b.get_developing_candle("AAPL", M1)
    with pytest.raises(SeriesMismatchError):
        b.on_trade(tick(ny(9, 30, 20), "101", **kw))
    with pytest.raises(SeriesMismatchError):
        b.on_minute_bar(bar(ny(9, 31), **kw))
    assert b.get_developing_candle("AAPL", M1) == snapshot
    assert b.late_counts() == {}


def test_mismatching_late_event_raises_rather_than_counting():
    b = CandleBuilder()
    b.on_trade(tick(ny(9, 30, 10)))
    b.on_trade(tick(ny(9, 40)))
    with pytest.raises(SeriesMismatchError):
        b.on_trade(tick(ny(9, 30, 20), feed_id="sip", consolidated=True))
    assert b.late_counts() == {}


def test_failed_mismatch_leaves_state_identical():
    b = CandleBuilder()
    b.on_trade(tick(ny(9, 30, 10), "100"))
    snapshot = b.get_developing_candle("AAPL", M1)
    with pytest.raises(SeriesMismatchError):
        b.on_trade(tick(ny(9, 30, 20), "101", price_basis=PriceBasis.SPLIT_ADJUSTED))
    assert b.get_developing_candle("AAPL", M1) == snapshot


def test_symbols_are_independent_series():
    b = CandleBuilder()
    b.on_trade(tick(ny(9, 30, 10), symbol="AAPL"))
    b.on_trade(tick(ny(9, 30, 10), symbol="MSFT", feed_id="sip", consolidated=True))
    assert b.get_developing_candle("MSFT", M1).feed_id == "sip"
    assert b.get_developing_candle("AAPL", M1).consolidated is False


def test_candle_carries_provenance_and_basis():
    b = CandleBuilder()
    b.on_trade(
        tick(ny(9, 30, 10), price_basis=PriceBasis.SPLIT_ADJUSTED, origin=CandleOrigin.REPLAY)
    )
    c = b.get_developing_candle("AAPL", M1)
    assert (c.feed_id, c.consolidated, c.price_basis, c.origin) == (
        "iex", False, PriceBasis.SPLIT_ADJUSTED, CandleOrigin.REPLAY,
    )


# --------------------------------------------------------------------- bucket conflicts


def test_bar_into_populated_bucket_conflicts_without_state_change():
    b = CandleBuilder()
    b.on_minute_bar(bar(ny(9, 30)))
    snapshot = b.get_developing_candle("AAPL", M1)
    with pytest.raises(BucketConflictError):
        b.on_minute_bar(bar(ny(9, 30), h="150"))
    with pytest.raises(BucketConflictError):
        b.on_trade(tick(ny(9, 30, 20)))
    assert b.get_developing_candle("AAPL", M1) == snapshot


def test_bar_into_tick_populated_bucket_conflicts():
    b = CandleBuilder()
    b.on_trade(tick(ny(9, 30, 20)))
    with pytest.raises(BucketConflictError):
        b.on_minute_bar(bar(ny(9, 30)))


# --------------------------------------------------------------------- bounded history


def test_history_is_bounded_per_series():
    b = CandleBuilder(max_history=3)
    minute_bars(b, ny(9, 30), 12)
    m1 = b.get_completed_candles("AAPL", M1)
    assert len(m1) == 3
    assert m1[-1].start == ny(9, 40)  # 12 bars, last (09:41) still developing
    assert len(b.get_completed_candles("AAPL", M5)) == 2
    assert len(b.get_completed_candles("AAPL", M1, limit=2)) == 2
    assert b.get_completed_candles("AAPL", M1, limit=0) == ()
    assert b.get_latest_completed_candle("AAPL", M1) == m1[-1]
    assert b.get_latest_completed_candle("MSFT", M1) is None


def test_decimal_precision_is_exact():
    b = CandleBuilder()
    for i in range(3):
        b.on_trade(tick(ny(9, 30, i), "0.1", "0.1"))
    assert b.get_developing_candle("AAPL", M1).volume == D("0.3")
