"""Research Console REST API v2 endpoints under /api/v2/research."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from mike_trade.dashboard.v2 import research_analytics as analytics
from mike_trade.dashboard.v2.models import (
    DatasetSnapshotSummary,
    ResearchAggregateResponse,
    ResearchCompareResponse,
    ResearchFacetsResponse,
    ResearchRunDetailResponse,
    ResearchRunListResponse,
    ResearchRunResponse,
    ResearchTradeItemResponse,
    ResearchTradeListResponse,
    UniverseMemberResponse,
    UniverseResponse,
)
from mike_trade.research.run_catalog import (
    AGGREGATE_DIMENSIONS,
    ResearchRunCatalog,
    ResearchRunQuery,
)
from mike_trade.research.universe_service import UniverseService


def _parse_filter_time(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        parsed = datetime.fromisoformat(val.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    # Date-only and naive inputs are UTC, never the server's local zone.
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _run_filters(
    search: str | None = Query(None, description="Free-text search: symbol, strategy, run ID"),
    symbol: str | None = Query(None, description="Filter by ticker symbol"),
    period_role: str | None = Query(None, description="RESEARCH, DEV, or HOLDOUT"),
    buy_strategy: str | None = Query(None, description="Filter by BUY strategy name"),
    sell_strategy: str | None = Query(None, description="Filter by SELL strategy name"),
    short_strategy: str | None = Query(None, description="Filter by SHORT strategy name"),
    cover_strategy: str | None = Query(None, description="Filter by COVER strategy name"),
    strategy: str | None = Query(None, description="Filter by any active strategy name"),
    stop_policy: str | None = Query(None, description="Filter by stop policy label"),
    run_purpose: str | None = Query(None, description="Filter by run purpose"),
    status: str | None = Query(None, description="Filter by completion status"),
    min_trades: int | None = Query(None, ge=0, description="Minimum completed trades"),
    min_win_rate: float | None = Query(None, description="Minimum win rate %"),
    min_pnl: float | None = Query(None, description="Minimum net P&L"),
    min_return: float | None = Query(None, description="Minimum return %"),
    min_profit_factor: float | None = Query(None, description="Minimum profit factor"),
    max_drawdown: float | None = Query(None, description="Maximum drawdown %"),
    window_start: str | None = Query(None, description="Runs starting on/after this date"),
    window_end: str | None = Query(None, description="Runs whose window ends on/before this date"),
    window_key: str | None = Query(None, description="Exact window, e.g. 2026-07-09..2026-10-09"),
    execution_date: str | None = Query(None, description="Execution date (YYYY-MM-DD)"),
) -> dict[str, Any]:
    """Shared filter parameters for the run list and the aggregate endpoints."""
    return {
        "search": search,
        "symbol": symbol,
        "period_role": period_role,
        "buy_strategy": buy_strategy,
        "sell_strategy": sell_strategy,
        "short_strategy": short_strategy,
        "cover_strategy": cover_strategy,
        "strategy": strategy,
        "stop_policy": stop_policy,
        "run_purpose": run_purpose,
        "status": status,
        "min_trades": min_trades,
        "min_win_rate": min_win_rate,
        "min_pnl": min_pnl,
        "min_return": min_return,
        "min_profit_factor": min_profit_factor,
        "max_drawdown": max_drawdown,
        "window_start": _parse_filter_time(window_start),
        "window_end": _parse_filter_time(window_end),
        "window_key": window_key,
        "execution_date": execution_date,
    }


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except ArithmeticError:
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
        backtested_counts, latest_run_ids = run_catalog.run_counts_by_symbol()

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
        filters: Annotated[dict[str, Any], Depends(_run_filters)],
        page: int = Query(1, ge=1, description="Page number (1-based)"),
        page_size: int = Query(50, ge=1, le=500, description="Items per page"),
        sort_by: str = Query(
            "execution_at",
            pattern=(
                "^(execution_at|created_at|net_pnl|return_percent|win_rate|profit_factor"
                "|max_drawdown|trade_count|expectancy|top3_share|symbols)$"
            ),
        ),
        sort_order: str = Query("desc", pattern="^(asc|desc)$"),
    ) -> ResearchRunListResponse:
        """Server-side paginated list of research runs with search, filtering, and sorting."""
        query = ResearchRunQuery(
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            **filters,
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

    @router.get("/runs/facets", response_model=ResearchFacetsResponse)
    def get_run_facets() -> ResearchFacetsResponse:
        """Filter options (with run counts) derived from the whole catalog, not one page."""
        return ResearchFacetsResponse(**run_catalog.get_facets())

    @router.get("/runs/aggregate", response_model=ResearchAggregateResponse)
    def aggregate_runs(
        filters: Annotated[dict[str, Any], Depends(_run_filters)],
        group_by: str = Query(..., description=f"One of {', '.join(AGGREGATE_DIMENSIONS)}"),
        sort_by: str = Query(
            "median_return",
            pattern="^(run_count|trade_count|median_return|mean_return|profitable_rate|median_profit_factor|median_drawdown|worst_drawdown)$",
        ),
        sort_order: str = Query("desc", pattern="^(asc|desc)$"),
        min_runs: int = Query(1, ge=1),
        limit: int = Query(100, ge=1, le=500),
        include_multi_symbol: bool = Query(
            False, description="With group_by=symbol, also count multi-symbol runs per symbol"
        ),
    ) -> ResearchAggregateResponse:
        """Per-group summary of run-level metrics across the filtered runs.

        Statistics are medians/quantiles over independent runs. Capital, P&L and
        returns are never summed across runs; only trade counts are.
        """
        if group_by not in AGGREGATE_DIMENSIONS:
            raise HTTPException(status_code=400, detail=f"Unsupported group_by: {group_by}")
        result = run_catalog.aggregate_runs(
            ResearchRunQuery(**filters),
            group_by,
            sort_by=sort_by,
            sort_order=sort_order,
            min_runs=min_runs,
            limit=limit,
            include_multi_symbol=include_multi_symbol,
        )
        notes = [
            "Each run is measured against its own starting capital; returns are summarized "
            "with medians and quartiles across runs, never added together.",
            "Identical reruns of the same experiment each count as a run; "
            "'distinct outcomes' shows how many differ.",
        ]
        if group_by in {"buy_strategy", "sell_strategy", "short_strategy", "cover_strategy"}:
            notes.append(
                "A run with several strategies in this role appears in each strategy's group, "
                "and the run's result reflects the whole strategy combination."
            )
        if group_by == "symbol" and not include_multi_symbol:
            notes.append(
                f"{result['multi_symbol_runs_excluded']} multi-symbol run(s) excluded: "
                "their result is a portfolio figure, not attributable to one symbol."
            )
        return ResearchAggregateResponse(**result, notes=notes)

    @router.get("/runs/compare", response_model=ResearchCompareResponse)
    def compare_runs(
        run_ids: Annotated[
            list[str],
            Query(alias="run_id", description="2 to 5 run IDs to compare"),
        ],
        include_curves: bool = Query(
            True, description="Include normalized (start = 100) equity curves for overlay"
        ),
        max_points: int = Query(400, ge=20, le=5000),
    ) -> ResearchCompareResponse:
        """Compare 2 to 5 research runs side-by-side."""
        if not (2 <= len(run_ids) <= 5):
            raise HTTPException(
                status_code=400,
                detail="Must provide between 2 and 5 run IDs to compare",
            )

        raw_runs: list[dict[str, Any]] = []
        runs: list[ResearchRunResponse] = []
        for rid in run_ids:
            r = run_catalog.get_run(rid)
            if not r:
                raise HTTPException(status_code=404, detail=f"Run not found: {rid}")
            clean_run = {k: v for k, v in r.items() if not k.startswith("_")}
            raw_runs.append(r)
            runs.append(ResearchRunResponse(**clean_run))

        leaders = analytics.metric_leaders([r.model_dump() for r in runs])
        comparative_metrics = {
            "runs_count": len(runs),
            "symbols_union": sorted({s for r in runs for s in r.symbols}),
            "highest_net_pnl_run": max(
                runs, key=lambda r: Decimal(r.net_pnl or "0")
            ).selection_id,
            "lowest_drawdown_run": min(
                runs, key=lambda r: Decimal(r.max_drawdown or "100")
            ).selection_id,
            "leaders": leaders,
            "period": analytics.period_overlap([r.model_dump() for r in runs]),
        }

        curves: dict[str, list[dict[str, Any]]] = {}
        if include_curves:
            for raw, run in zip(raw_runs, runs, strict=True):
                try:
                    result = json.loads(Path(raw["_results_path"]).read_text(encoding="utf-8"))
                except OSError as err:
                    raise HTTPException(
                        status_code=500, detail="Failed to read run artifact files"
                    ) from err
                curves[run.selection_id] = analytics.normalized_curve(
                    result.get("equity_curve", []),
                    _decimal_or_none(run.starting_capital),
                    max_points,
                )

        return ResearchCompareResponse(
            runs=runs,
            comparative_metrics=comparative_metrics,
            curves=curves,
            differences=analytics.configuration_differences(
                [r.model_dump() for r in runs]
            ),
        )

    @router.get("/runs/{run_id}", response_model=ResearchRunDetailResponse)
    def get_run_detail(
        run_id: str,
        max_points: int = Query(
            1200, ge=50, le=10000, description="Maximum points per chart series"
        ),
    ) -> ResearchRunDetailResponse:
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
        starting = _decimal_or_none(run.get("starting_capital"))
        trades = run_catalog.get_trades_for_run(run["selection_id"])
        stats = dict(run.get("trade_stats") or {})

        equity = analytics.equity_analysis(result.get("equity_curve", []), starting, max_points)
        series, series_total = analytics.trade_series(trades, max_points)
        holding = analytics.holding_seconds(trades)
        final_equity = _decimal_or_none(run.get("final_equity"))
        realized = _decimal_or_none(stats.get("realized_pnl"))
        net = _decimal_or_none(run.get("net_pnl"))

        diagnostics = {
            "total_cycles": len(trades),
            "closed_cycles": stats.get("closed_trades", 0),
            "open_cycles": sum(t["position_status"] != "CLOSED" for t in trades),
            "winning_cycles": stats.get("winning_trades", 0),
            "losing_cycles": stats.get("losing_trades", 0),
            "breakeven_cycles": stats.get("breakeven_trades", 0),
            "average_win": stats.get("average_win"),
            "average_loss": stats.get("average_loss"),
            "expectancy": stats.get("expectancy"),
            "payoff_ratio": stats.get("payoff_ratio"),
            "gross_profit": stats.get("gross_profit"),
            "gross_loss": stats.get("gross_loss"),
            "best_trade": stats.get("best_trade"),
            "worst_trade": stats.get("worst_trade"),
            "top3_profit_share": stats.get("top3_profit_share"),
            "realized_pnl_ex_top3": stats.get("realized_pnl_ex_top3"),
            "realized_pnl": stats.get("realized_pnl"),
            "starting_capital": run.get("starting_capital"),
            "final_equity": run.get("final_equity"),
            "net_pnl": run.get("net_pnl"),
            # Mark-to-market result not explained by closed trades (open positions, rounding).
            "unrealized_or_unexplained_pnl": (
                format(round(net - realized, 6), "f")
                if net is not None and realized is not None
                else None
            ),
            "peak_equity": equity["peak_equity"],
            "lowest_equity": equity["lowest_equity"],
            "total_fees": analytics.total_fees(result),
            "average_holding_seconds": holding["all"],
            "average_holding_seconds_winners": holding["winners"],
            "average_holding_seconds_losers": holding["losers"],
            "return_percent": run.get("return_percent"),
            "ending_equity_matches_curve": (
                final_equity is not None and bool(equity["total_points"])
            ),
        }

        # Safe response without internal path keys
        clean_run = {k: v for k, v in run.items() if not k.startswith("_")}

        return ResearchRunDetailResponse(
            run=ResearchRunResponse(**clean_run),
            configuration=configuration,
            artifact_locations=run.get("artifact_locations", []),
            equity_curve=equity["equity"],
            equity_curve_total_points=equity["total_points"],
            drawdown_curve=equity["drawdown"],
            trade_series=series,
            trade_series_total=series_total,
            pnl_distribution=analytics.pnl_distribution(trades),
            period_performance=analytics.period_performance(
                result.get("equity_curve", []),
                trades,
                _parse_filter_time(run.get("window_start")),
                _parse_filter_time(run.get("window_end")),
                starting,
            ),
            drawdown_detail=equity["detail"],
            assumptions=analytics.assumptions_from_manifest(manifest),
            metric_definitions=analytics.METRIC_DEFINITIONS,
            diagnostics=diagnostics,
        )

    @router.get("/runs/{run_id}/trades", response_model=ResearchTradeListResponse)
    def get_run_trades(
        run_id: str,
        page: int = Query(1, ge=1, description="Page number (1-based)"),
        page_size: int = Query(50, ge=1, le=500, description="Items per page"),
        symbol: str | None = Query(None, description="Filter by symbol"),
        position_status: str | None = Query(None, description="Filter by status (CLOSED, OPEN)"),
        direction: str | None = Query(None, pattern="^(LONG|SHORT)$"),
        entry_strategy: str | None = Query(None, description="Filter by entry strategy"),
        exit_strategy: str | None = Query(None, description="Filter by exit strategy"),
        exit_filter: str | None = Query(None, description="Filter by exit strategy or reason"),
        outcome: str | None = Query(None, pattern="^(WIN|LOSS)$"),
        date_from: str | None = Query(None, description="Minimum entry timestamp"),
        date_to: str | None = Query(None, description="Maximum entry timestamp"),
        sort_by: str = Query(
            "entry_timestamp",
            pattern="^(entry_timestamp|exit_timestamp|symbol|realized_pnl|realized_return_percent|holding_duration)$",
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

        if direction:
            filtered = [t for t in filtered if t.get("direction") == direction]

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

        def holding_seconds(trade: dict[str, Any]) -> float | None:
            opened = _parse_filter_time(trade.get("entry_timestamp"))
            closed = _parse_filter_time(trade.get("exit_timestamp"))
            return (closed - opened).total_seconds() if opened and closed else None

        sort_keys: dict[str, Any] = {
            "entry_timestamp": lambda t: t.get("entry_timestamp") or None,
            "exit_timestamp": lambda t: t.get("exit_timestamp") or None,
            "symbol": lambda t: t.get("symbol") or None,
            "realized_pnl": lambda t: (
                Decimal(t["realized_pnl"]) if t.get("realized_pnl") is not None else None
            ),
            "realized_return_percent": lambda t: (
                Decimal(t["realized_return_percent"])
                if t.get("realized_return_percent") is not None
                else None
            ),
            "holding_duration": holding_seconds,
        }
        key_fn = sort_keys[sort_by]
        # Missing values always sort last, in either direction; trade_id keeps paging stable.
        known = sorted(
            (t for t in filtered if key_fn(t) is not None),
            key=lambda t: (key_fn(t), t["trade_id"]),
            reverse=reverse,
        )
        filtered = known + [t for t in filtered if key_fn(t) is None]

        total = len(filtered)
        limit = max(1, min(500, page_size))
        offset = (page - 1) * limit
        paginated = filtered[offset : offset + limit]
        total_pages = (total + limit - 1) // limit if total > 0 else 1

        run = run_catalog.get_run(run_id)
        selection_id = run["selection_id"] if run else run_id

        subset_pnls = [
            Decimal(t["realized_pnl"])
            for t in filtered
            if t.get("position_status") == "CLOSED" and t.get("realized_pnl") is not None
        ]
        filtered_summary = {
            "trade_count": total,
            "closed_with_pnl": len(subset_pnls),
            "winning": sum(p > 0 for p in subset_pnls),
            "losing": sum(p < 0 for p in subset_pnls),
            "realized_pnl": str(sum(subset_pnls, Decimal("0"))),
        }

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
            available_symbols=sorted({str(t["symbol"]) for t in trades if t.get("symbol")}),
            available_directions=sorted(
                {str(t["direction"]) for t in trades if t.get("direction")}
            ),
            filtered_summary=filtered_summary,
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
