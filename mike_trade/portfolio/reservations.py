"""Serialized, idempotent capital reservations for managed entry cycles."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from weakref import WeakKeyDictionary

from mike_trade.domain.accounts import AccountScope
from mike_trade.portfolio.budgets import BudgetPolicy, BudgetSnapshot

__all__ = [
    "BudgetReservation",
    "ReservationLedger",
    "ReservationService",
    "ReservationRepository",
]

_ZERO = Decimal("0")
_ENVIRONMENTS = frozenset({"backtest", "shadow", "paper", "live"})
_TERMINAL_ORDER_STATUSES = frozenset({"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"})


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if value < _ZERO or (positive and value == _ZERO):
        operator = "> 0" if positive else ">= 0"
        raise ValueError(f"{name} must be {operator}")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _scope_id(value: AccountScope | str) -> str:
    raw = value.scope_id if isinstance(value, AccountScope) else _text(value, "account_scope_id")
    parts = raw.split(":")
    if any(not part for part in parts) or len(parts) < 2 or parts[0].lower() not in _ENVIRONMENTS:
        raise ValueError("account_scope_id must be environment-qualified")
    if parts[0].lower() in {"shadow", "backtest"} and len(parts) != 3:
        raise ValueError("SHADOW and BACKTEST account scopes require a scenario")
    if parts[0].lower() in {"paper", "live"} and len(parts) != 2:
        raise ValueError("PAPER and LIVE account scopes must not include a scenario")
    return raw


def _symbol(value: object) -> str:
    result = _text(value, "symbol").upper()
    if any(character.isspace() for character in result):
        raise ValueError("symbol must not contain whitespace")
    return result


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC (zero offset)")
    return value.astimezone(UTC)


def _priority(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int")
    return value


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    """Immutable state for one symbol's single managed entry cycle."""

    account_scope_id: str
    symbol: str
    client_order_id: str
    initial_reserved_usd: Decimal
    pending_reserved_usd: Decimal
    committed_open_notional_usd: Decimal
    state: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_scope_id", _scope_id(self.account_scope_id))
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(
            self, "client_order_id", _text(self.client_order_id, "client_order_id")
        )
        initial = _decimal(self.initial_reserved_usd, "initial_reserved_usd", positive=True)
        pending = _decimal(self.pending_reserved_usd, "pending_reserved_usd")
        committed = _decimal(
            self.committed_open_notional_usd, "committed_open_notional_usd"
        )
        if pending > initial:
            raise ValueError("pending_reserved_usd must not exceed initial_reserved_usd")
        state = _text(self.state, "state").upper()
        if state not in {"PENDING", "OPEN", "CLOSED"}:
            raise ValueError("state must be PENDING, OPEN, or CLOSED")
        if state == "PENDING" and committed != _ZERO:
            raise ValueError("PENDING reservations cannot have committed notional")
        if state == "CLOSED" and (pending != _ZERO or committed != _ZERO):
            raise ValueError("CLOSED reservations cannot retain capital")
        created = _utc(self.created_at, "created_at")
        updated = _utc(self.updated_at, "updated_at")
        if updated < created:
            raise ValueError("updated_at must not precede created_at")
        object.__setattr__(self, "initial_reserved_usd", initial)
        object.__setattr__(self, "pending_reserved_usd", pending)
        object.__setattr__(self, "committed_open_notional_usd", committed)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.account_scope_id, self.symbol, self.client_order_id)

    @property
    def reservation_id(self) -> str:
        return ":".join(self.key)

    @property
    def amount_usd(self) -> Decimal:
        """The still-pending portion of the reservation."""
        return self.pending_reserved_usd

    @property
    def reserved_amount_usd(self) -> Decimal:
        return self.pending_reserved_usd

    @property
    def cycle_open(self) -> bool:
        return self.state != "CLOSED"


class ReservationRepository(Protocol):
    """Persistence adapter hook for a single-writer durable store.

    ``save`` must atomically replace the reservations and processed activity
    IDs for the supplied scope in one transaction.  A SQLite adapter should
    implement it with the repository's existing ``unit_of_work`` writer.
    """

    def load(
        self, account_scope_id: str
    ) -> tuple[tuple[BudgetReservation, ...], frozenset[str]] | None | Awaitable[
        tuple[tuple[BudgetReservation, ...], frozenset[str]] | None
    ]: ...

    def save(
        self,
        account_scope_id: str,
        reservations: tuple[BudgetReservation, ...],
        processed_activity_ids: frozenset[str],
    ) -> None | Awaitable[None]: ...


@dataclass(frozen=True, slots=True)
class _ScopeState:
    reservations: tuple[BudgetReservation, ...] = ()
    processed_activity_ids: frozenset[str] = frozenset()


class ReservationLedger:
    """Atomic application budget ledger, serialized by account/scenario."""

    # Shared across ledger/service instances so all in-process writers for an
    # account use exactly one lock.  The persistence port remains responsible
    # for an atomic durable save through the application's single writer.
    _account_locks: WeakKeyDictionary[
        asyncio.AbstractEventLoop, dict[str, asyncio.Lock]
    ] = WeakKeyDictionary()

    def __init__(
        self,
        policy: BudgetPolicy,
        repository: ReservationRepository | None = None,
        clock: Callable[[], datetime] | Any | None = None,
    ) -> None:
        if not isinstance(policy, BudgetPolicy):
            raise TypeError("policy must be BudgetPolicy")
        self.policy = policy
        self._repository = repository
        self._clock = clock
        self._states: dict[str, _ScopeState] = {}

    @classmethod
    def _lock_for(cls, account_scope_id: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        loop_locks = cls._account_locks.setdefault(loop, {})
        lock = loop_locks.get(account_scope_id)
        if lock is None:
            lock = asyncio.Lock()
            loop_locks[account_scope_id] = lock
        return lock

    def _now(self) -> datetime:
        if self._clock is None:
            return datetime.now(UTC)
        now_method = getattr(self._clock, "now", None)
        value = now_method() if callable(now_method) else self._clock()
        return _utc(value, "clock")

    async def _load_locked(self, scope: str, *, refresh: bool = False) -> _ScopeState:
        if self._repository is None:
            return self._states.setdefault(scope, _ScopeState())
        if not refresh and scope in self._states:
            return self._states[scope]
        loaded = self._repository.load(scope)
        if inspect.isawaitable(loaded):
            loaded = await loaded
        if loaded is None:
            state = _ScopeState()
        else:
            reservations, activity_ids = loaded
            if not isinstance(reservations, tuple) or not all(
                isinstance(item, BudgetReservation) for item in reservations
            ):
                raise TypeError("repository reservations must be a tuple of BudgetReservation")
            if not isinstance(activity_ids, frozenset) or not all(
                isinstance(item, str) and item for item in activity_ids
            ):
                raise TypeError("repository activity IDs must be a frozenset of strings")
            if any(item.account_scope_id != scope for item in reservations):
                raise ValueError("repository returned a reservation from another scope")
            state = _ScopeState(reservations, activity_ids)
        self._states[scope] = state
        return state

    async def _save_locked(self, scope: str, state: _ScopeState) -> None:
        if self._repository is not None:
            saved = self._repository.save(
                scope, state.reservations, state.processed_activity_ids
            )
            if inspect.isawaitable(saved):
                await saved
        self._states[scope] = state

    @staticmethod
    def _find(
        state: _ScopeState, symbol: str, client_order_id: str
    ) -> tuple[int, BudgetReservation]:
        for index, reservation in enumerate(state.reservations):
            if (
                reservation.symbol == symbol
                and reservation.client_order_id == client_order_id
            ):
                return index, reservation
        raise KeyError(f"unknown reservation for {symbol}/{client_order_id}")

    @staticmethod
    def _totals(
        state: _ScopeState, symbol: str
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        committed = _ZERO
        pending = _ZERO
        symbol_committed = _ZERO
        symbol_pending = _ZERO
        for reservation in state.reservations:
            if reservation.state == "CLOSED":
                continue
            committed += reservation.committed_open_notional_usd
            pending += reservation.pending_reserved_usd
            if reservation.symbol == symbol:
                symbol_committed += reservation.committed_open_notional_usd
                symbol_pending += reservation.pending_reserved_usd
        return committed, pending, symbol_committed, symbol_pending

    def _snapshot_from_state(
        self,
        scope: str,
        symbol: str,
        state: _ScopeState,
        *,
        broker_buying_power_usd: Decimal,
        settled_cash_usd: Decimal | None,
        non_margin_buying_power_usd: Decimal | None,
        non_marginable_buying_power_usd: Decimal | None,
    ) -> BudgetSnapshot:
        committed, pending, symbol_committed, symbol_pending = self._totals(state, symbol)
        return BudgetSnapshot.calculate(
            policy=self.policy,
            account_scope_id=scope,
            symbol=symbol,
            committed_open_notional_usd=committed,
            pending_reserved_usd=pending,
            symbol_committed_open_notional_usd=symbol_committed,
            symbol_pending_reserved_usd=symbol_pending,
            broker_buying_power_usd=broker_buying_power_usd,
            settled_cash_usd=settled_cash_usd,
            non_margin_buying_power_usd=non_margin_buying_power_usd,
            non_marginable_buying_power_usd=non_marginable_buying_power_usd,
        )

    async def snapshot(
        self,
        account_scope_id: AccountScope | str,
        symbol: str,
        *,
        broker_buying_power_usd: Decimal,
        settled_cash_usd: Decimal | None = None,
        non_margin_buying_power_usd: Decimal | None = None,
        non_marginable_buying_power_usd: Decimal | None = None,
    ) -> BudgetSnapshot:
        scope = _scope_id(account_scope_id)
        normalized_symbol = _symbol(symbol)
        # Validate broker values before acquiring the shared writer lock.
        _decimal(broker_buying_power_usd, "broker_buying_power_usd")
        if settled_cash_usd is not None:
            _decimal(settled_cash_usd, "settled_cash_usd")
        if non_margin_buying_power_usd is not None:
            _decimal(non_margin_buying_power_usd, "non_margin_buying_power_usd")
        if non_marginable_buying_power_usd is not None:
            _decimal(non_marginable_buying_power_usd, "non_marginable_buying_power_usd")
        async with self._lock_for(scope):
            state = await self._load_locked(scope, refresh=self._repository is not None)
            return self._snapshot_from_state(
                scope,
                normalized_symbol,
                state,
                broker_buying_power_usd=broker_buying_power_usd,
                settled_cash_usd=settled_cash_usd,
                non_margin_buying_power_usd=non_margin_buying_power_usd,
                non_marginable_buying_power_usd=non_marginable_buying_power_usd,
            )

    query = snapshot

    def _reserve_in_state(
        self,
        scope: str,
        state: _ScopeState,
        *,
        symbol: str,
        client_order_id: str,
        amount_usd: Decimal,
        occurred_at: datetime,
        broker_buying_power_usd: Decimal,
        settled_cash_usd: Decimal | None,
        non_margin_buying_power_usd: Decimal | None,
        non_marginable_buying_power_usd: Decimal | None,
    ) -> tuple[_ScopeState, BudgetReservation]:
        for current in state.reservations:
            if current.symbol == symbol and current.client_order_id == client_order_id:
                if current.initial_reserved_usd != amount_usd:
                    raise ValueError("client_order_id already has a different reservation amount")
                return state, current
            if current.symbol == symbol and current.cycle_open:
                raise ValueError(f"an entry cycle for {symbol} is already pending or open")
        snapshot = self._snapshot_from_state(
            scope,
            symbol,
            state,
            broker_buying_power_usd=broker_buying_power_usd,
            settled_cash_usd=settled_cash_usd,
            non_margin_buying_power_usd=non_margin_buying_power_usd,
            non_marginable_buying_power_usd=non_marginable_buying_power_usd,
        )
        if amount_usd > snapshot.remaining_symbol_usd:
            raise ValueError("reservation would exceed symbol_budget_usd")
        if amount_usd > snapshot.remaining_total_usd:
            raise ValueError("reservation would exceed effective total budget")
        reservation = BudgetReservation(
            account_scope_id=scope,
            symbol=symbol,
            client_order_id=client_order_id,
            initial_reserved_usd=amount_usd,
            pending_reserved_usd=amount_usd,
            committed_open_notional_usd=_ZERO,
            state="PENDING",
            created_at=occurred_at,
            updated_at=occurred_at,
        )
        return replace(state, reservations=state.reservations + (reservation,)), reservation

    async def reserve(
        self,
        account_scope_id: AccountScope | str,
        symbol: str,
        client_order_id: str,
        amount_usd: Decimal,
        *,
        broker_buying_power_usd: Decimal,
        occurred_at: datetime | None = None,
        settled_cash_usd: Decimal | None = None,
        non_margin_buying_power_usd: Decimal | None = None,
        non_marginable_buying_power_usd: Decimal | None = None,
    ) -> BudgetReservation:
        scope = _scope_id(account_scope_id)
        normalized_symbol = _symbol(symbol)
        order_id = _text(client_order_id, "client_order_id")
        amount = _decimal(amount_usd, "amount_usd", positive=True)
        broker_power = _decimal(broker_buying_power_usd, "broker_buying_power_usd")
        when = self._now() if occurred_at is None else _utc(occurred_at, "occurred_at")
        if settled_cash_usd is not None:
            _decimal(settled_cash_usd, "settled_cash_usd")
        if non_margin_buying_power_usd is not None:
            _decimal(non_margin_buying_power_usd, "non_margin_buying_power_usd")
        if non_marginable_buying_power_usd is not None:
            _decimal(non_marginable_buying_power_usd, "non_marginable_buying_power_usd")
        async with self._lock_for(scope):
            state = await self._load_locked(scope, refresh=self._repository is not None)
            updated, reservation = self._reserve_in_state(
                scope,
                state,
                symbol=normalized_symbol,
                client_order_id=order_id,
                amount_usd=amount,
                occurred_at=when,
                broker_buying_power_usd=broker_power,
                settled_cash_usd=settled_cash_usd,
                non_margin_buying_power_usd=non_margin_buying_power_usd,
                non_marginable_buying_power_usd=non_marginable_buying_power_usd,
            )
            if updated is not state:
                await self._save_locked(scope, updated)
            return reservation

    async def apply_fill(
        self,
        account_scope_id: AccountScope | str,
        symbol: str,
        client_order_id: str,
        fill_id: str,
        quantity: Decimal,
        price: Decimal,
        *,
        occurred_at: datetime | None = None,
        final: bool = False,
    ) -> BudgetReservation:
        scope = _scope_id(account_scope_id)
        normalized_symbol = _symbol(symbol)
        order_id = _text(client_order_id, "client_order_id")
        activity_id = _text(fill_id, "fill_id")
        fill_notional = _decimal(quantity, "quantity", positive=True) * _decimal(
            price, "price", positive=True
        )
        if not isinstance(final, bool):
            raise TypeError("final must be bool")
        when = self._now() if occurred_at is None else _utc(occurred_at, "occurred_at")
        async with self._lock_for(scope):
            state = await self._load_locked(scope, refresh=self._repository is not None)
            index, current = self._find(state, normalized_symbol, order_id)
            if activity_id in state.processed_activity_ids:
                return current
            if current.state == "CLOSED":
                raise ValueError("cannot apply a fill to a closed cycle")
            pending = max(_ZERO, current.pending_reserved_usd - fill_notional)
            if final:
                pending = _ZERO
            changed = replace(
                current,
                pending_reserved_usd=pending,
                committed_open_notional_usd=(
                    current.committed_open_notional_usd + fill_notional
                ),
                state="OPEN",
                updated_at=when,
            )
            reservations = list(state.reservations)
            reservations[index] = changed
            updated = _ScopeState(
                tuple(reservations), state.processed_activity_ids | {activity_id}
            )
            await self._save_locked(scope, updated)
            return changed

    async def apply_terminal_event(
        self,
        account_scope_id: AccountScope | str,
        symbol: str,
        client_order_id: str,
        activity_id: str,
        status: str,
        *,
        occurred_at: datetime | None = None,
    ) -> BudgetReservation:
        scope = _scope_id(account_scope_id)
        normalized_symbol = _symbol(symbol)
        order_id = _text(client_order_id, "client_order_id")
        event_id = _text(activity_id, "activity_id")
        normalized_status = _text(status, "status").upper()
        if normalized_status not in _TERMINAL_ORDER_STATUSES:
            raise ValueError("status must be CANCELED, REJECTED, or EXPIRED")
        when = self._now() if occurred_at is None else _utc(occurred_at, "occurred_at")
        async with self._lock_for(scope):
            state = await self._load_locked(scope, refresh=self._repository is not None)
            index, current = self._find(state, normalized_symbol, order_id)
            if event_id in state.processed_activity_ids:
                return current
            next_state = "OPEN" if current.committed_open_notional_usd > _ZERO else "CLOSED"
            changed = replace(
                current,
                pending_reserved_usd=_ZERO,
                state=next_state,
                updated_at=when,
            )
            reservations = list(state.reservations)
            reservations[index] = changed
            updated = _ScopeState(
                tuple(reservations), state.processed_activity_ids | {event_id}
            )
            await self._save_locked(scope, updated)
            return changed

    async def release_pending(
        self,
        account_scope_id: AccountScope | str,
        symbol: str,
        client_order_id: str,
        activity_id: str,
        *,
        occurred_at: datetime | None = None,
    ) -> BudgetReservation:
        return await self.apply_terminal_event(
            account_scope_id,
            symbol,
            client_order_id,
            activity_id,
            "CANCELED",
            occurred_at=occurred_at,
        )

    async def close_cycle(
        self,
        account_scope_id: AccountScope | str,
        symbol: str,
        client_order_id: str,
        activity_id: str,
        *,
        occurred_at: datetime | None = None,
    ) -> BudgetReservation:
        scope = _scope_id(account_scope_id)
        normalized_symbol = _symbol(symbol)
        order_id = _text(client_order_id, "client_order_id")
        event_id = _text(activity_id, "activity_id")
        when = self._now() if occurred_at is None else _utc(occurred_at, "occurred_at")
        async with self._lock_for(scope):
            state = await self._load_locked(scope, refresh=self._repository is not None)
            index, current = self._find(state, normalized_symbol, order_id)
            if event_id in state.processed_activity_ids:
                return current
            changed = replace(
                current,
                pending_reserved_usd=_ZERO,
                committed_open_notional_usd=_ZERO,
                state="CLOSED",
                updated_at=when,
            )
            reservations = list(state.reservations)
            reservations[index] = changed
            updated = _ScopeState(
                tuple(reservations), state.processed_activity_ids | {event_id}
            )
            await self._save_locked(scope, updated)
            return changed

    @staticmethod
    def _candidate_value(candidate: object, *names: str, default: object = None) -> object:
        for name in names:
            if isinstance(candidate, Mapping) and name in candidate:
                return candidate[name]
            if hasattr(candidate, name):
                return getattr(candidate, name)
        return default

    def _parse_candidate(
        self, candidate: object
    ) -> tuple[int, int, str, str, str, Decimal, object]:
        symbol = _symbol(self._candidate_value(candidate, "symbol"))
        strategy_id = _text(
            self._candidate_value(candidate, "strategy_id", "strategy_key"), "strategy_id"
        )
        client_order_id = _text(
            self._candidate_value(candidate, "client_order_id"), "client_order_id"
        )
        amount = _decimal(
            self._candidate_value(
                candidate, "amount_usd", "requested_notional_usd", "notional"
            ),
            "amount_usd",
            positive=True,
        )
        symbol_priority = _priority(
            self._candidate_value(candidate, "symbol_priority", default=0),
            "symbol_priority",
        )
        strategy_priority = _priority(
            self._candidate_value(candidate, "strategy_priority", default=0),
            "strategy_priority",
        )
        return (
            symbol_priority,
            strategy_priority,
            symbol,
            strategy_id,
            client_order_id,
            amount,
            candidate,
        )

    async def reserve_many(
        self,
        account_scope_id: AccountScope | str,
        candidates: Iterable[object],
        *,
        broker_buying_power_usd: Decimal,
        occurred_at: datetime | None = None,
        settled_cash_usd: Decimal | None = None,
        non_margin_buying_power_usd: Decimal | None = None,
        non_marginable_buying_power_usd: Decimal | None = None,
    ) -> dict[str, BudgetReservation | None]:
        """Reserve eligible signals in deterministic ascending priority order.

        Lower integer priority values run first. Equal priorities are ordered by
        symbol and then strategy ID; client order ID is a final deterministic
        disambiguator when the explicit tie-breakers are identical.
        """
        scope = _scope_id(account_scope_id)
        parsed = [self._parse_candidate(candidate) for candidate in candidates]
        parsed.sort(key=lambda item: item[:5])
        broker_power = _decimal(broker_buying_power_usd, "broker_buying_power_usd")
        when = self._now() if occurred_at is None else _utc(occurred_at, "occurred_at")
        if settled_cash_usd is not None:
            _decimal(settled_cash_usd, "settled_cash_usd")
        if non_margin_buying_power_usd is not None:
            _decimal(non_margin_buying_power_usd, "non_margin_buying_power_usd")
        if non_marginable_buying_power_usd is not None:
            _decimal(non_marginable_buying_power_usd, "non_marginable_buying_power_usd")
        results: dict[str, BudgetReservation | None] = {}
        async with self._lock_for(scope):
            original = await self._load_locked(scope, refresh=self._repository is not None)
            state = original
            for _, _, symbol, _, client_order_id, amount, _ in parsed:
                try:
                    state, reservation = self._reserve_in_state(
                        scope,
                        state,
                        symbol=symbol,
                        client_order_id=client_order_id,
                        amount_usd=amount,
                        occurred_at=when,
                        broker_buying_power_usd=broker_power,
                        settled_cash_usd=settled_cash_usd,
                        non_margin_buying_power_usd=non_margin_buying_power_usd,
                        non_marginable_buying_power_usd=non_marginable_buying_power_usd,
                    )
                except ValueError:
                    results[client_order_id] = None
                else:
                    results[client_order_id] = reservation
            if state is not original:
                await self._save_locked(scope, state)
        return results

    process_eligible = reserve_many


class ReservationService(ReservationLedger):
    """Application-facing name for :class:`ReservationLedger`."""
