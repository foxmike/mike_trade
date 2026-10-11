"""Durable, restart-safe ENTRY eligibility controls.

Everything here gates *new entries only*.  The module deliberately exposes no
API that can block or delay fill ingestion, protective stop placement/updates,
reconciliation, manual close, or risk exits: every ``record_*`` method accepts
events unconditionally (and idempotently), and only
:meth:`EntryEligibilityEvaluator.evaluate` produces a block decision, which the
entry path alone consults.

Design choices:
1. State is persisted as plain JSON-compatible documents, one per account/scenario
   scope and component section, through :class:`EntryEligibilityRepository`
   (same load/save-or-await convention as the reservation ledger).  Decimals are
   stored as strings and instants as UTC ISO-8601 strings.
2. The trading day is the NY exchange session.  Instants on a non-trading day
   belong to the previous session, so counts reset only at the next session open
   day rather than at midnight or on a weekend.
3. A cycle closed with ``fee_status`` other than ``"final"`` still updates the
   streak from the computed net, but the result is recorded as provisional and
   the decision reports ``provisional=True`` when a lockout rests on it.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol, TypeVar
from weakref import WeakKeyDictionary

from mike_trade.domain.accounts import AccountScope
from mike_trade.domain.trading import Fill, OrderSide, PositionCycle, PositionCycleState
from mike_trade.scheduling.calendar import NEW_YORK, is_trading_day, previous_session

__all__ = [
    "EntryEligibilityEvaluator",
    "EligibilityDecision",
    "EntryBlockReason",
    "EntryCycleCounter",
    "LosingStreakTracker",
    "LockoutResetMode",
    "PostSaleCooldown",
    "EntryEligibilityRepository",
]

_ZERO = Decimal("0")
_ENVIRONMENTS = frozenset({"backtest", "shadow", "paper", "live"})
_FEE_STATUSES = frozenset({"final", "estimated", "unavailable"})
_T = TypeVar("_T")

_SECTION_CYCLES = "entry_cycles"
_SECTION_STREAKS = "losing_streaks"
_SECTION_COOLDOWNS = "post_sale_cooldowns"


class EntryBlockReason(Enum):
    """Explicit reasons an entry is not currently eligible."""

    MAX_ENTRY_CYCLES_REACHED = "MAX_ENTRY_CYCLES_REACHED"
    LOSING_STREAK_LOCKOUT = "LOSING_STREAK_LOCKOUT"
    POST_SALE_COOLDOWN = "POST_SALE_COOLDOWN"
    DAILY_LOSS_LIMIT_REACHED = "DAILY_LOSS_LIMIT_REACHED"
    MAX_OPEN_POSITIONS_REACHED = "MAX_OPEN_POSITIONS_REACHED"


class LockoutResetMode(Enum):
    NEXT_SESSION = "next_session"
    MANUAL = "manual"


class EntryEligibilityRepository(Protocol):
    """Persistence port: atomic replace of one section document per scope.

    Documents are JSON-compatible mappings.  ``save`` must be atomic through the
    application's single writer; either method may be sync or async.  Sections
    are ``entry_cycles``, ``losing_streaks`` and ``post_sale_cooldowns``.
    """

    def load(
        self, account_scope_id: str, section: str
    ) -> Mapping[str, Any] | None | Awaitable[Mapping[str, Any] | None]: ...

    def save(
        self, account_scope_id: str, section: str, document: Mapping[str, Any]
    ) -> None | Awaitable[None]: ...


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    """Result of an entry-eligibility evaluation; carries every blocking reason."""

    account_scope_id: str
    symbol: str
    evaluated_at: datetime
    session_date: date
    reasons: tuple[EntryBlockReason, ...] = ()
    details: tuple[str, ...] = ()
    cooldown_until: datetime | None = None
    provisional: bool = False

    @property
    def allowed(self) -> bool:
        return not self.reasons

    @property
    def blocked(self) -> bool:
        return bool(self.reasons)


# --------------------------------------------------------------------------- helpers


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


def _decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def _positive_int(value: object, name: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int")
    if value < 1:
        raise ValueError(f"{name} must be >= 1")
    return value


def _now(clock: Callable[[], datetime] | Any | None, override: datetime | None) -> datetime:
    if override is not None:
        return _utc(override, "now")
    if clock is None:
        return datetime.now(UTC)
    now_method = getattr(clock, "now", None)
    return _utc(now_method() if callable(now_method) else clock(), "clock")


def _session_date(at: datetime) -> date:
    """NY exchange session an instant belongs to (previous session if market closed all day)."""
    local_day = at.astimezone(NEW_YORK).date()
    if is_trading_day(local_day):
        return local_day
    return previous_session(local_day).session_date


def _strategy_text(value: str | None) -> str | None:
    return None if value is None else _text(value, "strategy_key")


class _SectionStore:
    """Per-scope, per-section document access serialized by an async lock."""

    _locks: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[str, str], asyncio.Lock]] = (
        WeakKeyDictionary()
    )

    def __init__(self, section: str, repository: EntryEligibilityRepository | None) -> None:
        self._section = section
        self._repository = repository
        self._memory: dict[str, dict[str, Any]] = {}

    def _lock(self, scope: str) -> asyncio.Lock:
        loop_locks = self._locks.setdefault(asyncio.get_running_loop(), {})
        return loop_locks.setdefault((scope, self._section), asyncio.Lock())

    async def _load(self, scope: str) -> dict[str, Any]:
        if self._repository is None:
            return copy.deepcopy(self._memory.get(scope, {}))
        loaded = self._repository.load(scope, self._section)
        if inspect.isawaitable(loaded):
            loaded = await loaded
        if loaded is None:
            return {}
        if not isinstance(loaded, Mapping):
            raise TypeError("repository document must be a mapping")
        return copy.deepcopy(dict(loaded))

    async def read(self, scope: str) -> dict[str, Any]:
        async with self._lock(scope):
            return await self._load(scope)

    async def update(self, scope: str, mutate: Callable[[dict[str, Any]], _T]) -> _T:
        async with self._lock(scope):
            original = await self._load(scope)
            document = copy.deepcopy(original)
            result = mutate(document)
            if document != original:
                if self._repository is not None:
                    saved = self._repository.save(scope, self._section, document)
                    if inspect.isawaitable(saved):
                        await saved
                self._memory[scope] = document
            return result


# ------------------------------------------------------------------ entry cycle limit


class EntryCycleCounter:
    """Counts filled entry cycles per account/scenario + symbol per NY session.

    The first positive buy fill of an entry consumes one slot; later fills of the
    same entry (same ``entry_id``, default the broker order id) consume nothing,
    even across a session boundary.  Entries that never fill consume nothing
    because they never reach :meth:`record_entry_fill`.
    """

    def __init__(
        self,
        max_entry_cycles_per_day: int | None,
        repository: EntryEligibilityRepository | None = None,
        clock: Callable[[], datetime] | Any | None = None,
    ) -> None:
        self.max_entry_cycles_per_day = _positive_int(
            max_entry_cycles_per_day, "max_entry_cycles_per_day", optional=True
        )
        self._store = _SectionStore(_SECTION_CYCLES, repository)
        self._clock = clock

    async def record_entry_fill(self, fill: Fill, *, entry_id: str | None = None) -> bool:
        """Record a fill; return True only if it consumed a new cycle slot.

        Never raises because of any limit; ingestion is unconditional.
        """
        if not isinstance(fill, Fill):
            raise TypeError("fill must be Fill")
        # BUY and SELL_SHORT both open a cycle: a short entry consumes the same daily slot,
        # so the per-symbol entry cap cannot be bypassed by shorting.
        if not fill.side.is_entry or fill.quantity <= _ZERO:
            return False
        scope = _scope_id(fill.account_scope_id)
        key = _text(entry_id, "entry_id") if entry_id is not None else fill.broker_order_id
        session = _session_date(fill.filled_at).isoformat()

        def mutate(document: dict[str, Any]) -> bool:
            entries = document.setdefault("entries", {}).setdefault(fill.symbol, {})
            if key in entries:
                return False
            entries[key] = session
            return True

        return await self._store.update(scope, mutate)

    async def count(
        self,
        account_scope_id: AccountScope | str,
        symbol: str,
        *,
        session_date: date | None = None,
        now: datetime | None = None,
    ) -> int:
        scope = _scope_id(account_scope_id)
        day = (session_date or _session_date(_now(self._clock, now))).isoformat()
        document = await self._store.read(scope)
        entries = document.get("entries", {}).get(_symbol(symbol), {})
        return sum(1 for session in entries.values() if session == day)

    async def blocked(
        self, account_scope_id: AccountScope | str, symbol: str, *, now: datetime
    ) -> str | None:
        if self.max_entry_cycles_per_day is None:
            return None
        used = await self.count(account_scope_id, symbol, now=now)
        if used >= self.max_entry_cycles_per_day:
            return f"{used} of {self.max_entry_cycles_per_day} entry cycles used this session"
        return None


# ------------------------------------------------------------- losing streak lockout


class LosingStreakTracker:
    """Consecutive losing-cycle streaks, lockouts, and audited manual resets.

    Streak keys are ``symbol`` or, with ``scope_by_strategy``, ``symbol`` +
    originating BUY strategy.  The tracker also keeps per-cycle net P&L (idempotent
    by ``cycle_id``) so the account daily loss can be derived durably.
    """

    def __init__(
        self,
        threshold: int | None,
        reset_mode: LockoutResetMode = LockoutResetMode.NEXT_SESSION,
        *,
        scope_by_strategy: bool = False,
        repository: EntryEligibilityRepository | None = None,
        clock: Callable[[], datetime] | Any | None = None,
    ) -> None:
        self.threshold = _positive_int(threshold, "threshold", optional=True)
        if not isinstance(reset_mode, LockoutResetMode):
            raise TypeError("reset_mode must be LockoutResetMode")
        if not isinstance(scope_by_strategy, bool):
            raise TypeError("scope_by_strategy must be bool")
        self.reset_mode = reset_mode
        self.scope_by_strategy = scope_by_strategy
        self._store = _SectionStore(_SECTION_STREAKS, repository)
        self._clock = clock

    def _key(self, symbol: str, strategy_key: str | None) -> str:
        if not self.scope_by_strategy:
            return symbol
        return f"{symbol}|{strategy_key if strategy_key is not None else ''}"

    def _expired(self, entry: Mapping[str, Any], session: date) -> bool:
        return (
            entry.get("locked", False)
            and self.reset_mode is LockoutResetMode.NEXT_SESSION
            and session > date.fromisoformat(entry["locked_session"])
        )

    @staticmethod
    def _clear(entry: dict[str, Any]) -> None:
        entry.update(streak=0, locked=False, locked_session=None, provisional=False)

    async def record_closed_cycle(
        self, cycle: PositionCycle, *, fee_status: str = "final"
    ) -> bool:
        """Apply a fully closed cycle; return False if it was already applied.

        ``fee_status`` other than ``"final"`` marks the result provisional.
        Never blocked by any lockout: exits and ingestion are unconditional.
        """
        if not isinstance(cycle, PositionCycle):
            raise TypeError("cycle must be PositionCycle")
        if fee_status not in _FEE_STATUSES:
            raise ValueError("fee_status must be final, estimated, or unavailable")
        if (
            cycle.state is not PositionCycleState.CLOSED
            or cycle.closed_at is None
            or cycle.remaining_quantity != _ZERO
        ):
            raise ValueError("cycle must be fully closed")
        scope = _scope_id(cycle.account_scope_id)
        net = cycle.realized_pnl
        session = _session_date(cycle.closed_at)
        key = self._key(cycle.symbol, cycle.entry_strategy_key)
        closed_at = cycle.closed_at
        provisional = fee_status != "final"

        def mutate(document: dict[str, Any]) -> bool:
            cycles = document.setdefault("cycles", {})
            if cycle.cycle_id in cycles:
                return False
            cycles[cycle.cycle_id] = {
                "net_pnl": str(net),
                "fee_status": fee_status,
                "provisional": provisional,
                "session_date": session.isoformat(),
                "key": key,
                "closed_at": closed_at.isoformat(),
            }
            entry = document.setdefault("keys", {}).setdefault(
                key,
                {"streak": 0, "locked": False, "locked_session": None, "provisional": False},
            )
            if self._expired(entry, session):
                self._clear(entry)
            if net < _ZERO:
                entry["streak"] += 1
                entry["provisional"] = entry["provisional"] or provisional
            elif net > _ZERO:
                entry["streak"] = 0
                entry["provisional"] = False
            if (
                self.threshold is not None
                and entry["streak"] >= self.threshold
                and not entry["locked"]
            ):
                entry["locked"] = True
                entry["locked_session"] = session.isoformat()
            return True

        return await self._store.update(scope, mutate)

    async def streak(
        self,
        account_scope_id: AccountScope | str,
        symbol: str,
        strategy_key: str | None = None,
        *,
        now: datetime | None = None,
    ) -> int:
        entry = await self._entry(account_scope_id, symbol, strategy_key, now)
        return int(entry["streak"])

    async def _entry(
        self,
        account_scope_id: AccountScope | str,
        symbol: str,
        strategy_key: str | None,
        now: datetime | None,
    ) -> dict[str, Any]:
        scope = _scope_id(account_scope_id)
        key = self._key(_symbol(symbol), _strategy_text(strategy_key))
        document = await self._store.read(scope)
        entry = dict(
            document.get("keys", {}).get(
                key, {"streak": 0, "locked": False, "locked_session": None, "provisional": False}
            )
        )
        if self._expired(entry, _session_date(_now(self._clock, now))):
            self._clear(entry)
        return entry

    async def blocked(
        self,
        account_scope_id: AccountScope | str,
        symbol: str,
        strategy_key: str | None = None,
        *,
        now: datetime,
    ) -> tuple[str, bool] | None:
        """Return ``(detail, provisional)`` while locked out, else None."""
        entry = await self._entry(account_scope_id, symbol, strategy_key, now)
        if self.threshold is None or not entry["locked"]:
            return None
        detail = (
            f"{entry['streak']} consecutive losing cycles (threshold {self.threshold}); "
            f"reset mode {self.reset_mode.value}"
        )
        if entry["provisional"]:
            detail += "; based on provisional (estimated/unavailable-fee) results"
        return detail, bool(entry["provisional"])

    async def daily_realized_pnl(
        self,
        account_scope_id: AccountScope | str,
        *,
        session_date: date | None = None,
        now: datetime | None = None,
    ) -> Decimal:
        """Net realized P&L (fees included) of cycles closed in the NY session."""
        scope = _scope_id(account_scope_id)
        day = (session_date or _session_date(_now(self._clock, now))).isoformat()
        document = await self._store.read(scope)
        return sum(
            (
                Decimal(item["net_pnl"])
                for item in document.get("cycles", {}).values()
                if item["session_date"] == day
            ),
            start=_ZERO,
        )

    async def reset_lockout(
        self,
        account_scope_id: AccountScope | str,
        symbol: str,
        *,
        reset_id: str,
        operator: str,
        reason: str,
        strategy_key: str | None = None,
        now: datetime | None = None,
    ) -> Mapping[str, Any]:
        """Audited operator reset; appends an immutable event and clears the streak.

        Idempotent on ``reset_id``: a repeat returns the original event unchanged.
        """
        scope = _scope_id(account_scope_id)
        normalized = _symbol(symbol)
        strategy = _strategy_text(strategy_key)
        event_id = _text(reset_id, "reset_id")
        who = _text(operator, "operator")
        why = _text(reason, "reason")
        at = _now(self._clock, now)
        key = self._key(normalized, strategy)

        def mutate(document: dict[str, Any]) -> Mapping[str, Any]:
            events = document.setdefault("reset_events", [])
            for existing in events:
                if existing["reset_id"] == event_id:
                    return copy.deepcopy(existing)
            entry = document.setdefault("keys", {}).setdefault(
                key,
                {"streak": 0, "locked": False, "locked_session": None, "provisional": False},
            )
            event = {
                "reset_id": event_id,
                "account_scope_id": scope,
                "symbol": normalized,
                "strategy_key": strategy,
                "key": key,
                "operator": who,
                "reason": why,
                "reset_at": at.isoformat(),
                "previous_streak": entry["streak"],
                "was_locked": entry["locked"],
            }
            events.append(event)
            self._clear(entry)
            return copy.deepcopy(event)

        return await self._store.update(scope, mutate)

    async def reset_events(
        self, account_scope_id: AccountScope | str
    ) -> tuple[Mapping[str, Any], ...]:
        document = await self._store.read(_scope_id(account_scope_id))
        return tuple(copy.deepcopy(document.get("reset_events", [])))


# ------------------------------------------------------------------- post-sale cooldown


class PostSaleCooldown:
    """Blocks re-entry for N seconds after the final closing fill of any exit."""

    def __init__(
        self,
        post_sale_cooldown_seconds: int,
        repository: EntryEligibilityRepository | None = None,
        clock: Callable[[], datetime] | Any | None = None,
    ) -> None:
        if isinstance(post_sale_cooldown_seconds, bool) or not isinstance(
            post_sale_cooldown_seconds, int
        ):
            raise TypeError("post_sale_cooldown_seconds must be int")
        if post_sale_cooldown_seconds < 0:
            raise ValueError("post_sale_cooldown_seconds must be >= 0")
        self.post_sale_cooldown_seconds = post_sale_cooldown_seconds
        self._store = _SectionStore(_SECTION_COOLDOWNS, repository)
        self._clock = clock

    async def record_closed_cycle(self, cycle: PositionCycle) -> bool:
        """Record the final closing fill time of any exit (strategy, stop, manual, risk)."""
        if not isinstance(cycle, PositionCycle):
            raise TypeError("cycle must be PositionCycle")
        if cycle.state is not PositionCycleState.CLOSED or cycle.closed_at is None:
            raise ValueError("cycle must be fully closed")
        return await self.record_final_exit(
            cycle.account_scope_id, cycle.symbol, cycle.cycle_id, cycle.closed_at
        )

    async def record_final_exit(
        self,
        account_scope_id: AccountScope | str,
        symbol: str,
        exit_id: str,
        closed_at: datetime,
    ) -> bool:
        """Persist the latest closing time; True if it advanced the stored value."""
        scope = _scope_id(account_scope_id)
        normalized = _symbol(symbol)
        identifier = _text(exit_id, "exit_id")
        at = _utc(closed_at, "closed_at")

        def mutate(document: dict[str, Any]) -> bool:
            last = document.setdefault("last_exit", {})
            current = last.get(normalized)
            if current is not None and (
                current["exit_id"] == identifier or datetime.fromisoformat(current["at"]) >= at
            ):
                return False
            last[normalized] = {"exit_id": identifier, "at": at.isoformat()}
            return True

        return await self._store.update(scope, mutate)

    async def cooldown_until(
        self, account_scope_id: AccountScope | str, symbol: str
    ) -> datetime | None:
        document = await self._store.read(_scope_id(account_scope_id))
        current = document.get("last_exit", {}).get(_symbol(symbol))
        if current is None:
            return None
        return datetime.fromisoformat(current["at"]) + timedelta(
            seconds=self.post_sale_cooldown_seconds
        )

    async def blocked(
        self, account_scope_id: AccountScope | str, symbol: str, *, now: datetime
    ) -> datetime | None:
        """Return the cooldown deadline while it is still in the future."""
        if self.post_sale_cooldown_seconds == 0:
            return None
        until = await self.cooldown_until(account_scope_id, symbol)
        if until is not None and _utc(now, "now") < until:
            return until
        return None


# ---------------------------------------------------------------------------- evaluator


class EntryEligibilityEvaluator:
    """Combines all entry blockers into a single decision listing every reason.

    The evaluator is read-only and is consulted by the entry path only.  It has
    no hook into fills, protective stops, reconciliation, manual close, or risk
    exits.
    """

    def __init__(
        self,
        cycles: EntryCycleCounter,
        streaks: LosingStreakTracker,
        cooldown: PostSaleCooldown,
        *,
        max_open_positions: int | None = None,
        daily_loss_limit: Decimal | None = None,
        clock: Callable[[], datetime] | Any | None = None,
    ) -> None:
        self._cycles = cycles
        self._streaks = streaks
        self._cooldown = cooldown
        self.max_open_positions = _positive_int(
            max_open_positions, "max_open_positions", optional=True
        )
        if daily_loss_limit is not None and _decimal(daily_loss_limit, "daily_loss_limit") <= _ZERO:
            raise ValueError("daily_loss_limit must be > 0")
        self.daily_loss_limit = daily_loss_limit
        self._clock = clock

    async def evaluate(
        self,
        account_scope_id: AccountScope | str,
        symbol: str,
        *,
        strategy_key: str | None = None,
        open_position_count: int | None = None,
        now: datetime | None = None,
    ) -> EligibilityDecision:
        """``open_position_count`` is the account/scenario's managed open + pending count."""
        scope = _scope_id(account_scope_id)
        normalized = _symbol(symbol)
        strategy = _strategy_text(strategy_key)
        at = _now(self._clock, now)
        open_count = 0
        if self.max_open_positions is not None:
            if isinstance(open_position_count, bool) or not isinstance(open_position_count, int):
                raise TypeError("open_position_count is required and must be int")
            if open_position_count < 0:
                raise ValueError("open_position_count must be >= 0")
            open_count = open_position_count

        reasons: list[EntryBlockReason] = []
        details: list[str] = []
        provisional = False
        cooldown_until: datetime | None = None

        cycle_detail = await self._cycles.blocked(scope, normalized, now=at)
        if cycle_detail is not None:
            reasons.append(EntryBlockReason.MAX_ENTRY_CYCLES_REACHED)
            details.append(cycle_detail)

        lockout = await self._streaks.blocked(scope, normalized, strategy, now=at)
        if lockout is not None:
            reasons.append(EntryBlockReason.LOSING_STREAK_LOCKOUT)
            details.append(lockout[0])
            provisional = lockout[1]

        cooldown_until = await self._cooldown.blocked(scope, normalized, now=at)
        if cooldown_until is not None:
            reasons.append(EntryBlockReason.POST_SALE_COOLDOWN)
            details.append(f"cooldown until {cooldown_until.isoformat()}")

        if self.daily_loss_limit is not None:
            realized = await self._streaks.daily_realized_pnl(scope, now=at)
            if realized <= -self.daily_loss_limit:
                reasons.append(EntryBlockReason.DAILY_LOSS_LIMIT_REACHED)
                details.append(
                    f"realized P&L {realized} breaches daily loss limit {self.daily_loss_limit}"
                )

        if self.max_open_positions is not None and open_count >= self.max_open_positions:
            reasons.append(EntryBlockReason.MAX_OPEN_POSITIONS_REACHED)
            details.append(
                f"{open_count} of {self.max_open_positions} open positions in use"
            )

        return EligibilityDecision(
            account_scope_id=scope,
            symbol=normalized,
            evaluated_at=at,
            session_date=_session_date(at),
            reasons=tuple(reasons),
            details=tuple(details),
            cooldown_until=cooldown_until,
            provisional=provisional,
        )
