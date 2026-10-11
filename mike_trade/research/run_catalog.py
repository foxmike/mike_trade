"""DuckDB persistent research run catalog with incremental indexing and fast SQL search."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from mike_trade.research.attribution import (
    build_signal_indexes,
    reconcile_trade_attribution,
    recover_run_strategies,
    recover_stop_configuration,
)

ZERO = Decimal("0")


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, TypeError, ArithmeticError):
        return None


def _to_float(value: object) -> float | None:
    dec = _to_decimal(value)
    return float(dec) if dec is not None else None


def _to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (ValueError, TypeError):
        return None


#: Bump whenever the persisted ``research_runs_index`` layout or derived metric
#: definitions change. A stale persisted index is dropped and rebuilt from the
#: immutable run artifacts on next use.
INDEX_SCHEMA_VERSION = 3

_ENTRY_SIDES = frozenset({"BUY", "SELL_SHORT"})
_EXIT_SIDES = frozenset({"SELL", "BUY_TO_COVER"})


def _fmt(value: Decimal | None, places: int = 6) -> str | None:
    """Fixed-point string rounded to ``places`` (keeps API payloads free of 28-digit noise)."""
    if value is None:
        return None
    return format(round(value, places), "f")


def stop_policy_label(stop_cfg: dict[str, Any]) -> str | None:
    """Short, filterable description of the recorded protective stop, or None.

    Runs recorded before stop settings were captured have no stop information;
    they return None (rendered as unrecorded) rather than a guessed default.
    """
    if not stop_cfg:
        return None
    spec = stop_cfg.get("protective_stop")
    if isinstance(spec, dict):
        kind = str(spec.get("type", "stop"))
        parts = [
            f"{key.replace('_percent', '')} {value}%"
            if key.endswith("_percent")
            else f"{key.replace('_amount', '')} ${value}"
            for key, value in spec.items()
            if key != "type"
        ]
        return f"{kind} {', '.join(parts)}".strip()
    if isinstance(spec, str) and spec:
        return spec
    pct = stop_cfg.get("protective_trailing_percent")
    if pct is not None:
        return f"native_trailing {pct}%"
    return None


def compute_trade_stats(pnls: list[Decimal]) -> dict[str, Any]:
    """Closed-trade statistics from realized P&L values (one per closed trade)."""
    wins = sorted((p for p in pnls if p > ZERO), reverse=True)
    losses = [p for p in pnls if p < ZERO]
    gross_profit = sum(wins, ZERO)
    gross_loss = -sum(losses, ZERO)
    realized = sum(pnls, ZERO)
    count = len(pnls)
    top3 = sum(wins[:3], ZERO)
    average_win = gross_profit / len(wins) if wins else None
    average_loss = -gross_loss / len(losses) if losses else None
    payoff = (
        average_win / -average_loss
        if average_win is not None and average_loss is not None and average_loss < ZERO
        else None
    )

    text = _fmt

    return {
        "closed_trades": count,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "breakeven_trades": count - len(wins) - len(losses),
        "gross_profit": _fmt(gross_profit),
        "gross_loss": _fmt(gross_loss),
        "realized_pnl": _fmt(realized),
        "average_win": text(average_win),
        "average_loss": text(average_loss),
        "expectancy": text(realized / count if count else None),
        "payoff_ratio": text(payoff),
        "best_trade": text(max(pnls) if pnls else None),
        "worst_trade": text(min(pnls) if pnls else None),
        "top3_profit_share": text(
            top3 / gross_profit * Decimal(100) if gross_profit > ZERO else None
        ),
        "realized_pnl_ex_top3": text(realized - top3 if pnls else None),
    }


def build_trade_records(
    run: dict[str, Any],
    manifest: dict[str, Any],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    """Reconstruct per-cycle trade records from the recorded ledger.

    Realized P&L comes from the recorded fills (average entry vs. average exit
    price times exited quantity; sign flipped for shorts). Fees are not
    deducted here: recorded runs carry zero fee assumptions, and slippage is
    already embedded in the fill prices.
    """
    fills = list(result.get("fills", []))
    cycles = list(result.get("cycles", []))
    sig_idx, int_idx = build_signal_indexes(result)
    versions = {
        str(x.get("name")): str(x.get("version"))
        for x in manifest.get("strategies", []) if isinstance(x, dict)
    }

    fills_by_symbol: dict[str, list[tuple[datetime, str, dict[str, Any]]]] = {}
    for fill in fills:
        at = _parse_time(fill.get("time"))
        if at is None:
            continue
        symbol = str(fill.get("symbol", "")).upper()
        fills_by_symbol.setdefault(symbol, []).append(
            (at, str(fill.get("side", "")).upper(), fill)
        )

    records: list[dict[str, Any]] = []
    for idx, cycle in enumerate(cycles):
        opened = _parse_time(cycle.get("opened_at"))
        closed = _parse_time(cycle.get("closed_at"))
        symbol = str(cycle.get("symbol", "UNKNOWN")).upper()
        cycle_id = str(cycle.get("cycle_id") or f"{run['run_id']}:{symbol}:{idx + 1}")

        related: list[dict[str, Any]] = []
        for at, side, fill in fills_by_symbol.get(symbol, []):
            legacy_match = bool(
                opened
                and (
                    (side in _ENTRY_SIDES and at >= opened and (closed is None or at < closed))
                    or (side in _EXIT_SIDES and at > opened and (closed is None or at <= closed))
                )
            )
            if fill.get("cycle_id") == cycle_id or (not fill.get("cycle_id") and legacy_match):
                related.append(fill)

        buys = [f for f in related if str(f.get("side")).upper() in _ENTRY_SIDES]
        sells = [f for f in related if str(f.get("side")).upper() in _EXIT_SIDES]
        direction = (
            "SHORT" if any(str(f.get("side")).upper() == "SELL_SHORT" for f in buys) else "LONG"
        )

        attr = reconcile_trade_attribution(cycle, buys, sells, sig_idx, int_idx, versions)

        bq = sum((_to_decimal(f.get("quantity")) or ZERO for f in buys), ZERO)
        sq = sum((_to_decimal(f.get("quantity")) or ZERO for f in sells), ZERO)

        def vwap(rows: list[dict[str, Any]], qty: Decimal) -> Decimal | None:
            if qty <= ZERO:
                return None
            return sum(
                (
                    (_to_decimal(f.get("quantity")) or ZERO)
                    * (_to_decimal(f.get("price")) or ZERO)
                    for f in rows
                ),
                ZERO,
            ) / qty

        bp = vwap(buys, bq)
        sp = vwap(sells, sq)
        bn = bp * bq if bp is not None else None
        sn = sp * sq if sp is not None else None

        realized = None
        if bp is not None and sp is not None and sq > ZERO:
            realized = (sp - bp) * sq if direction == "LONG" else (bp - sp) * sq
        if realized is None and closed is not None:
            realized = _to_decimal(cycle.get("realized_pnl"))
        basis = bp * sq if bp is not None else None
        realized_return = (
            realized / basis * Decimal("100")
            if realized is not None and basis is not None and basis > ZERO
            else None
        )

        records.append({
            "trade_id": cycle_id,
            "execution_mode": "BACKTEST",
            "account_id": f"BACKTEST:{run['selection_id']}",
            "research_run_id": run["selection_id"],
            "symbol": symbol,
            "cycle_id": cycle_id,
            "entry_timestamp": _to_iso(opened),
            "exit_timestamp": _to_iso(closed),
            "entry_price": _fmt(bp),
            "exit_price": _fmt(sp),
            "filled_quantity": str(bq),
            "exited_quantity": str(sq),
            "open_quantity": str(max(ZERO, bq - sq)),
            "entry_notional": _fmt(bn),
            "exit_notional": _fmt(sn),
            "realized_pnl": _fmt(realized),
            "realized_return_percent": _fmt(realized_return),
            "position_status": (
                "CLOSED" if closed else ("PARTIALLY_CLOSED" if sq > ZERO else "OPEN")
            ),
            "entry_strategy_id": attr.entry_strategy_id,
            "entry_strategy_version": attr.entry_strategy_version,
            "entry_signal_id": attr.entry_signal_id,
            "entry_signal_timestamp": attr.entry_signal_timestamp,
            "exit_strategy_id": attr.exit_strategy_id,
            "exit_strategy_version": attr.exit_strategy_version,
            "exit_signal_id": attr.exit_signal_id,
            "exit_signal_timestamp": attr.exit_signal_timestamp,
            "exit_reason": attr.exit_reason,
            "attribution_complete": attr.attribution_complete,
            "attribution_source": attr.attribution_source,
            "direction": direction,
        })

    return records


@dataclass(frozen=True, slots=True)
class ResearchRunQuery:
    """Filter and pagination criteria for research runs."""

    page: int = 1
    page_size: int = 50
    search: str | None = None
    symbol: str | None = None
    period_role: str | None = None
    buy_strategy: str | None = None
    sell_strategy: str | None = None
    short_strategy: str | None = None
    cover_strategy: str | None = None
    strategy: str | None = None
    stop_policy: str | None = None
    run_purpose: str | None = None
    status: str | None = None
    min_trades: int | None = None
    min_win_rate: float | None = None
    min_pnl: float | None = None
    min_return: float | None = None
    min_profit_factor: float | None = None
    max_drawdown: float | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    window_key: str | None = None
    execution_date: str | None = None
    sort_by: str = "execution_at"
    sort_order: str = "desc"


@dataclass(frozen=True, slots=True)
class ResearchRunQueryResult:
    """Paginated research run query result."""

    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int
    total_pages: int


#: Ordered (column, SQL type) pairs of the persisted index. Insert statements
#: are generated from this list so schema and writer cannot drift apart.
_INDEX_COLUMNS: tuple[tuple[str, str], ...] = (
    ("selection_id", "VARCHAR PRIMARY KEY"),
    ("run_id", "VARCHAR NOT NULL"),
    ("execution_id", "VARCHAR"),
    ("created_at", "TIMESTAMPTZ"),
    ("execution_at", "TIMESTAMPTZ"),
    ("completed_at", "TIMESTAMPTZ"),
    ("symbols", "VARCHAR[]"),
    ("period_role", "VARCHAR"),
    ("window_start", "TIMESTAMPTZ"),
    ("window_end", "TIMESTAMPTZ"),
    ("window_key", "VARCHAR"),
    ("dataset_id", "VARCHAR"),
    ("dataset_hash", "VARCHAR"),
    ("config_fingerprint", "VARCHAR"),
    ("artifact_version", "VARCHAR"),
    ("buy_strategies", "VARCHAR[]"),
    ("sell_strategies", "VARCHAR[]"),
    ("short_strategies", "VARCHAR[]"),
    ("cover_strategies", "VARCHAR[]"),
    ("stop_configuration_json", "TEXT"),
    ("stop_policy", "VARCHAR"),
    ("run_purpose", "VARCHAR"),
    ("artifact_group", "VARCHAR"),
    ("execution_mode", "VARCHAR"),
    ("status", "VARCHAR"),
    ("starting_capital", "VARCHAR"),
    ("final_equity", "VARCHAR"),
    ("net_pnl", "VARCHAR"),
    ("return_percent", "VARCHAR"),
    ("completed_trade_count", "INTEGER"),
    ("win_rate", "VARCHAR"),
    ("profit_factor", "VARCHAR"),
    ("max_drawdown", "VARCHAR"),
    ("data_source", "VARCHAR"),
    ("duplicate_artifact_count", "INTEGER"),
    ("execution_identity_uncertain", "BOOLEAN"),
    ("provenance_json", "TEXT"),
    ("locations", "VARCHAR[]"),
    ("manifest_path", "VARCHAR NOT NULL"),
    ("results_path", "VARCHAR NOT NULL"),
    ("result_hash", "VARCHAR NOT NULL"),
    ("net_pnl_num", "DOUBLE"),
    ("return_percent_num", "DOUBLE"),
    ("win_rate_num", "DOUBLE"),
    ("max_drawdown_num", "DOUBLE"),
    ("profit_factor_num", "DOUBLE"),
    ("expectancy_num", "DOUBLE"),
    ("top3_profit_share_num", "DOUBLE"),
    ("best_trade_num", "DOUBLE"),
    ("manifest_mtime", "DOUBLE NOT NULL"),
    ("results_mtime", "DOUBLE NOT NULL"),
    ("manifest_size", "BIGINT NOT NULL"),
    ("results_size", "BIGINT NOT NULL"),
    ("indexed_at", "TIMESTAMPTZ NOT NULL"),
    ("payload_json", "TEXT NOT NULL"),
)

_SORT_COLUMNS = {
    "execution_at": "execution_at",
    "created_at": "created_at",
    "net_pnl": "net_pnl_num",
    "return_percent": "return_percent_num",
    "win_rate": "win_rate_num",
    "profit_factor": "profit_factor_num",
    "max_drawdown": "max_drawdown_num",
    "trade_count": "completed_trade_count",
    "expectancy": "expectancy_num",
    "top3_share": "top3_profit_share_num",
    "symbols": "array_to_string(symbols, ' ')",
}

#: Aggregation dimensions. List dimensions are unnested (a run appears once in
#: every group it belongs to); scalar dimensions use the column directly.
_LIST_DIMENSIONS = {
    "buy_strategy": "buy_strategies",
    "sell_strategy": "sell_strategies",
    "short_strategy": "short_strategies",
    "cover_strategy": "cover_strategies",
    "symbol": "symbols",
}
_SCALAR_DIMENSIONS = {
    "stop_policy": "stop_policy",
    "period_role": "period_role",
    "run_purpose": "run_purpose",
    "status": "status",
    "window": "window_key",
    "artifact_group": "artifact_group",
}
AGGREGATE_DIMENSIONS = tuple(_LIST_DIMENSIONS) + tuple(_SCALAR_DIMENSIONS)

_FACET_KEYS = {
    "buy_strategy": "buy_strategies",
    "sell_strategy": "sell_strategies",
    "short_strategy": "short_strategies",
    "cover_strategy": "cover_strategies",
    "symbol": "symbols",
    "stop_policy": "stop_policies",
    "period_role": "period_roles",
    "run_purpose": "run_purposes",
    "status": "statuses",
    "window": "windows",
    "artifact_group": "artifact_groups",
}

_AGGREGATE_SORTS = {
    "run_count": "run_count",
    "trade_count": "trade_count",
    "median_return": "median_return",
    "mean_return": "mean_return",
    "profitable_rate": "profitable_run_rate",
    "median_profit_factor": "median_profit_factor",
    "median_drawdown": "median_drawdown",
    "worst_drawdown": "worst_drawdown",
}


class ResearchRunCatalog:
    """Persistent, thread-safe DuckDB index of historical backtest runs.

    Supports incremental updates, duplicate detection, deterministic sorting,
    and single-digit millisecond server-side pagination without full-file SHA256
    recalculation during polling.
    """

    def __init__(
        self,
        research_root: str | Path,
        catalog_path: str | Path | None = None,
    ) -> None:
        self.research_root = Path(research_root).resolve()
        if catalog_path is None or str(catalog_path) == ":memory:":
            self.catalog_path = Path(":memory:")
        else:
            self.catalog_path = Path(catalog_path).resolve()
        self._lock = threading.Lock()
        self._con: duckdb.DuckDBPyConnection | None = None
        self._last_sync_time: float = 0.0
        self._trade_cache: OrderedDict[
            tuple[str, float, float], list[dict[str, Any]]
        ] = OrderedDict()
        self._init_database()

    def _get_connection(self) -> duckdb.DuckDBPyConnection:
        if str(self.catalog_path) == ":memory:":
            if self._con is None:
                self._con = duckdb.connect(":memory:")
            return self._con
        return duckdb.connect(str(self.catalog_path))

    def _release(self, con: duckdb.DuckDBPyConnection) -> None:
        if str(self.catalog_path) != ":memory:":
            con.close()

    def _init_database(self) -> None:
        """Create the DuckDB tables if they do not exist; rebuild a stale index."""
        with self._lock:
            if str(self.catalog_path) != ":memory:":
                self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
            con = self._get_connection()
            try:
                con.execute("""
                    CREATE TABLE IF NOT EXISTS artifact_files_meta (
                        manifest_path VARCHAR PRIMARY KEY,
                        results_path VARCHAR NOT NULL,
                        run_id VARCHAR NOT NULL,
                        execution_id VARCHAR,
                        result_hash VARCHAR NOT NULL,
                        manifest_mtime DOUBLE NOT NULL,
                        results_mtime DOUBLE NOT NULL,
                        manifest_size BIGINT NOT NULL,
                        results_size BIGINT NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL
                    );
                """)
                con.execute(
                    "CREATE TABLE IF NOT EXISTS catalog_meta "
                    "(key VARCHAR PRIMARY KEY, value VARCHAR)"
                )
                row = con.execute(
                    "SELECT value FROM catalog_meta WHERE key = 'index_schema_version'"
                ).fetchone()
                if row is None or row[0] != str(INDEX_SCHEMA_VERSION):
                    # Derived metrics changed: the index is a pure cache of the
                    # immutable artifacts, so drop it and let sync() rebuild it.
                    con.execute("DROP TABLE IF EXISTS research_runs_index")
                    con.execute(
                        "INSERT OR REPLACE INTO catalog_meta VALUES ('index_schema_version', ?)",
                        [str(INDEX_SCHEMA_VERSION)],
                    )
                columns = ",\n".join(f"{name} {sql_type}" for name, sql_type in _INDEX_COLUMNS)
                con.execute(f"CREATE TABLE IF NOT EXISTS research_runs_index ({columns})")
            finally:
                self._release(con)

    def sync(self, force: bool = False) -> int:
        """Incrementally crawl research_root and update the DuckDB index.

        Returns:
            The number of runs indexed.
        """
        if not self.research_root.is_dir():
            return 0

        with self._lock:
            con = self._get_connection()
            try:
                # Load existing artifact file metadata
                cached_files: dict[str, tuple[float, int, float, int, str, str | None, str]] = {}
                if not force:
                    rows = con.execute("""
                        SELECT manifest_path, manifest_mtime, manifest_size,
                               results_mtime, results_size,
                               run_id, execution_id, result_hash
                        FROM artifact_files_meta
                    """).fetchall()
                    for r in rows:
                        cached_files[r[0]] = (r[1], r[2], r[3], r[4], r[5], r[6], r[7])

                # Discover pairs
                discovered_pairs: list[tuple[Path, Path, os.stat_result, os.stat_result]] = []
                for manifest_path in sorted(self.research_root.rglob("*.manifest.json")):
                    res_path = manifest_path.with_name(
                        manifest_path.name.replace(".manifest.json", ".results.json")
                    )
                    if not res_path.is_file():
                        continue
                    try:
                        m_stat = manifest_path.stat()
                        r_stat = res_path.stat()
                        discovered_pairs.append((manifest_path, res_path, m_stat, r_stat))
                    except OSError:
                        continue

                # Parse or reuse metadata for each pair
                groups: dict[tuple[str, str], list[dict[str, Any]]] = {}

                for m_path, r_path, m_stat, r_stat in discovered_pairs:
                    m_str = m_path.as_posix()
                    cached = cached_files.get(m_str)

                    if (
                        cached
                        and cached[0] == m_stat.st_mtime
                        and cached[1] == m_stat.st_size
                        and cached[2] == r_stat.st_mtime
                        and cached[3] == r_stat.st_size
                    ):
                        run_id = cached[4]
                        execution_id = cached[5]
                        result_hash = cached[6]
                    else:
                        # File changed or new: parse JSON and calculate content hash once
                        try:
                            m_raw = json.loads(m_path.read_text(encoding="utf-8"))
                            r_bytes = r_path.read_bytes()
                            run_id = str(m_raw.get("run_id", ""))
                            if not run_id:
                                continue
                            execution_id = m_raw.get("execution_id")
                            result_hash = hashlib.sha256(r_bytes).hexdigest()

                            con.execute("""
                                INSERT OR REPLACE INTO artifact_files_meta
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, [
                                m_str, r_path.as_posix(), run_id,
                                str(execution_id) if execution_id else None,
                                result_hash, m_stat.st_mtime, r_stat.st_mtime,
                                m_stat.st_size, r_stat.st_size, datetime.now(UTC),
                            ])
                        except (OSError, ValueError, KeyError):
                            continue

                    key = (
                        str(execution_id or run_id),
                        "execution" if execution_id else result_hash,
                    )
                    groups.setdefault(key, []).append({
                        "manifest_path": m_path,
                        "results_path": r_path,
                        "run_id": run_id,
                        "execution_id": str(execution_id) if execution_id else None,
                        "result_hash": result_hash,
                        "manifest_mtime": m_stat.st_mtime,
                        "results_mtime": r_stat.st_mtime,
                        "manifest_size": m_stat.st_size,
                        "results_size": r_stat.st_size,
                    })

                # Now process distinct variant groups into research_runs_index
                con.execute("DELETE FROM research_runs_index")

                insert_sql = (
                    f"INSERT INTO research_runs_index ({', '.join(n for n, _ in _INDEX_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _INDEX_COLUMNS)})"
                )
                indexed_count = 0
                for (_, _variant), copies in groups.items():
                    copies.sort(key=lambda item: item["manifest_path"].as_posix())
                    chosen = copies[0]
                    run_id = chosen["run_id"]
                    execution_id = chosen["execution_id"]

                    groups_for_run = [key for key in groups if key[0] == run_id]
                    if execution_id:
                        selection_id = execution_id
                    elif len(groups_for_run) == 1:
                        selection_id = run_id
                    else:
                        selection_id = f"legacy-{run_id}-{chosen['result_hash'][:16]}"

                    locations = [
                        str(item["manifest_path"].parent.relative_to(self.research_root))
                        for item in copies
                    ]

                    # Parse chosen artifact to build summary record
                    try:
                        manifest = json.loads(chosen["manifest_path"].read_text(encoding="utf-8"))
                        result = json.loads(chosen["results_path"].read_text(encoding="utf-8"))
                    except (OSError, ValueError, KeyError):
                        continue

                    # Strategy roles recovery
                    buys, sells, shorts, covers = recover_run_strategies(manifest)
                    stop_cfg = recover_stop_configuration(manifest)
                    stop_label = stop_policy_label(stop_cfg)

                    # Performance economics calculation
                    starting = _to_decimal(manifest.get("starting_capital"))
                    curve = result.get("equity_curve", [])
                    final = _to_decimal(curve[-1].get("equity")) if curve else None

                    # Trade metrics: derived from the recorded fills ledger so legacy
                    # artifacts without per-cycle realized_pnl still get real statistics.
                    records = build_trade_records(
                        {"run_id": run_id, "selection_id": selection_id}, manifest, result
                    )
                    completed_trade_count = sum(
                        1 for r in records if r["position_status"] == "CLOSED"
                    )
                    pnls = [
                        Decimal(r["realized_pnl"])
                        for r in records
                        if r["position_status"] == "CLOSED" and r["realized_pnl"] is not None
                    ]
                    stats = compute_trade_stats(pnls)
                    pnl_sum = Decimal(stats["realized_pnl"])

                    if final is None and starting is not None:
                        final = starting + pnl_sum
                    net = (
                        (final - starting)
                        if (final is not None and starting is not None)
                        else pnl_sum
                    )

                    ret_pct = (
                        (net / starting * Decimal(100))
                        if (starting and starting > ZERO)
                        else None
                    )
                    win_rate = (
                        (Decimal(stats["winning_trades"]) / Decimal(len(pnls)) * Decimal(100))
                        if pnls
                        else None
                    )

                    # Peak drawdown
                    peak = starting or ZERO
                    drawdown = ZERO
                    for pt in curve:
                        eq = _to_decimal(pt.get("equity"))
                        if eq is None:
                            continue
                        peak = max(peak, eq)
                        if peak > ZERO:
                            drawdown = max(drawdown, (peak - eq) / peak)

                    max_dd_pct = drawdown * Decimal(100)

                    gross_loss = Decimal(stats["gross_loss"])
                    profit_factor = (
                        Decimal(stats["gross_profit"]) / gross_loss if gross_loss > ZERO else None
                    )

                    folder_labels = [Path(loc).parts[0] for loc in locations if Path(loc).parts]
                    known_groups = {
                        "acceptance-once", "parallel-bench", "reproduction",
                        "r730-validation", "final-six", "initial-six", "results",
                    }
                    group = next((g for g in folder_labels if g in known_groups), None)
                    if group is None:
                        group = next((g for g in folder_labels if g.startswith("r730-")), None)

                    # Extract execution time: started_at only (NEVER window_start)
                    started_at = _parse_time(manifest.get("started_at"))
                    created_at = _parse_time(manifest.get("created_at"))
                    completed_at = _parse_time(manifest.get("completed_at"))
                    win_start = _parse_time(manifest.get("window_start"))
                    win_end = _parse_time(manifest.get("window_end"))
                    window_key = (
                        f"{win_start.date().isoformat()}..{win_end.date().isoformat()}"
                        if win_start and win_end
                        else None
                    )

                    symbols = [str(s) for s in manifest.get("symbols", [])]
                    period_role = manifest.get("period_role")
                    purpose = manifest.get("run_purpose")
                    status = str(manifest.get("completion_status") or "COMPLETED")
                    source = (manifest.get("data_provenance") or {}).get("source")

                    run_row = {
                        "selection_id": selection_id,
                        "execution_id": chosen["execution_id"],
                        "experiment_id": run_id,
                        "run_id": run_id,
                        "created_at": _to_iso(created_at),
                        "execution_at": _to_iso(started_at),
                        "completed_at": _to_iso(completed_at),
                        "symbols": symbols,
                        "period_role": period_role,
                        "window_start": _to_iso(win_start),
                        "window_end": _to_iso(win_end),
                        "dataset_id": manifest.get("data_revision") or manifest.get("data_vintage"),
                        "dataset_hash": manifest.get("dataset_hash"),
                        "config_fingerprint": manifest.get("config_hash"),
                        "artifact_version": result.get("schema_version"),
                        "buy_strategies": buys,
                        "sell_strategies": sells,
                        "short_strategies": shorts,
                        "cover_strategies": covers,
                        "stop_configuration": stop_cfg,
                        "stop_policy": stop_label,
                        "run_purpose": purpose,
                        "artifact_group": group,
                        "execution_mode": "BACKTEST",
                        "status": status,
                        "starting_capital": str(starting) if starting is not None else None,
                        "final_equity": str(final) if final is not None else None,
                        "net_pnl": _fmt(net),
                        "return_percent": _fmt(ret_pct),
                        "completed_trade_count": completed_trade_count,
                        "win_rate": _fmt(win_rate),
                        "profit_factor": _fmt(profit_factor),
                        "max_drawdown": _fmt(max_dd_pct),
                        "trade_stats": stats,
                        "data_source": source,
                        "duplicate_artifact_count": len(copies),
                        "execution_identity_uncertain": chosen["execution_id"] is None,
                        "provenance": dict(manifest.get("data_provenance") or {}),
                        "artifact_locations": locations,
                    }

                    values: dict[str, Any] = {
                        "selection_id": selection_id,
                        "run_id": run_id,
                        "execution_id": chosen["execution_id"],
                        "created_at": started_at or created_at,
                        "execution_at": started_at,
                        "completed_at": completed_at,
                        "symbols": symbols,
                        "period_role": period_role,
                        "window_start": win_start,
                        "window_end": win_end,
                        "window_key": window_key,
                        "dataset_id": run_row["dataset_id"],
                        "dataset_hash": run_row["dataset_hash"],
                        "config_fingerprint": run_row["config_fingerprint"],
                        "artifact_version": run_row["artifact_version"],
                        "buy_strategies": buys,
                        "sell_strategies": sells,
                        "short_strategies": shorts,
                        "cover_strategies": covers,
                        "stop_configuration_json": json.dumps(stop_cfg, sort_keys=True),
                        "stop_policy": stop_label,
                        "run_purpose": purpose,
                        "artifact_group": group,
                        "execution_mode": "BACKTEST",
                        "status": status,
                        "starting_capital": run_row["starting_capital"],
                        "final_equity": run_row["final_equity"],
                        "net_pnl": run_row["net_pnl"],
                        "return_percent": run_row["return_percent"],
                        "completed_trade_count": completed_trade_count,
                        "win_rate": run_row["win_rate"],
                        "profit_factor": run_row["profit_factor"],
                        "max_drawdown": run_row["max_drawdown"],
                        "data_source": source,
                        "duplicate_artifact_count": len(copies),
                        "execution_identity_uncertain": chosen["execution_id"] is None,
                        "provenance_json": json.dumps(run_row["provenance"], sort_keys=True),
                        "locations": locations,
                        "manifest_path": chosen["manifest_path"].as_posix(),
                        "results_path": chosen["results_path"].as_posix(),
                        "result_hash": chosen["result_hash"],
                        "net_pnl_num": _to_float(net),
                        "return_percent_num": _to_float(ret_pct),
                        "win_rate_num": _to_float(win_rate),
                        "max_drawdown_num": _to_float(max_dd_pct),
                        "profit_factor_num": _to_float(profit_factor),
                        "expectancy_num": _to_float(stats["expectancy"]),
                        "top3_profit_share_num": _to_float(stats["top3_profit_share"]),
                        "best_trade_num": _to_float(stats["best_trade"]),
                        "manifest_mtime": chosen["manifest_mtime"],
                        "results_mtime": chosen["results_mtime"],
                        "manifest_size": chosen["manifest_size"],
                        "results_size": chosen["results_size"],
                        "indexed_at": datetime.now(UTC),
                        "payload_json": json.dumps(run_row, sort_keys=True),
                    }
                    con.execute(insert_sql, [values[name] for name, _ in _INDEX_COLUMNS])
                    indexed_count += 1

                return indexed_count
            finally:
                self._last_sync_time = time.monotonic()
                self._release(con)

    # ------------------------------------------------------------------ querying

    def refresh(self, min_interval_seconds: float = 5.0) -> int:
        """Explicit throttled incremental refresh to discover newly written research runs."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_sync_time < min_interval_seconds:
                return 0
        return self.sync()

    def _ensure_indexed(self) -> None:
        row_count = 0
        with self._lock:
            con = self._get_connection()
            try:
                row_count = con.execute("SELECT count(*) FROM research_runs_index").fetchone()[0]
            finally:
                self._release(con)
        if row_count == 0:
            self.sync()
        else:
            now = time.monotonic()
            if now - self._last_sync_time > 10.0:
                self.sync()

    @staticmethod
    def _build_where(query: ResearchRunQuery) -> tuple[str, list[Any]]:
        where_clauses: list[str] = []
        params: list[Any] = []

        if query.symbol:
            where_clauses.append("list_contains(symbols, ?)")
            params.append(query.symbol.strip().upper())

        if query.period_role:
            where_clauses.append("period_role = ?")
            params.append(query.period_role.strip().upper())

        for field, column in (
            ("buy_strategy", "buy_strategies"),
            ("sell_strategy", "sell_strategies"),
            ("short_strategy", "short_strategies"),
            ("cover_strategy", "cover_strategies"),
        ):
            value = getattr(query, field)
            if value:
                where_clauses.append(f"list_contains({column}, ?)")
                params.append(value.strip())

        if query.strategy:
            strat = query.strategy.strip()
            where_clauses.append(
                "(list_contains(buy_strategies, ?) "
                "OR list_contains(sell_strategies, ?) "
                "OR list_contains(short_strategies, ?) "
                "OR list_contains(cover_strategies, ?))"
            )
            params.extend([strat, strat, strat, strat])

        if query.run_purpose:
            where_clauses.append("run_purpose = ?")
            params.append(query.run_purpose.strip())

        if query.stop_policy:
            value = query.stop_policy.strip()
            where_clauses.append("(stop_policy = ? OR lower(stop_configuration_json) LIKE ?)")
            params.extend([value, f"%{value.lower()}%"])

        if query.status:
            where_clauses.append("status = ?")
            params.append(query.status.strip())

        if query.min_trades is not None:
            where_clauses.append("completed_trade_count >= ?")
            params.append(query.min_trades)

        if query.min_win_rate is not None:
            where_clauses.append("win_rate_num >= ?")
            params.append(float(query.min_win_rate))

        if query.min_pnl is not None:
            where_clauses.append("net_pnl_num >= ?")
            params.append(float(query.min_pnl))

        if query.min_return is not None:
            where_clauses.append("return_percent_num >= ?")
            params.append(float(query.min_return))

        if query.min_profit_factor is not None:
            where_clauses.append("profit_factor_num >= ?")
            params.append(float(query.min_profit_factor))

        if query.max_drawdown is not None:
            where_clauses.append("max_drawdown_num <= ?")
            params.append(float(query.max_drawdown))

        if query.window_start is not None:
            where_clauses.append("window_start >= ?")
            params.append(query.window_start.astimezone(UTC))

        if query.window_end is not None:
            where_clauses.append("window_end <= ?")
            params.append(query.window_end.astimezone(UTC))

        if query.window_key:
            where_clauses.append("window_key = ?")
            params.append(query.window_key.strip())

        if query.execution_date:
            where_clauses.append("CAST(execution_at AS DATE) = CAST(? AS DATE)")
            params.append(query.execution_date.strip())

        if query.search:
            search_term = f"%{query.search.strip().lower()}%"
            where_clauses.append("""(
                lower(run_id) LIKE ? OR
                lower(selection_id) LIKE ? OR
                lower(coalesce(run_purpose, '')) LIKE ? OR
                lower(coalesce(stop_policy, '')) LIKE ? OR
                lower(array_to_string(symbols, ' ')) LIKE ? OR
                lower(array_to_string(buy_strategies, ' ')) LIKE ? OR
                lower(array_to_string(sell_strategies, ' ')) LIKE ? OR
                lower(array_to_string(short_strategies, ' ')) LIKE ? OR
                lower(array_to_string(cover_strategies, ' ')) LIKE ?
            )""")
            params.extend([search_term] * 9)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        return where_sql, params

    def query_runs(self, query: ResearchRunQuery) -> ResearchRunQueryResult:
        """Query research runs with server-side filtering, sorting, and pagination."""
        self._ensure_indexed()

        with self._lock:
            con = self._get_connection()
            try:
                where_sql, params = self._build_where(query)
                count_query = f"SELECT count(*) FROM research_runs_index {where_sql}"
                total = con.execute(count_query, params).fetchone()[0]

                order_col = _SORT_COLUMNS.get(query.sort_by, "execution_at")
                order_dir = "DESC" if query.sort_order.lower() == "desc" else "ASC"

                limit = max(1, min(500, query.page_size))
                offset = max(0, (query.page - 1) * limit)

                data_query = f"""
                    SELECT payload_json FROM research_runs_index
                    {where_sql}
                    ORDER BY {order_col} {order_dir} NULLS LAST, selection_id DESC
                    LIMIT ? OFFSET ?
                """
                rows = con.execute(data_query, params + [limit, offset]).fetchall()
                items = [json.loads(r[0]) for r in rows]

                total_pages = (total + limit - 1) // limit if total > 0 else 1

                return ResearchRunQueryResult(
                    items=items,
                    total=total,
                    page=query.page,
                    page_size=limit,
                    total_pages=total_pages,
                )
            finally:
                self._release(con)

    def run_counts_by_symbol(self) -> tuple[dict[str, int], dict[str, str]]:
        """Return ``(run_count, latest_selection_id)`` per symbol across the full index."""
        self._ensure_indexed()
        with self._lock:
            con = self._get_connection()
            try:
                rows = con.execute("""
                    SELECT sym, count(*) AS cnt,
                           arg_max(selection_id, coalesce(execution_at, created_at)) AS latest
                    FROM research_runs_index, UNNEST(symbols) AS expanded(sym)
                    GROUP BY sym
                """).fetchall()
            finally:
                self._release(con)
        return (
            {str(r[0]).upper(): int(r[1]) for r in rows},
            {str(r[0]).upper(): str(r[2]) for r in rows},
        )

    def get_facets(self) -> dict[str, Any]:
        """Distinct filter values with run counts over the *entire* catalog."""
        self._ensure_indexed()

        def option_rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
            return [
                {"value": str(value), "count": int(count)}
                for value, count in con.execute(sql).fetchall()
                if value is not None and str(value) != ""
            ]

        with self._lock:
            con = self._get_connection()
            try:
                facets: dict[str, Any] = {}
                for name, column in _LIST_DIMENSIONS.items():
                    facets[_FACET_KEYS[name]] = option_rows(
                        con,
                        f"""SELECT v, count(*) FROM research_runs_index, UNNEST({column}) AS e(v)
                            GROUP BY v ORDER BY count(*) DESC, v""",
                    )
                for name, column in _SCALAR_DIMENSIONS.items():
                    facets[_FACET_KEYS[name]] = option_rows(
                        con,
                        f"""SELECT {column}, count(*) FROM research_runs_index
                            GROUP BY {column} ORDER BY count(*) DESC, {column}""",
                    )
                total, unrecorded_stop, first_window, last_window = con.execute("""
                    SELECT count(*), count(*) FILTER (WHERE stop_policy IS NULL),
                           min(window_start), max(window_end)
                    FROM research_runs_index
                """).fetchone()
                bounds = con.execute("""
                    SELECT min(return_percent_num), max(return_percent_num),
                           max(max_drawdown_num), max(completed_trade_count)
                    FROM research_runs_index
                """).fetchone()
            finally:
                self._release(con)

        facets.update({
            "total_runs": int(total),
            "runs_without_stop_policy": int(unrecorded_stop),
            "earliest_window_start": _to_iso(first_window),
            "latest_window_end": _to_iso(last_window),
            "bounds": {
                "min_return_percent": bounds[0],
                "max_return_percent": bounds[1],
                "max_drawdown_percent": bounds[2],
                "max_trade_count": bounds[3],
            },
        })
        return facets

    def aggregate_runs(
        self,
        query: ResearchRunQuery,
        group_by: str,
        *,
        sort_by: str = "median_return",
        sort_order: str = "desc",
        min_runs: int = 1,
        limit: int = 100,
        include_multi_symbol: bool = False,
    ) -> dict[str, Any]:
        """Group the filtered runs and summarize each group's *run-level* metrics.

        Every statistic is computed across independent runs, each measured
        against its own starting capital. Capital, P&L and returns are
        deliberately never summed; only trade counts (which are additive) are.
        """
        if group_by not in AGGREGATE_DIMENSIONS:
            raise ValueError(f"unsupported group_by: {group_by}")
        self._ensure_indexed()

        where_sql, params = self._build_where(query)
        extra: list[str] = []
        if group_by in _LIST_DIMENSIONS:
            column = _LIST_DIMENSIONS[group_by]
            source = f"research_runs_index, UNNEST({column}) AS g(grp)"
            if group_by == "symbol" and not include_multi_symbol:
                extra.append("len(symbols) = 1")
        else:
            source = "research_runs_index"
        key_expr = "grp" if group_by in _LIST_DIMENSIONS else _SCALAR_DIMENSIONS[group_by]

        clauses = [where_sql[len("WHERE "):]] if where_sql else []
        clauses.extend(extra)
        full_where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        order_col = _AGGREGATE_SORTS.get(sort_by, "median_return")
        order_dir = "DESC" if sort_order.lower() == "desc" else "ASC"
        limit = max(1, min(500, limit))

        sql = f"""
            SELECT coalesce(CAST({key_expr} AS VARCHAR), 'UNRECORDED') AS grp_key,
                   count(*) AS run_count,
                   coalesce(sum(completed_trade_count), 0) AS trade_count,
                   count(*) FILTER (WHERE net_pnl_num > 0) AS profitable_runs,
                   100.0 * count(*) FILTER (WHERE net_pnl_num > 0) / count(*)
                       AS profitable_run_rate,
                   min(return_percent_num) AS min_return,
                   quantile_cont(return_percent_num, 0.25) AS p25_return,
                   median(return_percent_num) AS median_return,
                   quantile_cont(return_percent_num, 0.75) AS p75_return,
                   max(return_percent_num) AS max_return,
                   avg(return_percent_num) AS mean_return,
                   median(profit_factor_num) AS median_profit_factor,
                   count(profit_factor_num) AS profit_factor_runs,
                   median(win_rate_num) AS median_win_rate,
                   median(max_drawdown_num) AS median_drawdown,
                   max(max_drawdown_num) AS worst_drawdown,
                   median(expectancy_num) AS median_expectancy,
                   median(top3_profit_share_num) AS median_top3_share,
                   max(top3_profit_share_num) AS max_top3_share,
                   count(DISTINCT array_to_string(symbols, ',')) AS distinct_symbol_sets,
                   arg_max(selection_id, return_percent_num) AS best_run_id,
                   arg_min(selection_id, return_percent_num) AS worst_run_id,
                   min(window_start) AS first_window_start,
                   max(window_end) AS last_window_end,
                   count(DISTINCT window_key) AS distinct_windows,
                   count(DISTINCT concat_ws('|', array_to_string(symbols, ','), net_pnl,
                         completed_trade_count, window_key)) AS distinct_outcomes
            FROM {source}
            {full_where}
            GROUP BY grp_key
            HAVING count(*) >= ?
            ORDER BY {order_col} {order_dir} NULLS LAST, grp_key
            LIMIT ?
        """
        with self._lock:
            con = self._get_connection()
            try:
                cursor = con.execute(sql, [*params, max(1, min_runs), limit])
                names = [d[0] for d in cursor.description]
                rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
                considered = con.execute(
                    f"SELECT count(*) FROM research_runs_index {where_sql}", params
                ).fetchone()[0]
                multi_symbol_excluded = 0
                if group_by == "symbol" and not include_multi_symbol:
                    multi_symbol_excluded = con.execute(
                        f"""SELECT count(*) FROM research_runs_index
                            {where_sql + (' AND ' if where_sql else 'WHERE ')} len(symbols) > 1""",
                        params,
                    ).fetchone()[0]
            finally:
                self._release(con)

        groups = []
        for row in rows:
            row["first_window_start"] = _to_iso(row["first_window_start"])
            row["last_window_end"] = _to_iso(row["last_window_end"])
            row["key"] = row.pop("grp_key")
            groups.append(row)
        return {
            "group_by": group_by,
            "runs_considered": int(considered),
            "multi_symbol_runs_excluded": int(multi_symbol_excluded),
            "groups": groups,
        }

    def get_run(self, identifier: str) -> dict[str, Any] | None:
        """Find a single run by selection_id, execution_id, or run_id."""
        self._ensure_indexed()
        with self._lock:
            con = self._get_connection()
            try:
                row = con.execute("""
                    SELECT payload_json, manifest_path, results_path
                    FROM research_runs_index
                    WHERE selection_id = ? OR execution_id = ? OR run_id = ?
                    ORDER BY (selection_id = ?) DESC
                    LIMIT 1
                """, [identifier, identifier, identifier, identifier]).fetchone()
                if not row:
                    return None
                data = json.loads(row[0])
                data["_manifest_path"] = row[1]
                data["_results_path"] = row[2]
                return data
            finally:
                self._release(con)

    def get_trades_for_run(self, identifier: str) -> list[dict[str, Any]]:
        """Return all trades for a run with recovered strategy attribution.

        Records are cached per artifact (path + mtimes) because journal paging
        and sorting would otherwise re-derive them from the ledger on every
        request. Callers receive a shallow copy and must not mutate the rows.
        """
        run = self.get_run(identifier)
        if not run:
            raise LookupError(identifier)

        manifest_path = Path(run["_manifest_path"])
        results_path = Path(run["_results_path"])
        try:
            cache_key = (
                run["selection_id"],
                manifest_path.stat().st_mtime,
                results_path.stat().st_mtime,
            )
        except OSError:
            cache_key = None
        if cache_key is not None:
            with self._lock:
                cached = self._trade_cache.get(cache_key)
                if cached is not None:
                    self._trade_cache.move_to_end(cache_key)
                    return list(cached)

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        result = json.loads(results_path.read_text(encoding="utf-8"))
        records = build_trade_records(run, manifest, result)

        if cache_key is not None:
            with self._lock:
                self._trade_cache[cache_key] = records
                while len(self._trade_cache) > 8:
                    self._trade_cache.popitem(last=False)
        return list(records)
