"""Phase M lifecycle integration: partial fills, manual/external close, crash restart.

End-to-end coverage of the cycle FSM (``mike_trade/portfolio/cycles.py``), the
coordinator (``mike_trade/portfolio/manager.py``), reconciliation
(``mike_trade/execution/reconcile.py``) and projections
(``mike_trade/portfolio/status.py``) as a unit.

All broker interaction uses in-memory fake ports, time uses a manually advanced
fake clock, and every restart rebuilds state from a temporary SQLite database
(``tmp_path``). No network, no broker credentials, and no live or paper broker
orders are used anywhere in this module.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from mike_trade.controls.commands import ControlCommandType
from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.protection import ProtectionStatus
from mike_trade.domain.signals import Signal, SignalAction, SignalProvenance
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import OrderSide
from mike_trade.execution.intents import OrderPurpose
from mike_trade.execution.order_fsm import OrderStatus
from mike_trade.portfolio.cycles import (
    CycleEvent,
    CycleState,
    PositionCycle,
    apply_cycle_event,
    deserialize_cycle,
    serialize_cycle,
)
from mike_trade.portfolio.manager import (
    Disposition,
    EntryGateDecision,
    EntryRequest,
    EntryReservation,
    FillReport,
    OperatorCommand,
    OrderAck,
    OrderRequest,
    PortfolioManager,
    PortfolioManagerConfig,
    ProtectionUpdate,
    RiskExit,
)
from mike_trade.portfolio.status import build_symbol_status, phase_label
from mike_trade.risk.session_policy import ExitKind, MinimumHoldPolicy

D = Decimal
NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
SCOPE = "paper:acct-1"


# ------------------------------------------------------------------ fake clock


class FakeClock:
    """Deterministic, manually advanced UTC clock; the only time source here."""

    def __init__(self, start: datetime = NOW) -> None:
        if start.tzinfo is None or start.utcoffset() != timedelta(0):
            raise ValueError("start must be timezone-aware UTC")
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: int) -> None:
        self.current += timedelta(**delta)


# ------------------------------------------------------- coordinator fake ports


@dataclass
class World:
    """Shared call log plus the injectable behaviour of every fake port."""

    log: list[str] = field(default_factory=list)
    decision: EntryGateDecision = field(default_factory=lambda: EntryGateDecision(True))
    reserve_calls: int = 0
    deny_budget: bool = False
    entry_quantity: Decimal = D("10")
    entry_fills: list[str] = field(default_factory=list)
    released_pending: list[str] = field(default_factory=list)
    closed_cycles: list[PositionCycle] = field(default_factory=list)
    gate_closed: list[PositionCycle] = field(default_factory=list)
    gate_entry_fills: int = 0
    submitted: list[OrderRequest] = field(default_factory=list)
    ack_status: OrderStatus = OrderStatus.ACCEPTED
    cancel_ok: bool = True
    protection_ok: bool = True
    ensured: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    position: Decimal | None = D("0")


class FakeGate:
    def __init__(self, world: World) -> None:
        self.w = world

    async def evaluate_entry(self, account_scope_id, symbol, strategy_id, now):
        await asyncio.sleep(0)
        return self.w.decision

    async def record_entry_fill(self, cycle):
        self.w.gate_entry_fills += 1

    async def record_cycle_closed(self, cycle):
        self.w.gate_closed.append(cycle)


class FakeBudget:
    def __init__(self, world: World) -> None:
        self.w = world

    async def reserve(self, request: EntryRequest):
        await asyncio.sleep(0)
        if self.w.deny_budget:
            return None
        self.w.reserve_calls += 1
        n = self.w.reserve_calls
        return EntryReservation(f"intent-{n}", f"entry-{n}", self.w.entry_quantity)

    async def apply_entry_fill(self, cycle, report):
        self.w.entry_fills.append(report.fill_id)

    async def release_pending(self, cycle):
        self.w.released_pending.append(cycle.cycle_id)

    async def close_cycle(self, cycle):
        self.w.log.append("budget.close")
        self.w.closed_cycles.append(cycle)


class FakeOrders:
    def __init__(self, world: World) -> None:
        self.w = world

    async def submit(self, request: OrderRequest) -> OrderAck:
        self.w.log.append(f"submit.{request.purpose.value}")
        self.w.submitted.append(request)
        return OrderAck(request.client_order_id, self.w.ack_status)

    async def cancel(self, order_id: str) -> bool:
        self.w.log.append(f"cancel.{order_id}")
        return self.w.cancel_ok


class FakeProtection:
    def __init__(self, world: World) -> None:
        self.w = world

    async def cancel_and_confirm(self, cycle) -> bool:
        self.w.log.append("protection.cancel")
        return self.w.protection_ok

    async def ensure_protection(self, cycle, committed_sell_qty) -> None:
        self.w.log.append("protection.ensure")
        self.w.ensured.append((cycle.remaining_qty, committed_sell_qty))


class FakePositions:
    def __init__(self, world: World) -> None:
        self.w = world

    async def confirmed_quantity(self, account_scope_id, symbol):
        return self.w.position


def build(world: World | None = None, clock: FakeClock | None = None, **kwargs):
    world = world or World()
    clock = clock or FakeClock()
    ids = (f"id-{n}" for n in itertools.count(1))
    audits: list = []
    manager = PortfolioManager(
        gate=FakeGate(world),
        budget=FakeBudget(world),
        orders=FakeOrders(world),
        protection=FakeProtection(world),
        positions=FakePositions(world),
        clock=clock,
        audit_sink=audits.append,
        id_factory=lambda: next(ids),
        **kwargs,
    )
    return manager, world, clock, audits


# ---------------------------------------------------------------------- builders


def signal(action: SignalAction, symbol: str = "AAPL", instance: str = "strat_1") -> Signal:
    return Signal.create(
        strategy_key="strat",
        strategy_version="1",
        instance_id=instance,
        symbol=symbol,
        action=action,
        bar_time=NOW,
        generated_at=NOW,
        reference_price=D("100"),
        reason="test-signal",
        provenance=SignalProvenance("p", "f", True, "1m", NOW, {}),
    )


BUY = SignalAction.ENTER_LONG
SELL = SignalAction.EXIT_LONG


def fill(
    symbol: str, order_id: str, side: OrderSide, fill_id: str, qty: str, price: str = "100"
) -> FillReport:
    return FillReport(symbol, order_id, side, NOW, fill_id, D(qty), D(price))


def status_report(symbol: str, order_id: str, side: OrderSide, status: OrderStatus) -> FillReport:
    return FillReport(symbol, order_id, side, NOW, order_status=status)


async def hold(manager, world, *, symbol="AAPL", qty="10", entry_id="entry-1", stop_id="stop-1"):
    """Drive a BUY to a filled, actively protected position."""
    world.entry_quantity = D(qty)
    result = await manager.handle_signal(SCOPE, signal(BUY, symbol))
    assert result.disposition is Disposition.ACCEPTED
    world.position = D(qty)
    await manager.handle_fill(SCOPE, fill(symbol, entry_id, OrderSide.BUY, f"{entry_id}-f", qty))
    await manager.handle_protection_update(
        SCOPE,
        ProtectionUpdate(symbol, ProtectionStatus.ACTIVE, NOW, order_id=stop_id, quantity=D(qty)),
    )
    assert manager.snapshot(SCOPE).cycles[symbol].state is CycleState.WATCHING_SELL
    world.log.clear()
    world.submitted.clear()


def cycle_of(manager, symbol="AAPL") -> PositionCycle:
    return manager.snapshot(SCOPE).cycles[symbol]


# ------------------------------------------------- sqlite snapshot persistence


def persist_cycle(db_path: Path, cycle: PositionCycle) -> None:
    """Durably store one cycle snapshot; the crash-restart path reads it back."""
    payload = json.dumps(serialize_cycle(cycle))
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cycle_snapshots("
            "cycle_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO cycle_snapshots(cycle_id, payload_json) VALUES (?, ?) "
            "ON CONFLICT(cycle_id) DO UPDATE SET payload_json=excluded.payload_json",
            (cycle.cycle_id, payload),
        )
        conn.commit()
    finally:
        conn.close()


def load_cycle(db_path: Path, cycle_id: str) -> PositionCycle:
    """Rebuild a cycle from the SQLite file through a fresh connection."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload_json FROM cycle_snapshots WHERE cycle_id=?", (cycle_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "cycle snapshot missing from SQLite database"
    return deserialize_cycle(json.loads(row[0]))


# ------------------------------------------- 1. partial entry fill then cancel


async def test_partial_entry_fill_consumes_one_slot_and_never_rebuys_remainder() -> None:
    """Buy N=10 fills k=4, the remainder is canceled: one slot, protection for k, no rebuy."""
    manager, world, _, _ = build()
    await manager.handle_signal(SCOPE, signal(BUY))
    assert cycle_of(manager).state is CycleState.BUY_PENDING

    world.position = D("4")
    partial = await manager.handle_fill(SCOPE, fill("AAPL", "entry-1", OrderSide.BUY, "f-1", "4"))
    assert partial.disposition is Disposition.ACCEPTED
    cycle = cycle_of(manager)
    assert cycle.remaining_qty == D("4")
    assert cycle.filled_entry_qty == D("4")
    assert cycle.entry_slot_consumed is True
    # Protection is required for exactly the owned quantity k=4.
    assert world.ensured == [(D("4"), D("0"))]
    assert cycle.protection_status is not None

    canceled = await manager.handle_fill(
        SCOPE, status_report("AAPL", "entry-1", OrderSide.BUY, OrderStatus.CANCELED)
    )
    assert canceled.disposition is Disposition.ACCEPTED
    cycle = cycle_of(manager)
    assert cycle.remaining_qty == D("4")
    assert cycle.state is CycleState.PROTECTIVE_PENDING
    assert len(world.released_pending) == 1

    # Exactly one entry slot was consumed and the remainder is never re-bought.
    assert world.gate_entry_fills == 1
    assert world.reserve_calls == 1
    rebuy = await manager.handle_signal(SCOPE, signal(BUY))
    assert (rebuy.disposition, rebuy.reason) == (Disposition.IGNORED, "position_held")
    assert world.reserve_calls == 1
    assert [o for o in world.submitted if o.purpose is OrderPurpose.ENTRY_LONG] != []
    assert len([o for o in world.submitted if o.purpose is OrderPurpose.ENTRY_LONG]) == 1


# ------------------------------------------- 2. partial exit fill


async def test_partial_exit_fill_updates_remaining_and_blocks_buys_until_flat() -> None:
    """A full-quantity discretionary sell partially fills: remainder shrinks, buys stay blocked."""
    manager, world, _, _ = build()
    await hold(manager, world)
    await manager.handle_signal(SCOPE, signal(SELL, instance="sell_1"))
    sell_id = world.submitted[-1].client_order_id
    world.ensured.clear()

    result = await manager.handle_fill(SCOPE, fill("AAPL", sell_id, OrderSide.SELL, "s-1", "4"))
    assert result.disposition is Disposition.ACCEPTED
    cycle = cycle_of(manager)
    assert (cycle.remaining_qty, cycle.filled_exit_qty) == (D("6"), D("4"))
    assert cycle.filled_entry_qty == D("10")
    # The rest of the sell is still working, so protection must not overlap it.
    assert world.ensured == [(D("6"), D("6"))]

    buy = await manager.handle_signal(SCOPE, signal(BUY))
    assert (buy.disposition, buy.reason) == (Disposition.IGNORED, "position_held")
    assert world.reserve_calls == 1
    assert world.closed_cycles == []
    assert manager.snapshot(SCOPE).consulted_roles("AAPL") == {StrategyRole.SELL}


# ------------------------------------------- 3. manual close bypasses minimum hold


async def test_manual_close_bypasses_minimum_hold_and_closes_full_quantity() -> None:
    """An operator close inside the minimum-hold window closes the full remaining quantity."""
    config = PortfolioManagerConfig(minimum_hold=MinimumHoldPolicy(timedelta(hours=1)))
    manager, world, _, _ = build(config=config)
    await hold(manager, world)

    early = await manager.handle_signal(SCOPE, signal(SELL, instance="sell_1"))
    assert (early.disposition, early.reason) == (
        Disposition.IGNORED,
        "minimum_hold_not_elapsed",
    )
    assert not world.submitted

    [result] = await manager.handle_operator_command(
        OperatorCommand(ControlCommandType.CLOSE_POSITION, SCOPE, "AAPL")
    )
    assert result.disposition is Disposition.ACCEPTED
    order = world.submitted[-1]
    assert order.purpose is OrderPurpose.MANUAL_CLOSE
    assert order.side is OrderSide.SELL
    assert order.quantity == D("10")
    assert cycle_of(manager).state is CycleState.SELL_PENDING


# ------------------------------------------- 4. external/manual close detection


async def test_external_manual_close_detected_and_audited_not_strategy_win(
    tmp_path: Path,
) -> None:
    """Broker position goes to zero with no local sell: audited external/manual, budget freed once."""
    from mike_trade.domain.accounts import AccountScope as _Scope
    from mike_trade.domain.accounts import AccountSnapshot
    from mike_trade.domain.accounts import BrokerCapabilities
    from mike_trade.domain.trading import (
        BrokerOrder,
        BrokerOrderStatus,
        BudgetPolicy,
        Fill,
        OrderIntent,
        OrderPurpose as DomainPurpose,
        OrderType,
        Position,
        PositionCycle as StoredCycle,
        TimeInForce,
    )
    from mike_trade.execution.reconcile import AccountReconciler
    from mike_trade.persistence.repositories import (
        AccountScopeRepository,
        BrokerOrderRepository,
        FillRepository,
        OrderIntentRepository,
        PositionCycleRepository,
    )
    from mike_trade.persistence.schema import initialize_database
    from mike_trade.persistence.transaction import WriterSession

    scope = _Scope(AccountEnvironment.PAPER, "EXT-CLOSE")
    path = tmp_path / "external-close.sqlite3"
    initialize_database(path, AccountEnvironment.PAPER, now=NOW).close()
    session = WriterSession(path)
    try:
        from mike_trade.portfolio.cycles import CycleState as _CycleState

        writer = session.bind_scope(scope.scope_id)
        clock = FakeClock()
        AccountScopeRepository(writer, clock).register_scope(scope, broker="fake")
        budget = BudgetPolicy(D("100000"), D("100000"), 20)
        PositionCycleRepository(writer, clock).open_cycle(
            StoredCycle("cycle-aapl", scope.scope_id, "AAPL", entry_strategy_key="buy-alpha")
        )
        entry_intent = OrderIntent(
            intent_id="intent-aapl",
            client_order_id="client-aapl",
            account_scope_id=scope.scope_id,
            cycle_id="cycle-aapl",
            symbol="AAPL",
            side=OrderSide.BUY,
            purpose=DomainPurpose.ENTRY,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            created_at=NOW,
            quantity=D("10"),
        )
        intents = OrderIntentRepository(writer, clock, budget)
        receipt = intents.record_intent(entry_intent, D("1000"))
        intents.mark_submitted(receipt, broker_order_id="entry-aapl")
        BrokerOrderRepository(writer, clock).upsert_broker_order(
            BrokerOrder(
                broker_order_id="entry-aapl",
                client_order_id="client-aapl",
                account_scope_id=scope.scope_id,
                symbol="AAPL",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                status=BrokerOrderStatus.ACCEPTED,
                quantity=D("10"),
                filled_quantity=D("0"),
                filled_avg_price=None,
                stop_price=None,
                submitted_at=NOW,
                updated_at=NOW,
            ),
            intent_id=entry_intent.intent_id,
        )
        FillRepository(writer, clock).apply_fill(
            Fill(
                fill_id="entry-fill-aapl",
                broker_order_id="entry-aapl",
                account_scope_id=scope.scope_id,
                symbol="AAPL",
                side=OrderSide.BUY,
                quantity=D("5"),
                price=D("100"),
                fee=D("0"),
                filled_at=NOW,
            )
        )

        entry_snapshot = BrokerOrder(
            broker_order_id="entry-aapl",
            client_order_id="client-aapl",
            account_scope_id=scope.scope_id,
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            status=BrokerOrderStatus.ACCEPTED,
            quantity=D("10"),
            filled_quantity=D("5"),
            filled_avg_price=D("100"),
            stop_price=None,
            submitted_at=NOW,
            updated_at=NOW,
        )
        external_order = BrokerOrder(
            broker_order_id="external-sell",
            client_order_id="manual-broker-client",
            account_scope_id=scope.scope_id,
            symbol="AAPL",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            status=BrokerOrderStatus.FILLED,
            quantity=D("5"),
            filled_quantity=D("5"),
            filled_avg_price=D("100"),
            stop_price=None,
            submitted_at=NOW,
            updated_at=NOW,
        )
        external_fill = Fill(
            fill_id="external-fill",
            broker_order_id="external-sell",
            account_scope_id=scope.scope_id,
            symbol="AAPL",
            side=OrderSide.SELL,
            quantity=D("5"),
            price=D("100"),
            fee=D("0"),
            filled_at=NOW,
        )

        from collections.abc import Sequence as _Seq

        class _FakeBroker:
            environment = AccountEnvironment.PAPER

            def __init__(self) -> None:
                self._account = AccountSnapshot(
                    scope=scope,
                    as_of=NOW,
                    cash=D("10000"),
                    equity=D("10000"),
                    buying_power=D("10000"),
                    portfolio_value=D("10000"),
                    trading_blocked=False,
                    source="deterministic-fake",
                )

            def capabilities(self) -> BrokerCapabilities:
                return BrokerCapabilities(
                    provider="deterministic-fake",
                    environment=AccountEnvironment.PAPER,
                    supported_order_types=frozenset({"MARKET", "STOP"}),
                    supports_fractional=False,
                    supports_notional=False,
                    supports_native_trailing_stop=False,
                    supports_extended_hours=False,
                )

            async def get_account(self):
                return self._account

            async def list_positions(self) -> _Seq:
                return ()

            async def list_open_orders(self) -> _Seq:
                return ()

            async def get_order(self, broker_order_id: str):
                return {
                    "entry-aapl": entry_snapshot,
                    "external-sell": external_order,
                }.get(broker_order_id)

            async def get_order_by_client_id(self, client_order_id: str):
                orders = (entry_snapshot, external_order)
                return next(
                    (o for o in orders if o.client_order_id == client_order_id), None
                )

            async def list_fills(self, since: datetime) -> _Seq:
                return (external_fill,) if external_fill.filled_at >= since else ()

        broker = _FakeBroker()
        report = await AccountReconciler(
            broker=broker, writer=writer, clock=clock  # type: ignore[arg-type]
        ).reconcile()

        assert any(i.drift_type == "EXTERNAL_MANUAL_EXIT" for i in report.drift_items)
        row = writer.connection.execute(
            "SELECT state FROM position_cycles WHERE account_scope_id=? AND cycle_id='cycle-aapl'",
            (scope.scope_id,),
        ).fetchone()
        assert row is not None and row[0] == "CLOSED"
        (payload,) = writer.connection.execute(
            "SELECT payload_json FROM domain_events WHERE account_scope_id=? "
            "AND event_type='ExternalManualExitDetected'",
            (scope.scope_id,),
        ).fetchone()
        event = json.loads(payload)
        assert event["classification"] == "external_manual_exit"
        assert event["strategy_exit"] is False
        assert event["strategy_win"] is False
        assert (
            writer.connection.execute(
                "SELECT COUNT(*) FROM domain_events WHERE account_scope_id=? "
                "AND event_type IN ('StrategyExit','StrategyWin')",
                (scope.scope_id,),
            ).fetchone()[0]
            == 0
        )
        # The budget reservation is released exactly once the linked orders are gone.
        (reservation_state,) = writer.connection.execute(
            "SELECT state FROM budget_reservations WHERE account_scope_id=? AND cycle_id=?",
            (scope.scope_id, "cycle-aapl"),
        ).fetchone()
        assert reservation_state == "RELEASED"

        rerun = await AccountReconciler(
            broker=broker, writer=writer, clock=clock  # type: ignore[arg-type]
        ).reconcile()
        assert "external-fill" not in rerun.applied_fill_ids
        assert (
            writer.connection.execute(
                "SELECT COUNT(*) FROM domain_events WHERE account_scope_id=? "
                "AND event_type='ExternalManualExitDetected'",
                (scope.scope_id,),
            ).fetchone()[0]
            == 1
        )
        assert _CycleState.CLOSED is not None
    finally:
        session.close()


# ------------------------------------------- 5. crash restart with missing fill


async def test_crash_restart_applies_missing_fill_exactly_once_and_blocks_duplicate_entry(
    tmp_path: Path,
) -> None:
    """Persist BUY_PENDING to SQLite, lose the broker fill, rebuild, apply it once, block re-entry."""
    db_path = tmp_path / "crash-mid-cycle.sqlite3"
    clock = FakeClock()
    manager, world, _, _ = build(clock=clock)
    first = await manager.handle_signal(SCOPE, signal(BUY))
    assert first.disposition is Disposition.ACCEPTED
    pending = cycle_of(manager)
    assert pending.state is CycleState.BUY_PENDING
    persist_cycle(db_path, pending)
    cycle_id = pending.cycle_id

    # Crash: drop every in-memory object; rebuild the coordinator from the SQLite file.
    del manager, world, first, pending
    restored = load_cycle(db_path, cycle_id)
    assert restored.state is CycleState.BUY_PENDING
    assert restored.remaining_qty == D("0")

    clock2 = FakeClock()
    world2 = World()
    manager2, _, _, _ = build(world2, clock2, cycles=[restored])
    world2.position = D("10")  # the fill happened at the broker during downtime

    missing = fill("AAPL", "entry-1", OrderSide.BUY, "f-1", "10")
    applied = await manager2.handle_fill(SCOPE, missing)
    assert applied.disposition is Disposition.ACCEPTED
    cycle = cycle_of(manager2)
    assert cycle.remaining_qty == D("10")
    assert cycle.filled_entry_qty == D("10")

    replay = await manager2.handle_fill(SCOPE, missing)
    assert replay.disposition is Disposition.DUPLICATE
    assert world2.entry_fills == ["f-1"]
    assert world2.gate_entry_fills == 1

    # Entries stay blocked until protection is confirmed, and the duplicate BUY
    # signal after restart creates no second entry.
    blocked_before = await manager2.handle_signal(SCOPE, signal(BUY))
    assert blocked_before.disposition is Disposition.IGNORED
    assert world2.submitted == []
    protected = await manager2.handle_protection_update(
        SCOPE,
        ProtectionUpdate("AAPL", ProtectionStatus.ACTIVE, NOW, order_id="stop-1", quantity=D("10")),
    )
    assert protected.disposition is Disposition.ACCEPTED
    assert cycle_of(manager2).state is CycleState.WATCHING_SELL
    duplicate_buy = await manager2.handle_signal(SCOPE, signal(BUY))
    assert (duplicate_buy.disposition, duplicate_buy.reason) == (
        Disposition.IGNORED,
        "position_held",
    )
    assert world2.reserve_calls == 0
    assert world2.submitted == []
    assert cycle_of(manager2).entry_order_ids == ("entry-1",)
    assert manager2.snapshot(SCOPE).consulted_roles("AAPL") == {StrategyRole.SELL}
    status = build_symbol_status(cycle_of(manager2), mode="PAPER", now=NOW)
    assert status.held_quantity == D("10")
    assert status.entry_slots_used == 1


# ------------------------------------------- 6. crash restart with unknown order / missing stop


async def test_crash_restart_unknown_order_or_missing_stop_stays_reconciling(
    tmp_path: Path,
) -> None:
    """An unknown broker order plus missing protection keeps the symbol RECONCILING: no entries."""
    from mike_trade.domain.accounts import AccountScope as _Scope
    from mike_trade.domain.accounts import AccountSnapshot
    from mike_trade.domain.accounts import BrokerCapabilities
    from mike_trade.domain.trading import (
        BrokerOrder,
        BrokerOrderStatus,
        BudgetPolicy,
        Fill,
        OrderIntent,
        OrderPurpose as DomainPurpose,
        OrderType,
        Position,
        PositionCycle as StoredCycle,
        TimeInForce,
    )
    from mike_trade.execution.reconcile import AccountReconciler
    from mike_trade.persistence.repositories import (
        AccountScopeRepository,
        BrokerOrderRepository,
        FillRepository,
        OrderIntentRepository,
        PositionCycleRepository,
    )
    from mike_trade.persistence.schema import initialize_database
    from mike_trade.persistence.transaction import WriterSession

    scope = _Scope(AccountEnvironment.PAPER, "RECON-DRIFT")
    path = tmp_path / "crash-unknown.sqlite3"
    initialize_database(path, AccountEnvironment.PAPER, now=NOW).close()
    session = WriterSession(path)
    try:
        writer = session.bind_scope(scope.scope_id)
        clock = FakeClock()
        AccountScopeRepository(writer, clock).register_scope(scope, broker="fake")
        budget = BudgetPolicy(D("100000"), D("100000"), 20)
        PositionCycleRepository(writer, clock).open_cycle(
            StoredCycle("cycle-aapl", scope.scope_id, "AAPL", entry_strategy_key="buy-alpha")
        )
        entry_intent = OrderIntent(
            intent_id="intent-aapl",
            client_order_id="client-aapl",
            account_scope_id=scope.scope_id,
            cycle_id="cycle-aapl",
            symbol="AAPL",
            side=OrderSide.BUY,
            purpose=DomainPurpose.ENTRY,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            created_at=NOW,
            quantity=D("5"),
        )
        intents = OrderIntentRepository(writer, clock, budget)
        receipt = intents.record_intent(entry_intent, D("500"))
        intents.mark_submitted(receipt, broker_order_id="entry-aapl")
        entry_order = BrokerOrder(
            broker_order_id="entry-aapl",
            client_order_id="client-aapl",
            account_scope_id=scope.scope_id,
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            status=BrokerOrderStatus.FILLED,
            quantity=D("5"),
            filled_quantity=D("5"),
            filled_avg_price=D("100"),
            stop_price=None,
            submitted_at=NOW,
            updated_at=NOW,
        )
        BrokerOrderRepository(writer, clock).upsert_broker_order(
            entry_order, intent_id=entry_intent.intent_id
        )
        FillRepository(writer, clock).apply_fill(
            Fill(
                fill_id="entry-fill-aapl",
                broker_order_id="entry-aapl",
                account_scope_id=scope.scope_id,
                symbol="AAPL",
                side=OrderSide.BUY,
                quantity=D("5"),
                price=D("100"),
                fee=D("0"),
                filled_at=NOW,
            )
        )
        # No protective stop is recorded locally: the stop is missing.
        mystery = BrokerOrder(
            broker_order_id="mystery-order",
            client_order_id="outside-client",
            account_scope_id=scope.scope_id,
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            status=BrokerOrderStatus.ACCEPTED,
            quantity=D("5"),
            filled_quantity=D("0"),
            filled_avg_price=None,
            stop_price=None,
            submitted_at=NOW,
            updated_at=NOW,
        )

        from collections.abc import Sequence as _Seq

        class _FakeBroker:
            environment = AccountEnvironment.PAPER

            def capabilities(self) -> BrokerCapabilities:
                return BrokerCapabilities(
                    provider="deterministic-fake",
                    environment=AccountEnvironment.PAPER,
                    supported_order_types=frozenset({"MARKET", "STOP"}),
                    supports_fractional=False,
                    supports_notional=False,
                    supports_native_trailing_stop=False,
                    supports_extended_hours=False,
                )

            async def get_account(self):
                return AccountSnapshot(
                    scope=scope,
                    as_of=NOW,
                    cash=D("10000"),
                    equity=D("10000"),
                    buying_power=D("10000"),
                    portfolio_value=D("10000"),
                    trading_blocked=False,
                    source="deterministic-fake",
                )

            async def list_positions(self) -> _Seq:
                return (
                    Position(
                        account_scope_id=scope.scope_id,
                        symbol="AAPL",
                        quantity=D("5"),
                        avg_entry_price=D("100"),
                        cost_basis=D("500"),
                        as_of=NOW,
                    ),
                )

            async def list_open_orders(self) -> _Seq:
                return (mystery,)

            async def get_order(self, broker_order_id: str):
                return {
                    "entry-aapl": entry_order,
                    "mystery-order": mystery,
                }.get(broker_order_id)

            async def get_order_by_client_id(self, client_order_id: str):
                orders = (entry_order, mystery)
                return next(
                    (o for o in orders if o.client_order_id == client_order_id), None
                )

            async def list_fills(self, since: datetime) -> _Seq:
                return ()

        report = await AccountReconciler(
            broker=_FakeBroker(), writer=writer, clock=clock  # type: ignore[arg-type]
        ).reconcile()
        status = report.per_symbol_status["AAPL"]
        assert status.state is CycleState.RECONCILING
        assert report.entries_permitted is False
        assert report.unknown_orders == (mystery,)
        assert "PROTECTION_COVERAGE_MISSING" in status.reasons
        assert "UNKNOWN_BROKER_ORDER" in status.reasons
    finally:
        session.close()


# ------------------------------------------- 7. external holdings are UNMANAGED


async def test_external_holding_is_unmanaged_and_not_adopted(tmp_path: Path) -> None:
    """A pre-existing broker position with no managed cycle projects UNMANAGED and is never adopted."""
    from mike_trade.domain.accounts import AccountScope as _Scope
    from mike_trade.domain.accounts import AccountSnapshot
    from mike_trade.domain.accounts import BrokerCapabilities
    from mike_trade.domain.trading import Position
    from mike_trade.execution.reconcile import AccountReconciler
    from mike_trade.persistence.repositories import AccountScopeRepository
    from mike_trade.persistence.schema import initialize_database
    from mike_trade.persistence.transaction import WriterSession

    scope = _Scope(AccountEnvironment.PAPER, "UNMANAGED")
    path = tmp_path / "unmanaged.sqlite3"
    initialize_database(path, AccountEnvironment.PAPER, now=NOW).close()
    session = WriterSession(path)
    try:
        writer = session.bind_scope(scope.scope_id)
        clock = FakeClock()
        AccountScopeRepository(writer, clock).register_scope(scope, broker="fake")

        from collections.abc import Sequence as _Seq

        class _FakeBroker:
            environment = AccountEnvironment.PAPER

            def capabilities(self) -> BrokerCapabilities:
                return BrokerCapabilities(
                    provider="deterministic-fake",
                    environment=AccountEnvironment.PAPER,
                    supported_order_types=frozenset({"MARKET", "STOP"}),
                    supports_fractional=False,
                    supports_notional=False,
                    supports_native_trailing_stop=False,
                    supports_extended_hours=False,
                )

            async def get_account(self):
                return AccountSnapshot(
                    scope=scope,
                    as_of=NOW,
                    cash=D("10000"),
                    equity=D("10000"),
                    buying_power=D("10000"),
                    portfolio_value=D("10000"),
                    trading_blocked=False,
                    source="deterministic-fake",
                )

            async def list_positions(self) -> _Seq:
                return (
                    Position(
                        account_scope_id=scope.scope_id,
                        symbol="MSFT",
                        quantity=D("3"),
                        avg_entry_price=D("100"),
                        cost_basis=D("300"),
                        as_of=NOW,
                    ),
                )

            async def list_open_orders(self) -> _Seq:
                return ()

            async def get_order(self, broker_order_id: str):
                return None

            async def get_order_by_client_id(self, client_order_id: str):
                return None

            async def list_fills(self, since: datetime) -> _Seq:
                return ()

        report = await AccountReconciler(
            broker=_FakeBroker(), writer=writer, clock=clock  # type: ignore[arg-type]
        ).reconcile()
        assert report.unmanaged_holdings[0].symbol == "MSFT"
        assert report.per_symbol_status["MSFT"].state is CycleState.UNMANAGED
        assert report.entries_permitted is False
        # The holding is never adopted into managed state.
        assert (
            writer.connection.execute(
                "SELECT COUNT(*) FROM position_cycles WHERE account_scope_id=?",
                (scope.scope_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            writer.connection.execute(
                "SELECT COUNT(*) FROM positions WHERE account_scope_id=?",
                (scope.scope_id,),
            ).fetchone()[0]
            == 0
        )

        # The status projection renders the external holding as unmanaged.
        external = PositionCycle(
            scope.scope_id,
            "MSFT",
            "external-1",
            state=CycleState.UNMANAGED,
            filled_entry_qty=D("3"),
            remaining_qty=D("3"),
            average_entry_price=D("100"),
            first_positive_fill_at=NOW,
            entry_slot_consumed=True,
        )
        view = build_symbol_status(external, mode="PAPER", now=NOW)
        assert view.unmanaged is True
        assert view.cycle_state is CycleState.UNMANAGED
        assert view.phase_label == phase_label(CycleState.UNMANAGED)
        assert view.held_quantity == D("3")
        assert view.remaining_managed_quantity == D("0")
    finally:
        session.close()


# ------------------------------------------- 8. pause semantics


async def test_pause_blocks_entries_but_fills_and_protection_still_apply() -> None:
    """Operator pause blocks new entries while fills and protection updates keep applying."""
    manager, world, _, _ = build()
    await hold(manager, world)
    await manager.handle_operator_command(OperatorCommand(ControlCommandType.PAUSE, SCOPE))
    assert manager.snapshot(SCOPE).consulted_roles("MSFT") == frozenset()

    blocked = await manager.handle_signal(SCOPE, signal(BUY, symbol="MSFT"))
    assert (blocked.disposition, blocked.reason) == (Disposition.BLOCKED, "operator_paused")

    sold = await manager.handle_signal(SCOPE, signal(SELL, instance="sell_1"))
    assert sold.disposition is Disposition.ACCEPTED
    sell_id = world.submitted[-1].client_order_id
    partial = await manager.handle_fill(SCOPE, fill("AAPL", sell_id, OrderSide.SELL, "s-1", "3"))
    assert partial.disposition is Disposition.ACCEPTED
    assert cycle_of(manager).remaining_qty == D("7")
    protected = await manager.handle_protection_update(
        SCOPE, ProtectionUpdate("AAPL", ProtectionStatus.CANCELED, NOW, order_id="stop-1")
    )
    assert protected.disposition is Disposition.ACCEPTED

    still_blocked = await manager.handle_signal(SCOPE, signal(BUY, symbol="MSFT"))
    assert (still_blocked.disposition, still_blocked.reason) == (
        Disposition.BLOCKED,
        "operator_paused",
    )

    # The projection keeps reporting the pause as an entry block.
    paused, _ = apply_cycle_event(
        cycle_of(manager),
        CycleEvent(event_type=CycleEvent.OPERATOR_PAUSE, occurred_at=NOW, reason="operator"),
    )
    assert paused.state is CycleState.PAUSED
    view = build_symbol_status(paused, mode="PAPER", now=NOW)
    assert view.entries_blocked is True
    assert "paused" in view.block_reasons
