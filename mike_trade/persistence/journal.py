"""Atomic append-only persistence for domain events.

The journal deliberately depends on the documented ``domain_events`` table
contract rather than on the schema implementation.  Its three design choices
not fixed by that contract are:

1. ``expected_versions`` uses ``(stream_type, stream_id)`` keys and describes
   the committed stream head before this append (zero means a new stream).
2. Reusing an event ID for byte-for-byte equivalent event content is treated
   as a duplicate; reusing it for any different content is an error.
3. Payload containers may be mappings, lists, and tuples, and mapping keys must
   be strings so canonical ordering cannot depend on Python key coercion.

No row mutation is performed here.  Integrity verification reports evidence
of external tampering but never attempts to repair history.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4

from mike_trade.domain.errors import PersistenceError

__all__ = [
    "NewEvent",
    "RecordedEvent",
    "AppendResult",
    "canonical_payload_json",
    "DomainEventJournal",
    "commit_then_effect",
    "JournalWriteError",
    "JournalPayloadError",
    "StreamVersionConflictError",
    "IdempotencyConflictError",
    "DuplicateEventIdError",
    "CrossScopeJournalError",
]


class JournalWriteError(PersistenceError):
    """Base error for a journal operation that could not be completed."""


class JournalPayloadError(JournalWriteError):
    """Raised when an event payload cannot be represented canonically."""


class StreamVersionConflictError(JournalWriteError):
    """Raised when a stream's committed head differs from the expected head."""

    def __init__(
        self,
        account_scope_id: str,
        stream_type: str,
        stream_id: str,
        expected_version: int,
        actual_version: int,
    ) -> None:
        self.account_scope_id = account_scope_id
        self.stream_type = stream_type
        self.stream_id = stream_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"stream version conflict for {stream_type}/{stream_id}: "
            f"expected {expected_version}, found {actual_version}",
            code="stream_version_conflict",
            context={
                "account_scope_id": account_scope_id,
                "stream_type": stream_type,
                "stream_id": stream_id,
                "expected_version": expected_version,
                "actual_version": actual_version,
            },
        )


class IdempotencyConflictError(JournalWriteError):
    """Raised when an idempotency key is reused for different event content."""


class DuplicateEventIdError(JournalWriteError):
    """Raised when an event ID is reused for different event content."""


class CrossScopeJournalError(JournalWriteError):
    """Raised when one atomic append attempts to span account scopes."""


@dataclass(frozen=True)
class NewEvent:
    """An event ready to be assigned a durable sequence and stream version."""

    account_scope_id: str
    stream_type: str
    stream_id: str
    event_type: str
    payload: Mapping[str, Any]
    occurred_at: datetime
    idempotency_key: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None
    event_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise JournalPayloadError("occurred_at must be timezone-aware UTC")
        if self.occurred_at.utcoffset() != timedelta(0):
            raise JournalPayloadError("occurred_at must use UTC")


@dataclass(frozen=True)
class RecordedEvent:
    """All persisted columns of a row in ``domain_events``."""

    event_seq: int
    event_id: str
    account_scope_id: str
    stream_type: str
    stream_id: str
    stream_version: int
    event_type: str
    payload_json: str
    payload_sha256: str
    idempotency_key: str | None
    causation_id: str | None
    correlation_id: str | None
    occurred_at: datetime
    recorded_at: datetime

    @property
    def payload(self) -> Any:
        """Return the decoded canonical payload."""
        return json.loads(self.payload_json)


@dataclass(frozen=True)
class AppendResult:
    """Recorded events in input order and a parallel duplicate marker tuple."""

    recorded_events: tuple[RecordedEvent, ...]
    duplicates: tuple[bool, ...]

    def __post_init__(self) -> None:
        if len(self.recorded_events) != len(self.duplicates):
            raise ValueError("recorded_events and duplicates must have equal lengths")

    @property
    def events(self) -> tuple[RecordedEvent, ...]:
        """Alias useful to callers that do not need to distinguish new events."""
        return self.recorded_events

    @property
    def duplicate_event_ids(self) -> frozenset[str]:
        """IDs of returned records that were already durable."""
        return frozenset(
            event.event_id
            for event, duplicate in zip(self.recorded_events, self.duplicates, strict=True)
            if duplicate
        )

    @property
    def inserted_events(self) -> tuple[RecordedEvent, ...]:
        """The subset inserted by this call."""
        return tuple(
            event
            for event, duplicate in zip(self.recorded_events, self.duplicates, strict=True)
            if not duplicate
        )


def _canonical_value(value: Any, active_containers: set[int]) -> Any:
    if isinstance(value, Enum):
        return _canonical_value(value.value, active_containers)
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        raise JournalPayloadError("float values are not permitted in journal payloads")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise JournalPayloadError("non-finite Decimal values are not permitted")
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise JournalPayloadError("naive datetimes are not permitted in journal payloads")
        return _format_timestamp(value)
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_containers:
            raise JournalPayloadError("cyclic containers are not permitted in journal payloads")
        active_containers.add(identity)
        try:
            result: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise JournalPayloadError("journal payload mapping keys must be strings")
                result[key] = _canonical_value(item, active_containers)
            return result
        finally:
            active_containers.remove(identity)
    if isinstance(value, list | tuple):
        identity = id(value)
        if identity in active_containers:
            raise JournalPayloadError("cyclic containers are not permitted in journal payloads")
        active_containers.add(identity)
        try:
            return [_canonical_value(item, active_containers) for item in value]
        finally:
            active_containers.remove(identity)
    raise JournalPayloadError(
        f"unsupported journal payload value: {type(value).__name__}"
    )


def canonical_payload_json(payload: Mapping[str, Any]) -> str:
    """Serialize a payload into deterministic, lossless canonical JSON.

    ``Decimal`` values become strings, enum members become their values, and
    aware datetimes are normalized to UTC with six fractional digits.  Floats,
    non-finite decimals, naive datetimes, and unsupported objects are rejected.
    """
    if not isinstance(payload, Mapping):
        raise JournalPayloadError("journal payload must be a mapping")
    canonical = _canonical_value(payload, set())
    try:
        return json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise JournalPayloadError("journal payload cannot be encoded as JSON") from exc


class _Clock(Protocol):
    def now(self) -> datetime: ...


_ROW_COLUMNS = (
    "event_seq, event_id, account_scope_id, stream_type, stream_id, stream_version, "
    "event_type, payload_json, payload_sha256, idempotency_key, causation_id, "
    "correlation_id, occurred_at, recorded_at"
)
_StreamKey = tuple[str, str]


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise JournalPayloadError("timestamp must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_timestamp(value: str) -> datetime:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"stored timestamp is naive: {value!r}")
    return parsed.astimezone(UTC)


def _row_to_recorded(row: tuple[Any, ...]) -> RecordedEvent:
    return RecordedEvent(
        event_seq=int(row[0]),
        event_id=str(row[1]),
        account_scope_id=str(row[2]),
        stream_type=str(row[3]),
        stream_id=str(row[4]),
        stream_version=int(row[5]),
        event_type=str(row[6]),
        payload_json=str(row[7]),
        payload_sha256=str(row[8]),
        idempotency_key=None if row[9] is None else str(row[9]),
        causation_id=None if row[10] is None else str(row[10]),
        correlation_id=None if row[11] is None else str(row[11]),
        occurred_at=_parse_timestamp(str(row[12])),
        recorded_at=_parse_timestamp(str(row[13])),
    )


class DomainEventJournal:
    """SQLite domain-event journal with atomic batches and optimistic streams."""

    def __init__(self, conn: sqlite3.Connection, *, clock: _Clock | Callable[[], datetime]):
        self._conn = conn
        self._clock = clock

    def append(
        self,
        events: Iterable[NewEvent],
        *,
        expected_versions: Mapping[_StreamKey, int] | None = None,
    ) -> AppendResult:
        """Append and commit one all-or-nothing batch."""
        batch = self._prepare_batch(events)
        try:
            if self._conn.in_transaction:
                raise JournalWriteError(
                    "append requires no open transaction; use append_in_transaction"
                )
            if not batch:
                return AppendResult((), ())
            self._conn.execute("BEGIN IMMEDIATE")
            result = self._append_batch(batch, expected_versions)
            self._conn.commit()
            return result
        except JournalWriteError:
            self._rollback_quietly()
            raise
        except sqlite3.Error as exc:
            self._rollback_quietly()
            raise JournalWriteError("atomic journal append failed") from exc
        except Exception as exc:
            self._rollback_quietly()
            raise JournalWriteError("atomic journal append failed") from exc

    def append_in_transaction(
        self,
        events: Iterable[NewEvent],
        *,
        expected_versions: Mapping[_StreamKey, int] | None = None,
    ) -> AppendResult:
        """Append atomically inside an existing caller-owned transaction.

        A savepoint protects this batch.  Success does not commit the caller's
        transaction, and failure leaves the outer transaction open.
        """
        batch = self._prepare_batch(events)
        savepoint = f"journal_append_{uuid4().hex}"
        try:
            if not self._conn.in_transaction:
                raise JournalWriteError(
                    "append_in_transaction requires an open transaction"
                )
            if not batch:
                return AppendResult((), ())
            self._conn.execute(f"SAVEPOINT {savepoint}")
            result = self._append_batch(batch, expected_versions)
            self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return result
        except JournalWriteError:
            self._rollback_savepoint_quietly(savepoint)
            raise
        except sqlite3.Error as exc:
            self._rollback_savepoint_quietly(savepoint)
            raise JournalWriteError("transactional journal append failed") from exc
        except Exception as exc:
            self._rollback_savepoint_quietly(savepoint)
            raise JournalWriteError("transactional journal append failed") from exc

    def read_stream(
        self, account_scope_id: str, stream_type: str, stream_id: str
    ) -> list[RecordedEvent]:
        """Read one stream in version order."""
        try:
            rows = self._conn.execute(
                f"SELECT {_ROW_COLUMNS} FROM domain_events "
                "WHERE account_scope_id = ? AND stream_type = ? AND stream_id = ? "
                "ORDER BY stream_version",
                (account_scope_id, stream_type, stream_id),
            ).fetchall()
            return [_row_to_recorded(tuple(row)) for row in rows]
        except sqlite3.Error as exc:
            raise JournalWriteError("could not read journal stream") from exc

    def read_since(
        self, account_scope_id: str, after_event_seq: int, limit: int
    ) -> list[RecordedEvent]:
        """Read an account scope's events after a global sequence number."""
        if limit < 0:
            raise ValueError("limit must be non-negative")
        try:
            rows = self._conn.execute(
                f"SELECT {_ROW_COLUMNS} FROM domain_events "
                "WHERE account_scope_id = ? AND event_seq > ? "
                "ORDER BY event_seq LIMIT ?",
                (account_scope_id, after_event_seq, limit),
            ).fetchall()
            return [_row_to_recorded(tuple(row)) for row in rows]
        except sqlite3.Error as exc:
            raise JournalWriteError("could not read journal sequence") from exc

    def get_by_idempotency_key(
        self, account_scope_id: str, key: str
    ) -> RecordedEvent | None:
        """Return the event associated with a scoped idempotency key, if any."""
        try:
            row = self._conn.execute(
                f"SELECT {_ROW_COLUMNS} FROM domain_events "
                "WHERE account_scope_id = ? AND idempotency_key = ?",
                (account_scope_id, key),
            ).fetchone()
            return None if row is None else _row_to_recorded(tuple(row))
        except sqlite3.Error as exc:
            raise JournalWriteError("could not read journal idempotency key") from exc

    def verify_chain(self, account_scope_id: str) -> list[str]:
        """Report payload hash mismatches and non-contiguous stream versions."""
        problems: list[str] = []
        try:
            rows = self._conn.execute(
                "SELECT event_seq, event_id, stream_type, stream_id, stream_version, "
                "payload_json, payload_sha256 FROM domain_events "
                "WHERE account_scope_id = ? ORDER BY event_seq",
                (account_scope_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise JournalWriteError("could not verify journal chain") from exc

        expected_by_stream: dict[_StreamKey, int] = {}
        for row in rows:
            event_seq, event_id, stream_type, stream_id, version, payload, stored_hash = row
            actual_hash = hashlib.sha256(str(payload).encode("utf-8")).hexdigest()
            if actual_hash != stored_hash:
                problems.append(
                    f"event {event_id} (seq {event_seq}) payload_sha256 mismatch"
                )
            key = (str(stream_type), str(stream_id))
            expected = expected_by_stream.get(key, 1)
            if version != expected:
                problems.append(
                    f"stream {stream_type}/{stream_id} expected version {expected}, "
                    f"found {version} at event {event_id}"
                )
                expected_by_stream[key] = int(version) + 1
            else:
                expected_by_stream[key] = expected + 1
        return problems

    def _prepare_batch(self, events: Iterable[NewEvent]) -> tuple[NewEvent, ...]:
        try:
            batch = tuple(events)
        except Exception as exc:
            raise JournalWriteError("could not materialize journal batch") from exc
        if any(not isinstance(event, NewEvent) for event in batch):
            raise JournalWriteError("journal batches may contain only NewEvent values")
        scopes = {event.account_scope_id for event in batch}
        if len(scopes) > 1:
            raise CrossScopeJournalError("one journal batch cannot mix account scopes")
        return batch

    def _append_batch(
        self,
        batch: tuple[NewEvent, ...],
        expected_versions: Mapping[_StreamKey, int] | None,
    ) -> AppendResult:
        scope = batch[0].account_scope_id
        stream_keys = {(event.stream_type, event.stream_id) for event in batch}
        current_versions = {
            key: self._stream_version(scope, *key) for key in stream_keys
        }
        self._check_expected_versions(scope, current_versions, expected_versions)

        recorded: list[RecordedEvent] = []
        duplicates: list[bool] = []
        for event in batch:
            payload_json = canonical_payload_json(event.payload)
            payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

            existing_by_id = self._get_by_event_id(event.event_id)
            if existing_by_id is not None:
                if not self._same_event_content(
                    existing_by_id, event, payload_hash, _format_timestamp(event.occurred_at)
                ):
                    raise DuplicateEventIdError(
                        f"event_id {event.event_id!r} is already used for different content"
                    )
                recorded.append(existing_by_id)
                duplicates.append(True)
                continue

            if event.idempotency_key is not None:
                existing_by_key = self._get_by_idempotency(
                    event.account_scope_id, event.idempotency_key
                )
                if existing_by_key is not None:
                    if (
                        existing_by_key.payload_sha256 != payload_hash
                        or existing_by_key.event_type != event.event_type
                    ):
                        raise IdempotencyConflictError(
                            f"idempotency key {event.idempotency_key!r} is already used "
                            "for different event content"
                        )
                    recorded.append(existing_by_key)
                    duplicates.append(True)
                    continue

            stream_key = (event.stream_type, event.stream_id)
            stream_version = current_versions[stream_key] + 1
            recorded_at = _format_timestamp(self._now())
            occurred_at = _format_timestamp(event.occurred_at)
            cursor = self._conn.execute(
                "INSERT INTO domain_events ("
                "event_id, account_scope_id, stream_type, stream_id, stream_version, "
                "event_type, payload_json, payload_sha256, idempotency_key, causation_id, "
                "correlation_id, occurred_at, recorded_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.account_scope_id,
                    event.stream_type,
                    event.stream_id,
                    stream_version,
                    event.event_type,
                    payload_json,
                    payload_hash,
                    event.idempotency_key,
                    event.causation_id,
                    event.correlation_id,
                    occurred_at,
                    recorded_at,
                ),
            )
            if cursor.lastrowid is None:
                raise JournalWriteError("journal insert did not return an event sequence")
            persisted = RecordedEvent(
                event_seq=cursor.lastrowid,
                event_id=event.event_id,
                account_scope_id=event.account_scope_id,
                stream_type=event.stream_type,
                stream_id=event.stream_id,
                stream_version=stream_version,
                event_type=event.event_type,
                payload_json=payload_json,
                payload_sha256=payload_hash,
                idempotency_key=event.idempotency_key,
                causation_id=event.causation_id,
                correlation_id=event.correlation_id,
                occurred_at=_parse_timestamp(occurred_at),
                recorded_at=_parse_timestamp(recorded_at),
            )
            current_versions[stream_key] = stream_version
            recorded.append(persisted)
            duplicates.append(False)
        return AppendResult(tuple(recorded), tuple(duplicates))

    def _stream_version(
        self, account_scope_id: str, stream_type: str, stream_id: str
    ) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(stream_version), 0) FROM domain_events "
            "WHERE account_scope_id = ? AND stream_type = ? AND stream_id = ?",
            (account_scope_id, stream_type, stream_id),
        ).fetchone()
        return int(row[0])

    def _check_expected_versions(
        self,
        scope: str,
        current_versions: Mapping[_StreamKey, int],
        expected_versions: Mapping[_StreamKey, int] | None,
    ) -> None:
        if expected_versions is None:
            return
        for key, expected in expected_versions.items():
            if (
                not isinstance(key, tuple)
                or len(key) != 2
                or not all(isinstance(part, str) for part in key)
            ):
                raise JournalWriteError(
                    "expected_versions keys must be (stream_type, stream_id) tuples"
                )
            if key not in current_versions:
                raise JournalWriteError(
                    f"expected_versions contains a stream absent from the batch: {key!r}"
                )
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                raise JournalWriteError("expected stream versions must be non-negative integers")
            actual = current_versions[key]
            if actual != expected:
                raise StreamVersionConflictError(scope, key[0], key[1], expected, actual)

    def _get_by_event_id(self, event_id: str) -> RecordedEvent | None:
        row = self._conn.execute(
            f"SELECT {_ROW_COLUMNS} FROM domain_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return None if row is None else _row_to_recorded(tuple(row))

    def _get_by_idempotency(self, scope: str, key: str) -> RecordedEvent | None:
        row = self._conn.execute(
            f"SELECT {_ROW_COLUMNS} FROM domain_events "
            "WHERE account_scope_id = ? AND idempotency_key = ?",
            (scope, key),
        ).fetchone()
        return None if row is None else _row_to_recorded(tuple(row))

    @staticmethod
    def _same_event_content(
        existing: RecordedEvent,
        event: NewEvent,
        payload_hash: str,
        occurred_at: str,
    ) -> bool:
        return (
            existing.account_scope_id == event.account_scope_id
            and existing.stream_type == event.stream_type
            and existing.stream_id == event.stream_id
            and existing.event_type == event.event_type
            and existing.payload_sha256 == payload_hash
            and existing.idempotency_key == event.idempotency_key
            and existing.causation_id == event.causation_id
            and existing.correlation_id == event.correlation_id
            and _format_timestamp(existing.occurred_at) == occurred_at
        )

    def _now(self) -> datetime:
        now_method = getattr(self._clock, "now", None)
        if callable(now_method):
            value = now_method()
        elif callable(self._clock):
            value = self._clock()
        else:
            raise JournalWriteError("journal clock must be callable or expose now()")
        if not isinstance(value, datetime):
            raise JournalWriteError("journal clock must return a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise JournalWriteError("journal clock must return an aware datetime")
        return value.astimezone(UTC)

    def _rollback_quietly(self) -> None:
        try:
            if self._conn.in_transaction:
                self._conn.rollback()
        except sqlite3.Error:
            pass

    def _rollback_savepoint_quietly(self, savepoint: str) -> None:
        try:
            self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except sqlite3.Error:
            pass


def commit_then_effect(
    journal: DomainEventJournal,
    events: Iterable[NewEvent],
    effect: Callable[[tuple[RecordedEvent, ...]], Any],
) -> AppendResult:
    """Commit intent events, then invoke ``effect`` exactly once.

    The effect is never called if persistence fails.  If the effect itself
    raises, the recorded intent deliberately remains durable; the caller must
    reconcile that durable intent with the external system before retrying.
    """
    result = journal.append(events)
    effect(result.recorded_events)
    return result
