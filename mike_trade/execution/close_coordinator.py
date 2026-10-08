"""Race-safe coordination of long-position sell intents.

The coordinator is deliberately a small orchestration boundary.  It owns no
broker connection and no durable store: the broker order port, clock, and event
sink are injected.  Broker observations are authoritative for both position
quantity and order state.

All mutations for one ``position_id`` run under the same asynchronous lock.
In particular, a discretionary close cannot pass a trailing-stop replacement,
and an exit order is never sent until removal of the previous protective stop
has been confirmed by the broker.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Protocol

__all__ = [
    "CloseCoordinator",
    "SellIntent",
    "CloseOutcome",
    "OrphanOrder",
    "detect_orphan_orders",
]

_ZERO = Decimal("0")
_TERMINAL = frozenset({"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "REPLACED"})
_WORKING = frozenset(
    {
        "NEW",
        "PENDING_NEW",
        "ACCEPTED",
        "PENDING",
        "PARTIALLY_FILLED",
        "PENDING_CANCEL",
        "CANCEL_PENDING",
        "SUSPENDED",
    }
)
_STOP_TYPES = frozenset({"STOP", "STOP_LIMIT", "TRAILING_STOP"})
_STRATEGY_KINDS = frozenset({"STRATEGY", "STRATEGY_EXIT", "EXIT_LONG"})
_MANUAL_KINDS = frozenset({"MANUAL", "MANUAL_CLOSE", "CLOSE_ALL", "FLATTEN"})
_SAFETY_KINDS = frozenset(
    {"SAFETY", "SAFETY_EXIT", "RISK_SAFETY", "DAILY_LOSS", "RECONCILIATION", "END_OF_DAY"}
)


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _CloseOrderPort(Protocol):
    """Minimal async broker surface used by :class:`CloseCoordinator`.

    A port may return domain objects or mappings.  Status, quantity, and ID
    fields use the normal normalized names (``status``, ``quantity``/``qty``,
    ``filled_quantity``/``filled_qty``, and ``broker_order_id``/``order_id``).
    """

    def confirmed_position_quantity(
        self, position_id: str, symbol: str
    ) -> Decimal | Awaitable[Decimal]: ...

    def list_open_orders(self) -> Sequence[object] | Awaitable[Sequence[object]]: ...

    def cancel_order(self, order_id: str) -> object | Awaitable[object]: ...

    def get_order(self, order_id: str) -> object | None | Awaitable[object | None]: ...

    def submit_exit(self, intent: SellIntent, quantity: Decimal) -> object | Awaitable[object]: ...

    def submit_protective_stop(
        self,
        position_id: str,
        symbol: str,
        quantity: Decimal,
        stop_price: Decimal,
        client_order_id: str,
    ) -> object | Awaitable[object]: ...

    def replace_protective_stop(
        self,
        order_id: str,
        position_id: str,
        symbol: str,
        quantity: Decimal,
        stop_price: Decimal,
        client_order_id: str,
    ) -> object | Awaitable[object]: ...


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _symbol(value: object) -> str:
    result = _text(value, "symbol").upper()
    if any(character.isspace() for character in result):
        raise ValueError("symbol must not contain whitespace")
    return result


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite() or value < _ZERO or (positive and value == _ZERO):
        operator = "> 0" if positive else ">= 0"
        raise ValueError(f"{name} must be finite and {operator}")
    return value


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _enum_text(value: object | None) -> str:
    raw = getattr(value, "value", value)
    return "" if raw is None else str(raw).strip().upper()


def _field(item: object, *names: str, default: object = None) -> object:
    if isinstance(item, Mapping):
        for name in names:
            if name in item:
                return item[name]
        return default
    for name in names:
        if hasattr(item, name):
            return getattr(item, name)
    return default


async def _resolve[T](value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


def _status(order: object | None) -> str:
    return _enum_text(_field(order, "status"))


def _order_id(order: object | None) -> str | None:
    value = _field(order, "broker_order_id", "order_id", "id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _client_order_id(order: object | None) -> str | None:
    value = _field(order, "client_order_id", "intent_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _quantity(order: object, *names: str) -> Decimal:
    value = _field(order, *names, default=_ZERO)
    if isinstance(value, str):
        value = Decimal(value)
    return _decimal(value, names[0])


def _is_working(order: object) -> bool:
    status = _status(order)
    return status in _WORKING or (bool(status) and status not in _TERMINAL and status != "UNKNOWN")


def _remaining(order: object) -> Decimal:
    total = _quantity(order, "quantity", "qty")
    filled = _quantity(order, "filled_quantity", "filled_qty")
    return max(_ZERO, total - filled)


@dataclass(frozen=True, slots=True)
class SellIntent:
    """One requested long-position sale.

    ``quantity`` is validated against the broker position before cancellation.
    If the protective stop fills during cancellation, the submitted amount is
    capped to the newly confirmed residual.  Only ``STRATEGY`` kinds observe
    ``minimum_hold``; every manual and safety kind bypasses it.
    """

    position_id: str
    symbol: str
    quantity: Decimal
    kind: str | Enum
    client_order_id: str
    order_type: str | Enum = "MARKET"
    limit_price: Decimal | None = None
    first_confirmed_entry_fill_at: datetime | None = None
    minimum_hold: timedelta = timedelta(0)
    protective_order_id: str | None = None
    last_confirmed_stop_price: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "position_id", _text(self.position_id, "position_id"))
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity", positive=True))
        kind = _enum_text(self.kind)
        if kind not in _STRATEGY_KINDS | _MANUAL_KINDS | _SAFETY_KINDS:
            raise ValueError("kind must be a strategy, manual, or safety exit")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self, "client_order_id", _text(self.client_order_id, "client_order_id")
        )
        order_type = _enum_text(self.order_type)
        if order_type not in {"MARKET", "LIMIT"}:
            raise ValueError("exit order_type must be MARKET or LIMIT")
        object.__setattr__(self, "order_type", order_type)
        if order_type == "LIMIT":
            if self.limit_price is None:
                raise ValueError("LIMIT exit requires limit_price")
            _decimal(self.limit_price, "limit_price", positive=True)
        elif self.limit_price is not None:
            raise ValueError("MARKET exit must not specify limit_price")
        if self.first_confirmed_entry_fill_at is not None:
            object.__setattr__(
                self,
                "first_confirmed_entry_fill_at",
                _utc(self.first_confirmed_entry_fill_at, "first_confirmed_entry_fill_at"),
            )
        if not isinstance(self.minimum_hold, timedelta) or self.minimum_hold < timedelta(0):
            raise ValueError("minimum_hold must be a non-negative timedelta")
        if self.protective_order_id is not None:
            object.__setattr__(
                self,
                "protective_order_id",
                _text(self.protective_order_id, "protective_order_id"),
            )
        if self.last_confirmed_stop_price is not None:
            _decimal(self.last_confirmed_stop_price, "last_confirmed_stop_price", positive=True)

    @property
    def is_strategy(self) -> bool:
        return str(self.kind) in _STRATEGY_KINDS

    @property
    def bypasses_entry_gates(self) -> bool:
        return not self.is_strategy


@dataclass(frozen=True, slots=True)
class CloseOutcome:
    status: str
    position_id: str
    symbol: str
    submitted_quantity: Decimal = _ZERO
    confirmed_remaining_quantity: Decimal | None = None
    broker_order_id: str | None = None
    protective_order_id: str | None = None
    stop_price: Decimal | None = None
    reconciliation_required: bool = False
    entries_blocked: bool = False
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _text(self.status, "status").upper())
        object.__setattr__(self, "position_id", _text(self.position_id, "position_id"))
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        _decimal(self.submitted_quantity, "submitted_quantity")
        if self.confirmed_remaining_quantity is not None:
            _decimal(self.confirmed_remaining_quantity, "confirmed_remaining_quantity")
        for name in ("broker_order_id", "protective_order_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _text(value, name))
        if self.stop_price is not None:
            _decimal(self.stop_price, "stop_price", positive=True)

    @property
    def flat(self) -> bool:
        return self.confirmed_remaining_quantity == _ZERO

    @property
    def submitted(self) -> bool:
        return self.submitted_quantity > _ZERO


@dataclass(frozen=True, slots=True)
class OrphanOrder:
    """An open broker-side sell that is not safe to adopt or ignore."""

    broker_order_id: str
    symbol: str
    order_type: str
    reason: str
    client_order_id: str | None = None
    position_quantity: Decimal = _ZERO

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "broker_order_id", _text(self.broker_order_id, "broker_order_id")
        )
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "order_type", _text(self.order_type, "order_type").upper())
        object.__setattr__(self, "reason", _text(self.reason, "reason").upper())
        if self.client_order_id is not None:
            object.__setattr__(
                self, "client_order_id", _text(self.client_order_id, "client_order_id")
            )
        _decimal(self.position_quantity, "position_quantity")

    @property
    def drift_type(self) -> str:
        return self.reason


def _known_intent_ids(local_intents: Iterable[object]) -> tuple[set[str], set[str]]:
    broker_ids: set[str] = set()
    client_ids: set[str] = set()
    for intent in local_intents:
        if isinstance(intent, str) and intent.strip():
            client_ids.add(intent.strip())
            continue
        broker_id = _order_id(intent)
        client_id = _client_order_id(intent)
        if broker_id is not None:
            broker_ids.add(broker_id)
        if client_id is not None:
            client_ids.add(client_id)
    return broker_ids, client_ids


def _position_quantities(
    local_positions: Iterable[object] | Mapping[str, object],
) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    values: Iterable[object]
    if isinstance(local_positions, Mapping):
        values = tuple(local_positions.items())
    else:
        values = local_positions
    for position in values:
        if isinstance(position, tuple) and len(position) == 2:
            symbol, raw_position = position
            raw_quantity = (
                raw_position
                if isinstance(raw_position, (Decimal, str))
                else _field(
                    raw_position,
                    "quantity",
                    "qty",
                    "remaining_quantity",
                    default=_ZERO,
                )
            )
        else:
            symbol = _field(position, "symbol")
            raw_quantity = _field(position, "quantity", "qty", "remaining_quantity", default=_ZERO)
        if not isinstance(symbol, str):
            continue
        if isinstance(raw_quantity, str):
            raw_quantity = Decimal(raw_quantity)
        if isinstance(raw_quantity, Decimal) and raw_quantity.is_finite():
            result[_symbol(symbol)] = max(_ZERO, raw_quantity)
    return result


def detect_orphan_orders(
    broker_open_orders: Iterable[object],
    local_intents: Iterable[object],
    local_positions: Iterable[object] | Mapping[str, object],
) -> tuple[OrphanOrder, ...]:
    """Return explicit drift records without canceling or adopting any order."""

    broker_ids, client_ids = _known_intent_ids(local_intents)
    positions = _position_quantities(local_positions)
    found: list[OrphanOrder] = []
    seen: set[str] = set()
    for order in broker_open_orders:
        if not _is_working(order):
            continue
        side = _enum_text(_field(order, "side"))
        order_type = _enum_text(_field(order, "order_type", "type"))
        if side != "SELL" and order_type not in _STOP_TYPES:
            continue
        broker_id = _order_id(order)
        symbol_value = _field(order, "symbol")
        if broker_id is None or not isinstance(symbol_value, str) or broker_id in seen:
            continue
        symbol = _symbol(symbol_value)
        client_id = _client_order_id(order)
        quantity = positions.get(symbol, _ZERO)
        known = broker_id in broker_ids or (client_id is not None and client_id in client_ids)
        reason: str | None = None
        if order_type in _STOP_TYPES and quantity == _ZERO:
            reason = "ORPHAN_STOP_FLAT_POSITION"
        elif not known:
            reason = "ORPHAN_SELL_NO_LOCAL_INTENT"
        elif quantity == _ZERO:
            reason = "ORPHAN_SELL_NO_POSITION"
        if reason is not None:
            seen.add(broker_id)
            found.append(
                OrphanOrder(
                    broker_order_id=broker_id,
                    client_order_id=client_id,
                    symbol=symbol,
                    order_type=order_type or "UNKNOWN",
                    reason=reason,
                    position_quantity=quantity,
                )
            )
    return tuple(found)


@dataclass(slots=True)
class _Protection:
    symbol: str
    order_id: str | None = None
    stop_price: Decimal | None = None


class CloseCoordinator:
    """Serialize every sell-side mutation for each managed long position."""

    def __init__(
        self,
        broker: _CloseOrderPort,
        clock: Callable[[], datetime] | _Clock,
        event_sink: Callable[[Mapping[str, object]], object] | object,
    ) -> None:
        if not callable(clock) and not callable(getattr(clock, "now", None)):
            raise TypeError("clock must be callable or expose now()")
        if not callable(event_sink) and not callable(getattr(event_sink, "emit", None)):
            raise TypeError("event_sink must be callable or expose emit()")
        self._broker = broker
        self._clock = clock
        self._event_sink = event_sink
        self._locks: dict[str, asyncio.Lock] = {}
        self._protections: dict[str, _Protection] = {}
        self._frozen: set[str] = set()
        self._last_quantities: dict[str, Decimal] = {}
        self._closed = False

    def register_protective_stop(
        self,
        *,
        position_id: str,
        symbol: str,
        order_id: str,
        stop_price: Decimal,
    ) -> None:
        """Register local intent linkage; this never adopts a broker orphan."""

        key = _text(position_id, "position_id")
        self._protections[key] = _Protection(
            _symbol(symbol),
            _text(order_id, "order_id"),
            _decimal(stop_price, "stop_price", positive=True),
        )

    def last_confirmed_quantity(self, position_id: str) -> Decimal | None:
        return self._last_quantities.get(_text(position_id, "position_id"))

    def is_frozen(self, position_id: str) -> bool:
        return _text(position_id, "position_id") in self._frozen

    async def close(self, intent: SellIntent) -> CloseOutcome:
        if not isinstance(intent, SellIntent):
            raise TypeError("intent must be SellIntent")
        if self._closed:
            return self._outcome("REJECTED", intent, detail="coordinator is shut down")
        async with self._lock(intent.position_id):
            return await self._close_locked(intent)

    execute = close
    submit = close

    async def replace_protective_stop(
        self,
        *,
        position_id: str,
        symbol: str,
        order_id: str,
        stop_price: Decimal,
        client_order_id: str,
        quantity: Decimal | None = None,
    ) -> CloseOutcome:
        key = _text(position_id, "position_id")
        normalized_symbol = _symbol(symbol)
        old_order_id = _text(order_id, "order_id")
        candidate = _decimal(stop_price, "stop_price", positive=True)
        client_id = _text(client_order_id, "client_order_id")
        if quantity is not None:
            _decimal(quantity, "quantity", positive=True)
        if self._closed:
            return CloseOutcome(
                "REJECTED", key, normalized_symbol, detail="coordinator is shut down"
            )
        async with self._lock(key):
            if key in self._frozen:
                return CloseOutcome(
                    "FROZEN",
                    key,
                    normalized_symbol,
                    reconciliation_required=True,
                    entries_blocked=True,
                    detail="position has an unresolved order outcome",
                )
            confirmed = await self._confirmed_quantity(key, normalized_symbol)
            requested = confirmed if quantity is None else quantity
            committed = await self._working_sell_quantity(
                key, normalized_symbol, exclude={old_order_id}
            )
            if requested > max(_ZERO, confirmed - committed):
                return CloseOutcome(
                    "REJECTED",
                    key,
                    normalized_symbol,
                    confirmed_remaining_quantity=confirmed,
                    detail="protective quantity exceeds broker-confirmed available quantity",
                )
            if confirmed == _ZERO:
                self._protections.pop(key, None)
                return CloseOutcome(
                    "FLAT", key, normalized_symbol, confirmed_remaining_quantity=_ZERO
                )
            current = self._protections.get(key)
            if (
                current is not None
                and current.stop_price is not None
                and candidate < current.stop_price
            ):
                return CloseOutcome(
                    "REJECTED",
                    key,
                    normalized_symbol,
                    confirmed_remaining_quantity=confirmed,
                    protective_order_id=old_order_id,
                    stop_price=current.stop_price,
                    detail="protective stop replacement may not lower the last confirmed stop",
                )
            try:
                raw = await _resolve(
                    self._broker.replace_protective_stop(
                        old_order_id,
                        key,
                        normalized_symbol,
                        min(requested, confirmed - committed),
                        candidate,
                        client_id,
                    )
                )
            except Exception as exc:
                return await self._freeze(
                    key,
                    normalized_symbol,
                    f"protective replacement outcome unknown: {type(exc).__name__}",
                )
            status = _status(raw)
            if status == "UNKNOWN" or not status:
                return await self._freeze(
                    key, normalized_symbol, "protective replacement is UNKNOWN"
                )
            remaining = await self._confirmed_quantity(key, normalized_symbol)
            if status == "FILLED" or remaining == _ZERO:
                self._protections.pop(key, None)
                return CloseOutcome(
                    "FLAT" if remaining == _ZERO else "STOP_FILLED",
                    key,
                    normalized_symbol,
                    confirmed_remaining_quantity=remaining,
                    broker_order_id=_order_id(raw),
                )
            if status not in _WORKING:
                return CloseOutcome(
                    "REJECTED",
                    key,
                    normalized_symbol,
                    confirmed_remaining_quantity=remaining,
                    protective_order_id=old_order_id,
                    stop_price=current.stop_price if current is not None else None,
                    detail=f"protective replacement ended {status}",
                )
            new_order_id = _order_id(raw) or old_order_id
            confirmed_stop = _field(raw, "stop_price", default=candidate)
            if isinstance(confirmed_stop, str):
                confirmed_stop = Decimal(confirmed_stop)
            price = _decimal(confirmed_stop, "confirmed stop_price", positive=True)
            if current is not None and current.stop_price is not None:
                price = max(price, current.stop_price)
            self._protections[key] = _Protection(normalized_symbol, new_order_id, price)
            await self._emit(
                "PROTECTIVE_STOP_REPLACED",
                position_id=key,
                symbol=normalized_symbol,
                order_id=new_order_id,
                stop_price=price,
                quantity=remaining,
            )
            return CloseOutcome(
                "REPLACED",
                key,
                normalized_symbol,
                confirmed_remaining_quantity=remaining,
                protective_order_id=new_order_id,
                stop_price=price,
            )

    async def detect_orphans(
        self,
        *,
        local_intents: Iterable[object],
        local_positions: Iterable[object] | Mapping[str, object],
    ) -> tuple[OrphanOrder, ...]:
        orders = await _resolve(self._broker.list_open_orders())
        orphans = detect_orphan_orders(orders, local_intents, local_positions)
        for orphan in orphans:
            await self._emit(
                "RECONCILIATION_DRIFT",
                drift_type=orphan.reason,
                broker_order_id=orphan.broker_order_id,
                client_order_id=orphan.client_order_id,
                symbol=orphan.symbol,
                order_type=orphan.order_type,
                position_quantity=orphan.position_quantity,
            )
        return orphans

    async def shutdown(self) -> None:
        """Stop accepting work.  Broker-held protective orders are left intact."""

        self._closed = True
        await self._emit("CLOSE_COORDINATOR_SHUTDOWN")

    async def _close_locked(self, intent: SellIntent) -> CloseOutcome:
        if intent.position_id in self._frozen:
            return self._outcome(
                "FROZEN",
                intent,
                reconciliation_required=True,
                entries_blocked=True,
                detail="position has an unresolved order outcome",
            )
        protection = self._protections.get(intent.position_id)
        if intent.protective_order_id is not None:
            if protection is None:
                protection = _Protection(
                    intent.symbol,
                    intent.protective_order_id,
                    intent.last_confirmed_stop_price,
                )
                self._protections[intent.position_id] = protection
            elif protection.order_id != intent.protective_order_id:
                return await self._freeze(
                    intent.position_id,
                    intent.symbol,
                    "local protective order identity conflicts with close intent",
                    intent=intent,
                )

        if intent.is_strategy and intent.minimum_hold > timedelta(0):
            if intent.first_confirmed_entry_fill_at is None:
                return self._outcome(
                    "HOLD_BLOCKED",
                    intent,
                    detail="strategy exit has no confirmed entry fill time",
                )
            if self._now() < intent.first_confirmed_entry_fill_at + intent.minimum_hold:
                return self._outcome(
                    "HOLD_BLOCKED", intent, detail="minimum hold has not elapsed"
                )

        confirmed_before = await self._confirmed_quantity(intent.position_id, intent.symbol)
        excluded = (
            {protection.order_id}
            if protection is not None and protection.order_id
            else set()
        )
        already_committed = await self._working_sell_quantity(
            intent.position_id, intent.symbol, exclude=excluded
        )
        available_before = max(_ZERO, confirmed_before - already_committed)
        if intent.quantity > available_before:
            await self._emit(
                "SELL_REJECTED_OVERSIZE",
                position_id=intent.position_id,
                symbol=intent.symbol,
                requested_quantity=intent.quantity,
                confirmed_quantity=confirmed_before,
                working_sell_quantity=already_committed,
            )
            return self._outcome(
                "REJECTED",
                intent,
                remaining=confirmed_before,
                detail="sell quantity exceeds broker-confirmed position minus working sells",
            )
        if confirmed_before == _ZERO:
            self._protections.pop(intent.position_id, None)
            return self._outcome("FLAT", intent, remaining=_ZERO)

        if protection is not None and protection.order_id is not None:
            cancel = await self._cancel_and_confirm(protection.order_id)
            if cancel is None or _status(cancel) == "UNKNOWN":
                return await self._freeze(
                    intent.position_id,
                    intent.symbol,
                    "protective stop cancellation is UNKNOWN",
                    intent=intent,
                )
            self._protections[intent.position_id] = _Protection(
                intent.symbol, None, protection.stop_price
            )
            await self._emit(
                "PROTECTIVE_STOP_CANCEL_CONFIRMED",
                position_id=intent.position_id,
                symbol=intent.symbol,
                order_id=protection.order_id,
                final_status=_status(cancel),
            )

        # This second broker read is the critical race boundary.  Any stop fills
        # observed during cancellation reduce (or eliminate) the exit order.
        confirmed = await self._confirmed_quantity(intent.position_id, intent.symbol)
        if confirmed == _ZERO:
            self._protections.pop(intent.position_id, None)
            await self._emit(
                "POSITION_FLAT_DURING_STOP_CANCEL",
                position_id=intent.position_id,
                symbol=intent.symbol,
            )
            return self._outcome("FLAT", intent, remaining=_ZERO)
        committed = await self._working_sell_quantity(intent.position_id, intent.symbol)
        available = max(_ZERO, confirmed - committed)
        quantity = min(intent.quantity, available)
        if quantity == _ZERO:
            return self._outcome(
                "REJECTED",
                intent,
                remaining=confirmed,
                detail="broker-confirmed quantity is fully committed to working sells",
            )

        try:
            order = await _resolve(self._broker.submit_exit(intent, quantity))
        except Exception as exc:
            return await self._recover_protection(
                intent,
                quantity,
                protection,
                f"exit submission failed: {type(exc).__name__}",
            )
        status = _status(order)
        if status == "UNKNOWN" or not status:
            return await self._freeze(
                intent.position_id,
                intent.symbol,
                "exit submission outcome is UNKNOWN",
                intent=intent,
                submitted_quantity=quantity,
            )
        order_id = _order_id(order)
        await self._emit(
            "EXIT_SELL_SUBMITTED",
            position_id=intent.position_id,
            symbol=intent.symbol,
            order_id=order_id,
            quantity=quantity,
            kind=intent.kind,
        )
        if status == "PARTIALLY_FILLED":
            if order_id is None:
                return await self._unprotected(
                    intent, quantity, "partially filled exit has no broker order id"
                )
            canceled = await self._cancel_and_confirm(order_id)
            if canceled is None or _status(canceled) == "UNKNOWN":
                return await self._unprotected(
                    intent, quantity, "partial exit remainder cancellation is UNKNOWN"
                )
            return await self._recover_protection(
                intent, quantity, protection, "exit partially filled", broker_order_id=order_id
            )
        if status in {"REJECTED", "CANCELED", "CANCELLED", "EXPIRED"}:
            return await self._recover_protection(
                intent,
                quantity,
                protection,
                f"exit ended {status}",
                broker_order_id=order_id,
            )
        if status == "FILLED":
            remaining = await self._confirmed_quantity(intent.position_id, intent.symbol)
            if remaining == _ZERO:
                self._protections.pop(intent.position_id, None)
                return self._outcome(
                    "FLAT", intent, submitted=quantity, remaining=_ZERO, broker_order_id=order_id
                )
            return await self._recover_protection(
                intent,
                quantity,
                protection,
                "exit filled but a broker position remains",
                broker_order_id=order_id,
            )
        return self._outcome(
            "SUBMITTED",
            intent,
            submitted=quantity,
            remaining=confirmed,
            broker_order_id=order_id,
        )

    async def _recover_protection(
        self,
        intent: SellIntent,
        submitted: Decimal,
        prior: _Protection | None,
        detail: str,
        *,
        broker_order_id: str | None = None,
    ) -> CloseOutcome:
        remaining = await self._confirmed_quantity(intent.position_id, intent.symbol)
        if remaining == _ZERO:
            self._protections.pop(intent.position_id, None)
            return self._outcome(
                "FLAT",
                intent,
                submitted=submitted,
                remaining=_ZERO,
                broker_order_id=broker_order_id,
                detail=detail,
            )
        stop_price = prior.stop_price if prior is not None else intent.last_confirmed_stop_price
        if stop_price is None:
            return await self._unprotected(
                intent, submitted, f"{detail}; no last confirmed stop price", remaining=remaining
            )
        working = await self._working_sell_quantity(intent.position_id, intent.symbol)
        protectable = max(_ZERO, remaining - working)
        if protectable != remaining:
            return await self._unprotected(
                intent,
                submitted,
                f"{detail}; working sell prevents non-overlapping re-protection",
                remaining=remaining,
            )
        protection_client_id = f"{intent.client_order_id}-reprotect"
        try:
            order = await _resolve(
                self._broker.submit_protective_stop(
                    intent.position_id,
                    intent.symbol,
                    remaining,
                    stop_price,
                    protection_client_id,
                )
            )
        except Exception as exc:
            return await self._unprotected(
                intent,
                submitted,
                f"{detail}; re-protection failed: {type(exc).__name__}",
                remaining=remaining,
            )
        status = _status(order)
        order_id = _order_id(order)
        raw_stop = _field(order, "stop_price", default=stop_price)
        if isinstance(raw_stop, str):
            raw_stop = Decimal(raw_stop)
        if status not in _WORKING or order_id is None:
            return await self._unprotected(
                intent,
                submitted,
                f"{detail}; re-protection ended {status or 'UNKNOWN'}",
                remaining=remaining,
            )
        confirmed_stop = _decimal(raw_stop, "stop_price", positive=True)
        if confirmed_stop < stop_price:
            return await self._unprotected(
                intent,
                submitted,
                f"{detail}; broker confirmed re-protection below last stop",
                remaining=remaining,
            )
        self._protections[intent.position_id] = _Protection(
            intent.symbol, order_id, confirmed_stop
        )
        await self._emit(
            "PROTECTIVE_STOP_REESTABLISHED",
            position_id=intent.position_id,
            symbol=intent.symbol,
            order_id=order_id,
            quantity=remaining,
            stop_price=confirmed_stop,
            reason=detail,
        )
        return self._outcome(
            "REPROTECTED",
            intent,
            submitted=submitted,
            remaining=remaining,
            broker_order_id=broker_order_id,
            protective_order_id=order_id,
            stop_price=confirmed_stop,
            detail=detail,
        )

    async def _cancel_and_confirm(self, order_id: str) -> object | None:
        try:
            result = await _resolve(self._broker.cancel_order(order_id))
        except Exception:
            result = None
        status = _status(result)
        if status in _TERMINAL:
            return result
        try:
            observed = await _resolve(self._broker.get_order(order_id))
        except Exception:
            return None
        return observed if _status(observed) in _TERMINAL else None

    async def _confirmed_quantity(self, position_id: str, symbol: str) -> Decimal:
        value = await _resolve(self._broker.confirmed_position_quantity(position_id, symbol))
        quantity = _decimal(value, "broker-confirmed position quantity")
        self._last_quantities[position_id] = quantity
        return quantity

    async def _working_sell_quantity(
        self, position_id: str, symbol: str, *, exclude: set[str] | None = None
    ) -> Decimal:
        del position_id  # the normalized broker order may expose only symbol linkage
        excluded = exclude or set()
        orders = await _resolve(self._broker.list_open_orders())
        return sum(
            (
                _remaining(order)
                for order in orders
                if _is_working(order)
                and _enum_text(_field(order, "side")) == "SELL"
                and _enum_text(_field(order, "symbol")) == symbol
                and (_order_id(order) or "") not in excluded
            ),
            start=_ZERO,
        )

    async def _freeze(
        self,
        position_id: str,
        symbol: str,
        detail: str,
        *,
        intent: SellIntent | None = None,
        submitted_quantity: Decimal = _ZERO,
    ) -> CloseOutcome:
        self._frozen.add(position_id)
        await self._emit(
            "RECONCILIATION_REQUIRED",
            position_id=position_id,
            symbol=symbol,
            reason=detail,
        )
        await self._emit(
            "ENTRY_BLOCKED",
            position_id=position_id,
            symbol=symbol,
            reason="UNRESOLVED_SELL_ORDER",
        )
        if intent is not None:
            return self._outcome(
                "FROZEN",
                intent,
                submitted=submitted_quantity,
                reconciliation_required=True,
                entries_blocked=True,
                detail=detail,
            )
        return CloseOutcome(
            "FROZEN",
            position_id,
            symbol,
            submitted_quantity=submitted_quantity,
            reconciliation_required=True,
            entries_blocked=True,
            detail=detail,
        )

    async def _unprotected(
        self,
        intent: SellIntent,
        submitted: Decimal,
        detail: str,
        *,
        remaining: Decimal | None = None,
    ) -> CloseOutcome:
        if remaining is None:
            remaining = await self._confirmed_quantity(intent.position_id, intent.symbol)
        self._frozen.add(intent.position_id)
        await self._emit(
            "UNPROTECTED",
            position_id=intent.position_id,
            symbol=intent.symbol,
            quantity=remaining,
            reason=detail,
        )
        await self._emit(
            "ENTRY_BLOCKED",
            position_id=intent.position_id,
            symbol=intent.symbol,
            reason="UNPROTECTED",
        )
        return self._outcome(
            "UNPROTECTED",
            intent,
            submitted=submitted,
            remaining=remaining,
            reconciliation_required=True,
            entries_blocked=True,
            detail=detail,
        )

    def _outcome(
        self,
        status: str,
        intent: SellIntent,
        *,
        submitted: Decimal = _ZERO,
        remaining: Decimal | None = None,
        broker_order_id: str | None = None,
        protective_order_id: str | None = None,
        stop_price: Decimal | None = None,
        reconciliation_required: bool = False,
        entries_blocked: bool = False,
        detail: str = "",
    ) -> CloseOutcome:
        return CloseOutcome(
            status=status,
            position_id=intent.position_id,
            symbol=intent.symbol,
            submitted_quantity=submitted,
            confirmed_remaining_quantity=remaining,
            broker_order_id=broker_order_id,
            protective_order_id=protective_order_id,
            stop_price=stop_price,
            reconciliation_required=reconciliation_required,
            entries_blocked=entries_blocked,
            detail=detail,
        )

    def _lock(self, position_id: str) -> asyncio.Lock:
        return self._locks.setdefault(position_id, asyncio.Lock())

    def _now(self) -> datetime:
        method = getattr(self._clock, "now", None)
        value = method() if callable(method) else self._clock()  # type: ignore[operator]
        return _utc(value, "clock")

    async def _emit(self, event_type: str, **payload: object) -> None:
        event = MappingProxyType({"event_type": event_type, "occurred_at": self._now(), **payload})
        emit = getattr(self._event_sink, "emit", None)
        result = emit(event) if callable(emit) else self._event_sink(event)  # type: ignore[operator]
        if inspect.isawaitable(result):
            await result
