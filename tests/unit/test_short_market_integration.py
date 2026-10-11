"""Market-status monitor x bidirectional (BUY/SELL/SHORT/COVER) engine.

The monitor gates *new entries* of both directions and nothing else: SELL, COVER and
protective stops stay available in every market state, and the gate is additional to the
existing budget, margin, authorization and exposure restrictions.

SHADOW tests drive the real runtime graph (simulated broker, temporary SQLite) with scripted
strategy doubles and a real ``MarketStatusMonitor`` whose state machine is frozen so a test
can pin the market state; BACKTEST tests drive the real ``BacktestRunner`` with a real,
unfrozen monitor fed by synthetic SPY/QQQ/IWM candles.  No network, broker or stored data.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from scripted_strategies import build_bidirectional_registry
from test_short_cover_engine import (
    FEED,
    MIN,
    OPEN,
    SHORT_ON,
    events,
    flat,
    run,
    sides,
)
from test_short_cover_shadow import fills_of, orders, symbol
from test_strategy_collections_runtime import SCENARIO, Clock, ctx

from mike_trade.app import AppSettings, ShadowScenarioSettings, build_runtime
from mike_trade.backtest.feed import ReplayEvent
from mike_trade.domain.market import Candle as DomainCandle
from mike_trade.domain.market import Timeframe
from mike_trade.domain.trading import OrderPurpose, OrderSide, PositionDirection
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_status import (
    MarketState,
    MarketStatusEntryGate,
    MarketStatusMonitor,
)
from mike_trade.runtime import EntryGate, ExecutionMode

REFERENCE = ("SPY", "QQQ", "IWM")


class FrozenMonitor(MarketStatusMonitor):
    """Real monitor (data quality, authorization, snapshot/restore) with the state pinned."""

    def _process_transitions(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        return None

    def pin(self, state: MarketState) -> None:
        self._current_state = state


def feed(monitor: MarketStatusMonitor, clock: Clock, price: str = "100") -> None:
    """One fresh completed reference minute ending at the shadow clock."""
    for name in REFERENCE:
        monitor.add_bar(Candle(
            name, CandleTimeframe.M1, clock.value - timedelta(minutes=1), clock.value,
            D(price), D(price), D(price), D(price), D("10000"), "iex", False,
            CandleOrigin.REPLAY, PriceBasis.RAW, CandleStatus.CLOSED))


async def start(tmp_path: Path, clock: Clock, settings: dict, monitor, *, enabled=True,
                name="t.sqlite3", cash="50000", short=True):
    scenario = ShadowScenarioSettings(
        name=SCENARIO, starting_cash=D(cash), per_symbol_budget=D("3000"),
        total_budget=D("20000"), max_entry_cycles_per_session=3, consecutive_loss_limit=3,
        post_sale_cooldown=timedelta(0), minimum_hold=timedelta(minutes=1),
        short_selling_enabled=short)
    overrides = {"clock": clock, "strategies": build_bidirectional_registry()}
    if monitor is not None:
        overrides["market_status_monitor"] = monitor
    runtime = build_runtime(
        AppSettings(mode=ExecutionMode.SHADOW, db_path=tmp_path / name,
                    shadow_scenarios=(scenario,), market_status_enabled=enabled, **settings),
        overrides=overrides)
    await runtime.start()
    return runtime, runtime.broker.scenarios[SCENARIO]  # type: ignore[attr-defined]


async def mstep(runtime, clock, monitor, version, *, bid="100", ask=None, seconds=60,
                fresh=True):
    """Advance one minute, deliver fresh reference bars (unless ``fresh`` is off), process."""
    if version > 1:
        clock.value += timedelta(seconds=seconds)
    if fresh:
        feed(monitor, clock)
    result = await runtime.lifecycle.process(
        {"SPY": ctx("SPY", clock.value, version, bid=bid, ask=ask)})
    return result[SCENARIO]


def held(shadow) -> PositionDirection | None:
    position = shadow.account.state.positions.get("SPY")
    return None if position is None else position.direction


def pinned(state: MarketState, clock: Clock) -> FrozenMonitor:
    monitor = FrozenMonitor(clock=clock, initial_state=state, initial_time=clock.value)
    feed(monitor, clock)
    return monitor


# --------------------------------------------------------------- SHADOW: entry authorization

ENTRY_MATRIX = [
    # state, BUY enters, SHORT enters  (documented default policy)
    (MarketState.NORMAL, True, True),
    (MarketState.WARNING, False, True),
    (MarketState.CRASH, False, True),
    (MarketState.STABILIZING, False, False),
    (MarketState.RECOVERY, True, False),
]


@pytest.mark.parametrize(("state", "buy_enters", "short_enters"), ENTRY_MATRIX)
async def test_shadow_market_state_gates_buy_and_short_entries_separately(
    tmp_path, state, buy_enters, short_enters
):
    for role, expected, direction in (
        ("buy", buy_enters, PositionDirection.LONG),
        ("short", short_enters, PositionDirection.SHORT),
    ):
        clock = Clock()
        monitor = pinned(state, clock)
        config = symbol(buys=[("buy_alpha", {"fire_at": 3})]) if role == "buy" else symbol(
            shorts=[("short_alpha", {"fire_at": 3})])
        runtime, shadow = await start(tmp_path, clock, config, monitor, name=f"{role}.sqlite3")
        try:
            for version in range(1, 6):
                await mstep(runtime, clock, monitor, version, bid="150", ask="150.1")
            assert held(shadow) == (direction if expected else None), (state, role)
            if not expected:
                assert shadow.account.state.orders == {}  # refused before any order exists
        finally:
            await runtime.stop()


async def test_a_restricted_buy_never_suppresses_an_authorized_short_in_arbitration(tmp_path):
    clock = Clock()
    monitor = pinned(MarketState.CRASH, clock)
    runtime, shadow = await start(tmp_path, clock, symbol(
        buys=[("buy_alpha", {"fire_at": 3})], shorts=[("short_alpha", {"fire_at": 3})]),
        monitor)
    try:
        for version in range(1, 6):
            await mstep(runtime, clock, monitor, version, bid="150", ask="150.1")
        assert held(shadow) is PositionDirection.SHORT  # alpha would win a BUY-first tie-break
        assert [f[0] for f in fills_of(shadow)] == [OrderSide.SELL_SHORT]
    finally:
        await runtime.stop()


async def test_missing_or_stale_reference_data_blocks_buy_and_short_entries(tmp_path):
    for role in ("buy", "short"):
        clock = Clock()
        # no reference candle at all: fail closed
        empty = FrozenMonitor(clock=clock, initial_time=clock.value)
        config = symbol(buys=[("buy_alpha", {"fire_at": 3})]) if role == "buy" else symbol(
            shorts=[("short_alpha", {"fire_at": 3})])
        runtime, shadow = await start(tmp_path, clock, config, empty, name=f"{role}.sqlite3")
        try:
            for version in range(1, 6):
                await mstep(runtime, clock, empty, version, fresh=False)
            assert shadow.account.state.orders == {}
            assert "MARKET_DATA_UNAVAILABLE" in empty.evaluate().authorization.reasons
        finally:
            await runtime.stop()
    # data that goes stale (feed stops for > max observation age) blocks as well
    clock = Clock()
    monitor = pinned(MarketState.NORMAL, clock)
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})]), monitor, name="stale.sqlite3")
    try:
        await mstep(runtime, clock, monitor, 1)
        await mstep(runtime, clock, monitor, 2, fresh=False, seconds=400)
        await mstep(runtime, clock, monitor, 3, fresh=False, seconds=400)
        assert shadow.account.state.orders == {}
        assert "MARKET_DATA_STALE" in monitor.evaluate().authorization.reasons
    finally:
        await runtime.stop()


async def test_existing_authorization_margin_and_budget_restrictions_still_apply(tmp_path):
    # shorting not authorized on the scenario: CRASH policy permission is not authorization
    clock = Clock()
    monitor = pinned(MarketState.CRASH, clock)
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})]), monitor, short=False)
    try:
        for version in range(1, 6):
            await mstep(runtime, clock, monitor, version, bid="150", ask="150.1")
        assert shadow.account.state.orders == {}
    finally:
        await runtime.stop()
    # own-cash margin cannot carry the short: refused even though the market permits it
    clock = Clock()
    monitor = pinned(MarketState.CRASH, clock)
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})]), monitor, cash="1000", name="poor.sqlite3")
    try:
        for version in range(1, 6):
            await mstep(runtime, clock, monitor, version, bid="150", ask="150.1")
        assert shadow.account.state.orders == {}
    finally:
        await runtime.stop()
    # runtime-level block (e.g. durable operator control) also stops a market-permitted entry
    clock = Clock()
    monitor = pinned(MarketState.NORMAL, clock)
    runtime, shadow = await start(tmp_path, clock, symbol(
        buys=[("buy_alpha", {"fire_at": 3})]), monitor, name="blocked.sqlite3")
    try:
        runtime.entry_gate.block("OPERATOR_DISABLED")
        for version in range(1, 6):
            await mstep(runtime, clock, monitor, version)
        assert shadow.account.state.orders == {}
    finally:
        await runtime.stop()


# ------------------------------------------------------------ SHADOW: exits are never gated


async def test_sell_and_cover_exits_remain_possible_during_crash(tmp_path):
    for entry_role, exit_role, entry_state, direction in (
        ("buys", "sells", MarketState.NORMAL, PositionDirection.LONG),
        ("shorts", "covers", MarketState.CRASH, PositionDirection.SHORT),
    ):
        clock = Clock()
        monitor = pinned(entry_state, clock)
        config = symbol(**{entry_role: [(f"{entry_role[:-1]}_alpha", {"fire_at": 3})],
                           exit_role: [(f"{exit_role[:-1]}_alpha", {"fire_at": 7})]})
        runtime, shadow = await start(tmp_path, clock, config, monitor,
                                      name=f"{entry_role}.sqlite3")
        try:
            for version in range(1, 5):
                await mstep(runtime, clock, monitor, version)
            assert held(shadow) is direction
            monitor.pin(MarketState.CRASH)  # the market crashes while the position is open
            for version in range(5, 9):
                await mstep(runtime, clock, monitor, version)
            assert held(shadow) is None
            assert len(fills_of(shadow)) == 2
        finally:
            await runtime.stop()


async def test_exits_work_even_when_market_data_is_stale(tmp_path):
    clock = Clock()
    monitor = pinned(MarketState.CRASH, clock)
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})], covers=[("cover_alpha", {"fire_at": 6})]),
        monitor)
    try:
        for version in range(1, 5):
            await mstep(runtime, clock, monitor, version)
        for version in range(5, 8):  # reference feed dies; entries would be blocked
            await mstep(runtime, clock, monitor, version, fresh=False, seconds=400)
        assert not monitor.evaluate().authorization.short_allowed
        assert held(shadow) is None  # but the COVER was still honoured
    finally:
        await runtime.stop()


async def test_long_and_short_protective_stops_trigger_during_crash(tmp_path):
    stop = {"type": "fixed", "stop_percent": D("1")}
    # long opened in NORMAL, then CRASH: the stop still fires (bid through 99)
    clock = Clock()
    monitor = pinned(MarketState.NORMAL, clock)
    runtime, shadow = await start(tmp_path, clock, symbol(
        buys=[("buy_alpha", {"fire_at": 3})], stop=stop), monitor)
    try:
        for version in range(1, 5):
            await mstep(runtime, clock, monitor, version, bid="100", ask="100.1")
        assert held(shadow) is PositionDirection.LONG
        monitor.pin(MarketState.CRASH)
        actions = await mstep(runtime, clock, monitor, 5, bid="98", ask="98.1")
        assert "STOP_EXIT" in actions and held(shadow) is None
    finally:
        await runtime.stop()
    # short opened (and protected) in CRASH: the stop above the entry fires on the ask
    clock = Clock()
    monitor = pinned(MarketState.CRASH, clock)
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})], stop=stop), monitor, name="short.sqlite3")
    try:
        for version in range(1, 5):
            await mstep(runtime, clock, monitor, version, bid="100", ask="100.1")
        assert held(shadow) is PositionDirection.SHORT
        (stop_order,) = orders(shadow, OrderPurpose.PROTECTIVE_STOP)
        assert stop_order.side is OrderSide.BUY_TO_COVER
        actions = await mstep(runtime, clock, monitor, 5, bid="101.5", ask="101.6")
        assert "STOP_EXIT" in actions and held(shadow) is None
    finally:
        await runtime.stop()


# --------------------------------------------------- SHADOW: full cycle, disabled, restart


async def test_a_full_short_to_cover_cycle_runs_with_monitoring_enabled(tmp_path):
    clock = Clock()
    monitor = pinned(MarketState.NORMAL, clock)
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})], covers=[("cover_alpha", {"fire_at": 6})]),
        monitor)
    try:
        assert isinstance(runtime.entry_gate, MarketStatusEntryGate)
        for version in range(1, 5):
            await mstep(runtime, clock, monitor, version, bid="150", ask="150.1")
        assert held(shadow) is PositionDirection.SHORT
        for version in range(5, 8):
            await mstep(runtime, clock, monitor, version, bid="144", ask="144.1")
        assert [f[0] for f in fills_of(shadow)] == [OrderSide.SELL_SHORT, OrderSide.BUY_TO_COVER]
        assert shadow.account.state.realized_pnl == D("112.1")
    finally:
        await runtime.stop()


async def test_disabled_monitoring_preserves_the_baseline_engine(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})], covers=[("cover_alpha", {"fire_at": 6})]),
        None, enabled=False)
    try:
        assert type(runtime.entry_gate) is EntryGate
        assert runtime.market_status_monitor is None
        for version in range(1, 8):  # no reference data anywhere, and still trading
            await mstep(runtime, clock, None, version, bid="150", ask="150.1", fresh=False)
        assert [f[0] for f in fills_of(shadow)] == [OrderSide.SELL_SHORT, OrderSide.BUY_TO_COVER]
    finally:
        await runtime.stop()


async def test_restart_preserves_crash_state_and_an_open_short(tmp_path):
    clock = Clock()
    settings = symbol(shorts=[("short_alpha", {"fire_at": 3})],
                      buys=[("buy_alpha", {"fire_at": 99})],
                      covers=[("cover_alpha", {"fire_at": 99})],
                      stop={"type": "fixed", "stop_percent": D("1")})
    monitor = pinned(MarketState.CRASH, clock)
    runtime, shadow = await start(tmp_path, clock, settings, monitor)
    try:
        for version in range(1, 5):
            await mstep(runtime, clock, monitor, version)
        before = shadow.account.state.positions["SPY"]
        cycle, quantity, price = before.cycle_id, before.quantity, before.avg_cost
    finally:
        await runtime.stop()
    # a new process whose monitor knows nothing: it must come back in CRASH, not NORMAL
    clock.value += timedelta(minutes=1)
    restarted = FrozenMonitor(clock=clock, initial_time=clock.value)
    runtime, shadow = await start(tmp_path, clock, settings, restarted)
    try:
        assert restarted.current_state is MarketState.CRASH
        assert restarted.fail_closed_reason is None
        feed(restarted, clock)
        authorization = restarted.evaluate().authorization
        assert not authorization.buy_allowed and authorization.short_allowed
        assert authorization.sell_allowed and authorization.cover_allowed
        position = shadow.account.state.positions["SPY"]
        assert position.direction is PositionDirection.SHORT
        assert (position.quantity, position.avg_cost, position.cycle_id) == (
            quantity, price, cycle)
        stop = shadow.stops.state(cycle)
        assert stop.direction is PositionDirection.SHORT and stop.status == "ACTIVE"
        assert stop.covered_quantity == quantity
        assert shadow.account.state.restricted_collateral() > 0
        assert runtime.lifecycle.verify_startup().clean
    finally:
        await runtime.stop()


async def test_an_unreadable_saved_market_state_fails_closed_without_blocking_exits(tmp_path):
    clock = Clock()
    settings = symbol(shorts=[("short_alpha", {"fire_at": 3})],
                      covers=[("cover_alpha", {"fire_at": 7})])
    monitor = pinned(MarketState.NORMAL, clock)
    runtime, shadow = await start(tmp_path, clock, settings, monitor)
    try:
        for version in range(1, 5):
            await mstep(runtime, clock, monitor, version, bid="150", ask="150.1")
        assert held(shadow) is PositionDirection.SHORT
    finally:
        await runtime.stop()
    with sqlite3.connect(tmp_path / "t.sqlite3") as db:
        db.execute("UPDATE service_health SET details_json = '{not json' "
                   "WHERE component = 'market_status_monitor'")
    clock.value += timedelta(minutes=1)
    restarted = pinned(MarketState.NORMAL, clock)
    runtime, shadow = await start(tmp_path, clock, settings, restarted)
    try:
        assert restarted.fail_closed_reason == "MARKET_STATUS_RESTORE_FAILED"
        authorization = restarted.evaluate().authorization
        assert not authorization.buy_allowed and not authorization.short_allowed
        assert authorization.sell_allowed and authorization.cover_allowed
        for version in range(5, 9):
            await mstep(runtime, clock, restarted, version, bid="144", ask="144.1")
        assert held(shadow) is None  # the open short was still covered
        assert [f[0] for f in fills_of(shadow)][-1] is OrderSide.BUY_TO_COVER
    finally:
        await runtime.stop()


# ------------------------------------------------------------------ monitor determinism


def _ref_bar(name: str, index: int, price: str) -> Candle:
    start = OPEN + index * MIN
    return Candle(name, CandleTimeframe.M1, start, start + MIN, D(price), D(price), D(price),
                  D(price), D("10000"), "iex", False, CandleOrigin.REPLAY, PriceBasis.RAW,
                  CandleStatus.CLOSED)


def test_the_state_machine_advances_per_reference_minute_not_per_call() -> None:
    def build(extra_calls: int) -> MarketStatusMonitor:
        monitor = MarketStatusMonitor(initial_time=OPEN)
        for index in range(45):
            price = "100" if index < 30 else "93"
            for name in REFERENCE:
                monitor.add_bar(_ref_bar(name, index, price))
            for _ in range(1 + extra_calls):
                monitor.evaluate(as_of=OPEN + (index + 1) * MIN)
        return monitor

    once, spammed = build(0), build(7)
    assert once.current_state is MarketState.CRASH
    assert (spammed.current_state, spammed.bars_in_state, spammed.snapshot()["transitions"]) == (
        once.current_state, once.bars_in_state, once.snapshot()["transitions"])


# --------------------------------------------------------------------- BACKTEST gating

MARKET = {"market_status_enabled": True}


def with_reference(bars, crash_at: int | None, *, config=None, symbols=REFERENCE):
    """Trading-symbol events plus SPY/QQQ/IWM candles that drop 7% at ``crash_at``."""
    out = list(events(bars))
    for index in range(len(bars)):
        price = "100" if crash_at is None or index < crash_at else "93"
        for name in symbols:
            out.append(ReplayEvent.bar(DomainCandle(
                name, Timeframe.M1, OPEN + index * MIN, OPEN + (index + 1) * MIN, D(price),
                D(price), D(price), D(price), D("10000"), FEED)))
    return out


def backtest(tmp_path, bars, ids, params, *, crash_at, name="a", reference=True, **extra):
    config = {**SHORT_ON, **MARKET, **extra}
    return run(bars, ids, params, config=config, result_dir=tmp_path / name,
               bar_events=with_reference(bars, crash_at, symbols=REFERENCE if reference else ()))


def refusals(result):
    return [d for d in result.decisions
            if d["kind"] == "ENTRY_REFUSED" and d.get("gate") == "MARKET_STATUS"]


def test_backtest_normal_market_permits_buy_and_short(tmp_path):
    bars = flat("100", 12)
    buy = backtest(tmp_path, bars, ("buy_alpha", "sell_alpha"),
                   {"buy_alpha": {"fire_at": 3}, "sell_alpha": {"fire_at": 8}},
                   crash_at=None, name="buy")
    short = backtest(tmp_path, bars, ("short_alpha", "cover_alpha"),
                     {"short_alpha": {"fire_at": 3}, "cover_alpha": {"fire_at": 8}},
                     crash_at=None, name="short")
    assert sides(buy) == ["BUY", "SELL"] and sides(short) == ["SELL_SHORT", "BUY_TO_COVER"]
    assert not refusals(buy) and not refusals(short)


def test_backtest_crash_blocks_buy_but_permits_a_short_and_its_cover(tmp_path):
    bars = flat("100", 30)
    buy = backtest(tmp_path, bars, ("buy_alpha", "sell_alpha"),
                   {"buy_alpha": {"fire_at": 20}, "sell_alpha": {"fire_at": 25}},
                   crash_at=10, name="buy")
    assert sides(buy) == []
    (refused,) = refusals(buy)
    assert refused["side"] == "BUY" and refused["market_state"] == "CRASH"
    short = backtest(tmp_path, bars, ("short_alpha", "cover_alpha"),
                     {"short_alpha": {"fire_at": 20}, "cover_alpha": {"fire_at": 25}},
                     crash_at=10, name="short")
    assert sides(short) == ["SELL_SHORT", "BUY_TO_COVER"] and not refusals(short)


def test_backtest_a_restricted_buy_does_not_suppress_a_short_in_arbitration(tmp_path):
    result = backtest(
        tmp_path, flat("100", 30), ("buy_alpha", "short_alpha", "cover_alpha"),
        {"buy_alpha": {"fire_at": 20}, "short_alpha": {"fire_at": 20},
         "cover_alpha": {"fire_at": 25}}, crash_at=10)
    assert sides(result) == ["SELL_SHORT", "BUY_TO_COVER"]
    assert [r["side"] for r in refusals(result)] == ["BUY"]


def test_backtest_sell_exit_still_happens_after_the_market_crashes(tmp_path):
    result = backtest(tmp_path, flat("100", 30), ("buy_alpha", "sell_alpha"),
                      {"buy_alpha": {"fire_at": 3}, "sell_alpha": {"fire_at": 20}},
                      crash_at=10)
    assert sides(result) == ["BUY", "SELL"]  # entered in NORMAL, exited during CRASH


def test_backtest_without_reference_data_fails_closed_for_both_directions(tmp_path):
    for name, ids, params in (
        ("buy", ("buy_alpha",), {"buy_alpha": {"fire_at": 3}}),
        ("short", ("short_alpha",), {"short_alpha": {"fire_at": 3}}),
    ):
        result = backtest(tmp_path, flat("100", 12), ids, params, crash_at=None,
                          reference=False, name=name)
        assert sides(result) == []
        assert refusals(result)


def test_backtest_is_deterministic_and_disabled_monitoring_keeps_the_baseline(tmp_path):
    bars = flat("100", 30)
    ids = ("buy_alpha", "short_alpha", "sell_alpha", "cover_alpha")
    params = {"buy_alpha": {"fire_at": 3}, "sell_alpha": {"fire_at": 8},
              "short_alpha": {"fire_at": 14}, "cover_alpha": {"fire_at": 20}}
    first = backtest(tmp_path, bars, ids, params, crash_at=None, name="one")
    second = backtest(tmp_path, bars, ids, params, crash_at=None, name="two")
    assert (first.fills, first.decisions, first.equity_curve) == (
        second.fills, second.decisions, second.equity_curve)
    assert sides(first) == sides(second)
    baseline = run(bars, ids, params, config=SHORT_ON, result_dir=tmp_path / "base")
    assert sides(first) == sides(baseline) == ["BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER"]
    assert [f["price"] for f in first.fills] == [f["price"] for f in baseline.fills]


def test_the_production_config_switch_reaches_the_runtime_settings(tmp_path):
    import yaml

    from test_short_cover_config import _document

    from mike_trade.app import _fingerprint, load_app_settings

    def settings(flag: bool | None):
        document = _document()
        if flag is not None:
            document["execution"]["market_status_enabled"] = flag
        path = tmp_path / f"cfg-{flag}.yaml"
        path.write_text(yaml.safe_dump(document))
        return load_app_settings(path, environ={})

    off, default, on = settings(False), settings(None), settings(True)
    assert not off.market_status_enabled and not default.market_status_enabled
    assert on.market_status_enabled and on.market_status_reference_symbols == REFERENCE
    assert _fingerprint(off) == _fingerprint(default) != _fingerprint(on)
