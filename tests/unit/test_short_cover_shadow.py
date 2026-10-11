"""Bidirectional (LONG/SHORT) lifecycle in the SHADOW engine.

Drives the real runtime graph (credentials-free SHADOW mode, simulated broker, temporary
SQLite) with scripted strategy doubles; nothing here touches a broker.  Mirrors
``test_strategy_collections_runtime`` for the SHORT and COVER roles.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from scripted_strategies import build_bidirectional_registry
from test_strategy_collections_runtime import SCENARIO, Clock, assign, ctx

from mike_trade.app import (
    AppSettings,
    ShadowScenarioSettings,
    SymbolStrategySettings,
    build_runtime,
)
from mike_trade.domain.protection import ProtectionKind
from mike_trade.domain.trading import OrderPurpose, OrderSide, PositionDirection
from mike_trade.runtime import ExecutionMode


async def start(tmp_path: Path, clock: Clock, settings: dict, *, name="t.sqlite3",
                cash="50000", short=True, real=False, **scenario):
    config = ShadowScenarioSettings(
        name=SCENARIO, starting_cash=D(cash), per_symbol_budget=D("3000"),
        total_budget=D("20000"), max_entry_cycles_per_session=3, consecutive_loss_limit=3,
        post_sale_cooldown=timedelta(0), minimum_hold=timedelta(minutes=1),
        short_selling_enabled=short, **scenario)
    runtime = build_runtime(
        AppSettings(mode=ExecutionMode.SHADOW, db_path=tmp_path / name,
                    shadow_scenarios=(config,), **settings),
        # ``real``: use the production registry (the six real SHORT/COVER strategies) instead
        # of the scripted doubles
        overrides={"clock": clock} if real
        else {"clock": clock, "strategies": build_bidirectional_registry()})
    await runtime.start()
    return runtime, runtime.broker.scenarios[SCENARIO]  # type: ignore[attr-defined]


def symbol(buys=(), sells=(), shorts=(), covers=(), stop=None) -> dict:
    cfg = {"symbols": ("SPY",), "symbol_strategies": (SymbolStrategySettings(
        "SPY", assign(*buys), assign(*sells), assign(*shorts), assign(*covers)),)}
    if stop is not None:
        cfg["symbol_stop_policies"] = {"SPY": stop}
    return cfg


async def step(runtime, clock, version, *, bid="100", ask=None, seconds=60):
    if version > 1:
        clock.value += timedelta(seconds=seconds)
    result = await runtime.lifecycle.process(
        {"SPY": ctx("SPY", clock.value, version, bid=bid, ask=ask)})
    return result[SCENARIO]


def orders(shadow, purpose, side=None):
    return [o for o in shadow.account.state.orders.values()
            if o.purpose is purpose and (side is None or o.side is side)]


def fills_of(shadow):
    return [(f.side, f.quantity, f.price) for f in
            sorted(shadow.account.state.fills.values(), key=lambda f: f.filled_at)]


# ------------------------------------------------------------------ lifecycle and P&L


async def test_short_then_cover_books_the_gain_of_a_falling_price(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})], covers=[("cover_alpha", {"fire_at": 6})]))
    try:
        for version in range(1, 5):
            actions = await step(runtime, clock, version, bid="150", ask="150.1")
        position = shadow.account.state.positions["SPY"]
        assert position.direction is PositionDirection.SHORT and position.quantity == D("19")
        for version in range(5, 8):
            await step(runtime, clock, version, bid="144", ask="144.1")
        state = shadow.account.state
        assert "SPY" not in state.positions
        # SHORT 19 @ 150 (bid), COVER 19 @ 144.10 (ask): (150 - 144.10) x 19
        assert fills_of(shadow) == [(OrderSide.SELL_SHORT, D("19"), D("150")),
                                    (OrderSide.BUY_TO_COVER, D("19"), D("144.1"))]
        assert state.realized_pnl == D("112.1") and state.cash == D("50112.1")
        assert state.restricted_collateral() == D("0")  # released once covered
    finally:
        await runtime.stop()


async def test_short_loses_when_the_price_rises(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})], covers=[("cover_alpha", {"fire_at": 6})]))
    try:
        for version in range(1, 5):
            await step(runtime, clock, version, bid="150", ask="150.1")
        for version in range(5, 8):
            await step(runtime, clock, version, bid="155.9", ask="156")
        state = shadow.account.state
        assert state.realized_pnl == D("-114")  # (150 - 156) x 19
        assert state.cash == D("49886")
    finally:
        await runtime.stop()


async def test_short_equity_marks_the_liability_and_proceeds_are_restricted(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})], covers=[("cover_alpha", {"fire_at": 99})]))
    try:
        for version in range(1, 5):
            await step(runtime, clock, version, bid="150", ask="150.1")
        account = shadow.account
        state = account.state
        proceeds = D("19") * D("150")
        assert state.cash == D("50000") + proceeds            # proceeds credited ...
        assert state.restricted_collateral() == D("1.5") * proceeds  # ... but locked x1.5
        snapshot = await account.get_account()
        assert snapshot.shorting_enabled is True
        # buying power = cash - collateral: the sale proceeds are NOT purchasing power
        assert snapshot.buying_power == state.cash - D("1.5") * proceeds
        assert snapshot.buying_power < D("50000")
        # mark at 140: gain = (150 - 140) x 19; equity = cash - liability
        assert account.unrealized_pnl({"SPY": D("140")}) == D("190")
        assert account.equity({"SPY": D("140")}) == state.cash - D("19") * D("140")
        assert account.equity({"SPY": D("150")}) == D("50000")  # no edge from selling short
    finally:
        await runtime.stop()


async def test_shorting_is_inert_unless_the_scenario_authorizes_it(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})]), short=False)
    try:
        for version in range(1, 8):
            await step(runtime, clock, version, bid="150", ask="150.1")
        assert shadow.account.state.orders == {} and "SPY" not in shadow.account.state.positions
    finally:
        await runtime.stop()


async def test_a_short_must_fit_in_own_cash_not_in_sale_proceeds(tmp_path):
    clock = Clock()
    # $1,000 cash cannot carry the ~$1,425 own-cash margin of a $2,850 short
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})]), cash="1000")
    try:
        for version in range(1, 6):
            await step(runtime, clock, version, bid="150", ask="150.1")
        assert shadow.account.state.orders == {}
        assert shadow.account.state.cash == D("1000")
    finally:
        await runtime.stop()


# ------------------------------------------------------ transitions go through FLAT


async def test_sell_then_short_and_cover_then_buy_always_pass_through_flat(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        buys=[("buy_alpha", {"fire_at": 3}), ("buy_bravo", {"fire_at": 26})],
        sells=[("sell_alpha", {"fire_at": 8})],
        shorts=[("short_alpha", {"fire_at": 14})],
        covers=[("cover_alpha", {"fire_at": 20})]))
    try:
        held: list[tuple[int, str | None]] = []
        for version in range(1, 32):
            await step(runtime, clock, version, seconds=120)
            position = shadow.account.state.positions.get("SPY")
            held.append((version, None if position is None else position.direction.value))
        order = [f[0] for f in fills_of(shadow)]
        assert order == [OrderSide.BUY, OrderSide.SELL, OrderSide.SELL_SHORT,
                         OrderSide.BUY_TO_COVER, OrderSide.BUY]
        # a flat bar separates every exit from the next entry; never both directions at once
        directions = [d for _, d in held]
        assert directions.count("LONG") and directions.count("SHORT")
        for previous, current in zip(directions, directions[1:]):
            assert not ({previous, current} == {"LONG", "SHORT"})
    finally:
        await runtime.stop()


async def test_a_sell_never_closes_a_short_and_a_cover_never_closes_a_long(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path / "s", clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})],
        sells=[("sell_alpha", {"fire_at": 6, "fire_every": 1})],
        covers=[("cover_alpha", {"fire_at": 20})])) if (tmp_path / "s").mkdir() is None else None
    try:
        for version in range(1, 16):
            await step(runtime, clock, version, seconds=120)
        assert shadow.account.state.positions["SPY"].direction is PositionDirection.SHORT
        assert not orders(shadow, OrderPurpose.STRATEGY_EXIT, OrderSide.SELL)
    finally:
        await runtime.stop()
    (tmp_path / "l").mkdir()
    runtime, shadow = await start(tmp_path / "l", Clock(), symbol(
        buys=[("buy_alpha", {"fire_at": 3})],
        covers=[("cover_alpha", {"fire_at": 6, "fire_every": 1})],
        sells=[("sell_alpha", {"fire_at": 20})]))
    try:
        clock = Clock()
        for version in range(1, 16):
            await step(runtime, clock, version, seconds=120)
        assert shadow.account.state.positions["SPY"].direction is PositionDirection.LONG
        assert not orders(shadow, OrderPurpose.STRATEGY_EXIT, OrderSide.BUY_TO_COVER)
    finally:
        await runtime.stop()


# ------------------------------------------------------------------ arbitration


async def test_buy_and_short_compete_and_only_one_direction_ever_trades(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        buys=[("buy_alpha", {"fire_at": 3})], shorts=[("short_alpha", {"fire_at": 3})]))
    try:
        first = await step(runtime, clock, 3)
        assert sum(a.startswith("ENTRY:") for a in first) == 1
        (entry,) = orders(shadow, OrderPurpose.ENTRY)
        assert entry.side is OrderSide.BUY and entry.strategy_key == "buy_alpha"
        assert shadow.account.state.positions["SPY"].direction is PositionDirection.LONG
    finally:
        await runtime.stop()


async def test_multiple_short_strategies_open_one_short_and_never_scale(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(shorts=[
        ("short_charlie", {"fire_at": 3, "fire_every": 1}),
        ("short_alpha", {"fire_at": 3, "fire_every": 1}),
        ("short_bravo", {"fire_at": 3, "fire_every": 1})]))
    try:
        first = await step(runtime, clock, 3)
        assert sum(a.startswith("ENTRY:") for a in first) == 1
        for version in range(4, 9):
            later = await step(runtime, clock, version)
            assert not any(a.startswith("ENTRY:") for a in later)
        (entry,) = orders(shadow, OrderPurpose.ENTRY)
        assert entry.side is OrderSide.SELL_SHORT and entry.strategy_key == "short_alpha"
        position = shadow.account.state.positions["SPY"]
        assert position.quantity == entry.quantity
        (stop,) = orders(shadow, OrderPurpose.PROTECTIVE_STOP)
        assert stop.side is OrderSide.BUY_TO_COVER and stop.quantity == position.quantity
    finally:
        await runtime.stop()


async def test_multiple_cover_signals_close_the_whole_short_once(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})],
        covers=[(c, {"fire_at": 6}) for c in ("cover_charlie", "cover_alpha", "cover_bravo")]))
    try:
        await step(runtime, clock, 3)
        held = shadow.account.state.positions["SPY"].quantity
        await step(runtime, clock, 4)
        await step(runtime, clock, 5)
        actions = await step(runtime, clock, 6, seconds=120)
        assert sum(a.startswith("EXIT:") for a in actions) == 1
        (exit_order,) = orders(shadow, OrderPurpose.STRATEGY_EXIT)
        assert exit_order.side is OrderSide.BUY_TO_COVER and exit_order.quantity == held
        assert "SPY" not in shadow.account.state.positions  # flat: no residual, no reversal
        again = await step(runtime, clock, 7)
        assert not any(a.startswith("EXIT:") for a in again)
    finally:
        await runtime.stop()


# ------------------------------------------------------ indicator-state continuity (all roles)


async def test_every_role_keeps_its_persisted_state_advancing_while_ineligible(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        buys=[("buy_alpha", {"fire_at": 3})], sells=[("sell_alpha", {"fire_at": 9})],
        shorts=[("short_alpha", {"fire_on_nth": 16})],
        covers=[("cover_alpha", {"fire_on_nth": 24})]))
    try:
        for version in range(1, 30):
            await step(runtime, clock, version, seconds=120)
        # short_alpha fires on its OWN 16th evaluation: it was evaluated on every bar,
        # including the bars the long position was held and it was ineligible
        shorts = [f for f in fills_of(shadow) if f[0] is OrderSide.SELL_SHORT]
        covers = [f for f in fills_of(shadow) if f[0] is OrderSide.BUY_TO_COVER]
        assert len(shorts) == 1 and len(covers) == 1
    finally:
        await runtime.stop()
    with sqlite3.connect(tmp_path / "t.sqlite3") as db:
        counts = {key: json_count(state) for key, state in db.execute(
            "SELECT si.strategy_key, ss.state_json FROM strategy_state ss JOIN "
            "strategy_instances si ON si.instance_id = ss.instance_id")}
    assert set(counts) == {"buy_alpha", "sell_alpha", "short_alpha", "cover_alpha"}
    assert set(counts.values()) == {29}  # every role evaluated on every one of the 29 bars


def json_count(text: str) -> int:
    import json
    return int(json.loads(text)["count"])


# ------------------------------------------------------------------ protective stops


async def test_a_fixed_short_stop_sits_above_entry_and_covers_when_the_ask_rises(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})],
        stop={"type": "fixed", "stop_percent": D("1")}))
    try:
        await step(runtime, clock, 3, bid="100", ask="100.1")
        position = shadow.account.state.positions["SPY"]
        stop = shadow.stops.state(position.cycle_id)
        assert stop.direction is PositionDirection.SHORT and stop.status == "ACTIVE"
        assert stop.stop_price == D("101")  # 1% ABOVE the 100 fill
        assert stop.covered_quantity == position.quantity
        (stop_order,) = orders(shadow, OrderPurpose.PROTECTIVE_STOP)
        assert stop_order.side is OrderSide.BUY_TO_COVER
        # below the stop: untouched; the bid alone cannot trigger a short stop
        quiet = await step(runtime, clock, 4, bid="100.9", ask="100.95")
        assert "STOP_EXIT" not in quiet
        crash = await step(runtime, clock, 5, bid="101.4", ask="101.5")
        assert "STOP_EXIT" in crash
        assert "SPY" not in shadow.account.state.positions
        last = fills_of(shadow)[-1]
        assert last == (OrderSide.BUY_TO_COVER, position.quantity, D("101.5"))  # ask, not stop
        # no reverse position: the stop fill only closed the short
        assert [f[0] for f in fills_of(shadow)] == [OrderSide.SELL_SHORT, OrderSide.BUY_TO_COVER]
    finally:
        await runtime.stop()


async def test_a_protective_stop_overrides_the_minimum_hold_for_a_short(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})],
        covers=[("cover_alpha", {"fire_at": 4, "fire_every": 1})]))
    try:
        await step(runtime, clock, 3)
        held = await step(runtime, clock, 4, seconds=10)  # inside the 60 s minimum hold
        assert not any(a.startswith("EXIT:") for a in held)
        crash = await step(runtime, clock, 5, bid="104", ask="104.1", seconds=10)
        assert "STOP_EXIT" in crash and "SPY" not in shadow.account.state.positions
    finally:
        await runtime.stop()


@pytest.mark.parametrize("stop", [
    {"type": "native_trailing", "trail_percent": D("1.5")},
    {"type": "stepped_trailing", "initial_stop_percent": D("1.5"),
     "step_trigger_percent": D("1"), "step_percent": D("0.5")},
])
async def test_a_short_trailing_stop_follows_gains_down_and_never_loosens(tmp_path, stop):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})], stop=stop))
    try:
        await step(runtime, clock, 3, bid="100", ask="100.1")
        cycle = shadow.account.state.positions["SPY"].cycle_id
        stops = [shadow.stops.state(cycle).stop_price]
        prices = ("98.5", "97", "96", "96.5", "96.8", "96.4")
        for index, bid in enumerate(prices):
            await step(runtime, clock, 10 + index, bid=bid, ask=str(D(bid) + D("0.1")),
                       seconds=20)
            stops.append(shadow.stops.state(cycle).stop_price)
        assert stops[3] < stops[0]                       # favorable (falling) moves lowered it
        assert stops == sorted(stops, reverse=True)      # never raised, even on the rebound
        assert stops[-1] == stops[-3] == min(stops)
        assert "SPY" in shadow.account.state.positions   # the rebound stayed below the stop
        assert shadow.stops.state(cycle).direction is PositionDirection.SHORT
    finally:
        await runtime.stop()


@pytest.mark.parametrize("kind, stop", [
    (ProtectionKind.FIXED_STOP, {"type": "fixed", "stop_amount": D("1")}),
    (ProtectionKind.BROKER_TRAILING_STOP, {"type": "native_trailing", "trail_amount": D("1")}),
    (ProtectionKind.STEPPED_TRAILING_STOP, {
        "type": "stepped_trailing", "initial_stop_percent": D("1"),
        "step_trigger_percent": D("1"), "step_percent": D("0.5")}),
])
async def test_every_stop_mode_protects_a_short_with_no_cover_strategy(tmp_path, kind, stop):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})], stop=stop))
    try:
        await step(runtime, clock, 3)
        state = shadow.stops.state(shadow.account.state.positions["SPY"].cycle_id)
        assert state.policy.kind is kind and state.status == "ACTIVE"
        crash = await step(runtime, clock, 4, bid="102", ask="102.1", seconds=120)
        assert "STOP_EXIT" in crash and "SPY" not in shadow.account.state.positions
    finally:
        await runtime.stop()


# ------------------------------------------------------ pending / cancelled / restart


async def test_a_pending_short_blocks_a_second_entry_and_fills_on_the_next_quote(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3, "fire_every": 1})]))
    try:
        await step(runtime, clock, 1, bid="99", ask="99.1")  # the account marks from this quote
        clock.value += timedelta(seconds=60)
        # ... then the quote feed drops out: the entry is submitted but cannot fill yet
        no_quote = dataclasses.replace(
            ctx("SPY", clock.value, 3, bid="99", ask="99.1"), latest_quote=None)
        first = (await runtime.lifecycle.process({"SPY": no_quote}))[SCENARIO]
        assert sum(a.startswith("ENTRY:") for a in first) == 1 and not any(
            a.startswith("FILL:") for a in first)
        assert "SPY" not in shadow.account.state.positions       # submitted != filled
        clock.value += timedelta(seconds=30)
        again = (await runtime.lifecycle.process(
            {"SPY": dataclasses.replace(
                ctx("SPY", clock.value, 4, bid="99", ask="99.1"), latest_quote=None)}))[SCENARIO]
        assert not any(a.startswith("ENTRY:") for a in again)    # still one working entry
        clock.value += timedelta(seconds=30)
        filled = await step(runtime, clock, 5, bid="99", ask="99.1", seconds=0)
        assert any(a.startswith("FILL:SELL_SHORT") for a in filled)
        assert len(orders(shadow, OrderPurpose.ENTRY)) == 1
        assert shadow.account.state.positions["SPY"].direction is PositionDirection.SHORT
    finally:
        await runtime.stop()


async def test_a_cancelled_pending_short_leaves_no_position_and_allows_a_new_entry(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3, "fire_every": 4})]))
    try:
        await step(runtime, clock, 1, bid="99", ask="99.1")
        clock.value += timedelta(seconds=60)
        no_quote = dataclasses.replace(
            ctx("SPY", clock.value, 3, bid="99", ask="99.1"), latest_quote=None)
        await runtime.lifecycle.process({"SPY": no_quote})
        (pending,) = orders(shadow, OrderPurpose.ENTRY)
        await shadow.account.cancel_order(pending.broker_order_id, at=clock.value)
        assert "SPY" not in shadow.account.state.positions
        assert shadow.account.state.restricted_collateral() == D("0")
        clock.value += timedelta(minutes=1)
        retry = await step(runtime, clock, 7, bid="99", ask="99.1", seconds=0)  # fires again
        assert any(a.startswith("FILL:SELL_SHORT") for a in retry)
    finally:
        await runtime.stop()


async def test_a_short_its_stop_and_strategy_state_survive_a_restart(tmp_path):
    clock = Clock()
    settings = symbol(shorts=[("short_alpha", {"fire_at": 3})],
                      covers=[("cover_alpha", {"fire_at": 9})],
                      stop={"type": "fixed", "stop_percent": D("1")})
    runtime, shadow = await start(tmp_path, clock, settings)
    try:
        for version in range(1, 6):
            await step(runtime, clock, version)
        cycle = shadow.account.state.positions["SPY"].cycle_id
        quantity = shadow.account.state.positions["SPY"].quantity
    finally:
        await runtime.stop()
    # a new process: the ledger is replayed, the short and its stop come back
    runtime, shadow = await start(tmp_path, clock, settings)
    try:
        position = shadow.account.state.positions["SPY"]
        assert position.direction is PositionDirection.SHORT and position.quantity == quantity
        assert position.cycle_id == cycle
        stop = shadow.stops.state(cycle)
        assert stop.direction is PositionDirection.SHORT and stop.status == "ACTIVE"
        assert stop.stop_price == D("101") and stop.covered_quantity == quantity
        assert runtime.lifecycle.verify_startup().clean  # no protective-coverage drift
        for version in range(6, 12):  # strategy state continues from its persisted count
            await step(runtime, clock, version)
        assert shadow.account.state.positions.get("SPY") is None  # cover fired on its 9th bar
        assert [f[0] for f in fills_of(shadow)] == [OrderSide.SELL_SHORT, OrderSide.BUY_TO_COVER]
    finally:
        await runtime.stop()


# ------------------------------------------------ the REAL strategies, end to end (SHADOW)


def _wave_closes(n: int = 240) -> list[D]:
    import math

    return [D(f"{100 + 4 * math.sin(2 * math.pi * i / 60):.2f}") for i in range(n)]


def _history_context(closes: list[D], index: int, clock_value, window: int = 60):
    """A MarketContext whose M1 window holds the closes up to ``index`` (spread 0.10)."""
    from mike_trade.market_data.candles import (
        Candle,
        CandleOrigin,
        CandleStatus,
        CandleTimeframe,
        PriceBasis,
    )
    from mike_trade.market_data.context import ContextFreshness, MarketContext, QuoteSnapshot
    from mike_trade.scheduling.calendar import SessionStatus

    lo = max(0, index + 1 - window)
    bars = tuple(
        Candle("SPY", CandleTimeframe.M1, clock_value - timedelta(minutes=index - i + 1),
               clock_value - timedelta(minutes=index - i), closes[i], closes[i], closes[i],
               closes[i], D("100"), "iex", False, CandleOrigin.REPLAY, PriceBasis.RAW,
               CandleStatus.CLOSED)
        for i in range(lo, index + 1))
    close = closes[index]
    return MarketContext(
        symbol="SPY", as_of=clock_value, version=index + 1, feed_id="iex", consolidated=False,
        price_basis=PriceBasis.RAW, session_status=SessionStatus.REGULAR,
        session_id=f"XNYS-{clock_value.date().isoformat()}",
        freshness=ContextFreshness(clock_value, timedelta(0), timedelta(seconds=90), False, None),
        candles={CandleTimeframe.M1: bars}, indicators={},
        latest_quote=QuoteSnapshot("SPY", close - D("0.05"), close + D("0.05"), D("100"),
                                   D("100"), clock_value, clock_value, "iex"))


async def _run_real(tmp_path, name, *, stop=None):
    clock = Clock()
    params = {"fast_period": 3, "slow_period": 8}
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_ema_bearish_crossover", params)],
        covers=[("cover_ema_bullish_crossover", params)], stop=stop), name=name, real=True)
    closes = _wave_closes()
    submitted = []
    try:
        for index in range(len(closes)):
            clock.value += timedelta(minutes=1)
            before = set(shadow.account.state.orders)
            await runtime.lifecycle.process(
                {"SPY": _history_context(closes, index, clock.value)})
            # strategy-originated orders only (the engine's own protective stops are excluded)
            submitted += [(clock.value, shadow.account.state.orders[o].side)
                          for o in shadow.account.state.orders
                          if o not in before and shadow.account.state.orders[o].purpose
                          is not OrderPurpose.PROTECTIVE_STOP]
        fills = fills_of(shadow)
        state = shadow.account.state
        result = (fills, state.cash, state.realized_pnl, dict(state.positions) and True, submitted)
    finally:
        await runtime.stop()
    return result, closes


async def test_the_real_strategies_trade_short_round_trips_in_shadow(tmp_path):
    (fills, cash, realized, still_open, _), _closes = await _run_real(tmp_path, "real.sqlite3")
    sides_ = [f[0] for f in fills]
    assert len(sides_) >= 4 and set(sides_) == {OrderSide.SELL_SHORT, OrderSide.BUY_TO_COVER}
    assert sides_[0::2] == [OrderSide.SELL_SHORT] * len(sides_[0::2])
    assert sides_[1::2] == [OrderSide.BUY_TO_COVER] * len(sides_[1::2])
    assert all(a[1] == b[1] for a, b in zip(fills[0::2], fills[1::2]))  # full-position covers
    assert not still_open or sides_[-1] is OrderSide.SELL_SHORT


async def test_the_real_short_run_is_deterministic_in_shadow(tmp_path):
    (first, *_), _ = await _run_real(tmp_path, "a.sqlite3")
    (again, *_), _ = await _run_real(tmp_path, "b.sqlite3")
    assert first == again


async def test_shadow_and_backtest_act_on_the_same_signal_bars_for_the_same_candles(tmp_path):
    """Strategy evaluation is engine independent: the same candles produce the same decisions.

    Execution differs by design (SHADOW fills at the quote, BACKTEST at the next bar open), so
    only the *bars on which orders are submitted* are compared, not prices or accounts.
    """
    import asyncio

    from test_short_cover_engine import (
        OPEN,
        REAL_IDS,
        REAL_PARAMS,
        SHORT_ON,
        _real_registry,
        run,
    )

    # an inert 40% stop in both engines: only strategy decisions can differ
    (_fills, _cash, _realized, _open, shadow_orders), closes = await _run_real(
        tmp_path, "parity.sqlite3", stop={"type": "fixed", "stop_percent": D("40")})
    bars = [(str(c), str(c), str(c), str(c)) for c in closes]
    backtest = await asyncio.get_running_loop().run_in_executor(
        None, lambda: run(
            bars, REAL_IDS, REAL_PARAMS,
            # the same limits SHADOW's scenario applies: 3 entries a session, a 60 s minimum hold
            config={**SHORT_ON, "protective_stop": {"type": "fixed", "stop_percent": "40"},
                    "max_entry_cycles_per_session": 3, "minimum_hold_seconds": 60},
            registry=_real_registry(), result_dir=tmp_path / "bt"))
    backtest_orders = [(row["signal_bar_time"], row["side"]) for row in backtest.intents]
    # SHADOW submits during the context whose newest completed bar is the signal bar; express
    # both engines as "index of the signal bar" so their different clocks are comparable
    start = Clock().value
    shadow_index = [int((when - start).total_seconds() // 60) - 1 for when, _ in shadow_orders]
    backtest_index = [int((t - OPEN).total_seconds() // 60) for t, _ in backtest_orders]
    assert [side.value for _, side in shadow_orders] == [side for _, side in backtest_orders]
    assert shadow_index == backtest_index and len(shadow_index) >= 4


# ------------------------------------------------------------------ accounting report


@pytest.mark.parametrize("close_price, gross", [("144", "114"), ("156", "-114")])
async def test_the_ledger_report_reconciles_with_zero_residual_after_a_short_round_trip(
    tmp_path, close_price, gross
):
    from mike_trade.reporting.accounting import ReportPeriod, build_account_ledger_report

    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})], covers=[("cover_alpha", {"fire_at": 6})]))
    try:
        for version in range(1, 5):
            await step(runtime, clock, version, bid="150", ask="150.1")
        for version in range(5, 8):
            await step(runtime, clock, version, bid=close_price, ask=str(D(close_price) + D("0.1")))
        assert "SPY" not in shadow.account.state.positions
        report = build_account_ledger_report(
            shadow.account.scope, ReportPeriod.LIFETIME, clock.value, shadow_ledger=shadow.ledger)
        state = shadow.account.state
        # 19 sh: short @150.00, cover @(close + 0.10): gross = (150 - cover) x 19
        assert state.realized_pnl == (D("150") - (D(close_price) + D("0.1"))) * 19
        assert report.ending_cash == state.cash
        assert report.net_realized_pnl == state.realized_pnl
        assert report.residual == D("0") and report.reconciled is True
    finally:
        await runtime.stop()


async def test_the_ledger_report_values_an_open_short_as_a_liability_at_the_marks(tmp_path):
    from mike_trade.reporting.accounting import ReportPeriod, build_account_ledger_report

    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, symbol(
        shorts=[("short_alpha", {"fire_at": 3})], covers=[("cover_alpha", {"fire_at": 99})]))
    try:
        for version in range(1, 5):
            await step(runtime, clock, version, bid="150", ask="150.1")
        marks = {"SPY": D("140")}
        report = build_account_ledger_report(
            shadow.account.scope, ReportPeriod.LIFETIME, clock.value,
            shadow_ledger=shadow.ledger, marks=marks)
        account = shadow.account
        assert report.ending_equity == account.equity(marks)      # cash - 19 x 140
        assert report.ending_equity == D("50000") + (D("150") - D("140")) * 19
        assert report.residual == D("0") and report.reconciled is True
        # the proceeds are restricted: reported buying power excludes the locked collateral
        assert report.account_buying_power == account.state.cash - account.state.restricted_collateral()
    finally:
        await runtime.stop()


# ------------------------------------------------------------ restrictions apply to both


async def test_the_session_entry_cap_is_shared_by_longs_and_shorts(tmp_path):
    clock = Clock()
    # the scenario allows 3 entry cycles a session: long, short, long use them all
    runtime, shadow = await start(tmp_path, clock, symbol(
        buys=[("buy_alpha", {"fire_at": 3, "fire_every": 10})],
        sells=[("sell_alpha", {"fire_at": 6, "fire_every": 10})],
        shorts=[("short_alpha", {"fire_at": 8, "fire_every": 10})],
        covers=[("cover_alpha", {"fire_at": 10, "fire_every": 10})]))
    try:
        refusals = []
        for version in range(1, 21):  # through the 4th entry attempt (a SHORT at bar 18)
            actions = await step(runtime, clock, version, seconds=120)
            refusals += [a for a in actions if a.startswith("ENTRY_REFUSED")]
        entries = [f for f in fills_of(shadow) if f[0].is_entry]
        # cycles: BUY@3, SHORT@8, BUY@13, then the 4th entry (SHORT@18) hits the shared cap
        assert [f[0] for f in entries] == [OrderSide.BUY, OrderSide.SELL_SHORT, OrderSide.BUY]
        assert shadow.account.state.filled_entry_cycles("SPY", _session_of(clock.value)) == 3
        assert len(refusals) == 1 and refusals[0].startswith("ENTRY_REFUSED:SHORT:")
        assert "SPY" not in shadow.account.state.positions
    finally:
        await runtime.stop()


def _session_of(moment):
    from mike_trade.brokers.shadow.account import _session_date

    return _session_date(moment)
