/** Cross-run performance exploration. Every figure is a statistic over
 * independent runs (each against its own starting capital); capital, P&L and
 * returns are never summed across runs. */

import { Link } from "react-router";
import type { AggregateDimension, AggregateGroup, ResearchRunRow } from "../../api/research";
import { EmptyState, ErrorBanner, Loading, SortHeader } from "../../components/ui";
import { Banner, FilterPanel, StrategyChips, runHref } from "./components";
import { useAggregate, useRunFacets, useRunPage } from "./hooks";
import type { LabState } from "./labState";
import { RangeStrip } from "./charts";
import { fixed, plainPercent, profitFactor, shortDate, signClass, signedPercent } from "./researchFormat";
import { activeFilterCount, DIMENSION_FILTER } from "./urlState";

const QUESTIONS: Array<{ dimension: AggregateDimension; tab: string; question: string; keyLabel: string }> = [
  { dimension: "buy_strategy", tab: "BUY strategies", question: "Which BUY strategies have performed best?", keyLabel: "BUY strategy" },
  { dimension: "symbol", tab: "Stocks", question: "How does performance vary across stocks?", keyLabel: "Symbol" },
  { dimension: "stop_policy", tab: "Stop policies", question: "How do different stops affect the results?", keyLabel: "Stop policy" },
  { dimension: "window", tab: "Historical periods", question: "Which experiments used comparable periods?", keyLabel: "Historical window" },
  { dimension: "sell_strategy", tab: "SELL strategies", question: "How do the SELL strategies compare?", keyLabel: "SELL strategy" },
  { dimension: "short_strategy", tab: "SHORT strategies", question: "How do SHORT strategies compare?", keyLabel: "SHORT strategy" },
  { dimension: "cover_strategy", tab: "COVER strategies", question: "How do COVER strategies compare?", keyLabel: "COVER strategy" },
  { dimension: "period_role", tab: "Period roles", question: "How do results differ by period role?", keyLabel: "Period role" },
  { dimension: "run_purpose", tab: "Run purposes", question: "How do results differ by run purpose?", keyLabel: "Run purpose" },
];

function drillFilters(dimension: AggregateDimension, group: AggregateGroup, lab: LabState) {
  const field = DIMENSION_FILTER[dimension];
  if (field === undefined || group.key === "UNRECORDED") return null;
  return { ...lab.filters, [field]: group.key };
}

function MiniRunList({ runs, lab, metric }: { runs: ResearchRunRow[]; lab: LabState; metric: "drawdown" | "concentration" }) {
  if (runs.length === 0) return <p className="hint">No runs in this scope.</p>;
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>Run</th>
            <th className="right">Trades</th>
            <th className="right">Return</th>
            <th className="right">{metric === "drawdown" ? "Max DD" : "Top-3 share"}</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.selection_id}>
              <td>
                <Link className="cell-link" to={runHref(run.selection_id, lab.search)}>
                  {run.symbols.join(", ")}
                </Link>
                <StrategyChips run={run} compact />
              </td>
              <td className="right">{run.completed_trade_count}</td>
              <td className={`right ${signClass(run.return_percent)}`}>{signedPercent(run.return_percent)}</td>
              <td className="right">
                {metric === "drawdown" ? `${fixed(run.max_drawdown)}%` : plainPercent(run.trade_stats.top3_profit_share, 0)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function PerformanceTab({ lab }: { lab: LabState }) {
  const facets = useRunFacets();
  const question = QUESTIONS.find((item) => item.dimension === lab.groupBy) ?? QUESTIONS[0];
  const aggregate = useAggregate({
    ...lab.filters,
    groupBy: lab.groupBy,
    sort: lab.aggSort,
    direction: lab.aggDirection,
    limit: 200,
  });
  const drawdowns = useRunPage({ ...lab.filters, page: 1, pageSize: 8, sort: "max_drawdown", direction: "desc" });
  const concentrationMin = Math.max(lab.filters.minTrades ?? 0, 10);
  const concentrated = useRunPage({
    ...lab.filters,
    minTrades: concentrationMin,
    page: 1,
    pageSize: 8,
    sort: "top3_share",
    direction: "desc",
  });

  const groups = aggregate.data?.groups ?? [];
  const lows = groups.flatMap((g) => (g.min_return === null ? [] : [g.min_return]));
  const highs = groups.flatMap((g) => (g.max_return === null ? [] : [g.max_return]));
  const domain: [number, number] = [Math.min(0, ...lows), Math.max(0, ...highs)];

  const onSort = (field: string) =>
    lab.setAggSort(field, lab.aggSort === field && lab.aggDirection === "desc" ? "asc" : "desc");
  const filterCount = activeFilterCount(lab.filters);

  return (
    <>
      <details className="rl-scope" open={filterCount > 0 ? undefined : false}>
        <summary>
          Scope: {filterCount === 0 ? "all indexed runs" : `${String(filterCount)} filter${filterCount === 1 ? "" : "s"} active`}
          {aggregate.data ? ` · ${aggregate.data.runs_considered.toLocaleString("en-US")} runs considered` : ""}
        </summary>
        <FilterPanel filters={lab.filters} facets={facets.data} onPatch={lab.patchFilters} onReset={lab.resetFilters} />
      </details>

      <div className="rl-questions" role="tablist" aria-label="Performance question">
        {QUESTIONS.map((item) => (
          <button
            key={item.dimension}
            type="button"
            role="tab"
            aria-selected={item.dimension === lab.groupBy}
            className={item.dimension === lab.groupBy ? "active" : ""}
            onClick={() => lab.setGroupBy(item.dimension)}
          >
            {item.tab}
          </button>
        ))}
      </div>

      <section className="panel">
        <div className="panel-heading">
          <h2>{question?.question}</h2>
        </div>
        <Banner tone="info" title="Run-level statistics, not pooled results">
          Each row summarizes the individual runs in the group: medians and quartiles of return, profit factor and
          drawdown. Runs are measured against their own starting capital, so P&amp;L and returns are never added together.
          Trade counts are additive. {aggregate.data?.groups.some((g) => g.distinct_outcomes < g.run_count)
            ? "Some groups contain identical reruns; “distinct outcomes” shows how many runs actually differ."
            : ""}
        </Banner>
        {aggregate.isPending ? <Loading label="Aggregating runs…" /> : null}
        {aggregate.isError ? (
          <ErrorBanner title="Aggregate unavailable" message={aggregate.error instanceof Error ? aggregate.error.message : "Aggregate failed"} />
        ) : null}
        {aggregate.data !== undefined && groups.length === 0 ? (
          <EmptyState
            message="No runs in this scope have this dimension recorded."
            hint="For example, SHORT/COVER strategies and stop policies are only present on newer runs."
          />
        ) : null}
        {groups.length > 0 ? (
          <div className="table-wrap">
            <table className="data-table rl-table">
              <thead>
                <tr>
                  <th scope="col">{question?.keyLabel}</th>
                  <SortHeader label="Runs" field="run_count" sort={lab.aggSort} direction={lab.aggDirection} onSort={onSort} align="right" />
                  <SortHeader label="Trades" field="trade_count" sort={lab.aggSort} direction={lab.aggDirection} onSort={onSort} align="right" />
                  <SortHeader label="Profitable runs" field="profitable_rate" sort={lab.aggSort} direction={lab.aggDirection} onSort={onSort} align="right" />
                  <SortHeader label="Median return" field="median_return" sort={lab.aggSort} direction={lab.aggDirection} onSort={onSort} align="right" />
                  <th scope="col" title="Whisker: min–max of run returns · box: quartiles · tick: median">Run return range</th>
                  <SortHeader label="Median PF" field="median_profit_factor" sort={lab.aggSort} direction={lab.aggDirection} onSort={onSort} align="right" />
                  <th scope="col" className="right" style={{ textAlign: "right" }}>Median win rate</th>
                  <SortHeader label="Median max DD" field="median_drawdown" sort={lab.aggSort} direction={lab.aggDirection} onSort={onSort} align="right" />
                  <SortHeader label="Worst DD" field="worst_drawdown" sort={lab.aggSort} direction={lab.aggDirection} onSort={onSort} align="right" />
                  <th scope="col" className="right" style={{ textAlign: "right" }} title="Share of gross profit from each run's three largest winning trades">Top-3 trade share (median / max)</th>
                  <th scope="col">Drill down</th>
                </tr>
              </thead>
              <tbody>
                {groups.map((group) => {
                  const drill = question ? drillFilters(question.dimension, group, lab) : null;
                  const median = group.median_return;
                  return (
                    <tr key={group.key}>
                      <td className="cell-symbol">
                        {group.key === "UNRECORDED" ? <span className="unavailable">Not recorded</span> : group.key}
                        {group.first_window_start && lab.groupBy !== "window" ? (
                          <span className="cell-sub">{shortDate(group.first_window_start)} → {shortDate(group.last_window_end)}</span>
                        ) : null}
                      </td>
                      <td className="right">
                        {group.run_count}
                        {group.distinct_outcomes < group.run_count ? (
                          <span className="cell-sub">{group.distinct_outcomes} distinct</span>
                        ) : null}
                      </td>
                      <td className="right">{group.trade_count.toLocaleString("en-US")}</td>
                      <td className="right">{plainPercent(group.profitable_run_rate, 0)}</td>
                      <td className={`right ${signClass(median)}`}>{signedPercent(median)}</td>
                      <td>
                        <RangeStrip
                          domain={domain}
                          min={group.min_return}
                          p25={group.p25_return}
                          median={group.median_return}
                          p75={group.p75_return}
                          max={group.max_return}
                        />
                      </td>
                      <td className="right">
                        {profitFactor(group.median_profit_factor)}
                        {group.profit_factor_runs < group.run_count ? (
                          <span className="cell-sub">{group.profit_factor_runs}/{group.run_count} runs</span>
                        ) : null}
                      </td>
                      <td className="right">{plainPercent(group.median_win_rate)}</td>
                      <td className="right">{fixed(group.median_drawdown)}%</td>
                      <td className="right">{fixed(group.worst_drawdown)}%</td>
                      <td className="right">
                        {group.median_top3_share === null ? "—" : `${fixed(group.median_top3_share, 0)}% / ${fixed(group.max_top3_share, 0)}%`}
                      </td>
                      <td className="rl-actions">
                        {drill !== null ? (
                          <button type="button" className="link-button" onClick={() => lab.drillDown(drill, "explorer")}>
                            Runs
                          </button>
                        ) : (
                          <span className="unavailable">—</span>
                        )}
                        {group.best_run_id && group.worst_run_id && group.best_run_id !== group.worst_run_id ? (
                          <button
                            type="button"
                            className="link-button"
                            onClick={() => {
                              lab.setCompare([group.best_run_id as string, group.worst_run_id as string]);
                              lab.setView("compare");
                            }}
                          >
                            Best vs worst
                          </button>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
        {aggregate.data !== undefined ? (
          <ul className="rl-notes">
            {aggregate.data.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        ) : null}
      </section>

      <div className="panel-grid">
        <section className="panel">
          <div className="panel-heading"><h2>Deepest drawdowns</h2></div>
          <p className="hint">Largest peak-to-trough equity falls in scope (equity basis).</p>
          {drawdowns.isPending ? <Loading /> : <MiniRunList runs={drawdowns.data?.runs ?? []} lab={lab} metric="drawdown" />}
        </section>
        <section className="panel">
          <div className="panel-heading"><h2>Results concentrated in few trades</h2></div>
          <p className="hint">
            Share of gross profit from the three largest winning trades; runs with fewer than {concentrationMin} closed
            trades are excluded because tiny samples are trivially concentrated.
          </p>
          {concentrated.isPending ? <Loading /> : <MiniRunList runs={concentrated.data?.runs ?? []} lab={lab} metric="concentration" />}
        </section>
      </div>
    </>
  );
}
