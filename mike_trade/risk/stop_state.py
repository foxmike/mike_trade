"""Persisted broker-stop lifecycle and read projections.

TODO: Add a protection-state projection and append-only audit table migration.
Migration files are outside this task's allowed write paths, so persistence is
defined against an injected atomic storage protocol until that migration lands.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Protocol

__all__ = [
    "ProtectionCondition",
    "ProtectionStateRecord",
    "apply_event",
    "ProtectionStateRepository",
    "entry_block_reasons",
    "to_dashboard_view",
]


class ProtectionCondition(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    UPDATING = "UPDATING"
    UNCONFIRMED = "UNCONFIRMED"
    TRIGGERED = "TRIGGERED"
    EXPIRED = "EXPIRED"
    UNPROTECTED = "UNPROTECTED"
    OFFLINE = "OFFLINE"


_MODES = frozenset({"FIXED", "TRAILING_NATIVE", "TRAILING_STEPPED"})
_EXECUTION_MODES = frozenset({"BACKTEST", "SHADOW", "PAPER", "LIVE"})
_ZERO = Decimal("0")
UTC = timezone.utc


def _text(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _utc(value: datetime | None, name: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC")
    return value.astimezone(UTC)


def _decimal(value: object, name: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    return value


@dataclass(frozen=True, slots=True)
class ProtectionStateRecord:
    cycle_id: str
    account_id: str
    scenario_id: str | None
    symbol: str
    mode: str
    execution_mode: str = "PAPER"
    broker_order_id: str | None = None
    client_order_id: str | None = None
    protected_qty: Decimal = _ZERO
    remaining_position_qty: Decimal = _ZERO
    stop_price: Decimal | None = None
    limit_price: Decimal | None = None
    trail_price: Decimal | None = None
    trail_percent: Decimal | None = None
    broker_hwm: Decimal | None = None
    tif: str = "GTC"
    condition: ProtectionCondition = ProtectionCondition.PENDING
    last_broker_confirmed_at: datetime | None = None
    revision: int = 0
    reason: str | None = None
    pending_since: datetime | None = None
    applied_event_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name in ("cycle_id", "account_id", "symbol"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "symbol", self.symbol.upper())
        if self.scenario_id is not None:
            object.__setattr__(self, "scenario_id", _text(self.scenario_id, "scenario_id"))
        mode = _text(self.mode, "mode").upper()
        if mode not in _MODES:
            raise ValueError(f"mode must be one of {sorted(_MODES)}")
        object.__setattr__(self, "mode", mode)
        execution_mode = _text(self.execution_mode, "execution_mode").upper()
        if execution_mode not in _EXECUTION_MODES:
            raise ValueError(f"execution_mode must be one of {sorted(_EXECUTION_MODES)}")
        object.__setattr__(self, "execution_mode", execution_mode)
        for name in ("broker_order_id", "client_order_id", "reason"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _text(value, name))
        for name in ("protected_qty", "remaining_position_qty"):
            value = _decimal(getattr(self, name), name)
            if value < _ZERO:
                raise ValueError(f"{name} must be non-negative")
        for name in ("stop_price", "limit_price", "trail_price", "trail_percent", "broker_hwm"):
            _decimal(getattr(self, name), name, optional=True)
        if self.mode != "TRAILING_NATIVE" and self.broker_hwm is not None:
            raise ValueError("broker_hwm is only valid for TRAILING_NATIVE protection")
        if not isinstance(self.tif, str) or not self.tif.strip():
            raise ValueError("tif must be a non-empty string")
        object.__setattr__(self, "tif", self.tif.strip().upper())
        if not isinstance(self.condition, ProtectionCondition):
            object.__setattr__(self, "condition", ProtectionCondition(self.condition))
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")
        object.__setattr__(self, "last_broker_confirmed_at", _utc(self.last_broker_confirmed_at, "last_broker_confirmed_at", optional=True))
        object.__setattr__(self, "pending_since", _utc(self.pending_since, "pending_since", optional=True))
        if not isinstance(self.applied_event_ids, frozenset):
            raise TypeError("applied_event_ids must be a frozenset")

    @property
    def key(self) -> tuple[str, str, str | None, str]:
        """Mode/account/scenario/cycle key; cycle ids are scoped, not global."""
        return (self.execution_mode, self.account_id, self.scenario_id, self.cycle_id)


@dataclass(frozen=True, slots=True)
class _AuditEvent:
    event_id: str
    cycle_id: str
    account_id: str
    scenario_id: str | None
    mode: str
    execution_mode: str
    event_type: str
    occurred_at: datetime
    reason: str | None
    payload: Mapping[str, Any]


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported event value: {type(value).__name__}")


def _event_id(record: ProtectionStateRecord, event: Mapping[str, Any]) -> str:
    explicit = event.get("broker_update_id") or event.get("event_id")
    if explicit is not None:
        value = f"{record.execution_mode}|{record.account_id}|{record.scenario_id}|{record.cycle_id}|{explicit}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    body = json.dumps(dict(event), sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def apply_event(
    record: ProtectionStateRecord, event: Mapping[str, Any]
) -> tuple[ProtectionStateRecord, tuple[_AuditEvent, ...]]:
    """Apply one lifecycle event without I/O; illegal lifecycle changes raise ValueError."""
    if not isinstance(record, ProtectionStateRecord):
        raise TypeError("record must be ProtectionStateRecord")
    if not isinstance(event, Mapping):
        raise TypeError("event must be a mapping")
    kind = str(event.get("type", event.get("event_type", ""))).strip().lower().replace("-", "_").replace(" ", "_")
    done_for_day_event = kind == "done_for_day"
    aliases = {
        "placement_requested": "placement_requested", "placement_request": "placement_requested",
        "broker_ack": "broker_ack", "placement_confirmed": "broker_ack", "ack": "broker_ack",
        "replace_requested": "replace_requested", "replace_confirmed": "replace_confirmed",
        "cancel_confirmed": "cancel_confirmed", "trigger": "trigger", "fill": "trigger",
        "trigger_fill": "trigger", "expiry": "expiry", "expired": "expiry",
        "done_for_day": "expiry", "placement_failed": "failure", "placement_failure": "failure",
        "coverage_failed": "failure", "coverage_failure": "failure",
        "failure": "failure", "broker_unreadable": "unconfirmed", "unconfirmed": "unconfirmed",
        "heartbeat_offline": "offline", "engine_offline": "offline", "offline": "offline",
    }
    if kind not in aliases:
        raise ValueError(f"unsupported protection event {kind!r}")
    kind = aliases[kind]
    eid = _event_id(record, event)
    if eid in record.applied_event_ids:
        return record, ()
    now = _utc(event.get("occurred_at", datetime.now(UTC)), "occurred_at")
    assert now is not None
    payload = dict(event)
    target = record.condition
    updates: dict[str, Any] = {}
    reason = event.get("reason", record.reason)
    terminal = {ProtectionCondition.TRIGGERED, ProtectionCondition.EXPIRED}

    if record.condition is ProtectionCondition.TRIGGERED and kind != "offline":
        raise ValueError(f"illegal transition from {record.condition.value} via {kind}")
    if record.condition is ProtectionCondition.EXPIRED and kind not in {"offline", "placement_requested"}:
        raise ValueError(f"illegal transition from {record.condition.value} via {kind}")
    if kind == "placement_requested":
        if record.condition not in {ProtectionCondition.PENDING, ProtectionCondition.UNPROTECTED, ProtectionCondition.EXPIRED}:
            raise ValueError(f"illegal transition from {record.condition.value} via placement_requested")
        target = ProtectionCondition.PENDING
        updates["pending_since"] = now
        reason = None
    elif kind == "broker_ack":
        if record.condition not in {ProtectionCondition.PENDING, ProtectionCondition.UPDATING, ProtectionCondition.UNCONFIRMED, ProtectionCondition.OFFLINE, ProtectionCondition.ACTIVE}:
            raise ValueError(f"illegal transition from {record.condition.value} via broker_ack")
        target = ProtectionCondition.ACTIVE
        updates["last_broker_confirmed_at"] = now
        updates["pending_since"] = None
        reason = None
    elif kind == "replace_requested":
        if record.condition not in {ProtectionCondition.ACTIVE, ProtectionCondition.UNCONFIRMED, ProtectionCondition.OFFLINE}:
            raise ValueError(f"illegal transition from {record.condition.value} via replace_requested")
        target = ProtectionCondition.UPDATING
    elif kind == "replace_confirmed":
        if record.condition not in {ProtectionCondition.UPDATING, ProtectionCondition.UNCONFIRMED, ProtectionCondition.OFFLINE}:
            raise ValueError(f"illegal transition from {record.condition.value} via replace_confirmed")
        target = ProtectionCondition.ACTIVE
        updates["last_broker_confirmed_at"] = now
        reason = None
    elif kind == "cancel_confirmed":
        if record.condition not in {ProtectionCondition.ACTIVE, ProtectionCondition.UPDATING, ProtectionCondition.UNCONFIRMED, ProtectionCondition.OFFLINE, ProtectionCondition.PENDING}:
            raise ValueError(f"illegal transition from {record.condition.value} via cancel_confirmed")
        target = ProtectionCondition.UNPROTECTED if record.remaining_position_qty > _ZERO else ProtectionCondition.EXPIRED
        reason = "UNPROTECTED_POSITION" if target is ProtectionCondition.UNPROTECTED else reason
    elif kind == "trigger":
        if record.condition not in {ProtectionCondition.ACTIVE, ProtectionCondition.UPDATING, ProtectionCondition.UNCONFIRMED, ProtectionCondition.OFFLINE}:
            raise ValueError(f"illegal transition from {record.condition.value} via trigger")
        target = ProtectionCondition.TRIGGERED
        filled = _decimal(event.get("filled_qty", record.protected_qty), "filled_qty")
        updates["remaining_position_qty"] = max(_ZERO, record.remaining_position_qty - filled)
        updates["protected_qty"] = max(_ZERO, record.protected_qty - filled)
        updates["last_broker_confirmed_at"] = now
    elif kind == "expiry":
        if record.tif != "DAY" and str(event.get("status", "")).lower() != "done_for_day" and not done_for_day_event:
            raise ValueError("expiry requires DAY TIF or done_for_day status")
        if record.condition not in {ProtectionCondition.ACTIVE, ProtectionCondition.UPDATING, ProtectionCondition.UNCONFIRMED, ProtectionCondition.OFFLINE, ProtectionCondition.PENDING}:
            raise ValueError(f"illegal transition from {record.condition.value} via expiry")
        target = ProtectionCondition.EXPIRED
        reason = "PROTECTION_EXPIRED/OVERNIGHT"
    elif kind == "failure":
        if record.condition in {ProtectionCondition.ACTIVE, ProtectionCondition.UPDATING, ProtectionCondition.UNCONFIRMED, ProtectionCondition.OFFLINE, ProtectionCondition.PENDING}:
            target = ProtectionCondition.UNPROTECTED
            reason = "UNPROTECTED_POSITION"
        else:
            raise ValueError(f"illegal transition from {record.condition.value} via failure")
    elif kind == "unconfirmed":
        if record.condition in terminal:
            raise ValueError(f"illegal transition from {record.condition.value} via unconfirmed")
        target = ProtectionCondition.UNCONFIRMED
        reason = str(event.get("reason", "BROKER_STATE_UNREADABLE"))
    elif kind == "offline":
        target = ProtectionCondition.OFFLINE
        reason = str(event.get("reason", "ENGINE_OFFLINE"))

    # Event fields may update the broker snapshot. A long-position stop may only rise.
    for field in ("broker_order_id", "client_order_id", "protected_qty", "remaining_position_qty", "stop_price", "limit_price", "trail_price", "trail_percent", "broker_hwm", "tif"):
        if field in event:
            val = event[field]
            if field in {"protected_qty", "remaining_position_qty", "stop_price", "limit_price", "trail_price", "trail_percent", "broker_hwm"} and val is not None:
                val = _decimal(val, field)
            if field in {"broker_order_id", "client_order_id"} and val is not None:
                val = _text(val, field)
            if field == "tif" and val is not None:
                val = str(val).strip().upper()
            updates[field] = val
    proposed_stop = updates.get("stop_price", record.stop_price)
    if record.stop_price is not None and proposed_stop is not None and proposed_stop < record.stop_price:
        raise ValueError("stop_price may not decrease for a long position")
    if reason is not None:
        reason = _text(str(reason), "reason")
    updates.update(condition=target, reason=reason, revision=record.revision + 1,
                   applied_event_ids=record.applied_event_ids | {eid})
    updated = replace(record, **updates)
    audit = _AuditEvent(eid, record.cycle_id, record.account_id, record.scenario_id,
                        record.mode, record.execution_mode, kind.upper(), now, updated.reason, payload)
    return updated, (audit,)


class ProtectionStateStorage(Protocol):
    """Atomic persistence port for append-only audit and current projection writes."""

    def load(self, key: tuple[str, str, str | None, str]) -> ProtectionStateRecord | None: ...
    def list_records(self, *, mode: str | None = None, account_id: str | None = None,
                     scenario_id: str | None = None) -> tuple[ProtectionStateRecord, ...]: ...
    def commit(self, record: ProtectionStateRecord, events: tuple[_AuditEvent, ...]) -> None: ...


class InMemoryProtectionStateStorage:
    """Test storage with atomic append-only audit and upserted projection semantics."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str | None, str], ProtectionStateRecord] = {}
        self._events: dict[str, _AuditEvent] = {}

    @property
    def audit_events(self) -> tuple[_AuditEvent, ...]:
        return tuple(self._events.values())

    def load(self, key: tuple[str, str, str | None, str]) -> ProtectionStateRecord | None:
        return self._records.get(key)

    def list_records(self, *, mode: str | None = None, account_id: str | None = None,
                     scenario_id: str | None = None) -> tuple[ProtectionStateRecord, ...]:
        return tuple(record for record in self._records.values()
                     if (mode is None or record.execution_mode == mode.upper())
                     and (account_id is None or record.account_id == account_id)
                     and (scenario_id is None or record.scenario_id == scenario_id))

    def commit(self, record: ProtectionStateRecord, events: tuple[_AuditEvent, ...]) -> None:
        for event in events:
            self._events.setdefault(event.event_id, event)
        self._records[record.key] = record


class ProtectionStateRepository:
    """Repository facade; supplied storage owns the app's single-writer transaction."""

    def __init__(self, storage: ProtectionStateStorage) -> None:
        if storage is None:
            raise TypeError("storage is required")
        self._storage = storage

    def save_event(self, record: ProtectionStateRecord, event: Mapping[str, Any]) -> ProtectionStateRecord:
        current = self._storage.load(record.key) or record
        updated, events = apply_event(current, event)
        if updated is not current:
            self._storage.commit(updated, events)
        return updated

    def load(self, *, mode: str, account_id: str, scenario_id: str | None, cycle_id: str) -> ProtectionStateRecord | None:
        return self._storage.load((mode.upper(), account_id, scenario_id, cycle_id))

    def list_records(self, *, mode: str | None = None, account_id: str | None = None,
                     scenario_id: str | None = None) -> tuple[ProtectionStateRecord, ...]:
        return self._storage.list_records(mode=mode, account_id=account_id, scenario_id=scenario_id)


def entry_block_reasons(
    records: Any, *, now: datetime | None = None, pending_grace_period: timedelta = timedelta(seconds=30)
) -> list[str]:
    """Return fresh-entry protection blocks only; has no exit or reconciliation API."""
    current = _utc(now if now is not None else datetime.now(UTC), "now")
    assert current is not None
    if pending_grace_period < timedelta(0):
        raise ValueError("pending_grace_period must be non-negative")
    reasons: list[str] = []
    for record in records:
        if not isinstance(record, ProtectionStateRecord):
            raise TypeError("records must contain ProtectionStateRecord values")
        condition = record.condition
        if condition is ProtectionCondition.UNPROTECTED:
            reasons.append(record.reason or "UNPROTECTED_POSITION")
        elif condition is ProtectionCondition.UNCONFIRMED:
            reasons.append("PROTECTION_UNCONFIRMED")
        elif condition is ProtectionCondition.EXPIRED and record.remaining_position_qty > _ZERO:
            reasons.append(record.reason or "PROTECTION_EXPIRED/OVERNIGHT")
        elif condition is ProtectionCondition.PENDING and record.pending_since is not None and current - record.pending_since > pending_grace_period:
            reasons.append("PROTECTION_PENDING")
    return list(dict.fromkeys(reasons))


def to_dashboard_view(
    record: ProtectionStateRecord, now: datetime, heartbeat_age: timedelta,
    *, offline_after: timedelta = timedelta(seconds=60),
) -> dict[str, Any]:
    """Read-only dashboard view; stale broker snapshot values remain visible."""
    _utc(now, "now")
    if heartbeat_age < timedelta(0) or offline_after < timedelta(0):
        raise ValueError("heartbeat ages must be non-negative")
    offline = heartbeat_age > offline_after
    return {
        "cycle_id": record.cycle_id,
        "account_id": record.account_id,
        "scenario_id": record.scenario_id,
        "symbol": record.symbol,
        "mode": record.mode,
        "execution_mode": record.execution_mode,
        "condition": ProtectionCondition.OFFLINE if offline else record.condition,
        "stale": offline,
        "broker_order_id": record.broker_order_id,
        "client_order_id": record.client_order_id,
        "protected_qty": record.protected_qty,
        "remaining_position_qty": record.remaining_position_qty,
        "stop_price": record.stop_price,
        "limit_price": record.limit_price,
        "trail_price": record.trail_price,
        "trail_percent": record.trail_percent,
        "broker_hwm": record.broker_hwm,
        "tif": record.tif,
        "last_broker_confirmed_at": record.last_broker_confirmed_at,
        "revision": record.revision,
        "reason": record.reason if not offline else record.reason or "ENGINE_OFFLINE",
    }
