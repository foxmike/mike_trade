"""Targeted unit tests for the Market Status Monitor subsystem.

Covers the 10 core verification areas:
1. Normal market conditions
2. Transition into warning
3. Transition into crash (including direct flash crash)
4. Stabilization after crash
5. Recovery from stabilizing
6. New crash during recovery and stabilizing (shortcut transitions)
7. Hysteresis (prevents rapid oscillation)
8. Missing and stale reference observations (fail closed for entries, exits preserved)
9. Restart persistence (in-memory and atomic JSON file restoration)
10. Exit authorization in every state and core invariants
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from mike_trade.domain.signals import (
    Signal,
    SignalAction,
    SignalProvenance,
    make_signal_id,
)
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_status.config import (
    DrawdownThresholds,
    HysteresisConfig,
    MarketStatusConfig,
    ReferenceSymbolsConfig,
    VolatilityThresholds,
    WindowConfig,
)
from mike_trade.market_status.metrics import calculate_market_metrics
from mike_trade.market_status.monitor import MarketStatusMonitor
from mike_trade.market_status.persistence import (
    InMemoryMarketStatusRepository,
    JsonFileMarketStatusRepository,
)
from mike_trade.market_status.policy import (
    ActionKind,
    MarketStatusEntryGate,
    PermissionLevel,
    TradingAuthorizationDecision,
    TradingAuthorizationPolicy,
)
from mike_trade.market_status.state import (
    DIRECT_TRANSITIONS,
    PROGRESSIVE_TRANSITIONS,
    VALID_TRANSITIONS,
    MarketState,
    MarketStateSnapshot,
    StateTransitionRecord,
)
from mike_trade.portfolio.manager import EntryRequest

D = Decimal
START_TIME = datetime(2026, 10, 10, 13, 30, tzinfo=UTC)


def make_bar(
    symbol: str,
    index: int,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: str = "1000",
    start_time: datetime = START_TIME,
) -> Candle:
    """Create a standardized closed M1 candle for tests."""
    bar_start = start_time + timedelta(minutes=index)
    return Candle(
        symbol=symbol,
        timeframe=CandleTimeframe.M1,
        start=bar_start,
        end=bar_start + timedelta(minutes=1),
        open=D(open_),
        high=D(high),
        low=D(low),
        close=D(close),
        volume=D(volume),
        feed_id="sip",
        consolidated=True,
        origin=CandleOrigin.REPLAY,
        price_basis=PriceBasis.RAW,
        status=CandleStatus.CLOSED,
    )


def seed_flat_series(
    monitor: MarketStatusMonitor,
    count: int = 30,
    price: str = "100.0",
    start_time: datetime = START_TIME,
) -> None:
    """Feed a steady, baseline price series across all reference symbols."""
    for symbol in monitor.config.symbols.symbols:
        bars = [
            make_bar(symbol, i, price, price, price, price, start_time=start_time)
            for i in range(count)
        ]
        monitor.add_bars(bars)


# ============================================================================
# 1. Normal Conditions
# ============================================================================


def test_normal_market_conditions() -> None:
    monitor = MarketStatusMonitor()
    seed_flat_series(monitor, count=30, price="100.0")

    snapshot = monitor.evaluate()
    assert snapshot.state is MarketState.NORMAL
    assert snapshot.data_available is True
    assert snapshot.is_stale is False
    assert snapshot.metrics.composite_drawdown_percent == D("0")
    assert snapshot.metrics.realized_volatility_bps == D("0")

    auth = snapshot.authorization
    assert auth.buy_allowed is True
    assert auth.short_allowed is True
    assert auth.sell_allowed is True
    assert auth.cover_allowed is True
    assert auth.protective_stops_allowed is True
    assert auth.emergency_liquidation_allowed is True
    assert auth.buy_permission is PermissionLevel.ALLOWED
    assert auth.short_permission is PermissionLevel.ALLOWED


# ============================================================================
# 2. Transition into Warning
# ============================================================================


def test_transition_into_warning() -> None:
    monitor = MarketStatusMonitor()
    seed_flat_series(monitor, count=30, price="100.0")

    # Drop price to 98.0 (2.0% drawdown, worse than -1.5% warning threshold)
    # Confirmation threshold is 2 bars
    # Bar 30: first bar of warning condition
    for s in monitor.config.symbols.symbols:
        monitor.add_bar(make_bar(s, 30, "100.0", "100.0", "98.0", "98.0"))

    snap1 = monitor.evaluate()
    # Still NORMAL because 1 bar < warning_confirmation_bars (2)
    assert snap1.state is MarketState.NORMAL

    # Bar 31: second bar of warning condition
    for s in monitor.config.symbols.symbols:
        monitor.add_bar(make_bar(s, 31, "98.0", "98.2", "97.9", "98.0"))

    snap2 = monitor.evaluate()
    assert snap2.state is MarketState.WARNING
    assert snap2.previous_state is MarketState.NORMAL
    assert len(monitor.transitions) == 1
    assert monitor.transitions[0].from_state is MarketState.NORMAL
    assert monitor.transitions[0].to_state is MarketState.WARNING

    # Authorization in WARNING: BUY restricted, SHORT allowed, exits preserved
    assert snap2.authorization.buy_allowed is False
    assert snap2.authorization.short_allowed is True
    assert snap2.authorization.sell_allowed is True
    assert snap2.authorization.cover_allowed is True


# ============================================================================
# 3. Transition into Crash (Progressive and Flash Crash)
# ============================================================================


def test_transition_into_crash_progressive() -> None:
    monitor = MarketStatusMonitor()
    seed_flat_series(monitor, count=30, price="100.0")

    # Enter WARNING first (2 bars at 98.0, -2.0% drawdown)
    for i in (30, 31):
        for s in monitor.config.symbols.symbols:
            monitor.add_bar(make_bar(s, i, "98.0", "98.0", "98.0", "98.0"))
        monitor.evaluate()
    assert monitor.current_state is MarketState.WARNING

    # Further deteriorate to 96.0 (-4.0% drawdown, worse than -3.5% crash threshold)
    # Bar 32: crash candidate bar 1
    for s in monitor.config.symbols.symbols:
        monitor.add_bar(make_bar(s, 32, "98.0", "98.0", "96.0", "96.0"))
    snap_p1 = monitor.evaluate()
    assert snap_p1.state is MarketState.WARNING  # Needs 2 bars confirmation

    # Bar 33: crash candidate bar 2
    for s in monitor.config.symbols.symbols:
        monitor.add_bar(make_bar(s, 33, "96.0", "96.2", "95.8", "96.0"))
    snap_p2 = monitor.evaluate()
    assert snap_p2.state is MarketState.CRASH
    assert snap_p2.previous_state is MarketState.WARNING

    # Authorization in CRASH: BUY blocked, SHORT allowed, exits preserved
    assert snap_p2.authorization.buy_allowed is False
    assert snap_p2.authorization.short_allowed is True
    assert snap_p2.authorization.sell_allowed is True
    assert snap_p2.authorization.cover_allowed is True
    assert snap_p2.authorization.buy_permission is PermissionLevel.BLOCKED


def test_transition_into_crash_direct_flash_crash() -> None:
    monitor = MarketStatusMonitor()
    seed_flat_series(monitor, count=30, price="100.0")

    # Direct 5.5% drop from NORMAL (exceeds -5.0% flash crash threshold)
    for s in monitor.config.symbols.symbols:
        monitor.add_bar(make_bar(s, 30, "100.0", "100.0", "94.5", "94.5"))

    snapshot = monitor.evaluate()
    # Immediate transition to CRASH from NORMAL without waiting for intermediate WARNING
    assert snapshot.state is MarketState.CRASH
    assert snapshot.previous_state is MarketState.NORMAL
    assert monitor.transitions[0].reason == "FLASH_CRASH_ACUTE_DRAWDOWN"


# ============================================================================
# 4. Stabilization After Crash
# ============================================================================


def test_stabilization_after_crash() -> None:
    monitor = MarketStatusMonitor()
    seed_flat_series(monitor, count=30, price="100.0")

    # Drive into CRASH at index 30 with 95.0 (-5.0% drop)
    for s in monitor.config.symbols.symbols:
        monitor.add_bar(make_bar(s, 30, "100.0", "100.0", "94.5", "94.5"))
    monitor.evaluate()
    assert monitor.current_state is MarketState.CRASH

    # Remain in crash for minimum 5 bars (bars 31..35), hitting a trough at 94.0 (-6.0%)
    for i in range(31, 36):
        for s in monitor.config.symbols.symbols:
            monitor.add_bar(make_bar(s, i, "94.5", "94.5", "94.0", "94.0"))
        monitor.evaluate()
    assert monitor.current_state is MarketState.CRASH
    assert monitor.bars_in_state >= 5

    # Rebound from trough 94.0 to 95.5 (+1.5% off trough, exceeds +0.5% threshold)
    # and hold for stabilizing_confirmation_bars (3 bars: 36, 37, 38)
    for i in (36, 37, 38):
        for s in monitor.config.symbols.symbols:
            monitor.add_bar(make_bar(s, i, "95.0", "95.6", "95.0", "95.5"))
        monitor.evaluate()

    assert monitor.current_state is MarketState.STABILIZING
    snap = monitor.evaluate()
    # In STABILIZING: BUY blocked, new SHORT restricted, exits preserved
    assert snap.authorization.buy_allowed is False
    assert snap.authorization.short_allowed is False
    assert snap.authorization.sell_allowed is True
    assert snap.authorization.cover_allowed is True


# ============================================================================
# 5. Recovery from Stabilizing
# ============================================================================


def test_recovery_from_stabilizing_to_normal() -> None:
    monitor = MarketStatusMonitor()
    seed_flat_series(monitor, count=30, price="100.0")

    # Trigger flash crash into CRASH
    for s in monitor.config.symbols.symbols:
        monitor.add_bar(make_bar(s, 30, "100.0", "100.0", "94.0", "94.0"))
    monitor.evaluate()

    # Hold in CRASH and establish trough
    for i in range(31, 36):
        for s in monitor.config.symbols.symbols:
            monitor.add_bar(make_bar(s, i, "94.0", "94.0", "94.0", "94.0"))
        monitor.evaluate()

    # Rebound into STABILIZING
    for i in range(36, 40):
        for s in monitor.config.symbols.symbols:
            monitor.add_bar(make_bar(s, i, "95.0", "95.5", "95.0", "95.5"))
        monitor.evaluate()
    assert monitor.current_state is MarketState.STABILIZING

    # Spend min 5 bars in STABILIZING (40..45)
    for i in range(40, 46):
        for s in monitor.config.symbols.symbols:
            monitor.add_bar(make_bar(s, i, "95.5", "96.0", "95.5", "96.0"))
        monitor.evaluate()

    # Push to 99.0 (-1.0% drawdown, better than -1.2% recovery threshold)
    # with positive momentum for 3 confirmation bars
    for i in (46, 47, 48):
        for s in monitor.config.symbols.symbols:
            monitor.add_bar(make_bar(s, i, "98.5", "99.2", "98.5", "99.0"))
        monitor.evaluate()

    assert monitor.current_state is MarketState.RECOVERY
    snap_rec = monitor.evaluate()
    # In RECOVERY: BUY conditionally allowed, new SHORT restricted, exits preserved
    assert snap_rec.authorization.buy_allowed is True
    assert snap_rec.authorization.short_allowed is False
    assert snap_rec.authorization.sell_allowed is True
    assert snap_rec.authorization.cover_allowed is True

    # Continue recovery to 99.8 (-0.2% drawdown, above -0.5% normal threshold)
    # Allow volatility window (20 bars) to settle below warning threshold
    for i in range(49, 70):
        for s in monitor.config.symbols.symbols:
            monitor.add_bar(make_bar(s, i, "99.8", "99.8", "99.8", "99.8"))
        monitor.evaluate()

    assert monitor.current_state is MarketState.NORMAL
    snap_norm = monitor.evaluate()
    assert snap_norm.authorization.buy_allowed is True
    assert snap_norm.authorization.short_allowed is True


# ============================================================================
# 6. New Crash During Recovery and Stabilizing
# ============================================================================


def test_relapse_into_crash_from_stabilizing() -> None:
    monitor = MarketStatusMonitor()
    seed_flat_series(monitor, count=30, price="100.0")

    # Flash crash into CRASH
    for s in monitor.config.symbols.symbols:
        monitor.add_bar(make_bar(s, 30, "100.0", "100.0", "95.0", "95.0"))
    monitor.evaluate()
    for i in range(31, 36):
        for s in monitor.config.symbols.symbols:
            monitor.add_bar(make_bar(s, i, "95.0", "95.0", "95.0", "95.0"))
        monitor.evaluate()

    # Transition into STABILIZING
    for i in range(36, 40):
        for s in monitor.config.symbols.symbols:
            monitor.add_bar(make_bar(s, i, "95.0", "96.0", "95.0", "96.0"))
        monitor.evaluate()
    assert monitor.current_state is MarketState.STABILIZING

    # Sudden secondary breakdown breaching trough: price falls to 93.0
    for s in monitor.config.symbols.symbols:
        monitor.add_bar(make_bar(s, 40, "96.0", "96.0", "93.0", "93.0"))
    monitor.evaluate()

    # Prompt relapse back to CRASH
    assert monitor.current_state is MarketState.CRASH
    assert monitor.transitions[-1].from_state is MarketState.STABILIZING
    assert monitor.transitions[-1].to_state is MarketState.CRASH


def test_relapse_into_crash_from_recovery() -> None:
    # Setup monitor in RECOVERY
    config = MarketStatusConfig(
        hysteresis=HysteresisConfig(
            stabilizing_min_bars_in_crash=1,
            recovery_min_bars_in_stabilizing=1,
            stabilizing_confirmation_bars=1,
            recovery_confirmation_bars=1,
            stabilizing_no_new_lows_bars=1,
        )
    )
    monitor = MarketStatusMonitor(config=config)
    seed_flat_series(monitor, count=30, price="100.0")

    # Crash
    for s in monitor.config.symbols.symbols:
        monitor.add_bar(make_bar(s, 30, "100.0", "100.0", "94.0", "94.0"))
    monitor.evaluate()
    # Stabilize
    for s in monitor.config.symbols.symbols:
        monitor.add_bar(make_bar(s, 31, "94.0", "95.5", "94.0", "95.5"))
    monitor.evaluate()
    # Recover (price 99.6 is above 20-bar SMA, giving positive momentum)
    for s in monitor.config.symbols.symbols:
        monitor.add_bar(make_bar(s, 32, "95.5", "99.8", "95.5", "99.6"))
    monitor.evaluate()
    assert monitor.current_state is MarketState.RECOVERY

    # New severe collapse while in recovery: price drops to 95.0 (-5.0%)
    for s in monitor.config.symbols.symbols:
        monitor.add_bar(make_bar(s, 33, "99.6", "99.6", "95.0", "95.0"))
    monitor.evaluate()

    # Relapses immediately to CRASH
    assert monitor.current_state is MarketState.CRASH
    assert monitor.transitions[-1].from_state is MarketState.RECOVERY
    assert monitor.transitions[-1].to_state is MarketState.CRASH


# ============================================================================
# 7. Hysteresis (Averts Rapid Flip-Flopping)
# ============================================================================


def test_hysteresis_prevents_rapid_oscillation() -> None:
    monitor = MarketStatusMonitor()
    seed_flat_series(monitor, count=30, price="100.0")

    # Bar 30: single tick dips to 98.2 (-1.8%, below -1.5% warning threshold)
    for s in monitor.config.symbols.symbols:
        monitor.add_bar(make_bar(s, 30, "100.0", "100.0", "98.2", "98.2"))
    snap1 = monitor.evaluate()
    assert snap1.state is MarketState.NORMAL  # Stays NORMAL, 1 bar is not confirmed

    # Bar 31: bounces back to 99.5 (-0.5%)
    for s in monitor.config.symbols.symbols:
        monitor.add_bar(make_bar(s, 31, "98.2", "99.5", "98.2", "99.5"))
    snap2 = monitor.evaluate()
    assert snap2.state is MarketState.NORMAL  # Candidate counter reset

    # Now confirm into WARNING (2 bars at 98.0)
    for i in (32, 33):
        for s in monitor.config.symbols.symbols:
            monitor.add_bar(make_bar(s, i, "98.0", "98.0", "98.0", "98.0"))
        monitor.evaluate()
    assert monitor.current_state is MarketState.WARNING

    # In WARNING: price recovers slightly to 98.8 (-1.2% drawdown)
    # Notice: -1.2% is above the -1.5% warning entry threshold, but BELOW the -0.8% exit threshold
    for s in monitor.config.symbols.symbols:
        monitor.add_bar(make_bar(s, 34, "98.0", "98.8", "98.0", "98.8"))
    snap_hys = monitor.evaluate()
    # Hysteresis keeps state in WARNING!
    assert snap_hys.state is MarketState.WARNING


# ============================================================================
# 8. Missing and Stale Reference Observations
# ============================================================================


def test_missing_reference_symbols_fails_closed() -> None:
    monitor = MarketStatusMonitor()
    # Feed SPY only (need at least 2 reference symbols)
    for i in range(10):
        monitor.add_bar(make_bar("SPY", i, "100.0", "100.0", "100.0", "100.0"))

    snap = monitor.evaluate()
    assert snap.data_available is False
    assert snap.is_stale is False
    assert any("INSUFFICIENT_REFERENCE_SYMBOLS" in r for r in snap.reasons)

    # Fail closed for entries:
    assert snap.authorization.buy_allowed is False
    assert snap.authorization.short_allowed is False
    assert snap.authorization.buy_permission is PermissionLevel.BLOCKED
    assert snap.authorization.short_permission is PermissionLevel.BLOCKED

    # Exits and protective stops are UNCONDITIONALLY ALLOWED:
    assert snap.authorization.sell_allowed is True
    assert snap.authorization.cover_allowed is True
    assert snap.authorization.protective_stops_allowed is True
    assert snap.authorization.emergency_liquidation_allowed is True


def test_stale_reference_observations_fails_closed() -> None:
    monitor = MarketStatusMonitor()
    seed_flat_series(monitor, count=30, price="100.0")

    # Evaluate 10 minutes into the future (max_observation_age_seconds is 300s = 5m)
    future_time = START_TIME + timedelta(minutes=45)
    snap = monitor.evaluate(as_of=future_time)

    assert snap.data_available is True
    assert snap.is_stale is True
    assert any("STALE_REFERENCE_DATA" in r for r in snap.reasons)

    # Fail closed for entries:
    assert snap.authorization.buy_allowed is False
    assert snap.authorization.short_allowed is False

    # Exits and protective stops are UNCONDITIONALLY ALLOWED:
    assert snap.authorization.sell_allowed is True
    assert snap.authorization.cover_allowed is True
    assert snap.authorization.protective_stops_allowed is True
    assert snap.authorization.emergency_liquidation_allowed is True


# ============================================================================
# 9. Restart Persistence
# ============================================================================


def test_restart_persistence_in_memory_and_json(tmp_path: Path) -> None:
    # 1. In-memory repository persistence
    repo_mem = InMemoryMarketStatusRepository()
    monitor1 = MarketStatusMonitor()
    seed_flat_series(monitor1, count=30, price="100.0")

    # Drive monitor1 into WARNING
    for i in (30, 31):
        for s in monitor1.config.symbols.symbols:
            monitor1.add_bar(make_bar(s, i, "98.0", "98.0", "98.0", "98.0"))
        monitor1.evaluate()
    assert monitor1.current_state is MarketState.WARNING

    # Save to repo
    state_doc = monitor1.snapshot()
    repo_mem.save("test_scope", state_doc)

    # Restore in fresh monitor instance
    monitor2 = MarketStatusMonitor()
    loaded_doc = repo_mem.load("test_scope")
    assert loaded_doc is not None
    monitor2.restore(loaded_doc)

    assert monitor2.current_state is MarketState.WARNING
    assert monitor2.previous_state is MarketState.NORMAL
    assert len(monitor2.transitions) == 1

    # 2. Atomic JSON file repository persistence
    repo_json = JsonFileMarketStatusRepository(tmp_path)
    repo_json.save("live_scope", state_doc)

    loaded_json = repo_json.load("live_scope")
    assert loaded_json is not None
    monitor3 = MarketStatusMonitor()
    monitor3.restore(loaded_json)
    assert monitor3.current_state is MarketState.WARNING


# ============================================================================
# 10. Exit Authorization Invariants in Every State & EntryGate Integration
# ============================================================================


def test_exit_authorization_guaranteed_in_every_state() -> None:
    """Mathematical guarantee: exits, stops, and liquidations NEVER blocked."""
    policy = TradingAuthorizationPolicy()
    eval_time = datetime.now(UTC)

    # Test all 5 states + data unavailable + stale
    test_cases = [
        (MarketState.NORMAL, True, False),
        (MarketState.WARNING, True, False),
        (MarketState.CRASH, True, False),
        (MarketState.STABILIZING, True, False),
        (MarketState.RECOVERY, True, False),
        (MarketState.NORMAL, False, False),  # data unavailable
        (MarketState.NORMAL, True, True),   # stale data
    ]

    for state, data_avail, stale in test_cases:
        decision = policy.evaluate(state, eval_time, data_available=data_avail, is_stale=stale)
        # INVARIANTS:
        assert decision.sell_allowed is True
        assert decision.cover_allowed is True
        assert decision.protective_stops_allowed is True
        assert decision.emergency_liquidation_allowed is True

        assert decision.is_action_allowed(ActionKind.SELL) is True
        assert decision.is_action_allowed(ActionKind.COVER) is True
        assert decision.is_action_allowed(ActionKind.PROTECTIVE_STOP) is True
        assert decision.is_action_allowed(ActionKind.EMERGENCY_LIQUIDATION) is True


@pytest.mark.asyncio
async def test_market_status_entry_gate_adapter() -> None:
    """Verify clean integration with the portfolio EntryGate protocol."""
    monitor = MarketStatusMonitor()
    seed_flat_series(monitor, count=30, price="100.0")

    gate = MarketStatusEntryGate(monitor)

    prov = SignalProvenance(
        data_provider="alpaca",
        data_feed="sip",
        is_consolidated=True,
        timeframe="1m",
        bar_time=START_TIME,
        indicator_snapshot={},
    )
    buy_signal = Signal(
        signal_id=make_signal_id("inst1", "SPY", SignalAction.ENTER_LONG, START_TIME),
        strategy_key="test_strat",
        strategy_version="1.0.0",
        instance_id="inst1",
        symbol="SPY",
        action=SignalAction.ENTER_LONG,
        bar_time=START_TIME,
        generated_at=START_TIME,
        reference_price=D("100.0"),
        reason="test_buy",
        provenance=prov,
    )
    buy_req = EntryRequest("acct1", "cycle1", buy_signal)
    short_req = SimpleNamespace(
        account_scope_id="acct1",
        cycle_id="cycle2",
        signal=SimpleNamespace(action=SignalAction.ENTER_SHORT, symbol="SPY"),
    )

    # 1. Under NORMAL: both BUY and SHORT allowed
    res_buy = await gate.evaluate(buy_req)
    assert res_buy.allowed is True

    res_short = await gate.evaluate(short_req)
    assert res_short.allowed is True

    # 2. Drive monitor into CRASH
    for s in monitor.config.symbols.symbols:
        monitor.add_bar(make_bar(s, 30, "100.0", "100.0", "94.0", "94.0"))
    monitor.evaluate()
    assert monitor.current_state is MarketState.CRASH

    # Under CRASH: BUY blocked, SHORT allowed
    res_crash_buy = await gate.evaluate(buy_req)
    assert res_crash_buy.allowed is False
    assert "MARKET_CRASH_ACUTE_RISK" in res_crash_buy.reasons

    res_crash_short = await gate.evaluate(short_req)
    assert res_crash_short.allowed is True
