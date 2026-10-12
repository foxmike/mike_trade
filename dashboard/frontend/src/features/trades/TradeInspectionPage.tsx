/** Trade Explorer inspection view. The chart is the primary element; the
 * summary above it and the chronologies below it all come from the recorded
 * inspection payload. Deep links (/trades/:id?source=&run=) are preserved. */

import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { useResearchTradeInspection, useTradeInspection } from "../../state/queries";
import { useSelectedAccount } from "../../state/AccountContext";
import { ErrorBanner, Loading, PageHeader, TechnicalDetails } from "../../components/ui";
import type { ChartFocus } from "../charts/ExplorerChart";
import {
  slotFor,
  buildChartModel,
  epochSeconds,
  normalizeFillAction,
  type ChartModel,
  type Direction,
} from "../charts/chartModel";
import type { TradeEvent, TradeInspection } from "../../api/client";
import {
  dateTimeEt,
  decimal,
  durationSeconds,
  MISSING,
  money,
  percent,
  quantity,
  timeEt,
} from "../../lib/format";
import { useTradeNeighbors } from "./useTradeNeighbors";
import {
  contextQuery,
  describeEvent,
  entryStrategyLabel,
  exitLabel,
  holdingSeconds,
  isSignalEvent,
  toneOf,
} from "./tradeView";
import "./trades.css";

// Charting library is split out so the rest of the console loads without it.
const ExplorerChart = lazy(() => import("../charts/ExplorerChart"));

const unknown = (value: string | null | undefined): string =>
  value === null || value === undefined || value === "" ? "UNKNOWN" : value;

function Cell({
  label,
  value,
  sub,
  tone,
  wide,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  tone?: "positive" | "negative";
  wide?: boolean;
}) {
  const cls = [tone === "positive" ? "pos" : tone === "negative" ? "neg" : "", wide ? "wide" : ""].filter(Boolean).join(" ");
  return (
    <div className={cls || undefined}>
      <dt>{label}</dt>
      <dd>
        {value}
        {sub !== undefined ? <span className="sub">{sub}</span> : null}
      </dd>
    </div>
  );
}

function Summary({ inspection, direction }: { inspection: TradeInspection; direction: Direction }) {
  const { trade, diagnostics: diag } = inspection;
  const entryPrice = diag.entry_price ?? trade.entry_price;
  const exitPrice = diag.exit_price ?? trade.exit_price;
  const pnl = diag.realized_pnl ?? trade.realized_pnl;
  const ret = diag.realized_return_percent ?? trade.realized_return_percent;
  const entryStrategy = entryStrategyLabel(trade);
  return (
    <dl className="tx-summary" aria-label="Trade summary">
      <Cell
        label="Symbol"
        value={
          <>
            {trade.symbol}
            <span className={`tx-dir ${direction}`}>{direction}</span>
          </>
        }
        sub={`${trade.position_status} · ${trade.execution_mode}`}
      />
      <Cell label="Entry" value={decimal(entryPrice)} sub={trade.entry_timestamp ? `${dateTimeEt(trade.entry_timestamp)} ET` : MISSING} />
      <Cell
        label="Exit"
        value={trade.exit_timestamp == null && exitPrice == null ? "Open" : decimal(exitPrice)}
        sub={trade.exit_timestamp ? `${dateTimeEt(trade.exit_timestamp)} ET` : "no exit recorded"}
      />
      <Cell label="Quantity" value={quantity(diag.quantity || trade.filled_quantity)} />
      <Cell label="Realized P&L" value={money(pnl, true)} tone={toneOf(pnl)} sub={trade.unrealized_pnl != null ? `unrealized ${money(trade.unrealized_pnl, true)}` : undefined} />
      <Cell label="Return" value={percent(ret)} tone={toneOf(ret)} />
      <Cell label="Holding" value={durationSeconds(holdingSeconds(inspection))} />
      <Cell
        label="Entry strategy"
        wide
        value={entryStrategy ?? <span className="unavailable">UNKNOWN — not recorded</span>}
        sub={`decision ${unknown(trade.entry_decision)}${trade.entry_signal_id ? ` · signal ${trade.entry_signal_id}` : ""}`}
      />
      <Cell label="Exit" wide value={exitLabel(trade)} sub={trade.exit_reason === "STRATEGY_EXIT" ? "strategy exit" : trade.protective_stop_mode ? `protection ${trade.protective_stop_mode}` : undefined} />
      <Cell label="Initial stop" value={decimal(trade.initial_stop_price)} sub={diag.initial_stop_distance != null ? `distance ${decimal(diag.initial_stop_distance)}` : undefined} />
      <Cell label="Final stop" value={decimal(trade.final_stop_price)} sub={diag.final_stop_distance != null ? `distance ${decimal(diag.final_stop_distance)}` : undefined} />
    </dl>
  );
}

function useFocus() {
  const [focus, setFocus] = useState<ChartFocus | null>(null);
  const nonce = useRef(0);
  const focusAt = useCallback((timestamp: string) => {
    const epoch = epochSeconds(timestamp);
    if (epoch === null) return;
    nonce.current += 1;
    setFocus({ epoch, nonce: nonce.current });
  }, []);
  return { focus, focusAt, clear: () => setFocus(null) };
}

function InspectionBody({
  inspection,
  model,
}: {
  inspection: TradeInspection;
  model: ChartModel;
}) {
  const { trade, diagnostics: diag } = inspection;
  const { focus, focusAt } = useFocus();
  const chartRef = useRef<HTMLDivElement | null>(null);

  const select = (timestamp: string) => {
    focusAt(timestamp);
    chartRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  const onChart = (timestamp: string): boolean => {
    const epoch = epochSeconds(timestamp);
    return slotFor(model.bars, epoch) >= 0;
  };
  const activeEpoch = focus?.epoch ?? null;
  const isActive = (timestamp: string) => activeEpoch !== null && epochSeconds(timestamp) === activeEpoch;

  const signalEvents = useMemo(() => inspection.events.filter(isSignalEvent), [inspection.events]);
  const rowKey = (event: TradeEvent, index: number) => `${event.timestamp}-${event.event_type}-${index}`;
  const activate = (timestamp: string) => (event: React.KeyboardEvent) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      select(timestamp);
    }
  };

  return (
    <div className="inspection">
      <Summary inspection={inspection} direction={model.direction} />

      <div ref={chartRef}>
        <Suspense fallback={<Loading label="Loading chart…" />}>
          <ExplorerChart model={model} symbol={trade.symbol} focus={focus} />
        </Suspense>
      </div>
      {model.chartAvailable && diag.basis ? <p className="hint">{diag.basis}. One-minute OHLC bars do not establish intrabar ordering.</p> : null}
      {model.chartAvailable && !model.stops.recordedMovements && (model.stops.initial !== null || model.stops.final !== null) ? (
        <p className="hint">Stop movements were not recorded; only the recorded initial/final stop levels are shown as reference lines.</p>
      ) : null}

      <div className="tx-below">
        <div className="panel-sub">
          <h3>Fill chronology</h3>
          {inspection.fills.length === 0 ? (
            <p className="hint">No individual fills recorded for this trade.</p>
          ) : (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Time (ET)</th>
                    <th>Action</th>
                    <th className="right">Price</th>
                    <th className="right">Quantity</th>
                    <th className="right">Fee</th>
                  </tr>
                </thead>
                <tbody>
                  {inspection.fills.map((fill) => (
                    <tr
                      key={fill.fill_id}
                      className={`tx-click-row${isActive(fill.timestamp) ? " active" : ""}`}
                      tabIndex={0}
                      onClick={() => select(fill.timestamp)}
                      onKeyDown={activate(fill.timestamp)}
                    >
                      <td>{timeEt(fill.timestamp)}</td>
                      <td>
                        {normalizeFillAction(fill.side) ?? fill.side}
                        {!onChart(fill.timestamp) ? <span className="tx-offchart">not on chart</span> : null}
                      </td>
                      <td className="right">{decimal(fill.price)}</td>
                      <td className="right">{quantity(fill.quantity)}</td>
                      <td className="right">{fill.fee === null ? "—" : fill.fee}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="panel-sub">
          <h3>Signal chronology</h3>
          {signalEvents.length === 0 ? (
            <p className="hint">No strategy signals were recorded for this trade.</p>
          ) : (
            <ol className="tx-timeline">
              {signalEvents.map((event, index) => (
                <li
                  key={rowKey(event, index)}
                  tabIndex={0}
                  className={isActive(event.timestamp) ? "active" : undefined}
                  onClick={() => select(event.timestamp)}
                  onKeyDown={activate(event.timestamp)}
                >
                  <time>{timeEt(event.timestamp)}</time>
                  <strong>{event.event_type.replace("SIGNAL_", "")}</strong>
                  <span>{describeEvent(event) || MISSING}</span>
                </li>
              ))}
            </ol>
          )}
        </div>

        <div className="panel-sub">
          <h3>Decision &amp; event timeline</h3>
          {inspection.events.length === 0 ? (
            <p className="hint">No recorded trade events are available.</p>
          ) : (
            <ol className="tx-timeline">
              {inspection.events.map((event, index) => (
                <li
                  key={rowKey(event, index)}
                  tabIndex={0}
                  className={isActive(event.timestamp) ? "active" : undefined}
                  onClick={() => select(event.timestamp)}
                  onKeyDown={activate(event.timestamp)}
                  title="Show on chart"
                >
                  <time>{timeEt(event.timestamp)}</time>
                  <strong>{event.event_type}</strong>
                  <span>{describeEvent(event) || MISSING}</span>
                </li>
              ))}
            </ol>
          )}
        </div>

        <div className="panel-sub">
          <h3>Excursions &amp; diagnostics</h3>
          <dl className="kv-grid diagnostic-grid">
            <div><dt>MFE</dt><dd>{money(diag.maximum_favorable_excursion, true)}</dd></div>
            <div><dt>MAE</dt><dd>{money(diag.maximum_adverse_excursion, true)}</dd></div>
            <div><dt>Max unrealized gain</dt><dd>{money(diag.maximum_unrealized_gain, true)}</dd></div>
            <div><dt>Max unrealized loss</dt><dd>{money(diag.maximum_unrealized_loss, true)}</dd></div>
            <div><dt>Best price</dt><dd>{decimal(diag.best_price)}</dd></div>
            <div><dt>Worst price</dt><dd>{decimal(diag.worst_price)}</dd></div>
            <div><dt>Exit reason</dt><dd>{unknown(diag.exit_reason ?? trade.exit_reason)}</dd></div>
            <div><dt>Protection mode</dt><dd>{unknown(trade.protective_stop_mode)}</dd></div>
            <div><dt>Observations through</dt><dd>{diag.observations_through ? `${dateTimeEt(diag.observations_through)} ET` : MISSING}</dd></div>
          </dl>
          {diag.limitation ? <p className="hint">{diag.limitation}</p> : null}
        </div>
      </div>

      <TechnicalDetails summary="Data provenance and identifiers">
        <dl className="kv-grid kv-compact">
          <div><dt>Provenance</dt><dd>{unknown(trade.data_provenance)}</dd></div>
          <div><dt>Entry signal</dt><dd>{trade.entry_signal_id ?? MISSING}</dd></div>
          <div><dt>Exit signal</dt><dd>{trade.exit_signal_id ?? MISSING}</dd></div>
          <div><dt>Research run</dt><dd>{trade.research_run_id ?? MISSING}</dd></div>
          <div><dt>Cycle</dt><dd>{trade.cycle_id}</dd></div>
          <div><dt>Attribution complete</dt><dd>{String(trade.attribution_complete)}</dd></div>
          <div><dt>Bars</dt><dd>{model.bars.candles.length} candles · {model.hasVwap ? "VWAP recorded" : "no VWAP"}</dd></div>
          <div><dt>Bars limitation</dt><dd>{inspection.bars_limitation ?? "none"}</dd></div>
        </dl>
      </TechnicalDetails>
    </div>
  );
}

function isTyping(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (target.isContentEditable || ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName));
}

export default function TradeInspectionPage() {
  const { tradeId } = useParams<{ tradeId: string }>();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { selectedAccountId } = useSelectedAccount();

  const source = params.get("source") === "backtest" ? "backtest" : "account";
  const runId = params.get("run") ?? undefined;

  const accountQuery = useTradeInspection(source === "account" ? selectedAccountId : null, tradeId);
  const runQuery = useResearchTradeInspection(source === "backtest" ? runId : undefined, tradeId);
  const query = source === "account" ? accountQuery : runQuery;
  const inspection = query.data ?? null;

  const model = useMemo(() => (inspection === null ? null : buildChartModel(inspection)), [inspection]);
  const neighbors = useTradeNeighbors(source, selectedAccountId, runId, inspection?.trade ?? null);
  const suffix = contextQuery(params);

  const tradePath = (id: string) => `/trades/${encodeURIComponent(id)}${suffix}`;
  const previousPath = neighbors.previous ? tradePath(neighbors.previous.trade_id) : null;
  const nextPath = neighbors.next ? tradePath(neighbors.next.trade_id) : null;

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey || isTyping(event.target)) return;
      if (event.key === "ArrowLeft" && previousPath !== null) {
        event.preventDefault();
        void navigate(previousPath);
      } else if (event.key === "ArrowRight" && nextPath !== null) {
        event.preventDefault();
        void navigate(nextPath);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [previousPath, nextPath, navigate]);

  const listLink = `/trades${source === "backtest" ? `?source=backtest${runId ? `&run=${encodeURIComponent(runId)}` : ""}` : ""}`;
  const runLink = source === "backtest" && runId !== undefined ? `/research/runs/${encodeURIComponent(runId)}` : null;

  return (
    <div className="page page-wide">
      <PageHeader
        title={inspection === null ? "Trade inspection" : `Trade · ${inspection.trade.symbol}`}
        subtitle={
          inspection === null
            ? undefined
            : `${inspection.trade.execution_mode} · cycle ${inspection.trade.cycle_id} · times in America/New_York`
        }
        actions={
          <>
            {runLink !== null ? <Link className="button tx-btn" to={runLink}>Back to run</Link> : null}
            <Link className="button tx-btn" to={listLink}>{source === "backtest" ? "Run trades" : "Trade journal"}</Link>
          </>
        }
      />

      {inspection !== null ? (
        <div className="tx-nav">
          {previousPath !== null ? <Link className="button tx-btn" to={previousPath} title="Previous trade (←)">← Previous</Link> : <button type="button" disabled>← Previous</button>}
          {nextPath !== null ? <Link className="button tx-btn" to={nextPath} title="Next trade (→)">Next →</Link> : <button type="button" disabled>Next →</button>}
          <span className="tx-identity">
            {source === "backtest" ? `run ${runId ?? MISSING}` : `account ${selectedAccountId ?? MISSING}`} · trade {inspection.trade.trade_id}
          </span>
          <span className="tx-nav-spacer" />
          <span className="hint">← / → keys step through trades by entry time</span>
        </div>
      ) : null}

      {source === "account" && selectedAccountId === null ? (
        <div className="state-block state-empty">
          <p>No operational account selected.</p>
          <p className="hint">
            Select an account in the header to inspect this operational trade, or return to{" "}
            <Link className="link-button" to="/trades">
              Trade Explorer
            </Link>.
          </p>
        </div>
      ) : null}

      {query.isPending && !query.isError && !(source === "account" && selectedAccountId === null) ? (
        <Loading label="Loading trade inspection…" />
      ) : null}
      {query.isError ? (
        <ErrorBanner
          title="Trade inspection unavailable"
          message={query.error instanceof Error ? query.error.message : "Unable to inspect trade"}
          hint={source === "backtest" && runId === undefined ? "Missing research run context in the URL." : undefined}
        />
      ) : null}
      {inspection !== null && model !== null ? <InspectionBody key={inspection.trade.trade_id} inspection={inspection} model={model} /> : null}
    </div>
  );
}
