/** Research catalog API abstraction.
 *
 * The v2 research endpoints (/api/v2/research/*) are being implemented by a
 * separate backend agent. This adapter probes for them once per session and
 * falls back to the existing v1 read-only endpoints (/api/research/*) when
 * they are unavailable, so the frontend can switch during integration with
 * no component changes. Both implementations satisfy the same interface.
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
} from "./client";

export interface RunQuery {
  page?: number;
  pageSize?: number;
  sort?: string;
  direction?: string;
  search?: string;
  symbol?: string;
  buyStrategy?: string;
  sellStrategy?: string;
  purpose?: string;
  stopSetting?: string;
  executionDate?: string;
}

export interface RunListResult {
  runs: ResearchRun[];
  page: number;
  page_size: number;
  total: number;
  pages: number;
}

export interface ResearchDatasetMember {
  symbol: string;
  status: "DATASET_AND_BACKTEST" | "DATASET_ONLY" | "MISSING_DATASET";
  dataset_available: boolean;
  has_backtest_results: boolean;
  dataset_id: string | null;
  bar_count: number | null;
  backtest_run_count: number;
}

export interface ResearchDatasetInventory {
  universe_id: string;
  total_universe_symbols: number;
  available_dataset_symbols: number;
  missing_dataset_symbols: number;
  backtested_symbols: number;
  symbols: ResearchDatasetMember[];
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

function normalizeRun(run: ResearchRun): ResearchRun {
  return {
    ...run,
    strategies: Array.isArray(run.strategies) ? run.strategies : [],
    buy_strategies: Array.isArray(run.buy_strategies) ? run.buy_strategies : [],
    sell_strategies: Array.isArray(run.sell_strategies) ? run.sell_strategies : [],
    stop_configuration: run.stop_configuration ?? {},
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

function compareRuns(sort: string | undefined, direction: string): ((a: ResearchRun, b: ResearchRun) => number) | null {
  if (!sort) return null;
  const numeric = (run: ResearchRun): number | null => {
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
  return (a: ResearchRun, b: ResearchRun): number => {
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
    let runs = [...body.runs];

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
    if (query.stopSetting) {
      const needle = query.stopSetting.toLowerCase();
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
    const params = new URLSearchParams();
    if (query.page) params.set("page", String(query.page));
    if (query.pageSize) params.set("page_size", String(query.pageSize));
    if (query.sort) params.set("sort_by", query.sort);
    if (query.direction) params.set("sort_order", query.direction);
    if (query.search) params.set("search", query.search);
    if (query.symbol) params.set("symbol", query.symbol);
    if (query.buyStrategy) params.set("buy_strategy", query.buyStrategy);
    if (query.sellStrategy) params.set("sell_strategy", query.sellStrategy);
    if (query.purpose) params.set("run_purpose", query.purpose);
    if (query.stopSetting) params.set("stop_policy", query.stopSetting);
    if (query.executionDate) params.set("execution_date", query.executionDate);
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
