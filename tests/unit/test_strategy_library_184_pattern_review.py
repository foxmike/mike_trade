"""Independent review tests for the 184-strategy library (gap_exhaustion, bull_pennant_breakout).

Expected values come from hard-coded exchange dates and small oracles written here, never from the
tracker / strategies under test.
"""

from __future__ import annotations

import inspect
import random
from collections import namedtuple
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

from mike_trade import app
from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.backtest.runner import BacktestRequest, BacktestRunner
from mike_trade.domain.market import Candle as ReplayCandle
from mike_trade.domain.market import FeedProvenance, Timeframe
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    MinuteBar,
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.market_data.session_reference import (
    RegularSessionOHLC,
    SessionReference,
    SessionReferenceTracker,
)
from mike_trade.research.commands import _research_registry
from mike_trade.runtime import ExecutionMode
from mike_trade.scheduling.calendar import SessionStatus, session_for
from mike_trade.strategies.base import ReasonCode
from mike_trade.strategies.registry import StrategyRegistry

D = Decimal
NY = ZoneInfo("America/New_York")
REGISTRY = _research_registry()
WATCHING = {"eligibility": "WATCHING_BUY"}
SIX_CD = ("anchored_vwap_breakout", "vwap_rejection", "first_pullback_buy",
          "relative_volume_momentum", "moving_average_compression", "high_volume_reversal")
NEW = ("gap_exhaustion", "bull_pennant_breakout")
Hist = namedtuple("Hist", "timestamp open high low close volume feed_id")


# ------------------------------------------------------------------ registration / preservation


def production_classes(tmp_path: Path) -> list[type]:
    captured: list[type] = []
    original = StrategyRegistry.register

    def spy(self, cls):  # noqa: ANN001
        captured.append(cls)
        return original(self, cls)

    settings = app.AppSettings(mode=ExecutionMode.SHADOW, db_path=tmp_path / "p.sqlite3",
                               symbols=("SPY",), shadow_scenarios=(app.ShadowScenarioSettings(),))
    with mock.patch.object(StrategyRegistry, "register", spy):
        app.build_runtime(settings)
    return captured


def test_184_counts_production_research_parity_and_preserved_additions(tmp_path: Path) -> None:
    counts = {r: len(REGISTRY.list_strategies(r)) for r in StrategyRole}
    assert counts == {StrategyRole.BUY: 87, StrategyRole.SELL: 41,
                      StrategyRole.SHORT: 28, StrategyRole.COVER: 28}
    research = [c.spec.strategy_id for c in REGISTRY.list_strategies()]
    assert len(research) == len(set(research)) == 184
    production = production_classes(tmp_path)
    assert len(production) == len(set(production)) == 184
    assert set(production) == {REGISTRY.get(sid) for sid in research}
    ids = set(research)
    assert set(SIX_CD) <= ids and set(NEW) <= ids
    assert sum(i.startswith("short_") for i in ids) == 28 and sum(i.startswith("cover_") for i in ids) == 28
    # the reviewed batch C/D corrections are still in the source
    for sid, needle in (("anchored_vwap_breakout", "session_open <= item.start"),
                        ("first_pullback_buy", "item.start >= session.open_utc"),
                        ("relative_volume_momentum", "latest_session_bars"),
                        ("moving_average_compression", "latest_session_bars"),
                        ("high_volume_reversal", "latest_session_bars")):
        assert needle in inspect.getsource(inspect.getmodule(REGISTRY.get(sid)))  # type: ignore[arg-type]


# -------------------------------------------------------------------------- session tracker


def bar_at(day: date, i: int, o: object, h: object, low: object, c: object, volume: object = 100,
           feed: str = "iex") -> Candle:
    start = datetime.combine(day, time(9, 30), NY).astimezone(UTC) + timedelta(minutes=i)
    return Candle("SPY", CandleTimeframe.M1, start, start + timedelta(minutes=1), D(str(o)),
                  D(str(h)), D(str(low)), D(str(c)), D(str(volume)), feed, feed == "sip",
                  CandleOrigin.REPLAY, PriceBasis.RAW, CandleStatus.CLOSED)


def feed_day(tracker: SessionReferenceTracker, day: date, minutes: int, *, skip: tuple[int, ...] = (),
             base: int = 100, volume: object = 100) -> None:
    for i in range(minutes):
        if i in skip:
            continue
        tracker.add_bar(bar_at(day, i, base, base + 2 + (i == 7) * 3, base - 1 - (i == 11) * 2,
                               base + (i == minutes - 1), volume))


FULL, HALF = 390, 210  # independent session lengths: 09:30-16:00 and the 13:00 early close


def first_bar(tracker: SessionReferenceTracker, day: date) -> datetime:
    bar = bar_at(day, 0, 100, 101, 99, 100)
    tracker.add_bar(bar)
    return bar.end


def ids(ref: SessionReference) -> list[str]:
    return [h.session_id.removeprefix("XNYS-") for h in ref.history]


def test_history_spans_weekend_thanksgiving_and_the_half_day() -> None:
    t = SessionReferenceTracker()
    for day, n in ((date(2026, 11, 20), FULL), (date(2026, 11, 23), FULL), (date(2026, 11, 24), FULL),
                   (date(2026, 11, 25), FULL), (date(2026, 11, 27), HALF)):
        feed_day(t, day, n)
    ref = t.reference("SPY", first_bar(t, date(2026, 11, 30)))
    assert ref is not None
    assert ids(ref) == ["2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-27"]
    prev = ref.previous
    assert prev is not None and prev.session_id == "XNYS-2026-11-27"
    # previous-session OHLC used by existing strategies, computed independently from the bars
    assert (prev.open, prev.high, prev.low, prev.close) == (D(100), D(105), D(97), D(101))
    assert prev.volume == D(100) * HALF and (prev.close_time - prev.open_time) == timedelta(minutes=HALF)


def test_history_spans_christmas_and_new_year_holidays() -> None:
    t = SessionReferenceTracker()
    for day, n in ((date(2026, 12, 22), FULL), (date(2026, 12, 23), FULL), (date(2026, 12, 24), HALF),
                   (date(2026, 12, 28), FULL), (date(2026, 12, 29), FULL), (date(2026, 12, 30), FULL),
                   (date(2026, 12, 31), FULL)):
        feed_day(t, day, n)
    ref = t.reference("SPY", first_bar(t, date(2027, 1, 4)))  # Dec 25 and Jan 1 are holidays
    assert ref is not None and len(ref.history) == 7
    assert ids(ref)[0] == "2026-12-22" and ids(ref)[-1] == "2026-12-31"


def test_a_really_missing_session_a_partial_session_and_a_late_start_break_the_chain() -> None:
    def history_after(skip_day: date | None = None, drop: tuple[int, ...] = (), on: date | None = None,
                      minutes: int = FULL) -> list[str]:
        t = SessionReferenceTracker()
        for day in (date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25)):
            if day == skip_day:
                continue
            feed_day(t, day, FULL, skip=drop if day == on else ())
        feed_day(t, date(2026, 11, 27), HALF, skip=drop if on == date(2026, 11, 27) else ())
        ref = t.reference("SPY", first_bar(t, date(2026, 11, 30)))
        assert ref is not None
        return ids(ref)

    assert history_after() == ["2026-11-23", "2026-11-24", "2026-11-25", "2026-11-27"]
    assert history_after(skip_day=date(2026, 11, 24)) == ["2026-11-25", "2026-11-27"]  # real hole
    assert history_after(drop=(100,), on=date(2026, 11, 25)) == ["2026-11-27"]  # missing minute
    assert history_after(drop=(0,), on=date(2026, 11, 24)) == ["2026-11-25", "2026-11-27"]  # late start
    assert history_after(drop=(HALF - 1,), on=date(2026, 11, 27)) == []  # last minute missing
    t = SessionReferenceTracker()
    feed_day(t, date(2026, 11, 25), FULL, skip=(5,))
    ref = t.reference("SPY", first_bar(t, date(2026, 11, 27)))
    assert ref is not None and ref.previous is None and ref.history == ()


def test_mixed_feed_identity_and_zero_volume_sessions_are_handled_conservatively() -> None:
    t = SessionReferenceTracker()
    feed_day(t, date(2026, 11, 23), FULL)
    for i in range(FULL):  # same session observed through two different feeds
        t.add_bar(bar_at(date(2026, 11, 24), i, 100, 102, 99, 100, feed="iex" if i < 200 else "sip"))
    zero = date(2026, 11, 25)
    feed_day(t, zero, FULL, volume=0)
    ref = t.reference("SPY", first_bar(t, date(2026, 11, 27)))
    assert ref is not None
    assert ids(ref) == ["2026-11-25"]  # the mixed-feed session cut the chain
    assert ref.history[0].volume == D(0)  # a zero-volume session is recorded, but with no volume


def test_reference_is_point_in_time_and_hides_incomplete_future() -> None:
    t = SessionReferenceTracker()
    for day, n in ((date(2026, 11, 24), FULL), (date(2026, 11, 25), FULL), (date(2026, 11, 27), HALF)):
        feed_day(t, day, n)
    end = first_bar(t, date(2026, 11, 30))
    assert t.reference("SPY", end) is not None and len(t.reference("SPY", end).history) == 3
    # asking about the morning of Nov 27 once Nov 30 data is already inside the tracker: nothing
    # derived from later bars is exposed
    early = t.reference("SPY", datetime.combine(date(2026, 11, 27), time(9, 31), NY))
    assert early is not None and early.previous is None and early.history == () and early.current_open is None
    # while Nov 30 is still being observed, Nov 30 itself is never in its own history
    assert "2026-11-30" not in ids(t.reference("SPY", end))
    # a saturday/holiday has no references
    assert t.reference("SPY", datetime(2026, 11, 26, 15, tzinfo=UTC)) is None


def test_tracker_matches_an_independent_oracle_on_random_holes() -> None:
    days = [d for d in (date(2026, 11, 16) + timedelta(days=k) for k in range(60))
            if d.weekday() < 5 and d not in {date(2026, 11, 26), date(2026, 12, 25), date(2027, 1, 1)}]
    early = {date(2026, 11, 27), date(2026, 12, 24)}
    for seed in range(4):
        r = random.Random(seed)
        t = SessionReferenceTracker()
        complete: set[date] = set()
        skipped = set(r.sample(days[3:], 3))
        for idx, day in enumerate(days):
            if day in skipped:
                continue
            n = HALF if day in early else FULL
            drop = {r.randrange(n)} if r.random() < 0.25 else set()
            if drop == set():
                complete.add(day)
            expected: list[str] = []
            p = idx - 1
            while p >= 0 and days[p] in complete:
                expected.append(days[p].isoformat())
                p -= 1
            expected = expected[::-1][-10:]
            first = [i for i in range(n) if i not in drop][0]
            feed_day(t, day, first + 1, skip=tuple(drop))
            if first == 0:
                ref = t.reference("SPY", bar_at(day, 0, 1, 1, 1, 1).end)
                assert ref is not None and ids(ref) == expected, (seed, day)
            feed_day_rest = [i for i in range(first + 1, n) if i not in drop]
            for i in feed_day_rest:
                t.add_bar(bar_at(day, i, 100, 100, 100, 100))


def test_reference_without_history_is_backward_compatible() -> None:
    ref = SessionReference("XNYS-2026-10-09", "XNYS-2026-10-08", None, None)
    assert ref.history == ()


# ------------------------------------------------------------------------------ gap exhaustion


def hist_session(day: date, close: str, minutes: int, volume: str, low: str = "99") -> RegularSessionOHLC:
    s = session_for(day)
    c = D(close)
    return RegularSessionOHLC(s.session_id, c + 1, c + 4, D(low), c, s.open_utc, s.close_utc, D(volume))


def ge_context(bars: list[Candle], hist: tuple[RegularSessionOHLC, ...], day: date, open_: str) -> MarketContext:
    as_of = bars[-1].end
    ref = SessionReference(session_for(day).session_id, hist[-1].session_id, D(open_), hist[-1], hist)
    return MarketContext(
        symbol="SPY", as_of=as_of, version=len(bars), feed_id="sip", consolidated=True,
        price_basis=PriceBasis.RAW, session_status=SessionStatus.REGULAR,
        session_id=session_for(day).session_id,
        freshness=ContextFreshness(as_of, timedelta(0), timedelta(minutes=5), False, None),
        candles={CandleTimeframe.M1: tuple(bars)}, indicators={}, session_reference=ref)


THANKS = [(date(2026, 11, 20), "110", FULL), (date(2026, 11, 23), "108", FULL),
          (date(2026, 11, 24), "106", FULL), (date(2026, 11, 25), "104", FULL),
          (date(2026, 11, 27), "102", HALF)]
CURRENT = date(2026, 11, 30)


def gap_day_bars(closes: list[str], open_: str, volume: str) -> list[Candle]:
    out, prev = [], D(open_)
    for i, c in enumerate(closes):
        close = D(c)
        out.append(bar_at(CURRENT, i, prev, max(prev, close) + D("0.1"), min(prev, close) - D("0.1"),
                          close, volume, feed="sip"))
        prev = close
    return out


LOW99_CLOSES = ["97.5", "97.8", "98.2", "98.6", "98.9", "99.3"]


def evaluate_gap(bars, hist, open_, params=None):
    return REGISTRY.get("gap_exhaustion")().evaluate(ge_context(bars, hist, CURRENT, open_),
                                                      params or {}, dict(WATCHING))


def test_gap_volume_uses_the_early_close_session_length_as_denominator() -> None:
    # 4 full sessions of 390 000 and the half day of 210 000: exactly 1000 shares/minute overall
    hist = tuple(hist_session(d, c, n, str(1000 * n), low="99") for d, c, n in THANKS)
    closes = LOW99_CLOSES  # previous low 99: only the last close is above it
    # gap vs previous LOW (99): open 97.9 is 1.11% below
    passes = evaluate_gap(gap_day_bars(closes, "97.9", "1500"), hist, "97.9")
    assert passes.is_signal  # 1500 / 1000 = 1.5 exactly
    fails = evaluate_gap(gap_day_bars(closes, "97.9", "1499"), hist, "97.9")
    assert fails.reason_codes == (ReasonCode.NO_SIGNAL,)
    # a wrong denominator (five 390-minute sessions = 1950 minutes) would give 800/min and pass 1499
    total = D(sum(1000 * n for *_, n in THANKS))
    assert total / D(sum(n for *_, n in THANKS)) == D(1000)  # 1770 real minutes
    assert D(1499) / (total / D(5 * FULL)) >= D("1.5")  # five 390-minute sessions would pass 1499


def test_gap_is_measured_from_the_previous_low_and_can_confirm_on_the_first_minute() -> None:
    hist = tuple(hist_session(d, c, n, str(1000 * n), low="100") for d, c, n in THANKS)
    # opening gap 1% of the low exactly (open 99.00 vs low 100.00); the very first minute already
    # closes back above the low, which is the first qualifying completed candle
    first_minute = [bar_at(CURRENT, 0, "99", "100.6", "98.9", "100.4", "2000", feed="sip")]
    assert evaluate_gap(first_minute, hist, "99").is_signal
    # 0.99% is not enough, whichever way the percentage is measured
    short = [bar_at(CURRENT, 0, "99.01", "100.6", "98.9", "100.4", "2000", feed="sip")]
    assert evaluate_gap(short, hist, "99.01").reason_codes == (ReasonCode.NO_SIGNAL,)
    # once the first minute has already closed above the edge, later minutes are not "first"
    two = first_minute + [bar_at(CURRENT, 1, "100.4", "101", "100.2", "100.8", "2000", feed="sip")]
    assert evaluate_gap(two, hist, "99").reason_codes == (ReasonCode.NO_SIGNAL,)


def test_gap_fails_closed_on_zero_volume_unknown_volume_and_open_disagreement() -> None:
    closes = LOW99_CLOSES
    zero = tuple(hist_session(d, c, n, "0", low="99") for d, c, n in THANKS)
    r = evaluate_gap(gap_day_bars(closes, "97.9", "2000"), zero, "97.9")
    assert r.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    assert evaluate_gap(gap_day_bars(closes, "97.9", "2000"), zero, "97.9", {"min_relative_volume": D(0)}).is_signal
    ok = tuple(hist_session(d, c, n, str(1000 * n), low="99") for d, c, n in THANKS)
    # the tracker's opening price must be the opening price of the bars the strategy sees
    mismatch = evaluate_gap(gap_day_bars(closes, "97.9", "2000"), ok, "97.8")
    assert mismatch.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    # a missing minute inside today's prefix is never repaired
    bars = gap_day_bars(closes, "97.9", "2000")
    holey = bars[:2] + bars[3:]
    assert evaluate_gap(holey, ok, "97.9").reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


# ---------------------------------------------------- SHADOW composition and warmup availability


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


def history_bars(day: date, minutes: int, close: str, *, volume: str = "1000") -> list[Hist]:
    s = session_for(day)
    c = D(close)
    out = []
    for i in range(minutes):
        price = c if i == minutes - 1 else c + 1
        out.append(Hist(s.open_utc + timedelta(minutes=i), min(price, c + 1), c + 4 if i == 10 else c + 1,
                        c - 2 if i == 20 else c, price, D(volume), "iex"))
    return out


def gap_morning(closes: list[str], open_: str) -> list[Hist]:
    s, prev, out = session_for(CURRENT), D(open_), []
    for i, c in enumerate(closes):
        close = D(c)
        out.append(Hist(s.open_utc + timedelta(minutes=i), prev, max(prev, close) + D("0.1"),
                        min(prev, close) - D("0.1"), close, D(2000), "iex"))
        prev = close
    return out


def minute_bar_from(h: Hist) -> MinuteBar:
    return MinuteBar("SPY", h.timestamp, h.open, h.high, h.low, h.close, h.volume, h.feed_id, False,
                     origin=CandleOrigin.HISTORICAL_REST)


def warm_like_production(raw: list[Hist], now: datetime, warmup_bars: int, helper: bool):
    """Mirror ``AlpacaProductionMarketData.warm``: tracker gets every bar, candles a warmup slice."""
    md = app.ProductionMarketData(FixedClock(now), timedelta(minutes=5))
    completed = [b for b in raw if b.timestamp + timedelta(minutes=1) <= now]
    for b in completed:
        md.session_references.add_bar(Candle(
            "SPY", CandleTimeframe.M1, b.timestamp, b.timestamp + timedelta(minutes=1), b.open, b.high,
            b.low, b.close, b.volume, b.feed_id, False, CandleOrigin.HISTORICAL_REST, PriceBasis.RAW,
            CandleStatus.CLOSED))
    chosen = (app._warmup_candle_bars(completed, warmup_bars, now) if helper
              else completed[-(warmup_bars + 1):])
    for b in chosen:
        md.candles.on_minute_bar(minute_bar_from(b))
    # The builder closes a minute bar only when the next minute's bar arrives (live stream); the
    # tracker is fed from the closed bars exactly as ``_on_bar`` does.
    follow = Hist(now, D("100.2"), D("100.4"), D("100.1"), D("100.3"), D(100), "iex")
    for closed in md.candles.on_minute_bar(minute_bar_from(follow)).closed:
        md.session_references.add_bar(closed)
    md.clock.current = now + timedelta(minutes=1, seconds=1)
    return md


def shadow_history() -> list[Hist]:
    raw: list[Hist] = []
    for day, close in zip((date(2026, 11, 20), date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25),
                           date(2026, 11, 27)), ("110", "108", "106", "104", "102"), strict=True):
        raw += history_bars(day, HALF if day == date(2026, 11, 27) else FULL, close)
    return raw


CLOSES = ["98.6", "98.4", "98.8", "99.2", "99.6", "100.2"]


def test_shadow_context_carries_the_bounded_history_and_strategy_signals() -> None:
    raw = shadow_history() + gap_morning(CLOSES, "98.5")
    now = session_for(CURRENT).open_utc + timedelta(minutes=len(CLOSES))
    md = warm_like_production(raw, now, warmup_bars=60, helper=True)
    ctx = md.snapshot(["SPY"])["SPY"]
    ref = ctx.session_reference
    assert ref is not None and len(ref.history) == 5 and ref.previous is not None
    assert [h.close for h in ref.history] == [D(x) for x in ("110", "108", "106", "104", "102")]
    result = REGISTRY.get("gap_exhaustion")().evaluate(ctx, {}, dict(WATCHING))
    assert result.is_signal and result.bar_time == ctx.candles[CandleTimeframe.M1][-1].start


def test_mid_session_restart_needs_todays_prefix_in_the_candle_context() -> None:
    raw = shadow_history() + gap_morning(CLOSES, "98.5")
    now = session_for(CURRENT).open_utc + timedelta(minutes=len(CLOSES))
    # the old slice (last warmup_bars + 1 = 3 bars) has no complete-from-open session prefix
    naive = warm_like_production(raw, now, warmup_bars=2, helper=False).snapshot(["SPY"])["SPY"]
    assert REGISTRY.get("gap_exhaustion")().evaluate(naive, {}, dict(WATCHING)).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,)
    fixed = warm_like_production(raw, now, warmup_bars=2, helper=True).snapshot(["SPY"])["SPY"]
    assert REGISTRY.get("gap_exhaustion")().evaluate(fixed, {}, dict(WATCHING)).is_signal


def test_warmup_slice_helper_never_invents_bars() -> None:
    day = date(2026, 11, 30)
    open_utc = session_for(day).open_utc
    raw = [Hist(open_utc - timedelta(minutes=90) + timedelta(minutes=i), D(1), D(1), D(1), D(1), D(1), "iex")
           for i in range(90 + 40)]  # pre-market then 40 session minutes, with one hole removed
    raw = raw[:100] + raw[101:]
    at = open_utc + timedelta(minutes=40)
    picked = app._warmup_candle_bars(raw, 10, at)
    assert picked[0].timestamp == open_utc and picked[-1] == raw[-1]
    assert set(picked) <= set(raw) and len(picked) == len([b for b in raw if b.timestamp >= open_utc])
    # before the open (pre-market restart) only the usual tail is used
    pre = app._warmup_candle_bars(raw[:50], 10, open_utc - timedelta(minutes=40))
    assert len(pre) == 11 and pre[-1] == raw[49]
    # a holiday or weekend keeps the usual tail
    sat = datetime(2026, 11, 28, 15, tzinfo=UTC)
    assert len(app._warmup_candle_bars(raw, 10, sat)) == 11
    assert app._warmup_candle_bars(raw[:3], 10, at) == tuple(raw[:3])


# -------------------------------------------------------------------------------- BACKTEST


def replay_events(bars_by_day: list[tuple[date, list[Hist]]]) -> list[ReplayEvent]:
    prov = FeedProvenance("replay", "IEX", "synthetic", False)
    return [ReplayEvent.bar(ReplayCandle("SPY", Timeframe.M1, h.timestamp, h.timestamp + timedelta(minutes=1),
                                         h.open, h.high, h.low, h.close, h.volume, prov))
            for _, bars in bars_by_day for h in bars]


async def run_gap_backtest(window_start: datetime, tmp_path: Path):
    days = [(d, history_bars(d, HALF if d == date(2026, 11, 27) else FULL, c))
            for d, c in zip((date(2026, 11, 20), date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25),
                             date(2026, 11, 27)), ("110", "108", "106", "104", "102"), strict=True)]
    days.append((CURRENT, gap_morning(CLOSES, "98.5")))
    clock = ReplayClock(window_start)
    feed = HistoricalReplayFeed(replay_events(days), DataProvenance("synthetic", "IEX", False, "g"),
                                clock=clock, seed=5)
    registry = StrategyRegistry()
    registry.register(REGISTRY.get("gap_exhaustion"))
    end = session_for(CURRENT).open_utc + timedelta(minutes=len(CLOSES) + 1)
    request = BacktestRequest(symbols=("SPY",), window_start=window_start, window_end=end,
                              period_role=PeriodRole.RESEARCH, config={}, seed=5,
                              starting_capital=D("10000"), strategy_ids=("gap_exhaustion",),
                              context_history_bars=500, result_directory=tmp_path)
    return await BacktestRunner(registry, feed, clock, HistoricalExecutionSimulator(feed, clock)).run(request)


async def test_backtest_replay_across_thanksgiving_signals_with_full_history(tmp_path: Path) -> None:
    result = await run_gap_backtest(session_for(date(2026, 11, 20)).open_utc, tmp_path)
    assert [r["strategy"] for r in result.signals] == ["gap_exhaustion"]
    assert result.signals[0]["bar_time"] == session_for(CURRENT).open_utc + timedelta(minutes=5)
    assert all(r["trigger_bar_end"] <= r["time"] for r in result.signals)


async def test_backtest_window_without_enough_prior_sessions_never_invents_them(tmp_path: Path) -> None:
    # the window starts after the first two decline sessions: only three sessions are observed
    result = await run_gap_backtest(session_for(date(2026, 11, 24)).open_utc, tmp_path)
    assert not result.signals


# ----------------------------------------------------------------------------- bull pennant


PEN_OPEN = session_for(date(2026, 10, 8)).open_utc


def pbar(i: int, high: object, low: object, close: object, *, base: datetime = PEN_OPEN) -> Candle:
    start = base + timedelta(minutes=i)
    h, lo, c = D(str(high)), D(str(low)), D(str(close))
    return Candle("SPY", CandleTimeframe.M1, start, start + timedelta(minutes=1), min(max(c, lo), h), h, lo, c,
                  D(100), "sip", True, CandleOrigin.REPLAY, PriceBasis.RAW, CandleStatus.CLOSED)


def pennant_series(seed: int) -> list[Candle]:
    """Flat base, a pole, a (noisy) converging consolidation and a breakout bar: 61 bars."""
    r = random.Random(seed)
    pole = [D(100) + D("0.4") * k for k in range(9)]  # 100 -> 103.2
    bars = [pbar(i, 100.2, 99.8, 100) for i in range(61 - 1 - 9 - 14)]
    for k, price in enumerate(pole):
        bars.append(pbar(len(bars), price + D("0.15"), price - D("0.15"), price))
    upper, lower = D("103.3"), D("102.0")
    for j in range(14):
        up = upper - D("0.05") * j + D(r.randint(-4, 4)) / 100
        lo = min(lower + D("0.04") * j + D(r.randint(-4, 4)) / 100, up - D("0.03"))
        close = lo + (up - lo) * D(r.randint(20, 80)) / 100
        bars.append(pbar(len(bars), up, lo, close))
    close = D(r.randint(1019, 1040)) / 10
    bars.append(pbar(len(bars), close + D("0.1"), min(close, D("102.0")), close))
    assert len(bars) == 61
    return bars


def pennant_oracle(w: list[Candle], strength: int = 1) -> bool:
    """Independent re-derivation (exact fractions) of the documented default-parameter rule."""
    n, last = len(w), len(w) - 1
    H = [Fraction(b.high) for b in w]
    L = [Fraction(b.low) for b in w]
    C = [Fraction(b.close) for b in w]
    base = range(last)
    top = max(base, key=lambda i: (H[i], i))
    pennant = range(top + 1, last)
    if not 6 <= len(pennant) <= 20:
        return False
    start = max(0, top - 10)
    floor = min(L[i] for i in range(start, top + 1))
    height = H[top] - floor
    if height < Fraction(3, 200) * floor or H[top] - min(L[i] for i in pennant) > Fraction(1, 2) * height:
        return False
    hp = [i for i in range(1, last - 1) if H[i] > H[i - 1] and H[i] > H[i + 1] and i >= top]
    lp = [i for i in range(1, last - 1) if L[i] < L[i - 1] and L[i] < L[i + 1] and i > top]
    if len(hp) < 2 or len(lp) < 2:
        return False
    h1, h2, l1, l2 = hp[-2], hp[-1], lp[-2], lp[-1]
    if not (H[h2] < H[h1] and L[l2] > L[l1]):
        return False

    def up(x: int) -> Fraction:
        return H[h1] + (H[h2] - H[h1]) * (x - h1) / (h2 - h1)

    def down(x: int) -> Fraction:
        return L[l1] + (L[l2] - L[l1]) * (x - l1) / (l2 - l1)

    return (up(last) > down(last) and all(C[x] >= down(x) for x in range(l1, last))
            and C[last] > up(last) and all(C[x] <= up(x) for x in range(h1, last)))


def pennant_ctx(bars: list[Candle]) -> MarketContext:
    as_of = bars[-1].end
    return MarketContext(
        symbol="SPY", as_of=as_of, version=len(bars), feed_id="sip", consolidated=True,
        price_basis=PriceBasis.RAW, session_status=SessionStatus.REGULAR,
        session_id=bars[-1].start.astimezone(NY).date().isoformat(),
        freshness=ContextFreshness(as_of, timedelta(0), timedelta(minutes=5), False, None),
        candles={CandleTimeframe.M1: tuple(bars)}, indicators={})


def pennant_signal(bars: list[Candle]):
    return REGISTRY.get("bull_pennant_breakout")().evaluate(pennant_ctx(bars), {}, dict(WATCHING))


def test_pennant_matches_an_independent_oracle_on_noisy_pennants() -> None:
    fired = quiet = 0
    for seed in range(300):
        bars = pennant_series(seed)
        result = pennant_signal(bars)
        assert result.is_signal == pennant_oracle(bars), seed
        fired += result.is_signal
        quiet += not result.is_signal
    assert fired >= 10 and quiet >= 10  # both outcomes are exercised


def test_pennant_pivots_are_confirmed_before_use_and_never_signal_retrospectively() -> None:
    bars = next(b for b in (pennant_series(s) for s in range(300)) if pennant_signal(b).is_signal)
    # the decision at each bar uses only bars up to it: appending later bars never changes it
    extended = bars + [pbar(61, 110, 100, 108), pbar(62, 111, 101, 109)]
    for cut in (61, 62, 63):
        assert pennant_signal(extended[:cut][-61:]).is_signal == pennant_oracle(extended[:cut][-61:])
    assert pennant_signal(extended[:61]).is_signal
    # one bar before the breakout bar the pennant is simply not broken out yet
    assert not pennant_signal(bars[:-1]).is_signal
    # replaying the signal bar suppresses a duplicate
    first = pennant_signal(bars)
    again = REGISTRY.get("bull_pennant_breakout")().evaluate(
        pennant_ctx(bars), {}, {**WATCHING, **first.state_updates})
    assert not again.is_signal


def zigzag(prefix: list[Candle], up0: str, lo0: str, up_step: str, lo_step: str, final_close: str) -> list[Candle]:
    """Alternating touches of an upper and a lower boundary (real swing highs and swing lows)."""
    out = list(prefix)
    for j in range(14):
        up, lo = D(up0) + D(up_step) * j, D(lo0) + D(lo_step) * j
        mid = (up + lo) / 2
        if j % 2 == 0:
            out.append(pbar(len(out), up, mid, mid))  # touches the upper boundary
        else:
            out.append(pbar(len(out), mid + D("0.02"), lo, mid))  # touches the lower boundary
    close = D(final_close)
    out.append(pbar(len(out), close + D("0.1"), min(close, D(lo0)), close))
    assert len(out) == 61
    return out


def test_pennant_lookalikes_do_not_fire() -> None:
    pole = [pbar(i, 100.2, 99.8, 100) for i in range(37)]
    for k in range(9):  # 100 -> 103.2 over nine bars
        price = D(100) + D("0.4") * k
        pole.append(pbar(len(pole), price + D("0.15"), price - D("0.15"), price))
    assert len(pole) == 46
    # control: converging boundaries, closing above the extended upper line at the end
    assert pennant_signal(zigzag(pole, "103.3", "102.0", "-0.05", "0.04", "104")).is_signal
    # the same bars but a close inside the pennant is no breakout
    assert not pennant_signal(zigzag(pole, "103.3", "102.0", "-0.05", "0.04", "102.58")).is_signal
    assert not pennant_signal(zigzag(pole, "103.3", "102.0", "0", "0", "104")).is_signal  # rectangle
    assert not pennant_signal(zigzag(pole, "103.3", "102.0", "0.05", "-0.05", "104.5")).is_signal  # broadening
    assert not pennant_signal(zigzag(pole, "103.3", "102.0", "0", "0.05", "104")).is_signal  # ascending triangle
    assert not pennant_signal(zigzag(pole, "103.3", "102.0", "-0.05", "-0.05", "104")).is_signal  # falling channel
    # no flagpole: the same converging shape hovering at the base level (0.5% of range, not 1.5%)
    flat = [pbar(i, 100.2, 99.8, 100) for i in range(46)]
    assert not pennant_signal(zigzag(flat, "100.4", "100.0", "-0.015", "0.01", "100.45")).is_signal


def test_pennant_bars_must_be_regular_session_bars() -> None:
    bars = next(b for b in (pennant_series(s) for s in range(300)) if pennant_signal(b).is_signal)
    assert pennant_signal(bars).is_signal  # the session opens at the first bar here
    # the same series shifted so its pole is pre-market: only the last 20 bars are regular
    shifted_open = PEN_OPEN - timedelta(minutes=len(bars) - 20)
    moved = [pbar(i, b.high, b.low, b.close, base=shifted_open) for i, b in enumerate(bars)]
    result = pennant_signal(moved)
    assert not result.is_signal and result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    # a pennant that begins in the previous session does not span the overnight gap
    overnight = [pbar(i, b.high, b.low, b.close, base=PEN_OPEN + timedelta(days=1, minutes=-40))
                 if i >= 40 else pbar(i, b.high, b.low, b.close) for i, b in enumerate(bars)]
    assert not pennant_signal(overnight).is_signal
    # ... but a pennant wholly inside the second session is fine even with an overnight gap before it
    late = [pbar(i, b.high, b.low, b.close, base=PEN_OPEN + timedelta(days=1, minutes=-20))
            if i >= 20 else pbar(i, b.high, b.low, b.close) for i, b in enumerate(bars)]
    assert pennant_signal(late).is_signal
