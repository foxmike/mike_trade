/** Research catalog API abstraction.
 *
 * The run list / run / trade-inspection accessors probe /api/v2/research once
 * per session and fall back to the legacy v1 read-only endpoints, which keeps
 * the trade pages that share them working. The Research Lab analytics
 * (facets, aggregates, run analysis, journal, compare) are v2-only and are
 * exposed as plain functions at the bottom of this file.
 */

import {
  ApiError,
  getJson,
  getResearchRun,
  getResearchRuns,
  getResearchTrade,
  getResearchTrades,
  type ResearchRun,
  type ResearchRunDetail,
  type TradeFilters,
  type TradeInspection,
  type TradeListResponse,
  type TradeRecord,
} from "./client";

/** Server-side run filters shared by the explorer and the performance views. */
export interface RunFilters {
  search?: string;
  symbol?: string;
  buyStrategy?: string;
  sellStrategy?: string;
  shortStrategy?: string;
  coverStrategy?: string;
  purpose?: string;
  /** Stop-policy label as listed by the facets endpoint. */
  stopPolicy?: string;
  /** Legacy alias of stopPolicy (substring match on the recorded stop configuration). */
  stopSetting?: string;
  status?: string;
  periodRole?: string;
  windowStart?: string;
  windowEnd?: string;
  windowKey?: string;
  executionDate?: string;
  minTrades?: number;
  minWinRate?: number;
  minReturn?: number;
  minProfitFactor?: number;
  maxDrawdown?: number;
}

export interface RunQuery extends RunFilters {
  page?: number;
  pageSize?: number;
  sort?: string;
  direction?: string;
}

/** Closed-trade statistics recorded per run (money values are Decimal strings). */
export interface TradeStats {
  closed_trades: number;
  winning_trades: number;
  losing_trades: number;
  breakeven_trades: number;
  gross_profit: string | null;
  gross_loss: string | null;
  realized_pnl: string | null;
  average_win: string | null;
  average_loss: string | null;
  expectancy: string | null;
  payoff_ratio: string | null;
  best_trade: string | null;
  worst_trade: string | null;
  top3_profit_share: string | null;
  realized_pnl_ex_top3: string | null;
}

/** Run row as served by the v2 catalog (legacy v1 rows get safe defaults). */
export type ResearchRunRow = ResearchRun & {
  short_strategies: string[];
  cover_strategies: string[];
  stop_policy: string | null;
  trade_stats: Partial<TradeStats>;
};

export interface RunListResult {
  runs: ResearchRunRow[];
  page: number;
  page_size: number;
  total: number;
  pages: number;
}

export interface ResearchDatasetMember {
  symbol: string;
  asset_class?: string;
  group?: string;
  status: "DATASET_AND_BACKTEST" | "DATASET_ONLY" | "MISSING_DATASET";
  dataset_available: boolean;
  has_backtest_results: boolean;
  dataset_id: string | null;
  feed?: string | null;
  timeframe?: string | null;
  adjustment?: string | null;
  start?: string | null;
  end?: string | null;
  bar_count: number | null;
  sha256?: string | null;
  backtest_run_count: number;
  latest_backtest_run_id?: string | null;
}

export interface ResearchDatasetSnapshot {
  dataset_id: string;
  provider: string;
  feed: string;
  symbol: string;
  asset_class: string;
  timeframe: string;
  adjustment: string;
  acquired_at: string;
  start: string;
  end: string;
  bar_count: number;
  data_file: string;
  sha256: string;
}

export interface ResearchDatasetInventory {
  universe_id: string;
  selected_at?: string | null;
  total_universe_symbols: number;
  available_dataset_symbols: number;
  missing_dataset_symbols: number;
  backtested_symbols: number;
  symbols: ResearchDatasetMember[];
  snapshots?: ResearchDatasetSnapshot[];
  duckdb_catalog_available?: boolean;
}

export const emptyRunList: RunListResult = {
  runs: [],
  page: 1,
  page_size: 25,
  total: 0,
  pages: 1,
};

export interface ResearchCatalog {
  /** Which backend flavor is actually answering. */
  readonly flavor: "v1" | "v2";
  getDatasets(signal?: AbortSignal): Promise<ResearchDatasetInventory | null>;
  listRuns(query: RunQuery, signal?: AbortSignal): Promise<RunListResult>;
  getRun(selectionId: string, signal?: AbortSignal): Promise<ResearchRunDetail>;
  listRunTrades(selectionId: string, filters: TradeFilters, signal?: AbortSignal): Promise<TradeListResponse>;
  getRunTrade(selectionId: string, tradeId: string, signal?: AbortSignal): Promise<TradeInspection>;
}

interface V2RunListResponse {
  items: ResearchRun[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

interface V2TradeListResponse {
  items: Array<Partial<TradeListResponse["items"][number]> & Pick<TradeListResponse["items"][number], "trade_id" | "symbol" | "cycle_id">>;
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  available_entry_strategies?: string[];
  available_exit_reasons?: string[];
}

/** Query-string encoding of the shared server-side run filters. */
export function runFilterParams(filters: RunFilters): URLSearchParams {
  const params = new URLSearchParams();
  const text: Array<[string, string | undefined]> = [
    ["search", filters.search],
    ["symbol", filters.symbol],
    ["buy_strategy", filters.buyStrategy],
    ["sell_strategy", filters.sellStrategy],
    ["short_strategy", filters.shortStrategy],
    ["cover_strategy", filters.coverStrategy],
    ["run_purpose", filters.purpose],
    ["stop_policy", filters.stopPolicy ?? filters.stopSetting],
    ["status", filters.status],
    ["period_role", filters.periodRole],
    ["window_start", filters.windowStart],
    ["window_end", filters.windowEnd],
    ["window_key", filters.windowKey],
    ["execution_date", filters.executionDate],
  ];
  for (const [key, value] of text) if (value) params.set(key, value);
  const numeric: Array<[string, number | undefined]> = [
    ["min_trades", filters.minTrades],
    ["min_win_rate", filters.minWinRate],
    ["min_return", filters.minReturn],
    ["min_profit_factor", filters.minProfitFactor],
    ["max_drawdown", filters.maxDrawdown],
  ];
  for (const [key, value] of numeric) {
    if (value !== undefined && Number.isFinite(value)) params.set(key, String(value));
  }
  return params;
}

function assertV2RunList(value: unknown): asserts value is V2RunListResponse {
  if (
    value === null ||
    typeof value !== "object" ||
    !Array.isArray((value as { items?: unknown }).items) ||
    typeof (value as { total_pages?: unknown }).total_pages !== "number"
  ) {
    throw new Error("Research v2 contract error: expected items and total_pages");
  }
}

export function normalizeRun(run: ResearchRun): ResearchRunRow {
  const extra = run as Partial<ResearchRunRow>;
  return {
    ...run,
    strategies: Array.isArray(run.strategies) ? run.strategies : [],
    buy_strategies: Array.isArray(run.buy_strategies) ? run.buy_strategies : [],
    sell_strategies: Array.isArray(run.sell_strategies) ? run.sell_strategies : [],
    short_strategies: Array.isArray(extra.short_strategies) ? extra.short_strategies : [],
    cover_strategies: Array.isArray(extra.cover_strategies) ? extra.cover_strategies : [],
    stop_configuration: run.stop_configuration ?? {},
    stop_policy: extra.stop_policy ?? null,
    trade_stats: extra.trade_stats ?? {},
    provenance: run.provenance ?? {},
  };
}

function normalizeTrade(
  trade: V2TradeListResponse["items"][number],
): TradeListResponse["items"][number] {
  return {
    scenario_id: null,
    research_run_id: null,
    entry_timestamp: null,
    exit_timestamp: null,
    entry_price: null,
    exit_price: null,
    filled_quantity: "0",
    exited_quantity: "0",
    open_quantity: "0",
    entry_notional: null,
    exit_notional: null,
    realized_pnl: null,
    unrealized_pnl: null,
    realized_return_percent: null,
    position_status: "UNKNOWN",
    entry_strategy_id: null,
    entry_strategy_version: null,
    entry_signal_id: null,
    entry_signal_timestamp: null,
    entry_decision: null,
    exit_strategy_id: null,
    exit_strategy_version: null,
    exit_signal_id: null,
    exit_signal_timestamp: null,
    exit_reason: null,
    protective_stop_mode: null,
    initial_stop_price: null,
    final_stop_price: null,
    holding_duration_seconds: null,
    data_provenance: null,
    attribution_complete: false,
    execution_mode: "BACKTEST",
    account_id: "BACKTEST",
    ...trade,
  };
}

// ---------------------------------------------------------------------------
// v1 flavor: existing /api/research endpoints (unpaginated run list, so
// sorting and paging happen client-side over the full result).
// ---------------------------------------------------------------------------

function compareRuns(
  sort: string | undefined,
  direction: string,
): ((a: ResearchRunRow, b: ResearchRunRow) => number) | null {
  if (!sort) return null;
  const numeric = (run: ResearchRunRow): number | null => {
    switch (sort) {
      case "net_pnl":
        return run.net_pnl === null ? null : Number(run.net_pnl);
      case "return_percent":
        return run.return_percent === null ? null : Number(run.return_percent);
      case "win_rate":
        return run.win_rate === null ? null : Number(run.win_rate);
      case "profit_factor":
        return run.profit_factor === null ? null : Number(run.profit_factor);
      case "max_drawdown":
        return run.max_drawdown === null ? null : Number(run.max_drawdown);
      case "trade_count":
        return run.completed_trade_count;
      case "symbols":
        return run.symbols.length;
      default:
        return null;
    }
  };
  const factor = direction === "asc" ? 1 : -1;
  return (a: ResearchRunRow, b: ResearchRunRow): number => {
    if (sort === "execution_at") {
      const av = a.execution_at ?? "";
      const bv = b.execution_at ?? "";
      return av < bv ? -factor : av > bv ? factor : 0;
    }
    const an = numeric(a);
    const bn = numeric(b);
    if (an === null && bn === null) return 0;
    if (an === null) return 1;
    if (bn === null) return -1;
    return (an - bn) * factor;
  };
}

class V1Catalog implements ResearchCatalog {
  readonly flavor = "v1" as const;

  getDatasets(): Promise<null> {
    return Promise.resolve(null);
  }

  /** v1 serves the full run list unpaginated, so filtering, sorting and
   * paging happen here over the complete set — identical results to what a
   * v2 server would produce for the same query. */
  async listRuns(query: RunQuery, signal?: AbortSignal): Promise<RunListResult> {
    const body = await getResearchRuns(signal);
    let runs = body.runs.map(normalizeRun);

    if (query.search) {
      const needle = query.search.toLowerCase();
      runs = runs.filter((run) => {
        const text =
          `${run.symbols.join(" ")} ${[...run.buy_strategies, ...run.sell_strategies].join(" ")} ` +
          `${run.run_id} ${run.execution_id ?? ""} ${run.run_purpose ?? ""} ${run.artifact_group ?? ""}`.toLowerCase();
        return text.includes(needle);
      });
    }
    if (query.symbol) {
      const needle = query.symbol.toUpperCase();
      runs = runs.filter((run) => run.symbols.some((symbol) => symbol.toUpperCase().includes(needle)));
    }
    if (query.buyStrategy) runs = runs.filter((run) => run.buy_strategies.includes(query.buyStrategy as string));
    if (query.sellStrategy) runs = runs.filter((run) => run.sell_strategies.includes(query.sellStrategy as string));
    if (query.purpose) {
      runs = runs.filter(
        (run) => (run.run_purpose ?? run.artifact_group ?? "legacy/unknown") === query.purpose,
      );
    }
    const stopNeedle = query.stopPolicy ?? query.stopSetting;
    if (stopNeedle) {
      const needle = stopNeedle.toLowerCase();
      runs = runs.filter((run) => JSON.stringify(run.stop_configuration).toLowerCase().includes(needle));
    }
    if (query.executionDate) {
      runs = runs.filter((run) => (run.execution_at ?? "").slice(0, 10) === query.executionDate);
    }

    const compare = compareRuns(query.sort, query.direction ?? "desc");
    if (compare !== null) runs.sort(compare);
    const page = Math.max(1, query.page ?? 1);
    const pageSize = Math.max(1, query.pageSize ?? 25);
    const start = (page - 1) * pageSize;
    return {
      runs: runs.slice(start, start + pageSize),
      page,
      page_size: pageSize,
      total: runs.length,
      pages: Math.max(1, Math.ceil(runs.length / pageSize)),
    };
  }

  getRun(selectionId: string, signal?: AbortSignal): Promise<ResearchRunDetail> {
    return getResearchRun(selectionId, signal);
  }

  async listRunTrades(selectionId: string, filters: TradeFilters, signal?: AbortSignal): Promise<TradeListResponse> {
    return getResearchTrades(selectionId, filters, signal);
  }

  getRunTrade(selectionId: string, tradeId: string, signal?: AbortSignal): Promise<TradeInspection> {
    return getResearchTrade(selectionId, tradeId, signal);
  }
}

// ---------------------------------------------------------------------------
// v2 flavor: /api/v2/research endpoints (server-side pagination + sorting).
// Filter parameters are sent only here so the v1 endpoints stay untouched.
// ---------------------------------------------------------------------------

class V2Catalog implements ResearchCatalog {
  readonly flavor = "v2" as const;

  getDatasets(signal?: AbortSignal): Promise<ResearchDatasetInventory> {
    return getJson<ResearchDatasetInventory>("/api/v2/research/datasets", signal);
  }

  async listRuns(query: RunQuery = {}, signal?: AbortSignal): Promise<RunListResult> {
    const params = runFilterParams(query);
    if (query.page) params.set("page", String(query.page));
    if (query.pageSize) params.set("page_size", String(query.pageSize));
    if (query.sort) params.set("sort_by", query.sort);
    if (query.direction) params.set("sort_order", query.direction);
    const suffix = params.toString();
    const body = await getJson<unknown>(`/api/v2/research/runs${suffix ? `?${suffix}` : ""}`, signal);
    assertV2RunList(body);
    return {
      runs: body.items.map(normalizeRun),
      total: body.total,
      page: body.page,
      page_size: body.page_size,
      pages: body.total_pages,
    };
  }

  getRun(selectionId: string, signal?: AbortSignal): Promise<ResearchRunDetail> {
    return getJson<ResearchRunDetail>(
      `/api/v2/research/runs/${encodeURIComponent(selectionId)}`,
      signal,
    ).then((detail) => ({ ...detail, run: normalizeRun(detail.run) }));
  }

  async listRunTrades(selectionId: string, filters: TradeFilters, signal?: AbortSignal): Promise<TradeListResponse> {
    const params = new URLSearchParams();
    if (filters.page) params.set("page", String(filters.page));
    if (filters.pageSize) params.set("page_size", String(filters.pageSize));
    if (filters.symbol) params.set("symbol", filters.symbol);
    if (filters.dateFrom) params.set("date_from", filters.dateFrom);
    if (filters.dateTo) params.set("date_to", filters.dateTo);
    if (filters.entryStrategy) params.set("entry_strategy", filters.entryStrategy);
    if (filters.exitFilter) params.set("exit_filter", filters.exitFilter);
    if (filters.outcome) params.set("outcome", filters.outcome);
    if (filters.positionStatus) params.set("position_status", filters.positionStatus);
    if (filters.sort) params.set("sort_by", filters.sort);
    if (filters.direction) params.set("sort_order", filters.direction);
    const suffix = params.toString();
    const body = await getJson<V2TradeListResponse>(
      `/api/v2/research/runs/${encodeURIComponent(selectionId)}/trades${suffix ? `?${suffix}` : ""}`,
      signal,
    );
    const items = body.items.map(normalizeTrade);
    return {
      items,
      total: body.total,
      page: body.page,
      page_size: body.page_size,
      pages: body.total_pages,
      available_entry_strategies: body.available_entry_strategies ?? [
        ...new Set(items.flatMap((trade) => trade.entry_strategy_id ?? [])),
      ].sort(),
      available_exit_reasons: body.available_exit_reasons ?? [
        ...new Set(items.flatMap((trade) => trade.exit_reason ?? [])),
      ].sort(),
    };
  }

  getRunTrade(selectionId: string, tradeId: string, signal?: AbortSignal): Promise<TradeInspection> {
    return getJson<TradeInspection>(
      `/api/v2/research/runs/${encodeURIComponent(selectionId)}/trades/${encodeURIComponent(tradeId)}`,
      signal,
    );
  }
}

// ---------------------------------------------------------------------------
// Flavor resolution: probe v2 once per page load, then stick with the answer.
// ---------------------------------------------------------------------------

let resolved: ResearchCatalog | null = null;
let resolution: Promise<ResearchCatalog> | null = null;

export async function resolveResearchCatalog(): Promise<ResearchCatalog> {
  if (resolved !== null) return resolved;
  if (resolution === null) {
    resolution = (async () => {
      try {
        const probe = await getJson<unknown>("/api/v2/research/runs?page=1&page_size=1");
        assertV2RunList(probe);
        return new V2Catalog() as ResearchCatalog;
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          return new V1Catalog() as ResearchCatalog;
        }
        throw error;
      }
    })();
  }
  resolved = await resolution;
  return resolved;
}

export function researchFlavor(): "v1" | "v2" | "unresolved" {
  return resolved === null ? "unresolved" : resolved.flavor;
}

// ---------------------------------------------------------------------------
// Research Lab analytics (v2-only, read-only).
// ---------------------------------------------------------------------------

export interface FacetOption {
  value: string;
  count: number;
}

export interface RunFacets {
  total_runs: number;
  runs_without_stop_policy: number;
  buy_strategies: FacetOption[];
  sell_strategies: FacetOption[];
  short_strategies: FacetOption[];
  cover_strategies: FacetOption[];
  symbols: FacetOption[];
  stop_policies: FacetOption[];
  period_roles: FacetOption[];
  run_purposes: FacetOption[];
  statuses: FacetOption[];
  windows: FacetOption[];
  artifact_groups: FacetOption[];
  earliest_window_start: string | null;
  latest_window_end: string | null;
  bounds: Record<string, number | null>;
}

export type AggregateDimension =
  | "buy_strategy"
  | "sell_strategy"
  | "short_strategy"
  | "cover_strategy"
  | "symbol"
  | "stop_policy"
  | "period_role"
  | "run_purpose"
  | "status"
  | "window"
  | "artifact_group";

export interface AggregateGroup {
  key: string;
  run_count: number;
  distinct_outcomes: number;
  trade_count: number;
  profitable_runs: number;
  profitable_run_rate: number | null;
  min_return: number | null;
  p25_return: number | null;
  median_return: number | null;
  p75_return: number | null;
  max_return: number | null;
  mean_return: number | null;
  median_profit_factor: number | null;
  profit_factor_runs: number;
  median_win_rate: number | null;
  median_drawdown: number | null;
  worst_drawdown: number | null;
  median_expectancy: number | null;
  median_top3_share: number | null;
  max_top3_share: number | null;
  distinct_symbol_sets: number;
  distinct_windows: number;
  first_window_start: string | null;
  last_window_end: string | null;
  best_run_id: string | null;
  worst_run_id: string | null;
}

export interface AggregateResponse {
  group_by: AggregateDimension;
  runs_considered: number;
  multi_symbol_runs_excluded: number;
  groups: AggregateGroup[];
  notes: string[];
}

export interface AggregateQuery extends RunFilters {
  groupBy: AggregateDimension;
  sort?: string;
  direction?: string;
  minRuns?: number;
  limit?: number;
}

export interface EquityPoint {
  time: string;
  equity: number;
}
export interface DrawdownPoint {
  time: string;
  drawdown_percent: number;
}
export interface TradeSeriesPoint {
  trade_index: number;
  time: string;
  trade_id: string;
  symbol: string;
  trade_pnl: number;
  cumulative_pnl: number;
}
export interface DistributionBin {
  lower: number;
  upper: number;
  count: number;
}
export interface PnlDistribution {
  available: boolean;
  count?: number;
  min?: number;
  max?: number;
  mean?: number;
  median?: number;
  stdev?: number;
  winners?: number;
  losers?: number;
  bins: DistributionBin[];
}
export interface PeriodRow {
  period: string;
  start_equity: string | null;
  end_equity: string | null;
  equity_change: string | null;
  return_percent: string | null;
  closed_trades: number;
  winning_trades: number;
  realized_pnl: string;
}
export interface DrawdownDetail {
  available: boolean;
  max_drawdown_percent?: string;
  max_drawdown_amount?: string;
  peak_equity?: string;
  peak_time?: string;
  trough_equity?: string;
  trough_time?: string;
  recovered?: boolean;
  recovered_at?: string | null;
}

export interface RunAnalysis {
  run: ResearchRunRow;
  configuration: Record<string, unknown>;
  artifact_locations: string[];
  equity_curve: EquityPoint[];
  equity_curve_total_points: number;
  drawdown_curve: DrawdownPoint[];
  trade_series: TradeSeriesPoint[];
  trade_series_total: number;
  pnl_distribution: PnlDistribution;
  period_performance: { granularity: "day" | "week" | "month"; periods: PeriodRow[] };
  drawdown_detail: DrawdownDetail;
  assumptions: Record<string, unknown>;
  metric_definitions: Record<string, string>;
  diagnostics: Record<string, string | number | boolean | null>;
}

export interface JournalTrade extends TradeRecord {
  direction: string;
}

export interface JournalResponse {
  run_id: string;
  selection_id: string;
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
  items: JournalTrade[];
  available_entry_strategies: string[];
  available_exit_reasons: string[];
  available_symbols: string[];
  available_directions: string[];
  filtered_summary: {
    trade_count: number;
    closed_with_pnl: number;
    winning: number;
    losing: number;
    realized_pnl: string;
  };
}

export interface JournalQuery {
  page?: number;
  pageSize?: number;
  symbol?: string;
  entryStrategy?: string;
  exitReason?: string;
  outcome?: "WIN" | "LOSS";
  direction?: "LONG" | "SHORT";
  positionStatus?: string;
  sort?: string;
  order?: "asc" | "desc";
}

export interface ComparePeriod {
  comparable: boolean;
  identical_windows?: boolean;
  overlap_start?: string | null;
  overlap_end?: string | null;
  overlap_days?: number;
  reason?: string | null;
}

export interface CompareResponse {
  runs: ResearchRunRow[];
  comparative_metrics: {
    runs_count: number;
    symbols_union: string[];
    highest_net_pnl_run: string;
    lowest_drawdown_run: string;
    leaders: Record<string, string | null>;
    period: ComparePeriod;
  };
  curves: Record<string, Array<{ time: string; index: number }>>;
  differences: Array<{ field: string; label: string; values: Record<string, unknown> }>;
}

export function getRunFacets(signal?: AbortSignal): Promise<RunFacets> {
  return getJson<RunFacets>("/api/v2/research/runs/facets", signal);
}

export function getDatasetInventory(signal?: AbortSignal): Promise<ResearchDatasetInventory> {
  return getJson<ResearchDatasetInventory>("/api/v2/research/datasets", signal);
}

export function aggregateRuns(query: AggregateQuery, signal?: AbortSignal): Promise<AggregateResponse> {
  const params = runFilterParams(query);
  params.set("group_by", query.groupBy);
  if (query.sort) params.set("sort_by", query.sort);
  if (query.direction) params.set("sort_order", query.direction);
  if (query.minRuns) params.set("min_runs", String(query.minRuns));
  if (query.limit) params.set("limit", String(query.limit));
  return getJson<AggregateResponse>(`/api/v2/research/runs/aggregate?${params.toString()}`, signal);
}

export function getRunAnalysis(
  selectionId: string,
  maxPoints = 1200,
  signal?: AbortSignal,
): Promise<RunAnalysis> {
  return getJson<RunAnalysis>(
    `/api/v2/research/runs/${encodeURIComponent(selectionId)}?max_points=${String(maxPoints)}`,
    signal,
  ).then((body) => ({ ...body, run: normalizeRun(body.run) }));
}

export function getRunJournal(
  selectionId: string,
  query: JournalQuery,
  signal?: AbortSignal,
): Promise<JournalResponse> {
  const params = new URLSearchParams();
  if (query.page) params.set("page", String(query.page));
  if (query.pageSize) params.set("page_size", String(query.pageSize));
  if (query.symbol) params.set("symbol", query.symbol);
  if (query.entryStrategy) params.set("entry_strategy", query.entryStrategy);
  if (query.exitReason) params.set("exit_filter", query.exitReason);
  if (query.outcome) params.set("outcome", query.outcome);
  if (query.direction) params.set("direction", query.direction);
  if (query.positionStatus) params.set("position_status", query.positionStatus);
  if (query.sort) params.set("sort_by", query.sort);
  if (query.order) params.set("sort_order", query.order);
  const suffix = params.toString();
  return getJson<JournalResponse>(
    `/api/v2/research/runs/${encodeURIComponent(selectionId)}/trades${suffix ? `?${suffix}` : ""}`,
    signal,
  );
}

export function compareRunIds(selectionIds: string[], signal?: AbortSignal): Promise<CompareResponse> {
  const params = new URLSearchParams();
  for (const id of selectionIds) params.append("run_id", id);
  return getJson<CompareResponse>(`/api/v2/research/runs/compare?${params.toString()}`, signal).then(
    (body) => ({ ...body, runs: body.runs.map(normalizeRun) }),
  );
}
