"""SHADOW end-of-day liquidation through the real runtime graph.

Credentials-free SHADOW mode with scripted strategy doubles and a temporary SQLite ledger;
nothing here touches a broker.  Times are UTC: 2026-10-08 is EDT (ET = UTC-4, so 15:55 ET is
19:55Z); 2026-11-27 is an early-close session (13:00 ET = 18:00Z) in EST (ET = UTC-5).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from scripted_strategies import build_registry
from test_strategy_collections_runtime import SCENARIO, Clock, assign, ctx

from mike_trade.app import (
    AppSettings,
    ShadowScenarioSettings,
    SymbolStrategySettings,
    build_runtime,
)
from mike_trade.domain.errors import ConfigurationError
from mike_trade.brokers.shadow.fills import FillAssumptions
from mike_trade.domain.trading import (
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    TimeInForce,
)
from mike_trade.runtime import ExecutionMode

DAY = datetime(2026, 10, 8, 18, 0, tzinfo=UTC)  # 14:00 ET
EOD = datetime(2026, 10, 8, 19, 55, tzinfo=UTC)  # 15:55 ET
EARLY_DAY = datetime(2026, 11, 27, 16, 0, tzinfo=UTC)  # 11:00 ET, early-close session
EARLY_EOD = datetime(2026, 11, 27, 17, 55, tzinfo=UTC)  # 12:55 ET


def symbols_config(buys: dict[str, tuple[tuple[str, dict], ...]], sells=None) -> dict:
    sells = sells or {}
    return {
        "symbols": tuple(buys),
        "symbol_strategies": tuple(
            SymbolStrategySettings(name, assign(*pairs), assign(*sells.get(name, ())))
            for name, pairs in buys.items()
        ),
    }


async def start(
    tmp_path: Path,
    clock: Clock,
    settings: dict,
    *,
    eod: bool = True,
    cutoff: int = 30,
    name: str = "eod.sqlite3",
    **scenario_kw,
):
    scenario = ShadowScenarioSettings(
        name=SCENARIO, starting_cash=D("50000"), per_symbol_budget=D("3000"),
        total_budget=D("20000"), max_entry_cycles_per_session=5, consecutive_loss_limit=5,
        post_sale_cooldown=timedelta(0), minimum_hold=timedelta(minutes=1), **scenario_kw,
    )
    runtime = build_runtime(
        AppSettings(
            mode=ExecutionMode.SHADOW, db_path=tmp_path / name, shadow_scenarios=(scenario,),
            auto_eod_liquidation=eod, entry_cutoff_minutes=cutoff, **settings,
        ),
        overrides={"clock": clock, "strategies": build_registry()},
    )
    await runtime.start()
    return runtime, runtime.broker.scenarios[SCENARIO]  # type: ignore[attr-defined]


def entry(symbol: str = "SPY", fire_at: int = 3, every: int = 0):
    return {symbol: (("buy_alpha", {"fire_at": fire_at, "fire_every": every}),)}


async def enter(runtime, clock: Clock, start_at: datetime, symbols=("SPY",)):
    """Open one position per symbol (BUY fires on the third completed bar)."""
    clock.value = start_at
    for version in range(1, 4):
        if version > 1:
            clock.value += timedelta(minutes=1)
        await runtime.lifecycle.process(
            {s: ctx(s, clock.value, version) for s in symbols}
        )


async def at(runtime, clock: Clock, when: datetime, version: int, symbols=("SPY",), **quote):
    clock.value = when
    result = await runtime.lifecycle.process(
        {s: ctx(s, when, version, **quote) for s in symbols}
    )
    return result[SCENARIO]


def orders(shadow, purpose: OrderPurpose):
    return [o for o in shadow.account.state.orders.values() if o.purpose is purpose]


def open_orders(shadow):
    return [o for o in shadow.account.state.orders.values() if o.is_open]


async def test_eod_enabled_closes_full_position_with_correct_cash_and_pnl(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbols_config(entry()))
    try:
        await enter(runtime, clock, DAY)
        position = shadow.account.state.positions["SPY"]
        quantity = position.quantity
        before = await at(runtime, clock, EOD - timedelta(minutes=1), 4)
        assert not any("EOD" in a for a in before)
        assert "SPY" in shadow.account.state.positions

        actions = await at(runtime, clock, EOD, 5, bid="101", ask="101.1")
        assert any(a.startswith("EOD_EXIT:") for a in actions)
        state = shadow.account.state
        assert "SPY" not in state.positions
        (eod_order,) = orders(shadow, OrderPurpose.EOD_CLOSE)
        assert eod_order.quantity == quantity and eod_order.filled_quantity == quantity
        # Realized P&L and cash come from the normal fill model: bought at the ask, sold at
        # the bid, nothing invented.
        fills = sorted(state.fills.values(), key=lambda f: f.filled_at)
        buy, sell = fills[0], fills[-1]
        assert (buy.price, sell.price) == (D("100.1"), D("101"))
        assert state.realized_pnl == (D("101") - D("100.1")) * quantity
        assert state.cash == D("50000") + state.realized_pnl
        # The protective stop is gone with the position; nothing is left resting.
        assert open_orders(shadow) == []
    finally:
        await runtime.stop()


async def test_eod_disabled_leaves_position_open(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbols_config(entry()), eod=False)
    try:
        await enter(runtime, clock, DAY)
        actions = await at(runtime, clock, EOD + timedelta(minutes=2), 4)
        assert not any("EOD" in a for a in actions)
        assert "SPY" in shadow.account.state.positions
        assert orders(shadow, OrderPurpose.EOD_CLOSE) == []
        assert len(orders(shadow, OrderPurpose.PROTECTIVE_STOP)) == 1
    finally:
        await runtime.stop()


async def test_configured_time_is_honoured(tmp_path):
    clock = Clock()
    runtime, shadow = await start(
        tmp_path, clock, {**symbols_config(entry()), "eod_close_at": "15:40"}
    )
    try:
        await enter(runtime, clock, DAY)
        await at(runtime, clock, datetime(2026, 10, 8, 19, 39, tzinfo=UTC), 4)
        assert "SPY" in shadow.account.state.positions
        await at(runtime, clock, datetime(2026, 10, 8, 19, 40, tzinfo=UTC), 5)
        assert "SPY" not in shadow.account.state.positions
    finally:
        await runtime.stop()


async def test_multiple_positions_are_all_liquidated(tmp_path):
    clock = Clock()
    runtime, shadow = await start(
        tmp_path, clock, symbols_config({**entry("SPY"), **entry("QQQ")})
    )
    try:
        both = ("SPY", "QQQ")
        await enter(runtime, clock, DAY, both)
        assert set(shadow.account.state.positions) == set(both)
        actions = await at(runtime, clock, EOD, 5, both)
        assert sum(a.startswith("EOD_EXIT:") for a in actions) == 2
        assert shadow.account.state.positions == {}
        assert len(orders(shadow, OrderPurpose.EOD_CLOSE)) == 2
        assert open_orders(shadow) == []
    finally:
        await runtime.stop()


async def test_no_duplicate_exits_with_sell_strategy_firing_at_eod(tmp_path):
    clock = Clock()
    runtime, shadow = await start(
        tmp_path,
        clock,
        symbols_config(entry(), {"SPY": (("sell_alpha", {"fire_at": 5, "fire_every": 1}),)}),
    )
    try:
        await enter(runtime, clock, DAY)
        await at(runtime, clock, EOD, 5)
        await at(runtime, clock, EOD + timedelta(minutes=1), 6)
        await at(runtime, clock, EOD + timedelta(minutes=2), 7)
        assert orders(shadow, OrderPurpose.STRATEGY_EXIT) == []
        (eod_order,) = orders(shadow, OrderPurpose.EOD_CLOSE)
        assert eod_order.filled_quantity == eod_order.quantity
        exits = [f for f in shadow.account.state.fills.values() if f.side.is_exit]
        assert len(exits) == 1
    finally:
        await runtime.stop()


async def test_stop_triggered_during_eod_processing_wins_without_eod_order(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbols_config(entry()))
    try:
        await enter(runtime, clock, DAY)
        actions = await at(runtime, clock, EOD, 5, bid="90", ask="90.1")
        assert "STOP_EXIT" in actions
        assert "SPY" not in shadow.account.state.positions
        assert orders(shadow, OrderPurpose.EOD_CLOSE) == []
        assert len([f for f in shadow.account.state.fills.values() if f.side.is_exit]) == 1
        # Later EOD cycles find nothing to close and submit nothing.
        await at(runtime, clock, EOD + timedelta(minutes=1), 6, bid="90", ask="90.1")
        assert orders(shadow, OrderPurpose.EOD_CLOSE) == []
    finally:
        await runtime.stop()


async def test_no_entries_once_eod_begins_even_without_a_cutoff(tmp_path):
    clock = Clock()
    # Cutoff 0 lets the entry time gate pass, isolating the EOD gate.
    runtime, shadow = await start(
        tmp_path, clock, symbols_config(entry(fire_at=1, every=1)), cutoff=0
    )
    try:
        clock.value = EOD - timedelta(minutes=2)
        first = await at(runtime, clock, EOD - timedelta(minutes=2), 1)
        assert any(a.startswith("ENTRY:") for a in first)  # control: allowed before EOD
        closing = await at(runtime, clock, EOD, 2)
        assert any(a.startswith("EOD_EXIT:") for a in closing)
        # A BUY signal fires on every following bar; no replacement position is opened.
        for version, minute in ((3, 1), (4, 2), (5, 3)):
            later = await at(runtime, clock, EOD + timedelta(minutes=minute), version)
            assert not any(a.startswith("ENTRY:") for a in later)
        assert shadow.account.state.positions == {}
        assert len(orders(shadow, OrderPurpose.ENTRY)) == 1
    finally:
        await runtime.stop()


async def test_early_close_session_liquidates_before_its_own_close(tmp_path):
    clock = Clock(EARLY_DAY)
    runtime, shadow = await start(tmp_path, clock, symbols_config(entry()))
    try:
        await enter(runtime, clock, EARLY_DAY)
        # 15:55 ET is after the 13:00 close, so the same 5-minute lead applies: 12:55 ET.
        await at(runtime, clock, EARLY_EOD - timedelta(minutes=1), 4)
        assert "SPY" in shadow.account.state.positions
        actions = await at(runtime, clock, EARLY_EOD, 5)
        assert any(a.startswith("EOD_EXIT:") for a in actions)
        assert "SPY" not in shadow.account.state.positions
    finally:
        await runtime.stop()


async def test_missing_and_stale_observations_never_fabricate_a_fill(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbols_config(entry()))
    try:
        await enter(runtime, clock, DAY)
        no_quote = dataclasses.replace(ctx("SPY", EOD, 5), latest_quote=None)
        clock.value = EOD
        result = await runtime.lifecycle.process({"SPY": no_quote})
        assert "EOD_WAITING_FOR_QUOTE" in result[SCENARIO]
        stale = dataclasses.replace(
            ctx("SPY", EOD + timedelta(minutes=1), 6),
            freshness=dataclasses.replace(
                ctx("SPY", EOD, 6).freshness, stale=True, reason="STALE"
            ),
        )
        clock.value = EOD + timedelta(minutes=1)
        result = await runtime.lifecycle.process({"SPY": stale})
        assert "EOD_WAITING_FOR_QUOTE" in result[SCENARIO]
        assert "SPY" in shadow.account.state.positions
        assert orders(shadow, OrderPurpose.EOD_CLOSE) == []
        # The protective stop stays armed while the position cannot be sold.
        (stop,) = orders(shadow, OrderPurpose.PROTECTIVE_STOP)
        assert stop.is_open
        # The first fresh observation completes the liquidation.
        await at(runtime, clock, EOD + timedelta(minutes=2), 7)
        assert "SPY" not in shadow.account.state.positions
    finally:
        await runtime.stop()


def sized(context, bid_size: str):
    """The same context with a thin bid, so a market SELL can only partly fill."""
    quote = dataclasses.replace(context.latest_quote, bid_size=D(bid_size))
    return dataclasses.replace(context, latest_quote=quote)


def invariants(shadow, symbol: str = "SPY") -> None:
    """Never more than one resting exit, never an exit larger than the position, and a
    resting protective stop and an EOD order never coexist."""
    state = shadow.account.state
    resting = [o for o in state.orders.values() if o.is_open and o.side.is_exit]
    assert len(resting) <= 1
    position = state.positions.get(symbol)
    held = D("0") if position is None else position.quantity
    assert sum((o.remaining for o in resting), D("0")) <= held
    entered = sum((f.quantity for f in state.fills.values() if f.side.is_entry), D("0"))
    exited = sum((f.quantity for f in state.fills.values() if f.side.is_exit), D("0"))
    assert exited <= entered and entered - exited == held


async def legacy_pending_eod(shadow, when: datetime, symbol: str = "SPY"):
    """A ledger left by a crash mid-liquidation: stop cancelled, EOD order resting."""
    position = shadow.account.state.positions[symbol]
    await shadow.stops.prepare_discretionary_exit(position.cycle_id, at=when)
    return await shadow.account.submit_order(
        OrderIntent(
            intent_id="legacy-eod-intent", client_order_id=f"legacy-eod-{when.isoformat()}",
            account_scope_id=shadow.account.scope.scope_id, cycle_id=position.cycle_id,
            symbol=symbol, side=OrderSide.SELL, purpose=OrderPurpose.EOD_CLOSE,
            order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY, created_at=when,
            quantity=position.quantity,
        )
    )


async def test_restart_with_pending_eod_exit_finishes_it_without_duplicating(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbols_config(entry()))
    await enter(runtime, clock, DAY)
    pending = await legacy_pending_eod(shadow, EOD)
    await runtime.stop()

    clock.value = EOD + timedelta(minutes=1)
    restarted, shadow2 = await start(tmp_path, clock, symbols_config(entry()))
    try:
        # The startup tick found the half-finished liquidation, could not observe a quote,
        # retired the order and put the stop back before any entry could be accepted.
        (recovered,) = orders(shadow2, OrderPurpose.EOD_CLOSE)
        assert recovered.broker_order_id == pending.broker_order_id and not recovered.is_open
        assert "SPY" in shadow2.account.state.positions
        (resting,) = open_orders(shadow2)
        assert resting.purpose is OrderPurpose.PROTECTIVE_STOP
        invariants(shadow2)
        await at(restarted, clock, EOD + timedelta(minutes=2), 6)
        assert "SPY" not in shadow2.account.state.positions
        assert len(orders(shadow2, OrderPurpose.EOD_CLOSE)) == 2  # one dead, one filled
        assert open_orders(shadow2) == []
        invariants(shadow2)
    finally:
        await restarted.stop()


async def test_partial_eod_fill_keeps_residual_protected_then_completes(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbols_config(entry()))
    try:
        await enter(runtime, clock, DAY)
        quantity = shadow.account.state.positions["SPY"].quantity
        clock.value = EOD
        result = await runtime.lifecycle.process({"SPY": sized(ctx("SPY", EOD, 5), "10")})
        actions = result[SCENARIO]
        assert any(a.startswith("EOD_REMAINDER_CANCELED:") for a in actions)
        assert any(a.startswith("PROTECTION_RESTORED:") for a in actions)
        residual = shadow.account.state.positions["SPY"]
        assert residual.quantity == quantity - D("10")
        (eod_order,) = orders(shadow, OrderPurpose.EOD_CLOSE)
        assert not eod_order.is_open and eod_order.filled_quantity == D("10")
        # Exactly one resting order: a protective stop sized to the residual, not the
        # original position.
        (resting,) = open_orders(shadow)
        assert resting.purpose is OrderPurpose.PROTECTIVE_STOP
        assert resting.remaining == residual.quantity
        invariants(shadow)

        # Inside the retry interval nothing new is submitted; the residual stays covered.
        await at(runtime, clock, EOD + timedelta(seconds=30), 6)
        assert len(orders(shadow, OrderPurpose.EOD_CLOSE)) == 1
        invariants(shadow)

        # The next attempt closes exactly what is still held.
        await at(runtime, clock, EOD + timedelta(seconds=61), 7)
        assert "SPY" not in shadow.account.state.positions
        assert [o.quantity for o in orders(shadow, OrderPurpose.EOD_CLOSE)][-1] == residual.quantity
        assert open_orders(shadow) == []
        invariants(shadow)
    finally:
        await runtime.stop()


async def test_stop_first_after_partial_eod_fill_exits_only_the_residual(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbols_config(entry()))
    try:
        await enter(runtime, clock, DAY)
        quantity = shadow.account.state.positions["SPY"].quantity
        clock.value = EOD
        await runtime.lifecycle.process({"SPY": sized(ctx("SPY", EOD, 5), "10")})
        actions = await at(runtime, clock, EOD + timedelta(seconds=61), 6, bid="90", ask="90.1")
        assert "STOP_EXIT" in actions
        assert "SPY" not in shadow.account.state.positions
        assert len(orders(shadow, OrderPurpose.EOD_CLOSE)) == 1  # no second EOD order
        exited = sum(f.quantity for f in shadow.account.state.fills.values() if f.side.is_exit)
        assert exited == quantity
        assert open_orders(shadow) == []
        invariants(shadow)
    finally:
        await runtime.stop()


async def test_pending_eod_within_latency_then_unfilled_restores_the_stop(tmp_path):
    clock = Clock()
    runtime, shadow = await start(
        tmp_path, clock, symbols_config(entry()),
        fill_assumptions=FillAssumptions(latency=timedelta(seconds=30)),
    )
    try:
        # Entry also needs a quote after its own latency, so give it one more bar.
        clock.value = DAY
        for version in range(1, 6):
            clock.value += timedelta(minutes=1)
            await runtime.lifecycle.process({"SPY": ctx("SPY", clock.value, version)})
        assert "SPY" in shadow.account.state.positions
        position = shadow.account.state.positions["SPY"]
        await at(runtime, clock, EOD, 6)
        (eod_order,) = orders(shadow, OrderPurpose.EOD_CLOSE)
        assert eod_order.is_open  # inside its latency window the order may legitimately rest
        invariants(shadow)
        # Past the window with no quote that post-dates the order: cancel, protect again.
        late = ctx("SPY", EOD + timedelta(seconds=31), 7)
        old_quote = dataclasses.replace(late.latest_quote, provider_ts=EOD, receive_ts=EOD)
        clock.value = EOD + timedelta(seconds=31)
        result = await runtime.lifecycle.process(
            {"SPY": dataclasses.replace(late, latest_quote=old_quote)}
        )
        actions = result[SCENARIO]
        assert any(a.startswith("EOD_REMAINDER_CANCELED:") for a in actions)
        assert "SPY" in shadow.account.state.positions
        (resting,) = open_orders(shadow)
        assert resting.purpose is OrderPurpose.PROTECTIVE_STOP
        assert resting.remaining == position.quantity
        invariants(shadow)
    finally:
        await runtime.stop()


async def test_missed_eod_next_session_restores_coverage_before_anything_else(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbols_config(entry(fire_at=3, every=1)))
    await enter(runtime, clock, DAY)
    held = shadow.account.state.positions["SPY"]
    stale = await legacy_pending_eod(shadow, EOD)  # liquidation missed; stop left cancelled
    await runtime.stop()

    next_open = datetime(2026, 10, 9, 14, 0, tzinfo=UTC)  # 10:00 ET next session
    clock.value = next_open
    restarted, shadow2 = await start(tmp_path, clock, symbols_config(entry(fire_at=3, every=1)))
    try:
        # Startup is not blocked by the drifted ledger, and the startup tick (no candle)
        # already retired the dead DAY order and put the cancelled stop back before entries
        # could open.
        assert restarted.lifecycle.verify_startup().clean
        position = shadow2.account.state.positions["SPY"]
        assert position.quantity == held.quantity
        assert not shadow2.account.state.orders[stale.broker_order_id].is_open
        (resting,) = open_orders(shadow2)
        assert resting.purpose is OrderPurpose.PROTECTIVE_STOP
        assert resting.remaining == position.quantity
        # The BUY signal fires on every bar of the new session; the held symbol opens nothing
        # and the stop is not duplicated.
        actions = await at(restarted, clock, next_open + timedelta(minutes=1), 8)
        assert not any(a.startswith("ENTRY:") for a in actions)
        (resting,) = open_orders(shadow2)
        assert resting.purpose is OrderPurpose.PROTECTIVE_STOP
        invariants(shadow2)
        # The restored stop is live: a drop through it exits exactly the held quantity.
        crash = await at(restarted, clock, next_open + timedelta(minutes=2), 9, bid="90", ask="90.1")
        assert "STOP_EXIT" in crash
        assert "SPY" not in shadow2.account.state.positions
        invariants(shadow2)
    finally:
        await restarted.stop()


async def test_time_driven_tick_liquidates_without_a_new_candle(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbols_config(entry()))
    try:
        await enter(runtime, clock, DAY)
        runtime.lifecycle.bind_context_provider(
            lambda syms: {s: ctx(s, clock.value, 9) for s in syms}
        )
        clock.value = EOD - timedelta(minutes=1)
        assert await runtime.lifecycle.on_tick() == {}
        assert "SPY" in shadow.account.state.positions
        clock.value = EOD
        await runtime._lifecycle_tick()
        assert "SPY" not in shadow.account.state.positions
        (eod_order,) = orders(shadow, OrderPurpose.EOD_CLOSE)
        assert eod_order.filled_quantity == eod_order.quantity
        # A further tick finds nothing to do and submits nothing.
        assert await runtime.lifecycle.on_tick() == {}
        assert len(orders(shadow, OrderPurpose.EOD_CLOSE)) == 1
    finally:
        await runtime.stop()


def test_eod_setting_validation():
    # PAPER/LIVE EOD liquidation is supported by the Alpaca execution lifecycle.
    AppSettings(mode=ExecutionMode.PAPER, auto_eod_liquidation=True)
    with pytest.raises(ValueError):
        AppSettings(mode=ExecutionMode.PAPER, auto_eod_liquidation=True, eod_close_at="16:00")
    with pytest.raises(ValueError):
        AppSettings(mode=ExecutionMode.SHADOW, eod_close_at="16:00")
    with pytest.raises(ValueError):
        AppSettings(mode=ExecutionMode.SHADOW, eod_close_at="3:55pm")
    AppSettings(mode=ExecutionMode.SHADOW, auto_eod_liquidation=True, eod_close_at="15:45")
