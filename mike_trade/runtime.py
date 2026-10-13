"""Production runtime lifecycle and dependency container.

The runtime is broker neutral. :mod:`mike_trade.app` owns concrete construction;
this module owns ordering, fail-closed entry authorization, heartbeat publication,
signal handling, and graceful shutdown. Shutdown never submits, cancels, closes,
or flattens a broker order.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import inspect
import logging
import os
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from mike_trade.domain.errors import PersistenceError

__all__ = [
    "ExecutionMode", "StartupState", "RuntimeStartupError", "RuntimeStatus",
    "RuntimeInstanceLock", "EntryGate", "Runtime", "SystemClock", "ClockPort",
    "SchedulerPort", "BrokerPort", "MarketDataPort", "ResearchPort",
    "PersistencePort", "PortfolioPort", "RiskPort", "ExecutionPort",
    "ReportingPort", "ControlsPort",
]


class ExecutionMode(StrEnum):
    BACKTEST = "BACKTEST"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE = "LIVE"


class StartupState(StrEnum):
    CREATED = "CREATED"
    ACQUIRING_INSTANCE_LOCK = "ACQUIRING_INSTANCE_LOCK"
    OPENING_DATABASE = "OPENING_DATABASE"
    LOADING_CONTROLS = "LOADING_CONTROLS"
    VALIDATING_ENVIRONMENT = "VALIDATING_ENVIRONMENT"
    FETCHING_BROKER_FACTS = "FETCHING_BROKER_FACTS"
    RECONCILING = "RECONCILING"
    VERIFYING_SAFETY = "VERIFYING_SAFETY"
    WARMING_MARKET_DATA = "WARMING_MARKET_DATA"
    ESTABLISHING_FRESHNESS = "ESTABLISHING_FRESHNESS"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"
    BLOCKED = "BLOCKED"


class ClockPort(Protocol):
    def now(self) -> datetime: ...


class SchedulerPort(Protocol):
    def schedule(self, name: str, due: datetime) -> None: ...


class BrokerPort(Protocol):
    def submit_order(self, order: object) -> object: ...
    def cancel_order(self, order_id: str) -> object: ...


class MarketDataPort(Protocol):
    def subscribe(self, symbols: Sequence[str]) -> object: ...


class ResearchPort(Protocol):
    def fetch_inputs(self, symbol: str) -> Mapping[str, object]: ...


class PersistencePort(Protocol):
    def append_event(self, event: Mapping[str, object]) -> None: ...


class PortfolioPort(Protocol):
    def positions(self) -> Sequence[object]: ...


class RiskPort(Protocol):
    def check_entry(self, intent: object) -> None: ...


class ExecutionPort(Protocol):
    def submit_intent(self, intent: object) -> object: ...


class ReportingPort(Protocol):
    def publish(self, report: Mapping[str, object]) -> None: ...


class ControlsPort(Protocol):
    def entries_enabled(self) -> bool: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock.now() must return a timezone-aware datetime")
    return value.astimezone(UTC)


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _call_optional(target: object | None, names: Sequence[str], *args: object) -> Any:
    if target is None:
        return None
    for name in names:
        method = getattr(target, name, None)
        if callable(method):
            return await _await(method(*args))
    return None


def _result_clean(result: object) -> tuple[bool, tuple[str, ...], str]:
    """Normalize explicit validation results; an absent optional check is clean."""
    if result is None:
        return True, (), "CLEAN"
    if isinstance(result, bool):
        reasons = () if result else ("VALIDATION_FAILED",)
        return result, reasons, "CLEAN" if result else "DRIFTED"
    clean = getattr(result, "clean", None)
    if clean is None and isinstance(result, Mapping):
        clean = result.get("clean")
    if clean is None:
        clean = getattr(result, "entries_permitted", None)
    if clean is None:
        return True, (), "CLEAN"
    raw_reasons = getattr(result, "reasons", ())
    if isinstance(result, Mapping):
        raw_reasons = result.get("reasons", raw_reasons)
    reasons = tuple(str(item) for item in (raw_reasons or ()))
    status = str(getattr(result, "status", "CLEAN" if clean else "DRIFTED"))
    return bool(clean), reasons or (() if clean else ("RECONCILIATION_DRIFT",)), status


class RuntimeStartupError(RuntimeError):
    """A concrete startup phase failed; the runtime remains entry-blocked."""

    def __init__(self, state: StartupState, reason: str) -> None:
        self.state = state
        self.reason = reason
        super().__init__(f"startup blocked during {state.value}: {reason}")


class RuntimeInstanceLock:
    """Non-blocking process lock independent of SQLite's writer lock."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise PersistenceError(
                f"another mike_trade instance holds {self.path}",
                code="instance_lock_held", context={"lock_path": str(self.path)},
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    close = release


@dataclass(slots=True)
class EntryGate:
    """Single fail-closed gate consulted only by fresh-entry paths."""

    authorized: bool = False
    reasons: tuple[str, ...] = ("NOT_STARTED",)

    def block(self, *reasons: str) -> None:
        self.authorized = False
        normalized = tuple(str(reason).strip() for reason in reasons if str(reason).strip())
        self.reasons = normalized or ("ENTRY_BLOCKED",)

    def allow(self) -> None:
        self.authorized = True
        self.reasons = ()

    def entries_enabled(self) -> bool:
        return self.authorized


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    state: StartupState
    entries_allowed: bool
    reasons: tuple[str, ...]
    completed_steps: tuple[StartupState, ...]
    heartbeat_at: datetime
    reconciliation_status: str = "PENDING"
    last_error: str | None = None


@dataclass(slots=True)
class Runtime:
    """Full injected application graph plus its ordered runtime lifecycle."""

    mode: ExecutionMode
    clock: ClockPort
    scheduler: SchedulerPort
    broker: BrokerPort
    market_data: MarketDataPort
    research: ResearchPort | None
    persistence: PersistencePort
    portfolio: PortfolioPort
    risk: RiskPort
    execution: ExecutionPort
    reporting: ReportingPort
    controls: ControlsPort
    logger: logging.Logger
    calendar: object | None = None
    candle_engine: object | None = None
    strategies: object | None = None
    eligibility: object | None = None
    budget_manager: object | None = None
    protection: object | None = None
    reconciliation: object | None = None
    instance_lock: object | None = None
    config_fingerprint: str = ""
    account_scope_id: str = ""
    symbols: tuple[str, ...] = ()
    startup_validator: object | None = None
    lifecycle: object | None = None
    market_status_monitor: object | None = None
    entry_gate: EntryGate = field(default_factory=EntryGate)
    drain_timeout: float = 10.0
    heartbeat_interval: float = 5.0
    state: StartupState = field(default=StartupState.CREATED, init=False)
    completed_steps: list[StartupState] = field(default_factory=list, init=False)
    last_error: str | None = field(default=None, init=False)
    reconciliation_status: str = field(default="PENDING", init=False)
    broker_facts: dict[str, object] = field(default_factory=dict, init=False)
    _accepting_evaluations: bool = field(default=False, init=False)
    _signal_handlers_installed: bool = field(default=False, init=False)
    _heartbeat_task: asyncio.Task[None] | None = field(default=None, init=False)

    @property
    def entries_allowed(self) -> bool:
        return self.state is StartupState.RUNNING and self.entry_gate.authorized

    @property
    def entry_block_reasons(self) -> tuple[str, ...]:
        return () if self.entries_allowed else self.entry_gate.reasons

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            state=self.state, entries_allowed=self.entries_allowed,
            reasons=self.entry_block_reasons, completed_steps=tuple(self.completed_steps),
            heartbeat_at=_utc(self.clock.now()),
            reconciliation_status=self.reconciliation_status, last_error=self.last_error,
        )

    def _transition(self, state: StartupState, *block_reasons: str) -> None:
        self.state = state
        self.completed_steps.append(state)
        if state is not StartupState.RUNNING:
            self.entry_gate.block(*(block_reasons or (state.value,)))

    async def _restore_market_status(self) -> None:
        if self.market_status_monitor is None:
            return
        repo = getattr(self.market_status_monitor, "repository", None)
        if repo is None:
            session = getattr(self.persistence, "session", None)
            if session is not None:
                from mike_trade.market_status.persistence import SqliteMarketStatusRepository

                repo = SqliteMarketStatusRepository(session, clock=self.clock)
        if repo is not None:
            scope = self.account_scope_id or "default"
            try:
                doc = repo.load(scope)
                restore_fn = getattr(self.market_status_monitor, "restore", None)
                if doc and callable(restore_fn):
                    restore_fn(doc)
            except Exception as exc:
                # Cannot tell whether the market was in CRASH: block new entries until the
                # monitor has re-observed a full lookback.  Exits are never affected.
                self.logger.error(
                    "Market status restore failed; entries fail closed",
                    extra={"error": type(exc).__name__},
                )
                fail_closed = getattr(self.market_status_monitor, "fail_closed", None)
                if callable(fail_closed):
                    fail_closed("MARKET_STATUS_RESTORE_FAILED")

    async def _persist_market_status(self) -> None:
        if self.market_status_monitor is None:
            return
        repo = getattr(self.market_status_monitor, "repository", None)
        if repo is None:
            session = getattr(self.persistence, "session", None)
            if session is not None:
                from mike_trade.market_status.persistence import SqliteMarketStatusRepository

                repo = SqliteMarketStatusRepository(session, clock=self.clock)
        if repo is not None:
            scope = self.account_scope_id or "default"
            snapshot_fn = getattr(self.market_status_monitor, "snapshot", None)
            if callable(snapshot_fn):
                try:
                    repo.save(scope, snapshot_fn())
                except Exception as exc:  # persistence trouble must not stop the heartbeat
                    self.logger.error(
                        "Market status persist failed", extra={"error": type(exc).__name__}
                    )

    async def _heartbeat(self) -> None:
        details = {
            "state": self.state.value,
            "entries_allowed": str(self.entries_allowed).lower(),
            "entry_block_reasons": ",".join(self.entry_block_reasons),
            "reconciliation_status": self.reconciliation_status,
        }
        if self.market_status_monitor is not None:
            eval_fn = getattr(self.market_status_monitor, "evaluate", None)
            if callable(eval_fn):
                with contextlib.suppress(Exception):
                    mkt_eval = eval_fn()
                    details["market_state"] = mkt_eval.state.value
                    details["market_buy_allowed"] = str(mkt_eval.authorization.buy_allowed).lower()
        await self._persist_market_status()
        await _call_optional(self.persistence, ("heartbeat", "publish_heartbeat"), details)
        await _call_optional(self.broker, ("refresh_balance_snapshots",))
        try:  # model status is advisory: its failure must never stop the heartbeat
            model_details = await _call_optional(self.lifecycle, ("model_health",))
            if isinstance(model_details, Mapping):
                await _call_optional(
                    self.persistence, ("heartbeat_component",), "models", model_details
                )
        except Exception as exc:
            self.logger.error("Model status publication failed", extra={"error": type(exc).__name__})
        feed_details = await _call_optional(self.market_data, ("health_details",))
        if isinstance(feed_details, Mapping):
            await _call_optional(
                self.persistence,
                ("heartbeat_component",),
                "feed",
                feed_details,
            )

    async def _lifecycle_tick(self) -> None:
        """Time-driven lifecycle work (e.g. EOD liquidation) that needs no new candle."""
        try:
            await _call_optional(self.lifecycle, ("on_tick",))
        except Exception as exc:  # a failed tick must never stop the heartbeat
            self.logger.error("Lifecycle tick failed", exc_info=exc)

    async def _heartbeat_loop(self) -> None:
        try:
            while self.state in {StartupState.RUNNING, StartupState.PAUSED}:
                await asyncio.sleep(self.heartbeat_interval)
                if self.state in {StartupState.RUNNING, StartupState.PAUSED}:
                    await self._heartbeat()
                    await self._lifecycle_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.error("Heartbeat publication failed", exc_info=exc)

    async def _stop_heartbeat_loop(self) -> None:
        task, self._heartbeat_task = self._heartbeat_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def start(self) -> RuntimeStatus:
        """Execute the production startup state machine in the documented order."""
        if self.state is StartupState.RUNNING:
            return self.status()
        if self.state not in {StartupState.CREATED, StartupState.STOPPED, StartupState.BLOCKED}:
            raise RuntimeError(f"cannot start runtime from {self.state.value}")
        self.completed_steps.clear()
        self.last_error = None
        self.reconciliation_status = "PENDING"
        self._accepting_evaluations = False
        self.entry_gate.block("STARTING")
        phase = StartupState.ACQUIRING_INSTANCE_LOCK
        try:
            self._transition(phase, "INSTANCE_LOCK_PENDING")
            await _call_optional(self.instance_lock, ("acquire", "open"))

            phase = StartupState.OPENING_DATABASE
            self._transition(phase, "DATABASE_NOT_READY")
            await _call_optional(
                self.persistence,
                ("open_migrate_integrity", "open_and_migrate", "open"),
            )

            phase = StartupState.LOADING_CONTROLS
            self._transition(phase, "CONTROLS_NOT_LOADED")
            await _call_optional(
                self.persistence, ("load_durable_controls", "load_controls"),
                self.config_fingerprint,
            )
            await _call_optional(self.controls, ("load", "restore"))
            await self._restore_market_status()

            phase = StartupState.VALIDATING_ENVIRONMENT
            self._transition(phase, "ENVIRONMENT_NOT_VALIDATED")
            validated_components: set[int] = set()
            for component in (
                self.startup_validator,
                self.calendar,
                self.broker,
                self.market_data,
                self.eligibility,
            ):
                if component is None or id(component) in validated_components:
                    continue
                validated_components.add(id(component))
                result = await _call_optional(
                    component, ("validate_startup", "validate", "check_capabilities")
                )
                clean, reasons, _ = _result_clean(result)
                if not clean:
                    raise RuntimeError("; ".join(reasons))
            # Capability discovery and asset checks are distinct concrete facts
            # from adapter construction/credential validation.
            await _call_optional(self.broker, ("capabilities",))
            await _call_optional(self.market_data, ("capabilities",))
            assets = await _call_optional(
                self.eligibility, ("validate_assets", "check_asset_eligibility"),
                self.symbols,
            )
            clean, reasons, _ = _result_clean(assets)
            if not clean:
                raise RuntimeError("; ".join(reasons))

            phase = StartupState.FETCHING_BROKER_FACTS
            self._transition(phase, "BROKER_FACTS_PENDING")
            self.broker_facts = await self._fetch_broker_facts()

            phase = StartupState.RECONCILING
            self._transition(phase, "RECONCILIATION_PENDING")
            reconciliation = None
            if self.reconciliation is not None:
                method = getattr(self.reconciliation, "reconcile", None) or getattr(
                    self.reconciliation, "run", None
                )
                if callable(method):
                    try:
                        reconciliation = await _await(method(self.broker_facts))
                    except TypeError:
                        reconciliation = await _await(method())
            clean, reasons, status = _result_clean(reconciliation)
            self.reconciliation_status = status
            if not clean:
                raise RuntimeError("; ".join(reasons))

            phase = StartupState.VERIFYING_SAFETY
            self._transition(phase, "SAFETY_NOT_VERIFIED")
            for component in (self.budget_manager, self.eligibility, self.protection, self.risk):
                result = await _call_optional(
                    component, ("verify_startup", "verify", "check_startup")
                )
                clean, reasons, _ = _result_clean(result)
                if not clean:
                    raise RuntimeError("; ".join(reasons))

            phase = StartupState.WARMING_MARKET_DATA
            self._transition(phase, "MARKET_DATA_NOT_WARM")
            warm = await _call_optional(
                self.market_data, ("warm", "warmup", "warm_symbols"), self.symbols
            )
            clean, reasons, _ = _result_clean(warm)
            if not clean:
                raise RuntimeError("; ".join(reasons))

            phase = StartupState.ESTABLISHING_FRESHNESS
            self._transition(phase, "FEED_NOT_FRESH")
            await _call_optional(self.market_data, ("start", "connect"))
            fresh = await _call_optional(
                self.market_data, ("establish_freshness", "verify_freshness", "is_fresh")
            )
            if fresh is False:
                raise RuntimeError("market-data feed is not fresh")
            order_fresh = await _call_optional(
                self.broker, ("establish_order_update_freshness", "order_updates_fresh")
            )
            if order_fresh is False:
                raise RuntimeError("broker order-update stream is not fresh")

            # Restart recovery: finish any pending end-of-day exit before entries open.
            await self._lifecycle_tick()

            operator_authorized = True
            persisted_enabled = await _call_optional(
                self.persistence, ("entries_enabled",)
            )
            if persisted_enabled is False:
                operator_authorized = False
            if self.controls is not self.entry_gate:
                enabled = await _call_optional(self.controls, ("entries_enabled",))
                operator_authorized = operator_authorized and enabled is not False

            self.state = StartupState.RUNNING
            self.completed_steps.append(StartupState.RUNNING)
            if operator_authorized:
                self.entry_gate.allow()
            else:
                self.entry_gate.block("ENTRIES_DISABLED_BY_DURABLE_CONTROL")
            self._accepting_evaluations = True
            await _call_optional(self.scheduler, ("start",))
            await _call_optional(self.lifecycle, ("start_background",))
            await self._heartbeat()
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name="mike-trade-heartbeat"
            )
            self.logger.info("Runtime running", extra={"mode": self.mode.value})
            return self.status()
        except BaseException as exc:
            error_code = getattr(exc, "code", None)
            prefix = f"{error_code}: " if error_code else ""
            reason = f"{prefix}{type(exc).__name__}: {exc}"
            self.last_error = reason
            self.state = StartupState.BLOCKED
            self.completed_steps.append(StartupState.BLOCKED)
            self.entry_gate.block(reason)
            self._accepting_evaluations = False
            try:
                await self._heartbeat()
            except Exception:
                pass
            await self._stop_heartbeat_loop()
            await _call_optional(self.lifecycle, ("stop_background",))
            await _call_optional(self.scheduler, ("stop", "close"))
            await _call_optional(self.persistence, ("flush",))
            await _call_optional(self.market_data, ("close", "unsubscribe_all", "stop"))
            await _call_optional(self.persistence, ("close",))
            await _call_optional(self.instance_lock, ("release", "close"))
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise RuntimeStartupError(phase, reason) from exc

    async def _fetch_broker_facts(self) -> dict[str, object]:
        facts: dict[str, object] = {}
        for key, names in (
            ("account", ("get_account",)),
            ("open_orders", ("list_open_orders",)),
            ("linked_orders", ("list_linked_orders",)),
            ("positions", ("list_positions",)),
        ):
            facts[key] = await _call_optional(self.broker, names)
        activities = getattr(self.broker, "list_recent_activities", None)
        if callable(activities):
            facts["recent_activities"] = await _await(activities())
        else:
            fills = getattr(self.broker, "list_fills", None)
            facts["recent_activities"] = (
                await _await(fills(datetime.min.replace(tzinfo=UTC))) if callable(fills) else ()
            )
        return facts

    def pause(self, reason: str = "OPERATOR_PAUSE") -> RuntimeStatus:
        """Pause entries only; fills, exits, protection, and reconciliation continue."""
        if self.state is StartupState.RUNNING:
            self.state = StartupState.PAUSED
            self.entry_gate.block(reason)
        return self.status()

    def resume(self) -> RuntimeStatus:
        if self.state is StartupState.PAUSED and self.reconciliation_status == "CLEAN":
            self.state = StartupState.RUNNING
            self.entry_gate.allow()
        return self.status()

    async def stop(self) -> RuntimeStatus:
        """Drain and release resources without any broker trading side effect."""
        if self.state is StartupState.STOPPED:
            return self.status()
        self.state = StartupState.DRAINING
        self.entry_gate.block("SHUTTING_DOWN")
        self._accepting_evaluations = False
        await self._stop_heartbeat_loop()
        await _call_optional(self.lifecycle, ("stop_background",))
        await self._heartbeat()
        try:
            await asyncio.wait_for(
                _call_optional(self.execution, ("drain", "drain_in_flight")),
                timeout=self.drain_timeout,
            )
        except TimeoutError:
            self.last_error = "evaluation drain timed out"
        await _call_optional(self.scheduler, ("stop", "close"))
        await _call_optional(self.persistence, ("flush", "checkpoint"))
        await _call_optional(self.market_data, ("close", "unsubscribe_all", "stop"))
        await _call_optional(self.persistence, ("close",))
        await _call_optional(self.instance_lock, ("release", "close"))
        self.state = StartupState.STOPPED
        self.completed_steps.append(StartupState.STOPPED)
        self.entry_gate.block("STOPPED")
        self.logger.info("Runtime stopped", extra={"mode": self.mode.value})
        return self.status()

    def install_signal_handlers(self) -> None:
        """Map SIGTERM/SIGINT to graceful ``stop`` on the active event loop."""
        if self._signal_handlers_installed:
            return
        loop = asyncio.get_running_loop()

        def request_stop() -> None:
            if self.state not in {StartupState.DRAINING, StartupState.STOPPED}:
                loop.create_task(self.stop(), name="mike-trade-graceful-stop")

        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(signum, request_stop)
            except (NotImplementedError, RuntimeError):
                signal.signal(signum, lambda *_args: request_stop())
        self._signal_handlers_installed = True
