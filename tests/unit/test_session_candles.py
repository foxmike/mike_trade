"""D1/W1 session-candle data capability: calendar spans, conversion, aggregation, point-in-time store."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from daily_fixtures import SYMBOL, daily_candle, daily_series, minute_candles, sessions_ending

from mike_trade.market_data.alpaca_history import Adjustment, HistoricalBar
from mike_trade.market_data.candles import (
    INTRADAY_TIMEFRAMES,
    SESSION_TIMEFRAMES,
    Candle,
    CandleBuilder,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    InvalidCandleInputError,
    PriceBasis,
    SeriesMismatchError,
)
from mike_trade.market_data.context import ContextBuilder, InvalidContextInputError
from mike_trade.market_data.session_candles import (
    DEFAULT_SETTLE_DELAY,
    SessionCandleError,
    SessionCandleStore,
    aggregate_weeks,
    daily_candle_from_bar,
    history_issue,
    is_consecutive,
    last_settled_session,
    missing_sessions,
    settled_daily_candles,
    suspect_corporate_action,
)
from mike_trade.scheduling.calendar import session_for

D = Decimal
NY = ZoneInfo("America/New_York")


def et(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), NY).astimezone(UTC)


def alpaca_bar(day: date, close: str = "100", *, adjustment=Adjustment.RAW, hour: int = 0,
               volume: int = 1000, feed: str = "iex") -> HistoricalBar:
    stamp = datetime.combine(day, time(hour), NY).astimezone(UTC)
    c = D(close)
    return HistoricalBar(SYMBOL, stamp, c, c + 1, c - 1, c, volume, None, 10, feed, adjustment)


# ============================================================ timeframe and candle contract


def test_session_timeframes_have_calendar_spans_not_durations() -> None:
    assert SESSION_TIMEFRAMES == (CandleTimeframe.D1, CandleTimeframe.W1)
    assert all(not tf.is_session_based for tf in INTRADAY_TIMEFRAMES)
    with pytest.raises(ValueError):
        _ = CandleTimeframe.D1.duration
    assert CandleTimeframe.M5.duration == timedelta(minutes=5)
    with pytest.raises(InvalidCandleInputError):
        CandleBuilder(timeframes=(CandleTimeframe.M1, CandleTimeframe.D1))
    assert CandleBuilder()._timeframes == INTRADAY_TIMEFRAMES  # default is unchanged: intraday only


def test_d1_candle_spans_exactly_the_regular_session_including_early_closes() -> None:
    normal = daily_candle(date(2026, 10, 8), D(100))
    assert (normal.start, normal.end) == (et(date(2026, 10, 8), 9, 30), et(date(2026, 10, 8), 16))
    black_friday = daily_candle(date(2026, 11, 27), D(100))  # 13:00 ET early close
    assert black_friday.end == et(date(2026, 11, 27), 13)
    s = session_for(date(2026, 10, 8))
    base = dict(symbol=SYMBOL, timeframe=CandleTimeframe.D1, open=D(1), high=D(2), low=D(1),
                close=D(1), volume=D(1), feed_id="iex", consolidated=False,
                origin=CandleOrigin.REPLAY, price_basis=PriceBasis.RAW, status=CandleStatus.CLOSED)
    for start, end in (
        (s.open_utc, s.open_utc + timedelta(hours=24)),                 # naive 24h day
        (s.open_utc - timedelta(hours=9, minutes=30), s.close_utc),    # midnight start
        (s.open_utc, et(date(2026, 10, 8), 13)),                       # early-close span on a full day
        (et(date(2026, 10, 10), 9, 30), et(date(2026, 10, 10), 16)),   # a Saturday
        (et(date(2026, 11, 26), 9, 30), et(date(2026, 11, 26), 16)),   # Thanksgiving
    ):
        with pytest.raises(InvalidCandleInputError):
            Candle(start=start, end=end, **base)


def test_w1_candle_spans_the_weeks_sessions_with_holidays_and_early_closes() -> None:
    def week(start_day: date, last_day: date, end_hour: int = 16) -> Candle:
        return Candle(SYMBOL, CandleTimeframe.W1, et(start_day, 9, 30), et(last_day, end_hour),
                      D(1), D(2), D(1), D(1), D(1), "iex", False, CandleOrigin.REPLAY,
                      PriceBasis.RAW, CandleStatus.CLOSED)

    week(date(2026, 10, 5), date(2026, 10, 9))        # a normal week
    week(date(2026, 3, 30), date(2026, 4, 2))         # Good Friday week: four sessions
    week(date(2026, 11, 23), date(2026, 11, 27), 13)  # Thanksgiving week: Fri 13:00 early close
    for bad in (
        lambda: week(date(2026, 10, 6), date(2026, 10, 9)),            # does not start on Monday's session
        lambda: week(date(2026, 10, 5), date(2026, 10, 8)),            # ends before the last session
        lambda: week(date(2026, 3, 30), date(2026, 4, 3)),             # includes the Good Friday holiday
        lambda: week(date(2026, 11, 23), date(2026, 11, 27), 16),      # ignores the early close
    ):
        with pytest.raises(InvalidCandleInputError):
            bad()


# ================================================================== provider conversion


def test_alpaca_daily_stamps_map_to_the_trading_date_in_both_dst_regimes() -> None:
    summer = daily_candle_from_bar(alpaca_bar(date(2026, 7, 1), "50"))      # stamped 04:00Z
    winter = daily_candle_from_bar(alpaca_bar(date(2026, 1, 5), "60"))      # stamped 05:00Z
    assert alpaca_bar(date(2026, 7, 1)).timestamp.hour == 4
    assert alpaca_bar(date(2026, 1, 5)).timestamp.hour == 5
    assert (summer.start, summer.end) == (et(date(2026, 7, 1), 9, 30), et(date(2026, 7, 1), 16))
    assert (winter.start, winter.end) == (et(date(2026, 1, 5), 9, 30), et(date(2026, 1, 5), 16))
    assert summer.close == D(50) and summer.volume == D(1000) and summer.trade_count == 10
    assert summer.price_basis is PriceBasis.RAW and summer.status is CandleStatus.CLOSED
    assert summer.consolidated is False and daily_candle_from_bar(
        alpaca_bar(date(2026, 7, 1), feed="sip")).consolidated is True


@pytest.mark.parametrize("bar,code", [
    (lambda: alpaca_bar(date(2026, 7, 1), adjustment=Adjustment.SPLIT), "session_candle_price_basis"),
    (lambda: alpaca_bar(date(2026, 7, 1), adjustment=Adjustment.ALL), "session_candle_price_basis"),
    (lambda: alpaca_bar(date(2026, 7, 1), hour=9), "session_candle_stamp"),          # not midnight
    (lambda: HistoricalBar(SYMBOL, datetime(2026, 7, 1, 0, 0, tzinfo=UTC), D(1), D(2), D(1), D(1),
                           1, None, None, "iex", Adjustment.RAW), "session_candle_stamp"),  # UTC midnight
    (lambda: alpaca_bar(date(2026, 7, 4)), "session_candle_not_a_session"),          # Saturday
    (lambda: alpaca_bar(date(2026, 4, 3)), "session_candle_not_a_session"),          # Good Friday
])
def test_provider_bars_that_cannot_be_dated_or_priced_correctly_are_refused(bar, code) -> None:
    with pytest.raises(SessionCandleError) as raised:
        daily_candle_from_bar(bar())
    assert raised.value.code == code


def test_a_developing_current_day_is_never_ingested() -> None:
    days = sessions_ending(4, date(2026, 10, 8))
    bars = [alpaca_bar(d, str(100 + i)) for i, d in enumerate(days)]
    # at 15:00 on the last day its bar is still developing; at 16:59 it is not settled; at 17:00 it is
    assert len(settled_daily_candles(bars, now=et(days[-1], 15))) == 3
    assert len(settled_daily_candles(bars, now=et(days[-1], 16, 59))) == 3
    assert len(settled_daily_candles(bars, now=et(days[-1], 17))) == 4
    assert DEFAULT_SETTLE_DELAY == timedelta(hours=1)


# ================================================================= weekly aggregation


def test_weekly_candle_is_the_independent_aggregate_of_its_sessions() -> None:
    monday = date(2026, 10, 5)
    days = sessions_ending(5, date(2026, 10, 9))
    assert days[0] == monday
    spec = [  # open, high, low, close, volume
        ("10", "12", "9", "11", 100), ("11", "15", "10", "14", 200), ("14", "14.5", "8", "9", 300),
        ("9", "13", "9", "12", 400), ("12", "13", "11", "11.5", 500),
    ]
    daily = [
        daily_candle(d, D(c), open_=D(o), high=D(h), low=D(lo), volume=v)
        for d, (o, h, lo, c, v) in zip(days, spec, strict=True)
    ]
    (week,) = aggregate_weeks(daily)
    assert (week.open, week.high, week.low, week.close, week.volume) == (
        D(10), D(15), D(8), D("11.5"), D(1500))
    assert week.start == et(monday, 9, 30) and week.end == et(date(2026, 10, 9), 16)
    assert week.timeframe is CandleTimeframe.W1 and week.trade_count is None


def test_weekly_aggregation_omits_incomplete_weeks_and_handles_short_weeks() -> None:
    full = daily_series([D(100 + i) for i in range(5)], last=date(2026, 10, 9))
    holey = [c for c in daily_series([D(100)] * 5, last=date(2026, 10, 16)) if
             c.start.astimezone(NY).date() != date(2026, 10, 14)]    # Wednesday missing
    assert len(aggregate_weeks(full + holey)) == 1                        # only the complete week
    assert missing_sessions(full + holey) == (date(2026, 10, 14),)
    # Good Friday week has four sessions and is complete with four
    good_friday = daily_series([D(10)] * 4, last=date(2026, 4, 2))
    assert len(aggregate_weeks(good_friday)) == 1
    # Thanksgiving week closes early on Friday
    thanks = daily_series([D(10)] * 4, last=date(2026, 11, 27))
    (week,) = aggregate_weeks(thanks)
    assert week.end == et(date(2026, 11, 27), 13)
    # consecutive weeks
    two = aggregate_weeks(daily_series([D(1)] * 10, last=date(2026, 10, 9)))
    assert len(two) == 2 and is_consecutive(two[0], two[1])


# ================================================================ point-in-time store


def make_store(count: int = 15, last: date = date(2026, 10, 9)) -> tuple[SessionCandleStore, list[Candle]]:
    store = SessionCandleStore()
    candles = daily_series([D(100 + i) for i in range(count)], last=last)
    store.ingest_daily(SYMBOL, candles)
    return store, candles


def test_no_partial_day_leaks_a_day_is_visible_only_after_close_plus_settle() -> None:
    store, candles = make_store()
    tuesday = date(2026, 10, 6)
    ids = lambda at: [c.start.astimezone(NY).date() for c in  # noqa: E731
                      store.get_completed_candles(SYMBOL, CandleTimeframe.D1, as_of=at)]
    assert tuesday not in ids(et(tuesday, 10))                    # 10:00 Tuesday: only Monday and before
    assert date(2026, 10, 5) in ids(et(tuesday, 10))
    assert tuesday not in ids(et(tuesday, 16, 59))                # closed at 16:00 but not settled
    assert tuesday in ids(et(tuesday, 17))                        # settled one hour later
    assert tuesday not in ids(et(tuesday, 16, 59, ))
    # the early-close day settles at 14:00 ET
    store2 = SessionCandleStore()
    store2.ingest_daily(SYMBOL, daily_series([D(10)] * 12, last=date(2026, 11, 27)))
    black_friday = date(2026, 11, 27)
    seen = lambda at: any(c.start.astimezone(NY).date() == black_friday for c in  # noqa: E731
                          store2.get_completed_candles(SYMBOL, CandleTimeframe.D1, as_of=at))
    assert not seen(et(black_friday, 13, 59)) and seen(et(black_friday, 14))
    assert len(candles) == 15


def test_no_partial_week_leaks_a_week_is_visible_only_after_its_last_session_settles() -> None:
    store, _ = make_store(count=15, last=date(2026, 10, 16))     # weeks of 9/28, 10/5 and 10/12
    weeks = lambda at: store.get_completed_candles(SYMBOL, CandleTimeframe.W1, as_of=at)  # noqa: E731
    ends = lambda at: [w.end for w in weeks(at)]                 # noqa: E731
    previous_two = [et(date(2026, 10, 2), 16), et(date(2026, 10, 9), 16)]
    assert ends(et(date(2026, 10, 14), 11)) == previous_two     # Wednesday: not Friday's, not this week's
    assert ends(et(date(2026, 10, 16), 10)) == previous_two     # Friday intraday: week still developing
    assert ends(et(date(2026, 10, 16), 16, 59)) == previous_two  # closed 16:00 but not settled
    assert ends(et(date(2026, 10, 16), 17)) == [*previous_two, et(date(2026, 10, 16), 16)]
    assert ends(et(date(2026, 10, 19), 10)) == ends(et(date(2026, 10, 16), 17))   # next Monday
    # the weekly close is the Friday D1 close, the open is Monday's open
    last_week = weeks(et(date(2026, 10, 19), 10))[-1]
    friday = [c for c in store.get_completed_candles(SYMBOL, CandleTimeframe.D1,
                                                     as_of=et(date(2026, 10, 19), 10))][-1]
    assert last_week.close == friday.close
    # limit keeps the most recent
    assert [w.end for w in store.get_completed_candles(
        SYMBOL, CandleTimeframe.W1, 1, as_of=et(date(2026, 10, 19), 10))] == [et(date(2026, 10, 16), 16)]


def test_first_candle_wins_duplicates_and_conflicts_are_counted_history_never_mutates() -> None:
    store = SessionCandleStore()
    original = daily_candle(date(2026, 10, 7), D(100))
    assert store.ingest_daily(SYMBOL, [original]).added == 1
    assert store.ingest_daily(SYMBOL, [original]).duplicates == 1
    revised = daily_candle(date(2026, 10, 7), D(101))
    summary = store.ingest_daily(SYMBOL, [revised])
    assert (summary.added, summary.conflicts) == (0, 1) and store.conflicts(SYMBOL) == 1
    (kept,) = store.get_completed_candles(SYMBOL, CandleTimeframe.D1, as_of=et(date(2026, 10, 9), 12))
    assert kept.close == D(100)


def test_series_provenance_is_locked_and_only_raw_completed_daily_candles_are_accepted() -> None:
    store, _ = make_store(10)
    other_feed = daily_candle(date(2026, 10, 12), D(100), feed="sip")
    with pytest.raises(SeriesMismatchError):
        store.ingest_daily(SYMBOL, [other_feed])
    adjusted = Candle(SYMBOL, CandleTimeframe.D1, et(date(2026, 10, 12), 9, 30), et(date(2026, 10, 12), 16),
                      D(1), D(2), D(1), D(1), D(1), "iex", False, CandleOrigin.REPLAY,
                      PriceBasis.SPLIT_ADJUSTED, CandleStatus.CLOSED)
    with pytest.raises(SessionCandleError):
        store.ingest_daily(SYMBOL, [adjusted])
    with pytest.raises(SessionCandleError):
        store.ingest_daily(SYMBOL, [daily_candle(date(2026, 10, 12), D(1), symbol="QQQ")])
    intraday = minute_candles(["100"])
    with pytest.raises(SessionCandleError):
        store.ingest_daily(SYMBOL, intraday)
    assert not store.has("QQQ") and store.symbols() == (SYMBOL,)


def test_symbols_are_isolated_and_retention_is_bounded() -> None:
    store = SessionCandleStore(max_sessions=10)
    store.ingest_daily(SYMBOL, daily_series([D(1)] * 12, last=date(2026, 10, 9)))
    store.ingest_daily("QQQ", daily_series([D(2)] * 3, last=date(2026, 10, 9), symbol="QQQ"))
    at = et(date(2026, 10, 12), 10)
    spy = store.get_completed_candles(SYMBOL, CandleTimeframe.D1, as_of=at)
    assert len(spy) == 10 and spy[-1].close == D(1)                       # oldest two sessions evicted
    assert len(store.get_completed_candles("QQQ", CandleTimeframe.D1, as_of=at)) == 3
    assert store.get_completed_candles("IWM", CandleTimeframe.D1, as_of=at) == ()
    with pytest.raises(SessionCandleError):
        store.get_completed_candles(SYMBOL, CandleTimeframe.M1, as_of=at)
    with pytest.raises(SessionCandleError):
        SessionCandleStore(max_sessions=5)


def test_needs_refresh_tracks_the_last_settled_session_and_fingerprint_is_content_based() -> None:
    store, _ = make_store(10, last=date(2026, 10, 8))
    assert not store.needs_refresh(SYMBOL, et(date(2026, 10, 9), 10))        # Oct 8 is the last settled
    assert store.needs_refresh(SYMBOL, et(date(2026, 10, 9), 17))            # Oct 9 settled, not held
    assert last_settled_session(et(date(2026, 10, 10), 12)).session_date == date(2026, 10, 9)  # weekend
    assert last_settled_session(et(date(2026, 10, 12), 9, 31)).session_date == date(2026, 10, 9)
    twin, _ = make_store(10, last=date(2026, 10, 8))
    changed = SessionCandleStore()
    changed.ingest_daily(SYMBOL, daily_series([D(100 + i) for i in range(10)][:-1] + [D(500)],
                                              last=date(2026, 10, 8)))
    assert store.fingerprint() == twin.fingerprint() != changed.fingerprint()


def test_history_issue_names_each_reason_to_distrust_a_daily_history() -> None:
    now = et(date(2026, 10, 9), 10)
    good = daily_series([D(100)] * 30, last=date(2026, 10, 8))
    assert history_issue(good, as_of=now) is None
    assert history_issue([], as_of=now) == "no daily history"
    assert "missing session" in history_issue(good[:10] + good[11:], as_of=now)
    assert history_issue(good, as_of=et(date(2026, 10, 13), 10)) == "daily history is not up to date"
    split = good[:15] + [daily_candle(c.start.astimezone(NY).date(), c.close / 2, open_=c.close / 2)
                         for c in good[15:]]
    assert suspect_corporate_action(split, D("0.25")) and not suspect_corporate_action(good, D("0.25"))
    assert "discontinuity" in history_issue(split, as_of=now, max_overnight_move=D("0.25"))
    assert history_issue(split, as_of=now) is None                      # check is opt-in
    assert history_issue(aggregate_weeks(good), as_of=now) == "not a daily series"


# ===================================================================== context builder


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


class _Cache:
    def latest_quote(self, symbol): return None  # noqa: ANN001, E704
    def latest_trade(self, symbol): return None  # noqa: ANN001, E704


def test_context_builder_serves_d1_w1_as_known_at_the_clock_and_m1_only_contexts_are_unchanged() -> None:
    store, _ = make_store(15, last=date(2026, 10, 9))
    clock = _Clock(et(date(2026, 10, 14), 11))
    candles = CandleBuilder(timeframes=(CandleTimeframe.M1,))
    for bar in minute_candles(["100", "101"], day=date(2026, 10, 14)):
        from mike_trade.market_data.candles import MinuteBar
        candles.on_minute_bar(MinuteBar(SYMBOL, bar.start, bar.open, bar.high, bar.low, bar.close,
                                        bar.volume, "iex", False))
    candles.close_due()
    builder = ContextBuilder(candle_builder=candles, quote_trade_cache=_Cache(), clock=clock,
                             session_candles=store, max_age=timedelta(hours=1))
    clock.value = et(date(2026, 10, 14), 9, 33)
    ctx = builder.build(SYMBOL, timeframes=(CandleTimeframe.M1, CandleTimeframe.D1, CandleTimeframe.W1))
    assert set(ctx.candles) == {CandleTimeframe.M1, CandleTimeframe.D1, CandleTimeframe.W1}
    assert max(c.end for c in ctx.candles[CandleTimeframe.D1]) <= ctx.as_of
    assert ctx.candles[CandleTimeframe.D1][-1].start.astimezone(NY).date() == date(2026, 10, 9)
    assert ctx.candles[CandleTimeframe.W1][-1].end == et(date(2026, 10, 9), 16)
    plain = builder.build(SYMBOL, timeframes=(CandleTimeframe.M1,))
    assert set(plain.candles) == {CandleTimeframe.M1}
    no_source = ContextBuilder(candle_builder=candles, quote_trade_cache=_Cache(), clock=clock,
                               max_age=timedelta(hours=1))
    with pytest.raises(InvalidContextInputError):
        no_source.build(SYMBOL, timeframes=(CandleTimeframe.D1,))
