/** Experiment analysis: identity, configuration, metrics, recorded-data charts
 * and the run's trade journal (with links into the trade inspection route). */

import { useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import type { JournalQuery, JournalTrade, RunAnalysis } from "../../api/research";
import { ErrorBanner, KpiTile, Loading, PageHeader, Pagination, Section, SortHeader, TechnicalDetails } from "../../components/ui";
import { dateTimeEt, money, percent, quantity } from "../../lib/format";
import { Banner, CopyId, StopCell, StrategyChips } from "./components";
import { BarChart, ChartFrame, Histogram, Legend, LineChart } from "./charts";
import { useRunAnalysis, useRunJournal } from "./hooks";
import {
  axisMoney,
  dateOnly,
  fixed,
  holdingText,
  plainPercent,
  profitFactor,
  signClass,
  signedMoney,
  signedPercent,
  toNum,
  windowDays,
  windowText,
} from "./researchFormat";
import "./research.css";

const unavailable = (value: string | null | undefined): string =>
  value === null || value === undefined || value === "" ? "Not recorded" : value;

function moneyAxis(min: number, max: number): (value: number) => string {
  const span = max - min;
  return (value) =>
    span < 60
      ? `$${value.toFixed(2)}`
      : new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(value);
}

function recordText(value: unknown): string {
  if (value === null || value === undefined) return "Not recorded";
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
      .join(" · ");
  }
  return String(value);
}

function Charts({ analysis }: { analysis: RunAnalysis }) {
  const [periodMetric, setPeriodMetric] = useState<"equity" | "realized">("equity");
  const run = analysis.run;
  const start = toNum(run.starting_capital);
  const equity = analysis.equity_curve;
  const equitySeries = useMemo(
    () => [{ id: "equity", label: "Account equity", color: "#4da3ff", points: equity.map((p) => ({ x: Date.parse(p.time), y: p.equity })) }],
    [equity],
  );
  const equityValues = equity.map((p) => p.equity);
  const eqFormat = moneyAxis(Math.min(...equityValues, start ?? Infinity), Math.max(...equityValues, start ?? -Infinity));
  const drawdownSeries = useMemo(
    () => [{ id: "dd", label: "Drawdown", color: "#e0665a", points: analysis.drawdown_curve.map((p) => ({ x: Date.parse(p.time), y: -p.drawdown_percent })) }],
    [analysis.drawdown_curve],
  );
  const tradeSeries = useMemo(
    () => [{ id: "cum", label: "Cumulative realized P&L", color: "#6fdc9f", step: true, points: analysis.trade_series.map((p) => ({ x: Date.parse(p.time), y: p.cumulative_pnl })) }],
    [analysis.trade_series],
  );
  const detail = analysis.drawdown_detail;
  const periods = analysis.period_performance.periods;
  const bars = periods.map((row) => ({
    label: row.period,
    value: toNum(periodMetric === "equity" ? row.equity_change : row.realized_pnl) ?? 0,
    note:
      periodMetric === "equity"
        ? `${String(row.closed_trades)} closed trade(s), realized ${signedMoney(row.realized_pnl)}`
        : `equity change ${signedMoney(row.equity_change)}`,
  }));
  const downsampled = analysis.equity_curve_total_points > equity.length;
  const granularity = analysis.period_performance.granularity;

  return (
    <>
      <ChartFrame
        title="Equity curve"
        caption="Mark-to-market account equity"
        footnote={
          <>
            {equity.length === 0
              ? "This run's artifact recorded no equity curve."
              : downsampled
                ? `Showing ${equity.length.toLocaleString("en-US")} of ${analysis.equity_curve_total_points.toLocaleString("en-US")} recorded points; each bucket keeps its high and low so peaks and troughs are not lost. `
                : `All ${equity.length.toLocaleString("en-US")} recorded points shown. `}
            Includes unrealized P&amp;L of open positions. The dashed line is starting capital.
          </>
        }
      >
        <LineChart
          series={equitySeries}
          height={250}
          baseline={start ?? undefined}
          xFormat={(x) => dateOnly(x)}
          yFormat={eqFormat}
          ariaLabel="Equity curve of the experiment"
          emptyMessage="No equity curve recorded for this run."
        />
      </ChartFrame>

      <ChartFrame
        title="Drawdown"
        caption="Percent below the running equity peak"
        footnote={
          detail.available
            ? `Maximum ${fixed(detail.max_drawdown_percent)}% (${money(detail.max_drawdown_amount)}) from ${money(detail.peak_equity)} on ${dateOnly(Date.parse(detail.peak_time ?? ""))} to ${money(detail.trough_equity)} on ${dateOnly(Date.parse(detail.trough_time ?? ""))}; ${detail.recovered ? `recovered ${dateOnly(Date.parse(detail.recovered_at ?? ""))}` : "not recovered by the end of the run"}. Peak starts at starting capital.`
            : "Drawdown requires a recorded equity curve."
        }
      >
        <LineChart
          series={drawdownSeries}
          height={170}
          area
          baseline={0}
          xFormat={(x) => dateOnly(x)}
          yFormat={(y) => `${y.toFixed(2)}%`}
          ariaLabel="Drawdown curve of the experiment"
          emptyMessage="No equity curve, so drawdown cannot be reconstructed."
        />
      </ChartFrame>

      <ChartFrame
        title="Cumulative realized trade P&L"
        caption="Closed trades only, stepped at each exit"
        footnote={
          <>
            Not an equity curve: it excludes open-position marks and has no time between exits.
            {analysis.trade_series_total > analysis.trade_series.length
              ? ` Showing ${String(analysis.trade_series.length)} of ${String(analysis.trade_series_total)} trades.`
              : ` ${String(analysis.trade_series_total)} closed trade(s).`}
          </>
        }
      >
        <LineChart
          series={tradeSeries}
          height={200}
          baseline={0}
          xFormat={(x) => dateOnly(x)}
          yFormat={(y) => signedMoney(Math.round(y * 100) / 100)}
          ariaLabel="Cumulative realized P&L of closed trades"
          emptyMessage="No closed trades with a recorded P&L."
        />
      </ChartFrame>

      <div className="panel-grid">
        <ChartFrame
          title="Per-trade P&L distribution"
          caption={analysis.pnl_distribution.available ? `${String(analysis.pnl_distribution.count)} closed trades` : undefined}
          footnote={
            analysis.pnl_distribution.available
              ? `Mean ${signedMoney(analysis.pnl_distribution.mean)} · median ${signedMoney(analysis.pnl_distribution.median)} · std dev ${money(analysis.pnl_distribution.stdev)} · ${String(analysis.pnl_distribution.winners)} winners / ${String(analysis.pnl_distribution.losers)} losers. Green bins are profitable, red are losing.`
              : undefined
          }
        >
          <Histogram
            bins={analysis.pnl_distribution.bins}
            valueFormat={(v) => `${v < 0 ? "-" : ""}$${Math.abs(v).toFixed(Math.abs(v) < 10 ? 2 : 0)}`}
            ariaLabel="Histogram of per-trade realized P&L"
          />
        </ChartFrame>
        <ChartFrame
          title={`Performance by ${granularity}`}
          caption={periodMetric === "equity" ? "Change in account equity" : "Realized P&L of trades closed in the period"}
          footnote={`Buckets are ${granularity}s in exchange time over the evaluation window. Equity change includes unrealized moves; realized P&L counts trades by exit date.`}
        >
          <div className="source-switch rl-inline-switch" role="group" aria-label="Period metric">
            <button type="button" className={periodMetric === "equity" ? "active" : ""} onClick={() => setPeriodMetric("equity")}>
              Equity change
            </button>
            <button type="button" className={periodMetric === "realized" ? "active" : ""} onClick={() => setPeriodMetric("realized")}>
              Realized P&amp;L
            </button>
          </div>
          <BarChart bars={bars} valueFormat={(v) => signedMoney(Math.round(v * 100) / 100)} ariaLabel="Performance per period" />
        </ChartFrame>
      </div>
      <Legend series={[{ id: "g", label: "Gain / profitable", color: "#3fbf7f" }, { id: "r", label: "Loss", color: "#e0665a" }]} />
    </>
  );
}

function Journal({ selectionId }: { selectionId: string }) {
  const navigate = useNavigate();
  const [query, setQuery] = useState<JournalQuery>({ page: 1, pageSize: 25, sort: "entry_timestamp", order: "desc" });
  const journal = useRunJournal(selectionId, query);
  const data = journal.data;
  const patch = (change: Partial<JournalQuery>) => setQuery((current) => ({ ...current, ...change, page: change.page ?? 1 }));
  const sortBy = (field: string) =>
    setQuery((current) => ({
      ...current,
      sort: field,
      order: current.sort === field && current.order === "desc" ? "asc" : "desc",
      page: 1,
    }));

  const href = (trade: JournalTrade) =>
    `/trades/${encodeURIComponent(trade.trade_id)}?source=backtest&run=${encodeURIComponent(selectionId)}`;
  const open = (trade: JournalTrade) => void navigate(href(trade));
  const summary = data?.filtered_summary;

  return (
    <Section title="Trade journal">
      <div className="filter-bar">
        <label>
          Symbol
          <select value={query.symbol ?? ""} onChange={(e) => patch({ symbol: e.target.value || undefined })}>
            <option value="">All</option>
            {(data?.available_symbols ?? []).map((s) => <option key={s}>{s}</option>)}
          </select>
        </label>
        <label>
          Entry strategy
          <select value={query.entryStrategy ?? ""} onChange={(e) => patch({ entryStrategy: e.target.value || undefined })}>
            <option value="">All</option>
            {(data?.available_entry_strategies ?? []).map((s) => <option key={s}>{s}</option>)}
          </select>
        </label>
        <label>
          Exit reason / strategy
          <select value={query.exitReason ?? ""} onChange={(e) => patch({ exitReason: e.target.value || undefined })}>
            <option value="">All</option>
            {(data?.available_exit_reasons ?? []).map((s) => <option key={s}>{s}</option>)}
          </select>
        </label>
        <label>
          Result
          <select value={query.outcome ?? ""} onChange={(e) => patch({ outcome: (e.target.value || undefined) as JournalQuery["outcome"] })}>
            <option value="">Winners and losers</option>
            <option value="WIN">Winners</option>
            <option value="LOSS">Losers</option>
          </select>
        </label>
        <label>
          Direction
          <select value={query.direction ?? ""} onChange={(e) => patch({ direction: (e.target.value || undefined) as JournalQuery["direction"] })}>
            <option value="">Long and short</option>
            {(data?.available_directions ?? ["LONG", "SHORT"]).map((s) => <option key={s}>{s}</option>)}
          </select>
        </label>
        <label>
          Status
          <select value={query.positionStatus ?? ""} onChange={(e) => patch({ positionStatus: e.target.value || undefined })}>
            <option value="">All</option>
            <option>CLOSED</option>
            <option>PARTIALLY_CLOSED</option>
            <option>OPEN</option>
          </select>
        </label>
      </div>
      {journal.isError ? (
        <ErrorBanner title="Trade journal unavailable" message={journal.error instanceof Error ? journal.error.message : "Load failed"} />
      ) : null}
      {journal.isPending ? <Loading label="Loading trades…" /> : null}
      {summary !== undefined ? (
        <p className="hint rl-journal-summary">
          {summary.trade_count.toLocaleString("en-US")} trade(s) match · {summary.winning} winning / {summary.losing} losing · realized P&amp;L of
          matching closed trades{" "}
          <strong className={signClass(summary.realized_pnl)}>{signedMoney(summary.realized_pnl)}</strong>
        </p>
      ) : null}
      {data !== undefined ? (
        <>
          <div className="table-wrap">
            <table className="data-table trades-table rl-table">
              <thead>
                <tr>
                  <SortHeader label="Entry (ET)" field="entry_timestamp" sort={query.sort} direction={query.order ?? "desc"} onSort={sortBy} />
                  <SortHeader label="Symbol" field="symbol" sort={query.sort} direction={query.order ?? "desc"} onSort={sortBy} />
                  <th scope="col">Side</th>
                  <th scope="col">Entry strategy</th>
                  <th scope="col" className="right" style={{ textAlign: "right" }}>Entry</th>
                  <SortHeader label="Exit (ET)" field="exit_timestamp" sort={query.sort} direction={query.order ?? "desc"} onSort={sortBy} />
                  <th scope="col" className="right" style={{ textAlign: "right" }}>Exit</th>
                  <th scope="col" className="right" style={{ textAlign: "right" }}>Qty</th>
                  <SortHeader label="Held" field="holding_duration" sort={query.sort} direction={query.order ?? "desc"} onSort={sortBy} align="right" />
                  <SortHeader label="Realized P&L" field="realized_pnl" sort={query.sort} direction={query.order ?? "desc"} onSort={sortBy} align="right" />
                  <SortHeader label="Return" field="realized_return_percent" sort={query.sort} direction={query.order ?? "desc"} onSort={sortBy} align="right" />
                  <th scope="col">Exit reason</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((trade) => {
                  const held =
                    trade.entry_timestamp && trade.exit_timestamp
                      ? (Date.parse(trade.exit_timestamp) - Date.parse(trade.entry_timestamp)) / 1000
                      : null;
                  return (
                    <tr
                      key={trade.trade_id}
                      className="row-expandable"
                      tabIndex={0}
                      onClick={() => open(trade)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" && event.target === event.currentTarget) open(trade);
                      }}
                    >
                      <td>
                        <Link className="cell-link" to={href(trade)} onClick={(e) => e.stopPropagation()}>
                          {trade.entry_timestamp ? dateTimeEt(trade.entry_timestamp) : "Unavailable"}
                        </Link>
                      </td>
                      <td className="cell-symbol">{trade.symbol}</td>
                      <td><span className={`pill ${trade.direction === "SHORT" ? "pill-yellow" : "pill-neutral"}`}>{trade.direction}</span></td>
                      <td>{trade.entry_strategy_id ?? <span className="unavailable">UNKNOWN</span>}</td>
                      <td className="right">{money(trade.entry_price)}</td>
                      <td>{trade.exit_timestamp ? dateTimeEt(trade.exit_timestamp) : <span className="unavailable">Open</span>}</td>
                      <td className="right">{money(trade.exit_price)}</td>
                      <td className="right">{quantity(trade.filled_quantity)}</td>
                      <td className="right">{holdingText(held)}</td>
                      <td className={`right ${signClass(trade.realized_pnl)}`}>{signedMoney(trade.realized_pnl)}</td>
                      <td className={`right ${signClass(trade.realized_return_percent)}`}>{trade.realized_return_percent === null ? "—" : percent(trade.realized_return_percent)}</td>
                      <td>
                        {trade.exit_reason || trade.exit_strategy_id || (
                          <span className="unavailable">{trade.position_status === "OPEN" ? "Open" : "UNKNOWN"}</span>
                        )}
                      </td>
                      <td><span className="pill pill-neutral">{trade.position_status}</span></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {data.items.length === 0 ? <p className="hint">No trades match these filters.</p> : null}
          <Pagination
            page={data.page}
            pages={data.total_pages}
            total={data.total}
            onPage={(page) => setQuery((current) => ({ ...current, page }))}
            pageSize={data.page_size}
            onPageSize={(size) => setQuery((current) => ({ ...current, pageSize: size, page: 1 }))}
          />
          <p className="table-note">
            Select a trade to open its candlestick inspection. Realized P&amp;L uses recorded entry/exit fill prices
            (slippage is already in those prices); fees are zero in recorded assumptions.
          </p>
        </>
      ) : null}
    </Section>
  );
}

export default function ResearchRunDetailPage() {
  const { selectionId } = useParams<{ selectionId: string }>();
  const [search] = useSearchParams();
  const analysisQuery = useRunAnalysis(selectionId);
  const analysis = analysisQuery.data;
  const run = analysis?.run ?? null;
  const diag = analysis?.diagnostics ?? {};
  const backTo = search.get("from") ? `/research?${search.get("from") ?? ""}` : "/research";
  const stats = run?.trade_stats ?? {};
  const days = run === null ? null : windowDays(run);
  const configEntries = Object.entries(analysis?.configuration ?? {}).filter(
    ([key]) => !["strategy_collections", "assumptions"].includes(key),
  );

  return (
    <div className="page page-wide rl">
      <PageHeader
        title={run === null ? "Experiment" : `${run.symbols.join(", ") || "Experiment"} · ${[...run.buy_strategies, ...run.short_strategies].join(" + ") || "unattributed strategies"}`}
        subtitle={run === null ? undefined : `historical window ${windowText(run)}${days !== null ? ` (${String(days)} days)` : ""} · ${run.period_role ?? "period role not recorded"}`}
        actions={
          <>
            {run !== null ? (
              <Link className="button" to={`/research?view=explorer&cmp=${encodeURIComponent(run.selection_id)}`}>
                Start comparison
              </Link>
            ) : null}
            <Link className="button" to={backTo}>Back to Research Lab</Link>
          </>
        }
      />

      {analysisQuery.isPending ? <Loading label="Loading experiment…" /> : null}
      {analysisQuery.isError ? (
        <ErrorBanner
          title="Experiment unavailable"
          message={analysisQuery.error instanceof Error ? analysisQuery.error.message : "Run load failed"}
        />
      ) : null}

      {run !== null && analysis !== undefined ? (
        <>
          {run.execution_identity_uncertain ? (
            <Banner tone="warning" title="Legacy record">
              This artifact has no execution identifier or start time. Identical result ledgers are listed once and the
              original execution time cannot be recovered. Stop settings and run purpose were not recorded either.
            </Banner>
          ) : null}

          <section className="panel">
            <div className="kpi-ribbon">
              <KpiTile label="Net P&L (equity)" value={signedMoney(run.net_pnl)} tone={toNum(run.net_pnl) === null ? undefined : toNum(run.net_pnl)! >= 0 ? "positive" : "negative"} meta={`${money(run.starting_capital)} → ${money(run.final_equity)}`} />
              <KpiTile label="Total return" value={signedPercent(run.return_percent)} meta="on starting capital" />
              <KpiTile label="Closed trades" value={String(run.completed_trade_count)} meta={`${String(diag.open_cycles ?? 0)} open / ${String(diag.total_cycles ?? 0)} cycles`} />
              <KpiTile label="Win rate" value={run.completed_trade_count === 0 ? "—" : plainPercent(run.win_rate)} meta={`${String(stats.winning_trades ?? 0)} W / ${String(stats.losing_trades ?? 0)} L`} />
              <KpiTile label="Profit factor" value={profitFactor(run.profit_factor)} meta={run.profit_factor === null ? "no losing trades" : "gross profit / gross loss"} />
              <KpiTile label="Max drawdown" value={`${fixed(run.max_drawdown)}%`} meta={analysis.drawdown_detail.available ? `${money(analysis.drawdown_detail.max_drawdown_amount)} peak to trough` : "no equity curve"} />
              <KpiTile label="Average win" value={signedMoney(stats.average_win)} />
              <KpiTile label="Average loss" value={signedMoney(stats.average_loss)} />
              <KpiTile label="Expectancy" value={signedMoney(stats.expectancy)} meta="per closed trade" />
              <KpiTile label="Payoff ratio" value={fixed(stats.payoff_ratio)} meta="avg win / avg loss" />
            </div>
            <p className="table-note">
              Net P&amp;L and return are <strong>equity-basis</strong> (include open positions). Closed-trade realized
              P&amp;L is <strong className={signClass(stats.realized_pnl)}>{signedMoney(stats.realized_pnl)}</strong>
              {diag.unrealized_or_unexplained_pnl !== null && diag.unrealized_or_unexplained_pnl !== undefined && Math.abs(Number(diag.unrealized_or_unexplained_pnl)) >= 0.005
                ? `; the ${signedMoney(String(diag.unrealized_or_unexplained_pnl))} difference is open-position mark-to-market.`
                : "; it matches the equity result."}
            </p>
          </section>

          <div className="panel-grid">
            <Section title="Identity & execution">
              <dl className="kv-grid kv-compact">
                <div><dt>Run id</dt><dd><CopyId value={run.run_id} keep={16} /></dd></div>
                <div><dt>Selection id</dt><dd><CopyId value={run.selection_id} keep={16} /></dd></div>
                <div><dt>Execution id</dt><dd><CopyId value={run.execution_id} keep={16} /></dd></div>
                <div><dt>Executed</dt><dd>{run.execution_at ? dateTimeEt(run.execution_at) : "Not recorded"}</dd></div>
                <div><dt>Completed</dt><dd>{run.completed_at ? dateTimeEt(run.completed_at) : "Not recorded"}</dd></div>
                <div><dt>Status</dt><dd>{run.status}</dd></div>
                <div><dt>Mode</dt><dd>{run.execution_mode}</dd></div>
                <div><dt>Purpose / group</dt><dd>{run.run_purpose ?? run.artifact_group ?? "Not recorded"}</dd></div>
                <div><dt>Symbols</dt><dd>{run.symbols.join(", ") || "Not recorded"}</dd></div>
                <div><dt>Evaluation period</dt><dd>{windowText(run)}</dd></div>
                <div><dt>Dataset</dt><dd className="cell-mono">{unavailable(run.dataset_id)}</dd></div>
                <div><dt>Dataset hash</dt><dd><CopyId value={run.dataset_hash} keep={16} /></dd></div>
                <div><dt>Data source</dt><dd>{recordText(run.provenance)}</dd></div>
                <div><dt>Config fingerprint</dt><dd><CopyId value={run.config_fingerprint} keep={16} /></dd></div>
              </dl>
            </Section>
            <Section title="Strategies, stops & capital">
              <dl className="kv-grid kv-compact">
                <div className="rl-span"><dt>Strategies</dt><dd><StrategyChips run={run} /></dd></div>
                <div><dt>Stop policy</dt><dd><StopCell run={run} /></dd></div>
                <div><dt>Starting capital</dt><dd>{money(run.starting_capital)}</dd></div>
                <div><dt>Final equity</dt><dd>{money(run.final_equity)}</dd></div>
                <div><dt>Peak / lowest equity</dt><dd>{money(String(diag.peak_equity ?? ""))} / {money(String(diag.lowest_equity ?? ""))}</dd></div>
                <div><dt>Fees paid</dt><dd>{diag.total_fees === null || diag.total_fees === undefined ? "Not recorded" : money(String(diag.total_fees))}</dd></div>
                <div><dt>Fee assumption</dt><dd>{recordText(analysis.assumptions.fee_assumptions)}</dd></div>
                <div><dt>Slippage assumption</dt><dd>{recordText(analysis.assumptions.slippage_assumptions)}</dd></div>
                <div><dt>Fill assumption</dt><dd>{recordText(analysis.assumptions.fill_assumptions)}</dd></div>
                <div><dt>Avg hold (win / loss)</dt><dd>{holdingText(diag.average_holding_seconds_winners as number | null)} / {holdingText(diag.average_holding_seconds_losers as number | null)}</dd></div>
              </dl>
              {configEntries.length === 0 ? (
                <p className="hint">No run configuration was recorded in this artifact (legacy record).</p>
              ) : (
                <dl className="kv-grid kv-compact rl-config">
                  {configEntries.slice(0, 12).map(([key, value]) => (
                    <div key={key}><dt>{key.replace(/_/g, " ")}</dt><dd>{recordText(value)}</dd></div>
                  ))}
                </dl>
              )}
            </Section>
          </div>

          <Section title="Charts from recorded results">
            <Charts analysis={analysis} />
          </Section>

          <Section title="Are results concentrated in a few trades?">
            <dl className="kv-grid kv-compact">
              <div><dt>Best trade</dt><dd className="text-positive">{signedMoney(stats.best_trade)}</dd></div>
              <div><dt>Worst trade</dt><dd className="text-negative">{signedMoney(stats.worst_trade)}</dd></div>
              <div><dt>Gross profit / gross loss</dt><dd>{money(stats.gross_profit)} / {money(stats.gross_loss)}</dd></div>
              <div><dt>Top-3 winners’ profit share</dt><dd>{plainPercent(stats.top3_profit_share, 0)}</dd></div>
              <div><dt>Realized P&amp;L without top-3 winners</dt><dd className={signClass(stats.realized_pnl_ex_top3)}>{signedMoney(stats.realized_pnl_ex_top3)}</dd></div>
            </dl>
            <p className="table-note">
              {toNum(stats.top3_profit_share) !== null && Number(stats.top3_profit_share) > 50 && (stats.winning_trades ?? 0) > 3
                ? "More than half of the gross profit comes from three trades — treat the headline result as fragile."
                : "Concentration is moderate. Small samples (few winners) are inherently concentrated."}
            </p>
          </Section>

          <Journal selectionId={run.selection_id} />

          <TechnicalDetails summary="Metric definitions & raw configuration">
            <dl className="kv-grid kv-compact rl-defs">
              {Object.entries(analysis.metric_definitions).map(([key, text]) => (
                <div key={key}><dt>{key.replace(/_/g, " ")}</dt><dd className="rl-def-text">{text}</dd></div>
              ))}
            </dl>
            <p className="hint">Artifact locations: {analysis.artifact_locations.join(", ") || "n/a"}</p>
            <details className="tech-details">
              <summary>Raw recorded configuration JSON</summary>
              <pre>{JSON.stringify({ configuration: analysis.configuration, assumptions: analysis.assumptions, stop_configuration: run.stop_configuration }, null, 2)}</pre>
            </details>
          </TechnicalDetails>
        </>
      ) : null}
    </div>
  );
}
