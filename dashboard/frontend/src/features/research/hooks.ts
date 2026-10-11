/** Research Lab query hooks (read-only, server-side filtered and paginated). */

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  aggregateRuns,
  compareRunIds,
  getDatasetInventory,
  getRunAnalysis,
  getRunFacets,
  getRunJournal,
  resolveResearchCatalog,
  type AggregateQuery,
  type AggregateResponse,
  type JournalQuery,
  type JournalResponse,
  type RunListResult,
  type RunQuery,
} from "../../api/research";

export function useDatasetInventory() {
  return useQuery({
    queryKey: ["lab-datasets"],
    queryFn: ({ signal }) => getDatasetInventory(signal),
    staleTime: 30_000,
    retry: 1,
  });
}

export function useRunFacets() {
  return useQuery({
    queryKey: ["lab-facets"],
    queryFn: ({ signal }) => getRunFacets(signal),
    staleTime: 60_000,
    retry: 1,
  });
}

export function useRunPage(query: RunQuery, enabled = true) {
  return useQuery<RunListResult>({
    queryKey: ["lab-runs", query],
    queryFn: ({ signal }) =>
      resolveResearchCatalog().then((catalog) => catalog.listRuns(query, signal)),
    placeholderData: keepPreviousData,
    staleTime: 15_000,
    enabled,
    retry: 1,
  });
}

export function useAggregate(query: AggregateQuery) {
  return useQuery<AggregateResponse>({
    queryKey: ["lab-aggregate", query],
    queryFn: ({ signal }) => aggregateRuns(query, signal),
    placeholderData: keepPreviousData,
    staleTime: 30_000,
    retry: 1,
  });
}

export function useRunAnalysis(selectionId: string | undefined) {
  return useQuery({
    queryKey: ["lab-analysis", selectionId],
    queryFn: ({ signal }) => getRunAnalysis(selectionId as string, 1200, signal),
    enabled: selectionId !== undefined,
    staleTime: 60_000,
    retry: 1,
  });
}

export function useRunJournal(selectionId: string | undefined, query: JournalQuery) {
  return useQuery<JournalResponse>({
    queryKey: ["lab-journal", selectionId, query],
    queryFn: ({ signal }) => getRunJournal(selectionId as string, query, signal),
    enabled: selectionId !== undefined,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
    retry: 1,
  });
}

export function useCompare(selectionIds: string[]) {
  return useQuery({
    queryKey: ["lab-compare", selectionIds],
    queryFn: ({ signal }) => compareRunIds(selectionIds, signal),
    enabled: selectionIds.length >= 2,
    staleTime: 60_000,
    retry: 1,
  });
}
