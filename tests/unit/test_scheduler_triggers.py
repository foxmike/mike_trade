"""Unit tests for declarative triggers and the engine-owned due scheduler."""

from __future__ import annotations

import asyncio
import inspect
import threading
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta

import pytest

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.scheduling import calendar as nyse
from mike_trade.scheduling.calendar import Session
from mike_trade.scheduling.scheduler import (
    CandleClosedEvent,
    ControlTick,
    DueEvaluation,
    ManualEvaluationRequest,
    MarketDataArrivalEvent,
    QuoteChangeEvent,
    SessionEvent,
    StrategyScheduler,
)
from mike_trade.scheduling.triggers import (
    CANDLE_CLOSED,
    ENTRY_CUTOFF,
    INTERVAL,
    MANUAL,
    MARKET_DATA_ARRIVAL,
    QUOTE_CHANGE,
    SESSION_CLOSE,
    SESSION_OPEN,
    StrategyTrigger,
    TriggerConfigurationError,
    TriggerKind,
)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


def register(
    scheduler: StrategyScheduler,
    strategy_id: str,
    *triggers: StrategyTrigger,
    symbol: str = "AAPL",
    side: StrategyRole = StrategyRole.BUY,
    enabled: bool = True,
    paused: bool = False,
) -> None:
    scheduler.register(
        symbol=symbol,
        strategy_id=strategy_id,
        side=side,
        triggers=triggers,
        enabled=enabled,
        paused=paused,
    )


def test_all_trigger_declarations_are_frozen_and_validated() -> None:
    declarations = (
        MARKET_DATA_ARRIVAL,
        QUOTE_CHANGE,
        CANDLE_CLOSED("1m"),
        CANDLE_CLOSED("5m"),
        CANDLE_CLOSED("15m"),
        CANDLE_CLOSED("30m"),
        CANDLE_CLOSED("60m"),
        INTERVAL(30),
        SESSION_OPEN,
        ENTRY_CUTOFF,
        SESSION_CLOSE,
        MANUAL,
    )
    assert {item.kind for item in declarations} == set(TriggerKind)
    for declaration in declarations:
        with pytest.raises(FrozenInstanceError):
            declaration.kind = TriggerKind.MANUAL  # type: ignore[misc]

    for seconds in (0, -1):
        with pytest.raises(TriggerConfigurationError):
            INTERVAL(seconds)
    with pytest.raises(TriggerConfigurationError):
        CANDLE_CLOSED("2m")
    with pytest.raises(TriggerConfigurationError):
        StrategyTrigger("PRICE_WIGGLED")


def test_closed_candle_fires_once_per_new_bar_and_not_on_control_ticks() -> None:
    start = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)
    clock = FakeClock(start)
    scheduler = StrategyScheduler(clock, watched_symbols=("AAPL",))
    register(scheduler, "closed-bars", CANDLE_CLOSED(CandleTimeframe.M1))

    first = CandleClosedEvent("AAPL", "1m", start + timedelta(minutes=1), 7)
    assert len(scheduler.process(first)) == 1
    assert scheduler.process(first) == []
    for _ in range(5):
        clock.advance(seconds=1)
        assert scheduler.process(ControlTick()) == []

    second = scheduler.process(
        CandleClosedEvent("AAPL", "1m", start + timedelta(minutes=2), 8)
    )
    assert len(second) == 1
    assert second[0].bar_key is not None
    assert second[0].bar_key.end_utc == start + timedelta(minutes=2)


def test_interval_fires_at_most_once_for_each_elapsed_period() -> None:
    clock = FakeClock(datetime(2026, 1, 5, 14, 0, tzinfo=UTC))
    scheduler = StrategyScheduler(clock, watched_symbols=("AAPL",))
    register(scheduler, "timer", INTERVAL(30))

    for seconds in (1, 10, 18):
        clock.advance(seconds=seconds)
        assert scheduler.process(ControlTick()) == []
    clock.advance(seconds=1)
    first = scheduler.process(ControlTick())
    assert len(first) == 1
    assert first[0].due_at == datetime(2026, 1, 5, 14, 0, 30, tzinfo=UTC)
    assert scheduler.process(ControlTick()) == []
    clock.advance(seconds=29)
    assert scheduler.process(ControlTick()) == []
    clock.advance(seconds=1)
    assert len(scheduler.process(ControlTick())) == 1


def test_quote_change_requires_bid_or_ask_to_actually_change() -> None:
    now = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    scheduler = StrategyScheduler(FakeClock(now), watched_symbols=("AAPL",))
    register(scheduler, "quotes", QUOTE_CHANGE)

    assert len(scheduler.process(QuoteChangeEvent("AAPL", 100, 101, 1))) == 1
    assert scheduler.process(QuoteChangeEvent("AAPL", 100, 101, 2)) == []
    assert len(scheduler.process(QuoteChangeEvent("AAPL", 100, 102, 3))) == 1
    assert len(scheduler.process(QuoteChangeEvent("AAPL", 99, 102, 4))) == 1


class FakeEarlyCloseCalendar:
    def __init__(self, session: Session) -> None:
        self.session = session

    def session_for(self, day: date) -> Session:
        if day != self.session.session_date:
            raise ValueError("not a session")
        return self.session

    def entry_cutoff(self, session: Session, minutes_before_close: int) -> datetime:
        return session.close_utc - timedelta(minutes=minutes_before_close)


def test_session_boundaries_fire_once_at_calendar_reported_early_close_times() -> None:
    session = nyse.session_for(date(2026, 11, 27))
    assert session.is_early_close
    clock = FakeClock(session.open_utc - timedelta(minutes=1))
    scheduler = StrategyScheduler(
        clock,
        calendar=FakeEarlyCloseCalendar(session),
        watched_symbols=("AAPL",),
        entry_cutoff_minutes=15,
    )
    register(scheduler, "session", SESSION_OPEN, ENTRY_CUTOFF, SESSION_CLOSE)

    expected = (
        (session.open_utc, TriggerKind.SESSION_OPEN),
        (session.close_utc - timedelta(minutes=15), TriggerKind.ENTRY_CUTOFF),
        (session.close_utc, TriggerKind.SESSION_CLOSE),
    )
    for instant, kind in expected:
        clock.value = instant
        due = scheduler.process(ControlTick())
        assert [(item.trigger_kind, item.due_at) for item in due] == [(kind, instant)]
        assert scheduler.process(ControlTick()) == []


def _ordered_run() -> list[DueEvaluation]:
    start = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    at = start + timedelta(seconds=30)
    clock = FakeClock(start)
    scheduler = StrategyScheduler(clock, watched_symbols=("MSFT", "AAPL"))
    for symbol in ("MSFT", "AAPL"):
        register(
            scheduler,
            "zeta",
            SESSION_OPEN,
            CANDLE_CLOSED("1m"),
            MARKET_DATA_ARRIVAL,
            INTERVAL(30),
            MANUAL,
            symbol=symbol,
            side=StrategyRole.SELL,
        )
        register(
            scheduler,
            "alpha",
            SESSION_OPEN,
            CANDLE_CLOSED("1m"),
            MARKET_DATA_ARRIVAL,
            INTERVAL(30),
            MANUAL,
            symbol=symbol,
        )
    clock.value = at
    return scheduler.process_many(
        (
            ManualEvaluationRequest("MSFT", "zeta", "SELL", due_at=at),
            MarketDataArrivalEvent("MSFT", 2, at),
            CandleClosedEvent("MSFT", "1m", at, 2, at),
            MarketDataArrivalEvent("AAPL", 1, at),
            CandleClosedEvent("AAPL", "1m", at, 1, at),
            SessionEvent(TriggerKind.SESSION_OPEN, "fake-session", at),
            ControlTick(),
        )
    )


def test_output_order_is_repeatable_and_matches_documented_sort_key() -> None:
    first = _ordered_run()
    second = _ordered_run()
    assert first == second
    assert first == sorted(first, key=StrategyScheduler.sort_key)
    priorities = {
        TriggerKind.SESSION_OPEN: 0,
        TriggerKind.CANDLE_CLOSED: 1,
        TriggerKind.MARKET_DATA_ARRIVAL: 2,
        TriggerKind.INTERVAL: 3,
        TriggerKind.MANUAL: 4,
    }
    assert [priorities[item.trigger_kind] for item in first] == sorted(
        priorities[item.trigger_kind] for item in first
    )


def test_disabled_paused_unwatched_and_manual_gating() -> None:
    now = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    scheduler = StrategyScheduler(FakeClock(now), watched_symbols=("AAPL",))
    register(scheduler, "enabled", MARKET_DATA_ARRIVAL, MANUAL)
    register(scheduler, "disabled", MARKET_DATA_ARRIVAL, MANUAL, enabled=False)
    register(scheduler, "paused", MARKET_DATA_ARRIVAL, MANUAL, paused=True)
    register(scheduler, "unwatched", MARKET_DATA_ARRIVAL, MANUAL, symbol="MSFT")

    due = scheduler.process(MarketDataArrivalEvent("AAPL", 1, now))
    assert [item.strategy_id for item in due] == ["enabled"]
    assert scheduler.process(MarketDataArrivalEvent("MSFT", 1, now)) == []
    assert scheduler.process(ManualEvaluationRequest("AAPL", "disabled", "BUY")) == []
    assert scheduler.process(ManualEvaluationRequest("AAPL", "paused", "BUY")) == []
    assert scheduler.process(ManualEvaluationRequest("MSFT", "unwatched", "BUY")) == []
    assert len(scheduler.process(ManualEvaluationRequest("AAPL", "enabled", "BUY"))) == 1


@pytest.mark.asyncio
async def test_registration_creates_no_tasks_or_threads_per_symbol_or_strategy() -> None:
    scheduler = StrategyScheduler(
        FakeClock(datetime(2026, 1, 5, 15, 0, tzinfo=UTC)),
        watched_symbols=(f"SYM{i}" for i in range(10)),
    )
    task_count = len(asyncio.all_tasks())
    thread_count = threading.active_count()
    for number in range(10):
        for strategy in range(4):
            register(
                scheduler,
                f"strategy-{strategy}",
                MARKET_DATA_ARRIVAL,
                INTERVAL(30),
                symbol=f"SYM{number}",
            )
    await asyncio.sleep(0)
    assert len(asyncio.all_tasks()) == task_count
    assert threading.active_count() == thread_count


def test_scheduler_has_no_broker_or_provider_adapter_imports() -> None:
    import mike_trade.scheduling.scheduler as scheduler_module

    source = inspect.getsource(scheduler_module)
    assert "mike_trade.brokers" not in source
    assert "alpaca" not in source.lower()
