/** Dedicated trade inspection view (route-based). The selected trade stays in
 * its own URL so navigation, refresh and back all keep it visible — replacing
 * the old below-the-fold detail panel. Preserves entry/exit, quantity, P&L,
 * strategy attribution, exit reason, diagnostics and the interim chart. */

import { Link, useParams, useSearchParams } from "react-router";
import {
  useResearchTradeInspection,
  useTradeInspection,
} from "../../state/queries";
import { useSelectedAccount } from "../../state/AccountContext";
import { ErrorBanner, Loading, PageHeader, TechnicalDetails } from "../../components/ui";
import TradeChart from "./TradeChart";
import type { TradeInspection } from "../../api/client";
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

const unknown = (value: string | null | undefined): string =>
  value === null || value === undefined || value === "" ? "UNKNOWN" : value;

function Metric({ label, value, tone }: { label: string; value: string; tone?: "positive" | "negative" }) {
  return (
    <div className={tone === "positive" ? "kv-positive" : tone === "negative" ? "kv-negative" : undefined}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function pnlTone(value: string | null): "positive" | "negative" | undefined {
  if (value === null) return undefined;
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return undefined;
  return n > 0 ? "positive" : "negative";
}

function InspectionBody({ inspection }: { inspection: TradeInspection }) {
  const trade = inspection.trade;
  const diag = inspection.diagnostics;
  const tone = pnlTone(diag.realized_pnl);
  return (
    <div className="inspection">
      <dl className="kv-grid diagnostic-grid">
        <Metric label="Entry" value={decimal(diag.entry_price ?? trade.entry_price)} />
        <Metric label="Exit" value={decimal(diag.exit_price ?? trade.exit_price)} />
        <Metric label="Quantity" value={quantity(diag.quantity || trade.filled_quantity)} />
        <Metric label="Realized P&L" value={money(diag.realized_pnl, true)} tone={tone} />
        <Metric label="Unrealized P&L" value={money(diag.unrealized_pnl, true)} />
        <Metric label="Return" value={percent(diag.realized_return_percent)} tone={pnlTone(diag.realized_return_percent) === "positive" ? "positive" : "negative"} />
        <Metric label="Holding" value={durationSeconds(diag.holding_duration_seconds ?? trade.holding_duration_seconds)} />
        <Metric label="MFE" value={money(diag.maximum_favorable_excursion, true)} />
        <Metric label="MAE" value={money(diag.maximum_adverse_excursion, true)} />
        <Metric label="Best price" value={decimal(diag.best_price)} />
        <Metric label="Worst price" value={decimal(diag.worst_price)} />
        <Metric label="Initial stop" value={decimal(trade.initial_stop_price)} />
        <Metric label="Final stop" value={decimal(trade.final_stop_price)} />
        <Metric label="Initial stop distance" value={decimal(diag.initial_stop_distance)} />
        <Metric label="Final stop distance" value={decimal(diag.final_stop_distance)} />
        <Metric label="Exit reason" value={unknown(diag.exit_reason ?? trade.exit_reason)} />
        <Metric label="Protection mode" value={unknown(trade.protective_stop_mode)} />
        <Metric label="Diagnostics basis" value={unknown(diag.basis)} />
      </dl>

      <div className="attribution">
        <p>
          <strong>Entry strategy:</strong>{" "}
          {trade.entry_strategy_id === null ? (
            <span className="unavailable">UNKNOWN — attribution not recorded</span>
          ) : (
            `${trade.entry_strategy_id}${trade.entry_strategy_version ? ` v${trade.entry_strategy_version}` : ""}`
          )}
          {" · signal "}
          {trade.entry_signal_id ?? MISSING}
          {" @ "}
          {trade.entry_signal_timestamp === null ? MISSING : timeEt(trade.entry_signal_timestamp)}
          {" · decision "}
          {unknown(trade.entry_decision)}
        </p>
        <p>
          <strong>Exit:</strong>{" "}
          {trade.exit_reason === "STRATEGY_EXIT" ? (
            <>
              {unknown(trade.exit_strategy_id)}
              {trade.exit_strategy_version ? ` v${trade.exit_strategy_version}` : ""}
            </>
          ) : (
            unknown(trade.exit_reason)
          )}
        </p>
      </div>

      <div className="panel-sub">
        <h3>Price chart</h3>
        <p className="hint">
          Interim rendering of recorded one-minute bars. The interactive candlestick engine arrives
          with the dedicated Trade Explorer workstream; no synthetic bars are drawn.
        </p>
        <TradeChart inspection={inspection} />
        {diag.basis ? <p className="hint">{diag.basis}. One-minute OHLC bars do not establish intrabar ordering.</p> : null}
      </div>

      <div className="panel-sub">
        <h3>Fills</h3>
        {inspection.fills.length === 0 ? (
          <p className="hint">No fills recorded.</p>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Time (ET)</th>
                  <th>Side</th>
                  <th className="right">Price</th>
                  <th className="right">Quantity</th>
                  <th className="right">Fee</th>
                </tr>
              </thead>
              <tbody>
                {inspection.fills.map((fill) => (
                  <tr key={fill.fill_id}>
                    <td>{timeEt(fill.timestamp)}</td>
                    <td>{fill.side}</td>
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
        <h3>Event timeline</h3>
        {inspection.events.length === 0 ? (
          <p className="hint">No recorded trade events are available.</p>
        ) : (
          <ol className="event-timeline">
            {inspection.events.map((event, index) => (
              <li key={`${event.timestamp}-${event.event_type}-${index}`}>
                <time>{timeEt(event.timestamp)}</time>
                <strong>{event.event_type}</strong>
                <span>
                  {[event.strategy_id, event.source, event.price !== null ? `@ ${event.price}` : null, event.quantity !== null ? `qty ${event.quantity}` : null, event.reason]
                    .filter(Boolean)
                    .join(" · ")}
                </span>
              </li>
            ))}
          </ol>
        )}
      </div>

      <TechnicalDetails summary="Data provenance and identifiers">
        <dl className="kv-grid kv-compact">
          <div>
            <dt>Provenance</dt>
            <dd>{unknown(trade.data_provenance)}</dd>
          </div>
          <div>
            <dt>Entry signal</dt>
            <dd>{trade.entry_signal_id ?? MISSING}</dd>
          </div>
          <div>
            <dt>Exit signal</dt>
            <dd>{trade.exit_signal_id ?? MISSING}</dd>
          </div>
          <div>
            <dt>Research run</dt>
            <dd>{trade.research_run_id ?? MISSING}</dd>
          </div>
          <div>
            <dt>Attribution complete</dt>
            <dd>{String(trade.attribution_complete)}</dd>
          </div>
          <div>
            <dt>Bars limitation</dt>
            <dd>{inspection.bars_limitation ?? "none"}</dd>
          </div>
        </dl>
      </TechnicalDetails>
    </div>
  );
}

export default function TradeInspectionPage() {
  const { tradeId } = useParams<{ tradeId: string }>();
  const [params] = useSearchParams();
  const { selectedAccountId } = useSelectedAccount();

  const source = params.get("source") === "backtest" ? "backtest" : "account";
  const runId = params.get("run") ?? undefined;

  const accountQuery = useTradeInspection(source === "account" ? selectedAccountId : null, tradeId);
  const runQuery = useResearchTradeInspection(source === "backtest" ? runId : undefined, tradeId);

  const query = source === "account" ? accountQuery : runQuery;
  const inspection = query.data ?? null;

  const backLink =
    source === "backtest" && runId !== undefined
      ? `/research/runs/${encodeURIComponent(runId)}`
      : `/trades${source === "backtest" ? "?source=backtest" : ""}`;

  return (
    <div className="page page-wide">
      <PageHeader
        title={inspection === null ? "Trade inspection" : `Trade · ${inspection.trade.symbol} · ${inspection.trade.position_status}`}
        subtitle={
          inspection === null
            ? undefined
            : `${inspection.trade.execution_mode} · cycle ${inspection.trade.cycle_id} · entry ${
                inspection.trade.entry_timestamp === null ? MISSING : dateTimeEt(inspection.trade.entry_timestamp)
              }`
        }
        actions={<Link className="button" to={backLink}>Back to list</Link>}
      />

      {query.isPending && !query.isError ? <Loading label="Loading trade inspection…" /> : null}
      {query.isError ? (
        <ErrorBanner
          title="Trade inspection unavailable"
          message={query.error instanceof Error ? query.error.message : "Unable to inspect trade"}
          hint={source === "backtest" && runId === undefined ? "Missing research run context in the URL." : undefined}
        />
      ) : null}
      {inspection !== null && !inspection.bars_available ? (
        <div className="banner banner-warning" role="status">
          <strong>BARS UNAVAILABLE</strong>
          <span>
            {inspection.bars_limitation ?? "Recorded one-minute bars are not persisted for this trade."} The trade
            execution record remains authoritative.
          </span>
        </div>
      ) : null}
      {inspection !== null ? <InspectionBody inspection={inspection} /> : null}
    </div>
  );
}
