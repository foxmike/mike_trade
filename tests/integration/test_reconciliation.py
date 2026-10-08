"""SQLite integration coverage for fail-closed startup reconciliation.

All broker data comes from ``FakeBroker``.  The suite uses temporary PAPER
databases and never reads credentials or opens a network connection.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from mike_trade.domain.accounts import (
    AccountEnvironment,
    AccountScope,
    AccountSnapshot,
    BrokerCapabilities,
)
from mike_trade.domain.errors import ModeIsolationError
from mike_trade.domain.protection import ProtectionKind, ProtectionStatus
from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    BudgetPolicy,
    Fill,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    Position,
    PositionCycle,
    TimeInForce,
)
from mike_trade.execution.reconcile import AccountReconciler, ReconciliationHealth
from mike_trade.persistence.repositories import (
    AccountScopeRepository,
    BrokerOrderRepository,
    FillRepository,
    OrderIntentRepository,
    PositionCycleRepository,
    ProtectionRepository,
)
from mike_trade.persistence.schema import initialize_database
from mike_trade.persistence.transaction import ScopedWriter, WriterSession
from mike_trade.portfolio.cycles import CycleState

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
D = Decimal
SCOPE = AccountScope(AccountEnvironment.PAPER, "RECON-TEST")
BUDGET = BudgetPolicy(D("100000"), D("100000"), 20)


class Clock:
    def now(self) -> datetime:
        return NOW


@dataclass
class FakeBroker:
    account: AccountSnapshot
    positions: list[Position] = field(default_factory=list)
    open_orders: list[BrokerOrder] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    all_orders: dict[str, BrokerOrder] = field(default_factory=dict)
    protection_capable: bool = True
    environment: AccountEnvironment = AccountEnvironment.PAPER

    def __post_init__(self) -> None:
        self.all_orders.update({order.broker_order_id: order for order in self.open_orders})

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider="deterministic-fake",
            environment=self.environment,
            supported_order_types=frozenset(
                {"MARKET", "STOP"} if self.protection_capable else {"MARKET"}
            ),
            supports_fractional=False,
            supports_notional=False,
            supports_native_trailing_stop=False,
            supports_extended_hours=False,
        )

    async def get_account(self) -> AccountSnapshot:
        return self.account

    async def list_positions(self) -> Sequence[Position]:
        return tuple(self.positions)

    async def list_open_orders(self) -> Sequence[BrokerOrder]:
        return tuple(self.open_orders)

    async def get_order(self, broker_order_id: str) -> BrokerOrder | None:
        return self.all_orders.get(broker_order_id)

    async def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        return next(
            (
                order
                for order in self.all_orders.values()
                if order.client_order_id == client_order_id
            ),
            None,
        )

    async def list_fills(self, since: datetime) -> Sequence[Fill]:
        return tuple(fill for fill in self.fills if fill.filled_at >= since)

    async def submit_order(self, intent: OrderIntent) -> BrokerOrder:  # pragma: no cover
        raise AssertionError("reconciliation must not submit")

    async def replace_order(
        self, broker_order_id: str, **values: Decimal | None
    ) -> BrokerOrder:  # pragma: no cover
        raise AssertionError("reconciliation must not replace")

    async def cancel_order(self, broker_order_id: str) -> None:  # pragma: no cover
        raise AssertionError("reconciliation must not cancel")


def account(*, buying_power: str = "10000", trading_blocked: bool = False) -> AccountSnapshot:
    return AccountSnapshot(
        scope=SCOPE,
        as_of=NOW,
        cash=D("10000"),
        equity=D("10000"),
        buying_power=D(buying_power),
        portfolio_value=D("10000"),
        trading_blocked=trading_blocked,
        source="deterministic-fake",
    )


def position(symbol: str, quantity: str) -> Position:
    qty = D(quantity)
    return Position(
        account_scope_id=SCOPE.scope_id,
        symbol=symbol,
        quantity=qty,
        avg_entry_price=D("100"),
        cost_basis=qty * D("100"),
        as_of=NOW,
    )


def order(
    broker_id: str,
    client_id: str,
    symbol: str,
    *,
    side: OrderSide = OrderSide.BUY,
    status: BrokerOrderStatus = BrokerOrderStatus.FILLED,
    quantity: str = "5",
    filled: str = "5",
    stop_price: str | None = None,
) -> BrokerOrder:
    fill_qty = D(filled)
    kind = OrderType.STOP if stop_price is not None else OrderType.MARKET
    return BrokerOrder(
        broker_order_id=broker_id,
        client_order_id=client_id,
        account_scope_id=SCOPE.scope_id,
        symbol=symbol,
        side=side,
        order_type=kind,
        status=status,
        quantity=D(quantity),
        filled_quantity=fill_qty,
        filled_avg_price=D("100") if fill_qty else None,
        stop_price=None if stop_price is None else D(stop_price),
        submitted_at=NOW,
        updated_at=NOW,
    )


def fill(fill_id: str, broker_id: str, symbol: str, side: OrderSide, quantity: str) -> Fill:
    return Fill(
        fill_id=fill_id,
        broker_order_id=broker_id,
        account_scope_id=SCOPE.scope_id,
        symbol=symbol,
        side=side,
        quantity=D(quantity),
        price=D("100"),
        fee=D("0"),
        filled_at=NOW,
    )


def intent(
    intent_id: str,
    client_id: str,
    cycle_id: str,
    symbol: str,
    *,
    side: OrderSide = OrderSide.BUY,
    purpose: OrderPurpose = OrderPurpose.ENTRY,
    stop_price: str | None = None,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        client_order_id=client_id,
        account_scope_id=SCOPE.scope_id,
        cycle_id=cycle_id,
        symbol=symbol,
        side=side,
        purpose=purpose,
        order_type=OrderType.STOP if stop_price is not None else OrderType.MARKET,
        time_in_force=TimeInForce.GTC if stop_price is not None else TimeInForce.DAY,
        created_at=NOW,
        quantity=D("5"),
        stop_price=None if stop_price is None else D(stop_price),
    )


@dataclass
class Harness:
    session: WriterSession
    writer: ScopedWriter
    clock: Clock

    def close(self) -> None:
        self.session.close()


def harness(tmp_path: Path, name: str) -> Harness:
    path = tmp_path / name
    connection = initialize_database(path, AccountEnvironment.PAPER, now=NOW)
    connection.close()
    session = WriterSession(path)
    writer = session.bind_scope(SCOPE.scope_id)
    clock = Clock()
    AccountScopeRepository(writer, clock).register_scope(SCOPE, broker="fake")
    return Harness(session, writer, clock)


def seed_cycle(
    env: Harness,
    symbol: str = "AAPL",
    quantity: str = "5",
    *,
    apply_entry_fill: bool = True,
    with_protection: bool = False,
) -> tuple[BrokerOrder, BrokerOrder | None]:
    cycle_id = f"cycle-{symbol.lower()}"
    PositionCycleRepository(env.writer, env.clock).open_cycle(
        PositionCycle(cycle_id, SCOPE.scope_id, symbol, entry_strategy_key="buy-alpha")
    )
    entry_intent = intent(f"intent-{symbol}", f"client-{symbol}", cycle_id, symbol)
    intents = OrderIntentRepository(env.writer, env.clock, BUDGET)
    receipt = intents.record_intent(entry_intent, D("500"))
    intents.mark_submitted(receipt, broker_order_id=f"entry-{symbol}")
    entry_order = order(
        f"entry-{symbol}",
        f"client-{symbol}",
        symbol,
        quantity=quantity,
        filled=quantity,
    )
    stored_entry = order(
        f"entry-{symbol}",
        f"client-{symbol}",
        symbol,
        status=BrokerOrderStatus.ACCEPTED,
        quantity=quantity,
        filled="0",
    )
    BrokerOrderRepository(env.writer, env.clock).upsert_broker_order(
        stored_entry, intent_id=entry_intent.intent_id
    )
    if apply_entry_fill:
        FillRepository(env.writer, env.clock).apply_fill(
            fill(
                f"entry-fill-{symbol}",
                entry_order.broker_order_id,
                symbol,
                OrderSide.BUY,
                quantity,
            )
        )

    stop_order: BrokerOrder | None = None
    if with_protection:
        stop_intent = intent(
            f"stop-intent-{symbol}",
            f"stop-client-{symbol}",
            cycle_id,
            symbol,
            side=OrderSide.SELL,
            purpose=OrderPurpose.PROTECTIVE_STOP,
            stop_price="90",
        )
        stop_receipt = intents.record_intent(stop_intent, D("0"))
        intents.mark_submitted(stop_receipt, broker_order_id=f"stop-{symbol}")
        stop_order = order(
            f"stop-{symbol}",
            f"stop-client-{symbol}",
            symbol,
            side=OrderSide.SELL,
            status=BrokerOrderStatus.ACCEPTED,
            quantity=quantity,
            filled="0",
            stop_price="90",
        )
        BrokerOrderRepository(env.writer, env.clock).upsert_broker_order(
            stop_order, intent_id=stop_intent.intent_id
        )
        ProtectionRepository(env.writer, env.clock).record_protection_order(
            f"protection-{symbol}",
            cycle_id,
            ProtectionKind.FIXED_STOP,
            ProtectionStatus.ACTIVE,
            D(quantity),
            stop_price=D("90"),
            broker_order_id=stop_order.broker_order_id,
        )
    return entry_order, stop_order


async def test_missing_fill_is_applied_exactly_once_across_restart(tmp_path: Path) -> None:
    env = harness(tmp_path, "fills.sqlite3")
    try:
        entry, _ = seed_cycle(env, apply_entry_fill=False)
        activity = fill("broker-fill-1", entry.broker_order_id, "AAPL", OrderSide.BUY, "5")
        broker = FakeBroker(
            account(),
            positions=[position("AAPL", "5")],
            fills=[activity],
            all_orders={entry.broker_order_id: entry},
        )
        first = await AccountReconciler(
            broker=broker, writer=env.writer, clock=env.clock
        ).reconcile()
        assert first.applied_fill_ids == ("broker-fill-1",)
        quantity = env.writer.connection.execute(
            "SELECT quantity FROM positions WHERE account_scope_id=? AND symbol='AAPL'",
            (SCOPE.scope_id,),
        ).fetchone()[0]
        fill_events = env.writer.connection.execute(
            "SELECT COUNT(*) FROM domain_events WHERE account_scope_id=? "
            "AND event_type='FillApplied'",
            (SCOPE.scope_id,),
        ).fetchone()[0]

        second = await AccountReconciler(
            broker=broker, writer=env.writer, clock=env.clock
        ).reconcile()
        assert second.applied_fill_ids == ()
        assert env.writer.connection.execute(
            "SELECT quantity FROM positions WHERE account_scope_id=? AND symbol='AAPL'",
            (SCOPE.scope_id,),
        ).fetchone()[0] == quantity
        assert env.writer.connection.execute(
            "SELECT COUNT(*) FROM domain_events WHERE account_scope_id=? "
            "AND event_type='FillApplied'",
            (SCOPE.scope_id,),
        ).fetchone()[0] == fill_events
    finally:
        env.close()


async def test_external_holding_is_unmanaged_and_never_adopted(tmp_path: Path) -> None:
    env = harness(tmp_path, "unmanaged.sqlite3")
    try:
        report = await AccountReconciler(
            broker=FakeBroker(account(), positions=[position("MSFT", "3")]),
            writer=env.writer,
            clock=env.clock,
        ).reconcile()
        assert report.unmanaged_holdings[0].symbol == "MSFT"
        assert report.per_symbol_status["MSFT"].state is CycleState.UNMANAGED
        assert report.entries_permitted is False
        assert env.writer.connection.execute(
            "SELECT COUNT(*) FROM position_cycles WHERE account_scope_id=?",
            (SCOPE.scope_id,),
        ).fetchone()[0] == 0
        assert env.writer.connection.execute(
            "SELECT COUNT(*) FROM positions WHERE account_scope_id=?",
            (SCOPE.scope_id,),
        ).fetchone()[0] == 0
    finally:
        env.close()


async def test_unknown_open_order_reconciles_symbol_and_blocks_entries(tmp_path: Path) -> None:
    env = harness(tmp_path, "unknown-order.sqlite3")
    try:
        mystery = order(
            "mystery-order",
            "outside-client",
            "TSLA",
            status=BrokerOrderStatus.ACCEPTED,
            filled="0",
        )
        report = await AccountReconciler(
            broker=FakeBroker(account(), open_orders=[mystery]),
            writer=env.writer,
            clock=env.clock,
        ).reconcile()
        assert report.unknown_orders == (mystery,)
        assert report.per_symbol_status["TSLA"].state is CycleState.RECONCILING
        assert report.entries_permitted is False
    finally:
        env.close()


async def test_missing_protective_stop_is_degraded_and_blocks(tmp_path: Path) -> None:
    env = harness(tmp_path, "missing-stop.sqlite3")
    try:
        entry, _ = seed_cycle(env)
        report = await AccountReconciler(
            broker=FakeBroker(
                account(),
                positions=[position("AAPL", "5")],
                all_orders={entry.broker_order_id: entry},
            ),
            writer=env.writer,
            clock=env.clock,
        ).reconcile()
        status = report.per_symbol_status["AAPL"]
        assert status.state is CycleState.RECONCILING
        assert status.health is ReconciliationHealth.DEGRADED
        assert "PROTECTION_COVERAGE_MISSING" in status.reasons
        assert report.entries_permitted is False
    finally:
        env.close()


async def test_position_quantity_mismatch_records_explicit_drift(tmp_path: Path) -> None:
    env = harness(tmp_path, "quantity-drift.sqlite3")
    try:
        entry, stop = seed_cycle(env, with_protection=True)
        assert stop is not None
        report = await AccountReconciler(
            broker=FakeBroker(
                account(),
                positions=[position("AAPL", "4")],
                open_orders=[stop],
                all_orders={entry.broker_order_id: entry, stop.broker_order_id: stop},
            ),
            writer=env.writer,
            clock=env.clock,
        ).reconcile()
        drift = next(
            item
            for item in report.drift_items
            if item.drift_type == "POSITION_QUANTITY_MISMATCH"
        )
        assert drift.expected["local_fill_ledger_quantity"] == D("5")
        assert drift.actual["broker_position_quantity"] == D("4")
        assert report.entries_permitted is False
        assert env.writer.connection.execute(
            "SELECT COUNT(*) FROM reconciliation_drifts WHERE account_scope_id=? "
            "AND drift_type='POSITION_QUANTITY_MISMATCH'",
            (SCOPE.scope_id,),
        ).fetchone()[0] == 1
    finally:
        env.close()


async def test_external_manual_close_is_audited_without_strategy_win(tmp_path: Path) -> None:
    env = harness(tmp_path, "external-close.sqlite3")
    try:
        entry, _ = seed_cycle(env)
        external_order = order(
            "external-sell",
            "manual-broker-client",
            "AAPL",
            side=OrderSide.SELL,
            status=BrokerOrderStatus.FILLED,
        )
        external_fill = fill(
            "external-fill", external_order.broker_order_id, "AAPL", OrderSide.SELL, "5"
        )
        report = await AccountReconciler(
            broker=FakeBroker(
                account(),
                fills=[external_fill],
                all_orders={
                    entry.broker_order_id: entry,
                    external_order.broker_order_id: external_order,
                },
            ),
            writer=env.writer,
            clock=env.clock,
        ).reconcile()
        assert any(item.drift_type == "EXTERNAL_MANUAL_EXIT" for item in report.drift_items)
        assert env.writer.connection.execute(
            "SELECT state FROM position_cycles WHERE account_scope_id=? AND cycle_id='cycle-aapl'",
            (SCOPE.scope_id,),
        ).fetchone()[0] == "CLOSED"
        closed_cycle = PositionCycleRepository(env.writer, env.clock).get_cycle("cycle-aapl")
        assert closed_cycle is not None
        assert closed_cycle.state.value == "CLOSED"
        assert closed_cycle.remaining_quantity == D("0")
        event_type, payload = env.writer.connection.execute(
            "SELECT event_type,payload_json FROM domain_events WHERE account_scope_id=? "
            "AND event_type='ExternalManualExitDetected'",
            (SCOPE.scope_id,),
        ).fetchone()
        assert event_type == "ExternalManualExitDetected"
        assert '"classification":"external_manual_exit"' in payload
        assert '"strategy_exit":false' in payload
        assert '"strategy_win":false' in payload
        assert env.writer.connection.execute(
            "SELECT COUNT(*) FROM domain_events WHERE account_scope_id=? "
            "AND event_type IN ('StrategyExit','StrategyWin')",
            (SCOPE.scope_id,),
        ).fetchone()[0] == 0
    finally:
        env.close()


async def test_unsafe_account_or_insufficient_buying_power_blocks_account(
    tmp_path: Path,
) -> None:
    blocked = harness(tmp_path, "blocked-account.sqlite3")
    low_power = harness(tmp_path, "low-power.sqlite3")
    try:
        unsafe_report = await AccountReconciler(
            broker=FakeBroker(account(trading_blocked=True)),
            writer=blocked.writer,
            clock=blocked.clock,
        ).reconcile()
        low_report = await AccountReconciler(
            broker=FakeBroker(account(buying_power="50")),
            writer=low_power.writer,
            clock=low_power.clock,
            minimum_buying_power=D("100"),
        ).reconcile()
        assert "ACCOUNT_FLAG:trading_blocked" in unsafe_report.account_block_reasons
        assert "INSUFFICIENT_BUYING_POWER" in low_report.account_block_reasons
        assert not unsafe_report.entries_permitted
        assert not low_report.entries_permitted
    finally:
        blocked.close()
        low_power.close()


async def test_clean_matching_snapshot_permits_entries(tmp_path: Path) -> None:
    env = harness(tmp_path, "clean.sqlite3")
    try:
        entry, stop = seed_cycle(env, with_protection=True)
        assert stop is not None
        broker = FakeBroker(
            account(),
            positions=[position("AAPL", "5")],
            open_orders=[stop],
            all_orders={entry.broker_order_id: entry, stop.broker_order_id: stop},
        )
        report = await AccountReconciler(
            broker=broker, writer=env.writer, clock=env.clock
        ).reconcile()
        assert report.drift_items == ()
        assert report.unmanaged_holdings == ()
        assert report.unknown_orders == ()
        assert report.per_symbol_status["AAPL"].health is ReconciliationHealth.CLEAN
        assert report.entries_permitted is True
        assert report.clean is True
        assert report.status == "CLEAN"
    finally:
        env.close()


def test_reconciler_refuses_a_broker_from_another_mode(tmp_path: Path) -> None:
    path = tmp_path / "shadow-isolation.sqlite3"
    connection = initialize_database(path, AccountEnvironment.SHADOW, now=NOW)
    connection.close()
    session = WriterSession(path)
    try:
        writer = session.bind_scope("shadow:RECON-TEST:scenario-a")
        with pytest.raises(ModeIsolationError):
            AccountReconciler(
                broker=FakeBroker(account()),
                writer=writer,
                clock=Clock(),
            )
    finally:
        session.close()
