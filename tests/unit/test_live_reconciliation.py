"""Broker reconciliation: manual trades outside Mike_Trade, stop loss, failures and restarts.

A human using the Alpaca website / mobile app / DAS Trader on the SAME account leaves exactly the
footprints the exchange double's ``external_*`` helpers leave: orders Mike_Trade did not
place, executions, cancelled orders.  Broker positions and executions are authoritative.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal as D
from pathlib import Path

import pytest
from scripted_strategies import build_registry
from tests.live_support import ctx, start_paper
from tests.support.fake_alpaca import FakeAlpacaExchange

BUY = [("buy_alpha", {"fire_at": 3})]
SELL_LATER = [("sell_alpha", {"fire_at": 9})]


def db(tmp_path: Path, sql: str, *args):
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


async def held(tmp_path, exchange, sells=SELL_LATER, **kw):
    runtime = await start_paper(tmp_path, exchange, BUY, sells, registry=build_registry(), **kw)
    await runtime.lifecycle.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
    assert exchange.position_qty("AAA") == D("29")
    return runtime


def engine_sells(exchange):
    return [b for b in exchange.posts() if b["side"] == "sell" and b["type"] == "market"
            and not b["client_order_id"].startswith("web-")]


# ------------------------------------------------------------------ A. external full sale

async def test_external_full_sale_closes_the_cycle_cancels_protection_and_stops_exits(tmp_path, exchange):
    runtime = await held(tmp_path, exchange)
    try:
        life = runtime.lifecycle
        exchange.clock.advance(minutes=1)
        exchange.external_close_position("AAA", price="101")  # website "close position"
        assert exchange.position_qty("AAA") == 0 and not exchange.open_orders("AAA")
        report = await life.reconciler.reconcile("test")
        assert report.ok and len(report.external_sales) == 1
        ((state,),) = db(tmp_path, "SELECT state FROM position_cycles")
        assert state == "CLOSED"
        (event,) = db(tmp_path,
                      "SELECT payload_json FROM domain_events WHERE event_type = 'ExternalFillApplied'")
        assert '"strategy_exit":false' in event[0].replace(" ", "") and "external_full_exit" in event[0]
        ((status,),) = db(tmp_path, "SELECT status FROM trailing_stop_states")
        assert status in {"CANCELED", "TRIGGERED"}
        assert life.ledger.active_cycle("AAA") is None
        # SELL strategies no longer evaluate against a position, and nothing sells zero shares.
        for version in (9, 10):
            exchange.clock.advance(minutes=1)
            out = await life.process({"AAA": ctx("AAA", exchange.clock.now(), version, "101")})
            assert not any(a.startswith("EXIT") for a in out["live"])
        assert engine_sells(exchange) == []
        # P&L is preserved from the broker execution: (101 - 100) x 29, never a strategy win
        assert '"realized_pnl":"29"' in event[0].replace(" ", "")
        assert '"strategy_win":false' in event[0].replace(" ", "")
    finally:
        await runtime.stop()


# --------------------------------------------------------------- B. external partial sale

async def test_external_partial_sale_resizes_protection_and_caps_the_exit(tmp_path, exchange):
    runtime = await held(tmp_path, exchange, sells=[("sell_alpha", {"fire_at": 6})])
    try:
        life = runtime.lifecycle
        (stop,) = exchange.open_stops("AAA")
        exchange.clock.advance(minutes=1)
        # Alpaca will not sell shares a resting stop holds: the human cancels the stop first.
        with pytest.raises(Exception, match="insufficient qty"):
            exchange.external_sell("AAA", 11, "99")
        exchange.external_cancel(stop["id"])
        exchange.external_sell("AAA", 11, "99")
        report = await life.reconciler.reconcile("test")
        assert report.ok and len(report.external_sales) == 1
        state = life.reconciler.state("AAA")
        assert (state.managed_quantity, state.broker_quantity, state.protected) == (D("18"), D("18"), True)
        (new_stop,) = exchange.open_stops("AAA")
        assert new_stop["id"] != stop["id"] and D(new_stop["qty"]) == D("18")
        assert D(new_stop["stop_price"]) == D("97.00")  # never loosened
        ((cycle_state,),) = db(tmp_path, "SELECT state FROM position_cycles")
        assert cycle_state == "EXITING"  # 18 remain, still managed
        # the strategy exit sells exactly the 18 that remain - never an oversized 29
        exchange.clock.advance(minutes=5)
        exchange.set_price("AAA", "100")
        out = await life.process({"AAA": ctx("AAA", exchange.clock.now(), 6)})
        assert any(a.startswith("EXIT:FLAT") for a in out["live"])
        (sell,) = engine_sells(exchange)
        assert D(sell["qty"]) == D("18")
        assert exchange.position_qty("AAA") == 0
    finally:
        await runtime.stop()


# ----------------------------------------------------------------------- C. external buy

async def test_external_buy_is_recorded_but_never_adopted_into_the_managed_position(tmp_path, exchange):
    runtime = await held(tmp_path, exchange, sells=[("sell_alpha", {"fire_at": 6})])
    try:
        life = runtime.lifecycle
        exchange.clock.advance(minutes=1)
        exchange.external_buy("AAA", 40, "100.5")
        report = await life.reconciler.reconcile("test")
        state = life.reconciler.state("AAA")
        assert len(report.external_buys) == 1
        assert (state.managed_quantity, state.broker_quantity, state.unmanaged_quantity) == (
            D("29"), D("69"), D("40"))
        (stop,) = exchange.open_stops("AAA")
        assert D(stop["qty"]) == D("29")  # protection covers the managed shares only
        ((remaining,),) = db(tmp_path, "SELECT COUNT(*) FROM fills WHERE side = 'BUY'")
        assert remaining == 1  # only the engine's own BUY is in the trade ledger
        assert db(tmp_path, "SELECT activity_type FROM broker_activities") == [("EXTERNAL_BUY",)]
        again = await life.reconciler.reconcile("test")  # idempotent
        assert not again.external_buys
        assert db(tmp_path, "SELECT COUNT(*) FROM broker_activities") == [(1,)]
        # the engine's exit sells only what it manages and leaves the human's 40 shares alone
        exchange.clock.advance(minutes=5)
        await life.process({"AAA": ctx("AAA", exchange.clock.now(), 6)})
        (sell,) = engine_sells(exchange)
        assert D(sell["qty"]) == D("29") and exchange.position_qty("AAA") == D("40")
    finally:
        await runtime.stop()


async def test_a_symbol_the_human_already_holds_is_unmanaged_and_never_entered(tmp_path, exchange):
    exchange.positions["AAA"] = {"qty": D("50"), "avg": D("98")}  # opened outside Mike_Trade
    runtime = await start_paper(tmp_path, exchange, BUY, [], registry=build_registry())
    try:
        life = runtime.lifecycle
        assert life.reconciler.state("AAA").unmanaged_quantity == D("50")
        out = await life.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
        assert any(a.startswith("ENTRY_BLOCKED") for a in out["live"])
        assert exchange.posts() == [] and exchange.position_qty("AAA") == D("50")
    finally:
        await runtime.stop()


# ----------------------------------------------------------------- D. manual stop cancel

async def test_manual_stop_cancellation_is_detected_and_protection_restored(tmp_path, exchange):
    runtime = await held(tmp_path, exchange)
    try:
        life = runtime.lifecycle
        (stop,) = exchange.open_stops("AAA")
        exchange.external_cancel(stop["id"])
        assert exchange.open_stops("AAA") == []
        report = await life.reconciler.reconcile("test")
        assert any(a.startswith("PROTECTION_RESTORED") for a in report.actions)
        (restored,) = exchange.open_stops("AAA")
        assert restored["id"] != stop["id"]
        assert (D(restored["qty"]), D(restored["stop_price"])) == (D("29"), D("97.00"))
        again = await life.reconciler.reconcile("test")
        assert not again.actions and len(exchange.open_stops("AAA")) == 1  # no duplicate protection
    finally:
        await runtime.stop()


async def test_cancelled_stop_with_price_already_through_it_flattens_instead_of_leaving_it_bare(tmp_path, exchange):
    runtime = await held(tmp_path, exchange)
    try:
        life = runtime.lifecycle
        (stop,) = exchange.open_stops("AAA")
        exchange.external_cancel(stop["id"])
        exchange.prices["AAA"] = D("96")  # below the 97.00 stop: the exchange rejects a new stop
        report = await life.reconciler.reconcile("test")
        assert any("protection could not be maintained" in a or "SAFETY EXIT" in a for a in life.alerts)
        assert exchange.position_qty("AAA") == 0 and not exchange.open_orders("AAA")
        assert report.failures
        await life.reconciler.reconcile("test")
        ((state,),) = db(tmp_path, "SELECT state FROM position_cycles")
        assert state == "CLOSED"
    finally:
        await runtime.stop()


async def test_duplicate_protective_orders_are_cancelled_down_to_one(tmp_path, exchange):
    runtime = await held(tmp_path, exchange)
    try:
        life = runtime.lifecycle
        (stop,) = exchange.open_stops("AAA")
        # a second stop appears for the same cycle (e.g. a restarted instance racing a retry)
        exchange.orders[stop["id"]]["qty"] = "14"
        life.gateway.adapter._orders._outstanding.clear()  # as after a process restart
        duplicate = life.executor._place_stop(stop_cycle(life), "AAA", D("15"), D("96.50"))
        assert len(exchange.open_stops("AAA")) == 2
        report = await life.reconciler.reconcile("test")
        assert any(a.startswith("ORDER_CANCELED") and "DUPLICATE_PROTECTION" in a for a in report.actions)
        assert len(exchange.open_stops("AAA")) == 1 and duplicate.broker_order_id
    finally:
        await runtime.stop()


def stop_cycle(life):
    return life.ledger.active_cycle("AAA").cycle_id


# ------------------------------------------------- E. external flatten with a pending order

async def test_external_flatten_while_our_exit_is_pending_reconciles_without_a_second_sale(tmp_path, exchange):
    runtime = await held(tmp_path, exchange, sells=[("sell_alpha", {"fire_at": 6})])
    try:
        life = runtime.lifecycle
        exchange.auto_fill = False  # our exit stays working at the broker
        exchange.clock.advance(minutes=5)
        out = await life.process({"AAA": ctx("AAA", exchange.clock.now(), 6)})
        assert any(a.startswith("EXIT:SUBMITTED") for a in out["live"])
        (pending,) = [o for o in exchange.open_orders("AAA") if o["side"] == "sell"]
        assert pending["type"] == "market" and exchange.position_qty("AAA") == D("29")
        exchange.auto_fill = True
        exchange.external_close_position("AAA", price="100.5")  # cancels our pending sell, sells all
        assert exchange.orders[pending["id"]]["status"] == "canceled"
        report = await life.reconciler.reconcile("test")
        assert report.ok and len(report.external_sales) == 1
        ((state,),) = db(tmp_path, "SELECT state FROM position_cycles")
        assert state == "CLOSED"
        exchange.clock.advance(minutes=1)
        await life.process({"AAA": ctx("AAA", exchange.clock.now(), 7)})
        assert len(engine_sells(exchange)) == 1  # the original (cancelled) exit; never a repeat
        assert exchange.position_qty("AAA") == 0
    finally:
        await runtime.stop()
