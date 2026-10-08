"""Bounded, priority-aware event backlog for the engine scheduler.

The backlog deliberately owns classification.  Producers submit an event, not a
priority, so safety-sensitive event types cannot accidentally be made lossy.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic

from mike_trade.scheduling.scheduler import (
    DueEvaluation,
    ManualEvaluationRequest,
    MarketDataArrivalEvent,
    QuoteChangeEvent,
)

__all__ = [
    "EventClass",
    "EventBacklogStats",
    "EventBacklog",
    "BoundedEventBacklog",
    "classify_event",
]


class EventClass(StrEnum):
    CRITICAL = "CRITICAL"
    COALESCIBLE = "COALESCIBLE"


@dataclass(frozen=True, slots=True)
class EventBacklogStats:
    depth: int
    critical_depth: int
    coalescible_depth: int
    oldest_age_seconds: float
    coalesced: int
    discarded: int
    backpressure: int
    overflow: int


@dataclass(frozen=True, slots=True)
class _Queued:
    event: object
    enqueued_at: float


_COALESCIBLE_TYPES = (MarketDataArrivalEvent, QuoteChangeEvent, DueEvaluation,
                      ManualEvaluationRequest)
_CRITICAL_TYPE_NAMES = frozenset(
    {
        "FillEvent",
        "OrderUpdateEvent",
        "TradeUpdate",
        "BrokerOrderUpdate",
        "BrokerTradeUpdate",
        "ReconciliationEvent",
        "ReconciliationRequest",
        "ReconciliationDrift",
        "StopEvent",
        "StopTriggeredEvent",
        "ProtectiveEvent",
        "ProtectionEvent",
        "SafetyExitEvent",
        "ManualCloseEvent",
    }
)
_CRITICAL_NAME_PARTS = ("fill", "brokerorder", "brokertrade", "reconcil", "stop",
                        "protect", "safetyexit", "manualclose")


def classify_event(event: object) -> EventClass:
    """Classify from the concrete event type; callers cannot supply a class."""

    if isinstance(event, _COALESCIBLE_TYPES):
        return EventClass.COALESCIBLE

    type_name = type(event).__name__
    normalized = "".join(character for character in type_name.lower() if character.isalnum())
    if type_name in _CRITICAL_TYPE_NAMES or any(
        part in normalized for part in _CRITICAL_NAME_PARTS
    ):
        return EventClass.CRITICAL

    # Operator close/flatten/reconcile commands are critical.  Other control
    # commands are intentionally not guessed into either delivery class.
    command_type = getattr(event, "command_type", None)
    command_name = str(getattr(command_type, "value", command_type)).upper()
    if command_name in {"CLOSE_POSITION", "CLOSE_ALL", "FLATTEN", "RECONCILE_NOW"}:
        return EventClass.CRITICAL

    raise TypeError(f"unsupported scheduling event type: {type(event).__module__}.{type_name}")


def _text(value: object) -> str:
    return str(getattr(value, "value", value))


def _coalescing_key(event: object) -> tuple[str, str, str]:
    symbol = getattr(event, "symbol", None)
    strategy_id = getattr(event, "strategy_id", "*")
    side = getattr(event, "side", "*")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("coalescible events require a symbol")
    return (symbol.strip().upper(), _text(strategy_id), _text(side))


def _snapshot_version(event: object) -> int | None:
    version = getattr(event, "snapshot_version", None)
    if version is None:
        return None
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("coalescible events require a positive snapshot_version")
    return version


class EventBacklog:
    """Two-class bounded asynchronous backlog.

    Critical FIFO capacity and coalescible-key capacity are independently
    bounded.  Critical puts wait when full.  A coalescible put replaces the
    pending item for its key, while a new key waits if the key capacity is full.
    """

    def __init__(self, *, critical_maxsize: int = 128, coalescible_maxsize: int = 128) -> None:
        if isinstance(critical_maxsize, bool) or critical_maxsize < 1:
            raise ValueError("critical_maxsize must be a positive integer")
        if isinstance(coalescible_maxsize, bool) or coalescible_maxsize < 1:
            raise ValueError("coalescible_maxsize must be a positive integer")
        self._critical: asyncio.Queue[_Queued] = asyncio.Queue(maxsize=critical_maxsize)
        self._critical_enqueued: deque[float] = deque()
        self._coalescible_maxsize = coalescible_maxsize
        self._coalescible: OrderedDict[tuple[str, str, str], _Queued] = OrderedDict()
        self._watermarks: dict[str, int] = {}
        self._changed = asyncio.Event()
        self._space = asyncio.Condition()
        self._coalesced = 0
        self._discarded = 0
        self._backpressure = 0
        self._overflow = 0
        self._unfinished_tasks = 0
        self._finished = asyncio.Event()
        self._finished.set()

    @property
    def critical_maxsize(self) -> int:
        return self._critical.maxsize

    @property
    def coalescible_maxsize(self) -> int:
        return self._coalescible_maxsize

    @property
    def watermarks(self) -> dict[str, int]:
        return dict(self._watermarks)

    def watermark(self, symbol: str) -> int | None:
        return self._watermarks.get(symbol.strip().upper())

    def qsize(self) -> int:
        return self._critical.qsize() + len(self._coalescible)

    def empty(self) -> bool:
        return self.qsize() == 0

    @property
    def stats(self) -> EventBacklogStats:
        now = monotonic()
        timestamps = list(self._critical_enqueued)
        timestamps.extend(item.enqueued_at for item in self._coalescible.values())
        return EventBacklogStats(
            depth=self.qsize(),
            critical_depth=self._critical.qsize(),
            coalescible_depth=len(self._coalescible),
            oldest_age_seconds=max(0.0, now - min(timestamps)) if timestamps else 0.0,
            coalesced=self._coalesced,
            discarded=self._discarded,
            backpressure=self._backpressure,
            overflow=self._overflow,
        )

    def health(self) -> EventBacklogStats:
        return self.stats

    @property
    def depth(self) -> int:
        return self.qsize()

    @property
    def oldest_age_seconds(self) -> float:
        return self.stats.oldest_age_seconds

    @property
    def coalesced_count(self) -> int:
        return self._coalesced

    @property
    def discarded_count(self) -> int:
        return self._discarded

    @property
    def backpressure_count(self) -> int:
        return self._backpressure

    def _mark_unfinished(self) -> None:
        self._unfinished_tasks += 1
        self._finished.clear()

    async def put(self, event: object) -> bool:
        """Enqueue *event*; return False only for an obsolete snapshot."""

        event_class = classify_event(event)
        if event_class is EventClass.CRITICAL:
            if self._critical.full():
                self._backpressure += 1
                self._overflow += 1
            enqueued_at = monotonic()
            await self._critical.put(_Queued(event, enqueued_at))
            self._critical_enqueued.append(enqueued_at)
            self._mark_unfinished()
            self._changed.set()
            return True

        key = _coalescing_key(event)
        version = _snapshot_version(event)
        symbol = key[0]
        watermark = self._watermarks.get(symbol, 0)
        current = self._coalescible.get(key)
        current_version = _snapshot_version(current.event) if current is not None else None
        if version is not None and version < max(watermark, current_version or 0):
            self._discarded += 1
            return False
        if version is not None:
            self._watermarks[symbol] = max(watermark, version)

        if current is not None:
            if version is not None and current_version is not None and version <= current_version:
                self._discarded += 1
                return False
            self._coalescible[key] = _Queued(event, monotonic())
            self._coalesced += 1
            self._changed.set()
            return True

        async with self._space:
            if len(self._coalescible) >= self._coalescible_maxsize:
                self._backpressure += 1
                self._overflow += 1
            await self._space.wait_for(
                lambda: len(self._coalescible) < self._coalescible_maxsize
            )
            # Another producer may have installed this key while we waited.
            current = self._coalescible.get(key)
            if current is not None:
                current_version = _snapshot_version(current.event)
                if (version is not None and current_version is not None
                        and version <= current_version):
                    self._discarded += 1
                    return False
                self._coalesced += 1
            self._coalescible[key] = _Queued(event, monotonic())
            if current is None:
                self._mark_unfinished()
        self._changed.set()
        return True

    async def get(self) -> object:
        """Return the next item, always draining safety-critical FIFO work first."""

        while True:
            try:
                queued = self._critical.get_nowait()
                self._critical_enqueued.popleft()
                if self.empty():
                    self._changed.clear()
                return queued.event
            except asyncio.QueueEmpty:
                pass

            async with self._space:
                if self._coalescible:
                    _, queued = self._coalescible.popitem(last=False)
                    self._space.notify(1)
                    if self.empty():
                        self._changed.clear()
                    return queued.event

            self._changed.clear()
            if not self.empty():
                self._changed.set()
                continue
            await self._changed.wait()

    def task_done(self) -> None:
        """Mark one delivered item complete, regardless of its delivery class."""

        if self._unfinished_tasks <= 0:
            raise ValueError("task_done() called too many times")
        self._unfinished_tasks -= 1
        if self._unfinished_tasks == 0:
            self._finished.set()

    async def join_critical(self) -> None:
        await self._finished.wait()

    async def join(self) -> None:
        await self._finished.wait()


BoundedEventBacklog = EventBacklog
