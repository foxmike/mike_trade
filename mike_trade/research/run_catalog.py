"""DuckDB persistent research run catalog with incremental indexing and fast SQL search."""

from __future__ import annotations

import hashlib
import json
import os
import threading
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
        return dt.astimezone(UTC)
    except (ValueError, TypeError):
        return None


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
    max_drawdown: float | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
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
        self._init_database()

    def _get_connection(self) -> duckdb.DuckDBPyConnection:
        if str(self.catalog_path) == ":memory:":
            if self._con is None:
                self._con = duckdb.connect(":memory:")
            return self._con
        return duckdb.connect(str(self.catalog_path))

    def _init_database(self) -> None:
        """Create the DuckDB tables if they do not exist."""
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
                con.execute("""
                    CREATE TABLE IF NOT EXISTS research_runs_index (
                        selection_id VARCHAR PRIMARY KEY,
                        run_id VARCHAR NOT NULL,
                        execution_id VARCHAR,
                        created_at TIMESTAMPTZ,
                        execution_at TIMESTAMPTZ,
                        completed_at TIMESTAMPTZ,
                        symbols VARCHAR[],
                        period_role VARCHAR,
                        window_start TIMESTAMPTZ,
                        window_end TIMESTAMPTZ,
                        dataset_id VARCHAR,
                        dataset_hash VARCHAR,
                        config_fingerprint VARCHAR,
                        artifact_version VARCHAR,
                        buy_strategies VARCHAR[],
                        sell_strategies VARCHAR[],
                        short_strategies VARCHAR[],
                        cover_strategies VARCHAR[],
                        stop_configuration_json TEXT,
                        run_purpose VARCHAR,
                        artifact_group VARCHAR,
                        execution_mode VARCHAR,
                        status VARCHAR,
                        starting_capital VARCHAR,
                        final_equity VARCHAR,
                        net_pnl VARCHAR,
                        return_percent VARCHAR,
                        completed_trade_count INTEGER,
                        win_rate VARCHAR,
                        profit_factor VARCHAR,
                        max_drawdown VARCHAR,
                        data_source VARCHAR,
                        duplicate_artifact_count INTEGER,
                        execution_identity_uncertain BOOLEAN,
                        provenance_json TEXT,
                        locations VARCHAR[],
                        manifest_path VARCHAR NOT NULL,
                        results_path VARCHAR NOT NULL,
                        result_hash VARCHAR NOT NULL,
                        net_pnl_num DOUBLE,
                        return_percent_num DOUBLE,
                        win_rate_num DOUBLE,
                        max_drawdown_num DOUBLE,
                        profit_factor_num DOUBLE,
                        manifest_mtime DOUBLE NOT NULL,
                        results_mtime DOUBLE NOT NULL,
                        manifest_size BIGINT NOT NULL,
                        results_size BIGINT NOT NULL,
                        indexed_at TIMESTAMPTZ NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                """)
            finally:
                if str(self.catalog_path) != ":memory:":
                    con.close()

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

                    # Performance economics calculation
                    starting = _to_decimal(manifest.get("starting_capital"))
                    curve = result.get("equity_curve", [])
                    final = _to_decimal(curve[-1].get("equity")) if curve else None

                    # Trade metrics
                    cycles = result.get("cycles", [])
                    closed_cycles = [c for c in cycles if c.get("closed_at")]
                    completed_trade_count = len(closed_cycles)

                    # Calculate win_rate, pnl, drawdown from cycles & equity curve
                    wins = 0
                    pnl_sum = ZERO
                    for c in cycles:
                        c_pnl = _to_decimal(c.get("realized_pnl"))
                        if c_pnl is not None:
                            pnl_sum += c_pnl
                            if c_pnl > ZERO:
                                wins += 1

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
                        (Decimal(wins) / Decimal(completed_trade_count) * Decimal(100))
                        if completed_trade_count > 0
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

                    # Profit factor
                    pnls = [
                        _to_decimal(c.get("realized_pnl"))
                        for c in cycles
                        if c.get("realized_pnl") is not None
                    ]
                    pnls = [x for x in pnls if x is not None]
                    pos = sum((x for x in pnls if x > ZERO), ZERO)
                    neg = -sum((x for x in pnls if x < ZERO), ZERO)
                    profit_factor = (pos / neg) if neg > ZERO else None

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
                        "run_purpose": purpose,
                        "artifact_group": group,
                        "execution_mode": "BACKTEST",
                        "status": status,
                        "starting_capital": str(starting) if starting is not None else None,
                        "final_equity": str(final) if final is not None else None,
                        "net_pnl": str(net) if net is not None else None,
                        "return_percent": str(ret_pct) if ret_pct is not None else None,
                        "completed_trade_count": completed_trade_count,
                        "win_rate": str(win_rate) if win_rate is not None else None,
                        "profit_factor": str(profit_factor) if profit_factor is not None else None,
                        "max_drawdown": str(max_dd_pct),
                        "data_source": source,
                        "duplicate_artifact_count": len(copies),
                        "execution_identity_uncertain": chosen["execution_id"] is None,
                        "provenance": dict(manifest.get("data_provenance") or {}),
                        "artifact_locations": locations,
                    }

                    placeholders = ", ".join(["?"] * 50)
                    con.execute(f"""
                        INSERT INTO research_runs_index VALUES ({placeholders})
                    """, [
                        selection_id,
                        run_id,
                        chosen["execution_id"],
                        started_at or created_at,
                        started_at,
                        completed_at,
                        symbols,
                        period_role,
                        win_start,
                        win_end,
                        run_row["dataset_id"],
                        run_row["dataset_hash"],
                        run_row["config_fingerprint"],
                        run_row["artifact_version"],
                        buys,
                        sells,
                        shorts,
                        covers,
                        json.dumps(stop_cfg, sort_keys=True),
                        purpose,
                        group,
                        "BACKTEST",
                        status,
                        run_row["starting_capital"],
                        run_row["final_equity"],
                        run_row["net_pnl"],
                        run_row["return_percent"],
                        completed_trade_count,
                        run_row["win_rate"],
                        run_row["profit_factor"],
                        run_row["max_drawdown"],
                        source,
                        len(copies),
                        chosen["execution_id"] is None,
                        json.dumps(run_row["provenance"], sort_keys=True),
                        locations,
                        chosen["manifest_path"].as_posix(),
                        chosen["results_path"].as_posix(),
                        chosen["result_hash"],
                        _to_float(net),
                        _to_float(ret_pct),
                        _to_float(win_rate),
                        _to_float(max_dd_pct),
                        _to_float(profit_factor),
                        chosen["manifest_mtime"],
                        chosen["results_mtime"],
                        chosen["manifest_size"],
                        chosen["results_size"],
                        datetime.now(UTC),
                        json.dumps(run_row, sort_keys=True),
                    ])
                    indexed_count += 1

                return indexed_count
            finally:
                if str(self.catalog_path) != ":memory:":
                    con.close()

    def query_runs(self, query: ResearchRunQuery) -> ResearchRunQueryResult:
        """Query research runs with server-side filtering, sorting, and pagination."""
        # Ensure at least an initial sync has occurred
        with self._lock:
            con = self._get_connection()
            try:
                row_count = con.execute("SELECT count(*) FROM research_runs_index").fetchone()[0]
            finally:
                if str(self.catalog_path) != ":memory:":
                    con.close()

        if row_count == 0:
            self.sync()

        with self._lock:
            con = self._get_connection()
            try:
                where_clauses: list[str] = []
                params: list[Any] = []

                if query.symbol:
                    sym = query.symbol.strip().upper()
                    where_clauses.append("list_contains(symbols, ?)")
                    params.append(sym)

                if query.period_role:
                    where_clauses.append("period_role = ?")
                    params.append(query.period_role.strip().upper())

                if query.buy_strategy:
                    where_clauses.append("list_contains(buy_strategies, ?)")
                    params.append(query.buy_strategy.strip())

                if query.sell_strategy:
                    where_clauses.append("list_contains(sell_strategies, ?)")
                    params.append(query.sell_strategy.strip())

                if query.short_strategy:
                    where_clauses.append("list_contains(short_strategies, ?)")
                    params.append(query.short_strategy.strip())

                if query.cover_strategy:
                    where_clauses.append("list_contains(cover_strategies, ?)")
                    params.append(query.cover_strategy.strip())

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

                if query.max_drawdown is not None:
                    where_clauses.append("max_drawdown_num <= ?")
                    params.append(float(query.max_drawdown))

                if query.window_start is not None:
                    where_clauses.append("window_start >= ?")
                    params.append(query.window_start.astimezone(UTC))

                if query.window_end is not None:
                    where_clauses.append("window_end <= ?")
                    params.append(query.window_end.astimezone(UTC))

                if query.search:
                    search_term = f"%{query.search.strip().lower()}%"
                    where_clauses.append("""(
                        lower(run_id) LIKE ? OR
                        lower(selection_id) LIKE ? OR
                        lower(coalesce(run_purpose, '')) LIKE ? OR
                        lower(array_to_string(symbols, ' ')) LIKE ? OR
                        lower(array_to_string(buy_strategies, ' ')) LIKE ? OR
                        lower(array_to_string(sell_strategies, ' ')) LIKE ?
                    )""")
                    params.extend([search_term] * 6)

                where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

                # Total count
                count_query = f"SELECT count(*) FROM research_runs_index {where_sql}"
                total = con.execute(count_query, params).fetchone()[0]

                # Sorting
                sort_columns = {
                    "execution_at": "execution_at",
                    "created_at": "created_at",
                    "net_pnl": "net_pnl_num",
                    "return_percent": "return_percent_num",
                    "win_rate": "win_rate_num",
                    "max_drawdown": "max_drawdown_num",
                    "trade_count": "completed_trade_count",
                }
                order_col = sort_columns.get(query.sort_by, "execution_at")
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
                if str(self.catalog_path) != ":memory:":
                    con.close()

    def get_run(self, identifier: str) -> dict[str, Any] | None:
        """Find a single run by selection_id, execution_id, or run_id."""
        with self._lock:
            con = self._get_connection()
            try:
                row = con.execute("""
                    SELECT payload_json, manifest_path, results_path
                    FROM research_runs_index
                    WHERE selection_id = ? OR execution_id = ? OR run_id = ?
                    LIMIT 1
                """, [identifier, identifier, identifier]).fetchone()
                if not row:
                    return None
                data = json.loads(row[0])
                data["_manifest_path"] = row[1]
                data["_results_path"] = row[2]
                return data
            finally:
                if str(self.catalog_path) != ":memory:":
                    con.close()

    def get_trades_for_run(self, identifier: str) -> list[dict[str, Any]]:
        """Return all trades for a run with recovered strategy attribution."""
        run = self.get_run(identifier)
        if not run:
            raise LookupError(identifier)

        manifest_path = Path(run["_manifest_path"])
        results_path = Path(run["_results_path"])

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        result = json.loads(results_path.read_text(encoding="utf-8"))

        fills = list(result.get("fills", []))
        cycles = list(result.get("cycles", []))
        sig_idx, int_idx = build_signal_indexes(result)
        versions = {
            str(x.get("name")): str(x.get("version"))
            for x in manifest.get("strategies", []) if isinstance(x, dict)
        }

        entry_sides = frozenset({"BUY", "BUY_TO_COVER"})
        exit_sides = frozenset({"SELL", "SELL_SHORT"})

        records: list[dict[str, Any]] = []

        for idx, cycle in enumerate(cycles):
            opened = _parse_time(cycle.get("opened_at"))
            closed = _parse_time(cycle.get("closed_at"))
            symbol = str(cycle.get("symbol", "UNKNOWN")).upper()
            cycle_id = str(cycle.get("cycle_id") or f"{run['run_id']}:{symbol}:{idx + 1}")

            related = []
            for fill in fills:
                at = _parse_time(fill.get("time"))
                if str(fill.get("symbol", "")).upper() != symbol or at is None:
                    continue
                side = str(fill.get("side", "")).upper()
                legacy_match = bool(
                    opened
                    and (
                        (side in entry_sides and at >= opened and (closed is None or at < closed))
                        or (side in exit_sides and at > opened and (closed is None or at <= closed))
                    )
                )
                if fill.get("cycle_id") == cycle_id or (not fill.get("cycle_id") and legacy_match):
                    related.append(fill)

            buys = [f for f in related if str(f.get("side")).upper() in entry_sides]
            sells = [f for f in related if str(f.get("side")).upper() in exit_sides]

            # Reconcile attribution using deterministic matching
            attr = reconcile_trade_attribution(cycle, buys, sells, sig_idx, int_idx, versions)

            bq = sum((_to_decimal(f.get("quantity")) or ZERO for f in buys), ZERO)
            sq = sum((_to_decimal(f.get("quantity")) or ZERO for f in sells), ZERO)

            bp = (
                sum(
                    (
                        (_to_decimal(f.get("quantity")) or ZERO)
                        * (_to_decimal(f.get("price")) or ZERO)
                        for f in buys
                    ),
                    ZERO,
                )
                / bq
                if bq > ZERO
                else None
            )
            sp = (
                sum(
                    (
                        (_to_decimal(f.get("quantity")) or ZERO)
                        * (_to_decimal(f.get("price")) or ZERO)
                        for f in sells
                    ),
                    ZERO,
                )
                / sq
                if sq > ZERO
                else None
            )

            bn = bp * bq if bp is not None else None
            sn = sp * sq if sp is not None else None

            # Calculate realized PnL
            realized = None
            if bp is not None and sp is not None and sq > ZERO:
                realized = (sp - bp) * sq

            records.append({
                "trade_id": cycle_id,
                "execution_mode": "BACKTEST",
                "account_id": f"BACKTEST:{run['selection_id']}",
                "research_run_id": run["selection_id"],
                "symbol": symbol,
                "cycle_id": cycle_id,
                "entry_timestamp": _to_iso(opened),
                "exit_timestamp": _to_iso(closed),
                "entry_price": str(bp) if bp is not None else None,
                "exit_price": str(sp) if sp is not None else None,
                "filled_quantity": str(bq),
                "exited_quantity": str(sq),
                "open_quantity": str(max(ZERO, bq - sq)),
                "entry_notional": str(bn) if bn is not None else None,
                "exit_notional": str(sn) if sn is not None else None,
                "realized_pnl": str(realized) if realized is not None else None,
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
                "direction": "LONG",
            })

        return records
