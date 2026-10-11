"""Research Console REST API v2 endpoints under /api/v2/research."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from mike_trade.dashboard.v2.models import (
    DatasetSnapshotSummary,
    ResearchCompareResponse,
    ResearchRunDetailResponse,
    ResearchRunListResponse,
    ResearchRunResponse,
    ResearchTradeItemResponse,
    ResearchTradeListResponse,
    UniverseMemberResponse,
    UniverseResponse,
)
from mike_trade.research.run_catalog import (
    ResearchRunCatalog,
    ResearchRunQuery,
)
from mike_trade.research.universe_service import UniverseService


def _parse_filter_time(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, TypeError):
        return None


def create_research_v2_router(
    service: Any,
    catalog: ResearchRunCatalog | None = None,
) -> APIRouter:
    """Create the FastAPI router for /api/v2/research."""
    router = APIRouter(prefix="/api/v2/research", tags=["research"])

    settings = getattr(service, "settings", getattr(service, "_settings", None))
    research_root = Path(settings.research_root if settings else "research")
    dataset_root = Path(settings.research_dataset_root if settings else "research/data")

    # Use persistent duckdb index in dataset_root
    catalog_db_path = dataset_root / "research_runs.duckdb"
    run_catalog = catalog or ResearchRunCatalog(research_root, catalog_db_path)
    universe_service = UniverseService(dataset_root)

    @router.get("/datasets", response_model=UniverseResponse)
    def get_datasets() -> UniverseResponse:
        """Return inventory of the 100-stock universe and Parquet snapshots."""
        # The inventory is independently navigable, so it must initialize the run
        # index even when the operator has not opened the run list first.
        run_catalog.query_runs(ResearchRunQuery(page=1, page_size=1))
        # Query backtest run counts per symbol from DuckDB
        con = run_catalog._get_connection()
        try:
            rows = con.execute("""
                SELECT sym, count(*) as cnt, max(selection_id) as last_run
                FROM research_runs_index, UNNEST(symbols) AS expanded(sym)
                GROUP BY sym
            """).fetchall()
            backtested_counts = {str(r[0]).upper(): int(r[1]) for r in rows}
            latest_run_ids = {str(r[0]).upper(): str(r[2]) for r in rows}
        except Exception:
            backtested_counts = {}
            latest_run_ids = {}
        finally:
            if str(run_catalog.catalog_path) != ":memory:":
                con.close()

        inv = universe_service.get_inventory(backtested_counts, latest_run_ids)

        return UniverseResponse(
            universe_id=inv.universe_id,
            selected_at=inv.selected_at,
            total_universe_symbols=inv.total_universe_symbols,
            available_dataset_symbols=inv.available_dataset_symbols,
            missing_dataset_symbols=inv.missing_dataset_symbols,
            backtested_symbols=inv.backtested_symbols,
            symbols=[
                UniverseMemberResponse(
                    symbol=m.symbol,
                    asset_class=m.asset_class,
                    group=m.group,
                    status=m.status,
                    dataset_available=m.dataset_available,
                    has_backtest_results=m.has_backtest_results,
                    dataset_id=m.dataset_id,
                    feed=m.feed,
                    timeframe=m.timeframe,
                    adjustment=m.adjustment,
                    start=m.start,
                    end=m.end,
                    bar_count=m.bar_count,
                    sha256=m.sha256,
                    backtest_run_count=m.backtest_run_count,
                    latest_backtest_run_id=m.latest_backtest_run_id,
                )
                for m in inv.symbols
            ],
            snapshots=[
                DatasetSnapshotSummary(**s) for s in inv.snapshots
            ],
            duckdb_catalog_available=inv.duckdb_catalog_available,
        )

    @router.get("/runs", response_model=ResearchRunListResponse)
    def list_runs(
        page: int = Query(1, ge=1, description="Page number (1-based)"),
        page_size: int = Query(50, ge=1, le=500, description="Items per page"),
        search: str | None = Query(
            None, description="Free-text search across symbols, strategies, run ID"
        ),
        symbol: str | None = Query(None, description="Filter by ticker symbol"),
        period_role: str | None = Query(None, description="RESEARCH, DEV, or HOLDOUT"),
        buy_strategy: str | None = Query(None, description="Filter by BUY strategy name"),
        sell_strategy: str | None = Query(None, description="Filter by SELL strategy name"),
        short_strategy: str | None = Query(None, description="Filter by SHORT strategy name"),
        cover_strategy: str | None = Query(None, description="Filter by COVER strategy name"),
        strategy: str | None = Query(None, description="Filter by any active strategy name"),
        stop_policy: str | None = Query(None, description="Filter by stop policy"),
        run_purpose: str | None = Query(None, description="Filter by run purpose"),
        status: str | None = Query(None, description="Filter by completion status"),
        min_trades: int | None = Query(None, description="Minimum completed trades"),
        min_win_rate: float | None = Query(None, description="Minimum win rate %"),
        min_pnl: float | None = Query(None, description="Minimum net P&L"),
        max_drawdown: float | None = Query(None, description="Maximum drawdown %"),
        window_start: str | None = Query(None, description="Historical window start filter"),
        window_end: str | None = Query(None, description="Historical window end filter"),
        execution_date: str | None = Query(None, description="Execution date (YYYY-MM-DD)"),
        sort_by: str = Query(
            "execution_at",
            pattern="^(execution_at|created_at|net_pnl|return_percent|win_rate|profit_factor|max_drawdown|trade_count|symbols)$",
        ),
        sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    ) -> ResearchRunListResponse:
        """Server-side paginated list of research runs with search, filtering, and sorting."""
        query = ResearchRunQuery(
            page=page,
            page_size=page_size,
            search=search,
            symbol=symbol,
            period_role=period_role,
            buy_strategy=buy_strategy,
            sell_strategy=sell_strategy,
            short_strategy=short_strategy,
            cover_strategy=cover_strategy,
            strategy=strategy,
            stop_policy=stop_policy,
            run_purpose=run_purpose,
            status=status,
            min_trades=min_trades,
            min_win_rate=min_win_rate,
            min_pnl=min_pnl,
            max_drawdown=max_drawdown,
            window_start=_parse_filter_time(window_start),
            window_end=_parse_filter_time(window_end),
            execution_date=execution_date,
            sort_by=sort_by,
            sort_order=sort_order,
        )

        result = run_catalog.query_runs(query)
        items = [ResearchRunResponse(**item) for item in result.items]

        return ResearchRunListResponse(
            items=items,
            total=result.total,
            page=result.page,
            page_size=result.page_size,
            total_pages=result.total_pages,
        )

    @router.get("/runs/compare", response_model=ResearchCompareResponse)
    def compare_runs(
        run_ids: Annotated[
            list[str],
            Query(alias="run_id", description="2 to 5 run IDs to compare"),
        ],
    ) -> ResearchCompareResponse:
        """Compare 2 to 5 research runs side-by-side."""
        if not (2 <= len(run_ids) <= 5):
            raise HTTPException(
                status_code=400,
                detail="Must provide between 2 and 5 run IDs to compare",
            )

        runs: list[ResearchRunResponse] = []
        for rid in run_ids:
            r = run_catalog.get_run(rid)
            if not r:
                raise HTTPException(status_code=404, detail=f"Run not found: {rid}")
            clean_run = {k: v for k, v in r.items() if not k.startswith("_")}
            runs.append(ResearchRunResponse(**clean_run))

        comparative_metrics = {
            "runs_count": len(runs),
            "symbols_union": sorted({s for r in runs for s in r.symbols}),
            "highest_net_pnl_run": max(
                runs, key=lambda r: Decimal(r.net_pnl or "0")
            ).selection_id,
            "lowest_drawdown_run": min(
                runs, key=lambda r: Decimal(r.max_drawdown or "100")
            ).selection_id,
        }

        return ResearchCompareResponse(runs=runs, comparative_metrics=comparative_metrics)

    @router.get("/runs/{run_id}", response_model=ResearchRunDetailResponse)
    def get_run_detail(run_id: str) -> ResearchRunDetailResponse:
        """Return the detailed experiment dossier for a research run."""
        run = run_catalog.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Research run not found: {run_id}")

        manifest_path = Path(run["_manifest_path"])
        results_path = Path(run["_results_path"])

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            result = json.loads(results_path.read_text(encoding="utf-8"))
        except OSError as err:
            raise HTTPException(
                status_code=500, detail="Failed to read run artifact files"
            ) from err

        configuration = dict(manifest.get("configuration") or {})
        equity_curve = list(result.get("equity_curve", []))

        # Build trade distribution diagnostics
        trades = run_catalog.get_trades_for_run(run["selection_id"])
        closed = [
            t for t in trades
            if t.get("position_status") == "CLOSED" and t.get("realized_pnl") is not None
        ]
        pnls = [Decimal(t["realized_pnl"]) for t in closed]
        win_pnls = [p for p in pnls if p > Decimal("0")]
        loss_pnls = [p for p in pnls if p < Decimal("0")]

        avg_win = (
            str(sum(win_pnls, Decimal("0")) / Decimal(len(win_pnls)))
            if win_pnls
            else None
        )
        avg_loss = (
            str(sum(loss_pnls, Decimal("0")) / Decimal(len(loss_pnls)))
            if loss_pnls
            else None
        )
        expectancy = str(sum(pnls, Decimal("0")) / Decimal(len(pnls))) if pnls else None

        diagnostics = {
            "total_cycles": len(trades),
            "closed_cycles": len(closed),
            "winning_cycles": len(win_pnls),
            "losing_cycles": len(loss_pnls),
            "average_win": avg_win,
            "average_loss": avg_loss,
            "expectancy": expectancy,
            "starting_capital": run.get("starting_capital"),
            "final_equity": run.get("final_equity"),
            "net_pnl": run.get("net_pnl"),
        }

        # Safe response without internal path keys
        clean_run = {k: v for k, v in run.items() if not k.startswith("_")}

        return ResearchRunDetailResponse(
            run=ResearchRunResponse(**clean_run),
            configuration=configuration,
            artifact_locations=run.get("artifact_locations", []),
            equity_curve=equity_curve,
            diagnostics=diagnostics,
        )

    @router.get("/runs/{run_id}/trades", response_model=ResearchTradeListResponse)
    def get_run_trades(
        run_id: str,
        page: int = Query(1, ge=1, description="Page number (1-based)"),
        page_size: int = Query(50, ge=1, le=500, description="Items per page"),
        symbol: str | None = Query(None, description="Filter by symbol"),
        position_status: str | None = Query(None, description="Filter by status (CLOSED, OPEN)"),
        entry_strategy: str | None = Query(None, description="Filter by entry strategy"),
        exit_strategy: str | None = Query(None, description="Filter by exit strategy"),
        exit_filter: str | None = Query(None, description="Filter by exit strategy or reason"),
        outcome: str | None = Query(None, pattern="^(WIN|LOSS)$"),
        date_from: str | None = Query(None, description="Minimum entry timestamp"),
        date_to: str | None = Query(None, description="Maximum entry timestamp"),
        sort_by: str = Query(
            "entry_timestamp",
            pattern="^(entry_timestamp|exit_timestamp|symbol|realized_pnl|holding_duration)$",
        ),
        sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    ) -> ResearchTradeListResponse:
        """Return server-side paginated trade records for a run with recovered attribution."""
        try:
            trades = run_catalog.get_trades_for_run(run_id)
        except LookupError as err:
            raise HTTPException(
                status_code=404, detail=f"Research run not found: {run_id}"
            ) from err

        filtered = trades

        if symbol:
            sym_upper = symbol.strip().upper()
            filtered = [t for t in filtered if t.get("symbol") == sym_upper]

        if position_status:
            stat_upper = position_status.strip().upper()
            filtered = [t for t in filtered if t.get("position_status") == stat_upper]

        if entry_strategy:
            strat = entry_strategy.strip()
            filtered = [t for t in filtered if t.get("entry_strategy_id") == strat]

        if exit_strategy:
            strat = exit_strategy.strip()
            filtered = [t for t in filtered if t.get("exit_strategy_id") == strat]

        if exit_filter:
            value = exit_filter.strip()
            filtered = [
                t for t in filtered
                if t.get("exit_strategy_id") == value or t.get("exit_reason") == value
            ]

        if outcome:
            filtered = [
                t
                for t in filtered
                if t.get("realized_pnl") is not None
                and (
                    Decimal(t["realized_pnl"]) > 0
                    if outcome == "WIN"
                    else Decimal(t["realized_pnl"]) < 0
                )
            ]

        if date_from:
            filtered = [
                t for t in filtered if (t.get("entry_timestamp") or "") >= date_from
            ]
        if date_to:
            filtered = [
                t for t in filtered if (t.get("entry_timestamp") or "") <= date_to
            ]

        reverse = sort_order.lower() == "desc"
        if sort_by == "entry_timestamp":
            filtered.sort(key=lambda t: t.get("entry_timestamp") or "", reverse=reverse)
        elif sort_by == "exit_timestamp":
            filtered.sort(key=lambda t: t.get("exit_timestamp") or "", reverse=reverse)
        elif sort_by == "realized_pnl":
            filtered.sort(key=lambda t: Decimal(t.get("realized_pnl") or "0"), reverse=reverse)
        elif sort_by == "symbol":
            filtered.sort(key=lambda t: t.get("symbol") or "", reverse=reverse)
        elif sort_by == "holding_duration":
            def holding_seconds(trade: dict[str, Any]) -> float:
                opened = _parse_filter_time(trade.get("entry_timestamp"))
                closed = _parse_filter_time(trade.get("exit_timestamp"))
                return (closed - opened).total_seconds() if opened and closed else -1.0

            filtered.sort(
                key=holding_seconds,
                reverse=reverse,
            )

        total = len(filtered)
        limit = max(1, min(500, page_size))
        offset = (page - 1) * limit
        paginated = filtered[offset : offset + limit]
        total_pages = (total + limit - 1) // limit if total > 0 else 1

        run = run_catalog.get_run(run_id)
        selection_id = run["selection_id"] if run else run_id

        return ResearchTradeListResponse(
            run_id=run_id,
            selection_id=selection_id,
            page=page,
            page_size=limit,
            total=total,
            total_pages=total_pages,
            items=[ResearchTradeItemResponse(**t) for t in paginated],
            available_entry_strategies=sorted(
                {str(t["entry_strategy_id"]) for t in trades if t.get("entry_strategy_id")}
            ),
            available_exit_reasons=sorted(
                {str(t["exit_reason"]) for t in trades if t.get("exit_reason")}
            ),
        )

    @router.get("/runs/{run_id}/trades/{trade_id}")
    def get_run_trade_inspection(run_id: str, trade_id: str) -> Any:
        """Full trade inspection with candlestick bars, fills, events, and excursion diagnostics."""
        try:
            return service.trades.research_inspection(run_id, trade_id)
        except LookupError as err:
            raise HTTPException(
                status_code=404, detail=f"Trade {trade_id} not found in run {run_id}"
            ) from err

    return router
