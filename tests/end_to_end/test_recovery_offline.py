"""Offline restart, reconciliation, lock, shutdown, and dashboard recovery tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_full_product_cycle import Clock, downward, scenario, start_runtime, upward

from mike_trade.app import AppSettings, SQLitePersistence, build_runtime
from mike_trade.dashboard.api import DashboardReadService, DashboardSettings
from mike_trade.domain.accounts import (
    AccountEnvironment,
    AccountScope,
    AccountSnapshot,
    BrokerCapabilities,
)
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
from mike_trade.execution.reconcile import AccountReconciler
from mike_trade.persistence.repositories import (
    AccountScopeRepository,
    BrokerOrderRepository,
    CashEquityRepository,
    EntryControlRepository,
    FillRepository,
    OrderIntentRepository,
    PositionCycleRepository,
    ProtectionRepository,
)
from mike_trade.persistence.schema import initialize_database
from mike_trade.persistence.transaction import WriterSession
from mike_trade.runtime import RuntimeStartupError, StartupState

D = Decimal
NOW = datetime(2026, 10, 8, 14, 32, tzinfo=UTC)
PAPER_SCOPE = AccountScope(AccountEnvironment.PAPER, "offline")
DOMAIN_BUDGET = BudgetPolicy(D("100000"), D("100000"), 20)


async def test_restart_mid_cycle_preserves_account_stop_and_exact_decimal_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "shadow-restart.sqlite3"
    clock = Clock(NOW)
    runtime = await start_runtime(path, clock)
    await runtime.lifecycle.process({"SPY": upward("SPY", clock.value)})
    before = runtime.broker.scenarios["vwap_reference_shadow"]  # type: ignore[attr-defined]
    cash = before.account.state.cash
    position = before.account.state.positions["SPY"]
    stop = before.stops.state(position.cycle_id)
    assert stop is not None and stop.status == "ACTIVE"
    open_order_ids = {order.broker_order_id for order in await before.account.list_open_orders()}
    await runtime.stop()

    restarted = await start_runtime(path, clock)
    try:
        restored = restarted.broker.scenarios["vwap_reference_shadow"]  # type: ignore[attr-defined]
        assert restored.account.state.cash == cash
        assert restored.account.state.positions["SPY"] == position
        restored_stop = restored.stops.state(position.cycle_id)
        assert restored_stop is not None and restored_stop.status == "ACTIVE"
        assert {
            order.broker_order_id for order in await restored.account.list_open_orders()
        } == open_order_ids

        # Graceful shutdown adds no cancel/fill/close event and leaves broker-held
        # protection untouched. Reopening proves the durable effect, not an in-memory copy.
        event_count = len(restored.account.state.orders) + len(restored.account.state.fills)
        await restarted.stop()
        final = await start_runtime(path, clock)
        try:
            final_state = final.broker.scenarios["vwap_reference_shadow"]  # type: ignore[attr-defined]
            durable_fact_count = len(final_state.account.state.orders) + len(
                final_state.account.state.fills
            )
            assert durable_fact_count == event_count
            assert final_state.account.state.positions["SPY"] == position
            assert final_state.stops.state(position.cycle_id).status == "ACTIVE"
        finally:
            await final.stop()
    finally:
        if restarted.state is not StartupState.STOPPED:
            await restarted.stop()


def paper_account() -> AccountSnapshot:
    return AccountSnapshot(
        PAPER_SCOPE,
        NOW,
        D("10000"),
        D("10000"),
        D("10000"),
        portfolio_value=D("10000"),
        source="offline-fake",
    )


def position(symbol: str, quantity: str) -> Position:
    qty = D(quantity)
    return Position(
        PAPER_SCOPE.scope_id,
        symbol,
        qty,
        D("100"),
        qty * D("100"),
        NOW,
    )


def broker_order(
    broker_id: str,
    client_id: str,
    symbol: str,
    *,
    side: OrderSide = OrderSide.BUY,
    status: BrokerOrderStatus = BrokerOrderStatus.ACCEPTED,
    quantity: str = "5",
    filled: str = "0",
    stop_price: str | None = None,
) -> BrokerOrder:
    filled_qty = D(filled)
    return BrokerOrder(
        broker_id,
        client_id,
        PAPER_SCOPE.scope_id,
        symbol,
        side,
        OrderType.STOP if stop_price else OrderType.MARKET,
        status,
        D(quantity),
        filled_qty,
        NOW,
        NOW,
        filled_avg_price=D("100") if filled_qty else None,
        stop_price=D(stop_price) if stop_price else None,
    )


def fill(fill_id: str, order_id: str, symbol: str, quantity: str) -> Fill:
    return Fill(
        fill_id,
        order_id,
        PAPER_SCOPE.scope_id,
        symbol,
        OrderSide.BUY,
        D(quantity),
        D("100"),
        D("0"),
        NOW,
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
        intent_id,
        client_id,
        PAPER_SCOPE.scope_id,
        cycle_id,
        symbol,
        side,
        purpose,
        OrderType.STOP if stop_price else OrderType.MARKET,
        TimeInForce.GTC if stop_price else TimeInForce.DAY,
        NOW,
        quantity=D("5"),
        stop_price=D(stop_price) if stop_price else None,
    )


@dataclass
class OfflineBroker:
    account: AccountSnapshot = field(default_factory=paper_account)
    positions: list[Position] = field(default_factory=list)
    open_orders: list[BrokerOrder] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    all_orders: dict[str, BrokerOrder] = field(default_factory=dict)
    cancel_calls: list[str] = field(default_factory=list)
    close_calls: list[str] = field(default_factory=list)

    environment = AccountEnvironment.PAPER

    def __post_init__(self) -> None:
        self.all_orders.update({order.broker_order_id: order for order in self.open_orders})

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            "offline-fake",
            AccountEnvironment.PAPER,
            frozenset({"MARKET", "STOP"}),
            False,
            False,
            False,
            False,
        )

    async def get_account(self) -> AccountSnapshot:
        return self.account

    async def list_positions(self) -> tuple[Position, ...]:
        return tuple(self.positions)

    async def list_open_orders(self) -> tuple[BrokerOrder, ...]:
        return tuple(self.open_orders)

    async def get_order(self, order_id: str) -> BrokerOrder | None:
        return self.all_orders.get(order_id)

    async def get_order_by_client_id(self, client_id: str) -> BrokerOrder | None:
        return next(
            (order for order in self.all_orders.values() if order.client_order_id == client_id),
            None,
        )

    async def list_fills(self, since: datetime) -> tuple[Fill, ...]:
        return tuple(item for item in self.fills if item.filled_at >= since)

    async def submit_order(self, _intent: object) -> object:
        raise AssertionError("recovery reconciliation never submits")

    async def cancel_order(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)

    async def close_position(self, symbol: str) -> None:
        self.close_calls.append(symbol)

    def order_updates_fresh(self) -> bool:
        return True


class LazyReconciler:
    def __init__(
        self, persistence: SQLitePersistence, broker: OfflineBroker, clock: Clock
    ) -> None:
        self.persistence = persistence
        self.broker = broker
        self.clock = clock
        self.report = None

    async def reconcile(self, _facts: object = None):
        assert self.persistence.session is not None
        writer = self.persistence.session.bind_scope(PAPER_SCOPE.scope_id)
        self.report = await AccountReconciler(
            broker=self.broker, writer=writer, clock=self.clock
        ).reconcile()
        return self.report


def seed_paper_recovery(path: Path, clock: Clock) -> tuple[BrokerOrder, BrokerOrder]:
    conn = initialize_database(path, AccountEnvironment.PAPER, now=clock.now())
    conn.close()
    session = WriterSession(path)
    try:
        writer = session.bind_scope(PAPER_SCOPE.scope_id)
        AccountScopeRepository(writer, clock).register_scope(PAPER_SCOPE, broker="offline")

        cycles = PositionCycleRepository(writer, clock)
        intents = OrderIntentRepository(writer, clock, DOMAIN_BUDGET)
        orders = BrokerOrderRepository(writer, clock)

        # Outstanding GOOGL entry: broker has filled it, local fill ledger has not.
        cycles.open_cycle(PositionCycle("cycle-googl", PAPER_SCOPE.scope_id, "GOOGL"))
        entry_intent = intent("intent-googl", "client-googl", "cycle-googl", "GOOGL")
        receipt = intents.record_intent(entry_intent, D("500"))
        intents.mark_submitted(receipt, "entry-googl")
        local_entry = broker_order("entry-googl", "client-googl", "GOOGL", quantity="2")
        orders.upsert_broker_order(local_entry, intent_id=entry_intent.intent_id)

        # Managed AAPL position with active linked protection.
        cycles.open_cycle(PositionCycle("cycle-aapl", PAPER_SCOPE.scope_id, "AAPL"))
        aapl_intent = intent("intent-aapl", "client-aapl", "cycle-aapl", "AAPL")
        aapl_receipt = intents.record_intent(aapl_intent, D("500"))
        intents.mark_submitted(aapl_receipt, "entry-aapl")
        aapl_entry = broker_order(
            "entry-aapl", "client-aapl", "AAPL",
            status=BrokerOrderStatus.FILLED, filled="5",
        )
        orders.upsert_broker_order(aapl_entry, intent_id=aapl_intent.intent_id)
        FillRepository(writer, clock).apply_fill(
            Fill(
                "local-aapl-fill", "entry-aapl", PAPER_SCOPE.scope_id, "AAPL",
                OrderSide.BUY, D("5"), D("100"), D("0"), NOW,
            )
        )
        stop_intent = intent(
            "stop-intent-aapl", "stop-client-aapl", "cycle-aapl", "AAPL",
            side=OrderSide.SELL, purpose=OrderPurpose.PROTECTIVE_STOP, stop_price="90",
        )
        stop_receipt = intents.record_intent(stop_intent, D("0"))
        intents.mark_submitted(stop_receipt, "stop-aapl")
        stop = broker_order(
            "stop-aapl", "stop-client-aapl", "AAPL", side=OrderSide.SELL,
            quantity="5", stop_price="90",
        )
        orders.upsert_broker_order(stop, intent_id=stop_intent.intent_id)
        ProtectionRepository(writer, clock).record_protection_order(
            "protection-aapl", "cycle-aapl", ProtectionKind.FIXED_STOP,
            ProtectionStatus.ACTIVE, D("5"), stop_price=D("90"),
            broker_order_id=stop.broker_order_id,
        )
        return local_entry, stop
    finally:
        session.close()


async def test_recovery_reconciles_fill_once_and_blocks_external_and_orphan_stop(
    tmp_path: Path,
) -> None:
    path = tmp_path / "paper-recovery.sqlite3"
    clock = Clock(NOW)
    local_entry, linked_stop = seed_paper_recovery(path, clock)
    filled_entry = broker_order(
        local_entry.broker_order_id,
        local_entry.client_order_id,
        "GOOGL",
        status=BrokerOrderStatus.FILLED,
        quantity="2",
        filled="2",
    )
    orphan_stop = broker_order(
        "orphan-stop", "external-stop", "TSLA", side=OrderSide.SELL,
        quantity="1", stop_price="80",
    )
    broker = OfflineBroker(
        positions=[position("AAPL", "5"), position("GOOGL", "2"), position("MSFT", "3")],
        open_orders=[linked_stop, orphan_stop],
        fills=[fill("broker-fill-googl", "entry-googl", "GOOGL", "2")],
        all_orders={
            filled_entry.broker_order_id: filled_entry,
            linked_stop.broker_order_id: linked_stop,
            orphan_stop.broker_order_id: orphan_stop,
        },
    )

    def runtime_and_reconciler() -> tuple[object, LazyReconciler]:
        persistence = SQLitePersistence(
            path, AccountEnvironment.PAPER, (PAPER_SCOPE,), clock
        )
        reconciler = LazyReconciler(persistence, broker, clock)
        runtime = build_runtime(
            AppSettings(mode="PAPER", db_path=path, account_id="offline"),
            overrides={
                "clock": clock,
                "persistence": persistence,
                "broker": broker,
                "reconciliation": reconciler,
            },
        )
        return runtime, reconciler

    first, first_reconciler = runtime_and_reconciler()
    with pytest.raises(RuntimeStartupError):
        await first.start()  # type: ignore[attr-defined]
    report = first_reconciler.report
    assert report.applied_fill_ids == ("broker-fill-googl",)
    assert [item.symbol for item in report.unmanaged_holdings] == ["MSFT"]
    assert orphan_stop in report.unknown_orders
    assert any(
        item.drift_type == "UNKNOWN_BROKER_ORDER" and item.entity_ref == "orphan-stop"
        for item in report.drift_items
    )
    assert report.entries_permitted is False
    assert first.state is StartupState.BLOCKED  # type: ignore[attr-defined]

    second, second_reconciler = runtime_and_reconciler()
    with pytest.raises(RuntimeStartupError):
        await second.start()  # type: ignore[attr-defined]
    assert second_reconciler.report.applied_fill_ids == ()
    assert broker.cancel_calls == [] and broker.close_calls == []


async def test_instance_lock_contention_and_integrity_failure_block_startup(
    tmp_path: Path,
) -> None:
    clock = Clock(NOW)
    path = tmp_path / "locked.sqlite3"
    first = await start_runtime(path, clock)
    second = build_runtime(
        AppSettings(db_path=path, shadow_scenarios=(scenario(),)),
        overrides={"clock": clock},
    )
    try:
        with pytest.raises(RuntimeStartupError, match="instance_lock_held"):
            await second.start()
        assert second.entries_allowed is False
    finally:
        await first.stop()

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    bad = build_runtime(AppSettings(db_path=corrupt), overrides={"clock": clock})
    with pytest.raises(RuntimeStartupError):
        await bad.start()
    assert bad.state is StartupState.BLOCKED
    assert bad.entries_allowed is False
    assert bad.last_error is not None


async def test_cooldown_and_loss_lockout_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "eligibility.sqlite3"
    clock = Clock(NOW)
    configured = scenario(
        consecutive_loss_limit=1,
        max_entry_cycles_per_session=3,
        post_sale_cooldown=timedelta(minutes=5),
    )
    runtime = await start_runtime(path, clock, scenarios=(configured,))
    await runtime.lifecycle.process({"SPY": upward("SPY", clock.value)})
    clock.value += timedelta(minutes=2)
    await runtime.lifecycle.process({"SPY": downward("SPY", clock.value)})
    state = runtime.broker.scenarios[configured.name].account.state  # type: ignore[attr-defined]
    cooldown = state.cooldown_expiry("SPY")
    assert cooldown is not None
    assert state.losing_streak("SPY") == 1
    await runtime.stop()

    restarted = await start_runtime(path, clock, scenarios=(configured,))
    try:
        restored = restarted.broker.scenarios[configured.name].account.state  # type: ignore[attr-defined]
        assert restored.cooldown_expiry("SPY") == cooldown
        assert restored.losing_streak("SPY") == 1
        block = restored.entry_block("SPY", "vwap_bounce", clock.value)
        assert block is not None and block[0] == "LOSING_STREAK_LOCKOUT"
    finally:
        await restarted.stop()


async def test_durable_pause_keeps_running_runtime_entry_blocked(tmp_path: Path) -> None:
    path = tmp_path / "durable-controls.sqlite3"
    clock = Clock(NOW)
    runtime = await start_runtime(path, clock)
    assert runtime.persistence.session is not None  # type: ignore[attr-defined]
    EntryControlRepository(
        runtime.persistence.session.bind_scope(runtime.account_scope_id),  # type: ignore[attr-defined]
        clock,
    ).set_paused(True, updated_by="offline-test", reason="maintenance")
    await runtime.stop()

    restarted = await start_runtime(path, clock)
    try:
        assert restarted.state is StartupState.RUNNING
        assert restarted.entries_allowed is False
        assert restarted.entry_block_reasons == (
            "ENTRIES_DISABLED_BY_DURABLE_CONTROL",
        )
    finally:
        await restarted.stop()


async def test_stale_heartbeat_dashboard_reports_offline_and_stale_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dashboard.sqlite3"
    clock = Clock(NOW)
    runtime = await start_runtime(path, clock)
    scope = runtime.account_scope_id
    shadow = runtime.broker.scenarios["vwap_reference_shadow"]  # type: ignore[attr-defined]
    assert runtime.persistence.session is not None  # type: ignore[attr-defined]
    CashEquityRepository(
        runtime.persistence.session.bind_scope(scope), clock  # type: ignore[attr-defined]
    ).append_equity_snapshot(await shadow.account.get_account())
    await runtime.stop()

    clock.value += timedelta(seconds=61)
    dashboard = DashboardReadService(
        DashboardSettings(heartbeat_stale_seconds=15, heartbeat_offline_seconds=60),
        clock,
        path,
    )
    status = dashboard.status(scope)
    kpis = dashboard.kpis(scope)
    assert status.engine_state == "OFFLINE"
    assert status.is_stale is True
    assert status.entries_allowed is False
    assert kpis.cash.value == D("25000.00")
    assert kpis.cash.fresh is False
    assert kpis.cash.stale_reason == "engine_offline"
