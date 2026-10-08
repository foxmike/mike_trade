"""Backpressure and bounded strategy-worker contract tests."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.scheduling.queueing import EventBacklog
from mike_trade.scheduling.scheduler import DueEvaluation
from mike_trade.scheduling.triggers import TriggerKind
from mike_trade.scheduling.workers import (
    EvaluationOutcome,
    EvaluationPool,
    EvaluationPoolClosedError,
)


@dataclass(frozen=True)
class FillEvent:
    sequence: int


@dataclass(frozen=True)
class BrokerOrderUpdate:
    sequence: int


@dataclass(frozen=True)
class StopEvent:
    sequence: int


@dataclass(frozen=True)
class ReconciliationEvent:
    sequence: int


@dataclass(frozen=True)
class SafetyExitEvent:
    sequence: int


def due(version: int, *, symbol: str = "AAPL", strategy: str = "alpha") -> DueEvaluation:
    return DueEvaluation(
        symbol=symbol,
        strategy_id=strategy,
        side=StrategyRole.BUY,
        trigger_kind=TriggerKind.MARKET_DATA_ARRIVAL,
        snapshot_version=version,
        bar_key=None,
        due_at=datetime.now(UTC),
    )


async def test_snapshot_flood_keeps_only_latest_version() -> None:
    backlog = EventBacklog(critical_maxsize=1, coalescible_maxsize=1)
    for version in range(1, 101):
        assert await backlog.put(due(version))

    assert backlog.qsize() == 1
    assert (await backlog.get()).snapshot_version == 100
    assert backlog.stats.coalesced == 99
    assert backlog.watermark("AAPL") == 100


async def test_critical_events_survive_market_data_flood_in_fifo_order() -> None:
    backlog = EventBacklog(critical_maxsize=2, coalescible_maxsize=1)
    events = [FillEvent(1), BrokerOrderUpdate(2), StopEvent(3),
              ReconciliationEvent(4), SafetyExitEvent(5)]
    received: list[object] = []

    async def consume() -> None:
        while len([item for item in received if not isinstance(item, DueEvaluation)]) < 5:
            received.append(await backlog.get())

    consumer = asyncio.create_task(consume())
    for version, event in enumerate(events, 1):
        await backlog.put(due(version))
        await backlog.put(event)
    await consumer

    critical = [item for item in received if not isinstance(item, DueEvaluation)]
    assert critical == events
    assert backlog.stats.coalesced + backlog.stats.discarded >= 1


async def test_full_critical_queue_backpressures_instead_of_dropping() -> None:
    backlog = EventBacklog(critical_maxsize=1, coalescible_maxsize=1)
    first, second = FillEvent(1), FillEvent(2)
    await backlog.put(first)
    blocked = asyncio.create_task(backlog.put(second))
    await asyncio.sleep(0)
    assert not blocked.done()
    assert backlog.stats.backpressure == 1
    assert backlog.stats.overflow == 1
    assert await backlog.get() == first
    assert await blocked
    assert await backlog.get() == second


async def test_exception_is_isolated_and_non_signal_outcomes_have_no_signal() -> None:
    pool = EvaluationPool(max_workers=2, evaluation_timeout=0.05)

    def broken() -> object:
        raise RuntimeError("deliberate failure")

    results = await asyncio.gather(
        pool.submit(due(1), broken),
        pool.submit(due(1, strategy="ok"), lambda: {"side": "BUY"}),
    )
    error, normal = results
    assert error.outcome is EvaluationOutcome.ERROR
    assert "deliberate failure" in (error.error_detail or "")
    assert error.signal is None
    assert normal.outcome is EvaluationOutcome.SIGNAL

    timeout = await pool.submit(due(2), lambda: time.sleep(0.2), timeout=0.01)
    assert timeout.outcome is EvaluationOutcome.TIMEOUT
    assert timeout.signal is None
    await pool.shutdown(timeout=0.01)


async def test_stale_completion_and_pending_supersession() -> None:
    pool = EvaluationPool(max_workers=1, evaluation_timeout=1)
    started = threading.Event()
    release = threading.Event()

    def slow() -> str:
        started.set()
        release.wait(0.5)
        return "BUY"

    first = asyncio.create_task(pool.submit(due(1), slow))
    for _ in range(40):
        if started.is_set():
            break
        await asyncio.sleep(0.005)
    assert started.is_set()
    pending = asyncio.create_task(pool.submit(due(1, strategy="pending"), lambda: "BUY"))
    await asyncio.sleep(0)
    pool.update_watermark("AAPL", "alpha", StrategyRole.BUY, 2)
    pool.update_watermark("AAPL", "pending", StrategyRole.BUY, 2)
    release.set()

    stale, cancelled = await asyncio.gather(first, pending)
    assert stale.outcome is EvaluationOutcome.STALE_DISCARDED
    assert stale.signal is None
    assert cancelled.outcome is EvaluationOutcome.CANCELLED
    assert cancelled.signal is None
    await pool.shutdown()


async def test_shared_executor_and_concurrency_cap() -> None:
    pool = EvaluationPool(max_workers=2, max_in_flight=2)
    executor_id = id(pool.executor)
    lock = threading.Lock()
    active = high_water = 0

    def evaluate() -> None:
        nonlocal active, high_water
        with lock:
            active += 1
            high_water = max(high_water, active)
        time.sleep(0.01)
        with lock:
            active -= 1

    jobs = [pool.submit(due(1, symbol=f"S{symbol}", strategy=f"s{strategy}"), evaluate)
            for symbol in range(10) for strategy in range(3)]
    await asyncio.gather(*jobs)
    assert high_water <= 2
    assert pool.in_flight_high_water <= 2
    assert id(pool.executor) == executor_id
    assert pool.thread_submissions == 30
    await pool.shutdown()


async def test_process_execution_is_explicit_and_disabled_by_default() -> None:
    class CpuHeavy:
        cpu_heavy = True

        def evaluate(self) -> None:
            return None

    disabled = EvaluationPool(max_workers=1)
    await disabled.submit(due(1), CpuHeavy())
    assert disabled.process_executor is None
    assert disabled.process_submissions == 0
    await disabled.shutdown()

    # Selection itself is covered without making this unit test depend on a
    # platform-specific process start method or pickling a local fixture.
    enabled = EvaluationPool(max_workers=1, enable_process_pool=True)
    evaluator = enabled._resolve_evaluator(CpuHeavy())
    selected = enabled._select_executor(CpuHeavy(), evaluator)
    assert selected is enabled.process_executor
    await enabled.shutdown()


async def test_shutdown_is_bounded_and_rejects_new_work() -> None:
    pool = EvaluationPool(max_workers=1, evaluation_timeout=0.02, shutdown_timeout=0.05)
    running = asyncio.create_task(pool.submit(due(1), lambda: time.sleep(0.2)))
    await asyncio.sleep(0)
    await pool.shutdown()
    result = await running
    assert result.outcome is EvaluationOutcome.TIMEOUT
    with pytest.raises(EvaluationPoolClosedError):
        await pool.submit(due(2), lambda: None)
