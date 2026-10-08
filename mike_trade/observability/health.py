"""Engine health model: state, immutable snapshot, liveness, sink port.

The dashboard reuses :func:`classify_liveness` to decide OFFLINE independently,
without engine cooperation.  Health is published through an injected
``HealthSink`` port (for example the persistence/projection writer); this
module never opens a database connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

__all__ = [
    "EngineState",
    "EngineMode",
    "Liveness",
    "HealthSnapshot",
    "HealthSink",
    "classify_liveness",
]


class EngineState(StrEnum):
    """Lifecycle states of the engine runner."""

    STARTING = "STARTING"
    RECONCILING = "RECONCILING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    DEGRADED = "DEGRADED"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"


class EngineMode(StrEnum):
    """Execution modes carried on every heartbeat."""

    BACKTEST = "BACKTEST"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE = "LIVE"


class Liveness(StrEnum):
    """Heartbeat-age classification reused by the dashboard."""

    ONLINE = "ONLINE"
    STALE = "STALE"
    OFFLINE = "OFFLINE"


def _require_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _require_nonempty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """One immutable engine heartbeat.

    ``sequence`` is a monotonic counter assigned by the engine.  ``feed_freshness``
    is one of ``FRESH`` / ``STALE`` / ``UNKNOWN``.  ``scheduler_status``,
    ``reconciliation_status`` and ``protective_stop_status`` are short opaque
    status strings supplied by those components.  Pool depth and backpressure
    counters plus the router backlog expose saturation for DEGRADED decisions.
    """

    state: EngineState
    mode: EngineMode
    account_scope_id: str
    heartbeat_at: datetime
    sequence: int
    entries_allowed: bool
    entry_block_reasons: tuple[str, ...] = ()
    feed_freshness: str = "UNKNOWN"
    scheduler_status: str = "UNKNOWN"
    pool_depth: int = 0
    pool_backpressure: int = 0
    pool_overflow: int = 0
    router_backlog: int = 0
    reconciliation_status: str = "UNKNOWN"
    protective_stop_status: str = "UNKNOWN"
    last_error: str | None = field(default=None)

    def __post_init__(self) -> None:
        if not isinstance(self.state, EngineState):
            raise ValueError(f"state must be EngineState; got {type(self.state).__name__}")
        if not isinstance(self.mode, EngineMode):
            raise ValueError(f"mode must be EngineMode; got {type(self.mode).__name__}")
        object.__setattr__(
            self, "account_scope_id", _require_nonempty(self.account_scope_id, "account_scope_id")
        )
        object.__setattr__(self, "heartbeat_at", _require_utc(self.heartbeat_at, "heartbeat_at"))
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise ValueError("sequence must be an integer")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if not isinstance(self.entries_allowed, bool):
            raise ValueError("entries_allowed must be bool")
        if not isinstance(self.entry_block_reasons, tuple) or any(
            not isinstance(reason, str) or not reason.strip()
            for reason in self.entry_block_reasons
        ):
            raise ValueError("entry_block_reasons must be a tuple of non-empty strings")
        object.__setattr__(
            self, "entry_block_reasons", tuple(r.strip() for r in self.entry_block_reasons)
        )
        for name in ("feed_freshness", "scheduler_status", "reconciliation_status",
                     "protective_stop_status"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("pool_depth", "pool_backpressure", "pool_overflow", "router_backlog"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.last_error is not None and not isinstance(self.last_error, str):
            raise ValueError("last_error must be str or None")


class HealthSink(Protocol):
    """Port that receives heartbeats (for example the persistence writer)."""

    def publish(self, snapshot: HealthSnapshot) -> object:
        """Receive one heartbeat; may return an awaitable."""
        ...


def classify_liveness(
    snapshot: HealthSnapshot | None,
    now: datetime,
    max_heartbeat_age: timedelta,
) -> Liveness:
    """Classify liveness purely from heartbeat age.

    Thresholds (documented for dashboard reuse):

    * ``OFFLINE`` when there is no snapshot, no heartbeat timestamp, or the
      heartbeat age is strictly greater than ``2 * max_heartbeat_age``.
    * ``STALE`` when the age is strictly greater than ``max_heartbeat_age``
      but within twice it.
    * ``ONLINE`` otherwise (age at or under the threshold; future heartbeats
      count as fresh).

    Pure function of ``(snapshot, now, max_heartbeat_age)``; it performs no
    I/O and consults no engine state.
    """

    current = _require_utc(now, "now")
    if not isinstance(max_heartbeat_age, timedelta) or max_heartbeat_age <= timedelta(0):
        raise ValueError("max_heartbeat_age must be a positive timedelta")
    if snapshot is None or not isinstance(snapshot, HealthSnapshot):
        return Liveness.OFFLINE
    heartbeat = snapshot.heartbeat_at
    if not isinstance(heartbeat, datetime) or heartbeat.tzinfo is None:
        return Liveness.OFFLINE
    age = current - heartbeat.astimezone(UTC)
    if age <= timedelta(0):
        return Liveness.ONLINE
    if age <= max_heartbeat_age:
        return Liveness.ONLINE
    if age <= max_heartbeat_age * 2:
        return Liveness.STALE
    return Liveness.OFFLINE
