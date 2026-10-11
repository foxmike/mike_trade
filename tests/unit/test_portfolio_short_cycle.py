"""Production cycle FSM and PortfolioManager for SHORT cycles.

    FLAT -> entry pending (SELL_SHORT) -> SHORT -> exit pending (BUY_TO_COVER) -> FLAT

The long lifecycle is covered by ``test_portfolio_manager``; these tests prove that the same
manager runs the mirrored lifecycle, that shorting is opt-in and fails closed, that roles never
cross (SELL never closes a short, COVER never closes a long), that a fill with the wrong side can
never reverse a cycle, and that a short is verified only through a short-aware position port.
"""

from __future__ import annotations

import itertools
import json
from decimal import Decimal as D

import pytest
from test_portfolio_manager import (
    NOW,
    SCOPE,
    Clock,
    FakeBudget,
    FakeGate,
    FakeOrders,
    FakeProtection,
    World,
    fill,
    signal,
)

from mike_trade.domain.protection import ProtectionStatus
from mike_trade.domain.signals import SignalAction
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import OrderSide, PositionDirection
from mike_trade.execution.intents import OrderPurpose
from mike_trade.portfolio.cycles import (
    CycleEvent,
    CycleState,
    CycleTransitionError,
    PositionCycle,
    apply_cycle_event,
    deserialize_cycle,
    serialize_cycle,
)
from mike_trade.portfolio.manager import (
    Disposition,
    PortfolioManager,
    PortfolioManagerConfig,
    ProtectionUpdate,
)

SHORT = SignalAction.ENTER_SHORT
COVER = SignalAction.COVER_SHORT
BUY = SignalAction.ENTER_LONG
SELL = SignalAction.EXIT_LONG


class ShortAwarePositions:
    """Long and short broker-confirmed quantities, like a real direction-aware port."""

    def __init__(self, world: World) -> None:
        self.w = world
        self.short: D | None = D("0")

    async def confirmed_quantity(self, account_scope_id, symbol):
        return self.w.position

    async def confirmed_short_quantity(self, account_scope_id, symbol):
        return self.short


class LongOnlyPositions:
    """A legacy port: it can only report longs, so a short is unverifiable."""

    def __init__(self, world: World) -> None:
        self.w = world

    async def confirmed_quantity(self, account_scope_id, symbol):
        return self.w.position


def build(*, authorized=True, positions=None, config=None):
    world, clock = World(), Clock()
    ids = (f"id-{n}" for n in itertools.count(1))
    ports = (positions or ShortAwarePositions)(world)
    manager = PortfolioManager(
        gate=FakeGate(world), budget=FakeBudget(world), orders=FakeOrders(world),
        protection=FakeProtection(world), positions=ports, clock=clock,
        id_factory=lambda: next(ids),
        config=config or PortfolioManagerConfig(short_authorized=authorized),
    )
    return manager, world, clock, ports


def cycle_of(manager, symbol="AAPL") -> PositionCycle:
    return manager.snapshot(SCOPE).cycles[symbol]


async def hold_short(manager, world, ports, *, qty="10", entry="entry-1", stop="stop-1"):
    world.entry_quantity = D(qty)
    result = await manager.handle_signal(SCOPE, signal(SHORT))
    assert result.disposition is Disposition.ACCEPTED, result.reason
    ports.short = D(qty)
    await manager.handle_fill(SCOPE, fill("AAPL", entry, OrderSide.SELL_SHORT, f"{entry}-f", qty))
    await manager.handle_protection_update(SCOPE, ProtectionUpdate(
        "AAPL", ProtectionStatus.ACTIVE, NOW, order_id=stop, quantity=D(qty)))
    assert cycle_of(manager).state is CycleState.WATCHING_SELL
    world.log.clear()
    world.submitted.clear()


# ------------------------------------------------------------------ entry (opt-in, fail closed)


async def test_a_short_entry_is_blocked_unless_short_selling_is_authorized():
    manager, world, _, _ = build(authorized=False)
    result = await manager.handle_signal(SCOPE, signal(SHORT))
    assert (result.disposition, result.reason) == (Disposition.BLOCKED,
                                                    "short_selling_not_authorized")
    assert world.reserve_calls == 0 and world.submitted == [] and world.gate_calls == 0
    assert (cycle_of(manager, "AAPL") if "AAPL" in manager.snapshot(SCOPE).cycles else None) is None


async def test_an_authorized_short_reserves_and_submits_one_sell_short_entry():
    manager, world, _, _ = build()
    result = await manager.handle_signal(SCOPE, signal(SHORT))
    assert result.disposition is Disposition.ACCEPTED
    cycle = cycle_of(manager)
    assert cycle.direction is PositionDirection.SHORT and cycle.state is CycleState.BUY_PENDING
    [order] = world.submitted
    assert (order.purpose, order.side, order.quantity) == (
        OrderPurpose.ENTRY_SHORT, OrderSide.SELL_SHORT, D("10"))
    assert world.reserve_calls == 1


async def test_a_held_position_of_either_direction_blocks_every_entry():
    manager, world, _, ports = build()
    await hold_short(manager, world, ports)
    for action in (SHORT, BUY):
        again = await manager.handle_signal(SCOPE, signal(action, instance=f"s-{action.value}"))
        assert (again.disposition, again.reason) == (Disposition.IGNORED, "position_held")
    assert world.submitted == []


# ------------------------------------------------------------ the full short lifecycle


async def test_short_then_cover_runs_the_mirrored_lifecycle_to_a_verified_flat():
    manager, world, clock, ports = build()
    await hold_short(manager, world, ports)
    cycle = cycle_of(manager)
    assert cycle.direction is PositionDirection.SHORT and cycle.remaining_qty == D("10")
    assert cycle.entry_side is OrderSide.SELL_SHORT and cycle.exit_side is OrderSide.BUY_TO_COVER
    clock.advance(minutes=5)
    result = await manager.handle_signal(SCOPE, signal(COVER, instance="cover_1"))
    assert result.disposition is Disposition.ACCEPTED
    assert world.log[:2] == ["protection.cancel", "submit.EXIT_SHORT"]  # stop removed first
    [exit_order] = world.submitted
    assert (exit_order.side, exit_order.purpose, exit_order.quantity) == (
        OrderSide.BUY_TO_COVER, OrderPurpose.EXIT_SHORT, D("10"))
    assert cycle_of(manager).state is CycleState.SELL_PENDING
    ports.short = D("0")
    await manager.handle_fill(SCOPE, fill(
        "AAPL", exit_order.client_order_id, OrderSide.BUY_TO_COVER, "cover-f", "10", "94"))
    assert cycle_of(manager).state is CycleState.CLOSED  # zero short verified, budget released
    assert world.closed_cycles and world.gate_closed


async def test_a_cover_is_capped_at_the_confirmed_short_quantity():
    manager, world, clock, ports = build()
    await hold_short(manager, world, ports)
    ports.short = D("6")  # the broker shows less than the cycle believes
    clock.advance(minutes=5)
    await manager.handle_signal(SCOPE, signal(COVER, instance="cover_1"))
    [exit_order] = world.submitted
    assert exit_order.quantity == D("6")  # never more than the confirmed short: no reversal


async def test_a_protective_stop_fill_closes_the_short_after_a_verified_flat():
    manager, world, _, ports = build()
    await hold_short(manager, world, ports, stop="stop-1")
    ports.short = D("0")
    await manager.handle_fill(SCOPE, fill(
        "AAPL", "stop-1", OrderSide.BUY_TO_COVER, "stop-f", "10", "103"))
    assert cycle_of(manager).state is CycleState.CLOSED


# ----------------------------------------------------------------- roles never cross


async def test_a_sell_never_closes_a_short_and_a_cover_never_closes_a_long():
    manager, world, clock, ports = build()
    await hold_short(manager, world, ports)
    clock.advance(minutes=5)
    wrong = await manager.handle_signal(SCOPE, signal(SELL, instance="sell_1"))
    assert (wrong.disposition, wrong.reason) == (Disposition.IGNORED, "wrong_direction")
    assert world.submitted == [] and cycle_of(manager).state is CycleState.WATCHING_SELL
    # mirror: a held LONG ignores COVER
    manager2, world2, clock2, _ = build()
    world2.entry_quantity = D("10")
    await manager2.handle_signal(SCOPE, signal(BUY))
    world2.position = D("10")
    await manager2.handle_fill(SCOPE, fill("AAPL", "entry-1", OrderSide.BUY, "f1", "10"))
    await manager2.handle_protection_update(SCOPE, ProtectionUpdate(
        "AAPL", ProtectionStatus.ACTIVE, NOW, order_id="stop-1", quantity=D("10")))
    world2.submitted.clear()
    clock2.advance(minutes=5)
    wrong = await manager2.handle_signal(SCOPE, signal(COVER, instance="cover_1"))
    assert (wrong.disposition, wrong.reason) == (Disposition.IGNORED, "wrong_direction")
    assert world2.submitted == []


async def test_a_fill_with_the_wrong_side_can_never_reverse_a_cycle():
    manager, world, _, ports = build()
    await hold_short(manager, world, ports)
    result = await manager.handle_fill(SCOPE, fill(
        "AAPL", "rogue", OrderSide.SELL, "rogue-f", "10"))  # a SELL against a short
    assert result.disposition is Disposition.RECONCILING
    assert result.reason == "fill_rejected_by_cycle"
    assert cycle_of(manager).state is CycleState.RECONCILING
    assert cycle_of(manager).remaining_qty == D("10")  # untouched


# ----------------------------------------------- verification fails closed for shorts


async def test_a_port_that_cannot_report_shorts_leaves_a_short_unverified_and_defers_the_exit():
    manager, world, clock, _ = build(positions=LongOnlyPositions)
    world.entry_quantity = D("10")
    await manager.handle_signal(SCOPE, signal(SHORT))
    await manager.handle_fill(SCOPE, fill("AAPL", "entry-1", OrderSide.SELL_SHORT, "f1", "10"))
    await manager.handle_protection_update(SCOPE, ProtectionUpdate(
        "AAPL", ProtectionStatus.ACTIVE, NOW, order_id="stop-1", quantity=D("10")))
    world.submitted.clear()
    clock.advance(minutes=5)
    result = await manager.handle_signal(SCOPE, signal(COVER, instance="cover_1"))
    assert (result.disposition, result.reason) == (Disposition.DEFERRED, "position_unconfirmed")
    assert world.submitted == []  # nothing is sent on an unverified short


async def test_a_short_whose_broker_position_is_flat_reconciles_instead_of_covering():
    manager, world, clock, ports = build()
    await hold_short(manager, world, ports)
    ports.short = D("0")
    clock.advance(minutes=5)
    result = await manager.handle_signal(SCOPE, signal(COVER, instance="cover_1"))
    assert result.disposition is Disposition.RECONCILING
    assert result.reason == "broker_flat_while_cycle_open" and world.submitted == []


# ------------------------------------------------------------------- consulted roles


async def test_consulted_roles_follow_the_held_direction_and_the_authorization():
    manager, world, _, ports = build()
    assert manager.snapshot(SCOPE).consulted_roles("AAPL") == {StrategyRole.BUY,
                                                                StrategyRole.SHORT}
    assert build(authorized=False)[0].snapshot(SCOPE).consulted_roles("AAPL") == {
        StrategyRole.BUY}
    await hold_short(manager, world, ports)
    assert manager.snapshot(SCOPE).consulted_roles("AAPL") == {StrategyRole.COVER}


# ------------------------------------------------------------------ the pure FSM


def test_a_short_cycle_serializes_with_its_direction_and_old_snapshots_default_to_long():
    cycle = PositionCycle(SCOPE, "AAPL", "c1", direction=PositionDirection.SHORT)
    restored = deserialize_cycle(json.loads(json.dumps(serialize_cycle(cycle))))
    assert restored == cycle and restored.direction is PositionDirection.SHORT
    legacy = serialize_cycle(PositionCycle(SCOPE, "AAPL", "c2"))
    del legacy["direction"]  # written before shorting existed
    assert deserialize_cycle(legacy).direction is PositionDirection.LONG


def test_the_cycle_fsm_derives_sides_from_direction_and_refuses_mismatched_fills():
    long_cycle = PositionCycle(SCOPE, "AAPL", "c1")
    assert (long_cycle.entry_side, long_cycle.exit_side) == (OrderSide.BUY, OrderSide.SELL)
    cycle = PositionCycle(SCOPE, "AAPL", "c2")
    cycle, _ = apply_cycle_event(cycle, CycleEvent(
        CycleEvent.BUY_SIGNAL_ACCEPTED, NOW, strategy_id="s", entry_intent_id="i",
        target_entry_qty=D("10"), side=OrderSide.SELL_SHORT))
    assert cycle.direction is PositionDirection.SHORT
    cycle, _ = apply_cycle_event(cycle, CycleEvent(CycleEvent.BUY_SUBMITTED, NOW, order_id="o1"))
    with pytest.raises(CycleTransitionError, match="cannot open a SHORT cycle"):
        apply_cycle_event(cycle, CycleEvent(
            CycleEvent.FILL_APPLIED, NOW, fill_id="f0", side=OrderSide.BUY,
            quantity=D("10"), price=D("100"), order_id="o1"))
    cycle, _ = apply_cycle_event(cycle, CycleEvent(
        CycleEvent.FILL_APPLIED, NOW, fill_id="f1", side=OrderSide.SELL_SHORT,
        quantity=D("10"), price=D("100"), order_id="o1"))
    assert cycle.remaining_qty == D("10") and cycle.average_entry_price == D("100")
    with pytest.raises(CycleTransitionError, match="cannot close a SHORT cycle"):
        apply_cycle_event(cycle, CycleEvent(
            CycleEvent.FILL_APPLIED, NOW, fill_id="f2", side=OrderSide.SELL,
            quantity=D("10"), price=D("100"), order_id="x"))
    with pytest.raises(CycleTransitionError, match="a cover can never open a long"):
        apply_cycle_event(cycle, CycleEvent(
            CycleEvent.FILL_APPLIED, NOW, fill_id="f3", side=OrderSide.BUY_TO_COVER,
            quantity=D("11"), price=D("100"), order_id="x"))
