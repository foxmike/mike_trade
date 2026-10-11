/** Trade journal page. Two sources share one presentation:
 * - account trades (selected operational account scope)
 * - backtest trades of a selected research run
 * Opening a trade navigates to the dedicated inspection view (no more
 * below-the-fold detail panel). */

import { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { useResearchRuns, useResearchTrades, useTrades } from "../../state/queries";
import { useSelectedAccount } from "../../state/AccountContext";
import { ErrorBanner, Loading, PageHeader, Section } from "../../components/ui";
import { TradeFilterBar, TradesTable, DEFAULT_TRADE_FILTERS } from "./TradesTable";
import type { TradeFilters, TradeRecord } from "../../api/client";
import { dateTimeEt } from "../../lib/format";

type Source = "account" | "backtest";

export default function TradeJournalPage() {
  const navigate = useNavigate();
  const { selectedAccountId, accounts } = useSelectedAccount();
  const [params, setParams] = useSearchParams();
  const source: Source = params.get("source") === "backtest" ? "backtest" : "account";
  const [filters, setFilters] = useState<TradeFilters>({ ...DEFAULT_TRADE_FILTERS });

  // Backtest run selection: explicit ?run= wins, otherwise first run.
  const runsQuery = useResearchRuns({ page: 1, pageSize: 100, sort: "execution_at", direction: "desc" });
  const runs = runsQuery.data?.runs ?? [];
  const runParam = params.get("run");
  const [chosenRun, setChosenRun] = useState<string>("");
  const selectedRun = runParam ?? chosenRun ?? runs[0]?.selection_id ?? "";

  const setSource = (next: Source) => {
    const nextParams = new URLSearchParams(params);
    if (next === "backtest") nextParams.set("source", "backtest");
    else nextParams.delete("source");
    setParams(nextParams, { replace: true });
  };

  const chooseRun = (selectionId: string) => {
    setChosenRun(selectionId);
    const nextParams = new URLSearchParams(params);
    if (selectionId === "") nextParams.delete("run");
    else nextParams.set("run", selectionId);
    setParams(nextParams, { replace: true });
    setFilters({ ...DEFAULT_TRADE_FILTERS });
  };

  const tradesQuery = useTrades(source === "account" ? selectedAccountId : null, filters);
  const runTradesQuery = useResearchTrades(
    source === "backtest" && selectedRun !== "" ? selectedRun : undefined,
    filters,
  );

  const list = source === "account" ? tradesQuery.data : runTradesQuery.data;
  const loading = source === "account" ? tradesQuery.isPending : runTradesQuery.isPending;
  const error = source === "account" ? tradesQuery.error : runTradesQuery.error;

  const patchFilter = (patch: Partial<TradeFilters>) =>
    setFilters((current) => ({ ...current, ...patch, page: patch.page ?? 1 }));

  const selectedRunMeta = useMemo(
    () => runs.find((run) => run.selection_id === selectedRun) ?? null,
    [runs, selectedRun],
  );

  const openTrade = (trade: TradeRecord) => {
    const nextParams = new URLSearchParams();
    if (source === "account") {
      nextParams.set("source", "account");
    } else {
      nextParams.set("source", "backtest");
      nextParams.set("run", selectedRun);
    }
    void navigate(`/trades/${encodeURIComponent(trade.trade_id)}?${nextParams.toString()}`);
  };

  return (
    <div className="page page-wide">
      <PageHeader
        title="Trade Explorer"
        subtitle="Unified trade journal — account cycles and backtest trades · timestamps in America/New_York · click any trade for interactive candlestick charting and fill inspection"
      />

      <div className="source-switch">
        <button type="button" className={source === "account" ? "active" : ""} onClick={() => setSource("account")}>
          Account trades
        </button>
        <button type="button" className={source === "backtest" ? "active" : ""} onClick={() => setSource("backtest")}>
          Backtest trades
        </button>
      </div>

      {source === "backtest" ? (
        <Section title="Research run">
          {runsQuery.isPending ? <Loading label="Loading research runs…" /> : null}
          {runs.length === 0 && runsQuery.isSuccess ? (
            <p className="hint">No research runs available on this machine.</p>
          ) : null}
          {runs.length > 0 ? (
            <>
              <label className="inline-select">
                Selected execution
                <select value={selectedRun} onChange={(event) => chooseRun(event.target.value)}>
                  {runs.map((run) => (
                    <option key={run.selection_id} value={run.selection_id}>
                      {dateTimeEt(run.execution_at)} · {run.symbols.join(",")} · {run.completed_trade_count} trades
                    </option>
                  ))}
                </select>
              </label>
              {selectedRunMeta !== null ? (
                <p className="hint">
                  {selectedRunMeta.symbols.join(", ")} · historical window{" "}
                  {selectedRunMeta.window_start?.slice(0, 10) ?? "unknown"} –{" "}
                  {selectedRunMeta.window_end?.slice(0, 10) ?? "unknown"} ·{" "}
                  {selectedRunMeta.buy_strategies.join(", ") || "strategy attribution unavailable"} /{" "}
                  {selectedRunMeta.sell_strategies.join(", ") || "strategy attribution unavailable"}
                </p>
              ) : null}
            </>
          ) : null}
        </Section>
      ) : null}

      {source === "account" && selectedAccountId === null ? (
        <div className="state-block state-empty">
          <p>No operational account selected.</p>
          <p className="hint">
            Select an account in the header to browse its trade journal, or switch to{" "}
            <button type="button" className="link-button" onClick={() => setSource("backtest")}>
              Backtest trades
            </button>{" "}
            to inspect historical research runs.
          </p>
        </div>
      ) : (
        <Section
          title={source === "account" ? `Account trades${accounts.length > 0 ? ` (${selectedAccountId ?? ""})` : ""}` : "Backtest trades"}
        >
          <TradeFilterBar
            filters={filters}
            onPatch={patchFilter}
            availableEntryStrategies={list?.available_entry_strategies ?? []}
            availableExitReasons={list?.available_exit_reasons ?? []}
          />
          {error !== null ? (
            <ErrorBanner
              title="Trade list unavailable"
              message={error instanceof Error ? error.message : "Unable to load trades"}
            />
          ) : null}
          <TradesTable
            list={list}
            filters={filters}
            onPatch={patchFilter}
            loading={loading}
            emptyMessage="No trades match these filters."
            onOpen={openTrade}
          />
        </Section>
      )}
    </div>
  );
}
