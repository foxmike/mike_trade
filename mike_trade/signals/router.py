"""Single ordered decision router for strategy signals and safety-critical events.

Everything headed for the portfolio/order decision path enters here:

* strategy evaluation results from the i.2 ``EvaluationPool``, and
* CRITICAL events from the i.2 queueing layer (fills, broker order updates,
  reconciliation, stop/protective events, safety exits, manual close/flatten).

Serialization
-------------
Exactly one consumer mutates account, portfolio or order state at a time.  All
delivery to the injected consumer happens inside :meth:`DecisionRouter.drain`,
which holds one ``asyncio.Lock`` for the whole drain.  ``run()`` is a single
asyncio loop that only ever calls ``drain()``, so even if callers invoke
``drain()`` concurrently with ``run()`` the consumer is never entered twice.

Ordering
--------
Items are held in a heap and delivered highest priority first
(:class:`RoutePriority`; a lower number is delivered earlier).  Within one
priority the sort key is ``(event time UTC, snapshot_version, symbol,
strategy_id, arrival sequence)``.  The arrival sequence is assigned on
``submit_*`` and is the final tie-breaker, so identical input sets produce an
identical output order in any arrival order whenever the other keys differ.
Missing key parts sort first: no event time -> ``datetime.min`` UTC, no
snapshot version -> 0, no symbol/strategy -> ``""``.

Revalidation
------------
Strategy signals are revalidated at delivery time (not at submit time), using the
injected clock and :class:`FreshnessProvider`.  Critical events bypass
revalidation entirely: they are never dropped, expired or freshness-rejected.
ERROR/TIMEOUT/CANCELLED/STALE_DISCARDED (and NO_SIGNAL) results are recorded as
non-actionable and never reach the consumer.

The router never calls brokers, sizes orders or applies risk rules; it only
orders, revalidates and hands off.

``submit_*`` methods are synchronous and must be called from the event loop
thread (they are not thread-safe).
"""

from __future__ import annotations

import asyncio
import heapq
import inspect
import itertools
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from typing import Protocol

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.scheduling.queueing import EventClass, classify_event
from mike_trade.scheduling.scheduler import BarKey
from mike_trade.scheduling.workers import EvaluationOutcome, EvaluationResult

__all__ = [
    "RoutePriority",
    "RouteKind",
    "RouteStatus",
    "RejectReason",
    "RoutedDecision",
    "RoutingRecord",
    "FreshnessProvider",
    "ClockLike",
    "DecisionConsumer",
    "DecisionRouterClosedError",
    "DecisionRouter",
]

_MIN_TIME = datetime.min.replace(tzinfo=UTC)
_EVENT_TIME_ATTRS = ("event_time", "occurred_at", "timestamp", "updated_at", "filled_at", "due_at")


class RoutePriority(IntEnum):
    """Delivery priority; the lowest value is delivered first."""

    FILL_OR_BROKER_UPDATE = 1
    RECONCILIATION = 2
    PROTECTIVE = 3
    MANUAL_CLOSE = 4
    SELL_SIGNAL = 5
    BUY_SIGNAL = 6
    NON_ACTIONABLE = 7  # recorded only, never delivered


class RouteKind(StrEnum):
    CRITICAL_EVENT = "CRITICAL_EVENT"
    STRATEGY_SIGNAL = "STRATEGY_SIGNAL"


class RouteStatus(StrEnum):
    FORWARDED = "FORWARDED"
    REJECTED = "REJECTED"
    NON_ACTIONABLE = "NON_ACTIONABLE"
    DUPLICATE = "DUPLICATE"


class RejectReason(StrEnum):
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"
    STALE_DATA = "STALE_DATA"


class ClockLike(Protocol):
    def now(self) -> datetime: ...


class FreshnessProvider(Protocol):
    """Read-only view of the latest market-data snapshot per symbol."""

    def latest_snapshot_version(self, symbol: str) -> int | None: ...

    def newest_data_ts(self, symbol: str) -> datetime | None: ...


@dataclass(frozen=True, slots=True)
class RoutedDecision:
    """What the consumer receives.

    ``payload`` is the original critical event, or the ``EvaluationResult`` of an
    actionable strategy signal (``payload.side`` is the BUY/SELL role).
    """

    kind: RouteKind
    priority: RoutePriority
    sequence: int
    payload: object


@dataclass(frozen=True, slots=True)
class RoutingRecord:
    """Audit entry for every item the router has decided."""

    sequence: int
    kind: RouteKind
    priority: RoutePriority
    symbol: str
    strategy_id: str
    status: RouteStatus
    reason: str | None
    decided_at: datetime


type DecisionConsumer = Callable[[RoutedDecision], Awaitable[None] | None]


class DecisionRouterClosedError(RuntimeError):
    """Raised when an item is submitted after ``stop()``."""


@dataclass(frozen=True, slots=True)
class _Entry:
    kind: RouteKind
    priority: RoutePriority
    sequence: int
    symbol: str
    strategy_id: str
    payload: object
    dedupe_key: tuple[object, ...] | None = field(default=None)


def _aware_utc(value: object) -> datetime | None:
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(UTC)
    return None


def _event_time(event: object) -> datetime:
    for name in _EVENT_TIME_ATTRS:
        stamp = _aware_utc(getattr(event, name, None))
        if stamp is not None:
            return stamp
    return _MIN_TIME


def _normalized_name(event: object) -> str:
    return "".join(c for c in type(event).__name__.lower() if c.isalnum())


def _critical_priority(event: object) -> RoutePriority:
    name = _normalized_name(event)
    if any(part in name for part in ("fill", "orderupdate", "tradeupdate", "brokerorder",
                                     "brokertrade")):
        return RoutePriority.FILL_OR_BROKER_UPDATE
    if "reconcil" in name:
        return RoutePriority.RECONCILIATION
    if any(part in name for part in ("stop", "protect", "safetyexit")):
        return RoutePriority.PROTECTIVE
    if "manualclose" in name:
        return RoutePriority.MANUAL_CLOSE

    command_type = getattr(event, "command_type", None)
    command = str(getattr(command_type, "value", command_type)).upper()
    if command == "RECONCILE_NOW":
        return RoutePriority.RECONCILIATION
    if command in {"CLOSE_POSITION", "CLOSE_ALL", "FLATTEN"}:
        return RoutePriority.MANUAL_CLOSE
    # classify_event() said CRITICAL; if we cannot place it, deliver it first
    # rather than let it be starved.
    return RoutePriority.FILL_OR_BROKER_UPDATE


def _text(value: object) -> str:
    return value.strip().upper() if isinstance(value, str) else ""


def _version(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


class DecisionRouter:
    """Orders, revalidates and hands off decisions to one serialized consumer."""

    def __init__(
        self,
        *,
        consumer: DecisionConsumer,
        clock: ClockLike,
        freshness: FreshnessProvider,
        max_data_age: timedelta,
        record_limit: int = 10_000,
        dedupe_capacity: int = 10_000,
    ) -> None:
        if not callable(consumer):
            raise TypeError("consumer must be callable")
        if not isinstance(max_data_age, timedelta) or max_data_age <= timedelta(0):
            raise ValueError("max_data_age must be a positive timedelta")
        for name, value in (("record_limit", record_limit), ("dedupe_capacity", dedupe_capacity)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._consumer = consumer
        self._clock = clock
        self._freshness = freshness
        self._max_data_age = max_data_age
        self._dedupe_capacity = dedupe_capacity
        self._heap: list[tuple[tuple[object, ...], _Entry]] = []
        self._sequence = itertools.count(1)
        self._lock = asyncio.Lock()  # the single-consumer guard; see module docstring
        self._wake = asyncio.Event()
        self._closed = False
        self._forwarded_keys: OrderedDict[tuple[object, ...], None] = OrderedDict()
        self._records: deque[RoutingRecord] = deque(maxlen=record_limit)

    # ------------------------------------------------------------------ inspection

    @property
    def pending(self) -> int:
        return len(self._heap)

    @property
    def records(self) -> tuple[RoutingRecord, ...]:
        return tuple(self._records)

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------ submission

    def submit_event(self, event: object) -> int:
        """Queue a CRITICAL event.  Returns its arrival sequence number.

        Only events the i.2 queueing layer classifies as CRITICAL are accepted.
        """

        if classify_event(event) is not EventClass.CRITICAL:
            raise TypeError(f"{type(event).__name__} is not a critical event")
        priority = _critical_priority(event)
        symbol = _text(getattr(event, "symbol", None))
        strategy_id = str(getattr(event, "strategy_id", "") or "").strip()
        sort_key = (
            _event_time(event),
            _version(getattr(event, "snapshot_version", None)),
        )
        return self._push(
            RouteKind.CRITICAL_EVENT, priority, symbol, strategy_id, event, sort_key
        )

    def submit_evaluation(
        self, result: EvaluationResult, *, bar_key: BarKey | None = None
    ) -> int:
        """Queue an evaluation result.  Returns its arrival sequence number.

        ``bar_key`` (``DueEvaluation.bar_key``) identifies the bar for duplicate
        suppression; otherwise the signal's ``bar_time`` and then the result's
        ``snapshot_version`` are used.  With none of these, no suppression applies.
        """

        if not isinstance(result, EvaluationResult):
            raise TypeError("result must be EvaluationResult")
        symbol = _text(result.symbol)
        strategy_id = result.strategy_id.strip()
        if result.outcome is not EvaluationOutcome.SIGNAL:
            priority = RoutePriority.NON_ACTIONABLE
            dedupe_key = None
        else:
            priority = (
                RoutePriority.SELL_SIGNAL
                if result.side is StrategyRole.SELL
                else RoutePriority.BUY_SIGNAL
            )
            bar: object = bar_key
            if bar is None:
                bar = getattr(result.signal, "bar_time", None)
            if bar is None and result.snapshot_version is not None:
                bar = ("snapshot", result.snapshot_version)
            dedupe_key = (
                None if bar is None else (strategy_id, result.side.value, symbol, bar)
            )
        sort_key = (result.finished_at, _version(result.snapshot_version))
        return self._push(
            RouteKind.STRATEGY_SIGNAL, priority, symbol, strategy_id, result, sort_key,
            dedupe_key,
        )

    def _push(
        self,
        kind: RouteKind,
        priority: RoutePriority,
        symbol: str,
        strategy_id: str,
        payload: object,
        time_and_version: tuple[object, ...],
        dedupe_key: tuple[object, ...] | None = None,
    ) -> int:
        if self._closed:
            raise DecisionRouterClosedError("decision router is stopped")
        sequence = next(self._sequence)
        entry = _Entry(kind, priority, sequence, symbol, strategy_id, payload, dedupe_key)
        sort_key = (int(priority), *time_and_version, symbol, strategy_id, sequence)
        heapq.heappush(self._heap, (sort_key, entry))
        self._wake.set()
        return sequence

    # ------------------------------------------------------------------ delivery

    async def drain(self) -> int:
        """Deliver every queued item in priority order; return how many were decided.

        Items submitted while draining are merged into the ordering.  If the
        consumer raises, the exception propagates; the failed item is not
        retried and the remaining items stay queued.
        """

        async with self._lock:
            decided = 0
            while self._heap:
                _, entry = heapq.heappop(self._heap)
                await self._decide(entry)
                decided += 1
            self._wake.clear()
            return decided

    async def run(self) -> None:
        """Single consumer loop: drain whenever work arrives, until ``stop()``.

        Remaining items are drained before returning.
        """

        while True:
            await self.drain()
            if self._closed:
                return
            await self._wake.wait()

    def stop(self) -> None:
        """Refuse new submissions and let ``run()`` finish after draining."""

        self._closed = True
        self._wake.set()

    async def _decide(self, entry: _Entry) -> None:
        if entry.kind is RouteKind.CRITICAL_EVENT:
            # Never revalidated, expired or dropped.
            status, reason = RouteStatus.FORWARDED, None
        else:
            status, reason = self._decide_signal(entry)
        self._record(entry, status, reason)
        if status is RouteStatus.FORWARDED:
            outcome = self._consumer(
                RoutedDecision(entry.kind, entry.priority, entry.sequence, entry.payload)
            )
            if inspect.isawaitable(outcome):
                await outcome

    def _decide_signal(self, entry: _Entry) -> tuple[RouteStatus, str | None]:
        result = entry.payload
        assert isinstance(result, EvaluationResult)
        if result.outcome is not EvaluationOutcome.SIGNAL:
            return RouteStatus.NON_ACTIONABLE, result.outcome.value
        key = entry.dedupe_key
        if key is not None and key in self._forwarded_keys:
            return RouteStatus.DUPLICATE, None
        rejected = self._revalidate(result)
        if rejected is not None:
            return RouteStatus.REJECTED, rejected.value
        if key is not None:
            self._forwarded_keys[key] = None
            while len(self._forwarded_keys) > self._dedupe_capacity:
                self._forwarded_keys.popitem(last=False)
        return RouteStatus.FORWARDED, None

    def _revalidate(self, result: EvaluationResult) -> RejectReason | None:
        now = _aware_utc(self._clock.now())
        if now is None:
            raise ValueError("clock.now() must return a timezone-aware datetime")
        symbol = _text(result.symbol)

        for name in ("expires_at", "expiry"):
            raw = getattr(result.signal, name, None)
            if raw is None:
                continue
            expires_at = _aware_utc(raw)
            # An unusable expiry cannot be proven valid, so fail closed.
            if expires_at is None or now > expires_at:
                return RejectReason.EXPIRED
            break

        latest = self._freshness.latest_snapshot_version(symbol)
        if (
            result.snapshot_version is not None
            and latest is not None
            and latest > result.snapshot_version
        ):
            return RejectReason.SUPERSEDED

        data_ts = _aware_utc(self._freshness.newest_data_ts(symbol))
        if data_ts is None or now - data_ts > self._max_data_age:
            return RejectReason.STALE_DATA
        return None

    def _record(self, entry: _Entry, status: RouteStatus, reason: str | None) -> None:
        decided_at = _aware_utc(self._clock.now()) or _MIN_TIME
        self._records.append(
            RoutingRecord(
                sequence=entry.sequence,
                kind=entry.kind,
                priority=entry.priority,
                symbol=entry.symbol,
                strategy_id=entry.strategy_id,
                status=status,
                reason=reason,
                decided_at=decided_at,
            )
        )
