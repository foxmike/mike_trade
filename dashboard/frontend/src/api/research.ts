/** Research catalog API abstraction.
 *
 * The v2 research endpoints (/api/v2/research/*) are being implemented by a
 * separate backend agent. This adapter probes for them once per session and
 * falls back to the existing v1 read-only endpoints (/api/research/*) when
 * they are unavailable, so the frontend can switch during integration with
 * no component changes. Both implementations satisfy the same interface.
 */

import {
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
  listRuns(query: RunQuery, signal?: AbortSignal): Promise<RunListResult>;
  getRun(selectionId: string, signal?: AbortSignal): Promise<ResearchRunDetail>;
  listRunTrades(selectionId: string, filters: TradeFilters, signal?: AbortSignal): Promise<TradeListResponse>;
  getRunTrade(selectionId: string, tradeId: string, signal?: AbortSignal): Promise<TradeInspection>;
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

  listRunTrades(selectionId: string, filters: TradeFilters, signal?: AbortSignal): Promise<TradeListResponse> {
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

  async listRuns(query: RunQuery = {}, signal?: AbortSignal): Promise<RunListResult> {
    const params = new URLSearchParams();
    if (query.page) params.set("page", String(query.page));
    if (query.pageSize) params.set("page_size", String(query.pageSize));
    if (query.sort) params.set("sort", query.sort);
    if (query.direction) params.set("direction", query.direction);
    if (query.search) params.set("search", query.search);
    if (query.symbol) params.set("symbol", query.symbol);
    if (query.buyStrategy) params.set("buy_strategy", query.buyStrategy);
    if (query.sellStrategy) params.set("sell_strategy", query.sellStrategy);
    if (query.purpose) params.set("purpose", query.purpose);
    if (query.stopSetting) params.set("stop_setting", query.stopSetting);
    if (query.executionDate) params.set("execution_date", query.executionDate);
    const suffix = params.toString();
    return getJson<RunListResult>(`/api/v2/research/runs${suffix ? `?${suffix}` : ""}`, signal);
  }

  getRun(selectionId: string, signal?: AbortSignal): Promise<ResearchRunDetail> {
    return getJson<ResearchRunDetail>(`/api/v2/research/runs/${encodeURIComponent(selectionId)}`, signal);
  }

  listRunTrades(selectionId: string, filters: TradeFilters, signal?: AbortSignal): Promise<TradeListResponse> {
    const params = new URLSearchParams();
    if (filters.page) params.set("page", String(filters.page));
    if (filters.pageSize) params.set("page_size", String(filters.pageSize));
    if (filters.symbol) params.set("symbol", filters.symbol);
    if (filters.sort) params.set("sort", filters.sort);
    if (filters.direction) params.set("direction", filters.direction);
    const suffix = params.toString();
    return getJson<TradeListResponse>(
      `/api/v2/research/runs/${encodeURIComponent(selectionId)}/trades${suffix ? `?${suffix}` : ""}`,
      signal,
    );
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
        await getJson<unknown>("/api/v2/research/runs?page=1&page_size=1");
        return new V2Catalog() as ResearchCatalog;
      } catch {
        return new V1Catalog() as ResearchCatalog;
      }
    })();
  }
  resolved = await resolution;
  return resolved;
}

export function researchFlavor(): "v1" | "v2" | "unresolved" {
  return resolved === null ? "unresolved" : resolved.flavor;
}
