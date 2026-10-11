/** Experiment explorer: server-paginated, server-filtered, server-sorted run table. */

import { Link, useNavigate } from "react-router";
import { EmptyState, ErrorBanner, Loading, Pagination, SortHeader } from "../../components/ui";
import { dateTimeEt } from "../../lib/format";
import { MAX_COMPARE } from "./urlState";
import { CopyId, FilterPanel, StopCell, StrategyChips, runHref } from "./components";
import { useRunFacets, useRunPage } from "./hooks";
import type { LabState } from "./labState";
import { fixed, plainPercent, profitFactor, signClass, signedMoney, signedPercent, windowText } from "./researchFormat";

const SORT_OPTIONS: Array<[string, string]> = [
  ["execution_at", "Execution time"],
  ["return_percent", "Return"],
  ["net_pnl", "Net P&L"],
  ["profit_factor", "Profit factor"],
  ["win_rate", "Win rate"],
  ["max_drawdown", "Max drawdown"],
  ["trade_count", "Trade count"],
  ["expectancy", "Expectancy"],
  ["top3_share", "Top-3 trade profit share"],
  ["symbols", "Symbol"],
];

export default function ExplorerTab({ lab }: { lab: LabState }) {
  const navigate = useNavigate();
  const facets = useRunFacets();
  const runsQuery = useRunPage({
    ...lab.filters,
    page: lab.page,
    pageSize: lab.pageSize,
    sort: lab.sort,
    direction: lab.direction,
  });
  const data = runsQuery.data;
  const runs = data?.runs ?? [];
  const error = runsQuery.isError
    ? runsQuery.error instanceof Error
      ? runsQuery.error.message
      : "Run list load failed"
    : null;
  const stale = runsQuery.isPlaceholderData;
  const trayFull = lab.compareIds.length >= MAX_COMPARE;

  return (
    <>
      <FilterPanel
        filters={lab.filters}
        facets={facets.data}
        facetsError={facets.isError ? (facets.error instanceof Error ? facets.error.message : "load failed") : null}
        onPatch={lab.patchFilters}
        onReset={lab.resetFilters}
      />

      {error !== null && runs.length === 0 ? (
        <ErrorBanner title="Research runs unavailable" message={error} hint="The run catalog needs the /api/v2/research endpoints." />
      ) : null}
      {runsQuery.isPending && !runsQuery.isError ? <Loading label="Loading experiments…" /> : null}
      {data !== undefined && runs.length === 0 && error === null ? (
        <EmptyState message="No experiments match these filters." hint="Loosen or clear a filter; options listed above come from the full catalog." />
      ) : null}

      {runs.length > 0 ? (
        <div className={`panel${stale ? " rl-stale" : ""}`} aria-busy={stale}>
          <div className="panel-heading">
            <h2>
              {data?.total.toLocaleString("en-US")} experiment{data?.total === 1 ? "" : "s"}
            </h2>
            <div className="panel-actions">
              <label className="inline-select">
                Sort
                <select value={lab.sort} onChange={(event) => lab.setSort(event.target.value, "desc")}>
                  {SORT_OPTIONS.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <button type="button" onClick={() => lab.setSort(lab.sort, lab.direction === "asc" ? "desc" : "asc")}>
                {lab.direction === "asc" ? "Ascending ▲" : "Descending ▼"}
              </button>
            </div>
          </div>
          <div className="table-wrap">
            <table className="data-table research-table rl-table">
              <thead>
                <tr>
                  <th scope="col" className="rl-check-col">
                    <span className="rl-visually-hidden">Select for comparison</span>
                  </th>
                  <SortHeader label="Executed (ET)" field="execution_at" sort={lab.sort} direction={lab.direction} onSort={(field) => lab.setSort(field)} />
                  <SortHeader label="Symbols" field="symbols" sort={lab.sort} direction={lab.direction} onSort={(field) => lab.setSort(field)} />
                  <th scope="col">Historical window</th>
                  <th scope="col">Strategies</th>
                  <th scope="col">Stop policy</th>
                  <SortHeader label="Trades" field="trade_count" sort={lab.sort} direction={lab.direction} onSort={(field) => lab.setSort(field)} align="right" />
                  <SortHeader label="Net P&L" field="net_pnl" sort={lab.sort} direction={lab.direction} onSort={(field) => lab.setSort(field)} align="right" />
                  <SortHeader label="Return" field="return_percent" sort={lab.sort} direction={lab.direction} onSort={(field) => lab.setSort(field)} align="right" />
                  <SortHeader label="Win rate" field="win_rate" sort={lab.sort} direction={lab.direction} onSort={(field) => lab.setSort(field)} align="right" />
                  <SortHeader label="Profit factor" field="profit_factor" sort={lab.sort} direction={lab.direction} onSort={(field) => lab.setSort(field)} align="right" />
                  <SortHeader label="Max DD" field="max_drawdown" sort={lab.sort} direction={lab.direction} onSort={(field) => lab.setSort(field)} align="right" />
                  <th scope="col">Run identity</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => {
                  const selected = lab.compareIds.includes(run.selection_id);
                  const open = () => void navigate(runHref(run.selection_id, lab.search));
                  return (
                    <tr
                      key={run.selection_id}
                      className={`row-expandable${selected ? " rl-row-selected" : ""}`}
                      tabIndex={0}
                      onClick={open}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" && event.target === event.currentTarget) open();
                      }}
                    >
                      <td className="rl-check-col" onClick={(event) => event.stopPropagation()}>
                        <input
                          type="checkbox"
                          checked={selected}
                          disabled={!selected && trayFull}
                          onChange={() => lab.toggleCompare(run.selection_id)}
                          aria-label={`Compare ${run.symbols.join(", ")} run ${run.selection_id.slice(0, 8)}`}
                          title={!selected && trayFull ? `Up to ${String(MAX_COMPARE)} runs can be compared` : "Add to comparison"}
                        />
                      </td>
                      <td>
                        {run.execution_at === null ? (
                          <span className="unavailable" title="Legacy artifact: the original execution time was not recorded.">
                            Not recorded
                          </span>
                        ) : (
                          dateTimeEt(run.execution_at)
                        )}
                        {run.run_purpose || run.artifact_group ? (
                          <span className="cell-sub">{run.run_purpose ?? run.artifact_group}</span>
                        ) : null}
                      </td>
                      <td className="cell-symbol">{run.symbols.join(", ") || <span className="unavailable">n/a</span>}</td>
                      <td>
                        {windowText(run)}
                        {run.period_role ? <span className="cell-sub">{run.period_role}</span> : null}
                      </td>
                      <td className="cell-strategies">
                        <StrategyChips run={run} compact />
                      </td>
                      <td>
                        <StopCell run={run} />
                      </td>
                      <td className="right">{run.completed_trade_count.toLocaleString("en-US")}</td>
                      <td className={`right ${signClass(run.net_pnl)}`}>{signedMoney(run.net_pnl)}</td>
                      <td className={`right ${signClass(run.return_percent)}`}>{signedPercent(run.return_percent)}</td>
                      <td className="right">{run.completed_trade_count === 0 ? "—" : plainPercent(run.win_rate)}</td>
                      <td className="right">{profitFactor(run.profit_factor)}</td>
                      <td className="right">{fixed(run.max_drawdown)}%</td>
                      <td onClick={(event) => event.stopPropagation()}>
                        <CopyId value={run.execution_id ?? run.selection_id} />
                        {run.execution_identity_uncertain ? (
                          <span className="cell-sub" title="Identical result ledgers are listed once; the original execution cannot be recovered.">
                            legacy identity
                          </span>
                        ) : null}
                        {run.duplicate_artifact_count > 1 ? (
                          <span className="cell-sub">{run.duplicate_artifact_count} artifact copies</span>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <Pagination
            page={data?.page ?? 1}
            pages={data?.pages ?? 1}
            total={data?.total ?? 0}
            onPage={lab.setPage}
            pageSize={data?.page_size}
            onPageSize={lab.setPageSize}
          />
          <p className="table-note">
            Filtering, sorting and paging run on the server over the whole catalog; only this page of{" "}
            {runs.length} row(s) is loaded. Return, win rate and profit factor are per-run figures against each run's own
            starting capital. <Link to="/research?view=performance">Compare groups of runs in Performance</Link>.
          </p>
        </div>
      ) : null}

      {lab.compareIds.length > 0 ? (
        <div className="rl-tray" role="region" aria-label="Comparison tray">
          <span>
            <strong>{lab.compareIds.length}</strong> of {MAX_COMPARE} selected for comparison
          </span>
          <button type="button" onClick={() => lab.setCompare([])}>
            Clear
          </button>
          <button type="button" className="rl-primary" disabled={lab.compareIds.length < 2} onClick={() => lab.setView("compare")}>
            {lab.compareIds.length < 2 ? "Select one more run" : "Compare selected runs"}
          </button>
        </div>
      ) : null}
    </>
  );
}
