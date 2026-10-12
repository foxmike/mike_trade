"""Pydantic schemas for the Research Console v2 REST API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _V2Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class UniverseMemberResponse(_V2Model):
    symbol: str
    asset_class: str
    group: str
    status: str  # "DATASET_AND_BACKTEST", "DATASET_ONLY", "MISSING_DATASET"
    dataset_available: bool
    has_backtest_results: bool
    dataset_id: str | None = None
    feed: str | None = None
    timeframe: str | None = None
    adjustment: str | None = None
    start: str | None = None
    end: str | None = None
    bar_count: int | None = None
    sha256: str | None = None
    backtest_run_count: int = 0
    latest_backtest_run_id: str | None = None


class DatasetSnapshotSummary(_V2Model):
    dataset_id: str
    provider: str
    feed: str
    symbol: str
    asset_class: str
    timeframe: str
    adjustment: str
    acquired_at: str
    start: str
    end: str
    bar_count: int
    data_file: str
    sha256: str


class UniverseResponse(_V2Model):
    universe_id: str
    selected_at: str | None = None
    total_universe_symbols: int
    available_dataset_symbols: int
    missing_dataset_symbols: int
    backtested_symbols: int
    symbols: list[UniverseMemberResponse]
    snapshots: list[DatasetSnapshotSummary]
    duckdb_catalog_available: bool


class ResearchRunResponse(_V2Model):
    selection_id: str
    execution_id: str | None = None
    experiment_id: str
    run_id: str
    created_at: str | None = None
    execution_at: str | None = None
    completed_at: str | None = None
    symbols: list[str] = Field(default_factory=list)
    period_role: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    dataset_id: str | None = None
    dataset_hash: str | None = None
    config_fingerprint: str | None = None
    artifact_version: str | None = None
    buy_strategies: list[str] = Field(default_factory=list)
    sell_strategies: list[str] = Field(default_factory=list)
    short_strategies: list[str] = Field(default_factory=list)
    cover_strategies: list[str] = Field(default_factory=list)
    stop_configuration: dict[str, Any] = Field(default_factory=dict)
    stop_policy: str | None = None
    run_purpose: str | None = None
    artifact_group: str | None = None
    execution_mode: str = "BACKTEST"
    status: str = "COMPLETED"
    starting_capital: str | None = None
    final_equity: str | None = None
    net_pnl: str | None = None
    return_percent: str | None = None
    completed_trade_count: int = 0
    win_rate: str | None = None
    profit_factor: str | None = None
    max_drawdown: str | None = None
    trade_stats: dict[str, Any] = Field(default_factory=dict)
    data_source: str | None = None
    duplicate_artifact_count: int = 1
    execution_identity_uncertain: bool = False
    provenance: dict[str, Any] = Field(default_factory=dict)
    artifact_locations: list[str] = Field(default_factory=list)


class ResearchRunListResponse(_V2Model):
    items: list[ResearchRunResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class ResearchRunDetailResponse(_V2Model):
    run: ResearchRunResponse
    configuration: dict[str, Any] = Field(default_factory=dict)
    artifact_locations: list[str] = Field(default_factory=list)
    #: Mark-to-market account equity, bucket-downsampled to ``equity_curve_points``
    #: (extremes preserved). Includes unrealized P&L of open positions.
    equity_curve: list[dict[str, Any]] = Field(default_factory=list)
    equity_curve_total_points: int = 0
    #: Drawdown from running equity peak (percent), computed on the full curve.
    drawdown_curve: list[dict[str, Any]] = Field(default_factory=list)
    #: Closed-trade realized P&L only, stepped at each trade exit.
    trade_series: list[dict[str, Any]] = Field(default_factory=list)
    trade_series_total: int = 0
    pnl_distribution: dict[str, Any] = Field(default_factory=dict)
    period_performance: dict[str, Any] = Field(default_factory=dict)
    drawdown_detail: dict[str, Any] = Field(default_factory=dict)
    assumptions: dict[str, Any] = Field(default_factory=dict)
    metric_definitions: dict[str, str] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class ResearchTradeItemResponse(_V2Model):
    trade_id: str
    execution_mode: str = "BACKTEST"
    account_id: str
    research_run_id: str
    symbol: str
    cycle_id: str
    entry_timestamp: str | None = None
    exit_timestamp: str | None = None
    entry_price: str | None = None
    exit_price: str | None = None
    filled_quantity: str
    exited_quantity: str
    open_quantity: str
    entry_notional: str | None = None
    exit_notional: str | None = None
    realized_pnl: str | None = None
    realized_return_percent: str | None = None
    position_status: str
    entry_strategy_id: str | None = None
    entry_strategy_version: str | None = None
    entry_signal_id: str | None = None
    entry_signal_timestamp: str | None = None
    exit_strategy_id: str | None = None
    exit_strategy_version: str | None = None
    exit_signal_id: str | None = None
    exit_signal_timestamp: str | None = None
    exit_reason: str | None = None
    attribution_complete: bool
    attribution_source: str = "EXPLICIT"
    direction: str = "LONG"


class ResearchTradeListResponse(_V2Model):
    run_id: str
    selection_id: str
    page: int
    page_size: int
    total: int
    total_pages: int
    items: list[ResearchTradeItemResponse]
    available_entry_strategies: list[str] = Field(default_factory=list)
    available_exit_reasons: list[str] = Field(default_factory=list)
    available_symbols: list[str] = Field(default_factory=list)
    available_directions: list[str] = Field(default_factory=list)
    #: Totals over the whole filtered set (not just the current page).
    filtered_summary: dict[str, Any] = Field(default_factory=dict)


class ResearchCompareRequest(_V2Model):
    run_ids: list[str]


class ResearchCompareResponse(_V2Model):
    runs: list[ResearchRunResponse]
    comparative_metrics: dict[str, Any] = Field(default_factory=dict)
    #: Per selection_id: normalized (start = 100) equity curve, downsampled.
    curves: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    #: Configuration fields whose values differ between the compared runs.
    differences: list[dict[str, Any]] = Field(default_factory=list)


class ResearchFacetOption(_V2Model):
    value: str
    count: int


class ResearchFacetsResponse(_V2Model):
    total_runs: int = 0
    runs_without_stop_policy: int = 0
    buy_strategies: list[ResearchFacetOption] = Field(default_factory=list)
    sell_strategies: list[ResearchFacetOption] = Field(default_factory=list)
    short_strategies: list[ResearchFacetOption] = Field(default_factory=list)
    cover_strategies: list[ResearchFacetOption] = Field(default_factory=list)
    symbols: list[ResearchFacetOption] = Field(default_factory=list)
    stop_policies: list[ResearchFacetOption] = Field(default_factory=list)
    period_roles: list[ResearchFacetOption] = Field(default_factory=list)
    run_purposes: list[ResearchFacetOption] = Field(default_factory=list)
    statuses: list[ResearchFacetOption] = Field(default_factory=list)
    windows: list[ResearchFacetOption] = Field(default_factory=list)
    artifact_groups: list[ResearchFacetOption] = Field(default_factory=list)
    earliest_window_start: str | None = None
    latest_window_end: str | None = None
    bounds: dict[str, Any] = Field(default_factory=dict)


class ResearchAggregateGroup(_V2Model):
    key: str
    run_count: int
    distinct_outcomes: int = 0
    trade_count: int = 0
    profitable_runs: int = 0
    profitable_run_rate: float | None = None
    min_return: float | None = None
    p25_return: float | None = None
    median_return: float | None = None
    p75_return: float | None = None
    max_return: float | None = None
    mean_return: float | None = None
    median_profit_factor: float | None = None
    profit_factor_runs: int = 0
    median_win_rate: float | None = None
    median_drawdown: float | None = None
    worst_drawdown: float | None = None
    median_expectancy: float | None = None
    median_top3_share: float | None = None
    max_top3_share: float | None = None
    distinct_symbol_sets: int = 0
    distinct_windows: int = 0
    first_window_start: str | None = None
    last_window_end: str | None = None
    best_run_id: str | None = None
    worst_run_id: str | None = None


class ResearchAggregateResponse(_V2Model):
    group_by: str
    runs_considered: int
    multi_symbol_runs_excluded: int = 0
    groups: list[ResearchAggregateGroup]
    notes: list[str] = Field(default_factory=list)
