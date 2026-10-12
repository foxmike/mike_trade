/** URL-backed Research Lab state (view, filters, sort, paging, comparison tray). */

import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router";
import type { AggregateDimension, RunFilters } from "../../api/research";
import {
  MAX_COMPARE,
  filtersFromParams,
  parseCompareIds,
  parseView,
  writeFilters,
  type LabView,
} from "./urlState";

const DIMENSIONS: AggregateDimension[] = [
  "buy_strategy",
  "sell_strategy",
  "short_strategy",
  "cover_strategy",
  "symbol",
  "stop_policy",
  "period_role",
  "run_purpose",
  "status",
  "window",
  "artifact_group",
];

export interface LabState {
  view: LabView;
  filters: RunFilters;
  sort: string;
  direction: "asc" | "desc";
  page: number;
  pageSize: number;
  groupBy: AggregateDimension;
  aggSort: string;
  aggDirection: "asc" | "desc";
  selectedSymbol: string | null;
  compareIds: string[];
  setView: (view: LabView) => void;
  patchFilters: (patch: Partial<RunFilters>) => void;
  resetFilters: () => void;
  setSort: (sort: string, direction?: "asc" | "desc") => void;
  setPage: (page: number) => void;
  setPageSize: (size: number) => void;
  setGroupBy: (dimension: AggregateDimension) => void;
  setAggSort: (sort: string, direction: "asc" | "desc") => void;
  selectSymbol: (symbol: string | null) => void;
  toggleCompare: (selectionId: string) => void;
  setCompare: (ids: string[]) => void;
  /** Replace filters wholesale and jump to a view (drill-down). */
  drillDown: (filters: RunFilters, view: LabView) => void;
  /** Search string to navigate back to this exact lab state. */
  search: string;
}

export function useLabState(): LabState {
  const [params, setParams] = useSearchParams();

  const mutate = useCallback(
    (change: (next: URLSearchParams) => void, push = false) => {
      setParams(
        (previous) => {
          const next = new URLSearchParams(previous);
          change(next);
          return next;
        },
        { replace: !push },
      );
    },
    [setParams],
  );

  const filters = useMemo(() => filtersFromParams(params), [params]);
  const pageRaw = Number(params.get("page") ?? "1");
  const sizeRaw = Number(params.get("size") ?? "25");
  const dimRaw = params.get("group") as AggregateDimension | null;
  const aggDirRaw = params.get("adir");
  const dirRaw = params.get("dir");

  return {
    view: parseView(params.get("view")),
    filters,
    sort: params.get("sort") ?? "execution_at",
    direction: dirRaw === "asc" ? "asc" : "desc",
    page: Number.isInteger(pageRaw) && pageRaw > 0 ? pageRaw : 1,
    pageSize: [10, 25, 50, 100].includes(sizeRaw) ? sizeRaw : 25,
    groupBy: dimRaw !== null && DIMENSIONS.includes(dimRaw) ? dimRaw : "buy_strategy",
    aggSort: params.get("asort") ?? "median_return",
    aggDirection: aggDirRaw === "asc" ? "asc" : "desc",
    selectedSymbol: params.get("sym"),
    compareIds: parseCompareIds(params.get("cmp")),
    search: params.toString(),
    setView: (view) => mutate((next) => next.set("view", view), true),
    patchFilters: (patch) =>
      mutate((next) => {
        writeFilters(next, { ...filtersFromParams(next), ...patch });
        next.delete("page");
      }),
    resetFilters: () =>
      mutate((next) => {
        writeFilters(next, {});
        next.delete("page");
      }),
    setSort: (sort, direction) =>
      mutate((next) => {
        const current = next.get("sort") ?? "execution_at";
        const currentDir = next.get("dir") === "asc" ? "asc" : "desc";
        next.set("sort", sort);
        next.set("dir", direction ?? (current === sort ? (currentDir === "asc" ? "desc" : "asc") : "desc"));
        next.delete("page");
      }),
    setPage: (page) => mutate((next) => next.set("page", String(page))),
    setPageSize: (size) =>
      mutate((next) => {
        next.set("size", String(size));
        next.delete("page");
      }),
    setGroupBy: (dimension) => mutate((next) => next.set("group", dimension)),
    setAggSort: (sort, direction) =>
      mutate((next) => {
        next.set("asort", sort);
        next.set("adir", direction);
      }),
    selectSymbol: (symbol) =>
      mutate((next) => {
        if (symbol === null) next.delete("sym");
        else next.set("sym", symbol);
      }),
    toggleCompare: (selectionId) =>
      mutate((next) => {
        const ids = parseCompareIds(next.get("cmp"));
        const updated = ids.includes(selectionId)
          ? ids.filter((id) => id !== selectionId)
          : ids.length < MAX_COMPARE
            ? [...ids, selectionId]
            : ids;
        if (updated.length === 0) next.delete("cmp");
        else next.set("cmp", updated.join(","));
      }),
    setCompare: (ids) =>
      mutate((next) => {
        if (ids.length === 0) next.delete("cmp");
        else next.set("cmp", ids.slice(0, MAX_COMPARE).join(","));
      }),
    drillDown: (nextFilters, view) =>
      mutate((next) => {
        writeFilters(next, nextFilters);
        next.delete("page");
        next.set("view", view);
      }, true),
  };
}
