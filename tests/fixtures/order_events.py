"""Deterministic fault fixtures for the f-phase order-management stack.

Builders use :class:`~decimal.Decimal` quantities/prices, UTC-aware timestamps,
and stable ids. :class:`FixedClock` is the only clock and there is no
randomness; the fixed seed below exists only to document that choice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Sequence

from mike_trade.brokers.ports import BrokerPort
from mike_trade.domain.accounts import (
    AccountEnvironment,
    AccountSnapshot,
    BrokerCapabilities,
)
from mike_trade.domain.errors import BrokerRejectedError
from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    OrderSide,
    OrderType,
)
from mike_trade.domain.trading import OrderIntent as BrokerOrderIntent
from mike_trade.execution.fill_processor import FillEvent, OrderUpdateEvent
from mike_trade.execution.order_fsm import OrderStatus

__all__ = [
    "ScriptedFakeBroker",
    "FixedClock",
    "make_order_update",
    "make_fill_event",
    "FaultScript",
]

_SEED = 20261008
_FIXED_SEED = _SEED

_BASE_QTY = Decimal("10")
_BASE_PRICE = Decimal("100")


def _require_stable_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _require_utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a UTC-aware datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC")
    return value.astimezone(UTC)


def _require_decimal(value: object, name: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite() or value <= Decimal("0"):
        raise ValueError(f"{name} must be a finite positive Decimal")
    return value


class FixedClock:
    """Manually advanced UTC clock; the only time source in these fixtures."""

    def __init__(self, start: datetime) -> None:
        self._current = _require_utc(start, "start")

    def now(self) -> datetime:
        return self._current

    def set(self, value: datetime) -> datetime:
        self._current = _require_utc(value, "value")
        return self._current

    def advance(self, delta: timedelta) -> datetime:
        if not isinstance(delta, timedelta):
            raise TypeError("delta must be timedelta")
        self._current = self._current + delta
        return self._current


def make_fill_event(
    *,
    client_order_id: str,
    event_id: str,
    occurred_at: datetime,
    quantity: Decimal | None = None,
    cumulative_qty: Decimal | None = None,
    price: Decimal | None = None,
    average_price: Decimal | None = None,
    side: OrderSide | str | None = None,
    broker_order_id: str | None = None,
    symbol: str | None = None,
    fee: Decimal = Decimal("0"),
) -> FillEvent:
    """Build a deterministic normalized fill with stable ids and Decimal amounts."""
    cid = _require_stable_id(client_order_id, "client_order_id")
    eid = _require_stable_id(event_id, "event_id")
    at = _require_utc(occurred_at, "occurred_at")
    if quantity is not None:
        _require_decimal(quantity, "quantity")
    if cumulative_qty is not None:
        _require_decimal(cumulative_qty, "cumulative_qty")
    if price is not None:
        _require_decimal(price, "price")
    if average_price is not None:
        _require_decimal(average_price, "average_price")
    if not isinstance(fee, Decimal) or not fee.is_finite() or fee < Decimal("0"):
        raise ValueError("fee must be a finite non-negative Decimal")
    if quantity is None and cumulative_qty is None:
        raise ValueError("quantity or cumulative_qty is required")
    return FillEvent(
        client_order_id=cid,
        event_id=eid,
        occurred_at=at,
        quantity=quantity,
        cumulative_qty=cumulative_qty,
        price=price,
        average_price=average_price,
        side=side,
        broker_order_id=broker_order_id,
        symbol=symbol,
        fee=fee,
    )


def make_order_update(
    *,
    client_order_id: str,
    status: str | OrderStatus,
    occurred_at: datetime,
    event_id: str | None = None,
    broker_order_id: str | None = None,
    cumulative_qty: Decimal | None = None,
    average_price: Decimal | None = None,
    broker_confirmed: bool = True,
    broker_lookup: bool = False,
) -> OrderUpdateEvent:
    """Build a deterministic normalized order update with a stable identity."""
    cid = _require_stable_id(client_order_id, "client_order_id")
    at = _require_utc(occurred_at, "occurred_at")
    if event_id is not None:
        _require_stable_id(event_id, "event_id")
    if cumulative_qty is not None:
        _require_decimal(cumulative_qty, "cumulative_qty")
    if average_price is not None:
        _require_decimal(average_price, "average_price")
    if not isinstance(broker_confirmed, bool) or not isinstance(broker_lookup, bool):
        raise TypeError("broker_confirmed and broker_lookup must be bool")
    return OrderUpdateEvent(
        client_order_id=cid,
        status=status,
        occurred_at=at,
        event_id=event_id,
        broker_order_id=broker_order_id,
        cumulative_qty=cumulative_qty,
        average_price=average_price,
        broker_confirmed=broker_confirmed,
        broker_lookup=broker_lookup,
    )


@dataclass(frozen=True, slots=True)
class FaultScript:
    """Declarative broker fault program consumed by :class:`ScriptedFakeBroker`.

    ``submit`` is one of ``accept``, ``reject``, ``timeout_accepted``,
    ``timeout_absent`` or ``crash``. ``lookup`` is one of ``found``,
    ``absent`` or ``error``.
    """

    submit: str = "accept"
    lookup: str = "found"
    submit_status: BrokerOrderStatus = BrokerOrderStatus.ACCEPTED
    lookup_status: BrokerOrderStatus | None = None
    quantity: Decimal = _BASE_QTY
    filled_qty: Decimal = Decimal("0")
    price: Decimal = _BASE_PRICE

    def __post_init__(self) -> None:
        if self.submit not in {
            "accept",
            "reject",
            "timeout_accepted",
            "timeout_absent",
            "crash",
        }:
            raise ValueError(f"unknown submit fault: {self.submit!r}")
        if self.lookup not in {"found", "absent", "error"}:
            raise ValueError(f"unknown lookup fault: {self.lookup!r}")
        for name in ("quantity", "price"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= Decimal("0"):
                raise ValueError(f"{name} must be a finite positive Decimal")
        if (
            not isinstance(self.filled_qty, Decimal)
            or not self.filled_qty.is_finite()
            or self.filled_qty < Decimal("0")
        ):
            raise ValueError("filled_qty must be a finite non-negative Decimal")
        if self.filled_qty > self.quantity:
            raise ValueError("filled_qty must not exceed quantity")


class ScriptedFakeBroker:
    """Scriptable in-memory :class:`BrokerPort`; never touches a network.

    Program submit/lookup faults with :class:`FaultScript` via :meth:`program`.
    Duplicate, out-of-order, partial-then-cancel, and stop-vs-exit streams are
    built deterministically with :meth:`duplicate_stream`, :meth:`out_of_order`,
    :meth:`partial_then_cancel_stream`, and :meth:`stop_exit_race_stream`.
    A crash between intent persistence and the broker response is simulated by
    planting the broker-side order with :meth:`plant_order` and never calling
    :meth:`submit_order`, mirroring a process that died mid-submit.
    """

    def __init__(
        self,
        clock: FixedClock,
        environment: AccountEnvironment = AccountEnvironment.PAPER,
        script: FaultScript | None = None,
    ) -> None:
        if not isinstance(clock, FixedClock):
            raise TypeError("clock must be FixedClock")
        if not isinstance(environment, AccountEnvironment):
            raise TypeError("environment must be an AccountEnvironment")
        self._clock = clock
        self.environment = environment
        self._script = script or FaultScript()
        self._orders: dict[str, BrokerOrder] = {}
        self._sequence = 0
        self.submit_calls: list[BrokerOrderIntent] = []
        self.lookup_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.replace_calls: list[tuple[str, dict[str, Decimal | None]]] = []

    def program(self, script: FaultScript) -> None:
        if not isinstance(script, FaultScript):
            raise TypeError("script must be FaultScript")
        self._script = script

    @property
    def script(self) -> FaultScript:
        return self._script

    def submitted_client_ids(self) -> list[str]:
        return [call.client_order_id for call in self.submit_calls]

    def _next_broker_id(self, client_order_id: str) -> str:
        self._sequence += 1
        return f"broker-{client_order_id}-{self._sequence}"

    def _snapshot(
        self,
        intent: BrokerOrderIntent,
        status: BrokerOrderStatus,
        filled_qty: Decimal,
    ) -> BrokerOrder:
        now = self._clock.now()
        return BrokerOrder(
            broker_order_id=self._next_broker_id(intent.client_order_id),
            client_order_id=intent.client_order_id,
            account_scope_id=intent.account_scope_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            status=status,
            quantity=intent.quantity
            if intent.quantity is not None
            else self._script.quantity,
            filled_quantity=filled_qty,
            submitted_at=now,
            updated_at=now,
            filled_avg_price=self._script.price if filled_qty > 0 else None,
            limit_price=intent.limit_price,
            stop_price=intent.stop_price,
            trail_price=intent.trail_price,
            trail_percent=intent.trail_percent,
        )

    def plant_order(
        self,
        intent: BrokerOrderIntent,
        status: BrokerOrderStatus | None = None,
        *,
        filled_qty: Decimal | None = None,
    ) -> BrokerOrder:
        """Store a broker-side order without recording a submit call (crash setup)."""
        order = self._snapshot(
            intent,
            status or self._script.submit_status,
            filled_qty if filled_qty is not None else self._script.filled_qty,
        )
        self._orders[intent.client_order_id] = order
        return order

    async def submit_order(self, intent: BrokerOrderIntent) -> BrokerOrder:
        self.submit_calls.append(intent)
        mode = self._script.submit
        if mode == "reject":
            raise BrokerRejectedError(
                "scripted rejection", code="broker_rejected", context={}
            )
        if mode == "timeout_absent":
            raise TimeoutError("scripted submit timeout; order absent")
        order = self._snapshot(intent, self._script.submit_status, self._script.filled_qty)
        if mode == "accept":
            self._orders[intent.client_order_id] = order
            return order
        if mode == "timeout_accepted":
            self._orders[intent.client_order_id] = order
            raise TimeoutError("scripted submit timeout after accept")
        # mode == "crash": accepted remotely, response never observed.
        self._orders[intent.client_order_id] = order
        raise ConnectionError("simulated crash between persistence and response")

    def record_fill(
        self,
        client_order_id: str,
        filled_qty: Decimal,
        status: BrokerOrderStatus,
    ) -> BrokerOrder:
        """Rewrite the stored snapshot as fill-bearing (late fill confirmations)."""
        _require_stable_id(client_order_id, "client_order_id")
        _require_decimal(filled_qty, "filled_qty")
        if not isinstance(status, BrokerOrderStatus):
            raise TypeError("status must be BrokerOrderStatus")
        order = self._orders.get(client_order_id)
        if order is None:
            raise KeyError(f"unknown client_order_id: {client_order_id!r}")
        if filled_qty > order.quantity:
            raise ValueError("filled_qty must not exceed order quantity")
        updated = BrokerOrder(
            broker_order_id=order.broker_order_id,
            client_order_id=order.client_order_id,
            account_scope_id=order.account_scope_id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            status=status,
            quantity=order.quantity,
            filled_quantity=filled_qty,
            submitted_at=order.submitted_at,
            updated_at=self._clock.now(),
            filled_avg_price=self._script.price if filled_qty > 0 else None,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            trail_price=order.trail_price,
            trail_percent=order.trail_percent,
        )
        self._orders[client_order_id] = updated
        return updated

    async def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        self.lookup_calls.append(client_order_id)
        if self._script.lookup == "error":
            raise ConnectionError("scripted lookup failure")
        if self._script.lookup == "absent":
            return None
        order = self._orders.get(client_order_id)
        if order is None or self._script.lookup_status is None:
            return order
        if order.status is self._script.lookup_status:
            return order
        updated = BrokerOrder(
            broker_order_id=order.broker_order_id,
            client_order_id=order.client_order_id,
            account_scope_id=order.account_scope_id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            status=self._script.lookup_status,
            quantity=order.quantity,
            filled_quantity=order.filled_quantity,
            submitted_at=order.submitted_at,
            updated_at=self._clock.now(),
            filled_avg_price=order.filled_avg_price,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            trail_price=order.trail_price,
            trail_percent=order.trail_percent,
        )
        self._orders[client_order_id] = updated
        return updated

    async def get_order(self, broker_order_id: str) -> BrokerOrder | None:
        for order in self._orders.values():
            if order.broker_order_id == broker_order_id:
                return order
        return None

    async def list_open_orders(self) -> Sequence[BrokerOrder]:
        terminal = {
            BrokerOrderStatus.FILLED,
            BrokerOrderStatus.CANCELED,
            BrokerOrderStatus.EXPIRED,
            BrokerOrderStatus.REJECTED,
            BrokerOrderStatus.REPLACED,
        }
        return [o for o in self._orders.values() if o.status not in terminal]

    async def list_positions(self) -> Sequence[object]:
        return []

    async def get_account(self) -> AccountSnapshot:
        raise NotImplementedError("ScriptedFakeBroker.get_account is out of scope")

    async def list_fills(self, since: datetime) -> Sequence[object]:
        _require_utc(since, "since")
        return []

    async def cancel_order(self, broker_order_id: str) -> None:
        self.cancel_calls.append(broker_order_id)

    async def replace_order(
        self,
        broker_order_id: str,
        *,
        stop_price: Decimal | None = None,
        trail: Decimal | None = None,
        quantity: Decimal | None = None,
    ) -> BrokerOrder:
        self.replace_calls.append(
            (
                broker_order_id,
                {"stop_price": stop_price, "trail": trail, "quantity": quantity},
            )
        )
        for order in self._orders.values():
            if order.broker_order_id == broker_order_id:
                return order
        raise KeyError(f"unknown broker_order_id: {broker_order_id!r}")

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider="scripted-fake",
            environment=self.environment,
            supported_order_types=frozenset({"MARKET", "LIMIT"}),
            supports_fractional=False,
            supports_notional=True,
            supports_native_trailing_stop=False,
            supports_extended_hours=False,
            supports_short=False,
        )

    def duplicate_stream(
        self, events: Sequence[FillEvent | OrderUpdateEvent]
    ) -> list[FillEvent | OrderUpdateEvent]:
        """Repeat every event twice in order, exercising dedupe handling."""
        return [event for event in events for _ in (0, 1)]

    def out_of_order(
        self, events: Sequence[FillEvent | OrderUpdateEvent]
    ) -> list[FillEvent | OrderUpdateEvent]:
        """Return the deterministic reverse of ``events`` (no randomness)."""
        return list(reversed(events))

    def partial_then_cancel_stream(
        self,
        *,
        client_order_id: str,
        broker_order_id: str,
        occurred_at: datetime,
        partial_qty: Decimal,
        price: Decimal,
    ) -> list[FillEvent | OrderUpdateEvent]:
        """Emit partial fills followed by a broker-confirmed cancel."""
        at = _require_utc(occurred_at, "occurred_at")
        _require_decimal(partial_qty, "partial_qty")
        _require_decimal(price, "price")
        return [
            make_fill_event(
                client_order_id=client_order_id,
                event_id=f"{client_order_id}-partial-1",
                occurred_at=at,
                quantity=partial_qty,
                price=price,
                broker_order_id=broker_order_id,
            ),
            make_order_update(
                client_order_id=client_order_id,
                status=OrderStatus.CANCELED,
                occurred_at=at + timedelta(seconds=1),
                event_id=f"{client_order_id}-cancel-1",
                broker_order_id=broker_order_id,
                broker_confirmed=True,
            ),
        ]

    def stop_exit_race_stream(
        self,
        *,
        stop_client_id: str,
        exit_client_id: str,
        stop_broker_id: str,
        exit_broker_id: str,
        occurred_at: datetime,
        stop_qty: Decimal,
        exit_qty: Decimal,
        price: Decimal,
    ) -> list[FillEvent | OrderUpdateEvent]:
        """Emit a protective-stop fill racing an application exit fill."""
        at = _require_utc(occurred_at, "occurred_at")
        _require_decimal(stop_qty, "stop_qty")
        _require_decimal(exit_qty, "exit_qty")
        _require_decimal(price, "price")
        return [
            make_fill_event(
                client_order_id=stop_client_id,
                event_id=f"{stop_client_id}-race-fill",
                occurred_at=at,
                quantity=stop_qty,
                price=price,
                side=OrderSide.SELL,
                broker_order_id=stop_broker_id,
            ),
            make_fill_event(
                client_order_id=exit_client_id,
                event_id=f"{exit_client_id}-race-fill",
                occurred_at=at,
                quantity=exit_qty,
                price=price,
                side=OrderSide.SELL,
                broker_order_id=exit_broker_id,
            ),
        ]

    # Backwards-compatible alias for the scriptable event-stream helper.
    def stop_vs_exit_stream(
        self, **kwargs: object,
    ) -> list[FillEvent | OrderUpdateEvent]:
        return self.stop_exit_race_stream(**kwargs)  # type: ignore[arg-type]


@dataclass
class _UnusedPlaceholder:
    """Private container keeping field-import use explicit without new public symbols."""

    seen: list[str] = field(default_factory=list)


assert BrokerPort is not None
assert OrderType is not None
