/** Symbol detail page (route-based, replacing the old inline drawer).
 * Preserves signal timeline, evidence/provenance, broker order tree,
 * protection history and managed cycles. */

import { Link, useParams } from "react-router";
import { useSymbolDetail } from "../../state/queries";
import { useSelectedAccount } from "../../state/AccountContext";
import { ErrorBanner, Loading, PageHeader, Section, StaleFlag, TechnicalDetails } from "../../components/ui";
import type { CycleOrderTree, OrderNode, ProtectionEvent } from "../../api/client";
import { dateTimeEt, MISSING, money } from "../../lib/format";

const isAbort = (error: unknown): boolean =>
  error instanceof DOMException && error.name === "AbortError";

function coverageFor(tree: CycleOrderTree): { confirmed: number; protected: number; label: string } {
  let confirmed = 0;
  let prot = 0;
  for (const order of tree.orders) {
    const purpose = (order.purpose ?? "").toUpperCase();
    if (purpose === "ENTRY") confirmed += Number(order.filled_quantity);
    else if (purpose === "PROTECTIVE_STOP") prot += Number(order.quantity);
  }
  if (!Number.isFinite(confirmed)) confirmed = 0;
  if (!Number.isFinite(prot)) prot = 0;
  if (confirmed <= 0) return { confirmed, protected: prot, label: "NO_FILL" };
  if (prot >= confirmed) return { confirmed, protected: prot, label: "COVERED" };
  if (prot > 0) return { confirmed, protected: prot, label: "PARTIAL_COVERAGE" };
  return { confirmed, protected: prot, label: "UNPROTECTED" };
}

function OrderList({ orders }: { orders: OrderNode[] }) {
  const byPurpose = (purpose: string) =>
    orders.filter((order) => (order.purpose ?? "").toUpperCase() === purpose);
  const parents = byPurpose("ENTRY");
  const children = byPurpose("PROTECTIVE_STOP");
  const others = orders.filter(
    (order) => (order.purpose ?? "").toUpperCase() !== "ENTRY" && (order.purpose ?? "").toUpperCase() !== "PROTECTIVE_STOP",
  );
  const render = (order: OrderNode) => {
    const filled = Number(order.filled_quantity);
    const total = Number(order.quantity);
    const partial = Number.isFinite(filled) && Number.isFinite(total) && filled > 0 && filled < total;
    return (
      <li key={order.broker_order_id}>
        [{order.purpose ?? "—"}] {order.side} {order.order_type} · {order.status} · qty {order.quantity} · filled{" "}
        {order.filled_quantity}
        {partial ? " (PARTIAL FILL)" : ""} · avg {order.filled_avg_price ?? "—"} · limit {order.limit_price ?? "—"} · stop{" "}
        {order.stop_price ?? "—"}
        {order.parent_broker_order_id !== null ? ` · parent ${order.parent_broker_order_id}` : ""} ·{" "}
        {order.submitted_at} / {order.updated_at}
      </li>
    );
  };
  return (
    <div>
      {parents.length > 0 ? (
        <div>
          <h5>Parent entry</h5>
          <ul className="plain-list">{parents.map(render)}</ul>
        </div>
      ) : null}
      {children.length > 0 ? (
        <div>
          <h5>Protective stop children</h5>
          <ul className="plain-list">{children.map(render)}</ul>
        </div>
      ) : null}
      {others.length > 0 ? (
        <div>
          <h5>Other orders</h5>
          <ul className="plain-list">{others.map(render)}</ul>
        </div>
      ) : null}
    </div>
  );
}

function trailingKind(event: ProtectionEvent): string {
  const payload = event.payload as Record<string, unknown>;
  const raw =
    typeof payload.kind === "string"
      ? payload.kind
      : typeof payload.trailing_kind === "string"
        ? payload.trailing_kind
        : "";
  const haystack = `${event.event_type} ${raw}`.toUpperCase();
  if (haystack.includes("BROKER_TRAILING_STOP") || haystack.includes("NATIVE")) return "broker-native HWM";
  if (haystack.includes("STEPPED_TRAILING_STOP") || haystack.includes("STEPPED")) return "stepped local anchor";
  return raw.length > 0 ? raw : "unspecified anchor";
}

export default function SymbolDetailPage() {
  const { symbol } = useParams<{ symbol: string }>();
  const { selectedAccountId } = useSelectedAccount();
  const query = useSymbolDetail(selectedAccountId, symbol);
  const detail = query.data ?? null;

  const error =
    query.isError && !(query.error instanceof DOMException && query.error.name === "AbortError")
      ? query.error instanceof Error
        ? query.error.message
        : "Symbol detail load failed"
      : null;

  const cycles = detail?.managed_cycles.slice(0, 20) ?? [];

  return (
    <div className="page page-wide">
      <PageHeader
        title={symbol === undefined ? "Symbol detail" : `Symbol · ${symbol}`}
        subtitle={selectedAccountId ?? undefined}
        actions={<Link className="button" to="/operations">Back to Operations</Link>}
      />

      {error !== null ? (
        <ErrorBanner title="Symbol detail unavailable" message={error} hint="Retry with make doctor." />
      ) : null}
      {query.isPending && !query.isError ? <Loading label="Loading symbol detail…" /> : null}

      {detail !== null ? (
        <>
          {!detail.fresh ? <div className="stale-watermark" role="status">STALE — last-known values, not fresh</div> : null}

          <Section title="Signal timeline">
            {detail.timeline.length === 0 ? (
              <p className="hint">No evaluations or signals recorded (NO_SIGNAL).</p>
            ) : (
              <ul className="plain-list">
                {detail.timeline.map((entry, index) => (
                  <li key={`${entry.kind}-${entry.at}-${index}`}>
                    <time>{dateTimeEt(entry.at)}</time> [{entry.kind}]{" "}
                    {entry.kind === "SIGNAL"
                      ? `${entry.action ?? "—"} ${entry.disposition ?? entry.status}`
                      : `${entry.status}`}{" "}
                    — {entry.reason}
                    {entry.strategy_key !== null ? ` · strategy ${entry.strategy_key}` : ""}
                    {entry.role !== null ? ` (${entry.role})` : ""}
                    {entry.bar_time !== null ? ` · bar ${entry.bar_time}` : ""}
                    {entry.reference_price !== null ? ` · ref ${entry.reference_price}` : ""}
                  </li>
                ))}
              </ul>
            )}
            <p className="hint">NO_SIGNAL evaluations (no resulting signal) are included.</p>
          </Section>

          <div className="panel-grid">
            <Section title="Broker order tree">
              {detail.order_tree.length === 0 ? (
                <p className="hint">No broker orders for this symbol.</p>
              ) : (
                detail.order_tree.map((tree) => {
                  const coverage = coverageFor(tree);
                  return (
                    <div key={tree.cycle_id} className="order-cycle">
                      <h4>
                        Cycle {tree.cycle_id} [{tree.state}]
                      </h4>
                      <p>
                        Confirmed {coverage.confirmed} vs protected {coverage.protected} · coverage:{" "}
                        <strong className={coverage.label === "COVERED" ? "text-positive" : "text-negative"}>
                          {coverage.label}
                        </strong>
                      </p>
                      <OrderList orders={tree.orders} />
                    </div>
                  );
                })
              )}
            </Section>

            <Section title="Protection & trailing history">
              {detail.protection_history.length === 0 ? (
                <p className="hint">No protection changes recorded.</p>
              ) : (
                <ul className="plain-list">
                  {detail.protection_history.map((event, index) => (
                    <li key={`${event.at}-${event.cycle_id}-${index}`}>
                      <time>{dateTimeEt(event.at)}</time> · {event.cycle_id} · {event.event_type} ·{" "}
                      {trailingKind(event)} · previous {event.previous_stop_price ?? MISSING} → new{" "}
                      {event.new_stop_price ?? MISSING} · proposed {event.proposed_stop_price ?? MISSING} vs
                      confirmed {event.confirmed_stop_price ?? MISSING}
                    </li>
                  ))}
                </ul>
              )}
            </Section>
          </div>

          <div className="panel-grid">
            <Section title="Managed cycles">
              {cycles.length === 0 ? (
                <p className="hint">No managed cycles recorded.</p>
              ) : (
                <div className="table-wrap">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Cycle</th>
                        <th>State</th>
                        <th>Strategy</th>
                        <th>Opened (ET)</th>
                        <th>Closed (ET)</th>
                        <th className="right">Holding</th>
                        <th className="right">Realized P&L</th>
                        <th className="right">Entry slippage</th>
                      </tr>
                    </thead>
                    <tbody>
                      {cycles.map((cycle) => (
                        <tr key={cycle.cycle_id}>
                          <td className="cell-mono">{cycle.cycle_id}</td>
                          <td>{cycle.state}</td>
                          <td>{cycle.entry_strategy_key ?? "—"}</td>
                          <td>{cycle.opened_at === null ? "—" : dateTimeEt(cycle.opened_at)}</td>
                          <td>{cycle.closed_at === null ? "—" : dateTimeEt(cycle.closed_at)}</td>
                          <td className="right">
                            {cycle.holding_seconds === null ? "—" : `${cycle.holding_seconds}s`}
                          </td>
                          <td className="right">{money(cycle.realized_pnl, true)}</td>
                          <td className="right">{cycle.entry_slippage ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Section>

            <Section title="Evidence & feed provenance">
              <dl className="kv-grid kv-compact">
                <div>
                  <dt>Feed</dt>
                  <dd>
                    {detail.feed_provenance.feed ?? "—"}
                    {detail.feed_provenance.feed?.toLowerCase() === "iex"
                      ? " (single-exchange IEX tape, not consolidated SIP)"
                      : ""}
                  </dd>
                </div>
                <div>
                  <dt>Provenance</dt>
                  <dd>{detail.feed_provenance.provenance ?? "—"}</dd>
                </div>
                <div>
                  <dt>Consolidated</dt>
                  <dd>{detail.feed_provenance.consolidated === null ? "—" : String(detail.feed_provenance.consolidated)}</dd>
                </div>
                <div>
                  <dt>Last bar</dt>
                  <dd>{detail.feed_provenance.last_bar_at ?? "—"}</dd>
                </div>
                <div>
                  <dt>Recorded</dt>
                  <dd>{detail.feed_provenance.recorded_at ?? "—"}</dd>
                </div>
              </dl>
              {detail.evidence.length === 0 ? (
                <p className="hint">No trade-relevant evidence recorded.</p>
              ) : (
                <TechnicalDetails summary="Raw evidence">
                  <ul className="plain-list">
                    {detail.evidence.map((entry) => (
                      <li key={entry.signal_id}>
                        {entry.signal_id} · {entry.generated_at} · {JSON.stringify(entry.provenance)}
                      </li>
                    ))}
                  </ul>
                </TechnicalDetails>
              )}
            </Section>
          </div>

          <p className="hint">Read-only view. No element here submits orders or changes configuration.</p>
        </>
      ) : null}
    </div>
  );
}
