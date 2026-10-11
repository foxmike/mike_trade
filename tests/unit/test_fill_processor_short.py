"""Persisted position ledger (FillProcessor) for SHORT positions.

A short entry stores a SHORT position (positive quantity + direction); covers reduce it; a fill
that would reverse the position or close the wrong book is never applied: it is recorded as a
critical reconciliation drift and the position is left untouched.
"""

from __future__ import annotations

from decimal import Decimal as D

from test_fill_processor import NOW, SCOPE, Clock, env, ev, rows  # noqa: F401 - env is a fixture

from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    PositionCycle,
    PositionDirection,
    TimeInForce,
)


def add_order(env, ident, side, qty, *, direction=PositionDirection.LONG, cycle=None):
    """Record an intent + broker order; opens a new cycle unless ``cycle`` names one."""
    _session, _writer, cycles, intents, orders, _processor, _callbacks = env
    cycle_id = cycle or f"cycle-{ident}"
    if cycle is None:
        cycles.open_cycle(PositionCycle(
            cycle_id=cycle_id, account_scope_id=SCOPE.scope_id, symbol="XYZ",
            direction=direction))
    purpose = OrderPurpose.ENTRY if side.is_entry else OrderPurpose.STRATEGY_EXIT
    intent = OrderIntent(
        intent_id=f"intent-{ident}", client_order_id=f"client-{ident}",
        account_scope_id=SCOPE.scope_id, cycle_id=cycle_id, symbol="XYZ", side=side,
        purpose=purpose, order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY,
        created_at=NOW, quantity=D(qty))
    receipt = intents.record_intent(intent, D("1000") if purpose is OrderPurpose.ENTRY else D("0"))
    orders.upsert_broker_order(BrokerOrder(
        broker_order_id=f"broker-{ident}", client_order_id=intent.client_order_id,
        account_scope_id=SCOPE.scope_id, symbol="XYZ", side=side, order_type=OrderType.MARKET,
        status=BrokerOrderStatus.ACCEPTED, quantity=D(qty), filled_quantity=D("0"),
        submitted_at=NOW, updated_at=NOW), intent_id=receipt.intent.intent_id)
    return cycle_id


def position(writer):
    found = rows(writer, "SELECT direction,quantity,average_entry_price FROM positions")
    return None if not found else (found[0][0], D(found[0][1]), D(found[0][2]))


def drifts(writer):
    return [r[0] for r in rows(writer, "SELECT drift_type FROM reconciliation_drifts")]


def test_a_short_entry_fill_stores_a_short_position_and_consumes_its_reservation(env):
    add_order(env, "1", OrderSide.SELL_SHORT, "10", direction=PositionDirection.SHORT)
    result = env[5].ingest(ev("client-1", "s1", cumulative="10", avg="150"))
    assert result.incremental_qty == D("10")
    assert position(env[1]) == ("SHORT", D("10"), D("150"))
    assert rows(env[1], "SELECT side FROM fills") == [("SELL_SHORT",)]
    assert tuple(rows(env[1], "SELECT state,amount FROM budget_reservations")[0]) == (
        "CONSUMED", "0")


def test_a_cover_reduces_then_closes_the_short_and_closes_its_cycle(env):
    cycle = add_order(env, "1", OrderSide.SELL_SHORT, "10", direction=PositionDirection.SHORT)
    env[5].ingest(ev("client-1", "s1", cumulative="10", avg="150"))
    add_order(env, "2", OrderSide.BUY_TO_COVER, "10", cycle=cycle)
    env[5].ingest(ev("client-2", "c-part", qty="4", price="144"))
    assert position(env[1]) == ("SHORT", D("6"), D("150"))          # still short, magnitude 6
    assert rows(env[1], "SELECT state FROM position_cycles")[0][0] == "EXITING"
    env[5].ingest(ev("client-2", "c-rest", qty="6", price="144"))
    assert position(env[1]) is None
    assert rows(env[1], "SELECT state FROM position_cycles")[0][0] == "CLOSED"
    assert [r[0] for r in rows(env[1], "SELECT side FROM fills ORDER BY filled_at, rowid")] == [
        "SELL_SHORT", "BUY_TO_COVER", "BUY_TO_COVER"]


def test_a_cover_larger_than_the_short_is_never_applied_it_would_open_a_long(env):
    cycle = add_order(env, "1", OrderSide.SELL_SHORT, "10", direction=PositionDirection.SHORT)
    env[5].ingest(ev("client-1", "s1", cumulative="10", avg="150"))
    add_order(env, "2", OrderSide.BUY_TO_COVER, "12", cycle=cycle)
    env[5].ingest(ev("client-2", "c1", qty="12", price="144"))
    assert position(env[1]) == ("SHORT", D("10"), D("150"))   # untouched
    assert drifts(env[1]) == ["CRITICAL_OVERCOVER"]


def test_a_sell_or_buy_against_the_opposite_position_is_refused_not_netted(env):
    add_order(env, "1", OrderSide.SELL_SHORT, "10", direction=PositionDirection.SHORT)
    env[5].ingest(ev("client-1", "s1", cumulative="10", avg="150"))
    # a long SELL fill (a different cycle) against the short book
    add_order(env, "2", OrderSide.SELL, "5")
    env[5].ingest(ev("client-2", "x1", qty="5", price="150"))
    # a long BUY entry fill against the short book
    add_order(env, "3", OrderSide.BUY, "5")
    env[5].ingest(ev("client-3", "x2", qty="5", price="150"))
    assert position(env[1]) == ("SHORT", D("10"), D("150"))   # never reversed or netted
    assert drifts(env[1]) == ["CRITICAL_DIRECTION_MISMATCH", "CRITICAL_DIRECTION_MISMATCH"]


def test_the_long_ledger_is_unchanged_for_longs(env):
    cycle = add_order(env, "1", OrderSide.BUY, "10")
    env[5].ingest(ev("client-1", "b1", cumulative="10", avg="12.35"))
    assert position(env[1]) == ("LONG", D("10"), D("12.35"))
    add_order(env, "2", OrderSide.SELL, "10", cycle=cycle)
    env[5].ingest(ev("client-2", "s1", qty="10", price="13"))
    assert position(env[1]) is None and drifts(env[1]) == []
