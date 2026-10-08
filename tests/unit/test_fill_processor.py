from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    BudgetPolicy,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    PositionCycle,
    TimeInForce,
)
from mike_trade.execution.fill_processor import FillEvent, FillProcessor, OrderUpdateEvent
from mike_trade.execution.order_fsm import OrderStatus
from mike_trade.persistence.repositories import (
    AccountScopeRepository,
    BrokerOrderRepository,
    OrderIntentRepository,
    PositionCycleRepository,
)
from mike_trade.persistence.schema import initialize_database
from mike_trade.persistence.transaction import WriterSession

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
SCOPE = AccountScope(AccountEnvironment.PAPER, "ACCT")


class Clock:
    def now(self):
        return NOW


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "ledger.db"
    initialize_database(path, AccountEnvironment.PAPER, now=NOW).close()
    session = WriterSession(path)
    writer = session.bind_scope(SCOPE.scope_id)
    AccountScopeRepository(writer, Clock()).register_scope(SCOPE)
    cycles = PositionCycleRepository(writer, Clock())
    intents = OrderIntentRepository(
        writer,
        Clock(),
        BudgetPolicy(
            per_symbol_budget=D("100000"),
            total_application_budget=D("100000"),
            max_open_positions=20,
        ),
    )
    orders = BrokerOrderRepository(writer, Clock())
    callbacks = []
    processor = FillProcessor(writer, Clock(), callbacks.append)
    yield session, writer, cycles, intents, orders, processor, callbacks
    session.close()


def add_order(
    env, *, ident="1", side=OrderSide.BUY, qty="10", purpose=None, status=BrokerOrderStatus.ACCEPTED
):
    session, writer, cycles, intents, orders, processor, callbacks = env
    cycle_id = f"cycle-{ident}"
    cycles.open_cycle(
        PositionCycle(cycle_id=cycle_id, account_scope_id=SCOPE.scope_id, symbol="XYZ")
    )
    purpose = purpose or (
        OrderPurpose.ENTRY if side is OrderSide.BUY else OrderPurpose.STRATEGY_EXIT
    )
    intent = OrderIntent(
        intent_id=f"intent-{ident}",
        client_order_id=f"client-{ident}",
        account_scope_id=SCOPE.scope_id,
        cycle_id=cycle_id,
        symbol="XYZ",
        side=side,
        purpose=purpose,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        created_at=NOW,
        quantity=D(qty),
    )
    receipt = intents.record_intent(intent, D("1000") if purpose is OrderPurpose.ENTRY else D("0"))
    order = BrokerOrder(
        broker_order_id=f"broker-{ident}",
        client_order_id=intent.client_order_id,
        account_scope_id=SCOPE.scope_id,
        symbol="XYZ",
        side=side,
        order_type=OrderType.MARKET,
        status=status,
        quantity=D(qty),
        filled_quantity=D("0"),
        submitted_at=NOW,
        updated_at=NOW,
    )
    orders.upsert_broker_order(order, intent_id=receipt.intent.intent_id)
    return intent


def ev(client, id, *, qty=None, cumulative=None, price=None, avg=None, side=None, at=NOW):
    return FillEvent(
        client_order_id=client,
        event_id=id,
        occurred_at=at,
        quantity=None if qty is None else D(qty),
        cumulative_qty=None if cumulative is None else D(cumulative),
        price=None if price is None else D(price),
        average_price=None if avg is None else D(avg),
        side=side,
    )


def rows(writer, sql, args=()):
    return writer.connection.execute(sql, args).fetchall()


def test_single_full_fill_updates_order_position_and_decimal_cost_basis(env):
    add_order(env)
    result = env[5].ingest(ev("client-1", "fill-1", cumulative="10", avg="12.35"))
    assert result.incremental_qty == D("10")
    assert result.order_status is OrderStatus.FILLED
    position = rows(env[1], "SELECT quantity,average_entry_price FROM positions")[0]
    assert tuple(map(D, position)) == (D("10"), D("12.35"))
    order = rows(env[1], "SELECT filled_quantity,filled_avg_price FROM broker_orders")[0]
    assert tuple(map(D, order)) == (D("10"), D("12.35"))


def test_multiple_partial_fills_use_decimal_weighted_average(env):
    add_order(env)
    env[5].ingest(ev("client-1", "a", qty="3", price="10.10"))
    env[5].ingest(ev("client-1", "b", qty="7", price="10.20"))
    order = rows(env[1], "SELECT filled_quantity,filled_avg_price FROM broker_orders")[0]
    assert tuple(map(D, order)) == (D("10"), (D("3") * D("10.10") + D("7") * D("10.20")) / D("10"))
    assert D(rows(env[1], "SELECT quantity FROM positions")[0][0]) == D("10")


def test_partial_cancel_releases_unused_reservation(env):
    add_order(env)
    env[5].ingest(ev("client-1", "partial", qty="4", price="20"))
    env[5].ingest(OrderUpdateEvent("client-1", "CANCELED", NOW, event_id="cancel"))
    reservation = rows(env[1], "SELECT state,amount FROM budget_reservations")[0]
    assert tuple(reservation) == ("RELEASED", "0")


@pytest.mark.parametrize("status", ["REJECTED", "CANCELED", "EXPIRED"])
def test_terminal_zero_fill_releases_reservation(env, status):
    add_order(env)
    env[5].ingest(OrderUpdateEvent("client-1", status, NOW, event_id=status))
    assert tuple(rows(env[1], "SELECT state,amount FROM budget_reservations")[0]) == (
        "RELEASED",
        "0",
    )
    assert rows(env[1], "SELECT * FROM fills") == []


def test_duplicate_and_restart_reingestion_are_noops(env):
    add_order(env)
    event = ev("client-1", "stable-fill", qty="4", price="25")
    applied = env[5].ingest(event)
    assert applied.outcome == "APPLIED"
    assert env[5].ingest(event).outcome == "DUPLICATE"
    path = env[0].connection.execute("PRAGMA database_list").fetchone()[2]
    env[0].close()
    restarted = WriterSession(path)
    try:
        p = FillProcessor(restarted.bind_scope(SCOPE.scope_id), Clock())
        assert p.ingest(event).outcome == "DUPLICATE"
        assert D(restarted.connection.execute("SELECT quantity FROM positions").fetchone()[0]) == D(
            "4"
        )
        assert D(
            restarted.connection.execute("SELECT amount FROM budget_reservations").fetchone()[0]
        ) == D("600")
    finally:
        restarted.close()


def test_out_of_order_cumulative_fills_reconcile_by_quantity(env):
    add_order(env)
    env[5].ingest(ev("client-1", "cum-6", cumulative="6", avg="10"))
    env[5].ingest(
        ev("client-1", "cum-3-old", cumulative="3", avg="9", at=NOW - timedelta(seconds=1))
    )
    order = rows(env[1], "SELECT filled_quantity,filled_avg_price FROM broker_orders")[0]
    assert tuple(map(D, order)) == (D("6"), D("10"))
    assert D(rows(env[1], "SELECT quantity FROM positions")[0][0]) == D("6")


def test_accepted_does_not_change_quantity_and_cancel_pending_fill_is_applied(env):
    add_order(env)
    env[5].ingest(OrderUpdateEvent("client-1", "ACCEPTED", NOW, event_id="accepted"))
    assert D(rows(env[1], "SELECT filled_quantity FROM broker_orders")[0][0]) == D("0")
    env[5].ingest(OrderUpdateEvent("client-1", "PENDING_CANCEL", NOW, event_id="cancel-request"))
    result = env[5].ingest(ev("client-1", "race-fill", qty="2", price="10"))
    assert result.order_status is OrderStatus.PARTIALLY_FILLED
    assert D(rows(env[1], "SELECT quantity FROM positions")[0][0]) == D("2")


def test_cumulative_trade_update_applies_fill_and_average_price(env):
    add_order(env)
    result = env[5].ingest(
        OrderUpdateEvent(
            "client-1",
            "FILLED",
            NOW,
            event_id="trade-update-fill",
            cumulative_qty=D("10"),
            average_price=D("7.25"),
        )
    )
    assert result.incremental_qty == D("10")
    assert result.order_status is OrderStatus.FILLED
    assert tuple(map(D, rows(env[1], "SELECT quantity,average_entry_price FROM positions")[0])) == (
        D("10"),
        D("7.25"),
    )


def test_sell_oversell_records_drift_blocks_entries_without_negative_position(env):
    add_order(env, ident="buy")
    env[5].ingest(ev("client-buy", "buy-fill", qty="5", price="10"))
    add_order(env, ident="stop", side=OrderSide.SELL, qty="5", purpose=OrderPurpose.PROTECTIVE_STOP)
    add_order(env, ident="exit", side=OrderSide.SELL, qty="5", purpose=OrderPurpose.STRATEGY_EXIT)
    env[5].ingest(ev("client-stop", "stop-fill", qty="5", price="9", side=OrderSide.SELL))
    result = env[5].ingest(ev("client-exit", "exit-fill", qty="1", price="8", side=OrderSide.SELL))
    assert result.drift and result.drift.drift_type == "CRITICAL_OVERSELL"
    assert rows(env[1], "SELECT quantity FROM positions") == []
    assert rows(env[1], "SELECT kill_switch_enabled,paused FROM entry_controls")[0] == (1, 1)


def test_unknown_client_order_fill_is_recorded_as_drift_and_blocks_entries(env):
    result = env[5].ingest(ev("missing-client", "orphan", qty="1", price="2"))
    assert result.outcome == "UNMANAGED"
    assert result.drift is not None
    assert (
        rows(env[1], "SELECT external_activity_id FROM broker_activities")[0][0]
        == "provider:orphan"
    )
    assert rows(env[1], "SELECT kill_switch_enabled FROM entry_controls")[0][0] == 1
