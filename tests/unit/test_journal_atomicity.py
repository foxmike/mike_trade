from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

import pytest

from mike_trade.persistence.journal import (
    CrossScopeJournalError,
    DomainEventJournal,
    DuplicateEventIdError,
    IdempotencyConflictError,
    JournalPayloadError,
    JournalWriteError,
    NewEvent,
    StreamVersionConflictError,
    canonical_payload_json,
    commit_then_effect,
)

NOW = datetime(2026, 10, 8, 14, 30, 1, 123456, tzinfo=UTC)

ACCOUNT_SCOPES_DDL = """
CREATE TABLE account_scopes(
  account_scope_id TEXT PRIMARY KEY,
  environment TEXT NOT NULL
);
"""

DOMAIN_EVENTS_DDL = """
CREATE TABLE domain_events (
  event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  stream_type TEXT NOT NULL,
  stream_id TEXT NOT NULL,
  stream_version INTEGER NOT NULL CHECK (stream_version >= 1),
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  idempotency_key TEXT,
  causation_id TEXT,
  correlation_id TEXT,
  occurred_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  UNIQUE (account_scope_id, stream_type, stream_id, stream_version),
  UNIQUE (account_scope_id, idempotency_key)
);
"""

UPDATE_TRIGGER_DDL = """
CREATE TRIGGER domain_events_append_only_update
BEFORE UPDATE ON domain_events
BEGIN
  SELECT RAISE(ABORT, 'append-only: domain_events');
END;
"""

DELETE_TRIGGER_DDL = """
CREATE TRIGGER domain_events_append_only_delete
BEFORE DELETE ON domain_events
BEGIN
  SELECT RAISE(ABORT, 'append-only: domain_events');
END;
"""


class FixedClock:
    def now(self) -> datetime:
        return NOW


def _connect(path: Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=timeout)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[Path, sqlite3.Connection]]:
    path = tmp_path / "journal.db"
    conn = _connect(path)
    conn.executescript(
        ACCOUNT_SCOPES_DDL + DOMAIN_EVENTS_DDL + UPDATE_TRIGGER_DDL + DELETE_TRIGGER_DDL
    )
    conn.execute(
        "INSERT INTO account_scopes(account_scope_id, environment) VALUES (?, ?)",
        ("scope-1", "SHADOW"),
    )
    conn.execute(
        "INSERT INTO account_scopes(account_scope_id, environment) VALUES (?, ?)",
        ("scope-2", "PAPER"),
    )
    conn.commit()
    yield path, conn
    conn.close()


def _event(
    *,
    event_type: str = "SomethingHappened",
    stream_type: str = "order",
    stream_id: str = "order-1",
    payload: dict[str, object] | None = None,
    idempotency_key: str | None = None,
    event_id: str | None = None,
    account_scope_id: str = "scope-1",
) -> NewEvent:
    values: dict[str, object] = {
        "account_scope_id": account_scope_id,
        "stream_type": stream_type,
        "stream_id": stream_id,
        "event_type": event_type,
        "payload": {"quantity": Decimal("1.25")} if payload is None else payload,
        "occurred_at": NOW,
        "idempotency_key": idempotency_key,
    }
    if event_id is not None:
        values["event_id"] = event_id
    return NewEvent(**values)  # type: ignore[arg-type]


def test_multi_event_batch_is_atomic_on_third_insert(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    _, conn = database
    conn.execute(
        "CREATE TRIGGER fail_third_event BEFORE INSERT ON domain_events "
        "WHEN NEW.event_type = 'FailThird' "
        "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
    )
    journal = DomainEventJournal(conn, clock=FixedClock())

    with pytest.raises(JournalWriteError) as exc_info:
        journal.append(
            [
                _event(event_type="First", event_id="event-1"),
                _event(event_type="Second", event_id="event-2"),
                _event(event_type="FailThird", event_id="event-3"),
            ]
        )

    assert isinstance(exc_info.value.__cause__, sqlite3.IntegrityError)
    assert conn.execute("SELECT COUNT(*) FROM domain_events").fetchone()[0] == 0


def test_stream_versions_increment_independently(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    _, conn = database
    journal = DomainEventJournal(conn, clock=FixedClock())
    result = journal.append(
        [
            _event(stream_id="one", event_id="e1"),
            _event(stream_id="two", event_id="e2"),
            _event(stream_id="one", event_id="e3"),
        ]
    )

    assert [event.stream_version for event in result.recorded_events] == [1, 1, 2]
    assert result.duplicates == (False, False, False)
    assert [
        event.stream_version
        for event in journal.read_stream("scope-1", "order", "one")
    ] == [1, 2]


def test_stale_expected_version_rolls_back(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    _, conn = database
    journal = DomainEventJournal(conn, clock=FixedClock())
    journal.append([_event(event_id="e1")], expected_versions={("order", "order-1"): 0})

    with pytest.raises(StreamVersionConflictError) as exc_info:
        journal.append([_event(event_id="e2")], expected_versions={("order", "order-1"): 0})

    assert exc_info.value.actual_version == 1
    assert conn.execute("SELECT COUNT(*) FROM domain_events").fetchone()[0] == 1


def test_identical_idempotency_key_returns_original_without_insert(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    _, conn = database
    journal = DomainEventJournal(conn, clock=FixedClock())
    first = journal.append([_event(idempotency_key="request-1", event_id="original")])
    retry = journal.append([_event(idempotency_key="request-1", event_id="retry")])

    assert retry.recorded_events == first.recorded_events
    assert retry.duplicates == (True,)
    assert retry.duplicate_event_ids == frozenset({"original"})
    assert conn.execute("SELECT COUNT(*) FROM domain_events").fetchone()[0] == 1


def test_idempotency_key_with_different_payload_conflicts(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    _, conn = database
    journal = DomainEventJournal(conn, clock=FixedClock())
    journal.append([_event(idempotency_key="request-1", event_id="original")])

    with pytest.raises(IdempotencyConflictError):
        journal.append(
            [
                _event(
                    idempotency_key="request-1",
                    event_id="retry",
                    payload={"quantity": Decimal("2")},
                )
            ]
        )

    assert conn.execute("SELECT COUNT(*) FROM domain_events").fetchone()[0] == 1


def test_duplicate_event_id_requires_identical_content(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    _, conn = database
    journal = DomainEventJournal(conn, clock=FixedClock())
    original = _event(event_id="stable-id")
    first = journal.append([original])
    retry = journal.append([original])
    assert retry.recorded_events == first.recorded_events
    assert retry.duplicates == (True,)

    with pytest.raises(DuplicateEventIdError):
        journal.append([_event(event_id="stable-id", payload={"quantity": Decimal("9")})])
    assert conn.execute("SELECT COUNT(*) FROM domain_events").fetchone()[0] == 1


def test_float_payload_is_rejected(database: tuple[Path, sqlite3.Connection]) -> None:
    _, conn = database
    journal = DomainEventJournal(conn, clock=FixedClock())
    with pytest.raises(JournalPayloadError):
        journal.append([_event(payload={"unsafe": 1.5})])
    with pytest.raises(JournalPayloadError):
        canonical_payload_json({"unsafe": float("nan")})
    assert conn.execute("SELECT COUNT(*) FROM domain_events").fetchone()[0] == 0


def test_canonical_json_is_order_independent_and_normalizes_scalars() -> None:
    class Side(StrEnum):
        BUY = "buy"

    left = {
        "z": Decimal("10.00"),
        "nested": {"time": NOW, "side": Side.BUY},
        "a": [1, True],
    }
    right = {
        "a": [1, True],
        "nested": {"side": Side.BUY, "time": NOW},
        "z": Decimal("10.00"),
    }
    expected = (
        '{"a":[1,true],"nested":{"side":"buy",'
        '"time":"2026-10-08T14:30:01.123456Z"},"z":"10.00"}'
    )
    assert canonical_payload_json(left) == expected
    assert canonical_payload_json(left).encode() == canonical_payload_json(right).encode()


def test_commit_then_effect_is_not_called_after_append_failure(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    _, conn = database
    conn.execute(
        "CREATE TRIGGER reject_events BEFORE INSERT ON domain_events "
        "BEGIN SELECT RAISE(ABORT, 'reject'); END"
    )
    journal = DomainEventJournal(conn, clock=FixedClock())
    calls: list[object] = []

    with pytest.raises(JournalWriteError):
        commit_then_effect(journal, [_event()], calls.append)
    assert calls == []


def test_commit_then_effect_runs_once_after_commit(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    path, conn = database
    journal = DomainEventJournal(conn, clock=FixedClock())
    observations: list[tuple[int, tuple[str, ...]]] = []

    def effect(events: tuple[object, ...]) -> None:
        observer = _connect(path)
        try:
            count = observer.execute("SELECT COUNT(*) FROM domain_events").fetchone()[0]
        finally:
            observer.close()
        observations.append((count, tuple(event.event_id for event in events)))  # type: ignore[attr-defined]

    commit_then_effect(journal, [_event(event_id="committed")], effect)
    assert observations == [(1, ("committed",))]


def test_append_in_transaction_requires_open_transaction(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    _, conn = database
    journal = DomainEventJournal(conn, clock=FixedClock())
    with pytest.raises(JournalWriteError, match="requires an open transaction"):
        journal.append_in_transaction([_event()])


def test_append_in_transaction_leaves_commit_to_caller(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    path, conn = database
    journal = DomainEventJournal(conn, clock=FixedClock())
    conn.execute("BEGIN")
    journal.append_in_transaction([_event(event_id="pending")])
    observer = _connect(path)
    try:
        assert observer.execute("SELECT COUNT(*) FROM domain_events").fetchone()[0] == 0
    finally:
        observer.close()
    conn.commit()
    observer = _connect(path)
    try:
        assert observer.execute("SELECT COUNT(*) FROM domain_events").fetchone()[0] == 1
    finally:
        observer.close()


def test_append_only_triggers_reject_row_mutation(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    _, conn = database
    journal = DomainEventJournal(conn, clock=FixedClock())
    journal.append([_event(event_id="immutable")])
    with pytest.raises(sqlite3.IntegrityError, match="append-only: domain_events"):
        conn.execute("UPDATE domain_events SET event_type = 'Changed'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only: domain_events"):
        conn.execute("DELETE FROM domain_events")


def test_verify_chain_detects_tampered_payload(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    _, conn = database
    journal = DomainEventJournal(conn, clock=FixedClock())
    journal.append([_event(event_id="tampered")])
    conn.execute("DROP TRIGGER domain_events_append_only_update")
    conn.execute("UPDATE domain_events SET payload_json = '{\"quantity\":\"99\"}'")
    conn.execute(UPDATE_TRIGGER_DDL)
    conn.commit()

    problems = journal.verify_chain("scope-1")
    assert len(problems) == 1
    assert "payload_sha256 mismatch" in problems[0]


def test_cross_scope_batch_is_rejected_before_writing(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    _, conn = database
    journal = DomainEventJournal(conn, clock=FixedClock())
    with pytest.raises(CrossScopeJournalError):
        journal.append([_event(event_id="one"), _event(event_id="two", account_scope_id="scope-2")])
    assert conn.execute("SELECT COUNT(*) FROM domain_events").fetchone()[0] == 0


def test_begin_immediate_lock_contention_surfaces_as_journal_error(
    database: tuple[Path, sqlite3.Connection],
) -> None:
    path, first = database
    second = _connect(path, timeout=0)
    try:
        first.execute("BEGIN IMMEDIATE")
        journal = DomainEventJournal(second, clock=FixedClock())
        with pytest.raises(JournalWriteError) as exc_info:
            journal.append([_event(event_id="contended")])
        assert isinstance(exc_info.value.__cause__, sqlite3.OperationalError)
        assert "locked" in str(exc_info.value.__cause__).lower()
        assert second.execute("SELECT COUNT(*) FROM domain_events").fetchone()[0] == 0
    finally:
        first.rollback()
        second.close()
