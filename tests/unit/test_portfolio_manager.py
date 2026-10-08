from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mike_trade.controls.commands import ControlCommandType
from mike_trade.domain.protection import ProtectionStatus
from mike_trade.domain.signals import Signal, SignalAction, SignalProvenance
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import OrderSide
from mike_trade.execution.intents import OrderPurpose
from mike_trade.execution.order_fsm import OrderStatus
from mike_trade.portfolio.cycles import CycleState, PositionCycle
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
from mike_trade.risk.session_policy import ExitKind, MinimumHoldPolicy

D = Decimal
NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
SCOPE = "paper:acct-1"


# ------------------------------------------------------------------ in-memory fakes


class Clock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: int) -> None:
        self.current += timedelta(**delta)


@dataclass
class World:
    """Shared call log plus the injectable behaviour of every fake port."""

    log: list[str] = field(default_factory=list)
    decision: EntryGateDecision = field(default_factory=lambda: EntryGateDecision(True))
    gate_wait: asyncio.Event | None = None
    gate_calls: int = 0
    reserve_calls: int = 0
    deny_budget: bool = False
    entry_quantity: Decimal = D("10")
    entry_fills: list[str] = field(default_factory=list)
    released_pending: list[str] = field(default_factory=list)
    closed_cycles: list[PositionCycle] = field(default_factory=list)
    gate_closed: list[PositionCycle] = field(default_factory=list)
    gate_entry_fills: int = 0
    submitted: list[OrderRequest] = field(default_factory=list)
    submit_error: Exception | None = None
    ack_status: OrderStatus = OrderStatus.ACCEPTED
    cancel_ok: bool = True
    protection_ok: bool = True
    ensured: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    position: Decimal | None = D("0")


class FakeGate:
    def __init__(self, world: World) -> None:
        self.w = world

    async def evaluate_entry(self, account_scope_id, symbol, strategy_id, now):
        self.w.gate_calls += 1
        if self.w.gate_wait is not None and account_scope_id.endswith("slow"):
            await self.w.gate_wait.wait()
        await asyncio.sleep(0)  # force interleaving of concurrent handlers
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
        if self.w.submit_error is not None and request.purpose is OrderPurpose.ENTRY_LONG:
            raise self.w.submit_error
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


def build(world: World | None = None, clock: Clock | None = None, **kwargs):
    world = world or World()
    clock = clock or Clock()
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


# ------------------------------------------------------------------- entry / buy


async def test_buy_reserves_and_submits_one_entry() -> None:
    manager, world, _, _ = build()
    result = await manager.handle_signal(SCOPE, signal(BUY))
    assert result.disposition is Disposition.ACCEPTED
    assert cycle_of(manager).state is CycleState.BUY_PENDING
    assert cycle_of(manager).entry_intent_id == "intent-1"
    assert cycle_of(manager).originating_buy_strategy_id == "strat_1"
    [order] = world.submitted
    assert (order.purpose, order.side, order.quantity) == (
        OrderPurpose.ENTRY_LONG,
        OrderSide.BUY,
        D("10"),
    )
    assert world.reserve_calls == 1


async def test_concurrent_buy_signals_for_one_symbol_reserve_exactly_once() -> None:
    manager, world, _, _ = build()
    results = await asyncio.gather(*(manager.handle_signal(SCOPE, signal(BUY)) for _ in range(4)))
    dispositions = sorted(result.disposition.value for result in results)
    assert dispositions == ["ACCEPTED", "IGNORED", "IGNORED", "IGNORED"]
    assert world.reserve_calls == 1
    assert len(world.submitted) == 1
    assert world.gate_calls == 1


async def test_accounts_are_serialized_independently() -> None:
    manager, world, _, _ = build()
    world.gate_wait = asyncio.Event()
    slow = asyncio.create_task(manager.handle_signal("paper:slow", signal(BUY)))
    await asyncio.sleep(0)
    other = await asyncio.wait_for(manager.handle_signal(SCOPE, signal(BUY)), timeout=1)
    assert other.disposition is Disposition.ACCEPTED
    assert not slow.done()
    world.gate_wait.set()
    assert (await slow).disposition is Disposition.ACCEPTED


async def test_recurring_buy_is_ignored_and_audited_until_flat_and_rearmed() -> None:
    manager, world, clock, audits = build(
        config=PortfolioManagerConfig(post_close_cooldown=timedelta(minutes=10))
    )
    await hold(manager, world)
    ignored = await manager.handle_signal(SCOPE, signal(BUY))
    assert (ignored.disposition, ignored.reason) == (Disposition.IGNORED, "position_held")
    assert any(
        a["event_type"] == "BuySignalIgnored" and a["reason"] == "position_held" for a in audits
    )
    assert world.reserve_calls == 1

    await manager.handle_risk_exit(SCOPE, RiskExit("AAPL"))
    world.position = D("0")
    exit_id = world.submitted[-1].client_order_id
    await manager.handle_fill(SCOPE, fill("AAPL", exit_id, OrderSide.SELL, "sell-f", "10"))
    assert cycle_of(manager).state is CycleState.CLOSED

    cooling = await manager.handle_signal(SCOPE, signal(BUY))
    assert (cooling.disposition, cooling.reason) == (Disposition.IGNORED, "cooldown_active")
    assert world.reserve_calls == 1

    old_cycle_id = cycle_of(manager).cycle_id
    clock.advance(minutes=11)
    again = await manager.handle_signal(SCOPE, signal(BUY))
    assert again.disposition is Disposition.ACCEPTED
    assert cycle_of(manager).cycle_id != old_cycle_id
    assert world.reserve_calls == 2


@pytest.mark.parametrize(
    ("decision", "fragment"),
    [
        (EntryGateDecision(False, ("MAX_ENTRY_CYCLES_REACHED",)), "MAX_ENTRY_CYCLES_REACHED"),
        (EntryGateDecision(False), "ENTRY_NOT_ELIGIBLE"),
        (EntryGateDecision(True, stale_data=True), "STALE_DATA"),
        (EntryGateDecision(True, unresolved_orders=True), "UNRESOLVED_ORDERS"),
        (EntryGateDecision(True, protection_unknown=True), "PROTECTION_UNKNOWN"),
    ],
)
async def test_gate_blocks_entries(decision: EntryGateDecision, fragment: str) -> None:
    manager, world, _, _ = build()
    world.decision = decision
    result = await manager.handle_signal(SCOPE, signal(BUY))
    assert result.disposition is Disposition.BLOCKED
    assert fragment in result.reason
    assert world.reserve_calls == 0 and not world.submitted
    assert cycle_of(manager).state is CycleState.WATCHING_BUY


async def test_budget_denial_blocks_without_cycle_mutation() -> None:
    manager, world, _, _ = build()
    world.deny_budget = True
    result = await manager.handle_signal(SCOPE, signal(BUY))
    assert (result.disposition, result.reason) == (Disposition.BLOCKED, "budget_unavailable")
    assert cycle_of(manager).state is CycleState.WATCHING_BUY and not world.submitted


@pytest.mark.parametrize("state", [CycleState.RECONCILING, CycleState.ERROR, CycleState.UNMANAGED])
async def test_unsafe_cycle_states_block_entries(state: CycleState) -> None:
    extra = (
        {"reconciliation_state": CycleState.WATCHING_BUY} if state is CycleState.RECONCILING else {}
    )
    seeded = PositionCycle(SCOPE, "AAPL", "c-1", state=state, **extra)
    manager, world, _, _ = build(cycles=[seeded])
    result = await manager.handle_signal(SCOPE, signal(BUY))
    assert (result.disposition, result.reason) == (
        Disposition.BLOCKED,
        f"cycle_{state.value.lower()}",
    )
    assert world.gate_calls == 0 and world.reserve_calls == 0


async def test_unknown_protection_status_blocks_entries() -> None:
    seeded = PositionCycle(SCOPE, "AAPL", "c-1", protection_status=ProtectionStatus.UNKNOWN)
    manager, world, _, _ = build(cycles=[seeded])
    result = await manager.handle_signal(SCOPE, signal(BUY))
    assert (result.disposition, result.reason) == (Disposition.BLOCKED, "protection_unknown")
    assert world.reserve_calls == 0


async def test_submit_failure_keeps_reservation_and_reconciles() -> None:
    manager, world, _, _ = build()
    world.submit_error = TimeoutError("ambiguous")
    result = await manager.handle_signal(SCOPE, signal(BUY))
    assert result.disposition is Disposition.RECONCILING
    assert cycle_of(manager).state is CycleState.RECONCILING
    assert world.released_pending == []
    world.submit_error = None
    again = await manager.handle_signal(SCOPE, signal(BUY))
    assert (again.disposition, again.reason) == (Disposition.BLOCKED, "cycle_reconciling")


async def test_rejected_entry_releases_budget_and_rearms_when_flat() -> None:
    manager, world, _, _ = build()
    world.ack_status = OrderStatus.REJECTED
    result = await manager.handle_signal(SCOPE, signal(BUY))
    assert (result.disposition, result.reason) == (Disposition.REJECTED, "entry_rejected")
    assert len(world.released_pending) == 1
    assert cycle_of(manager).state is CycleState.WATCHING_BUY


async def test_canceled_entry_without_fill_requires_verified_flat_before_rearm() -> None:
    manager, world, _, _ = build()
    await manager.handle_signal(SCOPE, signal(BUY))
    world.position = None  # cannot verify flat -> fail closed
    result = await manager.handle_fill(
        SCOPE, status_report("AAPL", "entry-1", OrderSide.BUY, OrderStatus.CANCELED)
    )
    assert result.disposition is Disposition.RECONCILING
    assert world.released_pending == []


async def test_canceled_entry_without_fill_releases_and_rearms() -> None:
    manager, world, _, _ = build()
    await manager.handle_signal(SCOPE, signal(BUY))
    result = await manager.handle_fill(
        SCOPE, status_report("AAPL", "entry-1", OrderSide.BUY, OrderStatus.CANCELED)
    )
    assert result.disposition is Disposition.ACCEPTED
    assert len(world.released_pending) == 1
    assert cycle_of(manager).state is CycleState.WATCHING_BUY


# --------------------------------------------------------------- fills / protection


async def test_buy_fill_updates_budget_slot_and_requests_protection_once() -> None:
    manager, world, _, _ = build()
    await manager.handle_signal(SCOPE, signal(BUY))
    report = fill("AAPL", "entry-1", OrderSide.BUY, "f-1", "10")
    first = await manager.handle_fill(SCOPE, report)
    duplicate = await manager.handle_fill(SCOPE, report)
    assert first.disposition is Disposition.ACCEPTED
    assert duplicate.disposition is Disposition.DUPLICATE
    assert world.entry_fills == ["f-1"] and world.gate_entry_fills == 1
    assert world.ensured == [(D("10"), D("0"))]
    assert cycle_of(manager).state is CycleState.PROTECTIVE_PENDING


async def test_fill_the_cycle_cannot_absorb_forces_reconciliation() -> None:
    manager, world, _, _ = build()
    result = await manager.handle_fill(SCOPE, fill("AAPL", "x", OrderSide.BUY, "f-1", "1"))
    assert result.disposition is Disposition.REJECTED
    await manager.handle_signal(SCOPE, signal(BUY))
    sell_fill = await manager.handle_fill(SCOPE, fill("AAPL", "x", OrderSide.SELL, "f-2", "1"))
    assert sell_fill.disposition is Disposition.RECONCILING
    assert cycle_of(manager).state is CycleState.RECONCILING


async def test_rejected_protection_update_does_not_mutate_the_cycle() -> None:
    manager, world, _, _ = build()
    await hold(manager, world)
    before = cycle_of(manager)
    result = await manager.handle_protection_update(
        SCOPE,
        ProtectionUpdate("AAPL", ProtectionStatus.ACTIVE, NOW, order_id="stop-2", quantity=D("3")),
    )
    assert result.disposition is Disposition.REJECTED
    assert cycle_of(manager) is before


# -------------------------------------------------------------------- full sell


async def test_strategy_sell_cancels_protection_then_sells_the_full_quantity() -> None:
    manager, world, _, _ = build()
    await hold(manager, world)
    result = await manager.handle_signal(SCOPE, signal(SELL, instance="sell_1"))
    assert result.disposition is Disposition.ACCEPTED
    assert world.log == ["protection.cancel", "submit.EXIT_LONG"]
    [order] = world.submitted[-1:]
    assert (order.side, order.quantity) == (OrderSide.SELL, D("10"))
    cycle = cycle_of(manager)
    assert cycle.state is CycleState.SELL_PENDING
    assert cycle.protection_status is ProtectionStatus.CANCELED


async def test_sell_is_deferred_while_protection_cancel_is_unconfirmed() -> None:
    manager, world, _, _ = build()
    await hold(manager, world)
    world.protection_ok = False
    result = await manager.handle_signal(SCOPE, signal(SELL, instance="sell_1"))
    assert (result.disposition, result.reason) == (
        Disposition.DEFERRED,
        "protection_cancel_unconfirmed",
    )
    assert not world.submitted
    assert manager.snapshot(SCOPE).pending_exits == {}  # strategy exits are not retried


async def test_minimum_hold_applies_to_strategy_exits_only() -> None:
    config = PortfolioManagerConfig(minimum_hold=MinimumHoldPolicy(timedelta(minutes=5)))
    manager, world, clock, _ = build(config=config)
    await hold(manager, world)
    early = await manager.handle_signal(SCOPE, signal(SELL, instance="sell_1"))
    assert (early.disposition, early.reason) == (Disposition.IGNORED, "minimum_hold_not_elapsed")
    assert not world.submitted
    clock.advance(minutes=6)
    later = await manager.handle_signal(SCOPE, signal(SELL, instance="sell_1"))
    assert later.disposition is Disposition.ACCEPTED


@pytest.mark.parametrize("via", ["operator", "risk"])
async def test_manual_and_safety_exits_bypass_the_minimum_hold(via: str) -> None:
    config = PortfolioManagerConfig(minimum_hold=MinimumHoldPolicy(timedelta(hours=1)))
    manager, world, _, _ = build(config=config)
    await hold(manager, world)
    if via == "operator":
        [result] = await manager.handle_operator_command(
            OperatorCommand(ControlCommandType.CLOSE_POSITION, SCOPE, "AAPL")
        )
        purpose = OrderPurpose.MANUAL_CLOSE
    else:
        result = await manager.handle_risk_exit(SCOPE, RiskExit("AAPL", ExitKind.DAILY_LOSS))
        purpose = OrderPurpose.SAFETY_EXIT
    assert result.disposition is Disposition.ACCEPTED
    assert world.submitted[-1].purpose is purpose
    assert world.submitted[-1].quantity == D("10")


async def test_partial_exit_fill_resizes_protection_and_keeps_buys_blocked() -> None:
    manager, world, _, _ = build()
    await hold(manager, world)
    await manager.handle_signal(SCOPE, signal(SELL, instance="sell_1"))
    sell_id = world.submitted[-1].client_order_id
    world.ensured.clear()
    result = await manager.handle_fill(SCOPE, fill("AAPL", sell_id, OrderSide.SELL, "s-1", "4"))
    assert result.disposition is Disposition.ACCEPTED
    cycle = cycle_of(manager)
    assert (cycle.remaining_qty, cycle.filled_exit_qty) == (D("6"), D("4"))
    # the rest of the sell is still working, so protection must not overlap it
    assert world.ensured == [(D("6"), D("6"))]
    buy = await manager.handle_signal(SCOPE, signal(BUY))
    assert (buy.disposition, buy.reason) == (Disposition.IGNORED, "position_held")
    assert world.reserve_calls == 1
    assert world.closed_cycles == []


async def test_consulted_roles_are_sell_only_once_quantity_is_held() -> None:
    manager, world, _, _ = build()
    assert manager.snapshot(SCOPE).consulted_roles("AAPL") == {StrategyRole.BUY}
    await hold(manager, world)
    assert manager.snapshot(SCOPE).consulted_roles("AAPL") == {StrategyRole.SELL}
    assert manager.snapshot(SCOPE).consulted_roles("MSFT") == {StrategyRole.BUY}


# ------------------------------------------------------------------ final close


async def test_final_close_releases_budget_and_starts_cooldown_only_after_closed() -> None:
    config = PortfolioManagerConfig(post_close_cooldown=timedelta(minutes=10))
    manager, world, clock, _ = build(config=config)
    await hold(manager, world)
    await manager.handle_signal(SCOPE, signal(SELL, instance="sell_1"))
    sell_id = world.submitted[-1].client_order_id

    world.position = None  # fill arrives but flat cannot be verified yet
    result = await manager.handle_fill(SCOPE, fill("AAPL", sell_id, OrderSide.SELL, "s-1", "10"))
    assert result.disposition is Disposition.ACCEPTED
    assert cycle_of(manager).state is CycleState.CLOSING
    assert world.closed_cycles == [] and world.gate_closed == []

    world.position = D("0")
    await manager.handle_protection_update(
        SCOPE, ProtectionUpdate("AAPL", ProtectionStatus.CANCELED, NOW)
    )
    cycle = cycle_of(manager)
    assert cycle.state is CycleState.CLOSED
    assert cycle.cooldown_until == clock.current + timedelta(minutes=10)
    assert len(world.closed_cycles) == 1 and len(world.gate_closed) == 1


async def test_close_is_refused_while_the_broker_still_shows_a_position() -> None:
    manager, world, _, _ = build()
    await hold(manager, world)
    await manager.handle_signal(SCOPE, signal(SELL, instance="sell_1"))
    sell_id = world.submitted[-1].client_order_id
    world.position = D("2")
    await manager.handle_fill(SCOPE, fill("AAPL", sell_id, OrderSide.SELL, "s-1", "10"))
    assert cycle_of(manager).state is CycleState.RECONCILING
    assert world.closed_cycles == []


async def test_protective_stop_fill_closes_after_verified_flat() -> None:
    manager, world, _, _ = build()
    await hold(manager, world)
    world.position = D("0")
    await manager.handle_fill(SCOPE, fill("AAPL", "stop-1", OrderSide.SELL, "stop-f", "10"))
    cycle = cycle_of(manager)
    assert cycle.state is CycleState.CLOSED
    assert cycle.protection_status is ProtectionStatus.TRIGGERED
    assert len(world.closed_cycles) == 1 and not world.submitted


# ------------------------------------------------------------------- priorities


async def test_manual_close_cancels_and_confirms_a_working_strategy_sell_first() -> None:
    manager, world, _, _ = build()
    await hold(manager, world)
    await manager.handle_signal(SCOPE, signal(SELL, instance="sell_1"))
    strategy_sell = world.submitted[-1].client_order_id
    world.log.clear()

    [result] = await manager.handle_operator_command(
        OperatorCommand(ControlCommandType.CLOSE_POSITION, SCOPE, "AAPL")
    )
    assert result.disposition is Disposition.ACCEPTED
    assert world.log == [f"cancel.{strategy_sell}", "submit.MANUAL_CLOSE"]
    assert world.submitted[-1].quantity == D("10")

    again = await manager.handle_signal(SCOPE, signal(SELL, instance="sell_1"))
    assert (again.disposition, again.reason) == (Disposition.IGNORED, "exit_already_in_flight")
    late = await manager.handle_fill(
        SCOPE, status_report("AAPL", strategy_sell, OrderSide.SELL, OrderStatus.CANCELED)
    )
    assert (late.disposition, late.reason) == (Disposition.IGNORED, "sell_order_not_open")
    assert len(world.submitted) == 2


async def test_priority_exit_waits_for_unconfirmed_cancel_and_never_overlaps() -> None:
    manager, world, _, _ = build()
    await hold(manager, world)
    await manager.handle_signal(SCOPE, signal(SELL, instance="sell_1"))
    strategy_sell = world.submitted[-1].client_order_id
    world.cancel_ok = False

    [result] = await manager.handle_operator_command(
        OperatorCommand(ControlCommandType.CLOSE_POSITION, SCOPE, "AAPL")
    )
    assert (result.disposition, result.reason) == (
        Disposition.DEFERRED,
        "strategy_sell_cancel_unconfirmed",
    )
    assert len(world.submitted) == 1
    assert manager.snapshot(SCOPE).pending_exits == {"AAPL": ExitKind.MANUAL_CLOSE}
    ignored = await manager.handle_signal(SCOPE, signal(SELL, instance="sell_1"))
    assert ignored.reason == "priority_exit_pending" and len(world.submitted) == 1

    world.cancel_ok = True
    await manager.handle_fill(
        SCOPE, status_report("AAPL", strategy_sell, OrderSide.SELL, OrderStatus.CANCELED)
    )
    assert [order.purpose for order in world.submitted] == [
        OrderPurpose.EXIT_LONG,
        OrderPurpose.MANUAL_CLOSE,
    ]
    assert manager.snapshot(SCOPE).pending_exits == {}


async def test_deferred_priority_exit_retries_on_next_protection_event() -> None:
    manager, world, _, _ = build()
    await hold(manager, world)
    world.protection_ok = False
    [result] = await manager.handle_operator_command(
        OperatorCommand(ControlCommandType.CLOSE_POSITION, SCOPE, "AAPL")
    )
    assert result.disposition is Disposition.DEFERRED and not world.submitted
    assert manager.snapshot(SCOPE).pending_exits == {"AAPL": ExitKind.MANUAL_CLOSE}

    await manager.handle_protection_update(
        SCOPE, ProtectionUpdate("AAPL", ProtectionStatus.CANCELED, NOW, order_id="stop-1")
    )
    assert [order.purpose for order in world.submitted] == [OrderPurpose.MANUAL_CLOSE]
    assert manager.snapshot(SCOPE).pending_exits == {}


async def test_exit_quantity_is_capped_at_the_confirmed_position() -> None:
    manager, world, _, audits = build()
    await hold(manager, world)
    world.position = D("4")
    await manager.handle_risk_exit(SCOPE, RiskExit("AAPL"))
    assert world.submitted[-1].quantity == D("4")
    assert any(a["event_type"] == "ExitQuantityCapped" for a in audits)


async def test_unverifiable_position_defers_exit_and_restores_protection() -> None:
    manager, world, _, _ = build()
    await hold(manager, world)
    world.position = None
    result = await manager.handle_risk_exit(SCOPE, RiskExit("AAPL"))
    assert (result.disposition, result.reason) == (Disposition.DEFERRED, "position_unconfirmed")
    assert not world.submitted
    assert world.log[-1] == "protection.ensure"
    assert manager.snapshot(SCOPE).pending_exits == {"AAPL": ExitKind.RISK_SAFETY}


async def test_broker_flat_while_cycle_open_reconciles_instead_of_selling() -> None:
    manager, world, _, _ = build()
    await hold(manager, world)
    world.position = D("0")
    result = await manager.handle_risk_exit(SCOPE, RiskExit("AAPL"))
    assert result.disposition is Disposition.RECONCILING and not world.submitted


async def test_partially_filled_entry_remainder_is_canceled_before_the_exit() -> None:
    manager, world, _, _ = build()
    await manager.handle_signal(SCOPE, signal(BUY))
    world.position = D("4")
    await manager.handle_fill(SCOPE, fill("AAPL", "entry-1", OrderSide.BUY, "f-1", "4"))
    world.log.clear()
    result = await manager.handle_risk_exit(SCOPE, RiskExit("AAPL"))
    assert result.disposition is Disposition.ACCEPTED
    assert world.log == ["cancel.entry-1", "protection.cancel", "submit.SAFETY_EXIT"]
    assert len(world.released_pending) == 1
    assert world.submitted[-1].quantity == D("4")


async def test_sell_ending_without_fill_requires_reconciliation() -> None:
    manager, world, _, _ = build()
    await hold(manager, world)
    await manager.handle_signal(SCOPE, signal(SELL, instance="sell_1"))
    sell_id = world.submitted[-1].client_order_id
    result = await manager.handle_fill(
        SCOPE, status_report("AAPL", sell_id, OrderSide.SELL, OrderStatus.REJECTED)
    )
    assert result.disposition is Disposition.RECONCILING
    assert cycle_of(manager).state is CycleState.RECONCILING


async def test_exit_without_a_position_is_ignored() -> None:
    manager, world, _, _ = build()
    result = await manager.handle_signal(SCOPE, signal(SELL))
    assert (result.disposition, result.reason) == (Disposition.IGNORED, "no_position")
    assert not world.submitted


# ------------------------------------------------------- pause / kill switch / EOD


@pytest.mark.parametrize(
    ("block", "release", "reason"),
    [
        (ControlCommandType.PAUSE, ControlCommandType.RESUME, "operator_paused"),
        (
            ControlCommandType.DISABLE_ENTRIES,
            ControlCommandType.ENABLE_ENTRIES,
            "entry_kill_switch",
        ),
    ],
)
async def test_pause_and_kill_switch_block_entries_only(
    block: ControlCommandType, release: ControlCommandType, reason: str
) -> None:
    manager, world, _, _ = build()
    await manager.handle_operator_command(OperatorCommand(block, SCOPE))
    snapshot = manager.snapshot(SCOPE)
    assert snapshot.consulted_roles("AAPL") == frozenset()
    blocked = await manager.handle_signal(SCOPE, signal(BUY))
    assert (blocked.disposition, blocked.reason) == (Disposition.BLOCKED, reason)

    await manager.handle_operator_command(OperatorCommand(release, SCOPE))
    await hold(manager, world)
    await manager.handle_operator_command(OperatorCommand(block, SCOPE))

    # fills, protection updates, strategy sells and manual close all continue
    sold = await manager.handle_signal(SCOPE, signal(SELL, instance="sell_1"))
    assert sold.disposition is Disposition.ACCEPTED
    sell_id = world.submitted[-1].client_order_id
    partial = await manager.handle_fill(SCOPE, fill("AAPL", sell_id, OrderSide.SELL, "s-1", "3"))
    assert partial.disposition is Disposition.ACCEPTED
    [manual] = await manager.handle_operator_command(
        OperatorCommand(ControlCommandType.CLOSE_POSITION, SCOPE, "AAPL")
    )
    assert manual.disposition is Disposition.ACCEPTED
    assert world.submitted[-1].purpose is OrderPurpose.MANUAL_CLOSE
    assert world.submitted[-1].quantity == D("7")


async def test_eod_liquidation_is_disabled_by_default() -> None:
    assert PortfolioManagerConfig().auto_eod_liquidation is False
    manager, world, _, _ = build()
    await hold(manager, world)
    result = await manager.handle_risk_exit(SCOPE, RiskExit("AAPL", ExitKind.END_OF_DAY))
    assert (result.disposition, result.reason) == (
        Disposition.IGNORED,
        "eod_liquidation_disabled",
    )
    assert not world.submitted
    assert cycle_of(manager).state is CycleState.WATCHING_SELL


async def test_enabled_eod_liquidation_uses_the_priority_exit_path() -> None:
    config = PortfolioManagerConfig(
        auto_eod_liquidation=True, minimum_hold=MinimumHoldPolicy(timedelta(hours=1))
    )
    manager, world, _, _ = build(config=config)
    await hold(manager, world)
    result = await manager.handle_risk_exit(SCOPE, RiskExit("AAPL", ExitKind.END_OF_DAY))
    assert result.disposition is Disposition.ACCEPTED
    assert world.log == ["protection.cancel", "submit.SAFETY_EXIT"]
    assert world.submitted[-1].quantity == D("10")


async def test_close_all_exits_every_open_symbol_and_flatten_is_equivalent() -> None:
    manager, world, _, _ = build()
    await hold(manager, world, symbol="AAPL", entry_id="entry-1", stop_id="stop-1")
    world.position = D("10")
    await hold(manager, world, symbol="MSFT", entry_id="entry-2", stop_id="stop-2")
    results = await manager.handle_operator_command(
        OperatorCommand(ControlCommandType.FLATTEN, SCOPE)
    )
    assert [r.symbol for r in results] == ["AAPL", "MSFT"]
    assert all(r.disposition is Disposition.ACCEPTED for r in results)
    assert {o.symbol for o in world.submitted if o.purpose is OrderPurpose.MANUAL_CLOSE} == {
        "AAPL",
        "MSFT",
    }
    [empty] = await manager.handle_operator_command(
        OperatorCommand(ControlCommandType.CLOSE_ALL, "paper:other")
    )
    assert (empty.disposition, empty.reason) == (Disposition.IGNORED, "no_open_positions")


async def test_operator_command_validation_and_unsupported_types() -> None:
    manager, _, _, _ = build()
    with pytest.raises(ValueError, match="requires a symbol"):
        await manager.handle_operator_command(
            OperatorCommand(ControlCommandType.CLOSE_POSITION, SCOPE)
        )
    [result] = await manager.handle_operator_command(
        OperatorCommand(ControlCommandType.RECONCILE_NOW, SCOPE)
    )
    assert (result.disposition, result.reason) == (Disposition.IGNORED, "unsupported_command")
    with pytest.raises(ValueError, match="not a risk/safety exit kind"):
        RiskExit("AAPL", ExitKind.STRATEGY)


async def test_every_handler_result_carries_audit_records() -> None:
    manager, world, _, audits = build()
    result = await manager.handle_signal(SCOPE, signal(BUY))
    kinds = [a["event_type"] for a in result.audits]
    assert kinds[-1] == "PortfolioDecision" and "CycleTransitioned" in kinds
    assert list(result.audits) == audits
