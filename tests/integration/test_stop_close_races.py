"""Integration checks for stop/close interleavings (in-memory broker only)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from mike_trade.execution.close_coordinator import (
    CloseCoordinator,
    SellIntent,
    detect_orphan_orders,
)

D = Decimal
NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
ZERO = D("0")


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value


class Events:
    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []

    def emit(self, event: dict[str, object]) -> None:
        self.items.append(dict(event))

    def types(self) -> list[object]:
        return [item["event_type"] for item in self.items]


class FakeBroker:
    """Scriptable broker snapshot; every fill enforces its own no-short guard."""

    def __init__(self, quantity: str = "10") -> None:
        self.positions = {"position-1": D(quantity)}
        self.orders: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, object]] = []
        self.cancel_scripts: dict[str, Callable[[], Any]] = {}
        self.next_exit_status = "ACCEPTED"
        self.next_exit_fill = D("0")
        self.fail_protection = False
        self.replace_started: asyncio.Event | None = None
        self.replace_release: asyncio.Event | None = None
        self.sequence = 0

    def add_stop(
        self,
        order_id: str = "stop-1",
        *,
        quantity: str = "10",
        stop_price: str = "95",
        client_order_id: str = "stop-local-1",
    ) -> None:
        self.orders[order_id] = self._order(
            order_id,
            quantity=D(quantity),
            order_type="STOP",
            stop_price=D(stop_price),
            client_order_id=client_order_id,
        )

    def add_sell(
        self,
        order_id: str,
        *,
        quantity: str,
        client_order_id: str,
        symbol: str = "AAPL",
    ) -> None:
        self.orders[order_id] = self._order(
            order_id,
            quantity=D(quantity),
            order_type="LIMIT",
            client_order_id=client_order_id,
            symbol=symbol,
        )

    def _order(
        self,
        order_id: str,
        *,
        quantity: Decimal,
        order_type: str,
        status: str = "ACCEPTED",
        filled_quantity: Decimal = ZERO,
        stop_price: Decimal | None = None,
        client_order_id: str | None = None,
        symbol: str = "AAPL",
    ) -> dict[str, Any]:
        return {
            "broker_order_id": order_id,
            "client_order_id": client_order_id or f"client-{order_id}",
            "position_id": "position-1",
            "symbol": symbol,
            "side": "SELL",
            "order_type": order_type,
            "status": status,
            "quantity": quantity,
            "filled_quantity": filled_quantity,
            "stop_price": stop_price,
        }

    async def confirmed_position_quantity(self, position_id: str, symbol: str) -> Decimal:
        assert symbol == "AAPL"
        return self.positions.get(position_id, D("0"))

    async def list_open_orders(self) -> list[dict[str, Any]]:
        return list(self.orders.values())

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        self.calls.append(("cancel", order_id))
        scripted = self.cancel_scripts.get(order_id)
        if scripted is not None:
            result = scripted()
            if asyncio.iscoroutine(result):
                return await result
            return result
        order = self.orders[order_id]
        order["status"] = "CANCELED"
        return dict(order)

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        order = self.orders.get(order_id)
        return None if order is None else dict(order)

    async def submit_exit(self, intent: SellIntent, quantity: Decimal) -> dict[str, Any]:
        self.calls.append(("exit", quantity))
        self.sequence += 1
        order_id = f"exit-{self.sequence}"
        fill = self.next_exit_fill
        if self.next_exit_status == "FILLED":
            fill = quantity
        if fill:
            assert fill <= self.positions[intent.position_id], "fake broker would go short"
            self.positions[intent.position_id] -= fill
        order = self._order(
            order_id,
            quantity=quantity,
            order_type=str(intent.order_type),
            status=self.next_exit_status,
            filled_quantity=fill,
            client_order_id=intent.client_order_id,
        )
        self.orders[order_id] = order
        return dict(order)

    async def submit_protective_stop(
        self,
        position_id: str,
        symbol: str,
        quantity: Decimal,
        stop_price: Decimal,
        client_order_id: str,
    ) -> dict[str, Any]:
        self.calls.append(("protect", quantity))
        if self.fail_protection:
            raise RuntimeError("scripted protection failure")
        assert quantity <= self.positions[position_id]
        self.sequence += 1
        order_id = f"stop-reprotected-{self.sequence}"
        order = self._order(
            order_id,
            quantity=quantity,
            order_type="STOP",
            stop_price=stop_price,
            client_order_id=client_order_id,
            symbol=symbol,
        )
        self.orders[order_id] = order
        return dict(order)

    async def replace_protective_stop(
        self,
        order_id: str,
        position_id: str,
        symbol: str,
        quantity: Decimal,
        stop_price: Decimal,
        client_order_id: str,
    ) -> dict[str, Any]:
        self.calls.append(("replace-start", order_id))
        if self.replace_started is not None:
            self.replace_started.set()
        if self.replace_release is not None:
            await self.replace_release.wait()
        self.orders[order_id]["status"] = "REPLACED"
        self.sequence += 1
        new_id = f"stop-replaced-{self.sequence}"
        order = self._order(
            new_id,
            quantity=quantity,
            order_type="STOP",
            stop_price=stop_price,
            client_order_id=client_order_id,
            symbol=symbol,
        )
        self.orders[new_id] = order
        self.calls.append(("replace-done", new_id))
        return dict(order)

    def fill_stop_during_cancel(self, quantity: Decimal) -> Callable[[], dict[str, Any]]:
        def apply() -> dict[str, Any]:
            order = self.orders["stop-1"]
            assert quantity <= self.positions["position-1"]
            self.positions["position-1"] -= quantity
            order["filled_quantity"] += quantity
            order["status"] = (
                "FILLED" if order["filled_quantity"] == order["quantity"] else "CANCELED"
            )
            return dict(order)

        return apply


def coordinator(broker: FakeBroker) -> tuple[CloseCoordinator, Events]:
    events = Events()
    value = CloseCoordinator(broker, Clock(), events)
    value.register_protective_stop(
        position_id="position-1", symbol="AAPL", order_id="stop-1", stop_price=D("95")
    )
    return value, events


def intent(
    kind: str = "STRATEGY",
    *,
    quantity: str = "10",
    minimum_hold: timedelta = timedelta(0),
    filled_at: datetime | None = NOW - timedelta(days=1),
) -> SellIntent:
    return SellIntent(
        position_id="position-1",
        symbol="AAPL",
        quantity=D(quantity),
        kind=kind,
        client_order_id=f"exit-{kind.lower()}",
        first_confirmed_entry_fill_at=filled_at,
        minimum_hold=minimum_hold,
    )


async def test_discretionary_exit_waits_for_broker_confirmed_stop_cancel() -> None:
    broker = FakeBroker()
    broker.add_stop()
    cancel_started = asyncio.Event()
    release_cancel = asyncio.Event()

    async def delayed_cancel() -> dict[str, Any]:
        cancel_started.set()
        await release_cancel.wait()
        broker.orders["stop-1"]["status"] = "CANCELED"
        return dict(broker.orders["stop-1"])

    broker.cancel_scripts["stop-1"] = delayed_cancel
    close, _ = coordinator(broker)
    task = asyncio.create_task(close.close(intent()))
    await cancel_started.wait()
    assert not any(call[0] == "exit" for call in broker.calls)
    release_cancel.set()
    outcome = await task
    assert outcome.status == "SUBMITTED"
    assert [call[0] for call in broker.calls] == ["cancel", "exit"]


@pytest.mark.parametrize(
    ("stop_fill", "expected_status", "expected_exit"),
    [("10", "FLAT", None), ("4.25", "SUBMITTED", D("5.75"))],
)
async def test_stop_fill_during_cancel_uses_only_broker_confirmed_residual(
    stop_fill: str, expected_status: str, expected_exit: Decimal | None
) -> None:
    broker = FakeBroker()
    broker.add_stop()
    broker.cancel_scripts["stop-1"] = broker.fill_stop_during_cancel(D(stop_fill))
    close, _ = coordinator(broker)
    outcome = await close.close(intent())
    assert outcome.status == expected_status
    exits = [call[1] for call in broker.calls if call[0] == "exit"]
    assert exits == ([] if expected_exit is None else [expected_exit])
    assert close.last_confirmed_quantity("position-1") == D("10") - D(stop_fill)
    assert broker.positions["position-1"] >= D("0")


async def test_unknown_cancel_freezes_conflicts_and_blocks_entries() -> None:
    broker = FakeBroker()
    broker.add_stop()

    def unknown() -> dict[str, Any]:
        broker.orders["stop-1"]["status"] = "UNKNOWN"
        return dict(broker.orders["stop-1"])

    broker.cancel_scripts["stop-1"] = unknown
    close, events = coordinator(broker)
    first = await close.close(intent())
    second = await close.close(intent("MANUAL_CLOSE"))
    assert first.status == second.status == "FROZEN"
    assert first.reconciliation_required and first.entries_blocked
    assert not any(call[0] == "exit" for call in broker.calls)
    assert {"RECONCILIATION_REQUIRED", "ENTRY_BLOCKED"} <= set(events.types())


async def test_manual_close_serializes_behind_pending_trailing_replacement() -> None:
    broker = FakeBroker()
    broker.add_stop()
    broker.replace_started = asyncio.Event()
    broker.replace_release = asyncio.Event()
    close, _ = coordinator(broker)
    replacement = asyncio.create_task(
        close.replace_protective_stop(
            position_id="position-1",
            symbol="AAPL",
            order_id="stop-1",
            stop_price=D("97"),
            client_order_id="replace-1",
        )
    )
    await broker.replace_started.wait()
    manual = asyncio.create_task(close.close(intent("MANUAL_CLOSE")))
    await asyncio.sleep(0)
    assert not any(call[0] == "cancel" for call in broker.calls)
    broker.replace_release.set()
    assert (await replacement).status == "REPLACED"
    assert (await manual).status == "SUBMITTED"
    cancels = [call[1] for call in broker.calls if call[0] == "cancel"]
    assert cancels == ["stop-replaced-1"]


async def test_partial_exit_is_canceled_then_reprotected_at_last_stop() -> None:
    broker = FakeBroker()
    broker.add_stop(stop_price="95")
    broker.next_exit_status = "PARTIALLY_FILLED"
    broker.next_exit_fill = D("3.5")
    close, events = coordinator(broker)
    outcome = await close.close(intent("MANUAL_CLOSE"))
    assert outcome.status == "REPROTECTED"
    assert outcome.confirmed_remaining_quantity == D("6.5")
    assert outcome.stop_price is not None and outcome.stop_price >= D("95")
    assert ("protect", D("6.5")) in broker.calls
    assert "PROTECTIVE_STOP_REESTABLISHED" in events.types()
    working_quantity = sum(
        order["quantity"] - order["filled_quantity"]
        for order in broker.orders.values()
        if order["status"] == "ACCEPTED"
    )
    assert working_quantity == broker.positions["position-1"]


async def test_reprotection_failure_is_explicit_and_blocks_entries() -> None:
    broker = FakeBroker()
    broker.add_stop()
    broker.next_exit_status = "REJECTED"
    broker.fail_protection = True
    close, events = coordinator(broker)
    outcome = await close.close(intent("SAFETY_EXIT"))
    assert outcome.status == "UNPROTECTED"
    assert outcome.entries_blocked and outcome.reconciliation_required
    assert {"UNPROTECTED", "ENTRY_BLOCKED"} <= set(events.types())


async def test_quantity_above_confirmed_minus_working_sell_is_rejected() -> None:
    broker = FakeBroker()
    broker.add_stop()
    broker.add_sell("working-1", quantity="2", client_order_id="known-working")
    close, _ = coordinator(broker)
    outcome = await close.close(intent(quantity="9"))
    assert outcome.status == "REJECTED"
    assert not any(call[0] in {"cancel", "exit"} for call in broker.calls)
    assert broker.positions["position-1"] == D("10")


def test_orphan_stop_after_flat_and_sell_without_local_intent_are_reported() -> None:
    broker = FakeBroker(quantity="0")
    broker.add_stop(client_order_id="known-stop")
    broker.add_sell("sell-orphan", quantity="2", client_order_id="external-sell")
    orphans = detect_orphan_orders(
        broker.orders.values(),
        [{"broker_order_id": "stop-1", "client_order_id": "known-stop"}],
        [{"symbol": "AAPL", "quantity": D("0")}],
    )
    by_id = {item.broker_order_id: item.reason for item in orphans}
    assert by_id == {
        "stop-1": "ORPHAN_STOP_FLAT_POSITION",
        "sell-orphan": "ORPHAN_SELL_NO_LOCAL_INTENT",
    }
    assert broker.calls == []  # detection neither cancels nor adopts


async def test_shutdown_does_not_cancel_valid_broker_stop() -> None:
    broker = FakeBroker()
    broker.add_stop()
    close, events = coordinator(broker)
    await close.shutdown()
    assert broker.orders["stop-1"]["status"] == "ACCEPTED"
    assert broker.calls == []
    assert events.types() == ["CLOSE_COORDINATOR_SHUTDOWN"]


@pytest.mark.parametrize("kind", ["MANUAL_CLOSE", "SAFETY_EXIT"])
async def test_manual_and_safety_bypass_hold_while_strategy_respects_it(kind: str) -> None:
    hold = timedelta(hours=1)
    broker = FakeBroker()
    broker.add_stop()
    close, _ = coordinator(broker)
    blocked = await close.close(
        intent("STRATEGY", minimum_hold=hold, filled_at=NOW - timedelta(minutes=5))
    )
    assert blocked.status == "HOLD_BLOCKED"
    assert broker.calls == []
    allowed = await close.close(
        intent(kind, minimum_hold=hold, filled_at=NOW - timedelta(minutes=5))
    )
    assert allowed.status == "SUBMITTED"
    assert [call[0] for call in broker.calls] == ["cancel", "exit"]
