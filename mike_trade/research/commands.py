"""CLI workflows for persistent market history and reproducible experiments."""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import contextlib
import hashlib
import json
import os
import tempfile
import time
import traceback
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, time as dt_time, timedelta
from decimal import ROUND_DOWN, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from mike_trade.host_resources import (
    auto_worker_ceiling,
    available_cpu_count,
    available_memory_bytes as _available_memory_bytes,
)
from mike_trade.research.datasets import HistoricalDatasetRepository


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    """Serializable description of one independent replay experiment."""

    symbol: str
    strategy: str
    start: str
    end: str
    seed: int = 271828
    dataset_id: str | None = None
    feed: str | None = None
    parameters: Mapping[str, Mapping[str, Any]] | None = None
    starting_capital: str = "10000"
    execution: Mapping[str, Any] | None = None
    #: Explicit BUY / SELL strategy collections for this symbol. When both are
    #: omitted the job runs the production ``vwap_reference`` pair and
    #: ``strategy`` is only the recorded focus label (both legs always execute).
    buy_strategies: tuple[str, ...] | None = None
    sell_strategies: tuple[str, ...] | None = None
    #: Optional SHORT / COVER collections.  Shorting is opt-in and fails closed:
    #: ``short_selling_enabled`` must be true for any SHORT strategy to be eligible, and
    #: ``short_borrow_bps_per_day`` is an explicit *estimated* stock-borrow assumption.
    short_strategies: tuple[str, ...] | None = None
    cover_strategies: tuple[str, ...] | None = None
    short_selling_enabled: bool = False
    short_borrow_bps_per_day: str = "0"
    #: ``stop_policy``-shaped protective stop (fixed / native_trailing /
    #: stepped_trailing); omitted keeps the reference 1.5% native trailing stop.
    protective_stop: Mapping[str, Any] | None = None
    #: Optional replay EOD close policy: {"enabled": bool, "at": "HH:MM"}.
    end_of_day_close: Mapping[str, Any] | None = None
    #: Maximum completed entry cycles per symbol/session; research runner gate.
    max_entry_cycles_per_session: int = 1
    #: Root of the immutable model repository; required when any selected strategy is
    #: model-driven (``xgboost_probability_*`` / ``lightgbm_probability_*``).
    model_root: str | None = None
    #: Optional exact snapshot ids for the model's reference series (for example SPY); when
    #: omitted the newest same-feed snapshot covering the window is used.
    reference_dataset_ids: Mapping[str, str] | None = None
    #: Replay the Market Status Monitor (SPY/QQQ/IWM, same feed) so CRASH/WARNING states gate
    #: new entries exactly as in SHADOW.
    market_status_enabled: bool = False

    def validate(self) -> None:
        if not self.symbol or not self.symbol.replace(".", "").isalnum():
            raise ValueError(f"invalid experiment symbol: {self.symbol!r}")
        explicit = any(
            getattr(self, name) is not None
            for name in ("buy_strategies", "sell_strategies", "short_strategies",
                         "cover_strategies")
        )
        if explicit:
            if not self.strategy or not self.strategy.replace("_", "").replace("-", "").isalnum():
                raise ValueError(f"invalid experiment label: {self.strategy!r}")
            for name in ("buy_strategies", "sell_strategies", "short_strategies",
                         "cover_strategies"):
                value = getattr(self, name)
                if value is not None and (
                    isinstance(value, str) or any(not isinstance(i, str) for i in value)
                ):
                    raise ValueError(f"{name} must be a list of strategy ids")
            if not self.buy_strategies and not self.short_strategies:
                raise ValueError("an explicit lifecycle needs at least one BUY strategy")
            if self.short_strategies and not self.short_selling_enabled:
                raise ValueError(
                    "short_strategies require short_selling_enabled=True "
                    "(shorting is opt-in and fails closed)"
                )
            if Decimal(self.short_borrow_bps_per_day) < 0:
                raise ValueError("short_borrow_bps_per_day must be >= 0")
        elif self.strategy not in {"vwap_bounce", "vwap_breakdown"}:
            raise ValueError(f"unsupported registered lifecycle focus: {self.strategy!r}")
        if self.protective_stop is not None:
            from mike_trade.risk.stop_config import normalize_stop_spec

            normalize_stop_spec(self.protective_stop)
        if self.model_root is not None and (
            not isinstance(self.model_root, str) or not self.model_root.strip()
        ):
            raise ValueError("model_root must be a non-empty path string")
        if self.seed < 0:
            raise ValueError("experiment seed must be nonnegative")
        if isinstance(self.max_entry_cycles_per_session, bool) or not isinstance(
            self.max_entry_cycles_per_session, int
        ) or self.max_entry_cycles_per_session < 1:
            raise ValueError("max_entry_cycles_per_session must be a positive integer")
        if _moment(self.start) >= _moment(self.end):
            raise ValueError("experiment start must precede end")
        if self.parameters is not None and not isinstance(self.parameters, Mapping):
            raise ValueError("experiment parameters must be a strategy-id mapping")
        if Decimal(self.starting_capital) <= 0:
            raise ValueError("starting_capital must be a positive decimal string")
        if self.execution is not None and not isinstance(self.execution, Mapping):
            raise ValueError("execution assumptions must be a mapping")
        if self.feed is not None:
            if not isinstance(self.feed, str) or self.feed.strip().lower() not in ("iex", "sip"):
                raise ValueError("experiment feed must be 'iex' or 'sip'")
        if self.end_of_day_close is not None:
            if not isinstance(self.end_of_day_close, Mapping):
                raise ValueError("end_of_day_close must be a mapping")
            if set(self.end_of_day_close) - {"enabled", "at"}:
                raise ValueError("end_of_day_close accepts only enabled and at")
            enabled = self.end_of_day_close.get("enabled", False)
            at = self.end_of_day_close.get("at", "15:55")
            if not isinstance(enabled, bool) or not isinstance(at, str):
                raise ValueError("end_of_day_close requires bool enabled and HH:MM at")
            try:
                parsed_time = dt_time.fromisoformat(at)
                if len(at) != 5 or parsed_time >= dt_time(16, 0):
                    raise ValueError
            except ValueError as exc:
                raise ValueError("end_of_day_close.at must be HH:MM before 16:00") from exc


#: Default working-set estimate for one experiment process (override per batch).
DEFAULT_WORKER_MEMORY_BYTES = 512 * 1024 * 1024


def resolve_worker_count(
    requested: str | int,
    job_count: int,
    *,
    memory_per_worker_bytes: int = DEFAULT_WORKER_MEMORY_BYTES,
) -> int:
    """Pick the process count: ``auto`` or an explicit value, bounded by real resources.

    ``auto`` uses the CPUs minus a small co-tenant reserve; an explicit count is honoured up to
    the CPUs available.  Both are lowered only when free memory cannot hold that many workers.
    """
    if job_count < 1:
        raise ValueError("a batch must contain at least one experiment")
    if memory_per_worker_bytes < 1:
        raise ValueError("memory_per_worker_bytes must be positive")
    cpu_limit = available_cpu_count()
    available_memory = _available_memory_bytes()
    memory_limit = (
        cpu_limit
        if available_memory is None
        else max(1, available_memory // memory_per_worker_bytes)
    )
    if str(requested).lower() == "auto":
        return max(1, min(auto_worker_ceiling(cpu_limit), memory_limit, job_count))
    try:
        workers = int(requested)
    except (TypeError, ValueError) as exc:
        raise ValueError("--workers must be a positive integer or 'auto'") from exc
    if workers < 1:
        raise ValueError("--workers must be at least 1")
    return min(workers, cpu_limit, memory_limit, job_count)


_NATIVE_THREAD_VARIABLES = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)


@contextlib.contextmanager
def _single_threaded_workers() -> Iterator[None]:
    """Children inherit one-thread native pools: parallelism comes from the process count.

    Without this every worker's OpenMP/BLAS runtime would start one thread per CPU and N workers
    would oversubscribe the host N-fold.  A value the operator already exported is kept.
    """
    added = [name for name in _NATIVE_THREAD_VARIABLES if name not in os.environ]
    for name in added:
        os.environ[name] = "1"
    try:
        yield
    finally:
        for name in added:
            os.environ.pop(name, None)


def run_experiment_batch(
    jobs: list[ExperimentSpec],
    *,
    workers: str | int,
    root: str,
    results: str,
    batch_file: str | None = None,
    mp_context: Any | None = None,
    memory_per_worker_bytes: int = DEFAULT_WORKER_MEMORY_BYTES,
) -> dict[str, Any]:
    """Bounded process coordinator; each child invokes the existing runner.

    ``mp_context`` selects the multiprocessing start method.  Model campaigns pass ``spawn``:
    a forked child of a process that has initialized an OpenMP runtime (XGBoost/LightGBM) is
    unsafe.
    """
    repository = HistoricalDatasetRepository(root)
    resolved_jobs = []
    for job in jobs:
        job.validate()
        if job.dataset_id is None:
            start, end = _moment(job.start), _moment(job.end)
            target_feed = (job.feed or "iex").lower()
            candidates = [
                snapshot
                for snapshot in repository.snapshots()
                if snapshot.symbol == job.symbol.upper()
                and snapshot.feed == target_feed
                and snapshot.adjustment == "raw"
                and datetime.fromisoformat(snapshot.start) <= start
                and datetime.fromisoformat(snapshot.end) >= end
            ]
            if candidates:
                job = replace(
                    job, dataset_id=max(candidates, key=lambda item: item.acquired_at).dataset_id
                )
        resolved_jobs.append(job)
    jobs = resolved_jobs
    selected_workers = resolve_worker_count(
        workers, len(jobs), memory_per_worker_bytes=memory_per_worker_bytes
    )
    started = time.monotonic()
    output = Path(batch_file) if batch_file else Path(results) / "batch-results.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    batch_started_at = datetime.now(UTC)
    failure_log = output.with_name(
        f"{output.stem}.failures.{batch_started_at.strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}.jsonl"
    )
    completed: list[dict[str, Any]] = []
    pending_specs = iter(enumerate(jobs))
    in_flight: dict[concurrent.futures.Future[dict[str, Any]], int] = {}
    thread_scope = _single_threaded_workers()
    thread_scope.__enter__()
    pool = (
        ProcessPoolExecutor(max_workers=selected_workers)
        if mp_context is None
        else ProcessPoolExecutor(max_workers=selected_workers, mp_context=mp_context)
    )
    try:

        def submit_next() -> bool:
            try:
                index, spec = next(pending_specs)
            except StopIteration:
                return False
            future = pool.submit(_execute_experiment, asdict(spec), root, results)
            in_flight[future] = index
            return True

        # At most 2x workers are queued; completed output is aggregated here.
        for _ in range(min(len(jobs), selected_workers * 2)):
            submit_next()
        while in_flight:
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                index = in_flight.pop(future)
                try:
                    result = future.result()
                    result["job_index"] = index
                    completed.append(result)
                except BaseException as exc:
                    spec = jobs[index]
                    failure = {
                        "job_index": index,
                        "job_id": hashlib.sha256(
                            json.dumps(asdict(spec), sort_keys=True, default=str).encode()
                        ).hexdigest(),
                        "symbol": spec.symbol.upper(),
                        "strategy_configuration": spec.strategy,
                        "status": "failed",
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                        "traceback": "".join(traceback.format_exception(exc))[-12000:],
                        "timestamp": datetime.now(UTC).isoformat(),
                    }
                    # Keep the historical batch/report field names while adding the complete,
                    # self-describing failure record.
                    failure["error_type"] = failure["exception_type"]
                    failure["error"] = failure["exception_message"]
                    with failure_log.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(failure, sort_keys=True, default=str) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    completed.append(failure)
                    print(
                        f"research failure job_index={index} job_id={failure['job_id']} "
                        f"symbol={failure['symbol']} "
                        f"configuration={failure['strategy_configuration']} "
                        f"{failure['exception_type']}: {failure['exception_message']} "
                        f"failure_log={failure_log}",
                        flush=True,
                    )
                counts = {
                    state: sum(row["status"] == state for row in completed)
                    for state in ("completed", "failed")
                }
                print(
                    f"research progress completed={counts['completed']} failed={counts['failed']} "
                    f"total={len(jobs)} elapsed={time.monotonic() - started:.1f}s",
                    flush=True,
                )
                submit_next()
    except KeyboardInterrupt:
        for future in in_flight:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    except BaseException:
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)
    finally:
        thread_scope.__exit__(None, None, None)

    completed.sort(key=lambda row: row["job_index"])
    elapsed = time.monotonic() - started
    successes = sum(row["status"] == "completed" for row in completed)
    failures = len(completed) - successes
    batch = {
        "status": "completed" if failures == 0 else "completed_with_failures",
        "workers_requested": str(workers),
        "workers_used": selected_workers,
        "cpu_limit": available_cpu_count(),
        "memory_available_bytes": _available_memory_bytes(),
        "elapsed_seconds": round(elapsed, 3),
        "throughput_experiments_per_second": round(successes / elapsed, 4) if elapsed else 0,
        "completed_count": successes,
        "failed_count": failures,
        "failure_log": str(failure_log) if failures else None,
        "jobs": completed,
    }
    output.write_text(json.dumps(batch, sort_keys=True, indent=2, default=str) + "\n")
    print(json.dumps({key: value for key, value in batch.items() if key != "jobs"}, sort_keys=True))
    print(f"batch results: {output}")
    return batch


def _execute_experiment(job_data: dict[str, Any], root: str, results: str) -> dict[str, Any]:
    spec = ExperimentSpec(**job_data)
    spec.validate()
    result = _run_one(
        spec.symbol.upper(),
        _moment(spec.start),
        _moment(spec.end),
        root,
        results,
        spec.seed,
        spec.strategy,
        dataset_id=spec.dataset_id,
        feed=spec.feed,
        parameters=spec.parameters,
        starting_capital=Decimal(spec.starting_capital),
        execution=spec.execution,
        buy_strategies=spec.buy_strategies,
        sell_strategies=spec.sell_strategies,
        short_strategies=spec.short_strategies,
        cover_strategies=spec.cover_strategies,
        short_selling_enabled=spec.short_selling_enabled,
        short_borrow_bps_per_day=Decimal(spec.short_borrow_bps_per_day),
        protective_stop=spec.protective_stop,
        end_of_day_close=spec.end_of_day_close,
        max_entry_cycles_per_session=spec.max_entry_cycles_per_session,
        model_root=spec.model_root,
        reference_dataset_ids=spec.reference_dataset_ids,
        market_status_enabled=spec.market_status_enabled,
    )
    ledger = result.pop("trade_ledger")
    ledger_dir = Path(results) / "trade_ledgers"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = ledger_dir / f"{result['run_id']}.json"
    ledger_payload = json.dumps(
        {"run_id": result["run_id"], "dataset_id": result["dataset_id"],
         "trades": ledger}, sort_keys=True, indent=2
    ) + "\n"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=ledger_dir,
                                     suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(ledger_payload)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        try:
            os.link(temporary, ledger_path)
        except FileExistsError:
            if ledger_path.read_text(encoding="utf-8") != ledger_payload:
                raise ValueError(f"conflicting existing trade ledger: {ledger_path}") from None
    finally:
        temporary.unlink(missing_ok=True)
    result["trade_ledger_file"] = str(ledger_path)
    result.update(status="completed", worker_pid=os.getpid())
    return result


@lru_cache(maxsize=2)
def _cached_snapshot_bars(
    root: str, symbol: str, start: str, end: str, dataset_id: str, feed: str | None = None
) -> tuple[Any, ...]:
    """Bounded per-process cache of validated rows from immutable snapshots."""
    effective_feed = (feed or "").strip().lower()
    if not effective_feed:
        parts = dataset_id.split("-")
        effective_feed = (
            parts[1].lower()
            if len(parts) >= 2 and parts[1].lower() in ("iex", "sip", "delayed_sip")
            else "iex"
        )
    return HistoricalDatasetRepository(root).available(
        symbol, _moment(start), _moment(end), feed=effective_feed, adjustment="raw", dataset_id=dataset_id
    )


def _daily_history(
    repo: HistoricalDatasetRepository,
    registry: Any,
    symbol: str,
    start: datetime,
    end: datetime,
    strategy_params: Mapping[str, Mapping[str, Any]],
    feed: str = "iex",
) -> tuple[Any, str | None]:
    """Completed RAW D1 history (point-in-time store) for strategies that require it.

    Returns ``(None, None)`` when no selected strategy needs D1/W1, leaving the M1 backtest
    path untouched.  Otherwise loads an immutable ``1d`` snapshot covering the lookback before
    ``start`` (plus a margin for weekly aggregation and missing-session checks) through ``end``.
    """
    from mike_trade.market_data.candles import NY, CandleTimeframe
    from mike_trade.market_data.session_candles import SessionCandleStore
    from mike_trade.scheduling.calendar import previous_session

    needed = 0
    for strategy_id, params in strategy_params.items():
        history = registry.get(strategy_id).required_history(params)
        for frame, count in history.items():
            needed = max(needed, count if frame is CandleTimeframe.D1 else 5 * count + 5)
    if needed == 0:
        return None, None
    first = start.astimezone(NY).date()
    for _ in range(needed + 10):
        first = previous_session(first).session_date
    last = end.astimezone(NY).date()
    candles = repo.daily_candles(symbol, first, last, feed=feed)
    store = SessionCandleStore()
    store.ingest_daily(symbol, candles)
    return store, store.fingerprint()[:20]


MULTIDAY_STRATEGIES = frozenset({"cup_and_handle", "volatility_contraction_pattern"})


def _resolve_eod_close(
    end_of_day_close: Mapping[str, Any] | None,
    strategy_ids: Iterable[str],
) -> dict[str, Any] | None:
    """Resolve effective EOD close configuration for research backtests.

    Intraday research backtests default to {"enabled": True, "at": "15:55"} in New York time.
    Explicit per-experiment configurations override this (including {"enabled": False}).
    Intentionally multi-day strategies do not have EOD liquidation forced upon them.
    """
    if end_of_day_close is not None:
        return dict(end_of_day_close)
    active = {s for s in strategy_ids if s}
    if any(s in MULTIDAY_STRATEGIES for s in active):
        return None
    return {"enabled": True, "at": "15:55"}


def _moment(text: str) -> datetime:
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return value.astimezone(UTC)


def register(subparsers: Any) -> None:
    data = subparsers.add_parser("research-data", help="manage persistent historical datasets")
    actions = data.add_subparsers(dest="research_data_action", required=True)
    acquire = actions.add_parser("acquire", help="incrementally acquire Alpaca historical bars")
    acquire.add_argument("--symbols", required=True, help="comma-separated symbols")
    acquire.add_argument("--start", required=True, help="inclusive ISO timestamp")
    acquire.add_argument("--end", required=True, help="exclusive ISO timestamp")
    acquire.add_argument("--feed", default="iex", choices=("iex", "sip", "delayed_sip"))
    acquire.add_argument("--adjustment", default="raw", choices=("raw", "split", "dividend", "all"))
    acquire.add_argument(
        "--timeframe", default="1m", choices=("1m", "1d"),
        help="1m: minute bars; 1d: completed RAW daily bars (the D1/W1 strategy history)",
    )
    acquire.add_argument("--root", default=os.environ.get("MIKE_TRADE_RESEARCH_DIR", "research"))
    acquire.set_defaults(func=acquire_data)
    listing = actions.add_parser("list", help="list immutable datasets")
    listing.add_argument("--root", default=os.environ.get("MIKE_TRADE_RESEARCH_DIR", "research"))
    listing.set_defaults(func=list_data)

    for action in ("plan", "preflight", "acquire-universe", "status", "verify"):
        command = actions.add_parser(action, help=f"100-symbol universe {action}")
        command.add_argument("--universe", default="config/research_universe_100_v1.json")
        command.add_argument(
            "--root", default=os.environ.get("MIKE_TRADE_RESEARCH_DIR", "research/data")
        )
        if action == "acquire-universe":
            command.add_argument("--workers", type=int, default=1, choices=(1, 2))
            command.add_argument("--requests-per-minute", type=int, default=20)
        command.set_defaults(func=run_universe_data)

    backtest = subparsers.add_parser("research-backtest", help="run a VWAP lifecycle backtest")
    backtest.add_argument("--symbol", required=True)
    backtest.add_argument("--strategy", required=True, choices=("vwap_bounce", "vwap_breakdown"))
    backtest.add_argument("--start", required=True)
    backtest.add_argument("--end", required=True)
    backtest.add_argument(
        "--feed", default=None, choices=("iex", "sip"), help="historical bars feed ('iex' or 'sip')"
    )
    backtest.add_argument("--dataset-id", default=None, help="explicit snapshot dataset_id")
    backtest.add_argument(
        "--eod-close-at", default=None, help="explicit EOD close time HH:MM (e.g. 15:55)"
    )
    backtest.add_argument(
        "--disable-eod-close",
        action="store_true",
        default=False,
        help="explicitly disable EOD liquidation",
    )
    backtest.add_argument("--root", default=os.environ.get("MIKE_TRADE_RESEARCH_DIR", "research"))
    backtest.add_argument(
        "--results", default=os.environ.get("MIKE_TRADE_RESULTS_DIR", "research/results")
    )
    backtest.add_argument("--seed", type=int, default=271828)
    backtest.add_argument(
        "--workers", default="1", help="single run: 1 (accepted for interface consistency)"
    )
    backtest.set_defaults(func=run_backtest)

    six = subparsers.add_parser(
        "research-six", help="run the six initial VWAP baseline experiments"
    )
    six.add_argument("--start", required=True)
    six.add_argument("--end", required=True)
    six.add_argument(
        "--feed", default=None, choices=("iex", "sip"), help="historical bars feed ('iex' or 'sip')"
    )
    six.add_argument("--root", default=os.environ.get("MIKE_TRADE_RESEARCH_DIR", "research"))
    six.add_argument(
        "--results", default=os.environ.get("MIKE_TRADE_RESULTS_DIR", "research/results")
    )
    six.add_argument("--seed", type=int, default=271828)
    six.add_argument("--workers", default="auto", help="worker processes: integer or auto")
    six.set_defaults(func=run_six)

    batch = subparsers.add_parser(
        "research-batch", help="run serialized experiment specifications in parallel"
    )
    batch.add_argument(
        "--jobs", required=True, help="JSON file containing {experiments: [...]} specifications"
    )
    batch.add_argument("--workers", default="auto", help="worker processes: integer or auto")
    batch.add_argument(
        "--root", default=os.environ.get("MIKE_TRADE_RESEARCH_DIR", "/app/research/data")
    )
    batch.add_argument(
        "--results", default=os.environ.get("MIKE_TRADE_RESULTS_DIR", "/app/research/results")
    )
    batch.add_argument("--output", help="optional batch summary JSON path")
    batch.set_defaults(func=run_batch)

    results = subparsers.add_parser("research-results", help="list persisted research backtests")
    results.add_argument(
        "--results", default=os.environ.get("MIKE_TRADE_RESULTS_DIR", "research/results")
    )
    results.add_argument("--limit", type=int, default=50)
    results.set_defaults(func=list_results)


def _client() -> Any:
    key = os.environ.get("MIKE_TRADE_ALPACA_DATA_KEY_ID", "")
    secret = os.environ.get("MIKE_TRADE_ALPACA_DATA_SECRET_KEY", "")
    if not key or not secret:
        raise ValueError("Alpaca market-data credentials are not configured")
    from mike_trade.app import _AlpacaDataHttpClient

    return _AlpacaDataHttpClient(key, secret)


def run_universe_data(args: argparse.Namespace) -> int:
    from mike_trade.research.universe_acquisition import (
        BoundedAlpacaClient,
        Universe,
        UniverseAcquisition,
    )

    operation = UniverseAcquisition(Universe.load(args.universe), args.root)
    action = args.research_data_action
    if action == "plan":
        print(json.dumps(operation.plan(), sort_keys=True))
        return 0
    if action == "status":
        print(json.dumps(operation.status(), sort_keys=True))
        return 0
    if action == "verify":
        result = operation.verify()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ok"] else 1
    client = _client()
    try:
        if action == "preflight":
            result = operation.preflight(client)
            print(json.dumps(result, sort_keys=True))
            return 0
        if action == "acquire-universe":
            # Validate access and storage before creating acquisition state.
            result = operation.preflight(client)
            print(
                json.dumps(
                    {"preflight": "passed", "free_disk_bytes": result["free_disk_bytes"],
                     "historical_api": result["historical_api"]},
                    sort_keys=True,
                ),
                flush=True,
            )
            limited = BoundedAlpacaClient(client, requests_per_minute=args.requests_per_minute)
            return operation.acquire(
                limited, workers=args.workers, progress=lambda line: print(line, flush=True)
            )
        raise ValueError(f"unknown universe action: {action}")
    finally:
        client.close()


def acquire_data(args: argparse.Namespace) -> int:
    start, end = _moment(args.start), _moment(args.end)
    if start >= end:
        raise ValueError("--start must precede --end")
    client = _client()
    try:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        if getattr(args, "timeframe", "1m") == "1d":
            from mike_trade.market_data.candles import NY

            snapshots = HistoricalDatasetRepository(args.root).acquire_daily(
                client,
                symbols,
                start.astimezone(NY).date(),
                (end - timedelta(microseconds=1)).astimezone(NY).date(),
                feed=args.feed,
                adjustment=args.adjustment,
            )
        else:
            snapshots = HistoricalDatasetRepository(args.root).acquire(
                client,
                symbols,
                start,
                end,
                feed=args.feed,
                adjustment=args.adjustment,
            )
    finally:
        client.close()
    for snapshot in snapshots:
        print(
            json.dumps(
                {name: getattr(snapshot, name) for name in snapshot.__dataclass_fields__},
                sort_keys=True,
            )
        )
    return 0


def list_data(args: argparse.Namespace) -> int:
    repo = HistoricalDatasetRepository(args.root)
    repo.sync_catalog()
    for item in repo.snapshots():
        print(
            json.dumps(
                {name: getattr(item, name) for name in item.__dataclass_fields__}, sort_keys=True
            )
        )
    return 0


def _research_registry() -> Any:
    """Registry of every shipped strategy; BUY and SELL entries are independent."""
    from mike_trade.strategies.buy.momentum_breakout import MomentumBreakoutBuy
    from mike_trade.strategies.buy.opening_range_breakout import OpeningRangeBreakoutBuy
    from mike_trade.strategies.buy.vwap_bounce import VwapBounceBuy
    from mike_trade.strategies.buy.five_minute_orb_michelle import FiveMinuteOrbMichelleBuy
    from mike_trade.strategies.buy.vwap_bounce_break_michelle import VwapBounceBreakMichelleBuy
    from mike_trade.strategies.buy.vwap_bounce_break_michelle_corrected import (
        VwapBounceBreakMichelleCorrectedBuy,
    )
    from mike_trade.strategies.sell.five_minute_orb_michelle import FiveMinuteOrbMichelleSell
    from mike_trade.strategies.sell.vwap_bounce_break_michelle import VwapBounceBreakMichelleSell
    from mike_trade.strategies.sell.vwap_bounce_break_michelle_corrected import (
        VwapBounceBreakMichelleCorrectedSell,
    )
    from mike_trade.strategies.buy.aroon_trend import AroonTrendBuy
    from mike_trade.strategies.buy.afternoon_breakout import AfternoonBreakoutBuy
    from mike_trade.strategies.buy.cci_zero_line_crossover import CciZeroLineCrossoverBuy
    from mike_trade.strategies.buy.bollinger_band_breakout import BollingerBandBreakoutBuy
    from mike_trade.strategies.buy.cci_reversion import CciReversionBuy
    from mike_trade.strategies.buy.stochastic_oversold_reversal import StochasticOversoldReversalBuy
    from mike_trade.strategies.buy.money_flow_index import MoneyFlowIndexBuy
    from mike_trade.strategies.buy.chaikin_money_flow import ChaikinMoneyFlowBuy
    from mike_trade.strategies.buy.engulfing_candle_reversal import EngulfingCandleReversalBuy
    from mike_trade.strategies.buy.awesome_oscillator import AwesomeOscillatorBuy
    from mike_trade.strategies.buy.hull_moving_average import HullMovingAverageBuy
    from mike_trade.strategies.buy.high_of_day_momentum import HighOfDayMomentumBuy
    from mike_trade.strategies.buy.gap_and_go import GapAndGoBuy
    from mike_trade.strategies.buy.gap_fill import GapFillBuy
    from mike_trade.strategies.buy.gap_reversal import GapReversalBuy
    from mike_trade.strategies.buy.keltner_channel_breakout import KeltnerChannelBreakoutBuy
    from mike_trade.strategies.buy.linear_regression_trend import LinearRegressionTrendBuy
    from mike_trade.strategies.buy.moving_average_envelope import MovingAverageEnvelopeBuy
    from mike_trade.strategies.buy.moving_average_reclaim import MovingAverageReclaimBuy
    from mike_trade.strategies.buy.nr7_breakout import Nr7BreakoutBuy
    from mike_trade.strategies.buy.rate_of_change_momentum import RateOfChangeMomentumBuy
    from mike_trade.strategies.buy.triple_moving_average import TripleMovingAverageBuy
    from mike_trade.strategies.buy.dmi_crossover import DmiCrossoverBuy
    from mike_trade.strategies.buy.donchian_channel_breakout import DonchianChannelBreakoutBuy
    from mike_trade.strategies.buy.ema_crossover import EmaCrossoverBuy
    from mike_trade.strategies.buy.inside_bar_breakout import InsideBarBreakoutBuy
    from mike_trade.strategies.buy.macd_signal_crossover import MacdSignalCrossoverBuy
    from mike_trade.strategies.buy.macd_zero_line_crossover import MacdZeroLineCrossoverBuy
    from mike_trade.strategies.buy.midday_range_breakout import MiddayRangeBreakoutBuy
    from mike_trade.strategies.buy.opening_drive import OpeningDriveBuy
    from mike_trade.strategies.buy.opening_range_retest import OpeningRangeRetestBuy
    from mike_trade.strategies.buy.parabolic_sar import ParabolicSarBuy
    from mike_trade.strategies.buy.power_hour_momentum import PowerHourMomentumBuy
    from mike_trade.strategies.short.short_ema_bearish_crossover import ShortEmaBearishCrossover
    from mike_trade.strategies.short.short_supertrend_bearish_flip import ShortSupertrendBearishFlip
    from mike_trade.strategies.short.short_donchian_breakdown import ShortDonchianBreakdown
    from mike_trade.strategies.short.short_sma_bearish_crossover import ShortSmaBearishCrossover
    from mike_trade.strategies.short.short_macd_bearish_crossover import ShortMacdBearishCrossover
    from mike_trade.strategies.short.short_macd_zero_line_breakdown import (
        ShortMacdZeroLineBreakdown,
    )
    from mike_trade.strategies.short.short_rsi_centerline_breakdown import (
        ShortRsiCenterlineBreakdown,
    )
    from mike_trade.strategies.short.short_dmi_bearish_crossover import ShortDmiBearishCrossover
    from mike_trade.strategies.short.short_aroon_bearish_reversal import ShortAroonBearishReversal
    from mike_trade.strategies.short.short_parabolic_sar_bearish_reversal import (
        ShortParabolicSarBearishReversal,
    )
    from mike_trade.strategies.short.short_ichimoku_bearish_breakdown import (
        ShortIchimokuBearishBreakdown,
    )
    from mike_trade.strategies.short.short_opening_range_breakdown import (
        ShortOpeningRangeBreakdown,
    )
    from mike_trade.strategies.short.short_vwap_breakdown import ShortVwapBreakdown
    from mike_trade.strategies.short.short_bollinger_lower_band_breakdown import (
        ShortBollingerLowerBandBreakdown,
    )
    from mike_trade.strategies.short.short_keltner_channel_breakdown import (
        ShortKeltnerChannelBreakdown,
    )
    from mike_trade.strategies.short.short_obv_breakdown import ShortObvBreakdown
    from mike_trade.strategies.short.short_cci_zero_line_breakdown import (
        ShortCciZeroLineBreakdown,
    )
    from mike_trade.strategies.short.short_adx_bearish_trend_confirmation import (
        ShortAdxBearishTrendConfirmation,
    )
    from mike_trade.strategies.cover.cover_ema_bullish_crossover import CoverEmaBullishCrossover
    from mike_trade.strategies.cover.cover_supertrend_bullish_flip import CoverSupertrendBullishFlip
    from mike_trade.strategies.cover.cover_donchian_breakout import CoverDonchianBreakout
    from mike_trade.strategies.cover.cover_sma_bullish_crossover import CoverSmaBullishCrossover
    from mike_trade.strategies.cover.cover_macd_bullish_crossover import CoverMacdBullishCrossover
    from mike_trade.strategies.cover.cover_macd_zero_line_crossover import CoverMacdZeroLineCrossover
    from mike_trade.strategies.cover.cover_rsi_oversold_reversal import CoverRsiOversoldReversal
    from mike_trade.strategies.cover.cover_rsi_centerline_crossover import CoverRsiCenterlineCrossover
    from mike_trade.strategies.cover.cover_dmi_bullish_crossover import CoverDmiBullishCrossover
    from mike_trade.strategies.cover.cover_aroon_bullish_reversal import CoverAroonBullishReversal
    from mike_trade.strategies.cover.cover_parabolic_sar_bullish_reversal import CoverParabolicSarBullishReversal
    from mike_trade.strategies.cover.cover_vwap_reclaim import CoverVwapReclaim
    from mike_trade.strategies.cover.cover_bollinger_lower_band_reversion import CoverBollingerLowerBandReversion
    from mike_trade.strategies.cover.cover_keltner_channel_reversion import CoverKeltnerChannelReversion
    from mike_trade.strategies.cover.cover_cci_oversold_reversal import CoverCciOversoldReversal
    from mike_trade.strategies.cover.cover_stochastic_bullish_crossover import CoverStochasticBullishCrossover
    from mike_trade.strategies.cover.cover_williams_r_bullish_reversal import CoverWilliamsRBullishReversal
    from mike_trade.strategies.cover.cover_chaikin_money_flow_recovery import CoverChaikinMoneyFlowRecovery
    from mike_trade.strategies.cover.cover_bullish_engulfing import CoverBullishEngulfing
    from mike_trade.strategies.cover.cover_hammer_reversal import CoverHammerReversal
    from mike_trade.strategies.cover.cover_double_bottom_reversal import CoverDoubleBottomReversal
    from mike_trade.strategies.cover.cover_inverse_head_and_shoulders import CoverInverseHeadAndShoulders
    from mike_trade.strategies.cover.cover_trendline_breakout import CoverTrendlineBreakout
    from mike_trade.strategies.cover.cover_previous_swing_high_breakout import CoverPreviousSwingHighBreakout
    from mike_trade.strategies.cover.cover_higher_high_higher_low import CoverHigherHighHigherLow
    from mike_trade.strategies.cover.cover_bull_flag_breakout import CoverBullFlagBreakout
    from mike_trade.strategies.cover.cover_obv_breakout import CoverObvBreakout
    from mike_trade.strategies.cover.cover_bullish_momentum_divergence import CoverBullishMomentumDivergence
    from mike_trade.strategies.buy.previous_close_reclaim import PreviousCloseReclaimBuy
    from mike_trade.strategies.buy.previous_day_high_breakout import PreviousDayHighBreakoutBuy
    from mike_trade.strategies.buy.previous_day_low_reversal import PreviousDayLowReversalBuy
    from mike_trade.strategies.buy.rsi_centerline_crossover import RsiCenterlineCrossoverBuy
    from mike_trade.strategies.buy.rsi_oversold_reversal import RsiOversoldReversalBuy
    from mike_trade.strategies.buy.sma_crossover import SmaCrossoverBuy
    from mike_trade.strategies.buy.stochastic_crossover import StochasticCrossoverBuy
    from mike_trade.strategies.buy.williams_r_reversion import WilliamsRReversionBuy
    from mike_trade.strategies.buy.adaptive_moving_average import AdaptiveMovingAverageBuy
    from mike_trade.strategies.buy.adx_trend_following import AdxTrendFollowingBuy
    from mike_trade.strategies.buy.atr_expansion_breakout import AtrExpansionBreakoutBuy
    from mike_trade.strategies.buy.bollinger_squeeze import BollingerSqueezeBuy
    from mike_trade.strategies.buy.connors_rsi_mean_reversion import ConnorsRsiMeanReversionBuy
    from mike_trade.strategies.buy.ease_of_movement import EaseOfMovementBuy
    from mike_trade.strategies.buy.elder_impulse_system import ElderImpulseSystemBuy
    from mike_trade.strategies.buy.ema_pullback import EmaPullbackBuy
    from mike_trade.strategies.buy.hammer_reversal import HammerReversalBuy
    from mike_trade.strategies.buy.higher_high_higher_low import HigherHighHigherLowBuy
    from mike_trade.strategies.buy.ichimoku_cloud_breakout import IchimokuCloudBreakoutBuy
    from mike_trade.strategies.buy.keltner_channel_reversion import KeltnerChannelReversionBuy
    from mike_trade.strategies.buy.moving_average_ribbon import MovingAverageRibbonBuy
    from mike_trade.strategies.buy.resistance_breakout import ResistanceBreakoutBuy
    from mike_trade.strategies.buy.supertrend import SupertrendBuy
    from mike_trade.strategies.buy.volatility_squeeze_breakout import VolatilitySqueezeBreakoutBuy
    from mike_trade.strategies.buy.obv_breakout import ObvBreakoutBuy
    from mike_trade.strategies.buy.accumulation_distribution_breakout import AccumulationDistributionBreakoutBuy
    from mike_trade.strategies.buy.volume_price_trend_breakout import VolumePriceTrendBreakoutBuy
    from mike_trade.strategies.buy.tsi_momentum import TsiMomentumBuy
    from mike_trade.strategies.buy.stochastic_rsi import StochasticRsiBuy
    from mike_trade.strategies.buy.ultimate_oscillator import UltimateOscillatorBuy
    from mike_trade.strategies.buy.holy_grail_pullback import HolyGrailPullbackBuy
    from mike_trade.strategies.buy.rsi2_trend_pullback import Rsi2TrendPullbackBuy
    from mike_trade.strategies.buy.z_score_mean_reversion import ZScoreMeanReversionBuy
    from mike_trade.strategies.buy.double_bottom_reversal import DoubleBottomReversalBuy
    from mike_trade.strategies.buy.inverse_head_and_shoulders import InverseHeadAndShouldersBuy
    from mike_trade.strategies.buy.ascending_triangle_breakout import AscendingTriangleBreakoutBuy
    from mike_trade.strategies.buy.trendline_breakout import TrendlineBreakoutBuy
    from mike_trade.strategies.buy.triangle_breakout import TriangleBreakoutBuy
    from mike_trade.strategies.buy.bull_flag_breakout import BullFlagBreakoutBuy
    from mike_trade.strategies.buy.bollinger_band_mean_reversion import BollingerBandMeanReversionBuy
    from mike_trade.strategies.buy.vwap_pullback import VwapPullbackBuy
    from mike_trade.strategies.buy.support_bounce import SupportBounceBuy
    from mike_trade.strategies.buy.vwap_band_reversion import VWAPBandReversionBuy
    from mike_trade.strategies.buy.vwap_rejection import VwapRejectionBuy
    from mike_trade.strategies.buy.anchored_vwap_breakout import AnchoredVwapBreakoutBuy
    from mike_trade.strategies.buy.first_pullback_buy import FirstPullbackBuy
    from mike_trade.strategies.buy.gap_exhaustion import GapExhaustionBuy
    from mike_trade.strategies.buy.bull_pennant_breakout import BullPennantBreakoutBuy
    from mike_trade.strategies.buy.intraday_consolidation_breakout import (
        IntradayConsolidationBreakoutBuy,
    )
    from mike_trade.strategies.buy.relative_volume_momentum import RelativeVolumeMomentumBuy
    from mike_trade.strategies.buy.moving_average_compression import MovingAverageCompressionBuy
    from mike_trade.strategies.buy.momentum_continuation import MomentumContinuationBuy
    from mike_trade.strategies.buy.rsi_momentum import RsiMomentumBuy
    from mike_trade.strategies.buy.intraday_exhaustion_reversal import (
        IntradayExhaustionReversalBuy,
    )
    from mike_trade.strategies.buy.volume_dry_up_pullback import VolumeDryUpPullbackBuy
    from mike_trade.strategies.sell.aroon_bearish_reversal import AroonBearishReversalSell
    from mike_trade.strategies.sell.cci_zero_line_breakdown import CciZeroLineBreakdownSell
    from mike_trade.strategies.sell.cci_bearish_reversal import CciBearishReversalSell
    from mike_trade.strategies.sell.money_flow_index_reversal import MoneyFlowIndexReversalSell
    from mike_trade.strategies.sell.chaikin_money_flow_reversal import ChaikinMoneyFlowReversalSell
    from mike_trade.strategies.sell.bearish_engulfing_exit import BearishEngulfingExitSell
    from mike_trade.strategies.sell.awesome_oscillator_reversal import AwesomeOscillatorReversalSell
    from mike_trade.strategies.sell.keltner_channel_breakdown import KeltnerChannelBreakdownSell
    from mike_trade.strategies.sell.previous_swing_low_break import PreviousSwingLowBreakSell
    from mike_trade.strategies.sell.dmi_bearish_crossover import DmiBearishCrossoverSell
    from mike_trade.strategies.sell.donchian_lower_channel_exit import DonchianLowerChannelExitSell
    from mike_trade.strategies.sell.ema_bearish_crossover import EmaBearishCrossoverSell
    from mike_trade.strategies.sell.macd_bearish_crossover import MacdBearishCrossoverSell
    from mike_trade.strategies.sell.macd_zero_line_breakdown import MacdZeroLineBreakdownSell
    from mike_trade.strategies.sell.parabolic_sar_reversal import ParabolicSarReversalSell
    from mike_trade.strategies.sell.price_below_ema import PriceBelowEmaSell
    from mike_trade.strategies.sell.price_below_sma import PriceBelowSmaSell
    from mike_trade.strategies.sell.rsi_centerline_breakdown import RsiCenterlineBreakdownSell
    from mike_trade.strategies.sell.sma_bearish_crossover import SmaBearishCrossoverSell
    from mike_trade.strategies.sell.stochastic_bearish_crossover import StochasticBearishCrossoverSell
    from mike_trade.strategies.sell.williams_r_reversal import WilliamsRReversalSell
    from mike_trade.strategies.registry import StrategyRegistry
    from mike_trade.strategies.buy.lightgbm_probability_buy import LightgbmProbabilityBuy
    from mike_trade.strategies.buy.xgboost_probability_buy import XgboostProbabilityBuy
    from mike_trade.strategies.sell.lightgbm_probability_sell import LightgbmProbabilitySell
    from mike_trade.strategies.sell.xgboost_probability_sell import XgboostProbabilitySell
    from mike_trade.strategies.sell.momentum_reversal import MomentumReversalSell
    from mike_trade.strategies.sell.rsi_reversal import RsiReversalSell
    from mike_trade.strategies.sell.vwap_breakdown import VwapBreakdownSell
    from mike_trade.strategies.sell.adx_trend_weakening import AdxTrendWeakeningSell
    from mike_trade.strategies.sell.bollinger_upper_band_reversal import (
        BollingerUpperBandReversalSell,
    )
    from mike_trade.strategies.sell.failed_breakout_exit import FailedBreakoutExitSell
    from mike_trade.strategies.sell.lower_low_confirmation import LowerLowConfirmationSell
    from mike_trade.strategies.sell.opening_range_breakdown import OpeningRangeBreakdownSell
    from mike_trade.strategies.sell.stochastic_rsi_reversal import StochasticRsiReversalSell
    from mike_trade.strategies.sell.trendline_breakdown import TrendlineBreakdownSell
    from mike_trade.strategies.sell.ultimate_oscillator_reversal import (
        UltimateOscillatorReversalSell,
    )
    from mike_trade.strategies.sell.accumulation_distribution_breakdown import (
        AccumulationDistributionBreakdownSell,
    )
    from mike_trade.strategies.sell.bearish_momentum_divergence import (
        BearishMomentumDivergenceSell,
    )
    from mike_trade.strategies.sell.double_top_reversal import DoubleTopReversalSell
    from mike_trade.strategies.sell.head_and_shoulders_breakdown import (
        HeadAndShouldersBreakdownSell,
    )
    from mike_trade.strategies.sell.ichimoku_bearish_exit import IchimokuBearishExitSell
    from mike_trade.strategies.sell.obv_breakdown import ObvBreakdownSell
    from mike_trade.strategies.sell.shooting_star_reversal import ShootingStarReversalSell
    from mike_trade.strategies.sell.supertrend_bearish_flip import SupertrendBearishFlipSell
    from mike_trade.strategies.sell.volatility_expansion_reversal import (
        VolatilityExpansionReversalSell,
    )
    from mike_trade.strategies.sell.volume_climax_exit import VolumeClimaxExitSell
    from mike_trade.strategies.short.short_bearish_engulfing import ShortBearishEngulfing
    from mike_trade.strategies.short.short_shooting_star_reversal import ShortShootingStarReversal
    from mike_trade.strategies.short.short_double_top_breakdown import ShortDoubleTopBreakdown
    from mike_trade.strategies.short.short_head_and_shoulders_breakdown import (
        ShortHeadAndShouldersBreakdown,
    )
    from mike_trade.strategies.short.short_trendline_breakdown import ShortTrendlineBreakdown
    from mike_trade.strategies.short.short_previous_swing_low_break import ShortPreviousSwingLowBreak
    from mike_trade.strategies.short.short_lower_low_confirmation import ShortLowerLowConfirmation
    from mike_trade.strategies.short.short_failed_breakout_reversal import ShortFailedBreakoutReversal
    from mike_trade.strategies.short.short_bear_flag_breakdown import ShortBearFlagBreakdown
    from mike_trade.strategies.short.short_bearish_momentum_divergence import (
        ShortBearishMomentumDivergence,
    )
    from mike_trade.strategies.sell.high_volume_reversal import HighVolumeReversalSell

    from mike_trade.strategies.buy.cup_and_handle import CupAndHandleBuy
    from mike_trade.strategies.buy.volatility_contraction_pattern import VolatilityContractionPatternBuy

    registry = StrategyRegistry()
    for cls in (
        VwapBounceBuy,
        MomentumBreakoutBuy,
        OpeningRangeBreakoutBuy,
        FiveMinuteOrbMichelleBuy,
        VwapBounceBreakMichelleBuy,
        FiveMinuteOrbMichelleSell,
        VwapBounceBreakMichelleSell,
        VwapBounceBreakMichelleCorrectedBuy,
        VwapBounceBreakMichelleCorrectedSell,
        VwapBreakdownSell,
        MomentumReversalSell,
        RsiReversalSell,
        AroonTrendBuy,
        CciZeroLineCrossoverBuy,
        DmiCrossoverBuy,
        DonchianChannelBreakoutBuy,
        EmaCrossoverBuy,
        InsideBarBreakoutBuy,
        MacdSignalCrossoverBuy,
        MacdZeroLineCrossoverBuy,
        ParabolicSarBuy,
        RsiCenterlineCrossoverBuy,
        RsiOversoldReversalBuy,
        SmaCrossoverBuy,
        StochasticCrossoverBuy,
        WilliamsRReversionBuy,
        BollingerBandBreakoutBuy,
        CciReversionBuy,
        StochasticOversoldReversalBuy,
        MoneyFlowIndexBuy,
        ChaikinMoneyFlowBuy,
        EngulfingCandleReversalBuy,
        AwesomeOscillatorBuy,
        HullMovingAverageBuy,
        KeltnerChannelBreakoutBuy,
        LinearRegressionTrendBuy,
        MovingAverageEnvelopeBuy,
        MovingAverageReclaimBuy,
        Nr7BreakoutBuy,
        RateOfChangeMomentumBuy,
        TripleMovingAverageBuy,
        AdaptiveMovingAverageBuy,
        AdxTrendFollowingBuy,
        AtrExpansionBreakoutBuy,
        BollingerSqueezeBuy,
        ConnorsRsiMeanReversionBuy,
        EaseOfMovementBuy,
        ElderImpulseSystemBuy,
        EmaPullbackBuy,
        HammerReversalBuy,
        HigherHighHigherLowBuy,
        IchimokuCloudBreakoutBuy,
        KeltnerChannelReversionBuy,
        MovingAverageRibbonBuy,
        ResistanceBreakoutBuy,
        SupertrendBuy,
        VolatilitySqueezeBreakoutBuy,
        OpeningDriveBuy,
        OpeningRangeRetestBuy,
        HighOfDayMomentumBuy,
        MiddayRangeBreakoutBuy,
        AfternoonBreakoutBuy,
        PowerHourMomentumBuy,
        ShortEmaBearishCrossover,
        ShortSupertrendBearishFlip,
        ShortDonchianBreakdown,
        ShortSmaBearishCrossover,
        ShortMacdBearishCrossover,
        ShortMacdZeroLineBreakdown,
        ShortRsiCenterlineBreakdown,
        ShortDmiBearishCrossover,
        ShortAroonBearishReversal,
        ShortParabolicSarBearishReversal,
        ShortIchimokuBearishBreakdown,
        ShortOpeningRangeBreakdown,
        ShortVwapBreakdown,
        ShortBollingerLowerBandBreakdown,
        ShortKeltnerChannelBreakdown,
        ShortObvBreakdown,
        ShortCciZeroLineBreakdown,
        ShortAdxBearishTrendConfirmation,
        ShortBearishEngulfing,
        ShortShootingStarReversal,
        ShortDoubleTopBreakdown,
        ShortHeadAndShouldersBreakdown,
        ShortTrendlineBreakdown,
        ShortPreviousSwingLowBreak,
        ShortLowerLowConfirmation,
        ShortFailedBreakoutReversal,
        ShortBearFlagBreakdown,
        ShortBearishMomentumDivergence,
        CoverEmaBullishCrossover,
        CoverSupertrendBullishFlip,
        CoverDonchianBreakout,
        CoverSmaBullishCrossover,
        CoverMacdBullishCrossover,
        CoverMacdZeroLineCrossover,
        CoverRsiOversoldReversal,
        CoverRsiCenterlineCrossover,
        CoverDmiBullishCrossover,
        CoverAroonBullishReversal,
        CoverParabolicSarBullishReversal,
        CoverVwapReclaim,
        CoverBollingerLowerBandReversion,
        CoverKeltnerChannelReversion,
        CoverCciOversoldReversal,
        CoverStochasticBullishCrossover,
        CoverWilliamsRBullishReversal,
        CoverChaikinMoneyFlowRecovery,
        CoverBullishEngulfing,
        CoverHammerReversal,
        CoverDoubleBottomReversal,
        CoverInverseHeadAndShoulders,
        CoverTrendlineBreakout,
        CoverPreviousSwingHighBreakout,
        CoverHigherHighHigherLow,
        CoverBullFlagBreakout,
        CoverObvBreakout,
        CoverBullishMomentumDivergence,
        ObvBreakoutBuy,
        AccumulationDistributionBreakoutBuy,
        VolumePriceTrendBreakoutBuy,
        TsiMomentumBuy,
        StochasticRsiBuy,
        UltimateOscillatorBuy,
        HolyGrailPullbackBuy,
        Rsi2TrendPullbackBuy,
        ZScoreMeanReversionBuy,
        DoubleBottomReversalBuy,
        InverseHeadAndShouldersBuy,
        AscendingTriangleBreakoutBuy,
        TrendlineBreakoutBuy,
        TriangleBreakoutBuy,
        BullFlagBreakoutBuy,
        GapAndGoBuy,
        GapFillBuy,
        GapReversalBuy,
        PreviousCloseReclaimBuy,
        PreviousDayHighBreakoutBuy,
        PreviousDayLowReversalBuy,
        BollingerBandMeanReversionBuy,
        VwapPullbackBuy,
        SupportBounceBuy,
        VWAPBandReversionBuy,
        VwapRejectionBuy,
        AnchoredVwapBreakoutBuy,
        FirstPullbackBuy,
        IntradayConsolidationBreakoutBuy,
        RelativeVolumeMomentumBuy,
        MovingAverageCompressionBuy,
        GapExhaustionBuy,
        BullPennantBreakoutBuy,
        MomentumContinuationBuy,
        RsiMomentumBuy,
        IntradayExhaustionReversalBuy,
        VolumeDryUpPullbackBuy,
        AroonBearishReversalSell,
        CciZeroLineBreakdownSell,
        DmiBearishCrossoverSell,
        DonchianLowerChannelExitSell,
        EmaBearishCrossoverSell,
        MacdBearishCrossoverSell,
        MacdZeroLineBreakdownSell,
        ParabolicSarReversalSell,
        PriceBelowEmaSell,
        PriceBelowSmaSell,
        RsiCenterlineBreakdownSell,
        SmaBearishCrossoverSell,
        StochasticBearishCrossoverSell,
        WilliamsRReversalSell,
        CciBearishReversalSell,
        MoneyFlowIndexReversalSell,
        ChaikinMoneyFlowReversalSell,
        BearishEngulfingExitSell,
        AwesomeOscillatorReversalSell,
        KeltnerChannelBreakdownSell,
        PreviousSwingLowBreakSell,
        AdxTrendWeakeningSell,
        BollingerUpperBandReversalSell,
        FailedBreakoutExitSell,
        LowerLowConfirmationSell,
        OpeningRangeBreakdownSell,
        StochasticRsiReversalSell,
        TrendlineBreakdownSell,
        UltimateOscillatorReversalSell,
        AccumulationDistributionBreakdownSell,
        BearishMomentumDivergenceSell,
        DoubleTopReversalSell,
        HeadAndShouldersBreakdownSell,
        IchimokuBearishExitSell,
        ObvBreakdownSell,
        ShootingStarReversalSell,
        SupertrendBearishFlipSell,
        HighVolumeReversalSell,
        VolatilityExpansionReversalSell,
        VolumeClimaxExitSell,
        VolatilityContractionPatternBuy,
        CupAndHandleBuy,
        XgboostProbabilityBuy,
        LightgbmProbabilityBuy,
        XgboostProbabilitySell,
        LightgbmProbabilitySell,
    ):
        registry.register(cls)
    return registry


def _stop_distance_fraction(spec: Mapping[str, Any]) -> Any:
    """Return ``reference -> initial stop distance as a fraction of price``."""
    from mike_trade.risk.stop_config import normalize_stop_spec

    normalized = normalize_stop_spec(spec)
    for key in ("stop_percent", "trail_percent", "initial_stop_percent"):
        if key in normalized:
            fraction = normalized[key] / Decimal(100)
            return lambda reference: fraction
    amount = normalized.get("stop_amount", normalized.get("trail_amount"))
    return lambda reference: amount / reference


def research_default_execution_assumptions() -> dict[str, Any]:
    """Implicit research execution costs: none.

    Zero assumed slippage and fees is a research scenario that adds no cost the strategy
    specifications do not call for; it is not a guarantee of live execution prices. Any
    slippage or fee must be requested explicitly through an experiment's ``execution`` mapping.
    """
    return {
        "slippage_bps": Decimal("0"),
        "slippage_ticks": Decimal("0"),
        "fee_per_order": Decimal("0"),
        "fee_per_share": Decimal("0"),
        "fee_bps": Decimal("0"),
    }


def resolve_execution_assumptions(execution: Mapping[str, Any] | None) -> Any:
    """The simulator assumptions a job runs with: research defaults overlaid by ``execution``.

    Shared by the runner and campaign preflight so the validated values are the executed ones.
    """
    from mike_trade.backtest.execution import AmbiguityPolicy, BarFillPolicy, ExecutionAssumptions
    from mike_trade.domain.trading import OrderType

    assumption_values: dict[str, Any] = research_default_execution_assumptions()
    allowed_assumptions = set(ExecutionAssumptions.__dataclass_fields__)
    unexpected = set(execution or {}) - allowed_assumptions
    if unexpected:
        raise ValueError(f"unknown execution assumptions: {sorted(unexpected)}")
    assumption_values.update(execution or {})
    decimal_fields = {
        "slippage_bps",
        "slippage_ticks",
        "tick_size",
        "price_quantum",
        "participation",
        "quote_size_multiplier",
        "fee_per_order",
        "fee_per_share",
        "fee_bps",
        "fee_quantum",
        "quantity_quantum",
    }
    for field_name in decimal_fields:
        if field_name in assumption_values and not isinstance(
            assumption_values[field_name], Decimal
        ):
            assumption_values[field_name] = Decimal(str(assumption_values[field_name]))
    if isinstance(assumption_values.get("bar_fill_policy"), str):
        assumption_values["bar_fill_policy"] = BarFillPolicy(assumption_values["bar_fill_policy"])
    if isinstance(assumption_values.get("ambiguity_policy"), str):
        assumption_values["ambiguity_policy"] = AmbiguityPolicy(
            assumption_values["ambiguity_policy"]
        )
    for field_name in ("submission_latency", "stop_replace_latency", "max_quote_age"):
        value = assumption_values.get(field_name)
        if isinstance(value, (int, float)):
            assumption_values[field_name] = timedelta(seconds=value)
        elif isinstance(value, Mapping) and "seconds" in value:
            assumption_values[field_name] = timedelta(seconds=float(value["seconds"]))
    if "fractional_order_types" in assumption_values:
        assumption_values["fractional_order_types"] = frozenset(
            value if isinstance(value, OrderType) else OrderType(value)
            for value in assumption_values["fractional_order_types"]
        )
    return ExecutionAssumptions(**assumption_values)


def _reference_events(
    repo: Any,
    symbols: Sequence[str],
    snapshot: Any,
    primary: str,
    start: datetime,
    end: datetime,
    root: str,
    pinned_ids: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], list[Any]]:
    """Regular-session replay bars for reference symbols, from the *same feed* as ``snapshot``.

    A missing (or other-feed) snapshot is an error, never a substitution.  ``pinned_ids`` fixes
    the exact snapshot per symbol; otherwise the newest covering one is used and reported.
    """
    from mike_trade.backtest.feed import ReplayEvent
    from mike_trade.domain.market import Candle, FeedProvenance, Timeframe
    from mike_trade.scheduling.calendar import NEW_YORK, session_for

    is_consolidated = snapshot.feed == "sip"
    provenance = FeedProvenance("replay", snapshot.feed.upper(), "alpaca", is_consolidated)
    dataset_ids: dict[str, str] = {}
    events: list[Any] = []
    for reference_symbol in symbols:
        if reference_symbol == primary:
            continue
        pinned = (pinned_ids or {}).get(reference_symbol)
        candidates = [
            item
            for item in repo.snapshots()
            if item.symbol == reference_symbol
            and (pinned is None or item.dataset_id == pinned)
            and item.adjustment == "raw"
            and item.feed == snapshot.feed
            and datetime.fromisoformat(item.start) <= start
            and datetime.fromisoformat(item.end) >= end
        ]
        if not candidates:
            raise ValueError(
                f"no immutable {snapshot.feed.upper()} snapshot covers reference symbol "
                f"{reference_symbol} {start.isoformat()}..{end.isoformat()}"
            )
        chosen = max(candidates, key=lambda item: (item.acquired_at, item.dataset_id))
        dataset_ids[reference_symbol] = chosen.dataset_id
        for bar in _cached_snapshot_bars(
            str(Path(root).resolve()), reference_symbol, start.isoformat(), end.isoformat(),
            chosen.dataset_id, feed=chosen.feed,
        ):
            day = session_for(bar.timestamp.astimezone(NEW_YORK).date())
            if not day.open_utc <= bar.timestamp < day.close_utc:
                continue
            events.append(
                ReplayEvent.bar(
                    Candle(
                        reference_symbol, Timeframe.M1, bar.timestamp,
                        bar.timestamp + timedelta(minutes=1), bar.open, bar.high, bar.low,
                        bar.close, Decimal(bar.volume), provenance, bar.vwap,
                        int(bar.trade_count) if bar.trade_count is not None else None,
                    )
                )
            )
    return dataset_ids, events


def reference_dataset_ids_loaded(events: Sequence[Any]) -> set[str]:
    return {event.symbol for event in events}


def _model_replay_inputs(
    repo: Any,
    registry: Any,
    clock: Any,
    snapshot: Any,
    symbol: str,
    start: datetime,
    end: datetime,
    root: str,
    model_root: str | None,
    buy_strategies: tuple[str, ...] | None,
    sell_strategies: tuple[str, ...] | None,
    parameters: Mapping[str, Mapping[str, Any]] | None,
    reference_dataset_ids: Mapping[str, str] | None = None,
) -> tuple[Any, dict[str, str], list[Any]]:
    """Pinned-model service plus same-feed reference-symbol replay events, when needed.

    Returns ``(None, {}, [])`` unless a selected strategy is model-driven.  Reference series
    (for example SPY) come from the *same feed* as the primary snapshot; an absent snapshot is
    an error, never a substitution.
    """
    from mike_trade.modeling.serving import build_inference_service

    references: dict[str, Any] = {}
    for strategy_id in (*(buy_strategies or ()), *(sell_strategies or ())):
        try:
            cls = registry.get(strategy_id)
        except Exception:
            continue  # unknown ids are reported by the normal strategy validation
        reference_of = getattr(cls, "model_reference", None)
        if callable(reference_of):
            params = registry.validate_parameters(
                strategy_id, (parameters or {}).get(strategy_id, {})
            )
            reference = reference_of(params)
            references.setdefault(reference.key, reference)
    if not references:
        return None, {}, []
    if not model_root:
        raise ValueError(
            "model-driven strategies require model_root (the immutable model repository); "
            "no model is ever located implicitly or substituted"
        )
    service = build_inference_service(model_root, tuple(references.values()), clock.now)
    for reference in service.references:
        manifest = service.manifest(reference)
        if manifest is not None and manifest.feed != snapshot.feed:
            raise ValueError(
                f"model {reference.key} was trained on the {manifest.feed!r} feed but the "
                f"dataset is {snapshot.feed!r}; feeds are never mixed or substituted"
            )
    dataset_ids, events = _reference_events(
        repo, service.required_reference_symbols(), snapshot, symbol, start, end, root,
        reference_dataset_ids,
    )
    return service, dataset_ids, events


def _run_one(
    symbol: str,
    start: datetime,
    end: datetime,
    root: str,
    results: str,
    seed: int,
    strategy_name: str,
    *,
    dataset_id: str | None = None,
    feed: str | None = None,
    parameters: Mapping[str, Mapping[str, Any]] | None = None,
    starting_capital: Decimal = Decimal("10000"),
    execution: Mapping[str, Any] | None = None,
    buy_strategies: tuple[str, ...] | None = None,
    sell_strategies: tuple[str, ...] | None = None,
    short_strategies: tuple[str, ...] | None = None,
    cover_strategies: tuple[str, ...] | None = None,
    short_selling_enabled: bool = False,
    short_borrow_bps_per_day: Decimal = Decimal("0"),
    protective_stop: Mapping[str, Any] | None = None,
    end_of_day_close: Mapping[str, Any] | None = None,
    max_entry_cycles_per_session: int = 1,
    model_root: str | None = None,
    reference_dataset_ids: Mapping[str, str] | None = None,
    market_status_enabled: bool = False,
) -> dict[str, Any]:
    from mike_trade.backtest.clock import ReplayClock
    from mike_trade.backtest.execution import (
        AmbiguityPolicy,
        BarFillPolicy,
        ExecutionAssumptions,
        HistoricalExecutionSimulator,
    )
    from mike_trade.backtest.feed import (
        DataProvenance,
        HistoricalReplayFeed,
        QuoteFidelity,
        ReplayEvent,
    )
    from mike_trade.backtest.manifest import PeriodRole
    from mike_trade.backtest.runner import BacktestRequest, BacktestRunner, compute_metrics
    from mike_trade.domain.market import Candle, FeedProvenance, Timeframe
    from mike_trade.domain.strategy_types import StrategyRole
    from mike_trade.domain.trading import (
        OrderIntent,
        OrderPurpose,
        OrderSide,
        OrderType,
        TimeInForce,
    )
    from mike_trade.scheduling.calendar import NEW_YORK, session_for
    from mike_trade.strategies.presets.pairs import resolve_pair_preset

    repo = HistoricalDatasetRepository(root)
    target_feed = feed.strip().lower() if feed is not None else None
    if target_feed is not None and target_feed not in ("iex", "sip"):
        raise ValueError(f"unsupported feed: {feed}")

    candidate_snapshots = [
        s
        for s in repo.snapshots()
        if s.symbol == symbol
        and s.adjustment == "raw"
        and datetime.fromisoformat(s.start) <= start
        and datetime.fromisoformat(s.end) >= end
    ]
    if dataset_id is not None:
        snapshots = [s for s in candidate_snapshots if s.dataset_id == dataset_id]
        if snapshots and target_feed is not None and snapshots[0].feed != target_feed:
            raise ValueError(
                f"dataset_id {dataset_id} has feed '{snapshots[0].feed}', which does not match requested feed '{target_feed}'"
            )
    else:
        effective_feed = target_feed if target_feed is not None else "iex"
        snapshots = [s for s in candidate_snapshots if s.feed == effective_feed]

    if not snapshots:
        feed_desc = f" feed={target_feed or ('(any)' if dataset_id else 'iex')}"
        raise ValueError(
            f"no matching immutable snapshot covers {symbol} {start.isoformat()}..{end.isoformat()}{feed_desc}"
        )
    snapshot = max(snapshots, key=lambda s: (s.acquired_at, s.dataset_id))
    bars = _cached_snapshot_bars(
        str(Path(root).resolve()),
        symbol,
        start.isoformat(),
        end.isoformat(),
        snapshot.dataset_id,
        feed=snapshot.feed,
    )
    bars = tuple(
        bar
        for bar in bars
        if session_for(bar.timestamp.astimezone(NEW_YORK).date()).open_utc
        <= bar.timestamp
        < session_for(bar.timestamp.astimezone(NEW_YORK).date()).close_utc
    )
    if not bars:
        raise ValueError(
            f"no stored {snapshot.feed.upper()} bars cover {symbol} {start.isoformat()}..{end.isoformat()}"
        )
    is_consolidated = snapshot.feed == "sip"
    provenance = FeedProvenance("replay", snapshot.feed.upper(), "alpaca", is_consolidated)
    events = [
        ReplayEvent.bar(
            Candle(
                symbol,
                Timeframe.M1,
                bar.timestamp,
                bar.timestamp + timedelta(minutes=1),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                Decimal(bar.volume),
                provenance,
                bar.vwap,
                int(bar.trade_count) if bar.trade_count is not None else None,
            )
        )
        for bar in bars
    ]
    clock = ReplayClock(start)
    registry = _research_registry()
    model_service, reference_dataset_ids, reference_events = _model_replay_inputs(
        repo, registry, clock, snapshot, symbol, start, end, root, model_root,
        buy_strategies, sell_strategies, parameters, reference_dataset_ids,
    )
    status_dataset_ids: dict[str, str] = {}
    if market_status_enabled:
        wanted = [
            name for name in ("SPY", "QQQ", "IWM")
            if name not in reference_dataset_ids_loaded(reference_events)
        ]
        status_dataset_ids, status_events = _reference_events(
            repo, wanted, snapshot, symbol, start, end, root, reference_dataset_ids
        )
        reference_events = [*reference_events, *status_events]
        reference_dataset_ids = {**dict(reference_dataset_ids or {}), **status_dataset_ids}
    events = [*events, *reference_events]
    replay_feed = HistoricalReplayFeed(
        events,
        DataProvenance(
            "alpaca",
            snapshot.feed.upper(),
            is_consolidated,
            snapshot.dataset_id,
            "RAW",
            False,
            QuoteFidelity.UNAVAILABLE,
        ),
        clock=clock,
        seed=seed,
    )
    assumptions = resolve_execution_assumptions(execution)
    sim = HistoricalExecutionSimulator(replay_feed, clock, assumptions)
    explicit_roles = any(
        item is not None
        for item in (buy_strategies, sell_strategies, short_strategies, cover_strategies)
    )
    short_ids = tuple(short_strategies or ())
    cover_ids = tuple(cover_strategies or ())
    if not explicit_roles:
        pair = resolve_pair_preset("vwap_reference", mode="shadow")
        buy_ids = (pair["buy_cls"].spec.strategy_id,)
        sell_ids = (pair["sell_cls"].spec.strategy_id,)
        strategy_params = {
            buy_ids[0]: dict(pair["buy_params"]),
            sell_ids[0]: dict(pair["sell_params"]),
        }
        lifecycle = "vwap_reference"
    else:
        buy_ids, sell_ids = tuple(buy_strategies or ()), tuple(sell_strategies or ())
        for side, ids in (
            (StrategyRole.BUY, buy_ids),
            (StrategyRole.SELL, sell_ids),
            (StrategyRole.SHORT, short_ids),
            (StrategyRole.COVER, cover_ids),
        ):
            for strategy_id in ids:
                if registry.get(strategy_id).spec.side is not side:
                    raise ValueError(f"{strategy_id} is not a {side.value} strategy")
        if short_ids and not short_selling_enabled:
            raise ValueError("short strategies require short_selling_enabled=True")
        strategy_params = {
            strategy_id: {} for strategy_id in (*buy_ids, *sell_ids, *short_ids, *cover_ids)
        }
        lifecycle = "collection"
    for strategy_id, values in (parameters or {}).items():
        if strategy_id not in strategy_params:
            raise ValueError(
                f"parameters supplied for strategy outside lifecycle pair: {strategy_id}"
            )
        strategy_params[strategy_id].update(values)
    session_candles, daily_dataset = _daily_history(
        repo, registry, symbol, start, end, strategy_params, feed=snapshot.feed
    )
    stop_config: dict[str, Any] = (
        {"protective_trailing_percent": "1.5"}
        if protective_stop is None
        else {"protective_stop": {k: str(v) for k, v in protective_stop.items()}}
    )
    stop_fraction = _stop_distance_fraction(
        {"type": "native_trailing", "trail_percent": "1.5"}
        if protective_stop is None
        else protective_stop
    )

    def intent_factory(evaluation: Any, context: Any, request: Any) -> OrderIntent:
        # Entries are BUY (long) and SHORT; exits are SELL (closes a long) and COVER (closes a
        # short).  The roles never imply one another: no automatic reversal.
        role = evaluation.side
        is_buy = role in (StrategyRole.BUY, StrategyRole.SHORT)
        order_side = {
            StrategyRole.BUY: OrderSide.BUY,
            StrategyRole.SELL: OrderSide.SELL,
            StrategyRole.SHORT: OrderSide.SELL_SHORT,
            StrategyRole.COVER: OrderSide.BUY_TO_COVER,
        }[role]
        if is_buy:
            reference = evaluation.reference_price
            if reference is None or reference <= 0:
                raise ValueError("entry signal has no valid reference price for risk sizing")
            budget_shares = Decimal("3000") / reference
            risk_shares = Decimal("30") / (reference * stop_fraction(reference))
            quantity = min(budget_shares, risk_shares).to_integral_value(rounding=ROUND_DOWN)
        else:
            quantity = sim.held_quantity(symbol)
        return OrderIntent(
            intent_id=f"{evaluation.signal_id}",
            client_order_id=f"research-{evaluation.signal_id}",
            account_scope_id="backtest:research",
            cycle_id=f"research:{symbol}",
            symbol=symbol,
            side=order_side,
            purpose=OrderPurpose.ENTRY if is_buy else OrderPurpose.STRATEGY_EXIT,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            created_at=context.as_of,
            quantity=quantity,
        )

    effective_eod = _resolve_eod_close(
        end_of_day_close,
        (*buy_ids, *sell_ids, *short_ids, *cover_ids, strategy_name),
    )
    request = BacktestRequest(
        (symbol,),
        start,
        end,
        PeriodRole.RESEARCH,
        {
            "dataset_id": snapshot.dataset_id,
            **({"daily_dataset_id": daily_dataset} if daily_dataset else {}),
            **({"end_of_day_close": effective_eod} if effective_eod is not None else {}),
            "mode": "BACKTEST",
            "lifecycle": lifecycle,
            "focus_strategy": strategy_name,
            "position_sizing": "min($3000 symbol budget, $30 planned loss / stop distance)",
            **stop_config,
            **(
                {"reference_dataset_ids": {**reference_dataset_ids, **status_dataset_ids}}
                if reference_dataset_ids or status_dataset_ids
                else {}
            ),
            **({"market_status_enabled": True} if market_status_enabled else {}),
            "entry_cutoff_minutes": 30,
            "minimum_hold_seconds": 60,
            "max_entry_cycles_per_session": max_entry_cycles_per_session,
            **({"end_of_day_close": dict(end_of_day_close)} if end_of_day_close is not None else {}),
            **(
                {
                    "short_selling_enabled": True,
                    "short_borrow_bps_per_day": str(short_borrow_bps_per_day),
                }
                if short_selling_enabled
                else {}
            ),
            "execution_model": "bar-only next-bar-open",
            "assumptions": dict(assumptions.as_mapping()),
        },
        seed,
        starting_capital,
        (*buy_ids, *sell_ids, *short_ids, *cover_ids),
        strategy_params=strategy_params,
        initial_strategy_state={buy_id: {"eligibility": "WATCHING_BUY"} for buy_id in buy_ids},
        result_directory=results,
    )
    result = asyncio.run(
        BacktestRunner(
            registry,
            replay_feed,
            clock,
            sim,
            intent_factory=intent_factory,
            session_candles=session_candles,
            model_service=model_service,
        ).run(request)
    )
    metrics = compute_metrics(
        result,
        starting_capital=starting_capital,
        seed=seed,
        bars=[
            {"symbol": symbol, "time": bar.timestamp, "high": bar.high, "low": bar.low}
            for bar in bars
        ],
    )
    bar_open = {bar.timestamp: bar.open for bar in bars}
    estimated_slippage = sum(
        (
            abs(Decimal(str(fill["price"])) - bar_open[fill["time"]])
            * Decimal(str(fill["quantity"]))
            for fill in result.fills
            if fill["time"] in bar_open
        ),
        Decimal(0),
    )
    total_fill_notional = sum(
        (Decimal(str(fill["price"])) * Decimal(str(fill["quantity"])) for fill in result.fills),
        Decimal(0),
    )
    unmatched_benchmarks = sum(fill["time"] not in bar_open for fill in result.fills)
    ending_equity = result.equity_curve[-1]["equity"] if result.equity_curve else starting_capital
    return {
        "symbol": symbol,
        "strategy": strategy_name,
        "lifecycle": lifecycle,
        # In the default ``vwap_reference`` lifecycle ``strategy`` is only a focus
        # label: both legs execute, so the two labels give identical trades.
        "strategy_is_label": lifecycle == "vwap_reference",
        "legs": {"buy": list(buy_ids), "sell": list(sell_ids)},
        "protective_stop": stop_config.get("protective_stop", "native_trailing 1.5%"),
        "end_of_day_close": effective_eod,
        "max_entry_cycles_per_session": max_entry_cycles_per_session,
        "exit_reasons": {
            reason: sum(t.get("exit_reason") == reason for t in metrics.trades)
            for reason in sorted({str(t.get("exit_reason")) for t in metrics.trades})
        },
        "dataset_id": snapshot.dataset_id,
        "feed": snapshot.feed,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bars": len(bars),
        "run_id": result.run_id,
        "results_hash": result.results_hash,
        "evaluations": len(result.evaluations),
        "signals": len(result.signals),
        "trades": len(metrics.trades),
        "wins": sum(t["pnl"] > 0 for t in metrics.trades),
        "losses": sum(t["pnl"] < 0 for t in metrics.trades),
        "win_rate": str(metrics.win_rate),
        "gross_return": str(metrics.total_return + estimated_slippage / starting_capital),
        "net_return": str(metrics.net_return),
        "avg_pnl": str(metrics.expectancy_net),
        "profit_factor": None if metrics.profit_factor is None else str(metrics.profit_factor),
        "max_drawdown": str(metrics.max_drawdown),
        "sharpe": None,
        "costs": str(sum((t["costs"] for t in metrics.trades), Decimal(0))),
        "estimated_slippage": str(estimated_slippage),
        "slippage_benchmark_unavailable_fills": unmatched_benchmarks,
        "total_fill_notional": str(total_fill_notional),
        "ending_equity": str(ending_equity),
        "execution_assumptions": dict(assumptions.as_mapping()),
        "trade_ledger": [
            {k: None if v is None else str(v) for k, v in t.items()}
            for t in metrics.trades
        ],
    }


def run_backtest(args: argparse.Namespace) -> int:
    resolve_worker_count(args.workers, 1)
    eod_close: dict[str, Any] | None = None
    if getattr(args, "disable_eod_close", False):
        eod_close = {"enabled": False}
    elif getattr(args, "eod_close_at", None):
        eod_close = {"enabled": True, "at": args.eod_close_at}
    result = _run_one(
        args.symbol.upper(),
        _moment(args.start),
        _moment(args.end),
        args.root,
        args.results,
        args.seed,
        args.strategy,
        dataset_id=getattr(args, "dataset_id", None),
        feed=getattr(args, "feed", None),
        end_of_day_close=eod_close,
    )
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "trade_ledger"}, sort_keys=True
        )
    )
    return 0


def run_batch(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.jobs).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("experiments"), list):
        raise ValueError("jobs file must contain an experiments array")
    jobs = [ExperimentSpec(**item) for item in payload["experiments"]]
    batch_hash = hashlib.sha256(Path(args.jobs).read_bytes()).hexdigest()[:16]
    batch = run_experiment_batch(
        jobs,
        workers=args.workers,
        root=args.root,
        results=args.results,
        batch_file=args.output or str(Path(args.results) / f"batch-{batch_hash}.json"),
    )
    return 0 if batch["failed_count"] == 0 else 1


def run_six(args: argparse.Namespace) -> int:
    start, end = _moment(args.start), _moment(args.end)
    feed = getattr(args, "feed", None)
    records: list[ExperimentSpec] = []
    for symbol in ("NVDA", "IBM", "TSLA"):
        for strategy in ("vwap_bounce", "vwap_breakdown"):
            records.append(
                ExperimentSpec(
                    symbol, strategy, start.isoformat(), end.isoformat(), args.seed, feed=feed
                )
            )
    output = Path(args.results) / "initial-six-experiment.json"
    batch = run_experiment_batch(
        records, workers=args.workers, root=args.root, results=args.results, batch_file=str(output)
    )
    return 0 if batch["failed_count"] == 0 else 1


def list_results(args: argparse.Namespace) -> int:
    from mike_trade.backtest.commands import BacktestResultsIndex

    rows = BacktestResultsIndex(args.results).list_runs(limit=args.limit)
    print(json.dumps(rows, sort_keys=True, default=str, indent=2))
    return 0
