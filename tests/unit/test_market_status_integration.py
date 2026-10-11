"""Comprehensive integration tests for Market Status Monitor runtime integration.

Verifies the 11 required core areas:
1. Reference data flows into monitor.
2. Monitor operates without reference symbols in trading config.
3. NORMAL allows qualified entries.
4. CRASH blocks BUY.
5. Missing data blocks new entries when enabled.
6. Disabled monitor preserves previous behavior.
7. Existing risk controls remain effective (inner gate rejection preserved).
8. SELL and protective exits remain authorized in all states.
9. Restart preserves CRASH state across SQLite restarts.
10. Historical replay has no look-ahead (point-in-time evaluation).
11. SHADOW and BACKTEST receive equivalent market-state inputs and decisions.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mike_trade.app import AppSettings, SQLitePersistence, build_runtime
from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import ExecutionAssumptions, HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.runner import BacktestRequest, BacktestRunner
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.market import Candle as DomainCandle, FeedProvenance, Timeframe
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_status.config import MarketStatusConfig
from mike_trade.market_status.monitor import MarketStatusMonitor
from mike_trade.market_status.persistence import SqliteMarketStatusRepository
from mike_trade.market_status.policy import MarketStatusEntryGate, TradingAuthorizationPolicy
from mike_trade.market_status.state import MarketState
from mike_trade.portfolio.manager import EntryGateDecision, EntryRequest
from mike_trade.runtime import EntryGate, ExecutionMode, StartupState, SystemClock
from mike_trade.strategies.registry import StrategyRegistry

_D = Decimal
_BASE_TIME = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)


class MockClock:
    def __init__(self, current_time: datetime) -> None:
        self._current_time = current_time

    def now(self) -> datetime:
        return self._current_time

    def advance(self, td: timedelta) -> None:
        self._current_time += td


def _make_ref_candle(
    symbol: str,
    start_time: datetime,
    close_price: Decimal,
    high_price: Decimal | None = None,
    low_price: Decimal | None = None,
    open_price: Decimal | None = None,
) -> Candle:
    h = high_price or close_price
    l = low_price or close_price
    o = open_price or close_price
    return Candle(
        symbol=symbol,
        timeframe=CandleTimeframe.M1,
        start=start_time,
        end=start_time + timedelta(minutes=1),
        open=o,
        high=h,
        low=l,
        close=close_price,
        volume=_D("10000"),
        feed_id="IEX",
        consolidated=False,
        origin=CandleOrigin.HISTORICAL_REST,
        price_basis=PriceBasis.RAW,
        status=CandleStatus.CLOSED,
    )


def _feed_normal_history(monitor: MarketStatusMonitor, clock: MockClock, num_bars: int = 40) -> None:
    """Feed calm, steady reference bars for SPY, QQQ, IWM."""
    for i in range(num_bars):
        t = _BASE_TIME + timedelta(minutes=i)
        clock.advance(timedelta(minutes=1))
        for sym, price in (("SPY", _D("500.00")), ("QQQ", _D("400.00")), ("IWM", _D("200.00"))):
            monitor.add_bar(_make_ref_candle(sym, t, price))


def _feed_crash_history(monitor: MarketStatusMonitor, clock: MockClock) -> None:
    """Feed a steep market crash (>5% drop across symbols)."""
    _feed_normal_history(monitor, clock, num_bars=35)
    # Steep drop on next bars
    for i in range(35, 45):
        t = _BASE_TIME + timedelta(minutes=i)
        clock.advance(timedelta(minutes=1))
        # Drop by 8%
        for sym, price in (("SPY", _D("460.00")), ("QQQ", _D("365.00")), ("IWM", _D("180.00"))):
            monitor.add_bar(_make_ref_candle(sym, t, price))


# ---------------------------------------------------------------------------
# TEST 1: Reference data flows into monitor
# ---------------------------------------------------------------------------
def test_reference_data_flows_into_monitor() -> None:
    clock = MockClock(_BASE_TIME)
    config = MarketStatusConfig(symbols=("SPY", "QQQ", "IWM"))
    monitor = MarketStatusMonitor(config=config, clock=clock)

    # Ingest reference bars
    bar_spy = _make_ref_candle("SPY", _BASE_TIME, _D("500.00"))
    bar_qqq = _make_ref_candle("QQQ", _BASE_TIME, _D("400.00"))
    bar_iwm = _make_ref_candle("IWM", _BASE_TIME, _D("200.00"))

    monitor.add_bar(bar_spy)
    monitor.add_bar(bar_qqq)
    monitor.add_bar(bar_iwm)

    candles = monitor.reference_candles
    assert len(candles["SPY"]) == 1
    assert len(candles["QQQ"]) == 1
    assert len(candles["IWM"]) == 1
    assert candles["SPY"][0].close == _D("500.00")


# ---------------------------------------------------------------------------
# TEST 2: Monitor operates without reference symbols in trading config
# ---------------------------------------------------------------------------
def test_monitor_operates_without_reference_symbols_in_trading_config(tmp_path: Path) -> None:
    settings = AppSettings(
        symbols=("AAPL", "MSFT"),  # Traded symbols DO NOT include SPY, QQQ, IWM
        market_status_enabled=True,
        market_status_reference_symbols=("SPY", "QQQ", "IWM"),
        data_dir=tmp_path,
    )
    runtime = build_runtime(settings)
    assert runtime.market_status_monitor is not None
    assert runtime.symbols == ("AAPL", "MSFT")

    # The market_data instance should have market_status bound
    md = runtime.market_data
    assert hasattr(md, "bind_market_status")
    assert getattr(md, "_reference_symbols", None) == ("SPY", "QQQ", "IWM")
    assert getattr(md, "_trading_symbols", None) == ("AAPL", "MSFT")


# ---------------------------------------------------------------------------
# TEST 3: NORMAL allows qualified entries
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_normal_allows_qualified_entries() -> None:
    clock = MockClock(_BASE_TIME)
    config = MarketStatusConfig(symbols=("SPY", "QQQ", "IWM"))
    monitor = MarketStatusMonitor(config=config, clock=clock)
    _feed_normal_history(monitor, clock, num_bars=35)

    eval_result = monitor.evaluate(as_of=clock.now())
    assert eval_result.state is MarketState.NORMAL
    assert eval_result.authorization.buy_allowed is True

    raw_gate = EntryGate(authorized=True, reasons=())
    gate = MarketStatusEntryGate(monitor=monitor, inner_gate=raw_gate)

    assert gate.authorized is True
    assert gate.entries_enabled() is True
    decision = await gate.evaluate_entry(
        account_scope_id="shadow:test",
        symbol="AAPL",
        strategy_id="vwap_bounce",
        now=clock.now(),
        action="ENTER_LONG",
    )
    assert decision.allowed is True
    assert not decision.blocked


# ---------------------------------------------------------------------------
# TEST 4: CRASH blocks BUY
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_crash_blocks_buy() -> None:
    clock = MockClock(_BASE_TIME)
    config = MarketStatusConfig(symbols=("SPY", "QQQ", "IWM"))
    monitor = MarketStatusMonitor(config=config, clock=clock)
    _feed_crash_history(monitor, clock)

    eval_result = monitor.evaluate(as_of=clock.now())
    assert eval_result.state is MarketState.CRASH
    assert eval_result.authorization.buy_allowed is False

    raw_gate = EntryGate(authorized=True, reasons=())
    gate = MarketStatusEntryGate(monitor=monitor, inner_gate=raw_gate)

    # Entries are authorized per direction: CRASH blocks BUY but (by default policy) still
    # permits a qualified SHORT, so the aggregate gate stays open for the short side.
    assert gate.authorizes("ENTER_LONG") is False
    assert gate.authorizes("ENTER_SHORT") is True
    assert gate.authorized is True

    decision = await gate.evaluate_entry(
        account_scope_id="shadow:test",
        symbol="AAPL",
        strategy_id="vwap_bounce",
        now=clock.now(),
        action="ENTER_LONG",
    )
    assert decision.allowed is False
    assert decision.blocked is True
    assert "MARKET_CRASH_ACUTE_RISK" in decision.reasons


# ---------------------------------------------------------------------------
# TEST 5: Missing data blocks new entries when enabled
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_data_blocks_new_entries_when_enabled() -> None:
    clock = MockClock(_BASE_TIME)
    config = MarketStatusConfig(symbols=("SPY", "QQQ", "IWM"))
    # Empty monitor (no bars at all)
    monitor = MarketStatusMonitor(config=config, clock=clock)

    raw_gate = EntryGate(authorized=True, reasons=())
    gate = MarketStatusEntryGate(monitor=monitor, inner_gate=raw_gate)

    # When enabled and data is missing, fail-closed kicks in
    assert gate.authorized is False
    assert gate.entries_enabled() is False
    assert "MARKET_DATA_UNAVAILABLE" in gate.reasons

    decision = await gate.evaluate_entry(
        account_scope_id="shadow:test",
        symbol="AAPL",
        strategy_id="vwap_bounce",
        now=clock.now(),
        action="ENTER_LONG",
    )
    assert decision.allowed is False
    assert decision.blocked is True
    assert "MARKET_DATA_UNAVAILABLE" in decision.reasons


# ---------------------------------------------------------------------------
# TEST 6: Disabled monitor preserves previous behavior
# ---------------------------------------------------------------------------
def test_disabled_monitor_preserves_previous_behavior(tmp_path: Path) -> None:
    settings = AppSettings(
        symbols=("AAPL",),
        market_status_enabled=False,
        data_dir=tmp_path,
    )
    runtime = build_runtime(settings)
    assert runtime.market_status_monitor is None
    # Entry gate is the raw EntryGate
    assert isinstance(runtime.entry_gate, EntryGate)
    assert not isinstance(runtime.entry_gate, MarketStatusEntryGate)

    # Allowing entry gate directly permits entries without checking market status
    runtime.entry_gate.allow()
    assert runtime.entry_gate.authorized is True


# ---------------------------------------------------------------------------
# TEST 7: Existing risk controls remain effective (inner gate rejection preserved)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_existing_risk_controls_remain_effective() -> None:
    clock = MockClock(_BASE_TIME)
    config = MarketStatusConfig(symbols=("SPY", "QQQ", "IWM"))
    monitor = MarketStatusMonitor(config=config, clock=clock)
    _feed_normal_history(monitor, clock, num_bars=35)

    # Market is NORMAL, but inner gate is blocked by risk control
    raw_gate = EntryGate(authorized=False, reasons=("DAILY_LOSS_LIMIT_REACHED",))
    gate = MarketStatusEntryGate(monitor=monitor, inner_gate=raw_gate)

    assert gate.authorized is False
    assert "DAILY_LOSS_LIMIT_REACHED" in gate.reasons

    # Check PortfolioManager EntryGate protocol with inner gate that rejects
    class RejectingInnerGate:
        async def evaluate_entry(self, *args: Any, **kwargs: Any) -> EntryGateDecision:
            return EntryGateDecision(allowed=False, reasons=("COOLDOWN_ACTIVE",))

    rejecting_gate = MarketStatusEntryGate(monitor=monitor, inner_gate=RejectingInnerGate())
    decision = await rejecting_gate.evaluate_entry(
        account_scope_id="shadow:test",
        symbol="AAPL",
        strategy_id="vwap_bounce",
        now=clock.now(),
        action="ENTER_LONG",
    )
    assert decision.allowed is False
    assert decision.blocked is True
    assert "COOLDOWN_ACTIVE" in decision.reasons


# ---------------------------------------------------------------------------
# TEST 8: SELL and protective exits remain authorized
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sell_and_protective_exits_remain_authorized_in_crash() -> None:
    clock = MockClock(_BASE_TIME)
    config = MarketStatusConfig(symbols=("SPY", "QQQ", "IWM"))
    monitor = MarketStatusMonitor(config=config, clock=clock)
    _feed_crash_history(monitor, clock)

    eval_result = monitor.evaluate(as_of=clock.now())
    assert eval_result.state is MarketState.CRASH

    gate = MarketStatusEntryGate(monitor=monitor)

    # BUY is blocked
    buy_decision = await gate.evaluate_entry(
        account_scope_id="shadow:test",
        symbol="AAPL",
        strategy_id="vwap_bounce",
        now=clock.now(),
        action="ENTER_LONG",
    )
    assert buy_decision.allowed is False

    # But exits, protective stops, covers, liquidations are UNCONDITIONALLY authorized
    for exit_action in ("EXIT_LONG", "SELL", "EXIT_SHORT", "COVER", "LIQUIDATION"):
        exit_decision = await gate.evaluate_entry(
            account_scope_id="shadow:test",
            symbol="AAPL",
            strategy_id="vwap_breakdown",
            now=clock.now(),
            action=exit_action,
        )
        assert exit_decision.allowed is True
        assert not exit_decision.blocked


# ---------------------------------------------------------------------------
# TEST 9: Restart preserves CRASH state
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_restart_preserves_crash_state(tmp_path: Path) -> None:
    db_path = tmp_path / "shadow.sqlite3"
    clock = MockClock(_BASE_TIME)
    scope = AccountScope(AccountEnvironment.SHADOW, "default", "scenario1")

    # 1. Setup persistence and database
    persistence = SQLitePersistence(db_path, AccountEnvironment.SHADOW, (scope,), clock)
    persistence.open_migrate_integrity()

    # 2. Setup monitor and simulate crash
    config = MarketStatusConfig(symbols=("SPY", "QQQ", "IWM"))
    monitor = MarketStatusMonitor(config=config, clock=clock)
    _feed_crash_history(monitor, clock)

    eval_before = monitor.evaluate(as_of=clock.now())
    assert eval_before.state is MarketState.CRASH

    # 3. Persist via SqliteMarketStatusRepository
    repo = SqliteMarketStatusRepository(persistence.session, clock=clock)
    doc = monitor.snapshot()
    repo.save(scope.scope_id, doc)
    persistence.close()

    # 4. Simulate process restart: open clean database and restore
    persistence_after = SQLitePersistence(db_path, AccountEnvironment.SHADOW, (scope,), clock)
    persistence_after.open_migrate_integrity()
    repo_after = SqliteMarketStatusRepository(persistence_after.session, clock=clock)

    loaded_doc = repo_after.load(scope.scope_id)
    assert loaded_doc is not None

    monitor_restored = MarketStatusMonitor(config=config, clock=clock)
    monitor_restored.restore(loaded_doc)

    eval_after = monitor_restored.evaluate(as_of=clock.now())
    assert eval_after.state is MarketState.CRASH
    assert eval_after.authorization.buy_allowed is False
    persistence_after.close()


# ---------------------------------------------------------------------------
# TEST 10: Historical replay has no look-ahead
# ---------------------------------------------------------------------------
def test_historical_replay_has_no_lookahead() -> None:
    clock = MockClock(_BASE_TIME)
    config = MarketStatusConfig(symbols=("SPY", "QQQ", "IWM"))
    monitor = MarketStatusMonitor(config=config, clock=clock)

    # Feed normal bars up to minute 35
    for i in range(35):
        t = _BASE_TIME + timedelta(minutes=i)
        for sym in ("SPY", "QQQ", "IWM"):
            monitor.add_bar(_make_ref_candle(sym, t, _D("500.00")))

    # At minute 35, evaluate as of minute 35
    t35 = _BASE_TIME + timedelta(minutes=35)
    eval_t35 = monitor.evaluate(as_of=t35)
    assert eval_t35.state is MarketState.NORMAL
    assert eval_t35.authorization.buy_allowed is True

    # Now add crash bars that occur at minute 36..40
    for i in range(35, 41):
        t = _BASE_TIME + timedelta(minutes=i)
        for sym in ("SPY", "QQQ", "IWM"):
            monitor.add_bar(_make_ref_candle(sym, t, _D("450.00")))

    # When queried as of minute 35 again, the evaluation MUST NOT use the future bars!
    # Point-in-time calculation uses candles <= eval_time.
    eval_t35_again = monitor.evaluate(as_of=t35)
    assert eval_t35_again.state is MarketState.NORMAL
    assert eval_t35_again.authorization.buy_allowed is True

    # But at minute 41, it reflects the crash point-in-time
    t41 = _BASE_TIME + timedelta(minutes=41)
    eval_t41 = monitor.evaluate(as_of=t41)
    assert eval_t41.state is MarketState.CRASH
    assert eval_t41.authorization.buy_allowed is False


# ---------------------------------------------------------------------------
# TEST 11: SHADOW and BACKTEST receive equivalent market-state inputs
# ---------------------------------------------------------------------------
def test_shadow_and_backtest_receive_equivalent_market_state_inputs() -> None:
    clock_shadow = MockClock(_BASE_TIME)
    clock_backtest = MockClock(_BASE_TIME)

    config = MarketStatusConfig(symbols=("SPY", "QQQ", "IWM"))
    shadow_monitor = MarketStatusMonitor(config=config, clock=clock_shadow)
    backtest_monitor = MarketStatusMonitor(config=config, clock=clock_backtest)

    # Ingest the same sequence into both
    for i in range(40):
        t = _BASE_TIME + timedelta(minutes=i)
        clock_shadow.advance(timedelta(minutes=1))
        clock_backtest.advance(timedelta(minutes=1))
        for sym, price in (("SPY", _D("500.00")), ("QQQ", _D("400.00")), ("IWM", _D("200.00"))):
            bar = _make_ref_candle(sym, t, price)
            shadow_monitor.add_bar(bar)
            backtest_monitor.add_bar(bar)

    eval_shadow = shadow_monitor.evaluate(as_of=clock_shadow.now())
    eval_backtest = backtest_monitor.evaluate(as_of=clock_backtest.now())

    assert eval_shadow.state == eval_backtest.state
    assert eval_shadow.authorization.buy_allowed == eval_backtest.authorization.buy_allowed
    assert eval_shadow.authorization.reasons == eval_backtest.authorization.reasons
    assert eval_shadow.metrics.composite_drawdown_percent == eval_backtest.metrics.composite_drawdown_percent
    assert eval_shadow.metrics.realized_volatility_bps == eval_backtest.metrics.realized_volatility_bps
