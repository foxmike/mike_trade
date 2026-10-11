/** Symbol pipeline section: per-symbol lifecycle phase, entry watch, pending
 * entry/exit orders, configured strategies, protective stops and gates — all
 * from the engine's persisted tables via /api/accounts/{id}/cockpit. Rows
 * expand into the per-strategy activity (Feature 3). */

import { Fragment, useState } from "react";
import { Link } from "react-router";
import type { CockpitResponse, StrategyInstanceState, SymbolPipeline } from "../../api/client";
import { EmptyState, StaleFlag } from "../../components/ui";
import { dateTimeEt, decimal, MISSING, quantity, timeEt } from "../../lib/format";

/** Human text for entry-block / gate reason codes (backend terms preserved). */
export function reasonText(reason: string): string {
  const map: Record<string, string> = {
    engine_offline: "Engine offline — the evaluation loop is not running",
    paused: "Entries paused by operator control",
    kill_switch: "Entry kill switch engaged",
    reconciling: "Engine is reconciling broker state",
    market_closed: "Market closed",
    market_halted: "Market halted",
    past_entry_cutoff: "Past today's entry cutoff",
    feed_stale: "Market data feed stale",
    feed_disconnected: "Market data feed disconnected",
    feed_unknown: "Market data feed state unknown",
    broker_unavailable: "Broker link unavailable",
    broker_disconnected: "Broker link disconnected",
    unresolved_reconciliation_drift: "Unresolved reconciliation drift",
    COOLDOWN: "Symbol cooldown active",
    LOCKOUT: "Symbol locked out after consecutive losses",
  };
  return map[reason] ?? reason.replace(/_/g, " ");
}

export function stageClass(stage: string): string {
  const s = stage.toUpperCase();
  if (s.startsWith("ORDER_FILLED")) return "pill-green";
  if (s.startsWith("ORDER_SUBMITTED")) return "pill-accent";
  if (s.startsWith("SIGNAL_PRODUCED")) return "pill-blue";
  if (s.startsWith("SIGNAL_REJECTED")) return "pill-red";
  if (s.startsWith("NO_SIGNAL") || s.startsWith("EVALUATED")) return "pill-neutral";
  if (s.startsWith("WATCHING")) return "pill-dim";
  return "pill-muted";
}

function phaseClass(phase: string): string {
  const p = phase.toUpperCase();
  if (p === "BUY_PENDING" || p === "SELL_PENDING" || p === "PROTECTIVE_PENDING") return "pill-accent";
  if (p === "WATCHING_SELL" || p === "WATCHING_BUY") return "pill-dim";
  if (p === "UNMANAGED" || p === "RECONCILING") return "pill-red";
  if (p === "PAUSED") return "pill-yellow";
  return "pill-neutral";
}

function StageCell({ instance, engineOffline }: { instance: StrategyInstanceState; engineOffline: boolean }) {
  return (
    <>
      <span className={`pill ${stageClass(instance.stage)}`}>{instance.stage}</span>
      {instance.stage_detail ? <span className="cell-sub">{instance.stage_detail}</span> : null}
      {instance.stage_as_of ? (
        <span className="cell-sub">
          {engineOffline ? "last recorded " : ""}
          {timeEt(instance.stage_as_of)}
          {engineOffline ? <StaleFlag /> : null}
        </span>
      ) : null}
    </>
  );
}

function ExpandedPipeline({ row, engineOffline }: { row: SymbolPipeline; engineOffline: boolean }) {
  return (
    <tr className="expanded-row">
      <td colSpan={11}>
        <div className="expanded-grid">
          <div className="expanded-group">
            <h4>Strategy activity</h4>
            <table className="data-table mini-table">
              <thead>
                <tr>
                  <th>Role</th>
                  <th>Strategy</th>
                  <th>Stage</th>
                  <th>Last signal bar</th>
                </tr>
              </thead>
              <tbody>
                {row.instances.map((instance) => (
                  <tr key={instance.instance_id} className={instance.enabled ? undefined : "row-muted"}>
                    <td>{instance.role}</td>
                    <td>
                      {instance.strategy_key}
                      {instance.strategy_version ? <span className="cell-sub">v{instance.strategy_version}</span> : null}
                      {!instance.enabled ? <span className="cell-sub">disabled</span> : null}
                    </td>
                    <td>
                      <StageCell instance={instance} engineOffline={engineOffline} />
                    </td>
                    <td>{instance.last_signal_bar ? timeEt(instance.last_signal_bar) : MISSING}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="expanded-group">
            <h4>Pending orders</h4>
            {row.pending_orders.length === 0 ? (
              <p className="hint">No open orders for this symbol.</p>
            ) : (
              <table className="data-table mini-table">
                <thead>
                  <tr>
                    <th>Purpose</th>
                    <th>Side</th>
                    <th className="right">Qty</th>
                    <th>Type</th>
                    <th className="right">Price</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {row.pending_orders.map((order) => (
                    <tr key={order.broker_order_id}>
                      <td>{order.purpose ?? "—"}</td>
                      <td>{order.side}</td>
                      <td className="right">{quantity(order.quantity)}</td>
                      <td>{order.order_type ?? "—"}</td>
                      <td className="right">{order.stop_price !== null ? `stop ${decimal(order.stop_price)}` : order.limit_price !== null ? `lmt ${decimal(order.limit_price)}` : "—"}</td>
                      <td>{order.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          <div className="expanded-group">
            <h4>Protective stops</h4>
            {row.protections.length === 0 ? (
              <p className="hint">
                No active protective stop recorded
                {row.cycle ? " for the open cycle" : ""}.
              </p>
            ) : (
              <table className="data-table mini-table">
                <thead>
                  <tr>
                    <th>Kind</th>
                    <th>Status</th>
                    <th className="right">Stop</th>
                    <th className="right">HWM</th>
                    <th className="right">Covered / remaining</th>
                  </tr>
                </thead>
                <tbody>
                  {row.protections.map((protection) => (
                    <tr key={protection.protection_order_id}>
                      <td>{protection.kind ?? "—"}</td>
                      <td>
                        {protection.status}
                        {protection.coverage_gap ? <StaleFlag reason="coverage_gap" /> : null}
                      </td>
                      <td className="right">{decimal(protection.stop_price)}</td>
                      <td className="right">{decimal(protection.high_water_mark)}</td>
                      <td className="right">
                        {quantity(protection.covered_quantity)} / {quantity(protection.remaining_quantity)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </td>
    </tr>
  );
}

export default function PipelineSection({
  cockpit,
  engineOffline,
}: {
  cockpit: CockpitResponse;
  engineOffline: boolean;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const toggle = (symbol: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(symbol)) next.delete(symbol);
      else next.add(symbol);
      return next;
    });

  if (cockpit.symbols.length === 0) {
    return <EmptyState message="No configured trading symbols for this account." />;
  }

  return (
    <div className="table-wrap">
      <table className="data-table pipeline-table">
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Lifecycle phase</th>
            <th>Direction</th>
            <th>Entry watch</th>
            <th className="right">Pending in / out</th>
            <th>Strategies B / S / SC / CV</th>
            <th>Protection</th>
            <th>Gates</th>
            <th>Last evaluation</th>
            <th>Last signal</th>
            <th>Last activity (ET)</th>
          </tr>
        </thead>
        <tbody>
          {cockpit.symbols.map((row) => {
            const isOpen = expanded.has(row.symbol);
            const entryWatch = row.instances.filter(
              (instance) =>
                (instance.role === "BUY" || instance.role === "SHORT") &&
                instance.stage.startsWith("WATCHING"),
            );
            return (
              <Fragment key={row.symbol}>
                <tr
                  className="row-expandable"
                  tabIndex={0}
                  onClick={() => toggle(row.symbol)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      toggle(row.symbol);
                    }
                  }}
                >
                  <td>
                    <button
                      type="button"
                      className="link-button expander"
                      aria-expanded={isOpen}
                      onClick={(event) => {
                        event.stopPropagation();
                        toggle(row.symbol);
                      }}
                      title="Show strategy activity, pending orders and stops"
                    >
                      {isOpen ? "▾" : "▸"}
                    </button>{" "}
                    <Link
                      className="cell-link"
                      to={`/symbols/${encodeURIComponent(row.symbol)}`}
                      onClick={(event) => event.stopPropagation()}
                    >
                      {row.symbol}
                    </Link>
                    {!row.configured ? <span className="cell-sub">not in config</span> : null}
                  </td>
                  <td>
                    <span className={`pill ${phaseClass(row.phase)}`}>{row.phase}</span>
                  </td>
                  <td>{row.direction ?? "—"}</td>
                  <td>
                    {entryWatch.length > 0
                      ? `${entryWatch.map((instance) => `${instance.role}:${instance.strategy_key}`).join(", ")}`
                      : "—"}
                    {row.cycle !== null && row.cycle.state === "PENDING_ENTRY"
                      ? <span className="pill pill-accent">{row.cycle.state}</span>
                      : null}
                  </td>
                  <td className="right">
                    {row.pending_entry_orders} / {row.pending_exit_orders}
                  </td>
                  <td className="cell-strategies">
                    {[
                      row.buy_strategies.length && `B:${row.buy_strategies.join(",")}`,
                      row.sell_strategies.length && `S:${row.sell_strategies.join(",")}`,
                      row.short_strategies.length && `SC:${row.short_strategies.join(",")}`,
                      row.cover_strategies.length && `CV:${row.cover_strategies.join(",")}`,
                    ]
                      .filter(Boolean)
                      .join(" · ") || "—"}
                  </td>
                  <td>
                    {row.protections.length === 0
                      ? row.cycle
                        ? <span className="pill pill-yellow">NO ACTIVE STOP</span>
                        : "—"
                      : row.protections.map((protection) => (
                          <span key={protection.protection_order_id} className="protection-line">
                            <span className="pill pill-green">{protection.status}</span>{" "}
                            {protection.kind ?? ""}
                            {protection.stop_price !== null ? ` @ ${decimal(protection.stop_price)}` : ""}
                            {protection.coverage_gap ? <StaleFlag reason="coverage_gap" /> : null}
                          </span>
                        ))}
                  </td>
                  <td>
                    {row.block_reasons.filter((reason) => reason === "COOLDOWN" || reason === "LOCKOUT").join(", ") || "—"}
                    {row.cooldown_until ? <span className="cell-sub">until {timeEt(row.cooldown_until)}</span> : null}
                    {row.lockout_until ? <span className="cell-sub">lockout until {timeEt(row.lockout_until)}</span> : null}
                  </td>
                  <td>
                    {row.last_evaluation === null ? (
                      <span className="unavailable">not evaluated</span>
                    ) : (
                      <>
                        {row.last_evaluation.status}
                        <span className="cell-sub">
                          {row.last_evaluation.strategy_key ?? "—"} · {timeEt(row.last_evaluation.evaluated_at)}
                        </span>
                      </>
                    )}
                  </td>
                  <td>
                    {row.last_signal === null ? (
                      <span className="unavailable">no signal</span>
                    ) : (
                      <>
                        {row.last_signal.action} <span className={`pill ${row.last_signal.disposition === "REJECTED" ? "pill-red" : "pill-blue"}`}>{row.last_signal.disposition}</span>
                        <span className="cell-sub">{timeEt(row.last_signal.generated_at)}</span>
                      </>
                    )}
                  </td>
                  <td>{row.last_activity_at === null ? "—" : timeEt(row.last_activity_at)}</td>
                </tr>
                {isOpen ? <ExpandedPipeline row={row} engineOffline={engineOffline} /> : null}
              </Fragment>
            );
          })}
        </tbody>
      </table>
      <p className="table-note">
        Rows expand into per-strategy activity, pending orders and protective stops. Phase values
        use the engine's own terminology (WATCHING_BUY / BUY_PENDING / SELL_PENDING / …). Stage
        timestamps marked "last recorded" are the engine's saved state, not live observations.
      </p>
    </div>
  );
}

export function EntriesExplainer({
  cockpit,
}: {
  cockpit: CockpitResponse;
}) {
  if (cockpit.entries_allowed) {
    return (
      <div className="entries-explainer entries-allowed" role="status">
        <strong>ENTRIES ALLOWED</strong>
        <span>New entry evaluation is authorized under the current session and risk state.</span>
      </div>
    );
  }
  return (
    <div className="entries-explainer" role="status">
      <strong>ENTRIES BLOCKED</strong>
      <ul>
        {cockpit.entries_blocked_reasons.map((reason) => (
          <li key={reason}>
            <code>{reason}</code> — {reasonText(reason)}
          </li>
        ))}
      </ul>
    </div>
  );
}
