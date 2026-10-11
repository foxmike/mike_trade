/** Research run detail: run summary + its backtest trade journal, with
 * navigation into the dedicated trade inspection view. */

import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { useResearchRun, useResearchTrades } from "../../state/queries";
import { ErrorBanner, Loading, PageHeader, Section, TechnicalDetails } from "../../components/ui";
import { TradeFilterBar, TradesTable, DEFAULT_TRADE_FILTERS } from "../trades/TradesTable";
import type { TradeFilters, TradeRecord } from "../../api/client";
import { dateEt, dateTimeEt, money, percent } from "../../lib/format";

const unavailable = (value: string | null | undefined): string =>
  value === null || value === undefined || value === "" ? "Unavailable" : value;

export default function ResearchRunDetailPage() {
  const { selectionId } = useParams<{ selectionId: string }>();
  const navigate = useNavigate();
  const runQuery = useResearchRun(selectionId);
  const [filters, setFilters] = useState<TradeFilters>({ ...DEFAULT_TRADE_FILTERS });
  const tradesQuery = useResearchTrades(selectionId, filters);

  const run = runQuery.data?.run ?? null;
  const patchFilter = (patch: Partial<TradeFilters>) =>
    setFilters((current) => ({ ...current, ...patch, page: patch.page ?? 1 }));

  const openTrade = (trade: TradeRecord) => {
    void navigate(
      `/trades/${encodeURIComponent(trade.trade_id)}?source=backtest&run=${encodeURIComponent(selectionId ?? "")}`,
    );
  };

  return (
    <div className="page page-wide">
      <PageHeader
        title={run === null ? "Research run" : `Run · ${run.symbols.join(", ")}`}
        subtitle={run === null ? undefined : `execution ${dateTimeEt(run.execution_at)} · historical window ${unavailable(run.window_start)} – ${unavailable(run.window_end)}`}
        actions={<Link className="button" to="/research">Back to Research Lab</Link>}
      />

      {runQuery.isPending && !runQuery.isError ? <Loading label="Loading run…" /> : null}
      {runQuery.isError ? (
        <ErrorBanner
          title="Research run unavailable"
          message={runQuery.error instanceof Error ? runQuery.error.message : "Run load failed"}
        />
      ) : null}

      {run !== null ? (
        <>
          <Section title="Run summary">
            <dl className="kv-grid">
              <div>
                <dt>Starting capital</dt>
                <dd>{money(run.starting_capital)}</dd>
              </div>
              <div>
                <dt>Final equity</dt>
                <dd>{money(run.final_equity)}</dd>
              </div>
              <div>
                <dt>Net P&L</dt>
                <dd className={run.net_pnl === null ? undefined : Number(run.net_pnl) >= 0 ? "text-positive" : "text-negative"}>
                  {money(run.net_pnl, true)}
                </dd>
              </div>
              <div>
                <dt>Return</dt>
                <dd>{percent(run.return_percent)}</dd>
              </div>
              <div>
                <dt>Win rate</dt>
                <dd>{percent(run.win_rate, 1)}</dd>
              </div>
              <div>
                <dt>Profit factor</dt>
                <dd>{run.profit_factor === null ? "—" : Number(run.profit_factor).toFixed(2)}</dd>
              </div>
              <div>
                <dt>Max drawdown</dt>
                <dd>{percent(run.max_drawdown)}</dd>
              </div>
              <div>
                <dt>Completed trades</dt>
                <dd>{run.completed_trade_count}</dd>
              </div>
              <div>
                <dt>Period role</dt>
                <dd>{run.period_role ?? "Unavailable"}</dd>
              </div>
              <div>
                <dt>Dataset</dt>
                <dd>{unavailable(run.dataset_id)}</dd>
              </div>
              <div>
                <dt>Execution mode</dt>
                <dd>{run.execution_mode}</dd>
              </div>
              <div>
                <dt>Status</dt>
                <dd>{run.status}</dd>
              </div>
            </dl>
            {run.execution_identity_uncertain ? (
              <div className="banner banner-warning" role="status">
                <strong>LEGACY RECORD</strong>
                <span>
                  This legacy record has no execution identifier. Identical result ledgers are listed
                  once; the original execution time cannot be recovered.
                </span>
              </div>
            ) : null}
            <TechnicalDetails summary="Configuration & provenance">
              <dl className="kv-grid kv-compact">
                <div>
                  <dt>BUY strategies</dt>
                  <dd>{run.buy_strategies.join(", ") || "Unavailable"}</dd>
                </div>
                <div>
                  <dt>SELL strategies</dt>
                  <dd>{run.sell_strategies.join(", ") || "Unavailable"}</dd>
                </div>
                <div>
                  <dt>Stop configuration</dt>
                  <dd className="cell-mono">
                    {Object.keys(run.stop_configuration).length === 0
                      ? "Unavailable"
                      : JSON.stringify(run.stop_configuration)}
                  </dd>
                </div>
                <div>
                  <dt>Dataset hash</dt>
                  <dd className="cell-mono">{unavailable(run.dataset_hash)}</dd>
                </div>
                <div>
                  <dt>Config fingerprint</dt>
                  <dd className="cell-mono">{unavailable(run.config_fingerprint)}</dd>
                </div>
                <div>
                  <dt>Artifact locations</dt>
                  <dd className="cell-mono">
                    {(runQuery.data?.artifact_locations ?? []).join("\n") || "Unavailable"}
                  </dd>
                </div>
              </dl>
              <details className="tech-details">
                <summary>Raw configuration JSON</summary>
                <pre>{JSON.stringify(runQuery.data?.configuration ?? {}, null, 2)}</pre>
              </details>
            </TechnicalDetails>
          </Section>

          <Section title="Backtest trades">
            <TradeFilterBar
              filters={filters}
              onPatch={patchFilter}
              availableEntryStrategies={tradesQuery.data?.available_entry_strategies ?? []}
              availableExitReasons={tradesQuery.data?.available_exit_reasons ?? []}
            />
            {tradesQuery.isError ? (
              <ErrorBanner
                title="Trade list unavailable"
                message={tradesQuery.error instanceof Error ? tradesQuery.error.message : "Unable to load trades"}
              />
            ) : null}
            <TradesTable
              list={tradesQuery.data}
              filters={filters}
              onPatch={patchFilter}
              loading={tradesQuery.isPending}
              emptyMessage="No trades recorded for this run with the current filters."
              onOpen={openTrade}
            />
          </Section>
        </>
      ) : null}
    </div>
  );
}
