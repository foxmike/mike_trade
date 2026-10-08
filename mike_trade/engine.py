"""Engine runner: startup reconciliation, heartbeat, pause/resume, graceful drain.

The engine composes, by dependency injection: clock, market-data service,
scheduler, queueing backlog plus evaluation pool, decision router, the
reconciliation service and the health sink.  It constructs no broker or
provider clients directly.

One asyncio event loop runs the scheduler, the stream consumers and the
router: :meth:`Engine.start` launches the periodic heartbeat task on the
calling loop, and :meth:`Engine.stop` drains that same loop's router work
within a bound before shutting the pool down.

Shutdown never places liquidating orders, never cancels valid broker-held
protective stops, and never calls any broker close/cancel API -- the engine
holds no broker reference at all.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mike_trade.observability.health import (
    EngineMode,
    EngineState,
    HealthSink,
    HealthSnapshot,
)

__all__ = [
    "ReconciliationOutcome",
    "Engine",
]

_BLOCKED_STATES = frozenset(
    {
        EngineState.STARTING,
        EngineState.RECONCILING,
        EngineState.PAUSED,
        EngineState.DEGRADED,
        EngineState.DRAINING,
        EngineState.STOPPED,
    }
)


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    """Normalized result of the injected reconciliation step."""

    clean: bool
    reasons: tuple[str, ...] = ()
    status: str = "UNKNOWN"

    def __post_init__(self) -> None:
        if not isinstance(self.clean, bool):
            raise ValueError("clean must be bool")
        if not isinstance(self.reasons, tuple) or any(
            not isinstance(reason, str) or not reason.strip() for reason in self.reasons
        ):
            raise ValueError("reasons must be a tuple of non-empty strings")
        object.__setattr__(self, "reasons", tuple(r.strip() for r in self.reasons))
        if not isinstance(self.status, str) or not self.status.strip():
            raise ValueError("status must be a non-empty string")


def _utc_now(clock: Any) -> datetime:
    now = clock.now()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("clock.now() must return a timezone-aware datetime")
    return now.astimezone(UTC)


def _normalize_reconciliation(raw: object) -> ReconciliationOutcome:
    if isinstance(raw, ReconciliationOutcome):
        return raw
    if isinstance(raw, bool):
        return ReconciliationOutcome(clean=raw)
    if isinstance(raw, dict):
        return ReconciliationOutcome(
            clean=bool(raw.get("clean", False)),
            reasons=tuple(str(r) for r in raw.get("reasons", ())),
            status=str(raw.get("status", "UNKNOWN")),
        )
    clean = bool(getattr(raw, "clean", False))
    reasons = getattr(raw, "reasons", getattr(raw, "drift_reasons", ())) or ()
    status = str(getattr(raw, "status", "UNKNOWN"))
    return ReconciliationOutcome(clean=clean, reasons=tuple(str(r) for r in reasons),
                                 status=status)


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


class Engine:
    """Dependency-injected engine runner with heartbeat and graceful drain."""

    def __init__(
        self,
        *,
        clock: Any,
        market_data: Any = None,
        scheduler: Any = None,
        backlog: Any = None,
        pool: Any = None,
        router: Any = None,
        reconciliation: Any = None,
        health_sink: HealthSink,
        persistence_sink: Any = None,
        mode: EngineMode | str = EngineMode.PAPER,
        account_scope_id: str = "paper:default",
        heartbeat_interval: float = 5.0,
        drain_timeout: float = 5.0,
        protective_stop_status: str = "UNKNOWN",
    ) -> None:
        if not callable(getattr(clock, "now", None)):
            raise TypeError("clock must expose now()")
        if not callable(getattr(health_sink, "publish", None)):
            raise TypeError("health_sink must expose publish(snapshot)")
        if isinstance(heartbeat_interval, bool) or not isinstance(
            heartbeat_interval, (int, float)
        ) or heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be positive")
        if isinstance(drain_timeout, bool) or not isinstance(
            drain_timeout, (int, float)
        ) or drain_timeout <= 0:
            raise ValueError("drain_timeout must be positive")
        if isinstance(mode, str):
            mode = EngineMode(mode.strip().upper())
        if not isinstance(mode, EngineMode):
            raise TypeError("mode must be EngineMode")
        if not isinstance(account_scope_id, str) or not account_scope_id.strip():
            raise ValueError("account_scope_id must be a non-empty string")

        self._clock = clock
        self._market_data = market_data
        self._scheduler = scheduler
        self._backlog = backlog
        self._pool = pool
        self._router = router
        self._reconciliation = reconciliation
        self._health_sink = health_sink
        self._persistence_sink = persistence_sink
        self._mode = mode
        self._account_scope_id = account_scope_id.strip()
        self._heartbeat_interval = float(heartbeat_interval)
        self._drain_timeout = float(drain_timeout)
        self._protective_stop_status = protective_stop_status

        self._state = EngineState.STARTING
        self._sequence = 0
        self._entry_block_reasons: tuple[str, ...] = ("STARTING",)
        self._last_error: str | None = None
        self._reconciliation_status = "PENDING"
        self._accepting_evaluations = True
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._last_snapshot: HealthSnapshot | None = None

    # ------------------------------------------------------------ inspection

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def entries_allowed(self) -> bool:
        return self._state not in _BLOCKED_STATES

    @property
    def entry_block_reasons(self) -> tuple[str, ...]:
        return self._entry_block_reasons

    @property
    def accepting_evaluations(self) -> bool:
        return self._accepting_evaluations

    @property
    def last_snapshot(self) -> HealthSnapshot | None:
        return self._last_snapshot

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # ---------------------------------------------------------------- startup

    async def start(self) -> HealthSnapshot:
        """Run startup reconciliation, then enter RUNNING and start heartbeats."""
        self._transition(EngineState.RECONCILING, ("STARTUP_RECONCILIATION",))
        await self.publish_heartbeat()
        outcome = await self._run_reconciliation()
        if outcome.clean:
            self._reconciliation_status = outcome.status or "CLEAN"
            self._transition(EngineState.RUNNING, ())
        else:
            self._reconciliation_status = outcome.status or "DRIFTED"
            reasons = outcome.reasons or ("RECONCILIATION_DRIFT",)
            self._transition(EngineState.DEGRADED, tuple(reasons))
            if not self._last_error:
                self._last_error = "; ".join(reasons)
        snapshot = await self.publish_heartbeat()
        self._start_heartbeat_task()
        return snapshot

    async def _run_reconciliation(self) -> ReconciliationOutcome:
        step = self._reconciliation
        if step is None:
            return ReconciliationOutcome(clean=True, status="CLEAN")
        if isinstance(step, ReconciliationOutcome):
            return step
        if isinstance(step, bool):
            return _normalize_reconciliation(step)
        if isinstance(step, dict) and "clean" in step:
            return _normalize_reconciliation(step)
        try:
            if callable(getattr(step, "reconcile", None)):
                raw = await _maybe_await(step.reconcile())
            elif callable(step):
                raw = await _maybe_await(step())
            elif callable(getattr(step, "run", None)):
                raw = await _maybe_await(step.run())
            else:
                raise TypeError("reconciliation must expose reconcile()/run() or be callable")
            return _normalize_reconciliation(raw)
        except Exception as exc:  # reconciliation failure blocks entries, never entries
            detail = f"{type(exc).__name__}: {exc}"
            self._last_error = detail
            return ReconciliationOutcome(clean=False, reasons=(detail,), status="FAILED")

    # -------------------------------------------------------------- heartbeat

    def _start_heartbeat_task(self) -> None:
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        if self._state is EngineState.STOPPED:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._heartbeat_task = loop.create_task(
            self._heartbeat_loop(), name="engine-heartbeat"
        )

    async def _heartbeat_loop(self) -> None:
        try:
            while self._state not in (EngineState.STOPPED,):
                await asyncio.sleep(self._heartbeat_interval)
                if self._state is EngineState.STOPPED:
                    break
                try:
                    await self.publish_heartbeat()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error = f"{type(exc).__name__}: {exc}"
        except asyncio.CancelledError:
            pass

    async def publish_heartbeat(self) -> HealthSnapshot:
        """Evaluate degraded indicators and publish one heartbeat snapshot."""
        self._refresh_degraded()
        snapshot = self._build_snapshot()
        result = self._health_sink.publish(snapshot)
        if inspect.isawaitable(result):
            await result
        self._last_snapshot = snapshot
        return snapshot

    def _build_snapshot(self) -> HealthSnapshot:
        now = _utc_now(self._clock)
        self._sequence += 1
        allowed = self._state not in _BLOCKED_STATES
        reasons = () if allowed else self._entry_block_reasons
        feed = self._feed_freshness()
        pool_depth, pool_backpressure, pool_overflow = self._pool_counters()
        return HealthSnapshot(
            state=self._state,
            mode=self._mode,
            account_scope_id=self._account_scope_id,
            heartbeat_at=now,
            sequence=self._sequence,
            entries_allowed=allowed,
            entry_block_reasons=reasons,
            feed_freshness=feed,
            scheduler_status=self._scheduler_status(),
            pool_depth=pool_depth,
            pool_backpressure=pool_backpressure,
            pool_overflow=pool_overflow,
            router_backlog=self._router_backlog(),
            reconciliation_status=self._reconciliation_status,
            protective_stop_status=self._protective_stop_status,
            last_error=self._last_error,
        )

    # ------------------------------------------------------- pause and resume

    def pause(self, reason: str = "OPERATOR_PAUSE") -> HealthSnapshot:
        """Block new entries; fills, stops, reconciliation and exits still flow."""
        if self._state is EngineState.RUNNING:
            self._transition(EngineState.PAUSED, (reason,))
        return self._build_snapshot_preview()

    def resume(self) -> HealthSnapshot:
        """Return from PAUSED to RUNNING (or DEGRADED when indicators demand it)."""
        if self._state is EngineState.PAUSED:
            self._transition(EngineState.RUNNING, ())
            self._refresh_degraded()
        return self._build_snapshot_preview()

    def _build_snapshot_preview(self) -> HealthSnapshot:
        # A synchronous preview without publishing; keeps pause/resume usable
        # without a running loop while publish_heartbeat stays the durable path.
        now = _utc_now(self._clock)
        allowed = self._state not in _BLOCKED_STATES
        return HealthSnapshot(
            state=self._state,
            mode=self._mode,
            account_scope_id=self._account_scope_id,
            heartbeat_at=now,
            sequence=self._sequence,
            entries_allowed=allowed,
            entry_block_reasons=() if allowed else self._entry_block_reasons,
            feed_freshness=self._feed_freshness(),
            scheduler_status=self._scheduler_status(),
            pool_depth=self._pool_counters()[0],
            pool_backpressure=self._pool_counters()[1],
            pool_overflow=self._pool_counters()[2],
            router_backlog=self._router_backlog(),
            reconciliation_status=self._reconciliation_status,
            protective_stop_status=self._protective_stop_status,
            last_error=self._last_error,
        )

    # ------------------------------------------------------------ event paths

    def handle_evaluation_result(self, result: Any) -> bool:
        """Route one evaluation result; return True when forwarded.

        BUY-role strategy signals are forwarded for authorization only when
        entries are allowed.  SELL-role signals (exits) always flow.  ERROR and
        other non-signal outcomes surface as DEGRADED health detail and are
        never forwarded as BUY.
        """
        try:
            outcome = str(getattr(result, "outcome", "") or "").upper()
            outcome_value = getattr(getattr(result, "outcome", None), "value", outcome)
            outcome_name = str(outcome_value).upper()
            side = getattr(result, "side", getattr(getattr(result, "signal", None), "side", None))
            side_name = str(getattr(side, "value", side) or "").upper()
            error_detail = getattr(result, "error_detail", None) or getattr(
                result, "reason", None
            )
            actionable = bool(getattr(result, "actionable", outcome_name == "SIGNAL"))
            if outcome_name in ("ERROR", "TIMEOUT", "CANCELLED", "STALE_DISCARDED"):
                detail = str(error_detail or outcome_name)
                self._last_error = detail
                if self._state is EngineState.RUNNING:
                    self._transition(EngineState.DEGRADED, (f"STRATEGY_{outcome_name}",))
                elif self._state not in _BLOCKED_STATES:
                    pass
                else:
                    self._add_block_reason(f"STRATEGY_{outcome_name}")
                return False
            if not actionable or outcome_name != "SIGNAL":
                return False
            if side_name == "BUY":
                if not self.entries_allowed:
                    return False
                return self._forward_signal(result)
            # SELL and other exit roles always flow, even when entries block.
            return self._forward_signal(result)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            self._last_error = detail
            if self._state is EngineState.RUNNING:
                self._transition(EngineState.DEGRADED, ("STRATEGY_ERROR",))
            return False

    def _forward_signal(self, result: Any) -> bool:
        router = self._router
        if router is None:
            return True
        submit = getattr(router, "submit_evaluation", None)
        if not callable(submit):
            return True
        submit(result)
        return True

    def handle_critical_event(self, event: Any) -> bool:
        """Forward fills, stops, reconciliation, manual closes, safety exits."""
        router = self._router
        if router is None:
            return True
        submit = getattr(router, "submit_event", None)
        if not callable(submit):
            return True
        submit(event)
        return True

    def report_strategy_error(self, detail: str) -> None:
        """Surface a strategy/worker failure as DEGRADED detail; never as BUY."""
        self._last_error = str(detail)
        if self._state is EngineState.RUNNING:
            self._transition(EngineState.DEGRADED, ("STRATEGY_ERROR",))
        else:
            self._add_block_reason("STRATEGY_ERROR")

    def report_protective_status(self, status: str) -> None:
        if isinstance(status, str) and status.strip():
            self._protective_stop_status = status.strip()

    # ------------------------------------------------------------------ drain

    async def stop(self) -> HealthSnapshot:
        """Graceful drain: DRAINING -> flush -> pool shutdown -> STOPPED heartbeat.

        Never places liquidating orders, never cancels protective stops, and
        never calls any broker close/cancel API (the engine holds no broker).
        """
        if self._state is EngineState.STOPPED:
            return await self.publish_heartbeat()
        self._transition(EngineState.DRAINING, ("DRAINING",))
        self._accepting_evaluations = False
        await self.publish_heartbeat()
        try:
            await asyncio.wait_for(
                self._drain_critical(), timeout=self._drain_timeout
            )
        except TimeoutError:
            self._last_error = "DRAIN_TIMEOUT"
        await self._flush_durable()
        await self._shutdown_pool()
        self._stop_router()
        await self._cancel_heartbeat_task()
        self._transition(EngineState.STOPPED, ("STOPPED",))
        return await self.publish_heartbeat()

    async def _drain_critical(self) -> None:
        router = self._router
        drain = getattr(router, "drain", None) if router is not None else None
        if callable(drain):
            await _maybe_await(drain())

    async def _flush_durable(self) -> None:
        for sink in (self._persistence_sink, self._health_sink):
            flush = getattr(sink, "flush", None) if sink is not None else None
            if callable(flush):
                try:
                    await asyncio.wait_for(
                        _maybe_await(flush()), timeout=self._drain_timeout
                    )
                except TimeoutError:
                    self._last_error = "FLUSH_TIMEOUT"

    async def _shutdown_pool(self) -> None:
        pool = self._pool
        shutdown = getattr(pool, "shutdown", None) if pool is not None else None
        if callable(shutdown):
            try:
                await asyncio.wait_for(
                    _maybe_await(shutdown()), timeout=self._drain_timeout
                )
            except TimeoutError:
                self._last_error = "POOL_SHUTDOWN_TIMEOUT"

    def _stop_router(self) -> None:
        router = self._router
        stop = getattr(router, "stop", None) if router is not None else None
        if callable(stop):
            try:
                stop()
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"

    async def _cancel_heartbeat_task(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # -------------------------------------------------------------- indicators

    def refresh(self) -> HealthSnapshot:
        """Re-evaluate degraded indicators and return an unpublished preview."""
        self._refresh_degraded()
        return self._build_snapshot_preview()

    def _refresh_degraded(self) -> None:
        if self._state is not EngineState.RUNNING:
            return
        feed = self._feed_freshness()
        if feed in ("STALE", "UNKNOWN"):
            self._transition(EngineState.DEGRADED, (f"FEED_{feed}",))
            return
        _, backpressure, overflow = self._pool_counters()
        if overflow > 0 or backpressure > 0:
            self._transition(EngineState.DEGRADED, ("POOL_BACKPRESSURE",))
            return
        if self._reconciliation_status in ("DRIFTED", "FAILED"):
            self._transition(
                EngineState.DEGRADED, (f"RECONCILIATION_{self._reconciliation_status}",)
            )

    def _feed_freshness(self) -> str:
        feed = self._market_data
        if feed is None:
            return "UNKNOWN"
        for name in ("feed_freshness", "freshness"):
            value = getattr(feed, name, None)
            text = str(value() if callable(value) else value or "").upper()
            if text in ("FRESH", "STALE", "UNKNOWN"):
                return text
        if callable(getattr(feed, "is_fresh", None)):
            try:
                return "FRESH" if feed.is_fresh() else "STALE"
            except Exception:
                return "UNKNOWN"
        for name in ("fresh", "is_healthy", "healthy"):
            if hasattr(feed, name):
                value = getattr(feed, name)
                return "FRESH" if (value() if callable(value) else value) else "STALE"
        for name in ("stale", "is_stale", "degraded", "offline"):
            if hasattr(feed, name):
                value = getattr(feed, name)
                flag = value() if callable(value) else value
                return "STALE" if flag else "FRESH"
        newest = getattr(feed, "newest_data_ts", None)
        if callable(newest):
            try:
                return "FRESH" if newest("ANY") is not None else "UNKNOWN"
            except Exception:
                return "UNKNOWN"
        return "UNKNOWN"

    def _scheduler_status(self) -> str:
        scheduler = self._scheduler
        if scheduler is None:
            return "UNKNOWN"
        for name in ("status", "state", "health"):
            value = getattr(scheduler, name, None)
            if value is not None:
                return str(value() if callable(value) else value)
        return "OK"

    def _pool_counters(self) -> tuple[int, int, int]:
        pool = self._pool
        backlog = self._backlog
        depth = backpressure = overflow = 0
        for source in (pool, backlog):
            if source is None:
                continue
            stats = getattr(source, "stats", None)
            if callable(stats):
                try:
                    stats = stats()
                except Exception:
                    stats = None
            if stats is None and hasattr(source, "health") and callable(source.health):
                try:
                    stats = source.health()
                except Exception:
                    stats = None
            if stats is not None:
                depth += int(getattr(stats, "depth", 0) or 0)
                backpressure += int(getattr(stats, "backpressure", 0) or 0)
                overflow += int(getattr(stats, "overflow", 0) or 0)
                continue
            depth += int(getattr(source, "depth", 0) or 0)
            backpressure += int(getattr(source, "backpressure_count", 0) or 0)
            overflow += int(getattr(source, "overflow_count", getattr(source, "overflow", 0)) or 0)
        return (depth, backpressure, overflow)

    def _router_backlog(self) -> int:
        router = self._router
        if router is None:
            return 0
        for name in ("pending", "backlog", "depth", "qsize"):
            value = getattr(router, name, None)
            if value is not None:
                try:
                    return int(value() if callable(value) else value)
                except Exception:
                    continue
        return 0

    # ---------------------------------------------------------------- helpers

    def _transition(self, state: EngineState, reasons: tuple[str, ...]) -> None:
        self._state = state
        self._entry_block_reasons = tuple(reasons)

    def _add_block_reason(self, reason: str) -> None:
        if reason not in self._entry_block_reasons:
            self._entry_block_reasons = self._entry_block_reasons + (reason,)

