/** Research Lab state that belongs in the URL so Back from a run restores the
 * exact view, and any view can be shared as a link. */

import type { AggregateDimension, RunFilters } from "../../api/research";

export type LabView = "universe" | "explorer" | "performance" | "compare";

export const LAB_VIEWS: Array<{ id: LabView; label: string }> = [
  { id: "explorer", label: "Experiments" },
  { id: "performance", label: "Performance" },
  { id: "compare", label: "Compare" },
  { id: "universe", label: "Dataset universe" },
];

export const MAX_COMPARE = 5;

const FILTER_KEYS: Array<[keyof RunFilters, string, "text" | "number"]> = [
  ["search", "q", "text"],
  ["symbol", "symbol", "text"],
  ["buyStrategy", "buy", "text"],
  ["sellStrategy", "sell", "text"],
  ["shortStrategy", "short", "text"],
  ["coverStrategy", "cover", "text"],
  ["stopPolicy", "stop", "text"],
  ["purpose", "purpose", "text"],
  ["status", "status", "text"],
  ["periodRole", "role", "text"],
  ["windowStart", "from", "text"],
  ["windowEnd", "to", "text"],
  ["windowKey", "window", "text"],
  ["minTrades", "minTrades", "number"],
  ["minWinRate", "minWin", "number"],
  ["minReturn", "minRet", "number"],
  ["minProfitFactor", "minPF", "number"],
  ["maxDrawdown", "maxDD", "number"],
];

export function filtersFromParams(params: URLSearchParams): RunFilters {
  const filters: Record<string, string | number> = {};
  for (const [field, key, kind] of FILTER_KEYS) {
    const raw = params.get(key);
    if (raw === null || raw === "") continue;
    if (kind === "number") {
      const parsed = Number(raw);
      if (Number.isFinite(parsed)) filters[field] = parsed;
    } else {
      filters[field] = raw;
    }
  }
  return filters as RunFilters;
}

/** Copy `filters` into `params`, deleting keys for unset filters. */
export function writeFilters(params: URLSearchParams, filters: RunFilters): void {
  for (const [field, key] of FILTER_KEYS) {
    const value = filters[field];
    if (value === undefined || value === "") params.delete(key);
    else params.set(key, String(value));
  }
}

export function activeFilterCount(filters: RunFilters): number {
  return FILTER_KEYS.filter(([field]) => {
    const value = filters[field];
    return value !== undefined && value !== "";
  }).length;
}

export function parseView(raw: string | null): LabView {
  return LAB_VIEWS.some((view) => view.id === raw) ? (raw as LabView) : "explorer";
}

export function parseCompareIds(raw: string | null): string[] {
  if (!raw) return [];
  return [...new Set(raw.split(",").filter(Boolean))].slice(0, MAX_COMPARE);
}

/** Filter field that a performance group key maps to (for drill-down). */
export const DIMENSION_FILTER: Partial<Record<AggregateDimension, keyof RunFilters>> = {
  buy_strategy: "buyStrategy",
  sell_strategy: "sellStrategy",
  short_strategy: "shortStrategy",
  cover_strategy: "coverStrategy",
  symbol: "symbol",
  stop_policy: "stopPolicy",
  period_role: "periodRole",
  run_purpose: "purpose",
  status: "status",
  window: "windowKey",
};
