/** Frozen historical research universe: dataset availability vs. backtest coverage. */

import { useMemo, useState } from "react";
import { Link } from "react-router";
import type { ResearchDatasetMember } from "../../api/research";
import { EmptyState, ErrorBanner, KpiTile, Loading, SortHeader } from "../../components/ui";
import { dateEt, dateTimeEt } from "../../lib/format";
import { Banner, CopyId, DebouncedInput, StopCell, StrategyChips, runHref } from "./components";
import { useDatasetInventory, useRunPage } from "./hooks";
import type { LabState } from "./labState";
import { plainPercent, profitFactor, shortDate, signClass, signedMoney, signedPercent } from "./researchFormat";

type StatusFilter = "ALL" | "DATASET_AND_BACKTEST" | "DATASET_ONLY" | "MISSING_DATASET" | "NO_BACKTESTS";

const STATUS_FILTERS: Array<[StatusFilter, string]> = [
  ["ALL", "All"],
  ["DATASET_AND_BACKTEST", "Dataset + backtests"],
  ["DATASET_ONLY", "Dataset, no backtests"],
  ["MISSING_DATASET", "Dataset missing"],
  ["NO_BACKTESTS", "No completed backtests"],
];

type SortField = "symbol" | "group" | "bars" | "start" | "end" | "runs";

function matches(member: ResearchDatasetMember, filter: StatusFilter): boolean {
  switch (filter) {
    case "ALL":
      return true;
    case "NO_BACKTESTS":
      return !member.has_backtest_results;
    default:
      return member.status === filter;
  }
}

function sortValue(member: ResearchDatasetMember, field: SortField): string | number {
  switch (field) {
    case "symbol":
      return member.symbol;
    case "group":
      return member.group ?? "";
    case "bars":
      return member.bar_count ?? -1;
    case "start":
      return member.start ?? "";
    case "end":
      return member.end ?? "";
    case "runs":
      return member.backtest_run_count;
  }
}

function SymbolDetail({ member, lab }: { member: ResearchDatasetMember; lab: LabState }) {
  const inventory = useDatasetInventory();
  const runs = useRunPage(
    { symbol: member.symbol, page: 1, pageSize: 8, sort: "return_percent", direction: "desc" },
    member.has_backtest_results,
  );
  const snapshots = (inventory.data?.snapshots ?? []).filter((snap) => snap.symbol.toUpperCase() === member.symbol);
  return (
    <section className="panel rl-symbol-detail" aria-label={`${member.symbol} datasets and experiments`}>
      <div className="panel-heading">
        <h2>{member.symbol}</h2>
        <div className="panel-actions">
          <button type="button" onClick={() => lab.drillDown({ symbol: member.symbol }, "explorer")} disabled={!member.has_backtest_results}>
            Open all {member.backtest_run_count} experiments
          </button>
          <button type="button" onClick={() => lab.selectSymbol(null)}>
            Close
          </button>
        </div>
      </div>
      <div className="rl-two-col">
        <div>
          <h3 className="rl-subhead">Historical datasets</h3>
          {snapshots.length === 0 ? (
            <Banner tone="warning" title="No dataset on disk">
              {member.symbol} is in the frozen research universe but its historical bars have not been acquired. This
              console never downloads data; acquire it with the research CLI.
            </Banner>
          ) : (
            <ul className="rl-snapshot-list">
              {snapshots.map((snap) => (
                <li key={snap.dataset_id}>
                  <strong>{snap.dataset_id}</strong>
                  <dl className="kv-grid kv-compact">
                    <div><dt>Provider / feed</dt><dd>{snap.provider} / {snap.feed.toUpperCase()}</dd></div>
                    <div><dt>Timeframe</dt><dd>{snap.timeframe}</dd></div>
                    <div><dt>Adjustment</dt><dd>{snap.adjustment}</dd></div>
                    <div><dt>Coverage</dt><dd>{shortDate(snap.start)} → {shortDate(snap.end)}</dd></div>
                    <div><dt>Bars</dt><dd>{snap.bar_count.toLocaleString("en-US")}</dd></div>
                    <div><dt>Acquired</dt><dd>{dateTimeEt(snap.acquired_at)}</dd></div>
                    <div><dt>SHA-256</dt><dd><CopyId value={snap.sha256} keep={14} /></dd></div>
                  </dl>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div>
          <h3 className="rl-subhead">Experiments on {member.symbol}</h3>
          {!member.has_backtest_results ? (
            <p className="hint">No completed backtests are indexed for this symbol.</p>
          ) : runs.isPending ? (
            <Loading label="Loading experiments…" />
          ) : runs.isError ? (
            <ErrorBanner message={runs.error instanceof Error ? runs.error.message : "Load failed"} />
          ) : (
            <>
              <p className="hint">
                Top {runs.data?.runs.length ?? 0} of {runs.data?.total ?? 0} by return. Each figure is that run's own result.
              </p>
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr><th>Strategies</th><th>Stop</th><th className="right">Trades</th><th className="right">Return</th><th className="right">Net P&L</th><th className="right">Win</th><th className="right">PF</th></tr>
                  </thead>
                  <tbody>
                    {(runs.data?.runs ?? []).map((run) => (
                      <tr key={run.selection_id}>
                        <td className="cell-strategies">
                          <Link className="cell-link" to={runHref(run.selection_id, lab.search)}><StrategyChips run={run} compact /></Link>
                        </td>
                        <td><StopCell run={run} /></td>
                        <td className="right">{run.completed_trade_count}</td>
                        <td className={`right ${signClass(run.return_percent)}`}>{signedPercent(run.return_percent)}</td>
                        <td className={`right ${signClass(run.net_pnl)}`}>{signedMoney(run.net_pnl)}</td>
                        <td className="right">{run.completed_trade_count === 0 ? "—" : plainPercent(run.win_rate)}</td>
                        <td className="right">{profitFactor(run.profit_factor)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      </div>
    </section>
  );
}

export default function UniverseTab({ lab }: { lab: LabState }) {
  const inventory = useDatasetInventory();
  const [status, setStatus] = useState<StatusFilter>("ALL");
  const [query, setQuery] = useState("");
  const [group, setGroup] = useState("");
  const [sort, setSort] = useState<{ field: SortField; direction: "asc" | "desc" }>({ field: "runs", direction: "desc" });

  const members = useMemo(() => inventory.data?.symbols ?? [], [inventory.data]);
  const groups = useMemo(() => [...new Set(members.map((m) => m.group ?? "general"))].sort(), [members]);
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const list = members.filter(
      (member) =>
        matches(member, status) &&
        (group === "" || (member.group ?? "general") === group) &&
        (needle === "" ||
          member.symbol.toLowerCase().includes(needle) ||
          (member.dataset_id ?? "").toLowerCase().includes(needle)),
    );
    const factor = sort.direction === "asc" ? 1 : -1;
    return [...list].sort((a, b) => {
      const av = sortValue(a, sort.field);
      const bv = sortValue(b, sort.field);
      if (av === bv) return a.symbol.localeCompare(b.symbol);
      return (av < bv ? -1 : 1) * factor;
    });
  }, [members, status, group, query, sort]);

  const counts = useMemo(() => {
    const tally = (filter: StatusFilter) => members.filter((m) => matches(m, filter)).length;
    return Object.fromEntries(STATUS_FILTERS.map(([id]) => [id, tally(id)])) as Record<StatusFilter, number>;
  }, [members]);

  const selected = members.find((member) => member.symbol === lab.selectedSymbol) ?? null;
  const onSort = (field: string) =>
    setSort((current) =>
      current.field === field
        ? { field: current.field, direction: current.direction === "asc" ? "desc" : "asc" }
        : { field: field as SortField, direction: field === "symbol" || field === "group" ? "asc" : "desc" },
    );
  const sortKey = sort.field;

  if (inventory.isPending) return <Loading label="Loading dataset inventory…" />;
  if (inventory.isError || inventory.data === undefined) {
    return (
      <ErrorBanner
        title="Dataset inventory unavailable"
        message={inventory.error instanceof Error ? inventory.error.message : "Inventory load failed"}
      />
    );
  }
  const data = inventory.data;
  const totalRuns = members.reduce((sum, member) => sum + member.backtest_run_count, 0);

  return (
    <>
      <Banner tone="info" title="Historical research universe">
        This is the frozen {data.total_universe_symbols}-symbol research universe ({data.universe_id}). It is separate
        from the small active SHADOW watchlist the engine trades. Missing datasets are shown, never downloaded, from this screen.
      </Banner>
      <div className="kpi-ribbon">
        <KpiTile label="Universe symbols" value={data.total_universe_symbols} meta={data.universe_id} />
        <KpiTile label="Dataset available" value={data.available_dataset_symbols} tone="positive" />
        <KpiTile label="Dataset missing" value={data.missing_dataset_symbols} tone={data.missing_dataset_symbols > 0 ? "negative" : undefined} />
        <KpiTile label="With backtests" value={data.backtested_symbols} meta={`${String(counts.NO_BACKTESTS)} without`} />
        <KpiTile label="Backtest runs" value={totalRuns.toLocaleString("en-US")} meta="a multi-symbol run counts per symbol" />
      </div>

      <div className="filter-bar">
        <label>
          Search symbol or dataset
          <DebouncedInput value={query} onCommit={setQuery} placeholder="e.g. NVDA" size={22} />
        </label>
        <label>
          Group
          <select value={group} onChange={(event) => setGroup(event.target.value)}>
            <option value="">All groups</option>
            {groups.map((name) => <option key={name}>{name}</option>)}
          </select>
        </label>
        <div className="rl-chips" role="group" aria-label="Status filter">
          {STATUS_FILTERS.map(([id, label]) => (
            <button key={id} type="button" className={status === id ? "active" : ""} aria-pressed={status === id} onClick={() => setStatus(id)}>
              {label} <span className="rl-count">{counts[id]}</span>
            </button>
          ))}
        </div>
      </div>

      {visible.length === 0 ? (
        <EmptyState message="No universe symbols match these filters." />
      ) : (
        <div className="panel">
          <div className="panel-heading"><h2>{visible.length} of {members.length} symbols</h2></div>
          <div className="table-wrap rl-universe-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <SortHeader label="Symbol" field="symbol" sort={sortKey} direction={sort.direction} onSort={onSort} />
                  <SortHeader label="Group" field="group" sort={sortKey} direction={sort.direction} onSort={onSort} />
                  <th scope="col">Dataset</th>
                  <SortHeader label="Backtests" field="runs" sort={sortKey} direction={sort.direction} onSort={onSort} align="right" />
                  <SortHeader label="Coverage start" field="start" sort={sortKey} direction={sort.direction} onSort={onSort} />
                  <SortHeader label="Coverage end" field="end" sort={sortKey} direction={sort.direction} onSort={onSort} />
                  <SortHeader label="Bars" field="bars" sort={sortKey} direction={sort.direction} onSort={onSort} align="right" />
                  <th scope="col">Feed</th>
                  <th scope="col">Timeframe</th>
                  <th scope="col">Dataset identity</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((member) => (
                  <tr
                    key={member.symbol}
                    className={`row-expandable${member.symbol === lab.selectedSymbol ? " rl-row-selected" : ""}`}
                    tabIndex={0}
                    aria-selected={member.symbol === lab.selectedSymbol}
                    onClick={() => lab.selectSymbol(member.symbol === lab.selectedSymbol ? null : member.symbol)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") lab.selectSymbol(member.symbol === lab.selectedSymbol ? null : member.symbol);
                    }}
                  >
                    <td className="cell-symbol">{member.symbol}</td>
                    <td>{member.group ?? "general"}</td>
                    <td>
                      <span className={`pill ${member.dataset_available ? "pill-green" : "pill-red"}`}>
                        {member.dataset_available ? "AVAILABLE" : "MISSING"}
                      </span>
                    </td>
                    <td className="right">
                      {member.has_backtest_results ? (
                        <span className="pill pill-neutral">{member.backtest_run_count} run{member.backtest_run_count === 1 ? "" : "s"}</span>
                      ) : (
                        <span className="unavailable">none</span>
                      )}
                    </td>
                    <td>{member.start ? dateEt(member.start) : <span className="unavailable">—</span>}</td>
                    <td>{member.end ? dateEt(member.end) : <span className="unavailable">—</span>}</td>
                    <td className="right">{member.bar_count === null ? "—" : member.bar_count.toLocaleString("en-US")}</td>
                    <td>{member.feed ? member.feed.toUpperCase() : "—"}</td>
                    <td>{member.timeframe ?? "—"}</td>
                    <td className="cell-mono">{member.dataset_id ?? <span className="unavailable">no dataset</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="table-note">
            Select a symbol to see its datasets and experiments. Coverage is the acquired window of the stored snapshot.
          </p>
        </div>
      )}
      {selected !== null ? <SymbolDetail member={selected} lab={lab} /> : null}
      {lab.selectedSymbol !== null && selected === null ? (
        <Banner tone="warning" title="Symbol not in inventory">
          {lab.selectedSymbol} is not part of the research universe inventory.
        </Banner>
      ) : null}
    </>
  );
}
