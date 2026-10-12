"""PAPER BUY / SELL lifecycle on the real runtime graph against a simulated Alpaca exchange.

The exchange double speaks Alpaca's REST dialect through the production HTTP client, order and
account services, and adapter; nothing here opens a network connection or needs credentials.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from scripted_strategies import build_registry
from tests.live_support import ctx, start_paper
from tests.support.fake_alpaca import FakeAlpacaExchange

from mike_trade.domain.protection import ProtectionKind, ProtectionPolicy
from mike_trade.runtime import StartupState

BUY = [("buy_alpha", {"fire_at": 3})]


def rows(tmp_path: Path, sql: str, *args):
    conn = sqlite3.connect(tmp_path / "paper.sqlite3")
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


@pytest.fixture
def exchange() -> FakeAlpacaExchange:
    ex = FakeAlpacaExchange()
    ex.set_price("AAA", "100")
    return ex


async def test_paper_runtime_starts_with_stop_capabilities_recorded(tmp_path, exchange):
    runtime = await start_paper(tmp_path, exchange, BUY, [], registry=build_registry())
    try:
        assert runtime.state is StartupState.RUNNING
        assert runtime.lifecycle is not None and runtime.execution is runtime.lifecycle
    finally:
        await runtime.stop()


async def test_paper_runtime_refuses_to_start_when_stop_capability_is_unproven(tmp_path, exchange):
    from mike_trade.runtime import RuntimeStartupError

    with pytest.raises(RuntimeStartupError, match="CAPABILITY_UNPROVEN:stop_submit"):
        await start_paper(tmp_path, exchange, BUY, [], registry=build_registry(), capabilities=False)


async def test_buy_fill_is_protected_with_a_broker_held_stop_for_the_filled_quantity(tmp_path, exchange):
    runtime = await start_paper(tmp_path, exchange, BUY, [], registry=build_registry())
    try:
        actions = await runtime.lifecycle.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
        assert actions["live"][:2] == [actions["live"][0], "FILL:BUY:29"] and "PROTECTED" in actions["live"]
        assert exchange.position_qty("AAA") == D("29")
        (stop,) = exchange.open_stops("AAA")
        assert (stop["side"], stop["time_in_force"], D(stop["qty"]), D(stop["stop_price"])) == (
            "sell", "gtc", D("29"), D("97.00"))
        # durable accounting through the shared ledger
        ((state, remaining),) = rows(tmp_path, "SELECT state, 0 FROM position_cycles")
        assert state == "OPEN"
        (fill,) = rows(tmp_path, "SELECT side, quantity, price FROM fills")
        assert fill == ("BUY", "29", "100")
        ((kind, status, covered, price),) = rows(
            tmp_path, "SELECT kind, status, covered_quantity, stop_price FROM trailing_stop_states")
        assert (kind, status, D(covered), D(price)) == ("FIXED_STOP", "ACTIVE", D("29"), D("97.00"))
    finally:
        await runtime.stop()


async def test_no_pyramiding_while_held_or_on_signal_replay(tmp_path, exchange):
    runtime = await start_paper(
        tmp_path, exchange, [("buy_alpha", {"fire_at": 3, "fire_every": 1})], [], registry=build_registry())
    try:
        life = runtime.lifecycle
        await life.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
        for version in (3, 4, 5):
            exchange.clock.advance(minutes=1)
            await life.process({"AAA": ctx("AAA", exchange.clock.now(), version)})
        buys = [b for b in exchange.posts() if b["side"] == "buy"]
        assert len(buys) == 1 and exchange.position_qty("AAA") == D("29")
        assert len(exchange.open_stops("AAA")) == 1
    finally:
        await runtime.stop()


async def test_strategy_exit_cancels_confirms_then_sells_the_full_remainder(tmp_path, exchange):
    runtime = await start_paper(
        tmp_path, exchange, BUY, [("sell_alpha", {"fire_at": 5})], registry=build_registry())
    try:
        life = runtime.lifecycle
        await life.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
        (stop,) = exchange.open_stops("AAA")
        exchange.clock.advance(minutes=2)
        exchange.set_price("AAA", "101")
        out = await life.process({"AAA": ctx("AAA", exchange.clock.now(), 5, "101")})
        assert any(a.startswith("EXIT:FLAT") for a in out["live"])
        assert exchange.position_qty("AAA") == 0 and not exchange.open_orders("AAA")
        assert exchange.orders[stop["id"]]["status"] == "canceled"  # confirmed before the sell
        sells = [b for b in exchange.posts() if b["side"] == "sell" and b["type"] == "market"]
        assert len(sells) == 1 and D(sells[0]["qty"]) == D("29")
        ((state,),) = rows(tmp_path, "SELECT state FROM position_cycles")
        assert state == "CLOSED"
        ((status,),) = rows(tmp_path, "SELECT status FROM trailing_stop_states")
        assert status in {"CANCELED", "TRIGGERED"}
        # realized P&L is recorded from the broker fills (101-100)*29
        fills = rows(tmp_path, "SELECT side, quantity, price FROM fills ORDER BY filled_at, rowid")
        assert [f[0] for f in fills] == ["BUY", "SELL"] and fills[1][2] == "101"
    finally:
        await runtime.stop()


async def test_broker_held_stop_fills_when_price_falls_and_the_cycle_closes_from_the_fill(tmp_path, exchange):
    runtime = await start_paper(tmp_path, exchange, BUY, [], registry=build_registry())
    try:
        life = runtime.lifecycle
        await life.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
        exchange.clock.advance(minutes=1)
        exchange.set_price("AAA", "96.5")  # through the 97.00 stop; engine is not even running a bar
        assert exchange.position_qty("AAA") == 0
        report = await life.reconciler.reconcile("test")
        assert report.ok and len(report.applied_fills) == 1
        ((state,),) = rows(tmp_path, "SELECT state FROM position_cycles")
        assert state == "CLOSED"
        assert life.ledger.active_cycle("AAA") is None
        assert not report.external_sales  # the stop is the engine's own order, not a manual one
    finally:
        await runtime.stop()


async def test_rejected_entry_leaves_no_position_cycle_or_reservation(tmp_path, exchange):
    runtime = await start_paper(tmp_path, exchange, BUY, [], registry=build_registry())
    try:
        exchange.inject_failure("POST", "/v2/orders", status=422)
        out = await runtime.lifecycle.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
        assert any(a.startswith("ENTRY_FAILED") for a in out["live"])
        assert exchange.position_qty("AAA") == 0 and not exchange.orders
        ((state,),) = rows(tmp_path, "SELECT state FROM position_cycles")
        assert state == "ABANDONED"
        assert rows(tmp_path, "SELECT state FROM budget_reservations WHERE state = 'ACTIVE'") == []
        ((status,),) = rows(tmp_path, "SELECT status FROM order_intents")
        assert status == "SUBMIT_FAILED"
    finally:
        await runtime.stop()


async def test_partial_entry_fill_protects_what_filled_then_grows_with_the_rest(tmp_path, exchange):
    exchange.auto_fill = False
    runtime = await start_paper(tmp_path, exchange, BUY, [], registry=build_registry())
    try:
        life = runtime.lifecycle
        await life.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
        (entry,) = [o for o in exchange.orders.values() if o["side"] == "buy"]
        exchange.partial_fill(entry["id"], 10)
        await life.reconciler.reconcile("test")
        (stop,) = exchange.open_stops("AAA")
        assert D(stop["qty"]) == D("10")
        exchange.partial_fill(entry["id"], 19)
        await life.reconciler.reconcile("test")
        (stop2,) = exchange.open_stops("AAA")
        assert D(stop2["qty"]) == D("29") and D(stop2["stop_price"]) == D("97.00")
        assert len(exchange.open_orders("AAA")) == 1
    finally:
        await runtime.stop()
