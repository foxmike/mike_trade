"""Alpaca gateway for the PAPER/LIVE lifecycle.

A thin facade over the services an :class:`mike_trade.app.AlpacaBrokerAdapter` already
owns (orders, account, activities, assets).  It adds only what the lifecycle needs and the
generic adapter deliberately lacks: broker-consistent snapshots, execution (activity) reads
without a per-fill order lookup, standalone protective-stop submission and replacement, and an
explicit ``attempt`` number so a retry gets a fresh, deterministic ``client_order_id``.

The calls are synchronous (the Alpaca services underneath are); the lifecycle invokes them
directly so each mutation sits inside its persist-before-send unit of work.

No method here decides anything; every mutation is single-shot and an unknown outcome is
surfaced (``OrderNotSubmitted`` / ``UnknownOrderOutcome``) for the caller to resolve.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, cast

from mike_trade.brokers.alpaca.orders import (
    AlpacaOrder,
    NotSubmitted,
    make_client_order_id,
)
from mike_trade.brokers.alpaca.orders import OrderPurpose as AlpacaPurpose
from mike_trade.domain.accounts import AccountScope
from mike_trade.domain.errors import BrokerError
from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
)

__all__ = [
    "AlpacaLiveGateway",
    "BrokerPosition",
    "BrokerSnapshot",
    "Execution",
    "LiveGateway",
    "OrderNotSubmitted",
    "UnknownOrderOutcome",
    "convert_order",
    "purpose_for_intent",
]

ZERO = Decimal("0")

#: Alpaca order status -> engine status.  ``pending_replace`` is a *working* order.
_STATUS = {
    "new": BrokerOrderStatus.NEW,
    "accepted": BrokerOrderStatus.ACCEPTED,
    "accepted_for_bidding": BrokerOrderStatus.ACCEPTED,
    "pending_new": BrokerOrderStatus.PENDING_NEW,
    "pending_replace": BrokerOrderStatus.ACCEPTED,
    "partially_filled": BrokerOrderStatus.PARTIALLY_FILLED,
    "filled": BrokerOrderStatus.FILLED,
    "pending_cancel": BrokerOrderStatus.PENDING_CANCEL,
    "canceled": BrokerOrderStatus.CANCELED,
    "expired": BrokerOrderStatus.EXPIRED,
    "done_for_day": BrokerOrderStatus.EXPIRED,
    "replaced": BrokerOrderStatus.REPLACED,
    "rejected": BrokerOrderStatus.REJECTED,
}
_TYPES = {
    "market": OrderType.MARKET,
    "limit": OrderType.LIMIT,
    "stop": OrderType.STOP,
    "stop_limit": OrderType.STOP_LIMIT,
    "trailing_stop": OrderType.TRAILING_STOP,
}


class OrderNotSubmitted(BrokerError):
    """A submission is confirmed absent at the broker after an unknown outcome."""

    def __init__(self, client_order_id: str) -> None:
        self.client_order_id = client_order_id
        super().__init__(
            f"order {client_order_id!r} was not submitted", code="order_not_submitted",
            context={"client_order_id": client_order_id},
        )


class UnknownOrderOutcome(BrokerError):
    """A submission may exist at the broker but could not be resolved."""

    def __init__(self, client_order_id: str, detail: str) -> None:
        self.client_order_id = client_order_id
        super().__init__(
            f"order outcome unresolved for {client_order_id!r}: {detail}",
            code="order_outcome_unresolved", context={"client_order_id": client_order_id},
        )


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    symbol: str
    quantity: Decimal  # positive magnitude
    side: str  # "long" | "short"
    avg_entry_price: Decimal
    quantity_available: Decimal


@dataclass(frozen=True, slots=True)
class BrokerSnapshot:
    """Positions and open orders read back-to-back.

    ``consistent`` is False when positions changed while the orders were being read; the
    reconciler then takes no destructive action from this snapshot.
    """

    taken_at: datetime
    positions: Mapping[str, BrokerPosition]
    open_orders: tuple[BrokerOrder, ...]
    consistent: bool = True

    def orders_for(self, symbol: str) -> tuple[BrokerOrder, ...]:
        return tuple(order for order in self.open_orders if order.symbol == symbol)

    def quantity(self, symbol: str) -> Decimal:
        position = self.positions.get(symbol)
        return ZERO if position is None else position.quantity


@dataclass(frozen=True, slots=True)
class Execution:
    """One broker execution (an Alpaca FILL activity), keyed by its activity id."""

    activity_id: str
    order_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    occurred_at: datetime
    cumulative_quantity: Decimal | None = None
    raw_type: str = "fill"


class LiveGateway(Protocol):
    """What the lifecycle needs from a broker (the Alpaca implementation is below)."""

    scope: AccountScope

    def client_order_id(self, cycle_id: str, purpose: OrderPurpose, attempt: int) -> str: ...
    def trailing_client_order_id(self, cycle_id: str, attempt: int) -> str: ...
    def snapshot(self) -> BrokerSnapshot: ...
    def position(self, symbol: str) -> BrokerPosition | None: ...
    def get_order(self, order_id: str) -> BrokerOrder | None: ...
    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...
    def executions(self, since: datetime) -> tuple[Execution, ...]: ...
    def submit_market(self, intent: OrderIntent, attempt: int) -> BrokerOrder: ...
    def submit_stop(
        self, cycle_id: str, symbol: str, quantity: Decimal, stop_price: Decimal, attempt: int
    ) -> BrokerOrder: ...
    def submit_trailing_stop(
        self, cycle_id: str, symbol: str, quantity: Decimal, trail_percent: Decimal, attempt: int
    ) -> BrokerOrder: ...
    def replace_stop(
        self, order_id: str, stop_price: Decimal, quantity: Decimal, client_order_id: str
    ) -> BrokerOrder | None: ...
    def cancel_order(self, order_id: str) -> BrokerOrder | None: ...
    def clock(self) -> tuple[bool, datetime, datetime]: ...
    def account_check(self, notional: Decimal) -> tuple[bool, str]: ...
    def tick_size(self, symbol: str) -> Decimal: ...
    def stop_capabilities(self) -> Mapping[str, bool]: ...


def purpose_for_intent(intent: OrderIntent) -> AlpacaPurpose:
    """The Alpaca client-order-id lineage purpose of a domain intent."""
    if intent.purpose is OrderPurpose.ENTRY:
        return AlpacaPurpose.ENTRY
    return {
        OrderPurpose.STRATEGY_EXIT: AlpacaPurpose.EXIT,
        OrderPurpose.PROTECTIVE_STOP: AlpacaPurpose.PROTECTIVE_STOP,
        OrderPurpose.MANUAL_CLOSE: AlpacaPurpose.MANUAL_CLOSE,
        OrderPurpose.SAFETY_EXIT: AlpacaPurpose.SAFETY_EXIT,
        OrderPurpose.EOD_CLOSE: AlpacaPurpose.EOD_CLOSE,
    }[intent.purpose]


def convert_order(order: AlpacaOrder, scope: AccountScope, *, fallback: datetime) -> BrokerOrder:
    """Normalize an Alpaca order; an unrecognized status is UNKNOWN, never silently working."""
    if order.qty is None:
        raise BrokerError(
            f"Alpaca order {order.order_id} has no quantity", code="order_without_quantity",
            context={"order_id": order.order_id},
        )
    status = _STATUS.get(order.status.lower(), BrokerOrderStatus.UNKNOWN)
    order_type = _TYPES.get(order.type.lower())
    if order_type is None:
        raise BrokerError(
            f"unsupported Alpaca order type {order.type!r}", code="order_type_unsupported",
            context={"order_id": order.order_id},
        )
    submitted = order.submitted_at or order.created_at or fallback
    updated = order.updated_at or submitted
    return BrokerOrder(
        broker_order_id=order.order_id,
        client_order_id=order.client_order_id,
        account_scope_id=scope.scope_id,
        symbol=order.symbol,
        side=OrderSide.BUY if order.side.lower() == "buy" else OrderSide.SELL,
        order_type=order_type,
        status=status,
        quantity=order.qty,
        filled_quantity=order.filled_qty or ZERO,
        submitted_at=submitted,
        updated_at=max(updated, submitted),
        filled_avg_price=order.filled_avg_price if (order.filled_qty or ZERO) > ZERO else None,
        limit_price=order.limit_price if order_type is OrderType.LIMIT else None,
        stop_price=order.stop_price if order_type in (OrderType.STOP, OrderType.STOP_LIMIT) else None,
        trail_price=order.trail_price if order_type is OrderType.TRAILING_STOP else None,
        trail_percent=order.trail_percent if order_type is OrderType.TRAILING_STOP else None,
        replaced_by=getattr(order, "replaced_by", None),
    )


@dataclass(slots=True)
class AlpacaLiveGateway:
    """Concrete gateway over an already-validated ``AlpacaBrokerAdapter``."""

    adapter: Any
    clock_source: Any
    _tick: dict[str, Decimal] = field(default_factory=dict)

    # ------------------------------------------------------------------ plumbing

    @property
    def scope(self) -> AccountScope:
        return cast(AccountScope, self.adapter.scope)

    def _services(self) -> tuple[Any, Any, Any, Any]:
        self.adapter._ready()
        a = self.adapter
        return a._orders, a._account, a._activities, a._assets

    def _now(self) -> datetime:
        return cast(datetime, self.clock_source.now())

    def _checker(self) -> Callable[[str], bool]:
        from mike_trade.brokers.alpaca.capabilities import default_alpaca_capabilities

        matrix = self.adapter.capabilities_matrix or default_alpaca_capabilities()
        return cast(Callable[[str], bool], matrix.as_legacy_checker(self.adapter.mode.value))

    def stop_capabilities(self) -> Mapping[str, bool]:
        """Proven stop/replace capabilities for whole shares, GTC (the engine's only shape)."""
        check = self._checker()
        return {
            "stop_submit": check("standalone_stop_sell:whole:gtc"),
            "stop_replace": check("replace_stop_price"),
            "trailing_submit": check("standalone_trailing_stop_sell:whole:gtc"),
        }

    def client_order_id(self, cycle_id: str, purpose: OrderPurpose, attempt: int) -> str:
        orders, _, _, _ = self._services()
        alpaca_purpose = {
            OrderPurpose.ENTRY: AlpacaPurpose.ENTRY,
            OrderPurpose.STRATEGY_EXIT: AlpacaPurpose.EXIT,
            OrderPurpose.PROTECTIVE_STOP: AlpacaPurpose.PROTECTIVE_STOP,
            OrderPurpose.MANUAL_CLOSE: AlpacaPurpose.MANUAL_CLOSE,
            OrderPurpose.SAFETY_EXIT: AlpacaPurpose.SAFETY_EXIT,
            OrderPurpose.EOD_CLOSE: AlpacaPurpose.EOD_CLOSE,
        }[purpose]
        return make_client_order_id(
            orders.environment, orders.fingerprint, cycle_id, alpaca_purpose, attempt
        )

    def trailing_client_order_id(self, cycle_id: str, attempt: int) -> str:
        orders, _, _, _ = self._services()
        return make_client_order_id(
            orders.environment, orders.fingerprint, cycle_id, AlpacaPurpose.TRAILING_STOP, attempt
        )

    def _convert(self, order: AlpacaOrder) -> BrokerOrder:
        return convert_order(order, self.scope, fallback=self._now())

    # ------------------------------------------------------------------- reads

    def _positions(self) -> dict[str, BrokerPosition]:
        _, account, _, _ = self._services()
        result: dict[str, BrokerPosition] = {}
        for item in account.list_positions():
            if item.qty == ZERO:
                continue
            result[item.symbol] = BrokerPosition(
                symbol=item.symbol,
                quantity=abs(item.qty),
                side=item.side,
                avg_entry_price=item.avg_entry_price,
                quantity_available=abs(item.qty_available),
            )
        return result

    def snapshot(self) -> BrokerSnapshot:
        orders_service, _, _, _ = self._services()
        positions: dict[str, BrokerPosition] = {}
        orders: tuple[BrokerOrder, ...] = ()
        consistent = False
        for _ in range(3):
            before = self._positions()
            orders = tuple(
                self._convert(item) for item in orders_service.list_open_orders(limit=500)
            )
            positions = self._positions()
            if {s: p.quantity for s, p in before.items()} == {s: p.quantity for s, p in positions.items()}:
                consistent = True
                break
        return BrokerSnapshot(self._now(), positions, orders, consistent)

    def position(self, symbol: str) -> BrokerPosition | None:
        _, account, _, _ = self._services()
        item = account.get_position(symbol)
        if item is None or item.qty == ZERO:
            return None
        return BrokerPosition(
            item.symbol, abs(item.qty), item.side, item.avg_entry_price, abs(item.qty_available)
        )

    def get_order(self, order_id: str) -> BrokerOrder | None:
        orders, _, _, _ = self._services()
        found = orders.get_order(order_id)
        return None if found is None else self._convert(found)

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        orders, _, _, _ = self._services()
        found = orders.get_order_by_client_order_id(client_order_id)
        return None if found is None else self._convert(found)

    def executions(self, since: datetime) -> tuple[Execution, ...]:
        _, _, activities, _ = self._services()
        items = activities.list_activities(activity_types="FILL", after=since, direction="asc")
        result = [
            Execution(
                activity_id=item.id,
                order_id=item.order_id,
                symbol=item.symbol,
                side=OrderSide.BUY if item.side.lower() == "buy" else OrderSide.SELL,
                quantity=item.qty,
                price=item.price,
                occurred_at=item.transaction_time,
                cumulative_quantity=item.cum_qty,
                raw_type=item.type,
            )
            for item in items
            if getattr(item, "activity_type", "FILL") == "FILL" or hasattr(item, "order_id")
        ]
        return tuple(sorted(result, key=lambda e: (e.occurred_at, e.activity_id)))

    def clock(self) -> tuple[bool, datetime, datetime]:
        _, account, _, _ = self._services()
        value = account.get_clock()
        return bool(value.is_open), value.next_open, value.next_close

    def account_check(self, notional: Decimal) -> tuple[bool, str]:
        """(ok, reason): the account may open a position of ``notional`` dollars."""
        _, account, _, _ = self._services()
        snapshot = account.get_account()
        if snapshot.trading_blocked:
            return False, "TRADING_BLOCKED"
        if snapshot.account_blocked:
            return False, "ACCOUNT_BLOCKED"
        if snapshot.trade_suspended_by_user:
            return False, "TRADE_SUSPENDED"
        if snapshot.buying_power < notional:
            return False, "INSUFFICIENT_BUYING_POWER"
        return True, ""

    def tick_size(self, symbol: str) -> Decimal:
        cached = self._tick.get(symbol)
        if cached is not None:
            return cached
        _, _, _, assets = self._services()
        asset = assets.get_asset(symbol)
        tick = asset.price_increment or Decimal("0.01")
        self._tick[symbol] = tick
        return tick

    # --------------------------------------------------------------- mutations

    def _resolve(self, result: AlpacaOrder | NotSubmitted, client_order_id: str) -> BrokerOrder:
        if isinstance(result, NotSubmitted):
            raise OrderNotSubmitted(result.client_order_id or client_order_id)
        return self._convert(result)

    def submit_market(self, intent: OrderIntent, attempt: int) -> BrokerOrder:
        from mike_trade.brokers.alpaca.orders import OrderOutcomeUnresolved

        orders, account, _, assets = self._services()
        asset = assets.get_asset(intent.symbol)
        verdict = assets.check_eligibility(asset)
        if not verdict.eligible:
            raise BrokerError(
                f"asset {intent.symbol} is ineligible: {verdict.reasons!r}",
                code="asset_ineligible", context={"symbol": intent.symbol},
            )
        observed = account.get_position(intent.symbol)
        client_order_id = self.client_order_id(intent.cycle_id, intent.purpose, attempt)
        try:
            result = orders.submit_order(
                asset=asset,
                side=intent.side.broker_side,
                order_type=intent.order_type.value,
                qty=intent.quantity,
                notional=intent.notional,
                limit_price=intent.limit_price,
                time_in_force=intent.time_in_force.value,
                cycle_id=intent.cycle_id,
                purpose=purpose_for_intent(intent),
                attempt=attempt,
                known_position_qty=None if observed is None else abs(observed.qty),
                known_position_side=None if observed is None else str(observed.side).lower(),
                capability_check=self._checker(),
            )
        except OrderOutcomeUnresolved as exc:
            raise UnknownOrderOutcome(exc.client_order_id, repr(exc.lookup_error)) from exc
        return self._resolve(result, client_order_id)

    def submit_stop(
        self, cycle_id: str, symbol: str, quantity: Decimal, stop_price: Decimal, attempt: int
    ) -> BrokerOrder:
        from mike_trade.brokers.alpaca.orders import OrderOutcomeUnresolved

        orders, account, _, assets = self._services()
        asset = assets.get_asset(symbol)
        observed = account.get_position(symbol)
        client_order_id = self.client_order_id(cycle_id, OrderPurpose.PROTECTIVE_STOP, attempt)
        try:
            result = orders.submit_stop_sell(
                asset=asset, qty=quantity, stop_price=stop_price,
                known_position_qty=ZERO if observed is None else abs(observed.qty),
                capability_check=self._checker(), time_in_force="gtc",
                cycle_id=cycle_id, attempt=attempt,
            )
        except OrderOutcomeUnresolved as exc:
            raise UnknownOrderOutcome(exc.client_order_id, repr(exc.lookup_error)) from exc
        return self._resolve(result, client_order_id)

    def submit_trailing_stop(
        self, cycle_id: str, symbol: str, quantity: Decimal, trail_percent: Decimal, attempt: int
    ) -> BrokerOrder:
        from mike_trade.brokers.alpaca.orders import OrderOutcomeUnresolved

        orders, account, _, assets = self._services()
        asset = assets.get_asset(symbol)
        observed = account.get_position(symbol)
        client_order_id = self.trailing_client_order_id(cycle_id, attempt)
        try:
            result = orders.submit_trailing_stop_sell(
                asset=asset, qty=quantity,
                known_position_qty=ZERO if observed is None else abs(observed.qty),
                capability_check=self._checker(), trail_percent=trail_percent,
                time_in_force="gtc", cycle_id=cycle_id, attempt=attempt,
            )
        except OrderOutcomeUnresolved as exc:
            raise UnknownOrderOutcome(exc.client_order_id, repr(exc.lookup_error)) from exc
        return self._resolve(result, client_order_id)

    def replace_stop(
        self, order_id: str, stop_price: Decimal, quantity: Decimal, client_order_id: str
    ) -> BrokerOrder | None:
        """PATCH a working stop; return the REPLACEMENT order once the broker shows it.

        Alpaca keeps the old order working until the replacement is accepted, then marks the old
        order ``replaced``.  This method therefore confirms by reading the OLD order: only a
        ``replaced`` status (carrying ``replaced_by``) is a confirmed replacement.  Anything else
        returns ``None`` and the caller must treat the previous stop as still authoritative.
        """
        from mike_trade.brokers.alpaca.replacement import AlpacaStopReplacementAdapter
        from mike_trade.domain.trading import TimeInForce as TIF
        from mike_trade.execution.replace import ReplacementStatus

        _, _, _, _ = self._services()
        old = self.get_order(order_id)
        if old is None or old.is_terminal:
            return None
        tick = self.tick_size(old.symbol)
        checks = self.stop_capabilities()
        port = AlpacaStopReplacementAdapter(
            self.adapter._client,
            price_increment=tick,
            standalone_stop_verified=checks["stop_submit"],
            replace_verified=checks["stop_replace"],
        )
        outcome = port.replace_stop(order_id, stop_price, quantity, TIF.GTC, client_order_id)
        if outcome.status is ReplacementStatus.UNSUPPORTED:
            raise BrokerError(
                f"stop replacement is not a proven capability: {outcome.detail}",
                code="capability_unavailable", context={"order_id": order_id},
            )
        if outcome.status is not ReplacementStatus.CONFIRMED:
            outcome = port.lookup_order(order_id)
        if outcome.status is not ReplacementStatus.CONFIRMED or outcome.broker_order_id is None:
            return None
        replacement = self.get_order(outcome.broker_order_id)
        return replacement

    def cancel_order(self, order_id: str) -> BrokerOrder | None:
        orders, _, _, _ = self._services()
        orders.cancel_order(order_id)
        return self.get_order(order_id)
