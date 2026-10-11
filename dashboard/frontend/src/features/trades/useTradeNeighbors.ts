/** Previous / next trade (chronological by entry time) within the same account
 * or research run. Two tiny bounded queries keep this exact regardless of how
 * many trades exist; no list state is shared with the journal page. */

import { useMemo } from "react";
import type { TradeRecord } from "../../api/client";
import { useResearchTrades, useTrades } from "../../state/queries";

export interface Neighbors {
  previous: TradeRecord | null;
  next: TradeRecord | null;
}

export function useTradeNeighbors(
  source: "account" | "backtest",
  accountId: string | null,
  runId: string | undefined,
  current: TradeRecord | null,
): Neighbors {
  const entry = current?.entry_timestamp ?? null;
  const nextFilters = useMemo(
    () => ({ page: 1, pageSize: 6, sort: "entry_timestamp", direction: "asc", dateFrom: entry ?? undefined }),
    [entry],
  );
  const prevFilters = useMemo(
    () => ({ page: 1, pageSize: 6, sort: "entry_timestamp", direction: "desc", dateTo: entry ?? undefined }),
    [entry],
  );
  const enabled = entry !== null;
  const accountNext = useTrades(source === "account" && enabled ? accountId : null, nextFilters);
  const accountPrev = useTrades(source === "account" && enabled ? accountId : null, prevFilters);
  const runNext = useResearchTrades(source === "backtest" && enabled ? runId : undefined, nextFilters);
  const runPrev = useResearchTrades(source === "backtest" && enabled ? runId : undefined, prevFilters);

  return useMemo(() => {
    if (current === null || entry === null) return { previous: null, next: null };
    const nextItems = (source === "account" ? accountNext.data : runNext.data)?.items ?? [];
    const prevItems = (source === "account" ? accountPrev.data : runPrev.data)?.items ?? [];
    const at = Date.parse(entry);
    const time = (t: TradeRecord) => (t.entry_timestamp === null ? NaN : Date.parse(t.entry_timestamp));
    // Same-instant trades are ordered by id so neither direction skips them.
    const next = nextItems.find(
      (t) => t.trade_id !== current.trade_id && (time(t) > at || (time(t) === at && t.trade_id > current.trade_id)),
    );
    const previous = prevItems.find(
      (t) => t.trade_id !== current.trade_id && (time(t) < at || (time(t) === at && t.trade_id < current.trade_id)),
    );
    return { previous: previous ?? null, next: next ?? null };
  }, [source, current, entry, accountNext.data, accountPrev.data, runNext.data, runPrev.data]);
}
