"""Deterministic, engine-owned strategy due scheduler.

The scheduler is an in-memory router.  It owns no threads, creates no asyncio
tasks, and performs no strategy, broker, provider, or order work.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.scheduling import calendar as exchange_calendar
from mike_trade.scheduling.calendar import Session
from mike_trade.scheduling.triggers import StrategyTrigger, TriggerKind

__all__ = [
    "BarKey",
    "DueEvaluation",
    "StrategySchedule",
    "MarketDataArrivalEvent",
    "QuoteChangeEvent",
    "CandleClosedEvent",
    "SessionEvent",
    "ControlTick",
    "ManualEvaluationRequest",
    "SchedulingEvent",
    "StrategyScheduler",
    "Scheduler",
    "DueScheduler",
]


class ClockLike(Protocol):
    def now(self) -> datetime: ...


class CalendarLike(Protocol):
    def session_for(self, day: date) -> Session: ...

    def entry_cutoff(self, session: Session, minutes_before_close: int) -> datetime: ...


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _symbol(value: object) -> str:
    text = value.strip().upper() if isinstance(value, str) else ""
    if not text or any(character.isspace() for character in text):
        raise ValueError("symbol must be a non-empty string without whitespace")
    return text


def _identifier(value: object, field: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ValueError(f"{field} must be a non-empty string")
    return text


def _side(value: object) -> StrategyRole:
    if isinstance(value, StrategyRole):
        return value
    if isinstance(value, str):
        try:
            return StrategyRole(value.strip().upper())
        except ValueError:
            pass
    raise ValueError("side must be StrategyRole.BUY or StrategyRole.SELL")


def _version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("snapshot_version must be a positive integer")
    return value


def _timeframe(value: object) -> CandleTimeframe:
    if isinstance(value, CandleTimeframe):
        return value
    if isinstance(value, str):
        try:
            return CandleTimeframe(value.strip().lower())
        except ValueError:
            pass
    raise ValueError(f"unsupported candle timeframe {value!r}")


def _price(value: object, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be numeric") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return result


@dataclass(frozen=True, slots=True)
class BarKey:
    """Identity of one closed h.1 candle."""

    symbol: str
    timeframe: CandleTimeframe | str
    end_utc: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "timeframe", _timeframe(self.timeframe))
        object.__setattr__(self, "end_utc", _utc(self.end_utc, "end_utc"))


@dataclass(frozen=True, slots=True)
class DueEvaluation:
    """An immutable instruction to evaluate one strategy role once."""

    symbol: str
    strategy_id: str
    side: StrategyRole
    trigger_kind: TriggerKind
    snapshot_version: int | None
    bar_key: BarKey | None
    due_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "strategy_id", _identifier(self.strategy_id, "strategy_id"))
        object.__setattr__(self, "side", _side(self.side))
        if not isinstance(self.trigger_kind, TriggerKind):
            try:
                kind = TriggerKind(str(self.trigger_kind).strip().upper())
            except ValueError:
                raise ValueError(f"unknown trigger kind {self.trigger_kind!r}") from None
            object.__setattr__(self, "trigger_kind", kind)
        if self.snapshot_version is not None:
            object.__setattr__(self, "snapshot_version", _version(self.snapshot_version))
        if self.bar_key is not None and not isinstance(self.bar_key, BarKey):
            raise ValueError("bar_key must be BarKey or None")
        object.__setattr__(self, "due_at", _utc(self.due_at, "due_at"))


@dataclass(frozen=True, slots=True)
class StrategySchedule:
    """One configured strategy role in the scheduler registry."""

    symbol: str
    strategy_id: str
    side: StrategyRole
    triggers: frozenset[StrategyTrigger] | Iterable[StrategyTrigger]
    enabled: bool = True
    paused: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "strategy_id", _identifier(self.strategy_id, "strategy_id"))
        object.__setattr__(self, "side", _side(self.side))
        try:
            triggers = frozenset(self.triggers)
        except TypeError:
            raise ValueError("triggers must be an iterable of StrategyTrigger values") from None
        if not triggers or any(not isinstance(trigger, StrategyTrigger) for trigger in triggers):
            raise ValueError("triggers must contain at least one StrategyTrigger")
        object.__setattr__(self, "triggers", triggers)
        if not isinstance(self.enabled, bool) or not isinstance(self.paused, bool):
            raise ValueError("enabled and paused must be bool")


@dataclass(frozen=True, slots=True)
class MarketDataArrivalEvent:
    symbol: str
    snapshot_version: int
    due_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "snapshot_version", _version(self.snapshot_version))
        if self.due_at is not None:
            object.__setattr__(self, "due_at", _utc(self.due_at, "due_at"))


@dataclass(frozen=True, slots=True)
class QuoteChangeEvent:
    symbol: str
    bid: Decimal | int | float | str
    ask: Decimal | int | float | str
    snapshot_version: int
    due_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "bid", _price(self.bid, "bid"))
        object.__setattr__(self, "ask", _price(self.ask, "ask"))
        object.__setattr__(self, "snapshot_version", _version(self.snapshot_version))
        if self.due_at is not None:
            object.__setattr__(self, "due_at", _utc(self.due_at, "due_at"))


@dataclass(frozen=True, slots=True)
class CandleClosedEvent:
    symbol: str
    timeframe: CandleTimeframe | str
    bar_end_utc: datetime
    snapshot_version: int | None = None
    due_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "timeframe", _timeframe(self.timeframe))
        object.__setattr__(self, "bar_end_utc", _utc(self.bar_end_utc, "bar_end_utc"))
        if self.snapshot_version is not None:
            object.__setattr__(self, "snapshot_version", _version(self.snapshot_version))
        if self.due_at is not None:
            object.__setattr__(self, "due_at", _utc(self.due_at, "due_at"))


_SESSION_KINDS = frozenset(
    {TriggerKind.SESSION_OPEN, TriggerKind.ENTRY_CUTOFF, TriggerKind.SESSION_CLOSE}
)


@dataclass(frozen=True, slots=True)
class SessionEvent:
    kind: TriggerKind | str
    session_id: str
    due_at: datetime

    def __post_init__(self) -> None:
        try:
            kind = self.kind if isinstance(self.kind, TriggerKind) else TriggerKind(str(self.kind))
        except ValueError:
            raise ValueError(f"unknown session trigger kind {self.kind!r}") from None
        if kind not in _SESSION_KINDS:
            raise ValueError(f"{kind.value} is not a session trigger")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "session_id", _identifier(self.session_id, "session_id"))
        object.__setattr__(self, "due_at", _utc(self.due_at, "due_at"))


@dataclass(frozen=True, slots=True)
class ControlTick:
    """Request a cadence check using the scheduler's injected clock."""


@dataclass(frozen=True, slots=True)
class ManualEvaluationRequest:
    symbol: str
    strategy_id: str
    side: StrategyRole | str
    request_id: str | None = None
    due_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "strategy_id", _identifier(self.strategy_id, "strategy_id"))
        object.__setattr__(self, "side", _side(self.side))
        if self.request_id is not None:
            object.__setattr__(self, "request_id", _identifier(self.request_id, "request_id"))
        if self.due_at is not None:
            object.__setattr__(self, "due_at", _utc(self.due_at, "due_at"))


type SchedulingEvent = (
    MarketDataArrivalEvent
    | QuoteChangeEvent
    | CandleClosedEvent
    | SessionEvent
    | ControlTick
    | ManualEvaluationRequest
)


class _DefaultCalendar:
    def session_for(self, day: date) -> Session:
        return exchange_calendar.session_for(day)

    def entry_cutoff(self, session: Session, minutes_before_close: int) -> datetime:
        return exchange_calendar.entry_cutoff(session, minutes_before_close)


_PRIORITY = {
    TriggerKind.SESSION_OPEN: 0,
    TriggerKind.ENTRY_CUTOFF: 0,
    TriggerKind.SESSION_CLOSE: 0,
    TriggerKind.CANDLE_CLOSED: 1,
    TriggerKind.QUOTE_CHANGE: 2,
    TriggerKind.MARKET_DATA_ARRIVAL: 2,
    TriggerKind.INTERVAL: 3,
    TriggerKind.MANUAL: 4,
}

_RegistryKey = tuple[str, str, StrategyRole]
_SeenKey = tuple[_RegistryKey, TriggerKind, object]


class StrategyScheduler:
    """One deterministic scheduler shared by every symbol and strategy.

    The synchronous :meth:`process` API is useful to an engine event loop that
    already serializes inputs.  :meth:`handle` provides an asyncio-safe entry
    point using one scheduler-level lock; neither API creates background tasks.
    """

    def __init__(
        self,
        clock: ClockLike,
        *,
        calendar: CalendarLike | None = None,
        watched_symbols: Iterable[str] = (),
        entry_cutoff_minutes: int = 30,
    ) -> None:
        if not hasattr(clock, "now"):
            raise TypeError("clock must provide now()")
        if (
            isinstance(entry_cutoff_minutes, bool)
            or not isinstance(entry_cutoff_minutes, int)
            or entry_cutoff_minutes < 0
        ):
            raise ValueError("entry_cutoff_minutes must be a non-negative integer")
        self._clock = clock
        self._calendar: CalendarLike = calendar or _DefaultCalendar()
        self._entry_cutoff_minutes = entry_cutoff_minutes
        self._watched = {_symbol(symbol) for symbol in watched_symbols}
        self._registry: dict[_RegistryKey, StrategySchedule] = {}
        self._registered_at: dict[_RegistryKey, datetime] = {}
        self._next_interval: dict[tuple[_RegistryKey, StrategyTrigger], datetime] = {}
        self._latest_snapshot: dict[str, int] = {}
        self._quotes: dict[str, tuple[Decimal, Decimal]] = {}
        self._seen: set[_SeenKey] = set()
        self._last_control_at = self._now()
        self._manual_sequence = 0
        self._lock = asyncio.Lock()

    def _now(self) -> datetime:
        return _utc(self._clock.now(), "clock.now()")

    @property
    def watched_symbols(self) -> frozenset[str]:
        return frozenset(self._watched)

    @property
    def registrations(self) -> tuple[StrategySchedule, ...]:
        return tuple(sorted(self._registry.values(), key=self._registration_sort_key))

    @staticmethod
    def _registration_sort_key(item: StrategySchedule) -> tuple[str, str, str]:
        return item.symbol, item.strategy_id, item.side.value

    def watch(self, symbol: str) -> None:
        self._watched.add(_symbol(symbol))

    watch_symbol = watch

    def unwatch(self, symbol: str) -> None:
        self._watched.discard(_symbol(symbol))

    unwatch_symbol = unwatch

    def register(
        self,
        schedule: StrategySchedule | None = None,
        *,
        symbol: str | None = None,
        strategy_id: str | None = None,
        side: StrategyRole | str | None = None,
        triggers: Iterable[StrategyTrigger] | None = None,
        enabled: bool = True,
        paused: bool = False,
    ) -> StrategySchedule:
        if schedule is None:
            if symbol is None or strategy_id is None or side is None or triggers is None:
                raise TypeError("register requires a schedule or symbol/strategy_id/side/triggers")
            schedule = StrategySchedule(
                symbol=symbol,
                strategy_id=strategy_id,
                side=_side(side),
                triggers=triggers,
                enabled=enabled,
                paused=paused,
            )
        elif any(value is not None for value in (symbol, strategy_id, side, triggers)):
            raise TypeError("schedule cannot be combined with registration fields")
        elif not isinstance(schedule, StrategySchedule):
            raise TypeError("schedule must be StrategySchedule")

        key = (schedule.symbol, schedule.strategy_id, schedule.side)
        now = self._now()
        self._registry[key] = schedule
        self._registered_at[key] = now
        for interval_key in tuple(self._next_interval):
            if interval_key[0] == key:
                del self._next_interval[interval_key]
        for trigger in schedule.triggers:
            if trigger.kind is TriggerKind.INTERVAL:
                self._next_interval[(key, trigger)] = now + timedelta(
                    seconds=trigger.interval_seconds  # type: ignore[arg-type]
                )
        return schedule

    register_strategy = register

    def unregister(self, symbol: str, strategy_id: str, side: StrategyRole | str) -> None:
        key = (_symbol(symbol), _identifier(strategy_id, "strategy_id"), _side(side))
        self._registry.pop(key, None)
        self._registered_at.pop(key, None)
        for interval_key in tuple(self._next_interval):
            if interval_key[0] == key:
                del self._next_interval[interval_key]

    def set_state(
        self,
        symbol: str,
        strategy_id: str,
        side: StrategyRole | str,
        *,
        enabled: bool | None = None,
        paused: bool | None = None,
    ) -> StrategySchedule:
        key = (_symbol(symbol), _identifier(strategy_id, "strategy_id"), _side(side))
        current = self._registry[key]
        if enabled is not None and not isinstance(enabled, bool):
            raise ValueError("enabled must be bool")
        if paused is not None and not isinstance(paused, bool):
            raise ValueError("paused must be bool")
        updated = replace(
            current,
            enabled=current.enabled if enabled is None else enabled,
            paused=current.paused if paused is None else paused,
        )
        self._registry[key] = updated
        return updated

    def process(self, event: SchedulingEvent) -> list[DueEvaluation]:
        """Process one input and return a deterministically ordered due list."""

        return self.process_many((event,))

    def on_market_data_arrival(
        self, symbol: str, snapshot_version: int, due_at: datetime | None = None
    ) -> list[DueEvaluation]:
        return self.process(MarketDataArrivalEvent(symbol, snapshot_version, due_at))

    def on_quote_change(
        self,
        symbol: str,
        bid: Decimal | int | float | str,
        ask: Decimal | int | float | str,
        snapshot_version: int,
        due_at: datetime | None = None,
    ) -> list[DueEvaluation]:
        return self.process(QuoteChangeEvent(symbol, bid, ask, snapshot_version, due_at))

    def on_candle_closed(
        self,
        symbol: str,
        timeframe: CandleTimeframe | str,
        bar_end_utc: datetime,
        snapshot_version: int | None = None,
        due_at: datetime | None = None,
    ) -> list[DueEvaluation]:
        return self.process(
            CandleClosedEvent(
                symbol, timeframe, bar_end_utc, snapshot_version, due_at
            )
        )

    def on_session_event(
        self,
        kind: TriggerKind | str,
        session_id: str,
        due_at: datetime,
    ) -> list[DueEvaluation]:
        return self.process(SessionEvent(kind, session_id, due_at))

    def on_control_tick(self) -> list[DueEvaluation]:
        return self.process(ControlTick())

    def request_manual(
        self,
        symbol: str,
        strategy_id: str,
        side: StrategyRole | str,
        *,
        request_id: str | None = None,
        due_at: datetime | None = None,
    ) -> list[DueEvaluation]:
        return self.process(
            ManualEvaluationRequest(symbol, strategy_id, side, request_id, due_at)
        )

    def process_many(self, events: Iterable[SchedulingEvent]) -> list[DueEvaluation]:
        """Process inputs in their given order, then order all resulting records."""

        due: list[DueEvaluation] = []
        for event in events:
            due.extend(self._process_one(event))
        due.sort(key=self.sort_key)
        return due

    async def handle(self, event: SchedulingEvent) -> list[DueEvaluation]:
        """Asyncio-safe event entry point using one component-wide lock."""

        async with self._lock:
            return self.process(event)

    async def handle_many(self, events: Iterable[SchedulingEvent]) -> list[DueEvaluation]:
        async with self._lock:
            return self.process_many(events)

    @staticmethod
    def sort_key(item: DueEvaluation) -> tuple[datetime, int, str, str, str]:
        return (
            item.due_at,
            _PRIORITY[item.trigger_kind],
            item.symbol,
            item.strategy_id,
            item.side.value,
        )

    def _process_one(self, event: SchedulingEvent) -> list[DueEvaluation]:
        if isinstance(event, MarketDataArrivalEvent):
            self._latest_snapshot[event.symbol] = event.snapshot_version
            return self._emit_for_kind(
                event.symbol,
                TriggerKind.MARKET_DATA_ARRIVAL,
                event.due_at or self._now(),
                snapshot_version=event.snapshot_version,
                discriminator=event.snapshot_version,
            )
        if isinstance(event, QuoteChangeEvent):
            self._latest_snapshot[event.symbol] = event.snapshot_version
            quote = (cast(Decimal, event.bid), cast(Decimal, event.ask))
            previous = self._quotes.get(event.symbol)
            self._quotes[event.symbol] = quote
            if quote == previous:
                return []
            return self._emit_for_kind(
                event.symbol,
                TriggerKind.QUOTE_CHANGE,
                event.due_at or self._now(),
                snapshot_version=event.snapshot_version,
                discriminator=event.snapshot_version,
            )
        if isinstance(event, CandleClosedEvent):
            if event.snapshot_version is not None:
                self._latest_snapshot[event.symbol] = event.snapshot_version
            bar_key = BarKey(event.symbol, event.timeframe, event.bar_end_utc)
            return self._emit_for_kind(
                event.symbol,
                TriggerKind.CANDLE_CLOSED,
                event.due_at or event.bar_end_utc,
                snapshot_version=event.snapshot_version
                if event.snapshot_version is not None
                else self._latest_snapshot.get(event.symbol),
                bar_key=bar_key,
                discriminator=bar_key,
                timeframe=cast(CandleTimeframe, event.timeframe),
            )
        if isinstance(event, SessionEvent):
            return self._emit_all_symbols(
                cast(TriggerKind, event.kind),
                event.due_at,
                discriminator=event.session_id,
            )
        if isinstance(event, ControlTick):
            return self._control_tick()
        if isinstance(event, ManualEvaluationRequest):
            return self._manual(event)
        raise TypeError(f"unsupported scheduling event {type(event).__name__}")

    def _eligible(self, schedule: StrategySchedule) -> bool:
        return (
            schedule.symbol in self._watched and schedule.enabled and not schedule.paused
        )

    def _emit_for_kind(
        self,
        symbol: str,
        kind: TriggerKind,
        due_at: datetime,
        *,
        discriminator: object,
        snapshot_version: int | None = None,
        bar_key: BarKey | None = None,
        timeframe: CandleTimeframe | None = None,
    ) -> list[DueEvaluation]:
        due: list[DueEvaluation] = []
        for key, schedule in sorted(
            self._registry.items(), key=lambda item: self._registration_sort_key(item[1])
        ):
            if schedule.symbol != symbol or not self._eligible(schedule):
                continue
            matching = [trigger for trigger in schedule.triggers if trigger.kind is kind]
            if timeframe is not None:
                matching = [trigger for trigger in matching if trigger.timeframe is timeframe]
            if not matching:
                continue
            seen_key = (key, kind, discriminator)
            if seen_key in self._seen:
                continue
            self._seen.add(seen_key)
            due.append(
                DueEvaluation(
                    symbol=schedule.symbol,
                    strategy_id=schedule.strategy_id,
                    side=schedule.side,
                    trigger_kind=kind,
                    snapshot_version=snapshot_version,
                    bar_key=bar_key,
                    due_at=due_at,
                )
            )
        return due

    def _emit_all_symbols(
        self, kind: TriggerKind, due_at: datetime, *, discriminator: object
    ) -> list[DueEvaluation]:
        due: list[DueEvaluation] = []
        for symbol in sorted(self._watched):
            due.extend(
                self._emit_for_kind(
                    symbol,
                    kind,
                    due_at,
                    discriminator=discriminator,
                    snapshot_version=self._latest_snapshot.get(symbol),
                )
            )
        return due

    def _manual(self, event: ManualEvaluationRequest) -> list[DueEvaluation]:
        key = (event.symbol, event.strategy_id, cast(StrategyRole, event.side))
        schedule = self._registry.get(key)
        if (
            schedule is None
            or not self._eligible(schedule)
            or all(trigger.kind is not TriggerKind.MANUAL for trigger in schedule.triggers)
        ):
            return []
        self._manual_sequence += 1
        discriminator: object = event.request_id or self._manual_sequence
        seen_key = (key, TriggerKind.MANUAL, discriminator)
        if seen_key in self._seen:
            return []
        self._seen.add(seen_key)
        return [
            DueEvaluation(
                symbol=schedule.symbol,
                strategy_id=schedule.strategy_id,
                side=schedule.side,
                trigger_kind=TriggerKind.MANUAL,
                snapshot_version=self._latest_snapshot.get(schedule.symbol),
                bar_key=None,
                due_at=event.due_at or self._now(),
            )
        ]

    def _control_tick(self) -> list[DueEvaluation]:
        now = self._now()
        if now < self._last_control_at:
            raise ValueError("clock moved backwards")
        due = self._session_boundaries(self._last_control_at, now)
        due.extend(self._intervals(now))
        self._last_control_at = now
        return due

    def _intervals(self, now: datetime) -> list[DueEvaluation]:
        due: list[DueEvaluation] = []
        for (key, trigger), next_due in sorted(
            self._next_interval.items(),
            key=lambda item: (
                item[1],
                item[0][0][0],
                item[0][0][1],
                item[0][0][2].value,
                item[0][1].interval_seconds or 0,
            ),
        ):
            schedule = self._registry.get(key)
            if schedule is None or now < next_due:
                continue
            seconds = trigger.interval_seconds
            assert seconds is not None
            interval = timedelta(seconds=seconds)
            elapsed = (now - next_due) // interval
            self._next_interval[(key, trigger)] = next_due + interval * (elapsed + 1)
            if not self._eligible(schedule):
                continue
            discriminator = next_due
            seen_key = (key, TriggerKind.INTERVAL, discriminator)
            if seen_key in self._seen:
                continue
            self._seen.add(seen_key)
            due.append(
                DueEvaluation(
                    symbol=schedule.symbol,
                    strategy_id=schedule.strategy_id,
                    side=schedule.side,
                    trigger_kind=TriggerKind.INTERVAL,
                    snapshot_version=self._latest_snapshot.get(schedule.symbol),
                    bar_key=None,
                    due_at=next_due,
                )
            )
        return due

    def _session_boundaries(self, start: datetime, end: datetime) -> list[DueEvaluation]:
        if end == start:
            return []
        due: list[DueEvaluation] = []
        local_start = start.astimezone(exchange_calendar.NEW_YORK).date()
        local_end = end.astimezone(exchange_calendar.NEW_YORK).date()
        day = local_start
        while day <= local_end:
            try:
                session = self._calendar.session_for(day)
            except ValueError:
                day += timedelta(days=1)
                continue
            cutoff = _utc(
                self._calendar.entry_cutoff(session, self._entry_cutoff_minutes),
                "entry cutoff",
            )
            boundaries = (
                (TriggerKind.SESSION_OPEN, _utc(session.open_utc, "session open")),
                (TriggerKind.ENTRY_CUTOFF, cutoff),
                (TriggerKind.SESSION_CLOSE, _utc(session.close_utc, "session close")),
            )
            for kind, instant in boundaries:
                if start < instant <= end:
                    due.extend(
                        self._emit_all_symbols(
                            kind, instant, discriminator=session.session_id
                        )
                    )
            day += timedelta(days=1)
        return due


# Alternate descriptive name for callers that do not use the Strategy prefix.
Scheduler = StrategyScheduler
DueScheduler = StrategyScheduler
