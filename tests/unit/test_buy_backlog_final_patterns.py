"""Final BUY backlog patterns: gap_exhaustion (multi-session context) and bull_pennant_breakout.

Volatility Contraction Pattern and Cup and Handle stay BLOCKED_BY_INFRASTRUCTURE; a test pins the
data-contract fact that blocks them (no daily timeframe, bounded intraday history).
"""

from __future__ import annotations

import inspect
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

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
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.market_data.session_reference import (
    RegularSessionOHLC,
    SessionReference,
    SessionReferenceTracker,
)
from mike_trade.research.commands import _research_registry
from mike_trade.scheduling.calendar import SessionStatus, previous_session, session_for
from mike_trade.strategies.base import ParameterValidationError, ReasonCode
from mike_trade.strategies.registry import StrategyRegistry

D = Decimal
DAY = date(2026, 10, 9)
WATCHING = {"eligibility": "WATCHING_BUY"}
REGISTRY = _research_registry()


def prior_days(count: int, day: date = DAY) -> list[date]:
    """The ``count`` sessions immediately before ``day``, oldest first."""
    found, cursor = [], day
    for _ in range(count):
        cursor = previous_session(cursor).session_date
        found.append(cursor)
    return found[::-1]


def minute_bar(day: date, index: int, o: str, h: str, low: str, c: str, volume: str = "1000") -> Candle:
    start = session_for(day).open_utc + timedelta(minutes=index)
    return Candle("SPY", CandleTimeframe.M1, start, start + timedelta(minutes=1), D(o), D(h), D(low),
                  D(c), D(volume), "sip", True, CandleOrigin.REPLAY, PriceBasis.RAW,
                  CandleStatus.CLOSED)


def context(bars: list[Candle], reference: SessionReference | None, day: date = DAY,
            stale: bool = False) -> MarketContext:
    as_of = bars[-1].end
    return MarketContext(
        symbol="SPY", as_of=as_of, version=len(bars), feed_id="sip", consolidated=True,
        price_basis=PriceBasis.RAW, session_status=SessionStatus.REGULAR,
        session_id=session_for(day).session_id,
        freshness=ContextFreshness(as_of, timedelta(0), timedelta(minutes=5), stale,
                                   "DATA_TOO_OLD" if stale else None),
        candles={CandleTimeframe.M1: tuple(bars)}, indicators={}, session_reference=reference,
    )


# ======================================================================= registration


NEW_IDS = ("gap_exhaustion", "bull_pennant_breakout")


def test_library_counts_and_registration_parity() -> None:
    counts = {role: len(REGISTRY.list_strategies(role)) for role in StrategyRole}
    assert counts == {StrategyRole.BUY: 93, StrategyRole.SELL: 43, StrategyRole.SHORT: 28,
                      StrategyRole.COVER: 28}
    assert len(REGISTRY.list_strategies()) == 192
    production = inspect.getsource(app.build_runtime)
    research = inspect.getsource(inspect.getmodule(_research_registry)._research_registry)  # type: ignore[union-attr]
    for strategy_id in NEW_IDS:
        cls = REGISTRY.get(strategy_id)
        assert cls.spec.side is StrategyRole.BUY and cls.spec.version == "1.0.0"
        assert cls.spec.warmup_bars == cls.required_bars({})
        assert production.count(f"{cls.__name__},") == 1
        assert 1 <= research.count(f"{cls.__name__},") <= 2
    ids = [c.spec.strategy_id for c in REGISTRY.list_strategies()]
    assert len(ids) == len(set(ids))


def test_daily_pattern_candidates_are_registered_on_the_separate_d1_history_path() -> None:
    # VCP / Cup-and-Handle were blocked on missing daily history; D1/W1 support unblocked them
    for sid in ("volatility_contraction_pattern", "cup_and_handle"):
        cls = REGISTRY.get(sid)
        assert CandleTimeframe.D1 in cls.spec.required_timeframes
        assert cls.spec.history_bars and cls.spec.history_bars[0][0] is CandleTimeframe.D1
    assert {tf.value for tf in CandleTimeframe} == {"1m", "5m", "15m", "30m", "60m", "1d", "1w"}
    from mike_trade.market_data.session_reference import MAX_HISTORY_SESSIONS

    # ... while the multi-session intraday reference chain used by gap_exhaustion stays a bounded
    # few sessions (it is not the D1 store, which carries 150/200-day averages and 52-week ranges)
    assert MAX_HISTORY_SESSIONS < 20


# ===================================================================== gap exhaustion

GE = {}  # default parameters


def history(closes: list[str], *, volume: str | None = "390000", low_offset: str = "2") -> tuple[RegularSessionOHLC, ...]:
    days = prior_days(len(closes))
    out = []
    for day, close in zip(days, closes, strict=True):
        s = session_for(day)
        c = D(close)
        out.append(RegularSessionOHLC(
            s.session_id, c + 1, c + 4, c - D(low_offset), c, s.open_utc, s.close_utc,
            None if volume is None else D(volume)))
    return tuple(out)


def reference(hist: tuple[RegularSessionOHLC, ...], open_: str = "98.5") -> SessionReference:
    prior = previous_session(DAY)
    return SessionReference(session_for(DAY).session_id, prior.session_id, D(open_),
                            hist[-1] if hist else None, hist)


DOWN = ["110", "108", "106", "104", "102"]  # five sessions: four consecutive lower closes (-7.3%)
RECLAIM = ["98.6", "98.4", "98.8", "99.2", "99.6", "100.2"]  # previous low = 100 (close 102, offset 2)


def gap_bars(closes: list[str] = RECLAIM, open_: str = "98.5", volume: str = "2000", start: int = 0) -> list[Candle]:
    out, previous = [], D(open_)
    for i, c in enumerate(closes):
        close = D(c)
        out.append(minute_bar(DAY, start + i, str(previous), str(max(previous, close) + D("0.1")),
                              str(min(previous, close) - D("0.1")), c, volume))
        previous = close
    return out


def ge(bars, ref, params=None, state=None):
    return REGISTRY.get("gap_exhaustion")().evaluate(context(bars, ref), params or {}, state or dict(WATCHING))


def quiet_ge(*args, **kwargs):
    """A negative must be a genuine NO_SIGNAL (the rule was evaluated), not missing data."""
    result = ge(*args, **kwargs)
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,), result.reason_codes
    return result


def test_gap_exhaustion_signals_on_the_first_close_back_above_the_gap_edge() -> None:
    bars, ref = gap_bars(), reference(history(DOWN))
    result = ge(bars, ref)
    assert result.is_signal and result.bar_time == bars[-1].start and result.reference_price == D("100.2")
    # one bar earlier the gap is not yet closed: nothing fires (no look-ahead)
    quiet_ge(bars[:-1], ref)
    # the first-closure edge fires once: a further higher bar, or a replay of the same bar, does not
    quiet_ge(bars + gap_bars(["101"], open_="100.2", start=len(bars)), ref)
    quiet_ge(bars, ref, state={**WATCHING, **result.state_updates})


def test_gap_exhaustion_requires_every_part_of_the_context() -> None:
    bars = gap_bars()
    assert ge(bars, reference(history(DOWN))).is_signal
    # no downtrend: one session closed higher than the one before
    quiet_ge(bars, reference(history(["110", "108", "109", "104", "102"])))
    # a decline that is consecutive but too small for min_decline_pct
    quiet_ge(bars, reference(history(["102.6", "102.4", "102.3", "102.1", "102"])))
    assert ge(bars, reference(history(["102.6", "102.4", "102.3", "102.1", "102"])),
              {"min_decline_pct": D("0.5")}).is_signal
    # open below the previous close but NOT below the previous low (no gap in the chart sense)
    quiet_ge(gap_bars(open_="100.5"), reference(history(DOWN), "100.5"))
    # a gap smaller than min_gap_pct (open 99.9, 0.1 below the low of 100)
    shallow = gap_bars(["99.95", "99.9", "100.2"], open_="99.9")
    quiet_ge(shallow, reference(history(DOWN), "99.9"))
    # quiet gap: the per-minute volume is only the trend sessions' average (relative volume 1)
    quiet_ge(gap_bars(volume="1000"), reference(history(DOWN)))
    assert ge(gap_bars(volume="1000"), reference(history(DOWN)), {"min_relative_volume": D(0)}).is_signal
    # a re-cross is not the first closure: price closed above the gap edge, fell back and re-crossed
    again = gap_bars(["98.6", "100.4", "99.5", "99.9", "100.3"])
    quiet_ge(again, reference(history(DOWN)))
    # gaining the edge without ever being under it is not a gap closure
    quiet_ge(gap_bars(["100.5", "100.6"], open_="100.4"), reference(history(DOWN), "100.4"))


def test_gap_exhaustion_boundaries() -> None:
    ref = reference(history(DOWN))
    # the close must be strictly above the edge (100)
    quiet_ge(gap_bars(["98.6", "99", "100"]), ref)
    assert ge(gap_bars(["98.6", "99", "100.01"]), ref).is_signal
    # decline exactly at the threshold counts: (110-102)/110 = 7.2727..%
    assert ge(gap_bars(), ref, {"min_decline_pct": D("7.27")}).is_signal
    quiet_ge(gap_bars(), ref, {"min_decline_pct": D("7.28")})
    # gap exactly at the threshold, measured against the previous LOW (100): open 99.00 -> 1.00%
    exact = gap_bars(["99.2", "99.6", "100.2"], open_="99")
    assert ge(exact, reference(history(DOWN), "99"), {"min_gap_pct": D("1")}).is_signal
    quiet_ge(exact, reference(history(DOWN), "99"), {"min_gap_pct": D("1.01")})
    # ... and 99.01 is just short of 1% of the low (it would still pass against the close, 102)
    just_short = gap_bars(["99.2", "99.6", "100.2"], open_="99.01")
    quiet_ge(just_short, reference(history(DOWN), "99.01"), {"min_gap_pct": D("1")})


def test_gap_exhaustion_does_not_fabricate_a_trend_from_missing_or_unknown_history() -> None:
    bars = gap_bars()
    insufficient = (ReasonCode.INSUFFICIENT_DATA,)
    # four sessions are not enough for the default four consecutive lower closes (needs five)
    assert ge(bars, reference(history(DOWN[1:]))).reason_codes == insufficient
    assert ge(bars, reference(())).reason_codes == insufficient  # no previous session at all
    assert ge(bars, None).reason_codes == insufficient
    # trend_sessions is configurable; two lower closes need three sessions
    assert ge(bars, reference(history(DOWN[2:])), {"trend_sessions": 2}).is_signal
    # session volume unknown while the volume test is on -> fail closed; off -> still evaluable
    unknown = reference(history(DOWN, volume=None))
    assert ge(bars, unknown).reason_codes == insufficient
    assert ge(bars, unknown, {"min_relative_volume": D(0)}).is_signal
    # today's bars must start at the open and have no missing minute
    late = gap_bars(start=1)
    assert ge(late, reference(history(DOWN))).reason_codes == insufficient
    holed = gap_bars()[:3] + gap_bars(["99.6", "100.2"], open_="99.2", start=4)
    assert ge(holed, reference(history(DOWN))).reason_codes == insufficient


def test_gap_exhaustion_guards_and_parameters() -> None:
    bars, ref = gap_bars(), reference(history(DOWN))
    strategy = REGISTRY.get("gap_exhaustion")()
    ctx = context(bars, ref)
    for state in ({}, {"eligibility": "WATCHING_SHORT"}):
        assert strategy.evaluate(ctx, {}, state).reason_codes == (ReasonCode.NOT_ELIGIBLE,)
    stale = context(bars, ref, stale=True)
    assert strategy.evaluate(stale, {}, dict(WATCHING)).reason_codes == (ReasonCode.STALE_DATA,)
    for bad in ({"trend_sessions": 1}, {"trend_sessions": 9}, {"min_decline_pct": D("0.4")},
                {"min_gap_pct": D("0.05")}, {"min_gap_pct": D(21)}, {"min_relative_volume": D("-0.1")},
                {"min_relative_volume": D(11)}, {"min_gap_pct": 1.5}):
        with pytest.raises(ParameterValidationError):
            strategy.evaluate(ctx, bad, dict(WATCHING))


def test_gap_exhaustion_reads_the_real_tracker_history_end_to_end() -> None:
    """History produced by SessionReferenceTracker from M1 bars drives the strategy."""
    tracker = SessionReferenceTracker()
    for day, close in zip(prior_days(5), DOWN, strict=True):
        s = session_for(day)
        c = D(close)
        for i in range(int((s.close_utc - s.open_utc) / timedelta(minutes=1))):
            start = s.open_utc + timedelta(minutes=i)
            last = start + timedelta(minutes=1) == s.close_utc
            price = c if last else c + 1
            tracker.add_bar(Candle("SPY", CandleTimeframe.M1, start, start + timedelta(minutes=1),
                                   min(price, c + 1), c + 4 if i == 10 else c + 1,
                                   c - 2 if i == 20 else c, price, D(1000), "sip", True,
                                   CandleOrigin.REPLAY, PriceBasis.RAW, CandleStatus.CLOSED))
    bars = gap_bars()
    for bar in bars:
        tracker.add_bar(bar)
    ref = tracker.reference("SPY", bars[-1].end)
    assert ref is not None and len(ref.history) == 5 and ref.previous is not None
    assert ref.previous.low == D("100")  # c - 2 for the last close of 102
    assert [h.close for h in ref.history] == [D(x) for x in DOWN]
    assert ge(bars, ref).is_signal
    # the tracker already holds later bars, so an earlier as_of fails closed (no future-derived state)
    earlier = tracker.reference("SPY", bars[-2].end)
    assert earlier is not None and earlier.history == () and earlier.previous is None


async def test_backtest_replay_reaches_the_same_multi_session_decision(tmp_path: Path) -> None:
    provenance = FeedProvenance("replay", "IEX", "synthetic", False)
    events: list[ReplayEvent] = []

    def add(candle_start, o, h, low, c, volume) -> None:
        events.append(ReplayEvent.bar(ReplayCandle(
            "SPY", Timeframe.M1, candle_start, candle_start + timedelta(minutes=1), D(o), D(h), D(low),
            D(c), D(volume), provenance)))

    for day, close in zip(prior_days(5), DOWN, strict=True):
        s = session_for(day)
        c = D(close)
        for i in range(int((s.close_utc - s.open_utc) / timedelta(minutes=1))):
            add(s.open_utc + timedelta(minutes=i), c, c + 1 if i == 5 else c, c - 2 if i == 9 else c, c, "1000")
    today = session_for(DAY)
    previous = D("98.5")
    for i, close in enumerate(RECLAIM):
        c = D(close)
        add(today.open_utc + timedelta(minutes=i), previous, max(previous, c) + D("0.1"),
            min(previous, c) - D("0.1"), c, "2000")
        previous = c
    first = session_for(prior_days(5)[0])
    clock = ReplayClock(first.open_utc)
    feed = HistoricalReplayFeed(events, DataProvenance("synthetic", "IEX", False, "gap-exhaustion"),
                                clock=clock, seed=3)
    registry = StrategyRegistry()
    registry.register(REGISTRY.get("gap_exhaustion"))
    request = BacktestRequest(
        symbols=("SPY",), window_start=first.open_utc,
        window_end=today.open_utc + timedelta(minutes=len(RECLAIM) + 1), period_role=PeriodRole.RESEARCH,
        config={}, seed=3, starting_capital=D("10000"), strategy_ids=("gap_exhaustion",),
        context_history_bars=500, result_directory=tmp_path)
    result = await BacktestRunner(registry, feed, clock, HistoricalExecutionSimulator(feed, clock)).run(request)
    assert [row["strategy"] for row in result.signals] == ["gap_exhaustion"]
    assert result.signals[0]["bar_time"] == today.open_utc + timedelta(minutes=len(RECLAIM) - 1)


# ================================================================== bull pennant breakout

PEN = {"lookback": 22}


def path(points: list[tuple[int, str]]) -> list[Decimal]:
    out: list[Decimal] = []
    for (i0, c0), (i1, c1) in zip(points, points[1:], strict=False):
        a, b = D(c0), D(c1)
        out += [a + (b - a) * (i - i0) / (i1 - i0) for i in range(i0, i1)]
    out.append(D(points[-1][1]))
    return out


def closes_to_bars(closes: list[Decimal], half: str = "0.2", start: int = 0) -> list[Candle]:
    day = DAY
    return [minute_bar(day, start + i, str(c), str(c + D(half)), str(c - D(half)), str(c))
            for i, c in enumerate(closes)]


POLE = [(0, "100"), (3, "100"), (10, "110")]
# swing highs 110.2 (pole top, x10), 109.2 (x14), 108.8 (x18) fall; swing lows 107.3 (x12),
# 107.8 (x16), 108.0 (x20) rise; the upper boundary through x14/x18 reaches 108.4 at x22
PENNANT = [(10, "110"), (12, "107.5"), (14, "109"), (16, "108"), (18, "108.6"), (20, "108.2"), (21, "108.3")]


def pennant_bars(breakout: str = "108.9") -> list[Candle]:
    closes = [D(100)] + path(POLE[:-1] + PENNANT)  # one extra leading bar: bars[:-1] still has 23 bars
    return closes_to_bars(closes + [D(breakout)])


def quiet(result):
    """A geometric negative must be a genuine NO_SIGNAL, never merely missing data."""
    assert result.reason_codes == (ReasonCode.NO_SIGNAL,), result.reason_codes
    return result


def pen(bars, params=None, state=None):
    return REGISTRY.get("bull_pennant_breakout")().evaluate(
        context(bars, None), {**PEN, **(params or {})}, state or dict(WATCHING))


def test_pennant_signals_on_the_first_close_above_the_falling_upper_boundary() -> None:
    bars = pennant_bars()
    assert len(bars) == 24
    result = pen(bars)
    assert result.is_signal and result.bar_time == bars[-1].start
    # independent geometry: the upper line through the swing highs (x14, 109.2) and (x18, 108.8)
    upper_22 = D("108.8") + (D("108.8") - D("109.2")) / 4 * (22 - 18)
    lower_22 = D("108.0") + (D("108.0") - D("107.8")) / 4 * (22 - 20)
    assert upper_22 == D("108.4") and lower_22 > D("108.0") and upper_22 > lower_22
    quiet(pen(pennant_bars("108.4")))
    assert pen(pennant_bars("108.41")).is_signal
    # no look-ahead / one signal: the bar before the breakout, a further bar, and a replay do nothing
    quiet(pen(bars[:-1]))
    quiet(pen(bars + closes_to_bars([D("109.5")], start=24)))
    quiet(pen(bars, state={**WATCHING, **result.state_updates}))


def test_pennant_geometry_boundaries_and_invalidations() -> None:
    assert pen(pennant_bars()).is_signal
    base = path(POLE[:-1] + PENNANT)
    # the flagpole must be big enough and short enough
    quiet(pen(pennant_bars(), {"pole_pct": D(15)}))
    quiet(pen(pennant_bars(), {"pole_bars": 3}))
    # the pennant must last long enough and not too long (11 bars)
    quiet(pen(pennant_bars(), {"min_pennant_bars": 12}))
    quiet(pen(pennant_bars(), {"max_pennant_bars": 8}))
    # a deep first pullback retraces too much of the pole
    deep = list(base)
    deep[12] = D("90")
    deep_bars = closes_to_bars(deep + [D("108.9")])
    quiet(pen(deep_bars, {"max_retrace": D("0.1")}))
    # a premature close above the upper boundary means the first breakout already happened
    early = list(base)
    early[16] = D("109.3")
    quiet(pen(closes_to_bars(early + [D("109.6")])))
    # a close below the rising lower boundary invalidates the formation
    broken = list(base)
    broken[17] = D("107.0")
    quiet(pen(closes_to_bars(broken + [D("108.9")])))


def converge(upper: list[str], lower: list[str]) -> list[Candle]:
    """Pole then a zigzag through the given swing-high and swing-low closes, then a breakout."""
    points = POLE[:-1] + [(10, "110")]
    x = 10
    for hi, lo in zip(upper, lower, strict=False):
        points += [(x + 2, lo), (x + 4, hi)]
        x += 4
    closes = path(points)
    return closes_to_bars(closes + [D("109.8")])


def test_pennant_needs_converging_boundaries_unlike_a_rectangular_flag() -> None:
    assert pen(pennant_bars()).is_signal
    # rectangle: equal swing highs and equal swing lows (a bull flag shape), same breakout idea
    rect = converge(["109", "109", "109"], ["107.5", "107.5", "107.5"])
    quiet(pen(rect, {"lookback": 23, "max_pennant_bars": 20}))
    flag = REGISTRY.get("bull_flag_breakout")().evaluate(
        context(rect, None), {"lookback": 23, "max_flag_bars": 20, "pole_bars": 10}, dict(WATCHING))
    assert flag.is_signal  # the rectangular consolidation is the bull flag's pattern, not a pennant
    # broadening (rising highs and falling lows) and ascending (flat highs, rising lows) are not pennants
    broad = converge(["108.5", "109", "109.5"], ["107.5", "107", "106.5"])
    quiet(pen(broad, {"lookback": 23, "max_pennant_bars": 20}))
    flat_top = converge(["109", "109", "109"], ["107", "107.5", "108"])
    quiet(pen(flat_top, {"lookback": 23, "max_pennant_bars": 20}))


def test_pennant_is_not_a_plain_triangle_without_a_flagpole() -> None:
    # a long, gently converging triangle with no preceding advance: triangle_breakout fires, the
    # pennant (pole + short formation) must not
    points = [(0, "100"), (2, "104"), (4, "100.5"), (8, "103.6"), (12, "101"), (16, "103.2"),
              (20, "101.5"), (24, "102.8"), (28, "102"), (30, "102.3")]
    closes = path(points)
    bars = closes_to_bars(closes + [D("103.5")])
    triangle = REGISTRY.get("triangle_breakout")().evaluate(
        context(bars, None), {"strength": 1, "lookback": 30}, dict(WATCHING))
    assert triangle.is_signal
    quiet(pen(bars, {"lookback": 30}))


def test_pennant_uses_only_pivots_confirmed_before_the_signal_bar() -> None:
    # the only second swing high would be the last base bar (x21), which needs the signal bar as its
    # right-hand neighbour: it is not a pivot, so no upper boundary exists and nothing fires
    closes = path(POLE[:-1] + [(10, "110"), (12, "107.5"), (15, "108"), (17, "107.9"), (19, "108.2"),
                               (21, "108.9")])
    unconfirmed = closes_to_bars(closes + [D("108.7")])
    quiet(pen(unconfirmed))


def test_pennant_missing_bars_warmup_session_and_parameters() -> None:
    bars = pennant_bars()
    gapped = bars[:12] + [minute_bar(DAY, 13 + i, str(b.open), str(b.high), str(b.low), str(b.close))
                          for i, b in enumerate(bars[12:])]
    assert pen(gapped).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)
    assert pen(bars[-22:]).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)  # needs lookback + 1 = 23
    strategy = REGISTRY.get("bull_pennant_breakout")()
    for state in ({}, {"eligibility": "WATCHING_SHORT"}):
        assert strategy.evaluate(context(bars, None), PEN, state).reason_codes == (ReasonCode.NOT_ELIGIBLE,)
    assert strategy.evaluate(context(bars, None, stale=True), PEN, dict(WATCHING)).reason_codes == (
        ReasonCode.STALE_DATA,)
    for bad in ({"strength": 0}, {"strength": 6}, {"lookback": 19}, {"pole_bars": 2},
                {"pole_pct": D("0.1")}, {"min_pennant_bars": 3}, {"max_pennant_bars": 5},
                {"max_retrace": D("0.95")}, {"max_retrace": D("0.05")}, {"min_pennant_bars": 20, "max_pennant_bars": 10},
                {"lookback": 22, "max_pennant_bars": 21}, {"strength": 3, "min_pennant_bars": 8},
                {"pole_pct": 1.5}):
        with pytest.raises(ParameterValidationError):
            pen(bars, bad)
    assert strategy.required_bars({}) == strategy.spec.warmup_bars == 61
    assert strategy.required_bars({"lookback": 80}) == 81


async def test_pennant_backtest_signal_matches_direct_evaluation(tmp_path: Path) -> None:
    bars = pennant_bars()
    provenance = FeedProvenance("replay", "IEX", "synthetic", False)
    events = [ReplayEvent.bar(ReplayCandle("SPY", Timeframe.M1, b.start, b.end, b.open, b.high, b.low,
                                           b.close, b.volume, provenance)) for b in bars]
    first = session_for(DAY).open_utc
    clock = ReplayClock(first)
    feed = HistoricalReplayFeed(events, DataProvenance("synthetic", "IEX", False, "pennant"),
                                clock=clock, seed=5)
    registry = StrategyRegistry()
    registry.register(REGISTRY.get("bull_pennant_breakout"))
    request = BacktestRequest(
        symbols=("SPY",), window_start=first, window_end=first + timedelta(minutes=len(bars) + 1),
        period_role=PeriodRole.RESEARCH, config={}, seed=5, starting_capital=D("10000"),
        strategy_ids=("bull_pennant_breakout",), strategy_params={"bull_pennant_breakout": PEN},
        context_history_bars=500, result_directory=tmp_path)
    result = await BacktestRunner(registry, feed, clock, HistoricalExecutionSimulator(feed, clock)).run(request)
    assert [row["bar_time"] for row in result.signals] == [bars[-1].start]


def test_pennant_breakout_must_come_before_the_apex() -> None:
    from mike_trade.strategies.indicators.patterns import line_value
    from mike_trade.strategies.indicators.windows import swing_pivots

    points = POLE[:-1] + [(10, "110"), (12, "107.4"), (14, "108.8"), (16, "107.8"), (18, "108.4"),
                          (20, "108.2"), (21, "108.2")]
    bars = closes_to_bars(path(points) + [D("108.9")])
    base = bars[-23:-1]
    highs, lows = swing_pivots(base, 1)
    (hx1, hy1), (hx2, hy2) = [p for p in highs if p[0] >= 9][-2:]  # base index of the pole top is 9
    (lx1, ly1), (lx2, ly2) = [p for p in lows if p[0] > 9][-2:]
    last = len(base)
    upper, lower = line_value(hx1, hy1, hx2, hy2, last), line_value(lx1, ly1, lx2, ly2, last)
    assert hy2 < hy1 and ly2 > ly1       # falling highs, rising lows ...
    assert bars[-1].close > upper         # ... the close is above the extended upper line ...
    assert upper <= lower                 # ... but the lines have already met: past the apex
    quiet(pen(bars))


def test_pennant_close_under_the_rising_lower_boundary_invalidates_it_without_a_new_pivot() -> None:
    bars = pennant_bars()
    assert pen(bars).is_signal
    # x17 (index 18 with the leading bar) closes 107.84, just under the lower line (107.85 there),
    # with a tiny wick so it is not itself a swing low and the pivots are unchanged
    k = 18
    odd = minute_bar(DAY, k, "107.9", "107.9", "107.83", "107.84")
    assert odd.close < D("107.85") and odd.low > D("107.8")
    quiet(pen(bars[:k] + [odd] + bars[k + 1:]))
