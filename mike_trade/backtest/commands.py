"""Operator-facing bounded backtest command layer.

This module is parse/validate/delegate/render only. Business logic lives in
the application layer (:mod:`mike_trade.backtest.runner`,
:mod:`mike_trade.backtest.manifest`, :mod:`mike_trade.backtest.feed`).

Subcommands, wired via :func:`register` following the same pattern as
``register_config_commands`` (an ``add_parser`` group with sub-actions and a
dispatch function stored in argument defaults):

* ``backtest run`` -- requires explicit ``--symbols`` (1-10), ``--start`` and
  ``--end`` (bounded aware window), ``--dataset`` (path to a local offline
  dataset file), ``--seed`` and ``--config``. Optional ``--role``
  (``research`` | ``dev`` | ``holdout``) and ``--holdout-lock``. Builds a
  :class:`BacktestRequest` and delegates to the injected runner.
* ``backtest list`` -- the saved results index over the runner's persisted
  results/manifest store. Bounded by ``--limit`` (default 20).
* ``backtest show <run_id>`` -- prints the manifest and summary metrics.
* ``backtest export <run_id> --format csv|json --out <path>`` -- writes
  trades and metrics.

Rules enforced here, before any runner is invoked:

* A missing window, a window longer than the configured maximum, zero or more
  than 10 symbols, a missing dataset path, or any remote/network dataset
  source exits nonzero with a clear error and never calls the runner.
* There is never an implicit history download: this module performs no
  network calls at all and rejects any dataset value that is not a local
  file path.
* ``--role holdout`` without ``--holdout-lock`` is refused.
* The command layer never reads account credentials, never constructs an
  account client (``broker`` is always passed as ``None``), and never opens
  live/paper/shadow account state. It touches only the local results
  directory, and the results index only reads.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import inspect
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from mike_trade.backtest.manifest import HoldoutLock, PeriodRole
from mike_trade.backtest.runner import BacktestRequest, compute_metrics

__all__ = [
    "BacktestResultsIndex",
    "cmd_backtest_run",
    "cmd_backtest_list",
    "cmd_backtest_show",
    "cmd_backtest_export",
    "register",
]


_MAX_SYMBOLS = 10
_MAX_WINDOW_DAYS = 365
_DEFAULT_LIMIT = 20
_DEFAULT_ROLE = "research"
_DEFAULT_STARTING_CAPITAL = Decimal("100000")
_DEFAULT_RESULTS_DIR = ".backtest-runs"

_ROLE_BY_NAME: dict[str, PeriodRole] = {
    "research": PeriodRole.RESEARCH,
    "dev": PeriodRole.DEV,
    "holdout": PeriodRole.HOLDOUT,
}

_REMOTE_SOURCE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")

_TRADE_COLUMNS: tuple[str, ...] = (
    "symbol",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "quantity",
    "gross_pnl",
    "costs",
    "pnl",
    "hold_duration_seconds",
    "mfe",
    "mae",
)


class _InputError(ValueError):
    """A CLI argument failed validation; reported with exit code 2."""


def _fail(stderr: Any, subcommand: str, message: str) -> int:
    stderr.write(f"backtest {subcommand}: error: {message}\n")
    return 2


def _stdout(args: Any, given: Any) -> Any:
    return given if given is not None else sys.stdout


def _stderr(args: Any, given: Any) -> Any:
    return given if given is not None else sys.stderr


def _results_dir(args: Any) -> str:
    return str(getattr(args, "results_dir", None) or _DEFAULT_RESULTS_DIR)


# ---------------------------------------------------------------------------
# Argument parsing helpers (pure; no I/O, no account access)
# ---------------------------------------------------------------------------


def _parse_symbols(raw: Any) -> tuple[str, ...]:
    """Normalise ``--symbols`` (repeatable and/or comma-separated) to tickers."""
    if raw is None:
        return ()
    parts: list[str] = raw if isinstance(raw, (list, tuple)) else [raw]
    symbols: list[str] = []
    for part in parts:
        for token in str(part).split(","):
            token = token.strip().upper()
            if token:
                symbols.append(token)
    return tuple(symbols)


def _parse_moment(raw: Any, name: str) -> datetime:
    """Parse an ISO-8601 moment; naive values are read as UTC."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise _InputError(f"--{name} is required (bounded window needs --start and --end)")
    if isinstance(raw, datetime):
        moment = raw
    else:
        try:
            moment = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
        except ValueError:
            raise _InputError(
                f"--{name} {raw!r} is not an ISO-8601 datetime"
            ) from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _parse_seed(raw: Any) -> int:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise _InputError("--seed is required")
    if isinstance(raw, bool):
        raise _InputError("--seed must be an integer")
    try:
        seed = int(str(raw).strip())
    except ValueError:
        raise _InputError(f"--seed {raw!r} is not an integer") from None
    return seed


def _parse_role(raw: Any) -> PeriodRole:
    name = str(raw or _DEFAULT_ROLE).strip().lower()
    try:
        return _ROLE_BY_NAME[name]
    except KeyError:
        raise _InputError(
            f"--role {raw!r} is unknown; expected one of research, dev, holdout"
        ) from None


def _validate_dataset(raw: Any) -> Path:
    """Accept only an existing local dataset file; refuse remote sources."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise _InputError("--dataset is required (path to a local offline dataset file)")
    source = str(raw).strip()
    if _REMOTE_SOURCE_RE.match(source) or source.startswith("//"):
        raise _InputError(
            f"--dataset {source!r} looks like a remote source; "
            "backtests run only on local offline dataset files and never download"
        )
    path = Path(source)
    if not path.is_file():
        raise _InputError(f"--dataset not found or not a file: {source}")
    return path


def _load_mapping_file(raw: Any, flag: str) -> dict[str, Any]:
    """Load a JSON (preferred) or YAML mapping file into a plain dict."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise _InputError(f"--{flag} is required")
    path = Path(str(raw).strip())
    if not path.is_file():
        raise _InputError(f"--{flag} file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _InputError(f"--{flag} cannot be read: {exc}") from None
    try:
        document = json.loads(text)
    except ValueError:
        document = None
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            raise _InputError(
                f"--{flag} is not JSON and YAML support is unavailable; "
                "provide a JSON mapping file"
            ) from None
        try:
            document = yaml.safe_load(text)
        except Exception as exc:
            raise _InputError(f"--{flag} is neither valid JSON nor valid YAML: {exc}") from None
    if not isinstance(document, dict):
        raise _InputError(f"--{flag} must hold a top-level mapping of settings")
    return dict(document)


def _parse_lock_file(raw: Any) -> HoldoutLock | None:
    """Load an explicit holdout lock file into a :class:`HoldoutLock`."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    mapping = _load_mapping_file(raw, "holdout-lock")
    try:
        token = mapping["token"]
        research_hash = mapping["research_config_hash"]
        window_start = mapping["window_start"]
        window_end = mapping["window_end"]
        if isinstance(window_start, str):
            window_start = _parse_moment(window_start, "holdout-lock window_start")
        if isinstance(window_end, str):
            window_end = _parse_moment(window_end, "holdout-lock window_end")
        return HoldoutLock(
            token=str(token),
            research_config_hash=str(research_hash),
            window_start=window_start,
            window_end=window_end,
        )
    except KeyError as exc:
        raise _InputError(
            f"--holdout-lock is missing required key {exc}; "
            "expected token, research_config_hash, window_start, window_end"
        ) from None
    except (ValueError, TypeError) as exc:
        raise _InputError(f"--holdout-lock is invalid: {exc}") from None


def _parse_limit(raw: Any) -> int:
    if raw is None:
        return _DEFAULT_LIMIT
    try:
        limit = int(str(raw).strip())
    except ValueError:
        raise _InputError(f"--limit {raw!r} is not an integer") from None
    if limit < 1:
        raise _InputError(f"--limit must be >= 1 (got {limit})")
    return limit


def _build_request(args: Any) -> BacktestRequest:
    """Validate CLI arguments and build the runner request (no I/O but reads)."""
    symbols = _parse_symbols(getattr(args, "symbols", None))
    if not symbols:
        raise _InputError("--symbols is required (1-10 tickers)")
    if len(symbols) > _MAX_SYMBOLS:
        raise _InputError(
            f"--symbols takes at most {_MAX_SYMBOLS} tickers (got {len(symbols)})"
        )

    window_start = _parse_moment(getattr(args, "start", None), "start")
    window_end = _parse_moment(getattr(args, "end", None), "end")
    if window_end <= window_start:
        raise _InputError("--end must be after --start")
    if window_end - window_start > timedelta(days=_MAX_WINDOW_DAYS):
        raise _InputError(
            f"window of {(window_end - window_start).days} days exceeds the "
            f"configured maximum of {_MAX_WINDOW_DAYS} days"
        )

    dataset_path = _validate_dataset(getattr(args, "dataset", None))
    seed = _parse_seed(getattr(args, "seed", None))
    settings = _load_mapping_file(getattr(args, "config", None), "config")
    role = _parse_role(getattr(args, "role", None))
    lock = _parse_lock_file(getattr(args, "holdout_lock", None))
    if role is PeriodRole.HOLDOUT and lock is None:
        raise _InputError("--role holdout requires an explicit --holdout-lock")

    effective_config: dict[str, Any] = dict(settings)
    effective_config["dataset"] = str(dataset_path)

    starting_raw = settings.get("starting_capital", _DEFAULT_STARTING_CAPITAL)
    try:
        starting_capital = (
            starting_raw
            if isinstance(starting_raw, Decimal)
            else Decimal(str(starting_raw))
        )
    except (InvalidOperation, ValueError, TypeError):
        raise _InputError("--config starting_capital is not a valid decimal") from None
    if starting_capital <= 0:
        raise _InputError("--config starting_capital must be > 0")

    strategy_ids = settings.get("strategy_ids", ())
    if isinstance(strategy_ids, str):
        strategy_ids = (strategy_ids,)
    try:
        strategy_ids_tuple = tuple(str(item) for item in strategy_ids)
    except TypeError:
        raise _InputError("--config strategy_ids must be a list of names") from None
    strategy_params = settings.get("strategy_params", {})
    strategy_versions = settings.get("strategy_versions", {})
    if not isinstance(strategy_params, dict) or not isinstance(strategy_versions, dict):
        raise _InputError("--config strategy_params/strategy_versions must be mappings")

    try:
        return BacktestRequest(
            symbols=tuple(symbols),
            window_start=window_start,
            window_end=window_end,
            period_role=role,
            config=effective_config,
            seed=seed,
            starting_capital=starting_capital,
            strategy_ids=strategy_ids_tuple,
            strategy_params={str(k): dict(v) for k, v in strategy_params.items()},
            strategy_versions={str(k): str(v) for k, v in strategy_versions.items()},
            holdout_lock=lock,
            broker=None,
            result_directory=_results_dir(args),
        )
    except (ValueError, TypeError) as exc:
        raise _InputError(f"invalid backtest request: {exc}") from None


# ---------------------------------------------------------------------------
# Read-only saved results index over the runner's persisted store
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        moment = value
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


class BacktestResultsIndex:
    """Read-only index over ``<results-dir>/*.manifest.json`` + results files.

    The index only reads. It never writes, mutates, or deletes manifests or
    results. Ordering is deterministic: ``created_at`` descending, then
    ``run_id`` ascending. Summaries (trade count, net return) are computed
    with the application layer's :func:`compute_metrics`; files that fail to
    parse are skipped (list) or reported as missing (get).
    """

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        return self._directory

    def _read_json_file(self, path: Path) -> dict[str, Any] | None:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def _manifest_record(self, path: Path) -> dict[str, Any] | None:
        raw = self._read_json_file(path)
        if raw is None:
            return None
        required = (
            "run_id",
            "created_at",
            "config_hash",
            "symbols",
            "window_start",
            "window_end",
            "period_role",
            "seed",
        )
        if any(raw.get(key) is None for key in required):
            return None
        if not isinstance(raw.get("symbols"), list) or not raw["symbols"]:
            return None
        return raw

    def _results_for(self, run_id: str, manifest_path: Path | None = None) -> dict[str, Any]:
        path = (
            manifest_path.with_name(manifest_path.name.replace(".manifest.json", ".results.json"))
            if manifest_path is not None
            else self._directory / f"{run_id}.results.json"
        )
        if manifest_path is None and not path.is_file():
            matches = sorted(self._directory.glob(f"executions/*/{run_id}.results.json"))
            path = matches[0] if matches else path
        raw = self._read_json_file(path)
        return raw if raw is not None else {}

    def _summary(self, manifest: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
        fills = results.get("fills", ())
        equity = results.get("equity_curve", ())
        try:
            starting = Decimal(str(manifest.get("starting_capital", "0") or "0"))
        except (InvalidOperation, ValueError, TypeError):
            starting = Decimal(0)
        try:
            seed = int(manifest.get("seed", 0))
        except (ValueError, TypeError):
            seed = 0
        try:
            metrics = compute_metrics(
                {"fills": fills if isinstance(fills, list) else [],
                 "equity_curve": equity if isinstance(equity, list) else []},
                starting_capital=starting if starting > 0 else _DEFAULT_STARTING_CAPITAL,
                seed=seed,
            )
        except Exception:
            return {
                "trade_count": 0,
                "net_return": "0",
                "total_return": "0",
                "win_rate": "0",
                "max_drawdown": "0",
                "profit_factor": None,
                "expectancy_net": "0",
                "capital_utilization": "0",
                "sample_size": 0,
                "warnings": ["metrics unavailable for this run"],
            }
        return {
            "trade_count": int(metrics.sample_size),
            "net_return": str(metrics.net_return),
            "total_return": str(metrics.total_return),
            "win_rate": str(metrics.win_rate),
            "max_drawdown": str(metrics.max_drawdown),
            "profit_factor": None
            if metrics.profit_factor is None
            else str(metrics.profit_factor),
            "expectancy_net": str(metrics.expectancy_net),
            "capital_utilization": str(metrics.capital_utilization),
            "sample_size": int(metrics.sample_size),
            "warnings": list(metrics.warnings),
        }

    def _row(self, manifest: dict[str, Any], manifest_path: Path | None = None) -> dict[str, Any]:
        summary = self._summary(
            manifest,
            self._results_for(str(manifest.get("execution_id") or manifest["run_id"]), manifest_path),
        )
        return {
            "run_id": str(manifest["run_id"]),
            "execution_id": manifest.get("execution_id"),
            "created_at": str(manifest["created_at"]),
            "symbols": [str(item) for item in manifest["symbols"]],
            "window": {
                "start": str(manifest["window_start"]),
                "end": str(manifest["window_end"]),
            },
            "role": str(manifest["period_role"]),
            "config_hash": str(manifest["config_hash"]),
            "seed": int(manifest["seed"]),
            "trade_count": summary["trade_count"],
            "net_return": summary["net_return"],
        }

    def list_runs(self, limit: int = _DEFAULT_LIMIT) -> list[dict[str, Any]]:
        """Return index rows in deterministic order, never more than ``limit``."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("--limit must be an integer >= 1")
        if not self._directory.is_dir():
            return []
        manifests: list[dict[str, Any]] = []
        paths = sorted(self._directory.glob("*.manifest.json"))
        paths.extend(sorted(self._directory.glob("executions/*/*.manifest.json")))
        for path in paths:
            record = self._manifest_record(path)
            if record is not None:
                manifests.append({**record, "_artifact_path": str(path)})
        manifests.sort(
            key=lambda raw: (str(raw.get("created_at", "")), str(raw.get("run_id", ""))),
            reverse=False,
        )
        ordered = sorted(
            manifests,
            key=lambda raw: (str(raw.get("created_at", "")),),
            reverse=True,
        )
        # Stable regroup: created_at desc with run_id asc ties.
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in ordered:
            grouped.setdefault(str(record.get("created_at", "")), []).append(record)
        deterministic: list[dict[str, Any]] = []
        for created in sorted(grouped, reverse=True):
            deterministic.extend(sorted(grouped[created], key=lambda raw: str(raw["run_id"])))
        return [
            self._row(record, Path(record["_artifact_path"]))
            for record in deterministic[:limit]
        ]

    def get(self, run_id: str) -> dict[str, Any] | None:
        """Return the full record (manifest, summary, trades) for ``run_id``."""
        if not isinstance(run_id, str) or not run_id.strip():
            return None
        if "/" in run_id or "\\" in run_id or ".." in run_id:
            return None
        manifest_path = self._directory / f"{run_id}.manifest.json"
        if not manifest_path.is_file():
            candidates = sorted(self._directory.glob(f"executions/*/{run_id}.manifest.json"))
            if not candidates:
                candidates = [
                    path for path in sorted(self._directory.glob("executions/*/*.manifest.json"))
                    if (record := self._manifest_record(path)) is not None
                    and record.get("execution_id") == run_id
                ]
            manifest_path = candidates[0] if candidates else manifest_path
        manifest = self._manifest_record(manifest_path)
        if manifest is None:
            return None
        results = self._results_for(str(manifest.get("execution_id") or run_id), manifest_path)
        summary = self._summary(manifest, results)
        trades = self._trades(manifest, results)
        return {
            "run_id": str(manifest["run_id"]),
            "execution_id": manifest.get("execution_id"),
            "manifest": _jsonable(dict(manifest)),
            "summary": _jsonable(summary),
            "trades": _jsonable(trades),
        }

    def _trades(
        self, manifest: dict[str, Any], results: dict[str, Any]
    ) -> list[dict[str, Any]]:
        fills = results.get("fills", ())
        equity = results.get("equity_curve", ())
        try:
            starting = Decimal(str(manifest.get("starting_capital", "0") or "0"))
        except (InvalidOperation, ValueError, TypeError):
            starting = Decimal(0)
        try:
            seed = int(manifest.get("seed", 0))
        except (ValueError, TypeError):
            seed = 0
        try:
            metrics = compute_metrics(
                {"fills": fills if isinstance(fills, list) else [],
                 "equity_curve": equity if isinstance(equity, list) else []},
                starting_capital=starting if starting > 0 else _DEFAULT_STARTING_CAPITAL,
                seed=seed,
            )
        except Exception:
            return []
        return [dict(trade) for trade in metrics.trades]


# ---------------------------------------------------------------------------
# Command handlers (each returns a process exit code)
# ---------------------------------------------------------------------------


def cmd_backtest_run(
    args: Any,
    *,
    runner: Any = None,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    """Validate arguments, build a :class:`BacktestRequest`, delegate once."""
    out, err = _stdout(args, stdout), _stderr(args, stderr)
    try:
        request = _build_request(args)
    except _InputError as exc:
        return _fail(err, "run", str(exc))
    if runner is None:
        return _fail(err, "run", "no backtest runner is wired in; refusing to run")
    try:
        outcome = runner.run(request)
        if inspect.isawaitable(outcome):
            outcome = asyncio.run(_await_outcome(outcome))
    except _InputError as exc:
        return _fail(err, "run", str(exc))
    except Exception as exc:
        err.write(f"backtest run: error: runner failed: {exc}\n")
        return 1
    run_id = getattr(outcome, "run_id", None) or getattr(request, "run_id", "?")
    out.write(f"run_id={run_id}\n")
    out.write(f"symbols={','.join(request.symbols)}\n")
    out.write(f"window={request.window_start.isoformat()}..{request.window_end.isoformat()}\n")
    out.write(f"role={request.period_role.value}\n")
    out.write(f"seed={request.seed}\n")
    return 0


async def _await_outcome(awaitable: Any) -> Any:
    return await awaitable


def cmd_backtest_list(
    args: Any,
    *,
    index: BacktestResultsIndex | None = None,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    """List prior runs from the saved results index (newest first)."""
    out, err = _stdout(args, stdout), _stderr(args, stderr)
    try:
        limit = _parse_limit(getattr(args, "limit", None))
    except _InputError as exc:
        return _fail(err, "list", str(exc))
    as_json = bool(getattr(args, "json", False))
    store = index if index is not None else BacktestResultsIndex(_results_dir(args))
    try:
        rows = store.list_runs(limit=limit)
    except ValueError as exc:
        return _fail(err, "list", str(exc))
    if as_json:
        out.write(json.dumps({"runs": rows}, indent=2, sort_keys=True) + "\n")
        return 0
    out.write(f"runs={len(rows)} limit={limit}\n")
    for row in rows:
        out.write(
            f"{row['run_id']} {row['created_at']} "
            f"symbols={','.join(row['symbols'])} "
            f"window={row['window']['start']}..{row['window']['end']} "
            f"role={row['role']} seed={row['seed']} "
            f"trades={row['trade_count']} net_return={row['net_return']}\n"
        )
    return 0


def cmd_backtest_show(
    args: Any,
    *,
    index: BacktestResultsIndex | None = None,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    """Print the manifest and summary metrics for one run."""
    out, err = _stdout(args, stdout), _stderr(args, stderr)
    run_id = getattr(args, "run_id", None)
    if not run_id or not str(run_id).strip():
        return _fail(err, "show", "a run_id is required")
    as_json = bool(getattr(args, "json", False))
    store = index if index is not None else BacktestResultsIndex(_results_dir(args))
    record = store.get(str(run_id).strip())
    if record is None:
        return _fail(err, "show", f"unknown run_id: {run_id}")
    if as_json:
        out.write(
            json.dumps(
                {
                    "run_id": record["run_id"],
                    "manifest": record["manifest"],
                    "summary": record["summary"],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return 0
    manifest = record["manifest"]
    summary = record["summary"]
    out.write(f"run_id={record['run_id']}\n")
    for key in (
        "created_at",
        "symbols",
        "window_start",
        "window_end",
        "period_role",
        "config_hash",
        "seed",
        "starting_capital",
        "dataset_hash",
        "data_revision",
    ):
        if key in manifest:
            value = manifest[key]
            out.write(f"{key}={value}\n")
    for key in (
        "trade_count",
        "net_return",
        "total_return",
        "win_rate",
        "max_drawdown",
        "profit_factor",
        "expectancy_net",
        "capital_utilization",
    ):
        if key in summary:
            out.write(f"{key}={summary[key]}\n")
    return 0


def cmd_backtest_export(
    args: Any,
    *,
    index: BacktestResultsIndex | None = None,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    """Write one run's trades and metrics to ``--out`` as CSV or JSON."""
    out, err = _stdout(args, stdout), _stderr(args, stderr)
    run_id = getattr(args, "run_id", None)
    if not run_id or not str(run_id).strip():
        return _fail(err, "export", "a run_id is required")
    fmt = str(getattr(args, "format", None) or "").strip().lower()
    if fmt not in ("csv", "json"):
        return _fail(err, "export", "--format csv|json is required")
    dest = getattr(args, "out", None)
    if dest is None or not str(dest).strip():
        return _fail(err, "export", "--out <path> is required")
    store = index if index is not None else BacktestResultsIndex(_results_dir(args))
    record = store.get(str(run_id).strip())
    if record is None:
        return _fail(err, "export", f"unknown run_id: {run_id}")
    target = Path(str(dest).strip())
    try:
        if target.parent != Path(".") and str(target.parent):
            target.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            payload = {
                "run_id": record["run_id"],
                "metrics": record["summary"],
                "trades": record["trades"],
            }
            target.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        else:
            lines: list[str] = []
            for key in sorted(record["summary"]):
                value = record["summary"][key]
                rendered = (
                    json.dumps(value, sort_keys=True)
                    if isinstance(value, (dict, list))
                    else value
                )
                lines.append(f"# {key}={rendered}\n")
            rows = []
            for trade in record["trades"]:
                rows.append({col: trade.get(col, "") for col in _TRADE_COLUMNS})
            with target.open("w", encoding="utf-8", newline="\n") as stream:
                stream.writelines(lines)
                writer = csv.DictWriter(stream, fieldnames=list(_TRADE_COLUMNS))
                writer.writeheader()
                writer.writerows(rows)
    except OSError as exc:
        return _fail(err, "export", f"cannot write {target}: {exc}")
    out.write(f"exported run_id={record['run_id']} format={fmt} path={target}\n")
    return 0


# ---------------------------------------------------------------------------
# Registration hook (same shape as register_config_commands)
# ---------------------------------------------------------------------------


def register(subparsers: Any) -> None:
    """Add the ``backtest`` command group to an argparse subparsers action."""
    backtest_parser = subparsers.add_parser(
        "backtest",
        help="bounded offline backtests over local datasets (never downloads, never trades)",
    )
    actions = backtest_parser.add_subparsers(
        dest="backtest_subcommand", metavar="run|list|show|export"
    )

    run_parser = actions.add_parser(
        "run", help="run one bounded backtest from a local dataset"
    )
    run_parser.add_argument(
        "--symbols",
        action="append",
        default=None,
        metavar="SYM[,SYM...]",
        help="tickers to backtest, repeatable and/or comma-separated (1-10)",
    )
    run_parser.add_argument("--start", default=None, help="window start (ISO-8601)")
    run_parser.add_argument("--end", default=None, help="window end (ISO-8601)")
    run_parser.add_argument(
        "--dataset", default=None, help="path to a local offline dataset file"
    )
    run_parser.add_argument("--seed", default=None, help="replay seed (integer)")
    run_parser.add_argument("--config", default=None, help="backtest config file (JSON)")
    run_parser.add_argument(
        "--role",
        choices=("research", "dev", "holdout"),
        default="research",
        help="period role (holdout needs --holdout-lock)",
    )
    run_parser.add_argument(
        "--holdout-lock", default=None, help="explicit holdout lock file (JSON)"
    )
    run_parser.add_argument(
        "--results-dir", default=_DEFAULT_RESULTS_DIR, help="persisted results directory"
    )
    run_parser.set_defaults(backtest_subcommand="run", func=cmd_backtest_run)

    list_parser = actions.add_parser("list", help="list prior backtest runs (newest first)")
    list_parser.add_argument(
        "--limit", default=_DEFAULT_LIMIT, help="maximum rows to show (default 20)"
    )
    list_parser.add_argument(
        "--json", action="store_true", help="machine-readable JSON output"
    )
    list_parser.add_argument(
        "--results-dir", default=_DEFAULT_RESULTS_DIR, help="persisted results directory"
    )
    list_parser.set_defaults(backtest_subcommand="list", func=cmd_backtest_list)

    show_parser = actions.add_parser("show", help="print one run's manifest and metrics")
    show_parser.add_argument("run_id", help="run identifier from backtest list")
    show_parser.add_argument(
        "--json", action="store_true", help="machine-readable JSON output"
    )
    show_parser.add_argument(
        "--results-dir", default=_DEFAULT_RESULTS_DIR, help="persisted results directory"
    )
    show_parser.set_defaults(backtest_subcommand="show", func=cmd_backtest_show)

    export_parser = actions.add_parser("export", help="write one run's trades and metrics")
    export_parser.add_argument("run_id", help="run identifier from backtest list")
    export_parser.add_argument(
        "--format", choices=("csv", "json"), default=None, help="output format"
    )
    export_parser.add_argument("--out", default=None, help="destination file path")
    export_parser.add_argument(
        "--results-dir", default=_DEFAULT_RESULTS_DIR, help="persisted results directory"
    )
    export_parser.set_defaults(backtest_subcommand="export", func=cmd_backtest_export)
