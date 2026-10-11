"""Fail-closed reconciliation distinguishes SHORT from LONG positions.

Broker positions are only comparable within one direction: a local short matches a broker short,
an opposite book is reported (never netted or adopted), protective coverage must be on the
closing side (BUY_TO_COVER for a short), and an external cover that exactly flattens a managed
short is recorded as an external manual exit rather than a strategy exit.
"""

from __future__ import annotations

from pathlib import Path

from test_reconciliation import (  # noqa: F401
    BUDGET,
    NOW,
    SCOPE,
    D,
    FakeBroker,
    account,
    fill,
    harness,
    intent,
    order,
)

from mike_trade.domain.protection import ProtectionKind, ProtectionStatus
from mike_trade.domain.trading import (
    BrokerOrderStatus,
    OrderPurpose,
    OrderSide,
    Position,
    PositionCycle,
    PositionDirection,
)
from mike_trade.execution.reconcile import AccountReconciler, ReconciliationHealth
from mike_trade.persistence.repositories import (
    BrokerOrderRepository,
    FillRepository,
    OrderIntentRepository,
    PositionCycleRepository,
    ProtectionRepository,
)
from mike_trade.portfolio.cycles import CycleState


def broker_position(symbol, quantity, direction=PositionDirection.SHORT) -> Position:
    qty = D(quantity)
    return Position(
        account_scope_id=SCOPE.scope_id, symbol=symbol, quantity=qty, avg_entry_price=D("100"),
        cost_basis=qty * D("100"), as_of=NOW, direction=direction)


def seed_short(env, symbol="AAPL", quantity="5", *, stop_side=OrderSide.BUY_TO_COVER,
               with_protection=True):
    """A managed SHORT cycle: a filled SELL_SHORT entry and (optionally) a BUY stop above."""
    cycle_id = f"cycle-{symbol.lower()}"
    PositionCycleRepository(env.writer, env.clock).open_cycle(PositionCycle(
        cycle_id, SCOPE.scope_id, symbol, direction=PositionDirection.SHORT,
        entry_strategy_key="short-alpha"))
    entry_intent = intent(f"intent-{symbol}", f"client-{symbol}", cycle_id, symbol,
                          side=OrderSide.SELL_SHORT)
    intents = OrderIntentRepository(env.writer, env.clock, BUDGET)
    receipt = intents.record_intent(entry_intent, D("500"))
    intents.mark_submitted(receipt, broker_order_id=f"entry-{symbol}")
    entry = order(f"entry-{symbol}", f"client-{symbol}", symbol, side=OrderSide.SELL_SHORT,
                  quantity=quantity, filled=quantity)
    BrokerOrderRepository(env.writer, env.clock).upsert_broker_order(
        order(f"entry-{symbol}", f"client-{symbol}", symbol, side=OrderSide.SELL_SHORT,
              status=BrokerOrderStatus.ACCEPTED, quantity=quantity, filled="0"),
        intent_id=entry_intent.intent_id)
    FillRepository(env.writer, env.clock).apply_fill(
        fill(f"entry-fill-{symbol}", entry.broker_order_id, symbol, OrderSide.SELL_SHORT,
             quantity))
    stop = None
    if with_protection:
        stop_intent = intent(f"stop-intent-{symbol}", f"stop-client-{symbol}", cycle_id, symbol,
                             side=stop_side, purpose=OrderPurpose.PROTECTIVE_STOP,
                             stop_price="110")
        stop_receipt = intents.record_intent(stop_intent, D("0"))
        intents.mark_submitted(stop_receipt, broker_order_id=f"stop-{symbol}")
        stop = order(f"stop-{symbol}", f"stop-client-{symbol}", symbol, side=stop_side,
                     status=BrokerOrderStatus.ACCEPTED, quantity=quantity, filled="0",
                     stop_price="110")
        BrokerOrderRepository(env.writer, env.clock).upsert_broker_order(
            stop, intent_id=stop_intent.intent_id)
        ProtectionRepository(env.writer, env.clock).record_protection_order(
            f"protection-{symbol}", cycle_id, ProtectionKind.FIXED_STOP,
            ProtectionStatus.ACTIVE, D(quantity), stop_price=D("110"),
            broker_order_id=stop.broker_order_id)
    return entry, stop


def drift_types(report):
    return {d.drift_type for d in report.drift_items}


async def test_a_managed_short_with_a_buy_to_cover_stop_matches_the_broker_short(tmp_path: Path):
    env = harness(tmp_path, "short-clean.sqlite3")
    try:
        entry, stop = seed_short(env)
        report = await AccountReconciler(
            broker=FakeBroker(account(), positions=[broker_position("AAPL", "5")],
                              open_orders=[stop], all_orders={entry.broker_order_id: entry}),
            writer=env.writer, clock=env.clock).reconcile()
        status = report.per_symbol_status["AAPL"]
        assert drift_types(report) == set() and status.health is ReconciliationHealth.CLEAN
        assert status.local_quantity == D("5") == status.broker_quantity
        assert report.unmanaged_holdings == ()
    finally:
        env.close()


async def test_a_broker_short_against_a_local_long_is_unmanaged_and_never_netted(tmp_path: Path):
    env = harness(tmp_path, "direction-mismatch.sqlite3")
    try:
        from test_reconciliation import seed_cycle
        entry, _ = seed_cycle(env)  # a managed LONG of 5
        report = await AccountReconciler(
            broker=FakeBroker(account(), positions=[broker_position("AAPL", "5")],
                              all_orders={entry.broker_order_id: entry}),
            writer=env.writer, clock=env.clock).reconcile()
        assert "POSITION_DIRECTION_MISMATCH" in drift_types(report)
        assert [h.symbol for h in report.unmanaged_holdings] == ["AAPL"]  # the short is NOT owned
        assert report.per_symbol_status["AAPL"].state is CycleState.UNMANAGED
        assert report.entries_permitted is False
    finally:
        env.close()


async def test_a_short_stop_on_the_wrong_side_is_a_coverage_mismatch(tmp_path: Path):
    env = harness(tmp_path, "wrong-stop-side.sqlite3")
    try:
        entry, stop = seed_short(env, stop_side=OrderSide.SELL)  # a SELL stop cannot cover a short
        report = await AccountReconciler(
            broker=FakeBroker(account(), positions=[broker_position("AAPL", "5")],
                              open_orders=[stop], all_orders={entry.broker_order_id: entry}),
            writer=env.writer, clock=env.clock).reconcile()
        assert "PROTECTION_COVERAGE_MISMATCH" in drift_types(report)
        assert report.entries_permitted is False
    finally:
        env.close()


async def test_a_short_quantity_mismatch_is_reported(tmp_path: Path):
    env = harness(tmp_path, "short-qty.sqlite3")
    try:
        entry, stop = seed_short(env)
        report = await AccountReconciler(
            broker=FakeBroker(account(), positions=[broker_position("AAPL", "3")],
                              open_orders=[stop], all_orders={entry.broker_order_id: entry}),
            writer=env.writer, clock=env.clock).reconcile()
        assert "POSITION_QUANTITY_MISMATCH" in drift_types(report)
    finally:
        env.close()


async def test_an_external_cover_that_exactly_flattens_a_managed_short_is_a_manual_exit(
    tmp_path: Path,
):
    env = harness(tmp_path, "external-cover.sqlite3")
    try:
        entry, _ = seed_short(env, with_protection=False)
        manual = order("manual-cover", "outside-client", "AAPL", side=OrderSide.BUY_TO_COVER,
                       status=BrokerOrderStatus.FILLED, quantity="5", filled="5")
        activity = fill("cover-fill-1", "manual-cover", "AAPL", OrderSide.BUY_TO_COVER, "5")
        report = await AccountReconciler(
            broker=FakeBroker(account(), positions=[], fills=[activity],
                              all_orders={entry.broker_order_id: entry,
                                          manual.broker_order_id: manual}),
            writer=env.writer, clock=env.clock).reconcile()
        assert "EXTERNAL_MANUAL_EXIT" in drift_types(report)
        state = env.writer.connection.execute(
            "SELECT state FROM position_cycles WHERE account_scope_id=? AND symbol='AAPL'",
            (SCOPE.scope_id,)).fetchone()[0]
        assert state == "CLOSED"
        assert env.writer.connection.execute(
            "SELECT COUNT(*) FROM positions WHERE account_scope_id=?", (SCOPE.scope_id,)
        ).fetchone()[0] == 0
    finally:
        env.close()
