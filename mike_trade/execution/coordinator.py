"""Serialized, broker-neutral authorization for order-affecting commands.

The coordinator owns only short-lived authorization and cash-reservation state.
It deliberately does not persist intents, call a broker, or submit orders.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Protocol, TypeVar

__all__ = [
    "OrderCommandCoordinator",
    "OrderCommand",
    "CommandDecision",
    "DecisionReason",
    "CashReservation",
    "ReservationLedger",
    "ExecutionKey",
]

_ZERO = Decimal("0")
_T = TypeVar("_T")


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _IntentLike(Protocol):
    @property
    def execution_mode(self) -> str | Enum: ...

    @property
    def account_key(self) -> str: ...

    @property
    def symbol(self) -> str: ...

    @property
    def purpose(self) -> str | Enum: ...

    @property
    def signal_expires_at(self) -> datetime: ...

    @property
    def quote_reference_time(self) -> datetime: ...

    @property
    def qty(self) -> Decimal | None: ...

    @property
    def notional(self) -> Decimal | None: ...

    @property
    def limit_price(self) -> Decimal | None: ...


class _QuoteAgeProvider(Protocol):
    def quote_age(
        self, intent: _IntentLike, now: datetime
    ) -> timedelta | Awaitable[timedelta]: ...


class _OpenOrderProvider(Protocol):
    def has_open_or_unresolved_order(
        self, key: ExecutionKey, purpose: str
    ) -> bool | Awaitable[bool]: ...


class _PositionProvider(Protocol):
    def confirmed_long_quantity(self, key: ExecutionKey) -> Decimal | Awaitable[Decimal]: ...

    def committed_sell_quantity(self, key: ExecutionKey) -> Decimal | Awaitable[Decimal]: ...

    def protective_sell_quantity(self, key: ExecutionKey) -> Decimal | Awaitable[Decimal]: ...


class _AvailableCashProvider(Protocol):
    def available_cash(self, key: ExecutionKey) -> Decimal | Awaitable[Decimal]: ...


async def _resolve(value: _T | Awaitable[_T]) -> _T:
    if inspect.isawaitable(value):
        return await value
    return value


def _enum_text(value: object, *, field: str) -> str:
    raw = getattr(value, "value", value)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field} must be a non-empty string or string-valued enum")
    return raw.strip().upper()


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _decimal(value: object, *, field: str, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    if value < _ZERO or (positive and value == _ZERO):
        operator = "> 0" if positive else ">= 0"
        raise ValueError(f"{field} must be {operator}")
    return value


def _aware(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value


@dataclass(frozen=True, slots=True)
class ExecutionKey:
    """Strict isolation key for one mode, account/scenario, and symbol."""

    mode: str | Enum
    account_id: str
    symbol: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", _enum_text(self.mode, field="mode"))
        object.__setattr__(self, "account_id", _text(self.account_id, field="account_id"))
        symbol = _text(self.symbol, field="symbol").upper()
        if any(character.isspace() for character in symbol):
            raise ValueError("symbol must not contain whitespace")
        object.__setattr__(self, "symbol", symbol)

    @classmethod
    def from_intent(cls, intent: _IntentLike) -> ExecutionKey:
        return cls(intent.execution_mode, intent.account_key, intent.symbol)

    @property
    def account_scope(self) -> tuple[str, str]:
        return (_enum_text(self.mode, field="mode"), self.account_id)


@dataclass(frozen=True, slots=True)
class OrderCommand:
    """An intent plus coordinator-only reservation/cancellation instructions."""

    intent: _IntentLike
    reservation_amount: Decimal | None = None
    cancel_protective_first: bool = False

    def __post_init__(self) -> None:
        # Structural validation keeps this module independent of f.1's concrete intent.
        for field in (
            "execution_mode",
            "account_key",
            "symbol",
            "purpose",
            "signal_expires_at",
            "quote_reference_time",
            "qty",
            "notional",
            "limit_price",
        ):
            if not hasattr(self.intent, field):
                raise TypeError(f"intent must provide {field}")
        if self.reservation_amount is not None:
            _decimal(self.reservation_amount, field="reservation_amount", positive=True)
        if not isinstance(self.cancel_protective_first, bool):
            raise TypeError("cancel_protective_first must be bool")

    @property
    def key(self) -> ExecutionKey:
        return ExecutionKey.from_intent(self.intent)

    @property
    def purpose(self) -> str:
        return _enum_text(self.intent.purpose, field="purpose")

    @property
    def requested_cash(self) -> Decimal:
        if self.reservation_amount is not None:
            return self.reservation_amount
        if self.intent.notional is not None:
            return _decimal(self.intent.notional, field="intent.notional", positive=True)
        if self.intent.qty is not None and self.intent.limit_price is not None:
            quantity = _decimal(self.intent.qty, field="intent.qty", positive=True)
            price = _decimal(self.intent.limit_price, field="intent.limit_price", positive=True)
            return quantity * price
        raise ValueError(
            "ENTRY_LONG requires reservation_amount, notional, or qty with limit_price"
        )


class DecisionReason(Enum):
    AUTHORIZED = "AUTHORIZED"
    REJECTED_STALE_SIGNAL = "REJECTED_STALE_SIGNAL"
    REJECTED_STALE_QUOTE = "REJECTED_STALE_QUOTE"
    REJECTED_INSUFFICIENT_CASH = "REJECTED_INSUFFICIENT_CASH"
    REJECTED_OPEN_ORDER = "REJECTED_OPEN_ORDER"
    REJECTED_OVERSELL = "REJECTED_OVERSELL"
    REQUIRES_PROTECTIVE_CANCEL = "REQUIRES_PROTECTIVE_CANCEL"


@dataclass(frozen=True, slots=True)
class CashReservation:
    """Immutable receipt for cash held in a single account-scoped ledger."""

    reservation_id: str
    execution_key: ExecutionKey
    amount: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "reservation_id", _text(self.reservation_id, field="reservation_id")
        )
        if not isinstance(self.execution_key, ExecutionKey):
            raise TypeError("execution_key must be ExecutionKey")
        _decimal(self.amount, field="amount", positive=True)


@dataclass(frozen=True, slots=True)
class CommandDecision:
    reason: DecisionReason
    key: ExecutionKey
    purpose: str
    reservation: CashReservation | None = None
    available_cash: Decimal | None = None
    available_quantity: Decimal | None = None
    protective_quantity: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, DecisionReason):
            raise TypeError("reason must be DecisionReason")
        if not isinstance(self.key, ExecutionKey):
            raise TypeError("key must be ExecutionKey")
        object.__setattr__(self, "purpose", _enum_text(self.purpose, field="purpose"))
        for field in ("available_cash", "available_quantity", "protective_quantity"):
            value = getattr(self, field)
            if value is not None:
                _decimal(value, field=field)

    @property
    def authorized(self) -> bool:
        return self.reason is DecisionReason.AUTHORIZED

    @property
    def requires_cancel_confirmation(self) -> bool:
        return self.reason is DecisionReason.REQUIRES_PROTECTIVE_CANCEL

    @property
    def status(self) -> DecisionReason:
        """Alias useful to callers that name the typed outcome ``status``."""
        return self.reason


class ReservationLedger:
    """Account-isolated Decimal cash holds with atomic reserve/release/consume hooks."""

    def __init__(self, available_cash_provider: _AvailableCashProvider) -> None:
        self._provider = available_cash_provider
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._reservations: dict[str, tuple[CashReservation, Decimal]] = {}
        self._sequence = 0

    def _lock_for(self, account_scope: tuple[str, str]) -> asyncio.Lock:
        lock = self._locks.get(account_scope)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[account_scope] = lock
        return lock

    def _held(self, account_scope: tuple[str, str]) -> Decimal:
        return sum(
            (
                remaining
                for reservation, remaining in self._reservations.values()
                if reservation.execution_key.account_scope == account_scope
            ),
            start=_ZERO,
        )

    async def available(self, key: ExecutionKey) -> Decimal:
        if not isinstance(key, ExecutionKey):
            raise TypeError("key must be ExecutionKey")
        async with self._lock_for(key.account_scope):
            return await self._available_locked(key)

    async def _available_locked(self, key: ExecutionKey) -> Decimal:
        supplied = await _resolve(self._provider.available_cash(key))
        base = _decimal(supplied, field="available cash")
        available = base - self._held(key.account_scope)
        return max(_ZERO, available)

    async def reserve(
        self, key: ExecutionKey, amount: Decimal
    ) -> tuple[CashReservation | None, Decimal]:
        if not isinstance(key, ExecutionKey):
            raise TypeError("key must be ExecutionKey")
        requested = _decimal(amount, field="amount", positive=True)
        async with self._lock_for(key.account_scope):
            available = await self._available_locked(key)
            if requested > available:
                return None, available
            self._sequence += 1
            reservation = CashReservation(f"cash-{self._sequence}", key, requested)
            self._reservations[reservation.reservation_id] = (reservation, requested)
            return reservation, available - requested

    async def release(self, reservation: CashReservation) -> Decimal:
        """Release the exact unconsumed amount and return that released amount."""
        stored = self._stored(reservation)
        async with self._lock_for(stored.execution_key.account_scope):
            stored, remaining = self._pop_checked(reservation)
            return remaining

    async def consume(self, reservation: CashReservation, amount: Decimal) -> Decimal:
        """Consume part of a hold and return its exact remaining Decimal amount."""
        consumed = _decimal(amount, field="amount", positive=True)
        stored = self._stored(reservation)
        async with self._lock_for(stored.execution_key.account_scope):
            stored, remaining = self._checked(reservation)
            if consumed > remaining:
                raise ValueError("consumed amount exceeds remaining reservation")
            new_remaining = remaining - consumed
            if new_remaining == _ZERO:
                del self._reservations[stored.reservation_id]
            else:
                self._reservations[stored.reservation_id] = (stored, new_remaining)
            return new_remaining

    async def remaining(self, reservation: CashReservation) -> Decimal:
        stored = self._stored(reservation)
        async with self._lock_for(stored.execution_key.account_scope):
            _, remaining = self._checked(reservation)
            return remaining

    def _stored(self, reservation: CashReservation) -> CashReservation:
        if not isinstance(reservation, CashReservation):
            raise TypeError("reservation must be CashReservation")
        found = self._reservations.get(reservation.reservation_id)
        if found is None:
            raise KeyError(f"unknown reservation: {reservation.reservation_id}")
        return found[0]

    def _checked(self, reservation: CashReservation) -> tuple[CashReservation, Decimal]:
        found = self._reservations.get(reservation.reservation_id)
        if found is None or found[0] != reservation:
            raise KeyError(f"unknown reservation: {reservation.reservation_id}")
        return found

    def _pop_checked(self, reservation: CashReservation) -> tuple[CashReservation, Decimal]:
        found = self._checked(reservation)
        del self._reservations[reservation.reservation_id]
        return found


class OrderCommandCoordinator:
    """Serialize and authorize commands without performing submission side effects."""

    def __init__(
        self,
        *,
        clock: _Clock,
        quote_age_provider: _QuoteAgeProvider,
        open_order_provider: _OpenOrderProvider,
        position_provider: _PositionProvider,
        buying_power_budget_provider: _AvailableCashProvider,
        max_quote_age: timedelta,
        reservation_ledger: ReservationLedger | None = None,
    ) -> None:
        if not isinstance(max_quote_age, timedelta) or max_quote_age < timedelta(0):
            raise ValueError("max_quote_age must be a non-negative timedelta")
        self._clock = clock
        self._quote_age_provider = quote_age_provider
        self._open_order_provider = open_order_provider
        self._position_provider = position_provider
        self._max_quote_age = max_quote_age
        self._symbol_locks: dict[ExecutionKey, asyncio.Lock] = {}
        self._unresolved: set[ExecutionKey] = set()
        self.reservation_ledger = reservation_ledger or ReservationLedger(
            buying_power_budget_provider
        )

    def _lock_for(self, key: ExecutionKey) -> asyncio.Lock:
        lock = self._symbol_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._symbol_locks[key] = lock
        return lock

    async def authorize(self, command: OrderCommand) -> CommandDecision:
        if not isinstance(command, OrderCommand):
            raise TypeError("command must be OrderCommand")
        key = command.key
        purpose = command.purpose
        async with self._lock_for(key):
            # Every mutable/checker-backed decision is intentionally made here,
            # after any earlier command for this exact isolation key completes.
            now = _aware(self._clock.now(), field="clock.now()")
            expires_at = _aware(
                command.intent.signal_expires_at, field="intent.signal_expires_at"
            )
            if now >= expires_at:
                return CommandDecision(DecisionReason.REJECTED_STALE_SIGNAL, key, purpose)

            quote_age = await _resolve(self._quote_age_provider.quote_age(command.intent, now))
            if not isinstance(quote_age, timedelta):
                raise TypeError("quote_age provider must return timedelta")
            if quote_age < timedelta(0) or quote_age > self._max_quote_age:
                return CommandDecision(DecisionReason.REJECTED_STALE_QUOTE, key, purpose)

            if key in self._unresolved or await _resolve(
                self._open_order_provider.has_open_or_unresolved_order(key, purpose)
            ):
                return CommandDecision(DecisionReason.REJECTED_OPEN_ORDER, key, purpose)

            if purpose == "ENTRY_LONG":
                reservation, available = await self.reservation_ledger.reserve(
                    key, command.requested_cash
                )
                if reservation is None:
                    return CommandDecision(
                        DecisionReason.REJECTED_INSUFFICIENT_CASH,
                        key,
                        purpose,
                        available_cash=available,
                    )
                self._unresolved.add(key)
                return CommandDecision(
                    DecisionReason.AUTHORIZED,
                    key,
                    purpose,
                    reservation=reservation,
                    available_cash=available,
                )

            return await self._authorize_sell_locked(command, key, purpose)

    async def _authorize_sell_locked(
        self, command: OrderCommand, key: ExecutionKey, purpose: str
    ) -> CommandDecision:
        requested = _decimal(command.intent.qty, field="intent.qty", positive=True)
        held = _decimal(
            await _resolve(self._position_provider.confirmed_long_quantity(key)),
            field="confirmed long quantity",
        )
        committed = _decimal(
            await _resolve(self._position_provider.committed_sell_quantity(key)),
            field="committed sell quantity",
        )
        protective = _decimal(
            await _resolve(self._position_provider.protective_sell_quantity(key)),
            field="protective sell quantity",
        )
        if protective > committed:
            raise ValueError("protective sell quantity cannot exceed committed sell quantity")

        available = max(_ZERO, held - committed)
        if requested <= available:
            self._unresolved.add(key)
            return CommandDecision(
                DecisionReason.AUTHORIZED,
                key,
                purpose,
                available_quantity=available,
                protective_quantity=protective,
            )

        available_after_protective_cancel = max(_ZERO, held - (committed - protective))
        if (
            command.cancel_protective_first
            and protective > _ZERO
            and requested <= available_after_protective_cancel
        ):
            self._unresolved.add(key)
            return CommandDecision(
                DecisionReason.REQUIRES_PROTECTIVE_CANCEL,
                key,
                purpose,
                available_quantity=available_after_protective_cancel,
                protective_quantity=protective,
            )

        return CommandDecision(
            DecisionReason.REJECTED_OVERSELL,
            key,
            purpose,
            available_quantity=available,
            protective_quantity=protective,
        )

    async def release_reservation(self, reservation: CashReservation) -> Decimal:
        """Abandon an entry authorization and release its remaining cash exactly."""
        key = reservation.execution_key
        async with self._lock_for(key):
            released = await self.reservation_ledger.release(reservation)
            self._unresolved.discard(key)
            return released

    async def consume_reservation(
        self, reservation: CashReservation, amount: Decimal
    ) -> Decimal:
        """Fill hook: consume ``amount`` from an entry reservation."""
        return await self.reservation_ledger.consume(reservation, amount)

    async def complete(self, command: OrderCommand) -> None:
        """Clear the coordinator's unresolved claim after a terminal order outcome."""
        if not isinstance(command, OrderCommand):
            raise TypeError("command must be OrderCommand")
        async with self._lock_for(command.key):
            self._unresolved.discard(command.key)
