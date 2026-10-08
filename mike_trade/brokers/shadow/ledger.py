"""Append-only, idempotent SQLite ledger for SHADOW accounts.

The ledger stores every trade-affecting shadow fact as one ``domain_events`` row
through the project's single-writer ``ScopedWriter`` and ``DomainEventJournal``,
whose triggers already forbid UPDATE and DELETE.  This module exposes no
mutation other than :meth:`ShadowLedger.append`.

Design choices:
1. One ledger is bound to exactly one SHADOW ``AccountScope`` (account_id plus
   scenario_id).  Binding a non-SHADOW scope, or a writer whose database is not
   SHADOW, raises ``ModeIsolationError``; so does any row, read or written,
   whose recorded mode/account/scenario differs from the bound scope.
2. Each row is an envelope: mode, account_id, scenario_id, simulator version,
   assumptions, feed provenance, the ``estimated=True`` / ``broker_confirmed=False``
   labels, and ``data``.  Money and quantities are normalised to Decimal text at
   event construction, so a live append and a replay see identical data.
3. Idempotency is by ``event_id``: re-appending identical content is a no-op and
   reports ``False``; reusing an id for different content raises (journal rule).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol

from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.errors import ModeIsolationError, PersistenceError
from mike_trade.persistence.journal import DomainEventJournal, NewEvent
from mike_trade.persistence.repositories import AccountScopeRepository
from mike_trade.persistence.schema import decimal_to_db, to_db_timestamp
from mike_trade.persistence.transaction import ScopedWriter

__all__ = [
    "ShadowLedger",
    "ShadowLedgerEvent",
    "SIMULATOR_VERSION",
    "EVENT_KINDS",
    "ACCOUNT_CREATED",
    "CASH_ADJUSTED",
    "ORDER_SUBMITTED",
    "ORDER_REPLACED",
    "ORDER_CANCELED",
    "FILL_RECORDED",
    "LOCKOUT_CLEARED",
]

SIMULATOR_VERSION = "shadow-sim/1"
STREAM_TYPE = "shadow_ledger"

ACCOUNT_CREATED = "ShadowAccountCreated"
CASH_ADJUSTED = "ShadowCashAdjusted"
ORDER_SUBMITTED = "ShadowOrderSubmitted"
ORDER_REPLACED = "ShadowOrderReplaced"
ORDER_CANCELED = "ShadowOrderCanceled"
FILL_RECORDED = "ShadowFillRecorded"
LOCKOUT_CLEARED = "ShadowLockoutCleared"
EVENT_KINDS = frozenset(
    {
        ACCOUNT_CREATED,
        CASH_ADJUSTED,
        ORDER_SUBMITTED,
        ORDER_REPLACED,
        ORDER_CANCELED,
        FILL_RECORDED,
        LOCKOUT_CLEARED,
    }
)


class _Clock(Protocol):
    def now(self) -> datetime: ...


def _read_clock(clock: _Clock | Callable[[], datetime]) -> datetime:
    now_method = getattr(clock, "now", None)
    value = now_method() if callable(now_method) else clock()  # type: ignore[operator]
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PersistenceError("clock must return an aware datetime")
    return value.astimezone(UTC)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _normalize(value: Any, name: str) -> Any:
    """Convert to JSON-safe values; Decimals become text and floats are rejected."""
    if isinstance(value, Enum):
        return _normalize(value.value, name)
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        raise TypeError(f"{name}: float values are not permitted; use Decimal")
    if isinstance(value, Decimal):
        return decimal_to_db(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError(f"{name}: datetimes must be timezone-aware UTC")
        return to_db_timestamp(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{name}: mapping keys must be strings")
            result[key] = _normalize(item, f"{name}.{key}")
        return result
    if isinstance(value, list | tuple):
        return [_normalize(item, name) for item in value]
    raise TypeError(f"{name}: unsupported value type {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class ShadowLedgerEvent:
    """One immutable, estimated (never broker-confirmed) shadow fact."""

    event_id: str
    kind: str
    scope: AccountScope
    occurred_at: datetime
    data: Mapping[str, Any]
    simulator_version: str = SIMULATOR_VERSION
    assumptions: Mapping[str, str] = field(default_factory=dict)
    feed: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _text(self.event_id, "event_id"))
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unknown shadow ledger event kind: {self.kind!r}")
        if not isinstance(self.scope, AccountScope):
            raise TypeError("scope must be an AccountScope")
        if (
            not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("occurred_at must be timezone-aware UTC")
        object.__setattr__(
            self, "simulator_version", _text(self.simulator_version, "simulator_version")
        )
        if self.feed is not None:
            object.__setattr__(self, "feed", _text(self.feed, "feed"))
        assumptions = _normalize(dict(self.assumptions), "assumptions")
        if not all(isinstance(value, str) for value in assumptions.values()):
            raise TypeError("assumptions must map strings to strings")
        data = _normalize(dict(self.data), "data")
        object.__setattr__(self, "assumptions", MappingProxyType(assumptions))
        object.__setattr__(self, "data", MappingProxyType(data))

    @property
    def estimated(self) -> bool:
        return True

    @property
    def broker_confirmed(self) -> bool:
        return False


class ShadowLedger:
    """Append-only shadow ledger keyed by one SHADOW account_id/scenario_id."""

    def __init__(
        self,
        writer: ScopedWriter,
        scope: AccountScope,
        *,
        clock: _Clock | Callable[[], datetime],
    ) -> None:
        if not isinstance(scope, AccountScope):
            raise TypeError("scope must be an AccountScope")
        if scope.environment is not AccountEnvironment.SHADOW:
            raise ModeIsolationError(
                f"shadow ledger refuses {scope.environment.value} scope {scope.scope_id!r}",
                code="ledger_mode_mismatch",
            )
        if writer.environment is not AccountEnvironment.SHADOW:
            raise ModeIsolationError(
                f"shadow ledger refuses {writer.environment.value} database",
                code="ledger_mode_mismatch",
            )
        if writer.account_scope_id != scope.scope_id:
            raise ModeIsolationError(
                f"writer scope {writer.account_scope_id!r} differs from ledger scope "
                f"{scope.scope_id!r}",
                code="ledger_scope_mismatch",
            )
        self._writer = writer
        self._scope = scope
        self._clock = clock
        self._journal = DomainEventJournal(writer.connection, clock=clock)
        AccountScopeRepository(writer, clock).register_scope(scope)

    @property
    def scope(self) -> AccountScope:
        return self._scope

    def now(self) -> datetime:
        return _read_clock(self._clock)

    def contains(self, event_id: str) -> bool:
        """True if this ledger's stream already holds ``event_id``."""
        row = self._writer.connection.execute(
            "SELECT 1 FROM domain_events WHERE account_scope_id = ? AND stream_type = ? "
            "AND stream_id = ? AND event_id = ?",
            (self._scope.scope_id, STREAM_TYPE, self._scope.scope_id, event_id),
        ).fetchone()
        return row is not None

    def append(self, event: ShadowLedgerEvent) -> bool:
        """Durably append ``event``; returns False if it was an identical duplicate."""
        if not isinstance(event, ShadowLedgerEvent):
            raise TypeError("event must be a ShadowLedgerEvent")
        if event.scope != self._scope:
            raise ModeIsolationError(
                f"event scope {event.scope.scope_id!r} differs from ledger scope "
                f"{self._scope.scope_id!r}",
                code="ledger_scope_mismatch",
            )
        scope_id = self._scope.scope_id
        new_event = NewEvent(
            account_scope_id=scope_id,
            stream_type=STREAM_TYPE,
            stream_id=scope_id,
            event_type=event.kind,
            payload=self._envelope(event),
            occurred_at=event.occurred_at,
            event_id=event.event_id,
        )
        with self._writer.unit_of_work():
            result = self._journal.append_in_transaction([new_event])
        return not result.duplicates[0]

    def read_events(self) -> tuple[ShadowLedgerEvent, ...]:
        """Return this scope's events in append order, refusing foreign-mode rows."""
        scope_id = self._scope.scope_id
        problems = self._journal.verify_chain(scope_id)
        if problems:
            raise PersistenceError(
                f"shadow ledger integrity check failed: {problems[0]}",
                code="ledger_integrity",
            )
        events: list[ShadowLedgerEvent] = []
        for recorded in self._journal.read_stream(scope_id, STREAM_TYPE, scope_id):
            payload = recorded.payload
            self._check_envelope(payload, recorded.event_id)
            events.append(
                ShadowLedgerEvent(
                    event_id=recorded.event_id,
                    kind=recorded.event_type,
                    scope=self._scope,
                    occurred_at=recorded.occurred_at,
                    data=payload["data"],
                    simulator_version=payload["simulator_version"],
                    assumptions=payload["assumptions"],
                    feed=payload["feed"],
                )
            )
        return tuple(events)

    def _envelope(self, event: ShadowLedgerEvent) -> dict[str, Any]:
        return {
            "mode": self._scope.environment.value,
            "account_id": self._scope.account_id,
            "scenario_id": self._scope.scenario,
            "simulator_version": event.simulator_version,
            "assumptions": dict(event.assumptions),
            "feed": event.feed,
            "estimated": True,
            "broker_confirmed": False,
            "data": dict(event.data),
        }

    def _check_envelope(self, payload: Any, event_id: str) -> None:
        expected = (
            self._scope.environment.value,
            self._scope.account_id,
            self._scope.scenario,
        )
        found = (
            payload.get("mode"),
            payload.get("account_id"),
            payload.get("scenario_id"),
        )
        if found != expected:
            raise ModeIsolationError(
                f"ledger row {event_id!r} belongs to {found!r}, not {expected!r}",
                code="ledger_row_scope_mismatch",
            )
        if payload.get("estimated") is not True or payload.get("broker_confirmed") is not False:
            raise PersistenceError(
                f"ledger row {event_id!r} lacks estimated/broker_confirmed labels",
                code="ledger_row_unlabeled",
            )
