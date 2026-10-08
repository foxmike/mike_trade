"""Unit tests for the replay clock and the as-of historical replay feed.

Small synthetic in-memory fixtures only: no network, credentials, or Alpaca imports.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.feed import (
    DataProvenance,
    FutureAdjustedDataError,
    HistoricalReplayFeed,
    LookaheadError,
    QuoteFidelity,
    ReplayEvent,
)
from mike_trade.domain.accounts import MarketDataCapabilities
from mike_trade.domain.market import (
    Candle,
    DataFreshness,
    FeedProvenance,
    MarketEvent,
    MarketEventKind,
    Timeframe,
)
from mike_trade.market_data.ports import MarketDataPort
from mike_trade.market_data.quality import HealthState
from mike_trade.runtime import ClockPort
from mike_trade.scheduling.calendar import SessionStatus

D = Decimal
MIN = timedelta(minutes=1)
# 2026-03-02 is a Monday, EST (UTC-5): regular session 14:30-21:00 UTC.
OPEN = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
FEED_PROV = FeedProvenance(provider="replay", feed="IEX", source="fixture", is_consolidated=False)


def _prov(**kw: object) -> DataProvenance:
    base: dict[str, object] = {
        "source": "fixture", "feed": "IEX", "is_consolidated": False, "revision": "r1",
    }
    base.update(kw)
    return DataProvenance(**base)  # type: ignore[arg-type]


def _candle(start: datetime, close: str = "10", symbol: str = "AAA", **kw: object) -> Candle:
    c = D(close)
    return Candle(
        symbol=symbol, timeframe=Timeframe.M1, start=start, end=start + MIN,
        open=c, high=c + 1, low=c - 1, close=c, volume=D("100"), provenance=FEED_PROV, **kw,
    )  # type: ignore[arg-type]


def _bar(offset: int, close: str = "10", symbol: str = "AAA", **kw: object) -> ReplayEvent:
    return ReplayEvent.bar(_candle(OPEN + offset * MIN, close, symbol), **kw)  # type: ignore[arg-type]


def _quote(offset: int, symbol: str = "AAA") -> ReplayEvent:
    at = OPEN + offset * MIN
    return ReplayEvent.from_quote(MarketEvent(
        symbol=symbol, kind=MarketEventKind.QUOTE, event_time=at, received_at=at,
        provenance=FEED_PROV, bid=D("9.99"), ask=D("10.01"),
    ))


# ------------------------------------------------------------------ ReplayClock


def test_clock_conforms_to_clock_port_and_normalizes_to_utc() -> None:
    clock = ReplayClock(datetime(2026, 3, 2, 9, 0, tzinfo=timezone(timedelta(hours=-5))))
    port: ClockPort = clock
    assert port.now() == datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
    assert port.now().utcoffset() == timedelta(0)


def test_clock_only_moves_forward() -> None:
    clock = ReplayClock(OPEN)
    assert clock.advance_to(OPEN) == OPEN  # equal instant is a no-op
    assert clock.advance_to(OPEN + MIN) == OPEN + MIN
    with pytest.raises(ValueError, match="backwards"):
        clock.advance_to(OPEN)
    assert clock.now() == OPEN + MIN  # failed move leaves time untouched


def test_clock_rejects_naive_datetimes_and_non_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ReplayClock(datetime(2026, 3, 2, 14, 30))
    clock = ReplayClock(OPEN)
    with pytest.raises(ValueError, match="timezone-aware"):
        clock.advance_to(datetime(2026, 3, 2, 15, 0))
    with pytest.raises(ValueError, match="datetime"):
        clock.advance_to("2026-03-02")  # type: ignore[arg-type]


def test_clock_does_not_read_wall_clock_or_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("wall clock / sleep used")

    monkeypatch.setattr(time, "sleep", boom)
    monkeypatch.setattr(time, "time", boom)
    clock = ReplayClock(datetime(2030, 1, 1, tzinfo=UTC))
    clock.advance_to(datetime(2030, 1, 2, tzinfo=UTC))
    assert clock.now() == datetime(2030, 1, 2, tzinfo=UTC)


def test_clock_regular_session_presentation_and_boundaries() -> None:
    clock = ReplayClock(OPEN - MIN)
    assert clock.session_status() is SessionStatus.PRE_OPEN
    assert clock.time_to_close() is None
    assert not clock.at_session_open()
    clock.advance_to(OPEN)
    assert clock.at_session_open() and clock.is_regular_hours()
    assert clock.local_now().hour == 9 and clock.local_now().minute == 30
    assert clock.session_open() == OPEN
    assert clock.session_close() == datetime(2026, 3, 2, 21, 0, tzinfo=UTC)
    assert clock.time_to_close() == timedelta(hours=6, minutes=30)
    assert not clock.is_early_close()
    clock.advance_to(datetime(2026, 3, 2, 21, 0, tzinfo=UTC))
    assert clock.at_session_close()
    assert clock.session_status() is SessionStatus.CLOSED  # close instant is exclusive
    assert clock.time_to_close() is None


def test_clock_early_close_day_after_thanksgiving() -> None:
    clock = ReplayClock(datetime(2026, 11, 27, 17, 59, tzinfo=UTC))  # 12:59 ET
    assert clock.is_early_close()
    assert clock.session_close() == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    assert clock.is_regular_hours()
    clock.advance_to(datetime(2026, 11, 27, 18, 0, tzinfo=UTC))
    assert clock.at_session_close() and not clock.is_regular_hours()


def test_clock_holiday_and_weekend_have_no_session() -> None:
    holiday = ReplayClock(datetime(2026, 12, 25, 15, 0, tzinfo=UTC))
    assert holiday.session_status() is SessionStatus.HOLIDAY
    assert holiday.current_session() is None
    assert holiday.session_open() is None and holiday.session_close() is None
    assert not holiday.at_session_open() and not holiday.at_session_close()
    assert holiday.next_session().session_date == date(2026, 12, 28)
    weekend = ReplayClock(datetime(2026, 3, 1, 15, 0, tzinfo=UTC))
    assert weekend.session_status() is SessionStatus.CLOSED
    assert weekend.current_session() is None
    assert weekend.next_session().session_date == date(2026, 3, 2)


def test_clock_out_of_calendar_range_is_unknown_not_an_error() -> None:
    clock = ReplayClock(datetime(2099, 1, 5, 15, 0, tzinfo=UTC))
    assert clock.session_status() is SessionStatus.UNKNOWN
    assert clock.current_session() is None


# ------------------------------------------------------------ provenance / hash


def test_provenance_validation() -> None:
    with pytest.raises(ValueError):
        _prov(adjustment="BOGUS")
    with pytest.raises(ValueError):
        _prov(source=" ")
    with pytest.raises(ValueError):
        _prov(dataset_hash="xyz")
    assert _prov().feed_provenance().is_consolidated is False


def test_dataset_hash_is_filled_deterministic_and_verified() -> None:
    events = [_bar(0), _bar(1, "11")]
    feed = HistoricalReplayFeed(events, _prov())
    digest = feed.provenance.dataset_hash
    assert digest == HistoricalReplayFeed.compute_dataset_hash(
        [
            ReplayEvent.bar(_candle(OPEN), sequence=0),
            ReplayEvent.bar(_candle(OPEN + MIN, "11"), sequence=1),
        ]
    )
    verified = HistoricalReplayFeed(events, _prov(dataset_hash=digest))
    assert verified.provenance.dataset_hash == digest
    with pytest.raises(ValueError, match="hash"):
        HistoricalReplayFeed([_bar(0), _bar(1, "12")], _prov(dataset_hash=digest))


# --------------------------------------------------------------- availability


def test_bar_available_only_at_completion_plus_latency() -> None:
    feed = HistoricalReplayFeed([_bar(0)], _prov(), availability_latency=timedelta(seconds=5))
    completion = OPEN + MIN
    assert feed.as_of(OPEN) == ()  # developing bar is never exposed
    assert feed.as_of(completion) == ()
    assert feed.as_of(completion + timedelta(seconds=4)) == ()
    got = feed.as_of(completion + timedelta(seconds=5))
    assert len(got) == 1 and got[0].available_at == completion + timedelta(seconds=5)


def test_as_of_never_returns_future_records() -> None:
    feed = HistoricalReplayFeed([_bar(i) for i in range(5)] + [_quote(2)], _prov())
    for step in range(0, 8):
        t = OPEN + step * MIN
        assert all(e.available_at <= t for e in feed.as_of(t))  # type: ignore[operator]


def test_as_of_rejects_naive_time_and_lookahead_past_attached_clock() -> None:
    clock = ReplayClock(OPEN)
    feed = HistoricalReplayFeed([_bar(0)], _prov(), clock=clock)
    with pytest.raises(ValueError, match="timezone-aware"):
        feed.as_of(datetime(2026, 3, 2, 15, 0))
    with pytest.raises(LookaheadError):
        feed.as_of(OPEN + MIN)
    clock.advance_to(OPEN + MIN)
    assert len(feed.as_of(OPEN + MIN)) == 1


def test_developing_candle_rejected() -> None:
    partial = _candle(OPEN, closed=False)
    with pytest.raises(ValueError, match="developing"):
        ReplayEvent.bar(partial)


def test_non_minute_timeframe_and_unaligned_bar_rejected() -> None:
    five = Candle(
        symbol="AAA", timeframe=Timeframe.M5, start=OPEN, end=OPEN + 5 * MIN, open=D(1),
        high=D(2), low=D(1), close=D(1), volume=D(1), provenance=FEED_PROV,
    )
    with pytest.raises(ValueError, match="1-minute"):
        ReplayEvent.bar(five)
    with pytest.raises(ValueError, match="minute boundary"):
        ReplayEvent.bar(_candle(OPEN + timedelta(seconds=10)))


def test_floats_rejected_for_split_ratio() -> None:
    with pytest.raises(ValueError, match="Decimal"):
        ReplayEvent.split("AAA", OPEN, 2.0)  # type: ignore[arg-type]


def test_feed_mismatch_between_candle_and_dataset_rejected() -> None:
    with pytest.raises(ValueError, match="feed"):
        HistoricalReplayFeed([_bar(0)], _prov(feed="SIP", is_consolidated=True))


# ------------------------------------------------------------------- ordering


def test_strict_order_with_deterministic_tie_break() -> None:
    at_split = OPEN + MIN
    events = [
        _quote(1, "BBB"), _bar(0, symbol="BBB"), _bar(0, symbol="AAA"), _quote(1, "AAA"),
        ReplayEvent.split("ZZZ", at_split, D("2")),
        ReplayEvent.quality("AAA", at_split, "X", DataFreshness.STALE, HealthState.DEGRADED),
    ]
    feed = HistoricalReplayFeed(events, _prov(), availability_latency=timedelta(0))
    got = [(e.available_at, e.kind, e.symbol) for e in feed.events()]
    # all at OPEN+1m: SPLIT < DATA_QUALITY < BAR < QUOTE, then symbol
    assert got == [
        (at_split, "SPLIT", "ZZZ"),
        (at_split, "DATA_QUALITY", "AAA"),
        (at_split, "BAR", "AAA"),
        (at_split, "BAR", "BBB"),
        (at_split, "QUOTE", "AAA"),
        (at_split, "QUOTE", "BBB"),
    ]
    times = [e.available_at for e in feed.events()]
    assert times == sorted(times)  # type: ignore[type-var]


def test_sequence_breaks_remaining_ties() -> None:
    first = _bar(0, "10", sequence=2)
    second = ReplayEvent.bar(_candle(OPEN, "11"), sequence=1, revision_at=OPEN)  # same bar, vintage
    feed = HistoricalReplayFeed([first, second], _prov(point_in_time=True))
    assert [e.sequence for e in feed.events()] == [1, 2]


def test_same_dataset_and_seed_identical_sequence_and_rng() -> None:
    def build(seed: int) -> HistoricalReplayFeed:
        return HistoricalReplayFeed([_bar(i, symbol=s) for i in range(3) for s in ("AAA", "BBB")],
                                    _prov(), seed=seed)

    a, b, c = build(7), build(7), build(8)
    assert list(a.events()) == list(b.events())
    assert a.seed == 7 and c.seed == 8
    assert a.provenance.dataset_hash == b.provenance.dataset_hash
    assert [a.rng("x").random() for _ in range(3)] == [b.rng("x").random() for _ in range(3)]
    assert a.rng("x").random() != c.rng("x").random()
    assert a.rng("x").random() != a.rng("y").random()
    with pytest.raises(ValueError, match="seed"):
        HistoricalReplayFeed([], _prov(), seed=True)  # type: ignore[arg-type]


def test_duplicate_bar_vintage_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        HistoricalReplayFeed([_bar(0), _bar(0, "11")], _prov())


def test_dataset_bound_enforced() -> None:
    with pytest.raises(ValueError, match="max_events"):
        HistoricalReplayFeed([_bar(0), _bar(1)], _prov(), max_events=1)


# ------------------------------------------------ future-adjusted / revisions


def test_adjusted_series_refused_unless_point_in_time() -> None:
    with pytest.raises(FutureAdjustedDataError):
        HistoricalReplayFeed([_bar(0)], _prov(adjustment="SPLIT_ADJUSTED"))
    feed = HistoricalReplayFeed([_bar(0)], _prov(adjustment="SPLIT_ADJUSTED", point_in_time=True))
    assert len(feed.as_of(OPEN + MIN)) == 1


def test_revised_bar_with_later_vintage_refused_without_point_in_time() -> None:
    later = OPEN + timedelta(days=3)
    with pytest.raises(FutureAdjustedDataError):
        HistoricalReplayFeed([_bar(0, revision_at=later)], _prov())


def test_split_adjustment_announced_after_evaluation_time_refused() -> None:
    announced = OPEN + timedelta(days=30)
    with pytest.raises(FutureAdjustedDataError):
        HistoricalReplayFeed([_bar(0, adjustment_announced_at=announced)], _prov())


def test_point_in_time_revision_is_withheld_until_vintage_and_supersedes() -> None:
    revised_at = OPEN + timedelta(hours=1)
    original = _bar(0, "10")
    revised = _bar(0, "12", revision_at=revised_at)
    feed = HistoricalReplayFeed([original, revised], _prov(point_in_time=True))
    before = feed.as_of(revised_at - MIN)
    assert [e.candle.close for e in before if e.candle] == [D("10")]
    after = feed.as_of(revised_at)
    assert [e.candle.close for e in after if e.candle] == [D("12")]  # old vintage superseded


# ----------------------------------------------------------------- corp actions


def test_split_applied_only_from_effective_time() -> None:
    effective = OPEN + timedelta(days=1)
    feed = HistoricalReplayFeed([_bar(0), ReplayEvent.split("AAA", effective, D("2"))], _prov())
    assert feed.split_factor("AAA", effective - MIN) == D(1)
    assert feed.as_of(effective - MIN, kinds=["SPLIT"]) == ()
    assert feed.split_factor("AAA", effective) == D(2)
    assert feed.split_factor("BBB", effective) == D(1)
    with pytest.raises(ValueError):
        ReplayEvent.split("AAA", effective, D(1))


# ------------------------------------------------------------ data quality


def test_missing_bars_become_gap_markers_never_synthetic_bars() -> None:
    feed = HistoricalReplayFeed([_bar(0), _bar(4)], _prov())
    markers = [e for e in feed.events() if e.kind == ReplayEvent.KIND_DATA_QUALITY]
    assert len(markers) == 1
    gap = markers[0]
    assert gap.reason == "UNRESOLVED_GAP" and gap.detail == "missing_bars=3"
    assert gap.freshness is DataFreshness.STALE and gap.health is HealthState.DEGRADED
    assert gap.event_time == OPEN + MIN and gap.window_end == OPEN + 4 * MIN
    # known only once the revealing bar completes, not when the gap began
    assert gap.available_at == OPEN + 5 * MIN
    assert feed.as_of(OPEN + 4 * MIN, kinds=["DATA_QUALITY"]) == ()
    bars = [e for e in feed.events() if e.kind == ReplayEvent.KIND_BAR]
    assert [e.event_time for e in bars] == [OPEN, OPEN + 4 * MIN]  # nothing forward-filled


def test_no_gap_marker_across_sessions_or_outside_regular_hours() -> None:
    next_day = OPEN + timedelta(days=1)
    pre_market = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
    events = [
        ReplayEvent.bar(_candle(OPEN)),
        ReplayEvent.bar(_candle(next_day)),
        ReplayEvent.bar(_candle(pre_market)),
        ReplayEvent.bar(_candle(pre_market + 10 * MIN)),
    ]
    feed = HistoricalReplayFeed(events, _prov())
    assert not [e for e in feed.events() if e.kind == ReplayEvent.KIND_DATA_QUALITY]


def test_halt_marker_suppresses_gap_for_halted_minutes() -> None:
    halt = ReplayEvent.halt("AAA", OPEN + MIN, OPEN + 4 * MIN)
    feed = HistoricalReplayFeed([_bar(0), _bar(4), halt], _prov())
    kinds = [(e.kind, e.reason) for e in feed.events()]
    assert ("DATA_QUALITY", "TRADING_HALT") in kinds
    assert ("DATA_QUALITY", "UNRESOLVED_GAP") not in kinds
    open_halt = HistoricalReplayFeed(
        [_bar(0), _bar(6), ReplayEvent.halt("AAA", OPEN + 2 * MIN)], _prov()
    )
    gaps = [e for e in open_halt.events() if e.reason == "UNRESOLVED_GAP"]
    assert [g.detail for g in gaps] == ["missing_bars=1"]  # only minute 1 precedes the halt


def test_halt_window_validation() -> None:
    with pytest.raises(ValueError, match="window_end"):
        ReplayEvent.halt("AAA", OPEN, OPEN)


# ---------------------------------------------------------------- quote fidelity


def test_quote_fidelity_unavailable_without_quotes() -> None:
    feed = HistoricalReplayFeed([_bar(0)], _prov())
    assert feed.quote_fidelity() is QuoteFidelity.UNAVAILABLE
    assert feed.quote_fidelity("AAA") is QuoteFidelity.UNAVAILABLE


def test_quote_fidelity_sampled_or_bar_derived_when_quotes_exist() -> None:
    feed = HistoricalReplayFeed([_bar(0), _quote(0, "AAA")], _prov())
    assert feed.quote_fidelity() is QuoteFidelity.SAMPLED
    assert feed.quote_fidelity("AAA") is QuoteFidelity.SAMPLED
    assert feed.quote_fidelity("BBB") is QuoteFidelity.UNAVAILABLE
    derived = HistoricalReplayFeed([_quote(0)], _prov(quote_fidelity=QuoteFidelity.BAR_DERIVED))
    assert derived.quote_fidelity() is QuoteFidelity.BAR_DERIVED
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        HistoricalReplayFeed([_quote(0)], _prov(quote_fidelity=QuoteFidelity.UNAVAILABLE))


# ------------------------------------------------------------------- port usage


def test_feed_satisfies_market_data_port_and_capabilities() -> None:
    feed = HistoricalReplayFeed([_bar(0)], _prov())
    assert isinstance(feed, MarketDataPort)
    caps = feed.capabilities()
    assert isinstance(caps, MarketDataCapabilities)
    assert caps.feed == "IEX" and caps.is_consolidated is False
    assert caps.supported_timeframes == frozenset({"1m"})


def test_subscribe_advances_clock_to_availability_and_filters_symbols() -> None:
    clock = ReplayClock(OPEN)
    events = [_bar(0, symbol="AAA"), _bar(1, symbol="BBB"), _bar(2, symbol="AAA"), _quote(2, "AAA")]
    feed = HistoricalReplayFeed(events, _prov(), clock=clock)
    seen: list[tuple[datetime, MarketEvent]] = []

    async def run() -> None:
        stream = await feed.subscribe(["aaa"])
        async for event in stream:
            seen.append((clock.now(), event))

    asyncio.run(run())
    assert [(e.kind, e.symbol) for _, e in seen] == [
        (MarketEventKind.BAR, "AAA"), (MarketEventKind.QUOTE, "AAA"), (MarketEventKind.BAR, "AAA"),
    ]  # the quote at +2m is available before the bar covering [+2m, +3m)
    assert all(now == e.received_at for now, e in seen)  # clock sits at availability, never ahead
    assert clock.now() == OPEN + 3 * MIN
    assert seen[0][1].event_time == OPEN and seen[0][1].price == D("10")


def test_subscribe_unsubscribe_close() -> None:
    feed = HistoricalReplayFeed([_bar(0), _bar(1, symbol="BBB")], _prov())

    async def run() -> list[MarketEvent]:
        stream = await feed.subscribe(["AAA", "BBB"])
        await feed.unsubscribe(["BBB"])
        return [e async for e in stream]

    assert [e.symbol for e in asyncio.run(run())] == ["AAA"]
    asyncio.run(feed.close())
    with pytest.raises(RuntimeError):
        asyncio.run(feed.subscribe(["AAA"]))


def test_fetch_bars_excludes_unavailable_and_out_of_window_bars() -> None:
    clock = ReplayClock(OPEN + 3 * MIN)
    feed = HistoricalReplayFeed([_bar(i) for i in range(6)], _prov(), clock=clock)
    got = asyncio.run(feed.fetch_bars("AAA", Timeframe.M1, OPEN + MIN, OPEN + 10 * MIN))
    # clock is at +3m so bars 0..2 are complete; window excludes bar 0
    assert [c.start for c in got] == [OPEN + MIN, OPEN + 2 * MIN]
    assert all(isinstance(c.close, Decimal) for c in got)
    no_clock = HistoricalReplayFeed([_bar(i) for i in range(6)], _prov())
    bounded = asyncio.run(no_clock.fetch_bars("AAA", Timeframe.M1, OPEN, OPEN + 2 * MIN))
    assert [c.start for c in bounded] == [OPEN, OPEN + MIN]
    with pytest.raises(ValueError, match="1-minute"):
        asyncio.run(no_clock.fetch_bars("AAA", Timeframe.M5, OPEN, OPEN + 10 * MIN))


# ------------------------------------------------------------------ fixture file


def _write(path: Path, events: list[dict[str, object]]) -> Path:
    path.write_text(json.dumps({"events": events}), encoding="utf-8")
    return path


def test_from_json_file_loads_local_fixture(tmp_path: Path) -> None:
    fixture = _write(tmp_path / "f.json", [
        {"kind": "BAR", "symbol": "AAA", "start": "2026-03-02T14:30:00+00:00", "open": "10",
         "high": "11", "low": "9.5", "close": "10.5", "volume": "1000"},
        {"kind": "QUOTE", "symbol": "AAA", "time": "2026-03-02T14:30:30+00:00",
         "bid": "10.4", "ask": "10.6"},
        {"kind": "SPLIT", "symbol": "AAA", "effective_at": "2026-03-03T14:30:00+00:00",
         "ratio": "2"},
        {"kind": "HALT", "symbol": "AAA", "start": "2026-03-02T15:00:00+00:00"},
    ])
    feed = HistoricalReplayFeed.from_json_file(fixture, _prov())
    assert [e.kind for e in feed.events()] == ["QUOTE", "BAR", "DATA_QUALITY", "SPLIT"]
    bar = next(e for e in feed.events() if e.candle)
    assert bar.candle.close == D("10.5") and isinstance(bar.candle.volume, Decimal)  # type: ignore[union-attr]
    assert feed.quote_fidelity() is QuoteFidelity.SAMPLED
    again = HistoricalReplayFeed.from_json_file(fixture, _prov())
    assert again.provenance.dataset_hash == feed.provenance.dataset_hash


def test_from_json_file_rejects_floats_and_naive_times_and_oversize(tmp_path: Path) -> None:
    bad_float = tmp_path / "float.json"
    bad_float.write_text('{"events":[{"kind":"SPLIT","symbol":"AAA",'
                         '"effective_at":"2026-03-03T14:30:00+00:00","ratio":2.0}]}')
    with pytest.raises(ValueError, match="float"):
        HistoricalReplayFeed.from_json_file(bad_float, _prov())
    naive = _write(tmp_path / "naive.json", [
        {"kind": "SPLIT", "symbol": "AAA", "effective_at": "2026-03-03T14:30:00", "ratio": "2"}])
    with pytest.raises(ValueError, match="timezone-aware"):
        HistoricalReplayFeed.from_json_file(naive, _prov())
    with pytest.raises(ValueError, match="max_bytes"):
        HistoricalReplayFeed.from_json_file(naive, _prov(), max_bytes=5)
    unknown = _write(tmp_path / "unk.json", [{"kind": "NOPE", "symbol": "AAA"}])
    with pytest.raises(ValueError, match="unknown"):
        HistoricalReplayFeed.from_json_file(unknown, _prov())
