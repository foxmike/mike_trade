"""Restart-safe stepped-trailing state and immutable audit history.

The shared persistence migration is outside this task's allowed write paths.
``TRAILING_STATE_DDL`` is therefore exposed for a later migration to consume;
the store also applies it when opened so isolated SQLite databases work now.

All prices and quantities are stored as Decimal text and all timestamps use
fixed-width UTC ISO-8601 text.  The history is append-only in both this API and
SQLite (update/delete triggers).  A lower broker-confirmed long stop is
recorded as reconciliation drift and never replaces the confirmed stop.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from mike_trade.domain.trading import TimeInForce

UTC = timezone.utc

__all__ = [
    "TrailingStateStore",
    "TrailingStateRecord",
    "TrailingEvent",
    "TrailingEventType",
    "TrailingStatus",
    "TRAILING_STATE_DDL",
]


TRAILING_STATE_DDL = """
CREATE TABLE IF NOT EXISTS trailing_state_records (
  cycle_id TEXT PRIMARY KEY,
  entry_reference TEXT NOT NULL,
  high_watermark TEXT NOT NULL,
  ratchet_anchor TEXT NOT NULL,
  candidate_stop TEXT NOT NULL,
  last_confirmed_stop TEXT,
  protected_quantity TEXT NOT NULL,
  broker_order_id TEXT,
  broker_status TEXT,
  policy_revision TEXT NOT NULL,
  time_in_force TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','PENDING_REPLACE',
    'TRAIL_UPDATE_UNCONFIRMED','TRAIL_STALE','CLOSED')),
  pending_intent_id TEXT,
  pending_since TEXT,
  last_updated_at TEXT NOT NULL,
  stale_reason TEXT
);
CREATE TABLE IF NOT EXISTS trailing_state_history (
  event_id TEXT PRIMARY KEY,
  cycle_id TEXT NOT NULL REFERENCES trailing_state_records(cycle_id),
  event_type TEXT NOT NULL CHECK(event_type IN ('RATCHET_ADVANCED',
    'REPLACE_INTENT_RECORDED','REPLACE_CONFIRMED','REPLACE_REJECTED',
    'REPLACE_UNKNOWN','STOP_FILLED','STOP_CANCELED','RECONCILED','MARKED_STALE')),
  occurred_at TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  prior_values TEXT NOT NULL,
  new_values TEXT NOT NULL,
  policy_revision TEXT NOT NULL,
  reconciliation_drift INTEGER NOT NULL DEFAULT 0 CHECK(reconciliation_drift IN (0,1)),
  UNIQUE(cycle_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS trailing_state_history_cycle_time_idx
  ON trailing_state_history(cycle_id, occurred_at);
CREATE TRIGGER IF NOT EXISTS trailing_state_history_no_update
  BEFORE UPDATE ON trailing_state_history
  BEGIN SELECT RAISE(ABORT, 'append-only: trailing_state_history'); END;
CREATE TRIGGER IF NOT EXISTS trailing_state_history_no_delete
  BEFORE DELETE ON trailing_state_history
  BEGIN SELECT RAISE(ABORT, 'append-only: trailing_state_history'); END;
"""


class TrailingEventType(StrEnum):
    RATCHET_ADVANCED = "RATCHET_ADVANCED"
    REPLACE_INTENT_RECORDED = "REPLACE_INTENT_RECORDED"
    REPLACE_CONFIRMED = "REPLACE_CONFIRMED"
    REPLACE_REJECTED = "REPLACE_REJECTED"
    REPLACE_UNKNOWN = "REPLACE_UNKNOWN"
    STOP_FILLED = "STOP_FILLED"
    STOP_CANCELED = "STOP_CANCELED"
    RECONCILED = "RECONCILED"
    MARKED_STALE = "MARKED_STALE"


class TrailingStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PENDING_REPLACE = "PENDING_REPLACE"
    TRAIL_UPDATE_UNCONFIRMED = "TRAIL_UPDATE_UNCONFIRMED"
    TRAIL_STALE = "TRAIL_STALE"
    CLOSED = "CLOSED"


def _decimal(value: Decimal | None, name: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal" if value is not None else f"{name} is required")
    return value


def _timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return _timestamp(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    return _timestamp(parsed)


def _dec_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _dec(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"invalid persisted decimal: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"invalid persisted decimal: {value!r}")
    return parsed


def _json(values: dict[str, Any]) -> str:
    return json.dumps(values, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class TrailingStateRecord:
    cycle_id: str
    entry_reference: Decimal
    high_watermark: Decimal
    ratchet_anchor: Decimal
    candidate_stop: Decimal
    last_confirmed_stop: Decimal | None
    protected_quantity: Decimal
    broker_order_id: str | None
    broker_status: str | None
    policy_revision: str
    time_in_force: TimeInForce
    status: TrailingStatus = TrailingStatus.ACTIVE
    pending_intent_id: str | None = None
    pending_since: datetime | None = None
    last_updated_at: datetime | None = None
    stale_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.cycle_id, str) or not self.cycle_id.strip():
            raise ValueError("cycle_id must be a non-empty string")
        for name in ("entry_reference", "high_watermark", "ratchet_anchor", "candidate_stop", "protected_quantity"):
            _decimal(getattr(self, name), name)
        _decimal(self.last_confirmed_stop, "last_confirmed_stop", optional=True)
        if not isinstance(self.policy_revision, str) or not self.policy_revision.strip():
            raise ValueError("policy_revision must be non-empty")
        try:
            tif = self.time_in_force if isinstance(self.time_in_force, TimeInForce) else TimeInForce[str(self.time_in_force).upper()]
        except (KeyError, ValueError):
            raise ValueError("time_in_force must be DAY or GTC") from None
        object.__setattr__(self, "time_in_force", tif)
        object.__setattr__(self, "status", TrailingStatus(self.status))
        if self.pending_since is not None:
            object.__setattr__(self, "pending_since", _timestamp(self.pending_since))
        if self.last_updated_at is not None:
            object.__setattr__(self, "last_updated_at", _timestamp(self.last_updated_at))

    @property
    def requires_reconciliation(self) -> bool:
        return self.pending_intent_id is not None or self.status in (
            TrailingStatus.PENDING_REPLACE,
            TrailingStatus.TRAIL_UPDATE_UNCONFIRMED,
        )


@dataclass(frozen=True, slots=True)
class TrailingEvent:
    event_id: str
    cycle_id: str
    event_type: TrailingEventType
    occurred_at: datetime
    idempotency_key: str
    prior_values: dict[str, Any]
    new_values: dict[str, Any]
    policy_revision: str
    reconciliation_drift: bool = False


class TrailingStateStore:
    """SQLite repository for one or more position-cycle trailing records."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(Path(db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            self._conn.close()
            raise sqlite3.OperationalError("SQLite refused WAL mode")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(TRAILING_STATE_DDL)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> TrailingStateStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _transaction(self):
        return _Transaction(self._conn)

    def load(self, cycle_id: str) -> TrailingStateRecord | None:
        row = self._conn.execute("SELECT * FROM trailing_state_records WHERE cycle_id=?", (cycle_id,)).fetchone()
        return None if row is None else self._record(row)

    def create(self, record: TrailingStateRecord, *, idempotency_key: str, occurred_at: datetime | None = None) -> TrailingStateRecord:
        stamp = _stamp(occurred_at or datetime.now(UTC))
        if record.last_updated_at is not None:
            stamp = _stamp(record.last_updated_at)
        values = self._values(record)
        with self._transaction() as conn:
            prior = self._find_event(conn, record.cycle_id, idempotency_key)
            if prior is not None:
                return self.load(record.cycle_id)  # type: ignore[return-value]
            conn.execute(
                "INSERT INTO trailing_state_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record.cycle_id, values[0], values[1], values[2], values[3], values[4], values[5], values[6], values[7], values[8], values[9], values[10], values[11], values[12], stamp, values[13]),
            )
            self._append(conn, record.cycle_id, TrailingEventType.RATCHET_ADVANCED, idempotency_key, {}, self._snapshot(record), record.policy_revision, stamp)
        return self.load(record.cycle_id)  # type: ignore[return-value]

    def record_candidate(self, cycle_id: str, *, high_watermark: Decimal, ratchet_anchor: Decimal,
                         candidate_stop: Decimal, policy_revision: str, idempotency_key: str,
                         occurred_at: datetime | None = None) -> TrailingStateRecord:
        stamp = _stamp(occurred_at or datetime.now(UTC))
        with self._transaction() as conn:
            if self._find_event(conn, cycle_id, idempotency_key):
                return self._required(cycle_id)
            row = self._row(conn, cycle_id)
            record = self._record(row)
            self._ensure_advancement_allowed(record)
            high = _decimal(high_watermark, "high_watermark")
            anchor = _decimal(ratchet_anchor, "ratchet_anchor")
            candidate = _decimal(candidate_stop, "candidate_stop")
            prior = self._snapshot(record)
            if high < record.high_watermark or anchor < record.ratchet_anchor or candidate < record.candidate_stop:
                raise ValueError("trailing candidate values cannot move backward")
            conn.execute("UPDATE trailing_state_records SET high_watermark=?, ratchet_anchor=?, candidate_stop=?, policy_revision=?, last_updated_at=? WHERE cycle_id=?",
                         (str(high), str(anchor), str(candidate), policy_revision, stamp, cycle_id))
            updated = self._required(cycle_id)
            self._append(conn, cycle_id, TrailingEventType.RATCHET_ADVANCED, idempotency_key, prior, self._snapshot(updated), policy_revision, stamp)
        return self._required(cycle_id)

    def record_replacement_intent(self, cycle_id: str, *, intent_id: str, candidate_stop: Decimal,
                                  idempotency_key: str, occurred_at: datetime | None = None) -> TrailingEvent:
        stamp = _stamp(occurred_at or datetime.now(UTC))
        with self._transaction() as conn:
            dup = self._find_event(conn, cycle_id, idempotency_key)
            if dup is not None:
                return dup
            record = self._record(self._row(conn, cycle_id))
            if record.requires_reconciliation:
                return self._pending_event(conn, cycle_id)
            if record.status in (TrailingStatus.TRAIL_STALE, TrailingStatus.CLOSED):
                raise ValueError(f"cannot replace while trailing state is {record.status.value}")
            candidate = _decimal(candidate_stop, "candidate_stop")
            prior = self._snapshot(record)
            new = {"pending_intent_id": intent_id, "candidate_stop": str(candidate), "status": TrailingStatus.PENDING_REPLACE.value}
            conn.execute("UPDATE trailing_state_records SET candidate_stop=?, status=?, pending_intent_id=?, pending_since=?, last_updated_at=? WHERE cycle_id=?",
                         (str(candidate), TrailingStatus.PENDING_REPLACE.value, intent_id, stamp, stamp, cycle_id))
            return self._append(conn, cycle_id, TrailingEventType.REPLACE_INTENT_RECORDED, idempotency_key, prior, new, record.policy_revision, stamp)

    def confirm_replacement(self, cycle_id: str, *, price: Decimal, broker_order_id: str,
                            broker_status: str = "ACCEPTED", idempotency_key: str,
                            occurred_at: datetime | None = None) -> TrailingEvent:
        return self._broker_event(cycle_id, TrailingEventType.REPLACE_CONFIRMED, idempotency_key,
                                  occurred_at, price=price, broker_order_id=broker_order_id,
                                  broker_status=broker_status)

    def reject_replacement(self, cycle_id: str, *, broker_status: str = "REJECTED",
                           idempotency_key: str, occurred_at: datetime | None = None) -> TrailingEvent:
        return self._broker_event(cycle_id, TrailingEventType.REPLACE_REJECTED, idempotency_key,
                                  occurred_at, broker_status=broker_status)

    def mark_replacement_unknown(self, cycle_id: str, *, idempotency_key: str,
                                 occurred_at: datetime | None = None) -> TrailingEvent:
        return self._broker_event(cycle_id, TrailingEventType.REPLACE_UNKNOWN, idempotency_key, occurred_at)

    def record_stop_filled(self, cycle_id: str, *, broker_order_id: str,
                           idempotency_key: str, occurred_at: datetime | None = None) -> TrailingEvent:
        return self._terminal_stop_event(cycle_id, TrailingEventType.STOP_FILLED, broker_order_id,
                                         idempotency_key, occurred_at)

    def record_stop_canceled(self, cycle_id: str, *, broker_order_id: str,
                             idempotency_key: str, occurred_at: datetime | None = None) -> TrailingEvent:
        return self._terminal_stop_event(cycle_id, TrailingEventType.STOP_CANCELED, broker_order_id,
                                         idempotency_key, occurred_at)

    def mark_stale(self, cycle_id: str, reason: str, *, idempotency_key: str | None = None,
                   occurred_at: datetime | None = None) -> TrailingStateRecord:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be non-empty")
        stamp = _stamp(occurred_at or datetime.now(UTC))
        key = idempotency_key or f"stale:{cycle_id}:{stamp}:{reason}"
        with self._transaction() as conn:
            if self._find_event(conn, cycle_id, key):
                return self._required(cycle_id)
            record = self._record(self._row(conn, cycle_id))
            prior = self._snapshot(record)
            conn.execute("UPDATE trailing_state_records SET status=?, stale_reason=?, last_updated_at=? WHERE cycle_id=?",
                         (TrailingStatus.TRAIL_STALE.value, reason.strip(), stamp, cycle_id))
            updated = self._required(cycle_id)
            self._append(conn, cycle_id, TrailingEventType.MARKED_STALE, key, prior, {**self._snapshot(updated), "reason": reason.strip()}, record.policy_revision, stamp)
        return self._required(cycle_id)

    def reconcile(self, cycle_id: str, *, idempotency_key: str, broker_order_id: str | None = None,
                  broker_status: str | None = None, occurred_at: datetime | None = None) -> TrailingStateRecord:
        stamp = _stamp(occurred_at or datetime.now(UTC))
        with self._transaction() as conn:
            if self._find_event(conn, cycle_id, idempotency_key):
                return self._required(cycle_id)
            record = self._record(self._row(conn, cycle_id))
            prior = self._snapshot(record)
            status = TrailingStatus.ACTIVE if record.status is not TrailingStatus.TRAIL_STALE else record.status
            conn.execute("UPDATE trailing_state_records SET pending_intent_id=NULL, pending_since=NULL, broker_order_id=COALESCE(?,broker_order_id), broker_status=COALESCE(?,broker_status), status=?, last_updated_at=? WHERE cycle_id=?",
                         (broker_order_id, broker_status, status.value, stamp, cycle_id))
            updated = self._required(cycle_id)
            self._append(conn, cycle_id, TrailingEventType.RECONCILED, idempotency_key, prior, self._snapshot(updated), record.policy_revision, stamp)
        return self._required(cycle_id)

    def _terminal_stop_event(self, cycle_id: str, event_type: TrailingEventType, broker_order_id: str,
                             key: str, occurred_at: datetime | None) -> TrailingEvent:
        if not broker_order_id:
            raise ValueError("terminal stop update requires broker_order_id")
        stamp = _stamp(occurred_at or datetime.now(UTC))
        with self._transaction() as conn:
            prior_event = self._find_event(conn, cycle_id, key)
            if prior_event is not None:
                return prior_event
            record = self._record(self._row(conn, cycle_id))
            prior = self._snapshot(record)
            next_status = (
                TrailingStatus.CLOSED
                if event_type is TrailingEventType.STOP_FILLED
                else TrailingStatus.TRAIL_UPDATE_UNCONFIRMED
            )
            new = {**prior, "broker_order_id": broker_order_id, "broker_status": event_type.value,
                   "status": next_status.value, "pending_intent_id": None}
            conn.execute("UPDATE trailing_state_records SET broker_order_id=?, broker_status=?, status=?, pending_intent_id=NULL, pending_since=NULL, last_updated_at=? WHERE cycle_id=?",
                         (broker_order_id, event_type.value, next_status.value, stamp, cycle_id))
            return self._append(conn, cycle_id, event_type, key, prior, new, record.policy_revision, stamp)

    def history(self, cycle_id: str) -> tuple[TrailingEvent, ...]:
        rows = self._conn.execute("SELECT * FROM trailing_state_history WHERE cycle_id=? ORDER BY rowid", (cycle_id,)).fetchall()
        return tuple(self._event(row) for row in rows)

    def _broker_event(self, cycle_id: str, event_type: TrailingEventType, key: str,
                      occurred_at: datetime | None, *, price: Decimal | None = None,
                      broker_order_id: str | None = None, broker_status: str | None = None) -> TrailingEvent:
        stamp = _stamp(occurred_at or datetime.now(UTC))
        with self._transaction() as conn:
            dup = self._find_event(conn, cycle_id, key)
            if dup is not None:
                return dup
            record = self._record(self._row(conn, cycle_id))
            prior = self._snapshot(record)
            if event_type is TrailingEventType.REPLACE_CONFIRMED:
                if not broker_order_id:
                    raise ValueError("confirmation requires broker_order_id")
                confirmed = _decimal(price, "price")
                if record.last_confirmed_stop is not None and confirmed < record.last_confirmed_stop:
                    new = {"reported_price": str(confirmed), "broker_order_id": broker_order_id,
                           "broker_status": broker_status, "drift": "CONFIRMED_STOP_LOWER_THAN_CURRENT",
                           "status": TrailingStatus.TRAIL_UPDATE_UNCONFIRMED.value}
                    conn.execute("UPDATE trailing_state_records SET status=?, last_updated_at=? WHERE cycle_id=?",
                                 (TrailingStatus.TRAIL_UPDATE_UNCONFIRMED.value, stamp, cycle_id))
                    return self._append(conn, cycle_id, TrailingEventType.RECONCILED, key, prior, new,
                                        record.policy_revision, stamp, drift=True)
                conn.execute("UPDATE trailing_state_records SET last_confirmed_stop=?, broker_order_id=?, broker_status=?, status=?, pending_intent_id=NULL, pending_since=NULL, stale_reason=NULL, last_updated_at=? WHERE cycle_id=?",
                             (str(confirmed), broker_order_id, broker_status, TrailingStatus.ACTIVE.value, stamp, cycle_id))
            elif event_type is TrailingEventType.REPLACE_REJECTED:
                conn.execute("UPDATE trailing_state_records SET broker_status=?, status=?, pending_intent_id=NULL, pending_since=NULL, last_updated_at=? WHERE cycle_id=?",
                             (broker_status, TrailingStatus.ACTIVE.value, stamp, cycle_id))
            elif event_type is TrailingEventType.REPLACE_UNKNOWN:
                conn.execute("UPDATE trailing_state_records SET status=?, last_updated_at=? WHERE cycle_id=?",
                             (TrailingStatus.TRAIL_UPDATE_UNCONFIRMED.value, stamp, cycle_id))
            updated = self._required(cycle_id)
            new_values: dict[str, Any] = self._snapshot(updated)
            if price is not None:
                new_values["reported_price"] = str(price)
            self._append(conn, cycle_id, event_type, key, prior, new_values, record.policy_revision, stamp)
        return self.history(cycle_id)[-1]

    def _append(self, conn, cycle_id, event_type, key, prior, new, revision, stamp, drift=False):
        event_id = uuid4().hex
        conn.execute("INSERT INTO trailing_state_history VALUES (?,?,?,?,?,?,?,?,?)",
                     (event_id, cycle_id, event_type.value, stamp, key, _json(prior), _json(new), revision, int(drift)))
        return self._event(conn.execute("SELECT * FROM trailing_state_history WHERE event_id=?", (event_id,)).fetchone())

    @staticmethod
    def _snapshot(record: TrailingStateRecord) -> dict[str, Any]:
        return {"entry_reference": str(record.entry_reference), "high_watermark": str(record.high_watermark),
                "ratchet_anchor": str(record.ratchet_anchor), "candidate_stop": str(record.candidate_stop),
                "last_confirmed_stop": _dec_text(record.last_confirmed_stop),
                "protected_quantity": str(record.protected_quantity), "broker_order_id": record.broker_order_id,
                "broker_status": record.broker_status, "policy_revision": record.policy_revision,
                "time_in_force": record.time_in_force.value, "status": record.status.value,
                "pending_intent_id": record.pending_intent_id}

    @staticmethod
    def _values(r: TrailingStateRecord):
        return (str(r.entry_reference), str(r.high_watermark), str(r.ratchet_anchor), str(r.candidate_stop),
                _dec_text(r.last_confirmed_stop), str(r.protected_quantity), r.broker_order_id, r.broker_status,
                r.policy_revision, r.time_in_force.value, r.status.value, r.pending_intent_id,
                None if r.pending_since is None else _stamp(r.pending_since), r.stale_reason)

    @staticmethod
    def _record(row):
        return TrailingStateRecord(cycle_id=row["cycle_id"], entry_reference=_dec(row["entry_reference"]),
            high_watermark=_dec(row["high_watermark"]), ratchet_anchor=_dec(row["ratchet_anchor"]),
            candidate_stop=_dec(row["candidate_stop"]), last_confirmed_stop=_dec(row["last_confirmed_stop"]),
            protected_quantity=_dec(row["protected_quantity"]), broker_order_id=row["broker_order_id"],
            broker_status=row["broker_status"], policy_revision=row["policy_revision"],
            time_in_force=TimeInForce[row["time_in_force"]], status=TrailingStatus(row["status"]),
            pending_intent_id=row["pending_intent_id"], pending_since=None if row["pending_since"] is None else _parse_stamp(row["pending_since"]),
            last_updated_at=_parse_stamp(row["last_updated_at"]), stale_reason=row["stale_reason"])

    @staticmethod
    def _event(row):
        return TrailingEvent(row["event_id"], row["cycle_id"], TrailingEventType(row["event_type"]),
            _parse_stamp(row["occurred_at"]), row["idempotency_key"], json.loads(row["prior_values"]),
            json.loads(row["new_values"]), row["policy_revision"], bool(row["reconciliation_drift"]))

    @staticmethod
    def _row(conn, cycle_id):
        row = conn.execute("SELECT * FROM trailing_state_records WHERE cycle_id=?", (cycle_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown trailing cycle {cycle_id!r}")
        return row

    def _required(self, cycle_id):
        record = self.load(cycle_id)
        if record is None:
            raise KeyError(f"unknown trailing cycle {cycle_id!r}")
        return record

    @staticmethod
    def _ensure_advancement_allowed(record):
        if record.status is TrailingStatus.TRAIL_STALE:
            raise RuntimeError("trailing advancement is suspended while stale")
        if record.requires_reconciliation:
            raise RuntimeError("outstanding replacement requires reconciliation before advancement")
        if record.status is TrailingStatus.CLOSED:
            raise RuntimeError("closed trailing state cannot advance")

    @staticmethod
    def _find_event(conn, cycle_id, key):
        if not isinstance(key, str) or not key:
            raise ValueError("idempotency_key must be non-empty")
        row = conn.execute("SELECT * FROM trailing_state_history WHERE cycle_id=? AND idempotency_key=?", (cycle_id, key)).fetchone()
        return None if row is None else TrailingStateStore._event(row)

    @staticmethod
    def _pending_event(conn, cycle_id):
        row = conn.execute("SELECT * FROM trailing_state_history WHERE cycle_id=? AND event_type='REPLACE_INTENT_RECORDED' ORDER BY rowid DESC LIMIT 1", (cycle_id,)).fetchone()
        if row is None:
            raise RuntimeError("record has unresolved replacement state without intent history")
        return TrailingStateStore._event(row)


class _Transaction:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
