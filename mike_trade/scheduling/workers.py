"""Bounded, broker-neutral execution of strategy evaluations."""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import threading
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from mike_trade.domain.strategy_types import Evaluation, EvaluationStatus, StrategyRole
from mike_trade.scheduling.scheduler import DueEvaluation

__all__ = [
    "EvaluationOutcome",
    "EvaluationResult",
    "EvaluationPoolClosedError",
    "EvaluationPool",
]


class EvaluationOutcome(StrEnum):
    SIGNAL = "SIGNAL"
    NO_SIGNAL = "NO_SIGNAL"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    STALE_DISCARDED = "STALE_DISCARDED"


_NON_SIGNAL_OUTCOMES = frozenset(
    {
        EvaluationOutcome.NO_SIGNAL,
        EvaluationOutcome.ERROR,
        EvaluationOutcome.TIMEOUT,
        EvaluationOutcome.CANCELLED,
        EvaluationOutcome.STALE_DISCARDED,
    }
)


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    strategy_id: str
    side: StrategyRole
    symbol: str
    snapshot_version: int | None
    started_at: datetime
    finished_at: datetime
    outcome: EvaluationOutcome
    signal: object | None = None
    error_detail: str | None = None

    def __post_init__(self) -> None:
        for name in ("started_at", "finished_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(UTC))
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.outcome is EvaluationOutcome.SIGNAL:
            if self.signal is None:
                raise ValueError("SIGNAL outcomes require a signal payload")
        elif self.signal is not None:
            raise ValueError(f"{self.outcome.value} outcomes must not carry a signal")
        if self.outcome is EvaluationOutcome.ERROR and not self.error_detail:
            raise ValueError("ERROR outcomes require error_detail")

    @property
    def actionable(self) -> bool:
        return self.outcome is EvaluationOutcome.SIGNAL

    @property
    def reason(self) -> str | None:
        return self.error_detail


class EvaluationPoolClosedError(RuntimeError):
    """Raised when work is submitted after graceful shutdown begins."""


@dataclass(eq=False, slots=True)
class _Pending:
    key: tuple[str, str, str]
    version: int | None
    superseded: asyncio.Event = field(default_factory=asyncio.Event)


def _call(evaluator: Callable[..., object], args: tuple[object, ...],
          kwargs: Mapping[str, object]) -> object:
    return evaluator(*args, **kwargs)


class EvaluationPool:
    """One shared, bounded executor pool for all symbols and strategies."""

    def __init__(
        self,
        *,
        max_workers: int = 4,
        max_in_flight: int | None = None,
        evaluation_timeout: float = 5.0,
        shutdown_timeout: float = 10.0,
        enable_process_pool: bool = False,
        process_workers: int | None = None,
        thread_name_prefix: str = "strategy-evaluation",
    ) -> None:
        self._require_positive_int(max_workers, "max_workers")
        cap = max_workers if max_in_flight is None else max_in_flight
        self._require_positive_int(cap, "max_in_flight")
        self._require_positive_seconds(evaluation_timeout, "evaluation_timeout")
        self._require_positive_seconds(shutdown_timeout, "shutdown_timeout")
        if not isinstance(enable_process_pool, bool):
            raise ValueError("enable_process_pool must be bool")
        if process_workers is not None:
            self._require_positive_int(process_workers, "process_workers")

        self._thread_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix
        )
        self._process_executor: concurrent.futures.ProcessPoolExecutor | None = None
        self._process_workers = process_workers or max(1, min(2, max_workers))
        self._process_enabled = enable_process_pool
        self._semaphore = asyncio.Semaphore(cap)
        self._max_workers = max_workers
        self._max_in_flight = cap
        self._evaluation_timeout = float(evaluation_timeout)
        self._shutdown_timeout = float(shutdown_timeout)
        self._accepting = True
        self._shutdown_complete = False
        self._watermarks: dict[tuple[str, str, str], int] = {}
        self._pending: dict[tuple[str, str, str], set[_Pending]] = {}
        self._active_submissions: set[asyncio.Task[Any]] = set()
        self._executor_futures: set[concurrent.futures.Future[object]] = set()
        self._monitor_tasks: set[asyncio.Task[None]] = set()
        self._in_flight = 0
        self._in_flight_high_water = 0
        self._thread_submissions = 0
        self._process_submissions = 0
        self._counter_lock = threading.Lock()

    @staticmethod
    def _require_positive_int(value: object, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")

    @staticmethod
    def _require_positive_seconds(value: object, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{name} must be positive")

    @property
    def executor(self) -> concurrent.futures.ThreadPoolExecutor:
        return self._thread_executor

    @property
    def thread_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        return self._thread_executor

    @property
    def process_executor(self) -> concurrent.futures.ProcessPoolExecutor | None:
        return self._process_executor

    @property
    def max_workers(self) -> int:
        return self._max_workers

    @property
    def max_in_flight(self) -> int:
        return self._max_in_flight

    @property
    def in_flight(self) -> int:
        with self._counter_lock:
            return self._in_flight

    @property
    def in_flight_high_water(self) -> int:
        with self._counter_lock:
            return self._in_flight_high_water

    @property
    def thread_submissions(self) -> int:
        return self._thread_submissions

    @property
    def process_submissions(self) -> int:
        return self._process_submissions

    @property
    def accepting(self) -> bool:
        return self._accepting

    @staticmethod
    def evaluation_key(due: DueEvaluation) -> tuple[str, str, str]:
        return (due.symbol, due.strategy_id, due.side.value)

    def latest_snapshot_version(self, due: DueEvaluation) -> int | None:
        return self._watermarks.get(self.evaluation_key(due))

    def update_watermark(
        self,
        symbol: str,
        strategy_id: str,
        side: StrategyRole | str,
        snapshot_version: int,
    ) -> None:
        """Record a newer snapshot and cancel work that has not started."""

        if isinstance(snapshot_version, bool) or not isinstance(snapshot_version, int) \
                or snapshot_version < 1:
            raise ValueError("snapshot_version must be a positive integer")
        side_text = side.value if isinstance(side, StrategyRole) else str(side).upper()
        key = (symbol.strip().upper(), strategy_id.strip(), side_text)
        previous = self._watermarks.get(key, 0)
        if snapshot_version <= previous:
            return
        self._watermarks[key] = snapshot_version
        for pending in self._pending.get(key, ()):
            if pending.version is not None and pending.version < snapshot_version:
                pending.superseded.set()

    note_snapshot = update_watermark

    async def submit(
        self,
        due: DueEvaluation,
        strategy: object,
        *args: object,
        timeout: float | None = None,
        **kwargs: object,
    ) -> EvaluationResult:
        """Evaluate one due item without ever running strategy code on the event loop."""

        if not self._accepting:
            raise EvaluationPoolClosedError("evaluation pool is shutting down")
        if not isinstance(due, DueEvaluation):
            raise TypeError("due must be DueEvaluation")
        if timeout is not None:
            self._require_positive_seconds(timeout, "timeout")
        evaluator = self._resolve_evaluator(strategy)
        key = self.evaluation_key(due)
        pending = _Pending(key, due.snapshot_version)
        self._pending.setdefault(key, set()).add(pending)
        if due.snapshot_version is not None:
            self.update_watermark(*key, due.snapshot_version)
            if self._is_stale(due):
                pending.superseded.set()

        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_submissions.add(current_task)
        started_at = datetime.now(UTC)
        acquired = False
        try:
            acquire_task = asyncio.create_task(self._semaphore.acquire())
            stale_task = asyncio.create_task(pending.superseded.wait())
            done, _ = await asyncio.wait(
                {acquire_task, stale_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if acquire_task in done:
                acquired = True
            if stale_task in done and pending.superseded.is_set():
                if not acquire_task.done():
                    acquire_task.cancel()
                else:
                    acquired = True
                if acquired:
                    self._semaphore.release()
                return self._result(
                    due, started_at, EvaluationOutcome.CANCELLED,
                    error_detail="superseded before evaluation started",
                )
            stale_task.cancel()
            await asyncio.gather(stale_task, return_exceptions=True)

            # A newer version can arrive between semaphore acquisition and dispatch.
            if self._is_stale(due):
                self._semaphore.release()
                acquired = False
                return self._result(
                    due, started_at, EvaluationOutcome.CANCELLED,
                    error_detail="superseded before evaluation started",
                )

            executor = self._select_executor(strategy, evaluator)
            call = functools.partial(_call, evaluator, args, kwargs)
            future = executor.submit(call)
            self._executor_futures.add(future)
            with self._counter_lock:
                self._in_flight += 1
                self._in_flight_high_water = max(self._in_flight_high_water, self._in_flight)
            if executor is self._thread_executor:
                self._thread_submissions += 1
            else:
                self._process_submissions += 1

            monitor = asyncio.create_task(self._monitor_future(future))
            self._monitor_tasks.add(monitor)
            monitor.add_done_callback(self._monitor_tasks.discard)
            acquired = False  # the bounded completion monitor now owns the permit
            try:
                raw = await self._await_future(
                    future,
                    self._evaluation_timeout if timeout is None else float(timeout),
                )
            except TimeoutError:
                future.cancel()  # succeeds only if executor work has not started
                return self._result(
                    due, started_at, EvaluationOutcome.TIMEOUT,
                    error_detail="strategy evaluation exceeded its timeout",
                )
            except asyncio.CancelledError:
                future.cancel()
                return self._result(
                    due, started_at, EvaluationOutcome.CANCELLED,
                    error_detail="evaluation was cancelled",
                )
            except Exception as exc:  # strategy failures are isolated here
                if self._is_stale(due):
                    return self._result(due, started_at, EvaluationOutcome.STALE_DISCARDED)
                detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                return self._result(due, started_at, EvaluationOutcome.ERROR, error_detail=detail)

            if self._is_stale(due):
                return self._result(due, started_at, EvaluationOutcome.STALE_DISCARDED)
            outcome, signal, normalized_detail = self._normalize_output(raw)
            return self._result(
                due, started_at, outcome, signal=signal, error_detail=normalized_detail
            )
        finally:
            if acquired:
                self._semaphore.release()
            self._pending.get(key, set()).discard(pending)
            if not self._pending.get(key):
                self._pending.pop(key, None)
            if current_task is not None:
                self._active_submissions.discard(current_task)

    @staticmethod
    def _resolve_evaluator(strategy: object) -> Callable[..., object]:
        evaluate = getattr(strategy, "evaluate", None)
        candidate = evaluate if callable(evaluate) else strategy
        if not callable(candidate):
            raise TypeError("strategy must be callable or provide evaluate()")
        return cast(Callable[..., object], candidate)

    def _select_executor(
        self, strategy: object, evaluator: Callable[..., object]
    ) -> concurrent.futures.Executor:
        owner = getattr(evaluator, "__self__", None)
        cpu_heavy = bool(getattr(strategy, "cpu_heavy", getattr(owner, "cpu_heavy", False)))
        if not (self._process_enabled and cpu_heavy):
            return self._thread_executor
        if self._process_executor is None:
            self._process_executor = concurrent.futures.ProcessPoolExecutor(
                max_workers=self._process_workers
            )
        return self._process_executor

    def _is_stale(self, due: DueEvaluation) -> bool:
        if due.snapshot_version is None:
            return False
        return self._watermarks.get(self.evaluation_key(due), due.snapshot_version) \
            > due.snapshot_version

    async def _monitor_future(self, future: concurrent.futures.Future[object]) -> None:
        try:
            while not future.done():
                await asyncio.sleep(0.005)
        finally:
            self._executor_futures.discard(future)
            with self._counter_lock:
                self._in_flight -= 1
            self._semaphore.release()

    @staticmethod
    async def _await_future(
        future: concurrent.futures.Future[object], timeout: float
    ) -> object:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not future.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.sleep(min(0.005, remaining))
        return future.result()

    @staticmethod
    def _normalize_output(
        raw: object,
    ) -> tuple[EvaluationOutcome, object | None, str | None]:
        if isinstance(raw, Evaluation):
            if raw.status is EvaluationStatus.SIGNAL:
                return (EvaluationOutcome.SIGNAL, raw.signal, None)
            if raw.status is EvaluationStatus.ERROR:
                return (EvaluationOutcome.ERROR, None, raw.error_detail or raw.reason)
            return (EvaluationOutcome.NO_SIGNAL, None, raw.reason or None)
        if raw is None or raw is False:
            return (EvaluationOutcome.NO_SIGNAL, None, None)
        return (EvaluationOutcome.SIGNAL, raw, None)

    @staticmethod
    def _result(
        due: DueEvaluation,
        started_at: datetime,
        outcome: EvaluationOutcome,
        *,
        signal: object | None = None,
        error_detail: str | None = None,
    ) -> EvaluationResult:
        return EvaluationResult(
            strategy_id=due.strategy_id,
            side=due.side,
            symbol=due.symbol,
            snapshot_version=due.snapshot_version,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            outcome=outcome,
            signal=signal if outcome is EvaluationOutcome.SIGNAL else None,
            error_detail=error_detail,
        )

    async def shutdown(self, *, timeout: float | None = None) -> None:
        """Stop admission, bound the drain, then release both shared executors."""

        if self._shutdown_complete:
            return
        if timeout is not None:
            self._require_positive_seconds(timeout, "timeout")
        self._accepting = False
        bound = self._shutdown_timeout if timeout is None else float(timeout)
        deadline = asyncio.get_running_loop().time() + bound
        current = asyncio.current_task()
        submissions = [task for task in self._active_submissions if task is not current]
        try:
            if submissions:
                await asyncio.wait_for(
                    asyncio.gather(*submissions, return_exceptions=True),
                    timeout=max(0.001, deadline - asyncio.get_running_loop().time()),
                )
            monitors = tuple(self._monitor_tasks)
            if monitors:
                await asyncio.wait_for(
                    asyncio.gather(*monitors, return_exceptions=True),
                    timeout=max(0.001, deadline - asyncio.get_running_loop().time()),
                )
        except TimeoutError:
            for future in tuple(self._executor_futures):
                future.cancel()
        finally:
            self._thread_executor.shutdown(wait=False, cancel_futures=True)
            if self._process_executor is not None:
                self._process_executor.shutdown(wait=False, cancel_futures=True)
            self._shutdown_complete = True

    async def __aenter__(self) -> EvaluationPool:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.shutdown()
