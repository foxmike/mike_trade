"""CLI workflows for persistent market history and reproducible experiments."""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import hashlib
import json
import os
import time
import traceback
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

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
        if self.seed < 0:
            raise ValueError("experiment seed must be nonnegative")
        if _moment(self.start) >= _moment(self.end):
            raise ValueError("experiment start must precede end")
        if self.parameters is not None and not isinstance(self.parameters, Mapping):
            raise ValueError("experiment parameters must be a strategy-id mapping")
        if Decimal(self.starting_capital) <= 0:
            raise ValueError("starting_capital must be a positive decimal string")
        if self.execution is not None and not isinstance(self.execution, Mapping):
            raise ValueError("execution assumptions must be a mapping")


def available_cpu_count() -> int:
    """Return CPUs available to this process, honoring affinity and cgroup quota."""
    affinity = (
        len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else (os.cpu_count() or 1)
    )
    quota_count = affinity
    for quota_path in ("/sys/fs/cgroup/cpu.max", "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"):
        try:
            values = Path(quota_path).read_text().strip().split()
            if quota_path.endswith("cpu.max"):
                quota, period = values
            else:
                quota, period = (
                    values[0],
                    Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text().strip(),
                )
            if quota != "max" and int(quota) > 0:
                quota_count = max(1, int((int(quota) + int(period) - 1) // int(period)))
                break
        except (OSError, ValueError, IndexError):
            continue
    return max(1, min(affinity, quota_count))


def _available_memory_bytes() -> int | None:
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text().strip()
            if raw != "max":
                limit = int(raw)
                if limit < (1 << 60):
                    current_path = (
                        "/sys/fs/cgroup/memory.current"
                        if path.endswith("memory.max")
                        else "/sys/fs/cgroup/memory/memory.usage_in_bytes"
                    )
                    used = int(Path(current_path).read_text().strip())
                    return max(0, limit - used)
        except (OSError, ValueError):
            pass
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int(pages * page_size)
    except (ValueError, OSError, AttributeError):
        return None


def resolve_worker_count(requested: str | int, job_count: int) -> int:
    if job_count < 1:
        raise ValueError("a batch must contain at least one experiment")
    cpu_limit = available_cpu_count()
    available_memory = _available_memory_bytes()
    memory_limit = (
        cpu_limit if available_memory is None else max(1, available_memory // (512 * 1024 * 1024))
    )
    resource_limit = min(cpu_limit, memory_limit)
    if str(requested).lower() == "auto":
        return max(1, min(resource_limit, job_count))
    try:
        workers = int(requested)
    except (TypeError, ValueError) as exc:
        raise ValueError("--workers must be a positive integer or 'auto'") from exc
    if workers < 1:
        raise ValueError("--workers must be at least 1")
    return min(workers, resource_limit, job_count)


def run_experiment_batch(
    jobs: list[ExperimentSpec],
    *,
    workers: str | int,
    root: str,
    results: str,
    batch_file: str | None = None,
) -> dict[str, Any]:
    """Bounded process coordinator; each child invokes the existing runner."""
    repository = HistoricalDatasetRepository(root)
    resolved_jobs = []
    for job in jobs:
        job.validate()
        if job.dataset_id is None:
            start, end = _moment(job.start), _moment(job.end)
            candidates = [
                snapshot
                for snapshot in repository.snapshots()
                if snapshot.symbol == job.symbol.upper()
                and snapshot.feed == "iex"
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
    selected_workers = resolve_worker_count(workers, len(jobs))
    started = time.monotonic()
    completed: list[dict[str, Any]] = []
    pending_specs = iter(enumerate(jobs))
    in_flight: dict[concurrent.futures.Future[dict[str, Any]], int] = {}
    pool = ProcessPoolExecutor(max_workers=selected_workers)
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
                    completed.append(
                        {
                            "job_index": index,
                            "status": "failed",
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "traceback": "".join(traceback.format_exception(exc))[-12000:],
                        }
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
        "jobs": completed,
    }
    output = Path(batch_file) if batch_file else Path(results) / "batch-results.json"
    output.parent.mkdir(parents=True, exist_ok=True)
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
    )
    result.pop("trade_ledger", None)  # Ledger remains in the immutable per-run artifact.
    result.update(status="completed", worker_pid=os.getpid())
    return result


@lru_cache(maxsize=2)
def _cached_snapshot_bars(
    root: str, symbol: str, start: str, end: str, dataset_id: str
) -> tuple[Any, ...]:
    """Bounded per-process cache of validated rows from immutable snapshots."""
    return HistoricalDatasetRepository(root).available(
        symbol, _moment(start), _moment(end), feed="iex", adjustment="raw", dataset_id=dataset_id
    )


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
        snapshots = HistoricalDatasetRepository(args.root).acquire(
            client,
            (s.strip() for s in args.symbols.split(",") if s.strip()),
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
    from mike_trade.strategies.buy.intraday_consolidation_breakout import (
        IntradayConsolidationBreakoutBuy,
    )
    from mike_trade.strategies.buy.momentum_continuation import MomentumContinuationBuy
    from mike_trade.strategies.buy.rsi_momentum import RsiMomentumBuy
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

    registry = StrategyRegistry()
    for cls in (
        VwapBounceBuy,
        MomentumBreakoutBuy,
        OpeningRangeBreakoutBuy,
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
        IntradayConsolidationBreakoutBuy,
        MomentumContinuationBuy,
        RsiMomentumBuy,
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
    snapshots = [
        s
        for s in repo.snapshots()
        if s.symbol == symbol
        and s.feed == "iex"
        and s.adjustment == "raw"
        and datetime.fromisoformat(s.start) <= start
        and datetime.fromisoformat(s.end) >= end
        and (dataset_id is None or s.dataset_id == dataset_id)
    ]
    if not snapshots:
        raise ValueError(
            f"no matching immutable snapshot covers {symbol} {start.isoformat()}..{end.isoformat()}"
        )
    snapshot = max(snapshots, key=lambda s: s.acquired_at)
    bars = _cached_snapshot_bars(
        str(Path(root).resolve()), symbol, start.isoformat(), end.isoformat(), snapshot.dataset_id
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
            f"no stored IEX bars cover {symbol} {start.isoformat()}..{end.isoformat()}"
        )
    provenance = FeedProvenance("replay", "IEX", "alpaca", False)
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
    feed = HistoricalReplayFeed(
        events,
        DataProvenance(
            "alpaca", "IEX", False, snapshot.dataset_id, "RAW", False, QuoteFidelity.UNAVAILABLE
        ),
        clock=clock,
        seed=seed,
    )
    assumption_values: dict[str, Any] = {
        "slippage_bps": Decimal("5"),
        "fee_per_order": Decimal("0"),
        "fee_per_share": Decimal("0"),
        "fee_bps": Decimal("0"),
    }
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
    assumptions = ExecutionAssumptions(**assumption_values)
    sim = HistoricalExecutionSimulator(feed, clock, assumptions)
    registry = _research_registry()
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

    request = BacktestRequest(
        (symbol,),
        start,
        end,
        PeriodRole.RESEARCH,
        {
            "dataset_id": snapshot.dataset_id,
            "mode": "BACKTEST",
            "lifecycle": lifecycle,
            "focus_strategy": strategy_name,
            "position_sizing": "min($3000 symbol budget, $30 planned loss / stop distance)",
            **stop_config,
            "entry_cutoff_minutes": 30,
            "minimum_hold_seconds": 60,
            "max_entry_cycles_per_session": 1,
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
        BacktestRunner(registry, feed, clock, sim, intent_factory=intent_factory).run(request)
    )
    metrics = compute_metrics(result, starting_capital=starting_capital, seed=seed)
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
        "exit_reasons": {
            reason: sum(t.get("exit_reason") == reason for t in metrics.trades)
            for reason in sorted({str(t.get("exit_reason")) for t in metrics.trades})
        },
        "dataset_id": snapshot.dataset_id,
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
        "trade_ledger": [{k: str(v) for k, v in t.items()} for t in metrics.trades],
    }


def run_backtest(args: argparse.Namespace) -> int:
    resolve_worker_count(args.workers, 1)
    result = _run_one(
        args.symbol.upper(),
        _moment(args.start),
        _moment(args.end),
        args.root,
        args.results,
        args.seed,
        args.strategy,
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
    records: list[ExperimentSpec] = []
    for symbol in ("NVDA", "IBM", "TSLA"):
        for strategy in ("vwap_bounce", "vwap_breakdown"):
            records.append(
                ExperimentSpec(symbol, strategy, start.isoformat(), end.isoformat(), args.seed)
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
