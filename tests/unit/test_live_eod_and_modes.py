"""PAPER/LIVE end-of-day liquidation, LIVE routing through a mocked broker, capability gating."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from scripted_strategies import build_registry
from tests.live_support import ctx, start_paper, write_capabilities
from tests.support.fake_alpaca import FakeAlpacaExchange, FakeClock

from mike_trade.app import AppSettings, build_runtime
from mike_trade.domain.errors import ConfigurationError, LiveTradingNotAuthorizedError
from mike_trade.live.capability_records import load_capability_matrix
from mike_trade.live.wiring import eod_offset
from mike_trade.runtime import ExecutionMode, RuntimeStartupError

BUY = [("buy_alpha", {"fire_at": 3, "fire_every": 1})]


def db(tmp_path: Path, sql: str, *args, name: str = "paper.sqlite3"):
    conn = sqlite3.connect(tmp_path / name)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def at(hour: int, minute: int, second: int = 0, day: date = date(2026, 10, 12)) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=UTC)


def market_sells(exchange):
    return [b for b in exchange.posts() if b["side"] == "sell" and b["type"] == "market"]


async def eod_runtime(tmp_path, exchange, **kw):
    runtime = await start_paper(
        tmp_path, exchange, [("buy_alpha", {"fire_at": 3})], [("sell_alpha", {"fire_at": 999})],
        registry=build_registry(), eod=True, **kw)
    await runtime.lifecycle.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
    assert exchange.position_qty("AAA") == D("29")
    return runtime


# ----------------------------------------------------------------------------- schedule

def test_eod_offset_is_the_distance_from_the_close_and_validates_its_range():
    assert eod_offset("15:55") == timedelta(minutes=5)
    assert eod_offset("15:30") == timedelta(minutes=30)
    for bad in ("16:00", "09:00", "17:30"):
        with pytest.raises(ConfigurationError):
            eod_offset(bad)


async def test_liquidation_starts_five_minutes_before_the_actual_close_including_early_closes(tmp_path):
    exchange = FakeAlpacaExchange(FakeClock(at(15, 0)))
    exchange.set_price("AAA", "100")
    runtime = await eod_runtime(tmp_path, exchange)
    try:
        life = runtime.lifecycle
        start, close = life.eod_time(date(2026, 10, 12))
        assert (start, close) == (at(19, 55), at(20, 0))        # 15:55 / 16:00 New York
        start, close = life.eod_time(date(2026, 11, 27))        # day after Thanksgiving
        assert (start, close) == (at(17, 55, day=date(2026, 11, 27)), at(18, 0, day=date(2026, 11, 27)))
        assert life.eod_time(date(2026, 10, 10)) is None         # Saturday: no session
    finally:
        await runtime.stop()


# --------------------------------------------------------------------------- liquidation

async def test_eod_closes_the_position_through_the_broker_and_blocks_new_entries(tmp_path):
    exchange = FakeAlpacaExchange(FakeClock(at(15, 0)))
    exchange.set_price("AAA", "100")
    runtime = await eod_runtime(tmp_path, exchange)
    try:
        life = runtime.lifecycle
        (stop,) = exchange.open_stops("AAA")
        exchange.clock.t = at(19, 54, 50)
        assert await life.eod_tick() == [] and not life.eod_active  # not yet 15:55
        exchange.clock.t = at(19, 55)
        exchange.set_price("AAA", "100.4")
        actions = await life.eod_tick()
        assert life.eod_active and any(a.startswith("EOD_CLOSE:AAA:FLAT") for a in actions)
        assert exchange.position_qty("AAA") == 0 and not exchange.open_orders("AAA")
        assert exchange.orders[stop["id"]]["status"] == "canceled"  # stop cancelled+confirmed first
        (sell,) = market_sells(exchange)
        assert sell["client_order_id"].startswith("mt1-ec-") and D(sell["qty"]) == D("29")
        assert db(tmp_path, "SELECT state FROM position_cycles") == [("CLOSED",)]
        assert db(tmp_path, "SELECT purpose FROM order_intents WHERE purpose = 'EOD_CLOSE'") == [("EOD_CLOSE",)]
        # the BUY strategy keeps signalling, but nothing new is ever opened after EOD began
        exchange.clock.advance(seconds=30)
        out = await life.process({"AAA": ctx("AAA", exchange.clock.now(), 5, "100.4")})
        assert out["live"] == [] and len([b for b in exchange.posts() if b["side"] == "buy"]) == 1
    finally:
        await runtime.stop()


async def test_eod_on_an_early_close_day_closes_at_the_early_close_offset(tmp_path):
    day = date(2026, 11, 27)
    exchange = FakeAlpacaExchange(FakeClock(at(15, 0, day=day)))
    exchange.set_price("AAA", "100")
    runtime = await eod_runtime(tmp_path, exchange)
    try:
        life = runtime.lifecycle
        exchange.clock.t = at(17, 54, 59, day=day)
        assert await life.eod_tick() == []
        exchange.clock.t = at(17, 55, day=day)
        assert any(a.startswith("EOD_CLOSE") for a in await life.eod_tick())
        assert exchange.position_qty("AAA") == 0
    finally:
        await runtime.stop()


async def test_eod_pending_and_partial_closing_orders_are_never_marked_closed_early(tmp_path):
    exchange = FakeAlpacaExchange(FakeClock(at(15, 0)))
    exchange.set_price("AAA", "100")
    runtime = await eod_runtime(tmp_path, exchange)
    try:
        life = runtime.lifecycle
        exchange.auto_fill = False
        exchange.clock.t = at(19, 55)
        actions = await life.eod_tick()
        assert any(a.startswith("EOD_CLOSE:AAA:SUBMITTED") for a in actions)
        (closing,) = [o for o in exchange.open_orders("AAA") if o["side"] == "sell"]
        assert exchange.position_qty("AAA") == D("29")
        assert db(tmp_path, "SELECT state FROM position_cycles") == [("OPEN",)]
        exchange.clock.advance(seconds=5)
        assert any(a.startswith("EOD_PENDING") for a in await life.eod_tick())  # no second sale
        exchange.partial_fill(closing["id"], 10, "100.1")
        await life.eod_tick()
        assert exchange.position_qty("AAA") == D("19")
        assert db(tmp_path, "SELECT state FROM position_cycles") == [("EXITING",)]
        assert len(market_sells(exchange)) == 1
        exchange.partial_fill(closing["id"], 19, "100.1")
        await life.eod_tick()
        assert exchange.position_qty("AAA") == 0
        assert db(tmp_path, "SELECT state FROM position_cycles") == [("CLOSED",)]
    finally:
        await runtime.stop()


async def test_a_failed_closing_order_reprotects_the_position_and_is_retried_next_tick(tmp_path):
    exchange = FakeAlpacaExchange(FakeClock(at(15, 0)))
    exchange.set_price("AAA", "100")
    runtime = await eod_runtime(tmp_path, exchange)
    try:
        life = runtime.lifecycle
        exchange.inject_failure("POST", "/v2/orders", status=403, times=0)  # placeholder (no-op)
        original = exchange.open_stops("AAA")[0]
        exchange.clock.t = at(19, 55)
        # the closing market order is rejected: the position must keep a stop, not go bare
        exchange._failures.clear()
        exchange.inject_failure("POST", "/v2/orders", status=422, times=1)
        # the failure is consumed by the (stop-cancel-confirmed) market sell POST
        first = await life.eod_tick()
        assert any(a.startswith("EOD_CLOSE:AAA:REPROTECTED") for a in first)
        assert exchange.position_qty("AAA") == D("29")
        (restored,) = exchange.open_stops("AAA")
        assert restored["id"] != original["id"] and D(restored["stop_price"]) == D("97.00")
        assert db(tmp_path, "SELECT state FROM position_cycles") == [("OPEN",)]
        exchange.clock.advance(seconds=5)
        second = await life.eod_tick()
        assert any(a.startswith("EOD_CLOSE:AAA:FLAT") for a in second)
        assert exchange.position_qty("AAA") == 0 and not exchange.open_orders("AAA")
    finally:
        await runtime.stop()


async def test_repeated_eod_failures_raise_an_alert_instead_of_silently_giving_up(tmp_path):
    exchange = FakeAlpacaExchange(FakeClock(at(15, 0)))
    exchange.set_price("AAA", "100")
    runtime = await eod_runtime(tmp_path, exchange)
    try:
        life = runtime.lifecycle
        life.settings = replace(life.settings, eod_max_attempts=1)
        exchange.clock.t = at(19, 55)
        exchange.inject_failure("POST", "/v2/orders", status=422, times=1)  # the closing sell only
        first = await life.eod_tick()
        assert any("REPROTECTED" in a for a in first)
        exchange.clock.advance(seconds=2)
        again = await life.eod_tick()
        assert again == ["EOD_FAILED:AAA"] and any("EOD close failed" in a for a in life.alerts)
        assert exchange.position_qty("AAA") == D("29") and len(exchange.open_stops("AAA")) == 1
    finally:
        await runtime.stop()


async def test_eod_cancels_a_working_entry_order(tmp_path):
    exchange = FakeAlpacaExchange(FakeClock(at(15, 0)))
    exchange.set_price("AAA", "100")
    exchange.auto_fill = False
    runtime = await start_paper(tmp_path, exchange, BUY, [], registry=build_registry(), eod=True)
    try:
        life = runtime.lifecycle
        await life.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
        (entry,) = exchange.open_orders("AAA")
        exchange.clock.t = at(19, 55)
        actions = await life.eod_tick()
        assert any(a.startswith("EOD_CANCEL_ENTRY") for a in actions)
        assert exchange.orders[entry["id"]]["status"] == "canceled" and exchange.position_qty("AAA") == 0
    finally:
        await runtime.stop()


# ------------------------------------------------------------------------- LIVE routing

async def test_live_without_explicit_authorization_cannot_even_build(tmp_path):
    with pytest.raises(LiveTradingNotAuthorizedError):
        build_runtime(AppSettings(mode=ExecutionMode.LIVE, db_path=tmp_path / "live.sqlite3"))


async def test_live_orders_and_protective_stops_route_through_the_live_endpoint_only(tmp_path):
    exchange = FakeAlpacaExchange()
    exchange.set_price("AAA", "100")
    runtime = await start_paper(
        tmp_path, exchange, [("buy_alpha", {"fire_at": 3})], [], registry=build_registry(),
        mode=ExecutionMode.LIVE, name="live.sqlite3")
    try:
        life = runtime.lifecycle
        out = await life.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
        assert "PROTECTED" in out["live"] and exchange.position_qty("AAA") == D("29")
        assert exchange.hosts == {"api.alpaca.markets"}          # never the paper endpoint
        assert [row[0] for row in db(tmp_path, "SELECT account_scope_id FROM account_scopes", name="live.sqlite3")] == ["live:live-test"]
        assert db(tmp_path, "SELECT DISTINCT account_scope_id FROM order_intents", name="live.sqlite3") == [("live:live-test",)]
        assert len(exchange.open_stops("AAA")) == 1
    finally:
        await runtime.stop()


async def test_live_mode_does_not_accept_paper_capability_evidence(tmp_path):
    exchange = FakeAlpacaExchange()
    exchange.set_price("AAA", "100")
    (tmp_path / "alpaca_capabilities.json").write_text(json.dumps({"records": [
        {"environment": "PAPER", "kind": "paper_verification", "reference": "PV-1",
         "recorded_on": "2026-10-12", "order_type": "stop", "sizing": "whole", "time_in_force": "gtc",
         "side": "sell", "operation": "submit"}]}))
    with pytest.raises(RuntimeStartupError, match="CAPABILITY_UNPROVEN:stop_submit"):
        await start_paper(tmp_path, exchange, [("buy_alpha", {"fire_at": 3})], [], registry=build_registry(),
                          mode=ExecutionMode.LIVE, name="live.sqlite3", capabilities=False)


# --------------------------------------------------------------------- capability records

def test_capability_records_promote_only_their_own_environment(tmp_path):
    path = tmp_path / "caps.json"
    write_capabilities(path, environment="PAPER")
    matrix = load_capability_matrix(path)
    assert matrix.as_legacy_checker("PAPER")("standalone_stop_sell:whole:gtc")
    assert not matrix.as_legacy_checker("LIVE")("standalone_stop_sell:whole:gtc")
    assert matrix.as_legacy_checker("PAPER")("replace_stop_price")
    assert load_capability_matrix(tmp_path / "missing.json").as_legacy_checker("PAPER")("standalone_stop_sell:whole:gtc") is False


@pytest.mark.parametrize("document", [
    "not json",
    json.dumps({"records": [{"environment": "LIVE", "kind": "paper_verification", "reference": "x",
                             "recorded_on": "2026-10-12", "order_type": "stop", "time_in_force": "gtc",
                             "side": "sell", "operation": "submit"}]}),
    json.dumps({"records": [{"environment": "PAPER", "kind": "paper_verification"}]}),
])
def test_malformed_capability_records_fail_closed(tmp_path, document):
    path = tmp_path / "caps.json"
    path.write_text(document)
    with pytest.raises(ConfigurationError) as raised:
        load_capability_matrix(path)
    assert raised.value.code == "CAPABILITY_RECORDS_INVALID"

