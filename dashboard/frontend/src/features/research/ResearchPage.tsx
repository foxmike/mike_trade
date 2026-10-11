/** Research Lab: backtest run explorer. Historical datasets, backtest runs and
 * the live watchlist are conceptually separate; this page only touches the
 * research run catalog. Server-side pagination/filtering when the backend
 * supplies it (v2), client-side over the full v1 list otherwise. */

import { useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { useResearchDatasets, useResearchRuns } from "../../state/queries";
import {
  EmptyState,
  ErrorBanner,
  Loading,
  PageHeader,
  Pagination,
  SortHeader,
  TechnicalDetails,
} from "../../components/ui";
import type { RunQuery } from "../../api/research";
import { researchFlavor } from "../../api/research";
import type { ResearchRun } from "../../api/client";
import { dateTimeEt, money, percent, shortId } from "../../lib/format";

const DEFAULT_PAGE_SIZE = 25;

type SortableField =
  | "execution_at"
  | "net_pnl"
  | "return_percent"
  | "win_rate"
  | "profit_factor"
  | "max_drawdown"
  | "trade_count"
  | "symbols";

const SORTABLE: SortableField[] = [
  "execution_at",
  "net_pnl",
  "return_percent",
  "win_rate",
  "profit_factor",
  "max_drawdown",
  "trade_count",
  "symbols",
];

export function useRunFilterOptions(runs: ResearchRun[] | undefined) {
  return useMemo(() => {
    const list = runs ?? [];
    return {
      buy: [...new Set(list.flatMap((run) => run.buy_strategies))].sort(),
      sell: [...new Set(list.flatMap((run) => run.sell_strategies))].sort(),
      purposes: [...new Set(list.map((run) => run.run_purpose ?? run.artifact_group ?? "legacy/unknown"))].sort(),
    };
  }, [runs]);
}

function stopConfigText(run: ResearchRun): string {
  const keys = Object.keys(run.stop_configuration);
  if (keys.length === 0) return "Unavailable";
  return keys
    .map((key) => `${key}=${JSON.stringify(run.stop_configuration[key])}`)
    .join(" · ");
}

export function RunMetricsCells({ run }: { run: ResearchRun }) {
  const pnl = run.net_pnl === null ? null : Number(run.net_pnl);
  return (
    <>
      <td className="right">{run.completed_trade_count}</td>
      <td className={`right ${pnl === null ? "" : pnl >= 0 ? "text-positive" : "text-negative"}`}>
        {money(run.net_pnl, true)}
      </td>
      <td className="right">{percent(run.return_percent)}</td>
      <td className="right">{percent(run.win_rate, 1)}</td>
      <td className="right">{run.profit_factor === null ? "\u2014" : Number(run.profit_factor).toFixed(2)}</td>
      <td className="right">{percent(run.max_drawdown)}</td>
    </>
  );
}

export default function ResearchPage() {
  const navigate = useNavigate();
  const [query, setQuery] = useState<RunQuery>({
    page: 1,
    pageSize: DEFAULT_PAGE_SIZE,
    sort: "execution_at",
    direction: "desc",
  });
  const [search, setSearch] = useState("");
  const [buyStrategy, setBuyStrategy] = useState("");
  const [sellStrategy, setSellStrategy] = useState("");
  const [purpose, setPurpose] = useState("");
  const [symbolFilter, setSymbolFilter] = useState("");

  const appliedQuery: RunQuery = useMemo(
    () => ({
      ...query,
      search: search || undefined,
      buyStrategy: buyStrategy || undefined,
      sellStrategy: sellStrategy || undefined,
      purpose: purpose || undefined,
      symbol: symbolFilter || undefined,
    }),
    [query, search, buyStrategy, sellStrategy, purpose, symbolFilter],
  );

  const runsQuery = useResearchRuns(appliedQuery);
  const datasetsQuery = useResearchDatasets();
  const options = useRunFilterOptions(runsQuery.data?.runs);

  const patch = (partial: Partial<RunQuery>) =>
    setQuery((current) => ({ ...current, ...partial, page: partial.page ?? 1 }));

  const onSort = (field: string) => {
    if (query.sort === field) {
      patch({ direction: query.direction === "asc" ? "desc" : "asc" });
    } else {
      patch({ sort: field, direction: field === "execution_at" ? "desc" : "desc" });
    }
  };

  const runs = runsQuery.data?.runs ?? [];
  const error = runsQuery.isError
    ? runsQuery.error instanceof Error
      ? runsQuery.error.message
      : "Research run list load failed"
    : null;
  const flavor = researchFlavor();

  return (
    <div className="page page-wide">
      <PageHeader
        title="Research Lab"
        subtitle="Backtest run explorer — historical datasets, backtest runs and the live watchlist are kept separate"
      />

      {datasetsQuery.data ? (
        <div className="panel">
          <h2>Historical dataset inventory</h2>
          <dl className="kv-grid kv-compact">
            <div><dt>Universe</dt><dd>{datasetsQuery.data.universe_id}</dd></div>
            <div><dt>Universe symbols</dt><dd>{datasetsQuery.data.total_universe_symbols}</dd></div>
            <div><dt>Datasets available</dt><dd>{datasetsQuery.data.available_dataset_symbols}</dd></div>
            <div><dt>Datasets missing</dt><dd>{datasetsQuery.data.missing_dataset_symbols}</dd></div>
            <div><dt>Symbols backtested</dt><dd>{datasetsQuery.data.backtested_symbols}</dd></div>
          </dl>
          <TechnicalDetails summary="Inspect universe availability">
            <div className="table-wrap">
              <table className="data-table">
                <thead><tr><th>Symbol</th><th>Dataset</th><th>Backtests</th><th>Status</th></tr></thead>
                <tbody>
                  {datasetsQuery.data.symbols.map((member) => (
                    <tr key={member.symbol}>
                      <td className="cell-symbol">{member.symbol}</td>
                      <td>{member.dataset_id ?? "Missing"}</td>
                      <td>{member.backtest_run_count}</td>
                      <td>{member.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </TechnicalDetails>
        </div>
      ) : null}
      {datasetsQuery.isError ? (
        <ErrorBanner
          title="Dataset inventory unavailable"
          message={datasetsQuery.error instanceof Error ? datasetsQuery.error.message : "Dataset inventory load failed"}
        />
      ) : null}

      <div className="filter-bar">
        <label>
          Search
          <input
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
              patch({ page: 1 });
            }}
            placeholder="Symbol, strategy, purpose, id"
          />
        </label>
        <label>
          Symbol
          <input
            value={symbolFilter}
            onChange={(event) => {
              setSymbolFilter(event.target.value.toUpperCase());
              patch({ page: 1 });
            }}
            placeholder="All"
            size={8}
          />
        </label>
        <label>
          BUY strategy
          <select value={buyStrategy} onChange={(event) => { setBuyStrategy(event.target.value); patch({ page: 1 }); }}>
            <option value="">All</option>
            {options.buy.map((name) => (
              <option key={name}>{name}</option>
            ))}
          </select>
        </label>
        <label>
          SELL strategy
          <select value={sellStrategy} onChange={(event) => { setSellStrategy(event.target.value); patch({ page: 1 }); }}>
            <option value="">All</option>
            {options.sell.map((name) => (
              <option key={name}>{name}</option>
            ))}
          </select>
        </label>
        <label>
          Purpose / group
          <select value={purpose} onChange={(event) => { setPurpose(event.target.value); patch({ page: 1 }); }}>
            <option value="">All</option>
            {options.purposes.map((name) => (
              <option key={name}>{name}</option>
            ))}
          </select>
        </label>
      </div>

      {error !== null && runs.length === 0 ? (
        <ErrorBanner title="Research runs unavailable" message={error} hint="The run catalog requires the research artifacts to be readable." />
      ) : null}
      {runsQuery.isPending && !runsQuery.isError ? <Loading label="Loading research runs…" /> : null}

      {runsQuery.data !== undefined && runs.length === 0 && !runsQuery.isError ? (
        <EmptyState message="No research runs match these filters." />
      ) : null}

      {runs.length > 0 ? (
        <div className="panel">
          <div className="table-wrap">
            <table className="data-table research-table">
              <thead>
                <tr>
                  <SortHeader label="Execution time (ET)" field="execution_at" sort={query.sort} direction={query.direction ?? "desc"} onSort={onSort} />
                  <th>Historical period</th>
                  <SortHeader label="Symbols" field="symbols" sort={query.sort} direction={query.direction ?? "desc"} onSort={onSort} />
                  <th>Strategies (BUY / SELL)</th>
                  <th>Stop configuration</th>
                  <SortHeader label="Trades" field="trade_count" sort={query.sort} direction={query.direction ?? "desc"} onSort={onSort} align="right" />
                  <SortHeader label="Net P&L" field="net_pnl" sort={query.sort} direction={query.direction ?? "desc"} onSort={onSort} align="right" />
                  <SortHeader label="Return" field="return_percent" sort={query.sort} direction={query.direction ?? "desc"} onSort={onSort} align="right" />
                  <SortHeader label="Win rate" field="win_rate" sort={query.sort} direction={query.direction ?? "desc"} onSort={onSort} align="right" />
                  <SortHeader label="Profit factor" field="profit_factor" sort={query.sort} direction={query.direction ?? "desc"} onSort={onSort} align="right" />
                  <SortHeader label="Max drawdown" field="max_drawdown" sort={query.sort} direction={query.direction ?? "desc"} onSort={onSort} align="right" />
                  <th>Status / identity</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => {
                  const missing = run.execution_at === null;
                  return (
                    <tr
                      key={run.selection_id}
                      className="row-expandable"
                      tabIndex={0}
                      onClick={() => void navigate(`/research/runs/${encodeURIComponent(run.selection_id)}`)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") {
                          void navigate(`/research/runs/${encodeURIComponent(run.selection_id)}`);
                        }
                      }}
                    >
                      <td>
                        {missing ? (
                          <span className="unavailable">Execution time unavailable</span>
                        ) : (
                          dateTimeEt(run.execution_at)
                        )}
                        {run.completed_at ? (
                          <span className="cell-sub">completed {dateTimeEt(run.completed_at)}</span>
                        ) : null}
                      </td>
                      <td>
                        {run.window_start === null && run.window_end === null ? (
                          <span className="unavailable">Unavailable</span>
                        ) : (
                          <>
                            {(run.window_start ?? "").slice(0, 10)} – {(run.window_end ?? "").slice(0, 10)}
                            {run.period_role ? <span className="cell-sub">{run.period_role}</span> : null}
                          </>
                        )}
                      </td>
                      <td>{run.symbols.length > 0 ? run.symbols.join(", ") : <span className="unavailable">Unavailable</span>}</td>
                      <td className="cell-strategies">
                        <span className="cell-sub">BUY</span> {run.buy_strategies.join(", ") || <span className="unavailable">n/a</span>}
                        <br />
                        <span className="cell-sub">SELL</span> {run.sell_strategies.join(", ") || <span className="unavailable">n/a</span>}
                      </td>
                      <td>
                        <span className={Object.keys(run.stop_configuration).length === 0 ? "unavailable" : "cell-mono"}>
                          {stopConfigText(run)}
                        </span>
                      </td>
                      <RunMetricsCells run={run} />
                      <td>
                        {run.status}
                        {run.execution_identity_uncertain ? (
                          <span className="cell-sub">legacy execution identity unavailable</span>
                        ) : run.execution_id ? (
                          <span className="cell-sub">{shortId(run.execution_id)}</span>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <Pagination
            page={runsQuery.data?.page ?? 1}
            pages={runsQuery.data?.pages ?? 1}
            total={runsQuery.data?.total ?? 0}
            onPage={(page) => patch({ page })}
            pageSize={runsQuery.data?.page_size}
            onPageSize={(size) => patch({ pageSize: size, page: 1 })}
          />
          <TechnicalDetails summary="Catalog data source">
            <p>
              Active research catalog flavor: <strong>{flavor === "unresolved" ? "resolving…" : flavor}</strong>.{" "}
              {flavor === "v1"
                ? "Served from the existing /api/research endpoints; filters, sorting and pagination are applied client-side over the complete run list."
                : "Served from the new /api/v2/research endpoints with server-side pagination and filtering."}
              {" "}The dataset inventory above is independent of the completed-run catalog.
            </p>
          </TechnicalDetails>
        </div>
      ) : null}
    </div>
  );
}
