"""Unit tests for bounded warmup state diagnostics (new entries only)."""

import dataclasses
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from mike_trade.market_data import warmup as warmup_module
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import (
    ContextFreshness,
    MarketContext,
)
from mike_trade.market_data.warmup import (
    GateDecision,
    WarmupDiagnostics,
    WarmupRequirement,
    WarmupState,
    WarmupTracker,
)
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.indicators.core import EMACalculator, VWAPCalculator

NY = ZoneInfo("America/New_York")
M1 = CandleTimeframe.M1
D = Decimal


def ny(h, m=0, day=8):
    return datetime(2026, 10, day, h, m, tzinfo=NY)


class FakeClock:
    def __init__(self, now):
        self.value = now

    def now(self):
        return self.value


def closed_candle(symbol, start, *, feed="sip", volume="1000", close="100.5",
                  basis=PriceBasis.RAW, status=CandleStatus.CLOSED, minutes=1):
    return Candle(
        symbol=symbol, timeframe=M1, start=start,
        end=start + timedelta(minutes=minutes),
        open=D("100"), high=D("101"), low=D("99"), close=D(close),
        volume=D(volume), feed_id=feed, consolidated=(feed != "iex"),
        origin=CandleOrigin.REPLAY, price_basis=basis, status=status,
    )


def consecutive(symbol, first, count, **kw):
    return [closed_candle(symbol, first + timedelta(minutes=i), **kw) for i in range(count)]


def fresh_context(symbol, bars, as_of, *, session_status=SessionStatus.REGULAR,
                  session_id="XNYS-2026-10-08", feed="sip", stale=False):
    newest = max([b.end for b in bars]) if bars else as_of - timedelta(seconds=1)
    age = max(as_of - newest, timedelta(0))
    freshness = ContextFreshness(
        newest, age, timedelta(seconds=60), stale,
        "DATA_TOO_OLD" if stale else None,
    )
    return MarketContext(
        symbol=symbol, as_of=as_of, version=1, feed_id=feed,
        consolidated=(feed != "iex"), price_basis=PriceBasis.RAW,
        session_status=session_status, session_id=session_id,
        freshness=freshness, candles={M1: tuple(bars)}, indicators={},
    )


def tracker_for(clock, reqs, **kw):
    return WarmupTracker(clock=clock, requirements={"AAPL": tuple(reqs)}, **kw)


def base_req(**kw):
    params = {"name": "m1", "timeframe": M1, "bars_needed": 5}
    params.update(kw)
    return WarmupRequirement(**params)


# ---------------------------------------------------------------- boundary


def test_n_minus_1_warming_n_ready():
    clock = FakeClock(ny(10, 10))
    tracker = tracker_for(clock, [base_req(bars_needed=5)])
    bars = consecutive("AAPL", ny(9, 30), 4)
    tracker.seed_historical(bars)
    diag = tracker.diagnostics("AAPL")
    status = diag.for_symbol("AAPL").requirements[0]
    assert (status.bars_have, status.bars_needed) == (4, 5)
    assert status.state is WarmupState.WARMING
    assert "INSUFFICIENT_BARS" in status.reasons
    tracker.add_bar(closed_candle("AAPL", ny(9, 30) + timedelta(minutes=4)))
    status = tracker.diagnostics("AAPL").for_symbol("AAPL").requirements[0]
    assert (status.bars_have, status.bars_needed) == (5, 5)
    assert status.state is WarmupState.READY
    assert status.reasons == ()


def test_indicator_warmup_bars_extend_need():
    clock = FakeClock(ny(10, 10))
    req = WarmupRequirement(name="ema5", timeframe=M1, bars_needed=2,
                            indicator_factory=lambda: EMACalculator(5))
    assert req.effective_bars_needed == 5
    tracker = tracker_for(clock, [req])
    tracker.seed_historical(consecutive("AAPL", ny(9, 30), 4))
    status = tracker.diagnostics("AAPL").for_symbol("AAPL").requirements[0]
    assert status.bars_needed == 5 and status.state is WarmupState.WARMING
    tracker.add_bar(closed_candle("AAPL", ny(9, 30) + timedelta(minutes=4)))
    assert tracker.diagnostics("AAPL").for_symbol("AAPL").requirements[0].state is WarmupState.READY


# ---------------------------------------------------------------- determinism


def test_replay_equal_diagnostics_and_seed_matches_live():
    seq = consecutive("AAPL", ny(9, 30), 6)
    c1, c2 = FakeClock(ny(10, 10)), FakeClock(ny(10, 10))
    t1 = tracker_for(c1, [base_req()])
    t2 = tracker_for(c2, [base_req()])
    for b in seq:
        t1.add_bar(b)
    for b in seq:
        t2.add_bar(b)
    assert t1.diagnostics() == t2.diagnostics()

    c3, c4 = FakeClock(ny(10, 10)), FakeClock(ny(10, 10))
    t3 = tracker_for(c3, [base_req()])
    t4 = tracker_for(c4, [base_req()])
    t3.seed_historical(list(seq))
    for b in seq:
        t4.add_bar(b)
    assert t3.diagnostics() == t4.diagnostics()
    assert t3.diagnostics("AAPL").for_symbol("AAPL").requirements[0].state is WarmupState.READY


# ---------------------------------------------------------------- negative gates


def positive_setup(volume_relevant=False):
    clock = FakeClock(ny(10, 10))
    req = base_req(bars_needed=3, volume_relevant=volume_relevant)
    tracker = tracker_for(clock, [req])
    bars = consecutive("AAPL", ny(9, 30), 3)
    tracker.seed_historical(bars)
    ctx = fresh_context("AAPL", bars, ny(10, 10))
    return tracker, ctx


def test_positive_gate_allows():
    tracker, ctx = positive_setup()
    decision = tracker.buy_gate("AAPL", ctx)
    assert decision.allowed is True and decision.reason_codes == ()


def test_stale_context_blocks():
    tracker, _ = positive_setup()
    bars = consecutive("AAPL", ny(9, 30), 3)
    ctx = fresh_context("AAPL", bars, ny(10, 10), stale=True)
    decision = tracker.buy_gate("AAPL", ctx)
    assert decision.allowed is False and "STALE_DATA" in decision.reason_codes


def test_missing_context_blocks():
    tracker, _ = positive_setup()
    decision = tracker.buy_gate("AAPL", None)
    assert decision.allowed is False and "MISSING_DATA" in decision.reason_codes


def test_bar_gap_blocks():
    clock = FakeClock(ny(10, 10))
    tracker = tracker_for(clock, [base_req(bars_needed=3)])
    bars = [closed_candle("AAPL", ny(9, 30) + timedelta(minutes=i)) for i in (0, 1, 3)]
    tracker.seed_historical(bars)
    ctx = fresh_context("AAPL", bars, ny(10, 10))
    decision = tracker.buy_gate("AAPL", ctx)
    assert decision.allowed is False and "BAR_GAP" in decision.reason_codes


def test_zero_volume_blocks_when_relevant():
    clock = FakeClock(ny(10, 10))
    tracker = tracker_for(clock, [base_req(bars_needed=3, volume_relevant=True)])
    bars = consecutive("AAPL", ny(9, 30), 2) + [closed_candle("AAPL", ny(9, 32), volume="0")]
    tracker.seed_historical(bars)
    ctx = fresh_context("AAPL", bars, ny(10, 10))
    decision = tracker.buy_gate("AAPL", ctx)
    assert decision.allowed is False and "ZERO_VOLUME" in decision.reason_codes


def test_zero_volume_ignored_when_not_relevant():
    clock = FakeClock(ny(10, 10))
    tracker = tracker_for(clock, [base_req(bars_needed=3, volume_relevant=False)])
    bars = consecutive("AAPL", ny(9, 30), 2) + [closed_candle("AAPL", ny(9, 32), volume="0")]
    tracker.seed_historical(bars)
    ctx = fresh_context("AAPL", bars, ny(10, 10))
    assert tracker.buy_gate("AAPL", ctx).allowed is True


def test_invalid_ohlc_blocks():
    clock = FakeClock(ny(10, 10))
    tracker = tracker_for(clock, [base_req(bars_needed=2)])
    tracker.seed_historical(consecutive("AAPL", ny(9, 30), 2))
    bad = SimpleNamespace(
        symbol="AAPL", timeframe=M1, start=ny(9, 32), end=ny(9, 33),
        open=D("100"), high=D("98"), low=D("99"), close=D("100"),
        volume=D("10"), feed_id="sip", price_basis=PriceBasis.RAW,
        status=CandleStatus.CLOSED,
    )
    tracker.add_bar(bad)
    bars = consecutive("AAPL", ny(9, 30), 2)
    decision = tracker.buy_gate("AAPL", fresh_context("AAPL", bars, ny(10, 10)))
    assert decision.allowed is False and "INVALID_OHLC" in decision.reason_codes
    assert tracker.diagnostics("AAPL").for_symbol("AAPL").requirements[0].state is WarmupState.INVALID


def test_mixed_feed_blocks():
    clock = FakeClock(ny(10, 10))
    tracker = tracker_for(clock, [base_req(bars_needed=2)])
    tracker.seed_historical(consecutive("AAPL", ny(9, 30), 2, feed="sip"))
    tracker.add_bar(closed_candle("AAPL", ny(9, 32), feed="iex"))
    bars = consecutive("AAPL", ny(9, 30), 2)
    decision = tracker.buy_gate("AAPL", fresh_context("AAPL", bars, ny(10, 10)))
    assert decision.allowed is False and "MIXED_FEED" in decision.reason_codes


def test_unclosed_bar_blocks():
    clock = FakeClock(ny(10, 10))
    tracker = tracker_for(clock, [base_req(bars_needed=2)])
    tracker.seed_historical(consecutive("AAPL", ny(9, 30), 2))
    tracker.add_bar(closed_candle("AAPL", ny(9, 32), status=CandleStatus.DEVELOPING))
    bars = consecutive("AAPL", ny(9, 30), 2)
    decision = tracker.buy_gate("AAPL", fresh_context("AAPL", bars, ny(10, 10)))
    assert decision.allowed is False and "UNCLOSED_BAR" in decision.reason_codes


def test_basis_mismatch_blocks():
    clock = FakeClock(ny(10, 10))
    tracker = tracker_for(clock, [base_req(bars_needed=2)])
    tracker.seed_historical(consecutive("AAPL", ny(9, 30), 2))
    tracker.add_bar(closed_candle("AAPL", ny(9, 32), basis=PriceBasis.SPLIT_ADJUSTED))
    bars = consecutive("AAPL", ny(9, 30), 2)
    decision = tracker.buy_gate("AAPL", fresh_context("AAPL", bars, ny(10, 10)))
    assert decision.allowed is False and "PRICE_BASIS_MISMATCH" in decision.reason_codes


def test_non_regular_session_blocks():
    tracker, _ = positive_setup()
    bars = consecutive("AAPL", ny(9, 30), 3)
    ctx = fresh_context("AAPL", bars, ny(17, 0),
                        session_status=SessionStatus.CLOSED, session_id="XNYS-2026-10-08")
    decision = tracker.buy_gate("AAPL", ctx)
    assert decision.allowed is False and "SESSION_NOT_REGULAR" in decision.reason_codes


def test_unknown_session_blocks():
    tracker, _ = positive_setup()
    bars = consecutive("AAPL", ny(9, 30), 3)
    ctx = fresh_context("AAPL", bars, ny(10, 10),
                        session_status=SessionStatus.UNKNOWN, session_id=None)
    decision = tracker.buy_gate("AAPL", ctx)
    assert decision.allowed is False and "UNKNOWN_SESSION" in decision.reason_codes


# ---------------------------------------------------------------- session VWAP reset


def test_vwap_session_reset():
    clock = FakeClock(ny(10, 10, day=9))
    req = WarmupRequirement(name="vwap", timeframe=M1, bars_needed=2, session_scoped=True,
                            indicator_factory=VWAPCalculator)
    tracker = WarmupTracker(clock=clock, requirements={"AAPL": (req,)})
    day1 = consecutive("AAPL", ny(9, 30, day=8), 2)
    tracker.seed_historical(day1)
    ctx1 = fresh_context("AAPL", day1, ny(10, 0, day=8), session_id="XNYS-2026-10-08")
    assert tracker.buy_gate("AAPL", ctx1).allowed is True
    ctx2 = fresh_context("AAPL", [], ny(10, 0, day=9), session_id="XNYS-2026-10-09")
    # Freshness with no bars: build manually to avoid NO_DATA staleness.
    ctx2 = dataclasses.replace(
        ctx2, freshness=ContextFreshness(ny(10, 0, day=9), timedelta(0), timedelta(seconds=3600), False, None)
    )
    decision = tracker.buy_gate("AAPL", ctx2)
    assert decision.allowed is False
    assert "INSUFFICIENT_BARS" in decision.reason_codes
    day2 = consecutive("AAPL", ny(9, 30, day=9), 2)
    tracker.seed_historical(day2)
    ctx2b = fresh_context("AAPL", day2, ny(10, 0, day=9), session_id="XNYS-2026-10-09")
    assert tracker.buy_gate("AAPL", ctx2b).allowed is True


# ---------------------------------------------------------------- bounds + immutability + scope


def test_buffers_stay_bounded():
    clock = FakeClock(ny(10, 10))
    tracker = tracker_for(clock, [base_req(bars_needed=3)])
    start = datetime(2026, 10, 8, 9, 30, tzinfo=ZoneInfo("UTC"))
    for i in range(200):
        tracker.add_bar(closed_candle("AAPL", start + timedelta(minutes=i)))
    bound = tracker.buffer_bound("AAPL", M1)
    assert bound == 3
    assert tracker.buffer_size("AAPL", M1) <= bound


def test_diagnostics_immutable():
    tracker, _ = positive_setup()
    diag = tracker.diagnostics("AAPL")
    assert isinstance(diag, WarmupDiagnostics)
    with pytest.raises(dataclasses.FrozenInstanceError):
        diag.as_of = diag.as_of  # type: ignore[misc]
    entry = diag.for_symbol("AAPL")
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.symbol = "X"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.requirements[0].bars_have = 99  # type: ignore[misc]
    decision = tracker.buy_gate("AAPL", fresh_context(
        "AAPL", consecutive("AAPL", ny(9, 30), 3), ny(10, 10)))
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.allowed = False  # type: ignore[misc]


def test_no_exit_fill_stop_reconcile_or_manual_close_api():
    forbidden = ("exit", "fill", "stop", "reconcil", "manual_close", "manual-close",
                 "close_position", "cancel_order", "flatten", "liquidat")
    public = [n for n in dir(warmup_module) if not n.startswith("_")]
    hits = [n for n in public if any(f in n.lower() for f in forbidden)]
    assert hits == []
    tracker_public = [n for n in dir(WarmupTracker) if not n.startswith("_")]
    hits = [n for n in tracker_public if any(f in n.lower() for f in forbidden)]
    assert hits == []
    assert "new entries only" in warmup_module.__doc__.lower()
    assert isinstance(GateDecision(symbol="AAPL", allowed=True, reason_codes=()), GateDecision)
