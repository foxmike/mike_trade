/** Side-by-side comparison of 2–5 experiments using the compare endpoint. */

import { Link } from "react-router";
import type { CompareResponse, ResearchRunRow } from "../../api/research";
import { EmptyState, ErrorBanner, Loading } from "../../components/ui";
import { Banner, StopCell, StrategyChips, runHref } from "./components";
import { ChartFrame, Legend, LineChart, MetricBars, type ChartSeries } from "./charts";
import { useCompare } from "./hooks";
import type { LabState } from "./labState";
import {
  COMPARE_COLORS,
  dateOnly,
  fixed,
  plainPercent,
  profitFactor,
  runLabel,
  shortDate,
  signClass,
  signedMoney,
  signedPercent,
  toNum,
  windowText,
} from "./researchFormat";

interface MetricRow {
  key: string;
  label: string;
  leaderKey?: string;
  value: (run: ResearchRunRow) => number | null;
  text: (run: ResearchRunRow) => string;
  /** Unit for deltas against the first run. */
  delta?: (diff: number) => string;
  className?: (run: ResearchRunRow) => string;
}

const METRICS: MetricRow[] = [
  {
    key: "return",
    label: "Return",
    leaderKey: "return_percent",
    value: (run) => toNum(run.return_percent),
    text: (run) => signedPercent(run.return_percent),
    delta: (d) => `${d > 0 ? "+" : ""}${d.toFixed(2)} pp`,
    className: (run) => signClass(run.return_percent),
  },
  {
    key: "pnl",
    label: "Net P&L (equity basis)",
    leaderKey: "net_pnl",
    value: (run) => toNum(run.net_pnl),
    text: (run) => signedMoney(run.net_pnl),
    delta: (d) => `${d < 0 ? "-" : "+"}$${Math.abs(d).toFixed(2)}`,
    className: (run) => signClass(run.net_pnl),
  },
  {
    key: "win",
    label: "Win rate",
    leaderKey: "win_rate",
    value: (run) => (run.completed_trade_count === 0 ? null : toNum(run.win_rate)),
    text: (run) => (run.completed_trade_count === 0 ? "—" : plainPercent(run.win_rate)),
    delta: (d) => `${d > 0 ? "+" : ""}${d.toFixed(1)} pp`,
  },
  {
    key: "pf",
    label: "Profit factor",
    leaderKey: "profit_factor",
    value: (run) => toNum(run.profit_factor),
    text: (run) => profitFactor(run.profit_factor),
    delta: (d) => `${d > 0 ? "+" : ""}${d.toFixed(2)}`,
  },
  {
    key: "dd",
    label: "Max drawdown",
    leaderKey: "max_drawdown",
    value: (run) => toNum(run.max_drawdown),
    text: (run) => `${fixed(run.max_drawdown)}%`,
    delta: (d) => `${d > 0 ? "+" : ""}${d.toFixed(2)} pp`,
  },
  {
    key: "trades",
    label: "Closed trades",
    leaderKey: "completed_trade_count",
    value: (run) => run.completed_trade_count,
    text: (run) => String(run.completed_trade_count),
    delta: (d) => `${d > 0 ? "+" : ""}${String(Math.round(d))}`,
  },
  {
    key: "exp",
    label: "Expectancy / trade",
    value: (run) => toNum(run.trade_stats.expectancy),
    text: (run) => signedMoney(run.trade_stats.expectancy),
    delta: (d) => `${d < 0 ? "-" : "+"}$${Math.abs(d).toFixed(2)}`,
    className: (run) => signClass(run.trade_stats.expectancy),
  },
  {
    key: "avgwin",
    label: "Average win",
    value: (run) => toNum(run.trade_stats.average_win),
    text: (run) => signedMoney(run.trade_stats.average_win),
  },
  {
    key: "avgloss",
    label: "Average loss",
    value: (run) => toNum(run.trade_stats.average_loss),
    text: (run) => signedMoney(run.trade_stats.average_loss),
  },
  {
    key: "top3",
    label: "Top-3 winners’ profit share",
    value: (run) => toNum(run.trade_stats.top3_profit_share),
    text: (run) => plainPercent(run.trade_stats.top3_profit_share, 0),
  },
];

const BAR_METRICS = ["return", "dd", "win", "pf"] as const;

function differs(compare: CompareResponse, field: string): boolean {
  return compare.differences.some((item) => item.field === field);
}

function EquityOverlay({ compare }: { compare: CompareResponse }) {
  const sameWindow = compare.comparative_metrics.period.identical_windows === true;
  const series: ChartSeries[] = compare.runs.map((run, index) => {
    const raw = compare.curves[run.selection_id] ?? [];
    const first = raw[0] === undefined ? 0 : Date.parse(raw[0].time);
    return {
      id: run.selection_id,
      label: runLabel(run),
      color: COMPARE_COLORS[index % COMPARE_COLORS.length] ?? "#4da3ff",
      points: raw.map((point) => ({
        // Identical windows share a calendar axis; otherwise align on elapsed days.
        x: sameWindow ? Date.parse(point.time) : (Date.parse(point.time) - first) / 86_400_000,
        y: point.index,
      })),
    };
  });
  return (
    <ChartFrame
      title="Equity, indexed to 100 at starting capital"
      caption={sameWindow ? "Shared calendar axis" : "Aligned by elapsed days — runs cover different windows"}
      footnote="Mark-to-market account equity from each run's recorded curve, rebased so runs with different capital are comparable."
    >
      <Legend series={series} />
      <LineChart
        series={series}
        height={260}
        baseline={100}
        xFormat={(x) => (sameWindow ? dateOnly(x) : `day ${x.toFixed(0)}`)}
        yFormat={(y) => y.toFixed(2)}
        ariaLabel="Overlaid normalized equity curves of the compared runs"
        emptyMessage="The compared runs recorded no equity curve."
      />
    </ChartFrame>
  );
}

export default function CompareTab({ lab }: { lab: LabState }) {
  const ids = lab.compareIds;
  const query = useCompare(ids);

  if (ids.length < 2) {
    return (
      <EmptyState
        message={ids.length === 0 ? "No experiments selected for comparison." : "Select at least one more experiment."}
        hint="Tick 2–5 experiments in the Experiments tab, or use “Best vs worst” in Performance."
      />
    );
  }
  if (query.isPending) return <Loading label="Comparing experiments…" />;
  if (query.isError || query.data === undefined) {
    return (
      <ErrorBanner
        title="Comparison unavailable"
        message={query.error instanceof Error ? query.error.message : "Compare failed"}
        hint="A selected run may no longer be in the catalog. Remove it and retry."
      />
    );
  }
  const compare = query.data;
  const runs = compare.runs;
  const period = compare.comparative_metrics.period;
  const leaders = compare.comparative_metrics.leaders;

  return (
    <>
      <div className="rl-compare-heads">
        {runs.map((run, index) => (
          <div key={run.selection_id} className="rl-compare-head" style={{ borderTopColor: COMPARE_COLORS[index % COMPARE_COLORS.length] }}>
            <strong>{run.symbols.join(", ") || "n/a"}</strong>
            <StrategyChips run={run} compact />
            <span className="cell-sub">{windowText(run)}</span>
            <span className="rl-head-actions">
              <Link className="cell-link" to={runHref(run.selection_id, lab.search)}>Open experiment →</Link>
              <button type="button" className="link-button" onClick={() => lab.toggleCompare(run.selection_id)}>Remove</button>
            </span>
          </div>
        ))}
      </div>

      {period.identical_windows ? (
        <Banner tone="info" title="Same historical window">
          All runs cover {windowText(runs[0] as ResearchRunRow)} ({period.overlap_days} days), so their returns are directly comparable.
        </Banner>
      ) : (
        <Banner tone="warning" title="Different historical windows">
          {period.reason ?? "The runs do not cover the same period."}{" "}
          {period.overlap_days ? `They overlap for ${String(period.overlap_days)} days (${shortDate(period.overlap_start)} → ${shortDate(period.overlap_end)}). ` : "They do not overlap. "}
          Returns reflect different market conditions; do not rank them as like-for-like.
        </Banner>
      )}

      <EquityOverlay compare={compare} />

      <section className="panel">
        <div className="panel-heading"><h2>Metrics</h2></div>
        <div className="rl-bar-grid">
          {BAR_METRICS.map((key) => {
            const metric = METRICS.find((m) => m.key === key);
            if (metric === undefined) return null;
            const leader = metric.leaderKey ? leaders[metric.leaderKey] : null;
            return (
              <div key={key}>
                <h3 className="rl-subhead">{metric.label}</h3>
                <MetricBars
                  ariaLabel={`${metric.label} by run`}
                  items={runs.map((run, index) => ({
                    id: run.selection_id,
                    label: run.symbols.join(", ") || `Run ${String(index + 1)}`,
                    color: COMPARE_COLORS[index % COMPARE_COLORS.length] ?? "#4da3ff",
                    value: metric.value(run),
                    text: metric.text(run),
                    best: leader === run.selection_id,
                  }))}
                />
              </div>
            );
          })}
        </div>
      </section>

      <section className="panel">
        <div className="panel-heading"><h2>Side by side</h2></div>
        <div className="table-wrap">
          <table className="data-table rl-compare-table">
            <thead>
              <tr>
                <th scope="col">Attribute</th>
                {runs.map((run, index) => (
                  <th key={run.selection_id} scope="col" style={{ borderTop: `3px solid ${COMPARE_COLORS[index % COMPARE_COLORS.length] ?? "#4da3ff"}` }}>
                    {run.symbols.join(", ")}
                    <span className="cell-sub">{run.selection_id.slice(0, 8)}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr className={differs(compare, "symbols") ? "rl-differs" : ""}>
                <th scope="row">Symbols</th>
                {runs.map((run) => <td key={run.selection_id}>{run.symbols.join(", ")}</td>)}
              </tr>
              <tr className={differs(compare, "window_key") ? "rl-differs" : ""}>
                <th scope="row">Historical period</th>
                {runs.map((run) => <td key={run.selection_id}>{windowText(run)}<span className="cell-sub">{run.period_role ?? ""}</span></td>)}
              </tr>
              <tr className={["buy_strategies", "sell_strategies", "short_strategies", "cover_strategies"].some((f) => differs(compare, f)) ? "rl-differs" : ""}>
                <th scope="row">Strategies</th>
                {runs.map((run) => <td key={run.selection_id}><StrategyChips run={run} compact /></td>)}
              </tr>
              <tr className={differs(compare, "stop_policy") ? "rl-differs" : ""}>
                <th scope="row">Stop policy</th>
                {runs.map((run) => <td key={run.selection_id}><StopCell run={run} /></td>)}
              </tr>
              <tr className={differs(compare, "dataset_id") ? "rl-differs" : ""}>
                <th scope="row">Dataset</th>
                {runs.map((run) => <td key={run.selection_id} className="cell-mono">{run.dataset_id ?? "Unavailable"}</td>)}
              </tr>
              <tr className={differs(compare, "starting_capital") ? "rl-differs" : ""}>
                <th scope="row">Starting capital</th>
                {runs.map((run) => <td key={run.selection_id}>{run.starting_capital ? signedMoney(run.starting_capital).replace("+", "") : "—"}</td>)}
              </tr>
              {METRICS.map((metric) => {
                const leader = metric.leaderKey ? leaders[metric.leaderKey] : null;
                const baseline = runs[0] === undefined ? null : metric.value(runs[0]);
                return (
                  <tr key={metric.key}>
                    <th scope="row">{metric.label}</th>
                    {runs.map((run, index) => {
                      const value = metric.value(run);
                      const diff = index > 0 && value !== null && baseline !== null ? value - baseline : null;
                      return (
                        <td key={run.selection_id} className={`${metric.className?.(run) ?? ""}${leader === run.selection_id ? " rl-best-cell" : ""}`}>
                          {metric.text(run)}
                          {diff !== null && metric.delta !== undefined && diff !== 0 ? (
                            <span className="cell-sub">{metric.delta(diff)} vs first</span>
                          ) : null}
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="table-note">
          Highlighted cells lead their metric (lowest wins for drawdown); a leader is only marked when values differ.
          Deltas are measured against the first run. Rows tinted amber differ between the runs. Win rate and expectancy
          use closed trades only; returns and net P&amp;L are equity-basis and include open positions.
        </p>
      </section>
    </>
  );
}
