from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mike_trade.execution.coordinator import (
    DecisionReason,
    ExecutionKey,
    OrderCommand,
    OrderCommandCoordinator,
)

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
D = Decimal
ZERO = D("0")


@dataclass(frozen=True)
class Intent:
    execution_mode: str = "PAPER"
    account_key: str = "account-1"
    symbol: str = "AAPL"
    purpose: str = "ENTRY_LONG"
    signal_expires_at: datetime = NOW + timedelta(minutes=1)
    quote_reference_time: datetime = NOW
    qty: Decimal | None = None
    notional: Decimal | None = D("60")
    limit_price: Decimal | None = None


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


class QuoteAges:
    async def quote_age(self, intent: Intent, now: datetime) -> timedelta:
        return now - intent.quote_reference_time


class NoOpenOrders:
    async def has_open_or_unresolved_order(
        self, key: ExecutionKey, purpose: str
    ) -> bool:
        return False


class FirstOpenCheckGate(NoOpenOrders):
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def has_open_or_unresolved_order(
        self, key: ExecutionKey, purpose: str
    ) -> bool:
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            await self.release.wait()
        return False


class TwoKeyBarrier(NoOpenOrders):
    def __init__(self) -> None:
        self.arrived = 0
        self.both_arrived = asyncio.Event()
        self.release = asyncio.Event()

    async def has_open_or_unresolved_order(
        self, key: ExecutionKey, purpose: str
    ) -> bool:
        self.arrived += 1
        if self.arrived == 2:
            self.both_arrived.set()
        await self.release.wait()
        return False


class Positions:
    def __init__(
        self,
        *,
        held: Decimal = ZERO,
        committed: Decimal = ZERO,
        protective: Decimal = ZERO,
    ) -> None:
        self.held = held
        self.committed = committed
        self.protective = protective

    async def confirmed_long_quantity(self, key: ExecutionKey) -> Decimal:
        return self.held

    async def committed_sell_quantity(self, key: ExecutionKey) -> Decimal:
        return self.committed

    async def protective_sell_quantity(self, key: ExecutionKey) -> Decimal:
        return self.protective


class Cash:
    def __init__(self, available: Decimal) -> None:
        self.amount = available

    async def available_cash(self, key: ExecutionKey) -> Decimal:
        return self.amount


def coordinator(
    *,
    clock: Clock | None = None,
    open_orders: NoOpenOrders | None = None,
    positions: Positions | None = None,
    cash: Cash | None = None,
    max_quote_age: timedelta = timedelta(seconds=5),
) -> OrderCommandCoordinator:
    return OrderCommandCoordinator(
        clock=clock or Clock(),
        quote_age_provider=QuoteAges(),
        open_order_provider=open_orders or NoOpenOrders(),
        position_provider=positions or Positions(),
        buying_power_budget_provider=cash or Cash(D("1000")),
        max_quote_age=max_quote_age,
    )


@pytest.mark.asyncio
async def test_same_account_symbol_concurrent_entries_authorize_exactly_one() -> None:
    gate = FirstOpenCheckGate()
    subject = coordinator(open_orders=gate)
    command = OrderCommand(Intent(), reservation_amount=D("10"))

    first = asyncio.create_task(subject.authorize(command))
    await gate.entered.wait()
    second = asyncio.create_task(subject.authorize(command))
    await asyncio.sleep(0)
    gate.release.set()
    decisions = await asyncio.gather(first, second)

    assert [decision.reason for decision in decisions].count(DecisionReason.AUTHORIZED) == 1
    assert [decision.reason for decision in decisions].count(
        DecisionReason.REJECTED_OPEN_ORDER
    ) == 1


@pytest.mark.asyncio
async def test_different_symbols_share_one_account_cash_reservation_section() -> None:
    subject = coordinator(cash=Cash(D("100")))
    commands = (
        OrderCommand(Intent(symbol="AAPL", notional=D("75"))),
        OrderCommand(Intent(symbol="MSFT", notional=D("75"))),
    )

    decisions = await asyncio.gather(*(subject.authorize(command) for command in commands))

    assert [decision.reason for decision in decisions].count(DecisionReason.AUTHORIZED) == 1
    assert [decision.reason for decision in decisions].count(
        DecisionReason.REJECTED_INSUFFICIENT_CASH
    ) == 1
    authorized = next(decision for decision in decisions if decision.authorized)
    assert authorized.reservation is not None
    assert authorized.reservation.amount == D("75")


@pytest.mark.asyncio
async def test_same_symbol_on_different_accounts_is_fully_independent() -> None:
    barrier = TwoKeyBarrier()
    subject = coordinator(open_orders=barrier, cash=Cash(D("60")))
    commands = (
        OrderCommand(Intent(account_key="account-a", notional=D("60"))),
        OrderCommand(Intent(account_key="account-b", notional=D("60"))),
    )

    tasks = [asyncio.create_task(subject.authorize(command)) for command in commands]
    await asyncio.wait_for(barrier.both_arrived.wait(), timeout=0.1)
    barrier.release.set()
    decisions = await asyncio.gather(*tasks)

    assert all(decision.reason is DecisionReason.AUTHORIZED for decision in decisions)
    assert decisions[0].key.account_scope != decisions[1].key.account_scope


@pytest.mark.asyncio
async def test_signal_expiry_is_rechecked_after_waiting_for_symbol_lock() -> None:
    clock = Clock()
    gate = FirstOpenCheckGate()
    subject = coordinator(clock=clock, open_orders=gate)
    first_command = OrderCommand(Intent(notional=D("10")))
    expiring_command = OrderCommand(
        Intent(signal_expires_at=NOW + timedelta(seconds=1), notional=D("10"))
    )

    first = asyncio.create_task(subject.authorize(first_command))
    await gate.entered.wait()
    waiting = asyncio.create_task(subject.authorize(expiring_command))
    await asyncio.sleep(0)
    clock.current = NOW + timedelta(seconds=2)
    gate.release.set()
    first_decision, waiting_decision = await asyncio.gather(first, waiting)

    assert first_decision.reason is DecisionReason.AUTHORIZED
    assert waiting_decision.reason is DecisionReason.REJECTED_STALE_SIGNAL


@pytest.mark.asyncio
async def test_quote_freshness_is_rechecked_after_waiting_for_symbol_lock() -> None:
    clock = Clock()
    gate = FirstOpenCheckGate()
    subject = coordinator(
        clock=clock,
        open_orders=gate,
        max_quote_age=timedelta(seconds=1),
    )
    command = OrderCommand(Intent(notional=D("10")))

    first = asyncio.create_task(subject.authorize(command))
    await gate.entered.wait()
    waiting = asyncio.create_task(subject.authorize(command))
    await asyncio.sleep(0)
    clock.current = NOW + timedelta(seconds=2)
    gate.release.set()
    first_decision, waiting_decision = await asyncio.gather(first, waiting)

    assert first_decision.reason is DecisionReason.AUTHORIZED
    assert waiting_decision.reason is DecisionReason.REJECTED_STALE_QUOTE


@pytest.mark.asyncio
async def test_exit_cannot_exceed_confirmed_quantity_less_working_sells() -> None:
    subject = coordinator(positions=Positions(held=D("10"), committed=D("3")))
    command = OrderCommand(
        Intent(purpose="EXIT_LONG", qty=D("8"), notional=None),
    )

    decision = await subject.authorize(command)

    assert decision.reason is DecisionReason.REJECTED_OVERSELL
    assert decision.available_quantity == D("7")


@pytest.mark.asyncio
async def test_exit_conflicting_with_protective_stop_requires_cancel_confirmation() -> None:
    subject = coordinator(
        positions=Positions(held=D("10"), committed=D("10"), protective=D("10"))
    )
    command = OrderCommand(
        Intent(purpose="MANUAL_CLOSE", qty=D("10"), notional=None),
        cancel_protective_first=True,
    )

    decision = await subject.authorize(command)

    assert decision.reason is DecisionReason.REQUIRES_PROTECTIVE_CANCEL
    assert decision.requires_cancel_confirmation
    assert not decision.authorized
    assert decision.protective_quantity == D("10")


@pytest.mark.asyncio
async def test_decimal_reservation_consume_and_release_restore_cash_exactly() -> None:
    subject = coordinator(cash=Cash(D("100.10")))
    command = OrderCommand(Intent(notional=D("33.33")))
    key = command.key
    before = await subject.reservation_ledger.available(key)
    decision = await subject.authorize(command)
    assert decision.reservation is not None

    after_reserve = await subject.reservation_ledger.available(key)
    remaining = await subject.consume_reservation(decision.reservation, D("3.33"))
    released = await subject.release_reservation(decision.reservation)
    after_release = await subject.reservation_ledger.available(key)

    assert before == D("100.10")
    assert after_reserve == D("66.77")
    assert remaining == D("30.00")
    assert released == D("30.00")
    assert after_release == before
    assert all(isinstance(value, Decimal) for value in (before, remaining, released))
    with pytest.raises(TypeError, match="must be Decimal"):
        await subject.reservation_ledger.reserve(key, 1.0)  # type: ignore[arg-type]
