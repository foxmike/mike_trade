"""Broker-native protective stops: Michelle fixed-to-trailing, ratchets, trailing policies."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from scripted_strategies import build_registry
from test_michelle_strategies import OPEN, context, daily_history, orb_bars
from tests.live_support import ctx, start_paper
from tests.support.fake_alpaca import FakeAlpacaExchange, FakeClock

from mike_trade.domain.protection import ProtectionKind, ProtectionPolicy
from mike_trade.live.executor import floor_to_tick, policy_from_dict, policy_to_dict, ratchet_candidate


def db(tmp_path: Path, sql: str, *args):
    conn = sqlite3.connect(tmp_path / "paper.sqlite3")
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ----------------------------------------------------------------------- pure mechanics

ORB = ProtectionPolicy(
    ProtectionKind.FIXED_THEN_TRAILING_STOP, trail_percent=D("0.5"),
    initial_stop_price=D("100.35"), activation_reference=D("100.85"),
)


@pytest.mark.parametrize(
    ("high", "expected"),
    [
        ("100.65", None),            # entry price: 0.995*H = 100.147 < reference + 0.25
        ("100.85", None),
        ("101.35", None),            # 0.995*101.35 = 100.843 -> NOT above 100.85
        ("101.36", D("100.8532")),   # 0.995*101.36 = 100.8532 > 100.85: activates
        ("102", D("101.49")),
        ("105", D("104.475")),
    ],
)
def test_michelle_trail_activates_only_when_995_percent_of_the_high_clears_reference_plus_25c(high, expected):
    assert ratchet_candidate(ORB, D("100.65"), D(high)) == expected


def test_stepped_policy_candidates_are_a_pure_function_of_the_high_watermark():
    policy = ProtectionPolicy(
        ProtectionKind.STEPPED_TRAILING_STOP, trail_amount=D("1"), step_size=D("0.5"),
        activation_gain=D("1"),
    )
    assert ratchet_candidate(policy, D("100"), D("100.9")) is None  # below activation
    assert ratchet_candidate(policy, D("100"), D("101")) == D("100.0")
    assert ratchet_candidate(policy, D("100"), D("102")) == D("101.0")
    assert ratchet_candidate(policy, D("100"), D("102.4")) == D("101.0")  # sub-step: no reprice


def test_fixed_and_native_trailing_policies_have_no_engine_ratchet():
    fixed = ProtectionPolicy(ProtectionKind.FIXED_STOP, stop_offset_percent=D("3"))
    native = ProtectionPolicy(ProtectionKind.BROKER_TRAILING_STOP, trail_percent=D("1.5"))
    assert ratchet_candidate(fixed, D("100"), D("120")) is None
    assert ratchet_candidate(native, D("100"), D("120")) is None


def test_stop_prices_floor_to_the_tick_and_policies_round_trip_through_storage():
    assert floor_to_tick(D("100.8532"), D("0.01")) == D("100.85")
    assert policy_from_dict(policy_to_dict(ORB)) == ORB


# ----------------------------------------------------------- ORB Michelle on a PAPER account

SIGNAL_AT = OPEN + timedelta(minutes=6)


def orb_context(at):
    return context(orb_bars(), daily=daily_history(), at=at)


async def michelle(tmp_path, exchange, name="paper.sqlite3"):
    return await start_paper(
        tmp_path, exchange,
        [("five_minute_orb_michelle_buy", {})], [("five_minute_orb_michelle_sell", {})],
        symbol="ABC", name=name,
    )


def abc_exchange() -> FakeAlpacaExchange:
    exchange = FakeAlpacaExchange(FakeClock(SIGNAL_AT))
    exchange.set_price("ABC", "100.65")
    return exchange


async def settle(times: int = 30) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


async def test_michelle_orb_entry_is_protected_at_orb_high_minus_25c_then_trails_monotonically(tmp_path):
    exchange = abc_exchange()
    runtime = await michelle(tmp_path, exchange)
    try:
        life = runtime.lifecycle
        out = await life.process({"ABC": orb_context(SIGNAL_AT)})
        assert any(a.startswith("FILL:BUY") for a in out["live"]) and "PROTECTED" in out["live"]
        (stop,) = exchange.open_stops("ABC")
        assert (D(stop["stop_price"]), stop["time_in_force"], D(stop["qty"])) == (D("100.35"), "gtc", D("29"))

        # below activation the stop stays fixed
        exchange.clock.advance(seconds=5)
        life.observe_trade("ABC", D("101.20"), exchange.clock.now())
        await settle()
        assert [D(o["stop_price"]) for o in exchange.open_stops("ABC")] == [D("100.35")]

        # 0.995 x 101.40 = 100.893 > 100.85: the trail activates; the stop moves up via replace
        exchange.clock.advance(seconds=5)
        life.observe_trade("ABC", D("101.40"), exchange.clock.now())
        await settle()
        (tightened,) = exchange.open_stops("ABC")
        assert D(tightened["stop_price"]) == D("100.89") and tightened["id"] != stop["id"]
        assert exchange.orders[stop["id"]]["status"] == "replaced"

        # a pullback never loosens it; a new high tightens again
        exchange.clock.advance(seconds=5)
        life.observe_trade("ABC", D("100.95"), exchange.clock.now())
        await settle()
        assert [D(o["stop_price"]) for o in exchange.open_stops("ABC")] == [D("100.89")]
        exchange.clock.advance(seconds=5)
        life.observe_trade("ABC", D("102.00"), exchange.clock.now())
        await settle()
        assert [D(o["stop_price"]) for o in exchange.open_stops("ABC")] == [D("101.49")]
        # exactly one protective order is ever working
        assert len(exchange.open_orders("ABC")) == 1
        ((kind, price, high),) = db(
            tmp_path, "SELECT kind, stop_price, high_water_mark FROM trailing_stop_states")
        assert (kind, D(price), D(high)) == ("FIXED_THEN_TRAILING_STOP", D("101.49"), D("102.00"))
    finally:
        await runtime.stop()


async def test_michelle_trailing_stop_fills_at_the_broker_and_the_cycle_closes_from_the_fill(tmp_path):
    exchange = abc_exchange()
    runtime = await michelle(tmp_path, exchange)
    try:
        life = runtime.lifecycle
        await life.process({"ABC": orb_context(SIGNAL_AT)})
        exchange.clock.advance(seconds=5)
        life.observe_trade("ABC", D("102"), exchange.clock.now())
        await settle()
        exchange.clock.advance(seconds=5)
        exchange.set_price("ABC", "101.40")  # through the 101.49 trailing stop
        assert exchange.position_qty("ABC") == 0
        report = await life.reconciler.reconcile("test")
        assert len(report.applied_fills) == 1 and not report.external_sales
        assert db(tmp_path, "SELECT state FROM position_cycles") == [("CLOSED",)]
        assert [b for b in exchange.posts() if b["type"] == "market" and b["side"] == "sell"] == []
    finally:
        await runtime.stop()


async def test_restart_resumes_the_michelle_trail_from_the_persisted_high_water_mark(tmp_path):
    exchange = abc_exchange()
    first = await michelle(tmp_path, exchange)
    life = first.lifecycle
    await life.process({"ABC": orb_context(SIGNAL_AT)})
    exchange.clock.advance(seconds=5)
    life.observe_trade("ABC", D("102"), exchange.clock.now())
    await settle()
    (stop,) = exchange.open_stops("ABC")
    assert D(stop["stop_price"]) == D("101.49")
    await first.stop()

    second = await michelle(tmp_path, exchange)
    try:
        life2 = second.lifecycle
        runtime = life2.executor.stops[life2.ledger.active_cycle("ABC").cycle_id]
        assert (runtime.stop_price, runtime.high, runtime.order_id) == (D("101.49"), D("102.00"), stop["id"])
        assert [o["id"] for o in exchange.open_stops("ABC")] == [stop["id"]]  # nothing re-placed
        exchange.clock.advance(seconds=5)
        life2.observe_trade("ABC", D("103"), exchange.clock.now())
        await settle()
        assert [D(o["stop_price"]) for o in exchange.open_stops("ABC")] == [D("102.48")]
        # a restart must never lower the stop when the new session's first high is lower
        exchange.clock.advance(seconds=5)
        life2.observe_trade("ABC", D("101"), exchange.clock.now())
        await settle()
        assert [D(o["stop_price"]) for o in exchange.open_stops("ABC")] == [D("102.48")]
    finally:
        await second.stop()


async def test_an_unconfirmed_replace_leaves_the_previous_stop_working_and_is_retried(tmp_path):
    exchange = abc_exchange()
    runtime = await michelle(tmp_path, exchange)
    try:
        life = runtime.lifecycle
        await life.process({"ABC": orb_context(SIGNAL_AT)})
        (stop,) = exchange.open_stops("ABC")
        exchange.inject_failure("PATCH", "/v2/orders/", status=500)
        exchange.clock.advance(seconds=5)
        life.observe_trade("ABC", D("102"), exchange.clock.now())
        await settle()
        # no protection gap, no duplicate: the old stop still protects every share
        assert [o["id"] for o in exchange.open_stops("ABC")] == [stop["id"]]
        assert D(exchange.open_stops("ABC")[0]["stop_price"]) == D("100.35")
        exchange.clock.advance(seconds=5)
        life.observe_trade("ABC", D("102.01"), exchange.clock.now())
        await settle()
        (retried,) = exchange.open_stops("ABC")
        assert D(retried["stop_price"]) == D("101.49") and len(exchange.open_orders("ABC")) == 1
    finally:
        await runtime.stop()


async def test_a_strategy_exit_of_a_trailing_position_confirms_the_cancel_of_the_current_stop_first(tmp_path):
    exchange = FakeAlpacaExchange()
    exchange.set_price("AAA", "100")
    runtime = await start_paper(
        tmp_path, exchange, [("buy_alpha", {"fire_at": 3})], [("sell_alpha", {"fire_at": 7})],
        registry=build_registry(), stop={"type": "stepped_trailing", "initial_stop_percent": D("2"),
                                          "step_trigger_percent": D("1"), "step_percent": D("0.5")},
    )
    try:
        life = runtime.lifecycle
        await life.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
        (stop,) = exchange.open_stops("AAA")
        assert D(stop["stop_price"]) == D("98.00")
        exchange.clock.advance(minutes=1)
        exchange.set_price("AAA", "102.5")
        await life.process({"AAA": ctx("AAA", exchange.clock.now(), 4, "102.5")})
        # stepped trail: distance 2.00, step 0.50; high 102.7 (bar high) -> anchor 102.5 -> stop 100.50
        (moved,) = exchange.open_stops("AAA")
        assert D(moved["stop_price"]) > D("98.00") and moved["id"] != stop["id"]
        exchange.clock.advance(minutes=3)
        out = await life.process({"AAA": ctx("AAA", exchange.clock.now(), 7, "102.5")})
        assert any(a.startswith("EXIT:FLAT") for a in out["live"]) and exchange.position_qty("AAA") == 0
        assert exchange.orders[moved["id"]]["status"] == "canceled"
    finally:
        await runtime.stop()


async def test_native_trailing_policy_uses_the_brokers_trailing_stop_order(tmp_path):
    exchange = FakeAlpacaExchange()
    exchange.set_price("AAA", "100")
    runtime = await start_paper(
        tmp_path, exchange, [("buy_alpha", {"fire_at": 3})], [], registry=build_registry(),
        stop={"type": "native_trailing", "trail_percent": D("1.5")}, capabilities=False,
    )
    try:
        life = runtime.lifecycle
        await life.process({"AAA": ctx("AAA", exchange.clock.now(), 3)})
        (trail,) = [o for o in exchange.open_orders("AAA") if o["type"] == "trailing_stop"]
        assert D(trail["trail_percent"]) == D("1.5") and D(trail["qty"]) == D("29")
    finally:
        await runtime.stop()
