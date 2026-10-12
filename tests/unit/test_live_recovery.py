"""Restart recovery, broker/API failure handling, unknown submit outcomes, stream wake-ups."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from scripted_strategies import build_registry
from tests.live_support import ctx, start_paper
from tests.support.fake_alpaca import FakeAlpacaExchange, trade_update_frames

from mike_trade.brokers.alpaca.auth import AlpacaCredentials, AlpacaEnvironment
from mike_trade.brokers.alpaca.updates import TradeUpdateStream
from mike_trade.live.stream import TradeUpdateWatcher

BUY = [("buy_alpha", {"fire_at": 3})]


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


async def open_position(tmp_path, exchange, **kw):
    runtime = await start_paper(tmp_path, exchange, BUY, [("sell_alpha", {"fire_at": 99})],
                                registry=build_registry(), **kw)
    await runtime.lifecycle.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
    return runtime


def stop_posts(exchange):
    return [b for b in exchange.posts() if b["type"] == "stop"]


# ----------------------------------------------------------------------- restart recovery

async def test_restart_readopts_the_position_and_keeps_the_same_single_stop(tmp_path, exchange):
    first = await open_position(tmp_path, exchange)
    (stop,) = exchange.open_stops("AAA")
    await first.stop()
    second = await start_paper(tmp_path, exchange, BUY, [("sell_alpha", {"fire_at": 99})],
                               registry=build_registry())
    try:
        life = second.lifecycle
        state = life.reconciler.state("AAA")
        assert (state.managed_quantity, state.protected) == (D("29"), True)
        assert [o["id"] for o in exchange.open_stops("AAA")] == [stop["id"]]
        assert len(stop_posts(exchange)) == 1 and len([b for b in exchange.posts() if b["side"] == "buy"]) == 1
        # a later bar neither re-enters nor re-protects
        exchange.clock.advance(minutes=1)
        out = await life.process({"AAA": ctx("AAA", exchange.clock.now(), 4)})
        assert not any(a.startswith("ENTRY") for a in out["live"]) and len(stop_posts(exchange)) == 1
    finally:
        await second.stop()


async def test_restart_restores_a_stop_cancelled_while_the_engine_was_down(tmp_path, exchange):
    first = await open_position(tmp_path, exchange)
    (stop,) = exchange.open_stops("AAA")
    await first.stop()
    exchange.external_cancel(stop["id"])
    second = await start_paper(tmp_path, exchange, BUY, [("sell_alpha", {"fire_at": 99})],
                               registry=build_registry())
    try:
        (restored,) = exchange.open_stops("AAA")
        assert restored["id"] != stop["id"] and D(restored["stop_price"]) == D("97.00")
        assert D(restored["qty"]) == D("29")
    finally:
        await second.stop()


async def test_restart_after_a_manual_close_while_down_closes_the_cycle_and_leaves_nothing_open(tmp_path, exchange):
    first = await open_position(tmp_path, exchange)
    await first.stop()
    exchange.external_close_position("AAA", price="100.2")
    second = await start_paper(tmp_path, exchange, BUY, [], registry=build_registry())
    try:
        assert db(tmp_path, "SELECT state FROM position_cycles") == [("CLOSED",)]
        assert exchange.open_orders("AAA") == [] and second.lifecycle.ledger.active_cycle("AAA") is None
    finally:
        await second.stop()


async def test_restart_with_a_pending_entry_order_neither_duplicates_nor_loses_it(tmp_path, exchange):
    exchange.auto_fill = False
    first = await start_paper(tmp_path, exchange, [("buy_alpha", {"fire_at": 3, "fire_every": 1})], [],
                              registry=build_registry())
    await first.lifecycle.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
    (entry,) = [o for o in exchange.orders.values() if o["side"] == "buy"]
    await first.stop()
    second = await start_paper(tmp_path, exchange, [("buy_alpha", {"fire_at": 3, "fire_every": 1})], [],
                               registry=build_registry())
    try:
        life = second.lifecycle
        assert life.reconciler.state("AAA").entry_pending
        for version in (4, 5):
            exchange.clock.advance(minutes=1)
            await life.process({"AAA": ctx("AAA", exchange.clock.now(), version)})
        assert len([b for b in exchange.posts() if b["side"] == "buy"]) == 1
        exchange.partial_fill(entry["id"], 29)
        await life.reconciler.reconcile("test")
        assert exchange.position_qty("AAA") == D("29") and len(exchange.open_stops("AAA")) == 1
    finally:
        await second.stop()


# ----------------------------------------------------------- broker / API failure recovery

async def test_a_failed_broker_read_changes_nothing_and_blocks_decisions_until_it_recovers(tmp_path, exchange):
    runtime = await start_paper(tmp_path, exchange, BUY, [], registry=build_registry())
    try:
        life = runtime.lifecycle
        exchange.inject_failure("GET", "/v2/orders", status=403, times=2)
        report = await life.reconciler.reconcile("test")
        assert not report.ok and life.reconciler.entries_blocked_reasons == ["BROKER_READ_FAILED"]
        out = await life.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
        assert out["live"] == ["ENTRY_SKIPPED:STALE_HOLDINGS"]
        assert exchange.posts() == [] and db(tmp_path, "SELECT COUNT(*) FROM position_cycles") == [(0,)]
        recovered = await life.reconciler.reconcile("test")
        assert recovered.ok and life.reconciler.entries_blocked_reasons == []
        out = await life.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
        assert any(a.startswith("ENTRY:") for a in out["live"])
    finally:
        await runtime.stop()


async def test_an_exit_is_never_decided_from_holdings_that_could_not_be_verified(tmp_path, exchange):
    runtime = await start_paper(tmp_path, exchange, BUY, [("sell_alpha", {"fire_at": 6})],
                                registry=build_registry())
    try:
        life = runtime.lifecycle
        await life.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
        exchange.clock.advance(minutes=3)
        exchange.inject_failure("GET", "/v2/positions", status=403, times=1)
        out = await life.process({"AAA": ctx("AAA", exchange.clock.now(), 6)})
        assert out["live"] == ["EXIT_SKIPPED:STALE_HOLDINGS"]
        assert exchange.position_qty("AAA") == D("29") and len(exchange.open_stops("AAA")) == 1
        again = await life.process({"AAA": ctx("AAA", exchange.clock.now(), 6)})
        assert any(a.startswith("EXIT:FLAT") for a in again["live"])
    finally:
        await runtime.stop()


async def test_an_unknown_submit_outcome_that_reached_the_broker_is_adopted_not_duplicated(tmp_path, exchange):
    runtime = await start_paper(tmp_path, exchange, BUY, [], registry=build_registry())
    try:
        # the POST lands at the broker but the response is lost (timeout)
        exchange.inject_failure("POST", "/v2/orders", timeout=True, after_effect=True)
        out = await runtime.lifecycle.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
        assert any(a.startswith("ENTRY:") for a in out["live"])
        assert len([o for o in exchange.orders.values() if o["side"] == "buy"]) == 1
        assert exchange.position_qty("AAA") == D("29") and len(exchange.open_stops("AAA")) == 1
        assert db(tmp_path, "SELECT status FROM order_intents ORDER BY created_at, rowid")[0] == ("SUBMITTED",)
    finally:
        await runtime.stop()


async def test_an_unresolved_submit_outcome_is_reconciled_later_and_never_abandoned_blindly(tmp_path, exchange):
    runtime = await start_paper(tmp_path, exchange, BUY, [], registry=build_registry())
    try:
        life = runtime.lifecycle
        exchange.inject_failure("POST", "/v2/orders", timeout=True, after_effect=True)
        exchange.inject_failure("GET", "orders:by_client_order_id", status=403, times=1)
        out = await life.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
        assert out["live"] == ["ENTRY_UNKNOWN"]
        assert exchange.position_qty("AAA") == D("29")  # the order DID reach the broker
        assert db(tmp_path, "SELECT status FROM order_intents") == [("UNKNOWN",)]
        assert db(tmp_path, "SELECT state FROM position_cycles") == [("PENDING_ENTRY",)]
        report = await life.reconciler.reconcile("test")  # lookup by client_order_id now succeeds
        assert any(a.startswith("INTENT_RESOLVED_FOUND") for a in report.actions)
        assert db(tmp_path, "SELECT status FROM order_intents ORDER BY created_at, rowid")[0] == ("SUBMITTED",)
        assert len(exchange.open_stops("AAA")) == 1
        assert db(tmp_path, "SELECT state FROM position_cycles") == [("OPEN",)]
    finally:
        await runtime.stop()


async def test_an_unknown_outcome_with_no_order_at_the_broker_is_resolved_absent_after_the_grace(tmp_path, exchange):
    runtime = await start_paper(tmp_path, exchange, BUY, [], registry=build_registry())
    try:
        life = runtime.lifecycle
        exchange.inject_failure("POST", "/v2/orders", timeout=True)  # request never reached the broker
        exchange.inject_failure("GET", "orders:by_client_order_id", status=403, times=1)
        out = await life.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
        assert out["live"] == ["ENTRY_UNKNOWN"] and not exchange.orders
        await life.reconciler.reconcile("test")  # inside the grace: still unresolved
        assert db(tmp_path, "SELECT status FROM order_intents") == [("UNKNOWN",)]
        exchange.clock.advance(seconds=45)
        report = await life.reconciler.reconcile("test")
        assert any(a.startswith("INTENT_RESOLVED_ABSENT") for a in report.actions)
        assert db(tmp_path, "SELECT status FROM order_intents") == [("SUBMIT_FAILED",)]
        assert db(tmp_path, "SELECT state FROM position_cycles") == [("ABANDONED",)]
        assert db(tmp_path, "SELECT COUNT(*) FROM budget_reservations WHERE state = 'ACTIVE'") == [(0,)]
    finally:
        await runtime.stop()


async def test_ingestion_is_idempotent_across_passes_and_overlapping_activity_windows(tmp_path, exchange):
    runtime = await open_position(tmp_path, exchange)
    try:
        life = runtime.lifecycle
        for _ in range(3):
            await life.reconciler.reconcile("test")
        assert db(tmp_path, "SELECT COUNT(*) FROM fills") == [(1,)]
        exchange.set_price("AAA", "96")
        for _ in range(3):
            await life.reconciler.reconcile("test")
        assert db(tmp_path, "SELECT side FROM fills ORDER BY filled_at, rowid") == [("BUY",), ("SELL",)]
    finally:
        await runtime.stop()


# --------------------------------------------------------------------- stream wake-ups

class _Socket:
    def __init__(self, frames: list[str]) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        for frame in frames:
            self.queue.put_nowait(frame)
        self.sent: list[dict] = []

    async def connect(self, _url: str):
        return self

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def recv(self) -> str:
        return await self.queue.get()

    async def close(self) -> None:
        return None


async def test_trade_update_stream_wakes_the_reconciler_within_the_event_not_the_poll_interval(tmp_path, exchange):
    runtime = await open_position(tmp_path, exchange)
    try:
        life = runtime.lifecycle
        life.reconciler.interval = 3600.0  # polling alone would take an hour
        life.reconciler.start()
        await asyncio.sleep(0)
        socket = _Socket([
            json.dumps({"stream": "authorization", "data": {"status": "authorized", "action": "authenticate"}}),
            json.dumps({"stream": "listening", "data": {"streams": ["trade_updates"]}}),
        ])
        credentials = AlpacaCredentials("PKTEST", "sk-test", AlpacaEnvironment.PAPER)
        watcher = TradeUpdateWatcher(lambda: TradeUpdateStream(credentials, socket), life.reconciler.kick,
                                     retry_delay=0.01)
        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.05)
        before = len(db(tmp_path, "SELECT 1 FROM fills"))
        exchange.external_close_position("AAA", price="100.4")
        for frame in trade_update_frames(exchange)[-3:]:
            socket.queue.put_nowait(frame)
        for _ in range(100):
            if db(tmp_path, "SELECT state FROM position_cycles") == [("CLOSED",)]:
                break
            await asyncio.sleep(0.02)
        task.cancel()
        assert db(tmp_path, "SELECT state FROM position_cycles") == [("CLOSED",)]
        assert watcher.updates >= 1 and len(db(tmp_path, "SELECT 1 FROM fills")) == before + 1
        assert socket.sent[0]["action"] == "auth" and socket.sent[1]["action"] == "listen"
    finally:
        await runtime.stop()
