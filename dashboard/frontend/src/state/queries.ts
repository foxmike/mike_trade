/** TanStack Query bindings for the read-only dashboard API. */

import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import {
  getAccountDetail,
  getHealth,
  getKpis,
  getStatus,
  getSymbolDetail,
  getTrade,
  getTrades,
  getWatchlist,
  listAccounts,
  type AccountDetailResponse,
  type AccountStatus,
  type HealthResponse,
  type KpisResponse,
  type SymbolDetailResponse,
  type TradeFilters,
  type TradeInspection,
  type TradeListResponse,
  type WatchlistResponse,
} from "../api/client";
import {
  resolveResearchCatalog,
  type RunQuery,
  type RunListResult,
} from "../api/research";
import {
  getMarketOverview,
  getMarketBenchmarks,
  getMarketTransitions,
  getMarketBars,
  getMarketConfig,
  type MarketStatusOverview,
  type BenchmarkSymbol,
  type TransitionsList,
  type MarketBarsResponse,
  type ThresholdsConfig,
} from "../api/market";

export const POLL_INTERVAL_MS = 5000;

export function useAccounts() {
  return useQuery({
    queryKey: ["accounts"],
    queryFn: ({ signal }) => listAccounts(signal),
    refetchInterval: 15000,
    staleTime: 10000,
  });
}

export function useStatus(accountId: string | null): UseQueryResult<AccountStatus> {
  return useQuery({
    queryKey: ["status", accountId],
    queryFn: ({ signal }) => getStatus(accountId as string, signal),
    enabled: accountId !== null,
    refetchInterval: POLL_INTERVAL_MS,
    retry: 1,
  });
}

export function useKpis(accountId: string | null): UseQueryResult<KpisResponse> {
  return useQuery({
    queryKey: ["kpis", accountId],
    queryFn: ({ signal }) => getKpis(accountId as string, signal),
    enabled: accountId !== null,
    refetchInterval: POLL_INTERVAL_MS,
    retry: 1,
  });
}

export function useWatchlist(accountId: string | null): UseQueryResult<WatchlistResponse> {
  return useQuery({
    queryKey: ["watchlist", accountId],
    queryFn: ({ signal }) => getWatchlist(accountId as string, signal),
    enabled: accountId !== null,
    refetchInterval: POLL_INTERVAL_MS,
    retry: 1,
  });
}

export function useHealth(accountId: string | null): UseQueryResult<HealthResponse> {
  return useQuery({
    queryKey: ["health", accountId],
    queryFn: ({ signal }) => getHealth(accountId as string, signal),
    enabled: accountId !== null,
    refetchInterval: POLL_INTERVAL_MS,
    retry: 1,
  });
}

export function useAccountDetail(accountId: string | null): UseQueryResult<AccountDetailResponse> {
  return useQuery({
    queryKey: ["account-detail", accountId],
    queryFn: ({ signal }) => getAccountDetail(accountId as string, signal),
    enabled: accountId !== null,
    refetchInterval: POLL_INTERVAL_MS,
    retry: 1,
  });
}

export function useSymbolDetail(
  accountId: string | null,
  symbol: string | undefined,
): UseQueryResult<SymbolDetailResponse> {
  return useQuery({
    queryKey: ["symbol-detail", accountId, symbol],
    queryFn: ({ signal }) => getSymbolDetail(accountId as string, symbol as string, signal),
    enabled: accountId !== null && symbol !== undefined,
    refetchInterval: POLL_INTERVAL_MS,
    retry: 1,
  });
}

export function useTrades(
  accountId: string | null,
  filters: TradeFilters,
): UseQueryResult<TradeListResponse> {
  return useQuery({
    queryKey: ["trades", accountId, filters],
    queryFn: ({ signal }) => getTrades(accountId as string, filters, signal),
    enabled: accountId !== null,
    retry: 1,
  });
}

export function useTradeInspection(
  accountId: string | null,
  tradeId: string | undefined,
): UseQueryResult<TradeInspection> {
  return useQuery({
    queryKey: ["trade-inspection", accountId, tradeId],
    queryFn: ({ signal }) => getTrade(accountId as string, tradeId as string, signal),
    enabled: accountId !== null && tradeId !== undefined,
    retry: 1,
  });
}

export function useResearchRuns(query: RunQuery): UseQueryResult<RunListResult> {
  return useQuery({
    queryKey: ["research-runs", query],
    queryFn: ({ signal }) =>
      resolveResearchCatalog().then((catalog) => catalog.listRuns(query, signal)),
    staleTime: 30000,
    retry: 1,
  });
}

export function useResearchDatasets() {
  return useQuery({
    queryKey: ["research-datasets"],
    queryFn: ({ signal }) =>
      resolveResearchCatalog().then((catalog) => catalog.getDatasets(signal)),
    staleTime: 30000,
    retry: 1,
  });
}

export function useResearchRun(selectionId: string | undefined) {
  return useQuery({
    queryKey: ["research-run", selectionId],
    queryFn: ({ signal }) =>
      resolveResearchCatalog().then((catalog) => catalog.getRun(selectionId as string, signal)),
    enabled: selectionId !== undefined,
    staleTime: 30000,
    retry: 1,
  });
}

const emptyTradeList: TradeListResponse = {
  items: [],
  page: 1,
  page_size: 25,
  total: 0,
  pages: 1,
  available_entry_strategies: [],
  available_exit_reasons: [],
};

export function useResearchTrades(selectionId: string | undefined, filters: TradeFilters) {
  return useQuery({
    queryKey: ["research-trades", selectionId, filters],
    queryFn: ({ signal }) =>
      selectionId === undefined
        ? Promise.resolve(emptyTradeList)
        : resolveResearchCatalog().then((catalog) => catalog.listRunTrades(selectionId, filters, signal)),
    enabled: selectionId !== undefined,
    retry: 1,
  });
}

export function useResearchTradeInspection(
  selectionId: string | undefined,
  tradeId: string | undefined,
) {
  return useQuery({
    queryKey: ["research-trade-inspection", selectionId, tradeId],
    queryFn: ({ signal }) =>
      resolveResearchCatalog().then((catalog) =>
        catalog.getRunTrade(selectionId as string, tradeId as string, signal),
      ),
    enabled: selectionId !== undefined && tradeId !== undefined,
    retry: 1,
  });
}

export function useMarketOverview(accountId: string | null): UseQueryResult<MarketStatusOverview> {
  return useQuery({
    queryKey: ["market-overview", accountId],
    queryFn: ({ signal }) => getMarketOverview(accountId, signal),
    refetchInterval: POLL_INTERVAL_MS,
    retry: 1,
  });
}

export function useMarketBenchmarks(accountId: string | null): UseQueryResult<BenchmarkSymbol[]> {
  return useQuery({
    queryKey: ["market-benchmarks", accountId],
    queryFn: ({ signal }) => getMarketBenchmarks(accountId, signal),
    refetchInterval: POLL_INTERVAL_MS,
    retry: 1,
  });
}

export function useMarketTransitions(
  accountId: string | null,
  limit: number = 50,
): UseQueryResult<TransitionsList> {
  return useQuery({
    queryKey: ["market-transitions", accountId, limit],
    queryFn: ({ signal }) => getMarketTransitions(accountId, limit, signal),
    refetchInterval: POLL_INTERVAL_MS,
    retry: 1,
  });
}

export function useMarketBars(
  accountId: string | null,
  symbol?: string | null,
  limit: number = 120,
): UseQueryResult<MarketBarsResponse> {
  return useQuery({
    queryKey: ["market-bars", accountId, symbol, limit],
    queryFn: ({ signal }) => getMarketBars(accountId, symbol, limit, signal),
    refetchInterval: POLL_INTERVAL_MS,
    retry: 1,
  });
}

export function useMarketConfig(): UseQueryResult<ThresholdsConfig> {
  return useQuery({
    queryKey: ["market-config"],
    queryFn: ({ signal }) => getMarketConfig(signal),
    staleTime: 60000,
    retry: 1,
  });
}

