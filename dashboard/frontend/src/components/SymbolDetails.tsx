import { useEffect, useMemo, useState } from "react";
import { ApiError, getSymbolDetail } from "../api/client";
import type {
  CycleOrderTree,
  ManagedCycle,
  OrderNode,
  ProtectionEvent,
  SymbolDetailResponse,
  TimelineEntry,
} from "../api/client";

export interface SymbolDetailsProps {
  accountId: string;
  symbol: string;
  onClose: () => void;
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : "Symbol detail load failed";
}

function timelineLabel(entry: TimelineEntry): string {
  if (entry.kind === "SIGNAL") {
    const action = entry.action ?? "—";
    const disposition = entry.disposition ?? entry.status;
    return `SIGNAL ${action} ${disposition} — ${entry.reason}`;
  }
  return `EVALUATION ${entry.status} — ${entry.reason}`;
}

/** Confirmed entry fill vs protective-stop coverage derived from the broker order tree. */
function coverageFor(tree: CycleOrderTree): {
  confirmed: number;
  protected: number;
  label: string;
} {
  let confirmed = 0;
  let prot = 0;
  for (const order of tree.orders) {
    if ((order.purpose ?? "").toUpperCase() === "ENTRY") {
      confirmed += Number(order.filled_quantity);
    } else if ((order.purpose ?? "").toUpperCase() === "PROTECTIVE_STOP") {
      prot += Number(order.quantity);
    }
  }
  if (!Number.isFinite(confirmed)) confirmed = 0;
  if (!Number.isFinite(prot)) prot = 0;
  let label = "UNPROTECTED";
  if (confirmed <= 0) label = "NO_FILL";
  else if (prot >= confirmed) label = "COVERED";
  else if (prot > 0) label = "PARTIAL_COVERAGE";
  return { confirmed, protected: prot, label };
}

function OrderList({ orders }: { orders: OrderNode[] }) {
  const parents = orders.filter((order) => (order.purpose ?? "").toUpperCase() === "ENTRY");
  const children = orders.filter(
    (order) => (order.purpose ?? "").toUpperCase() === "PROTECTIVE_STOP",
  );
  const others = orders.filter(
    (order) =>
      (order.purpose ?? "").toUpperCase() !== "ENTRY" &&
      (order.purpose ?? "").toUpperCase() !== "PROTECTIVE_STOP",
  );
  const renderOrder = (order: OrderNode) => {
    const filled = Number(order.filled_quantity);
    const total = Number(order.quantity);
    const partial = Number.isFinite(filled) && Number.isFinite(total) && filled > 0 && filled < total;
    return (
      <li key={order.broker_order_id}>
        [{order.purpose ?? "—"}] {order.side} {order.order_type} · {order.status} · qty{" "}
        {order.quantity} · filled {order.filled_quantity}
        {partial ? " (PARTIAL FILL)" : ""} · avg {order.filled_avg_price ?? "—"} · limit{" "}
        {order.limit_price ?? "—"} · stop {order.stop_price ?? "—"}
        {order.parent_broker_order_id !== null
          ? ` · parent ${order.parent_broker_order_id}`
          : ""}{" "}
        · {order.submitted_at} / {order.updated_at}
      </li>
    );
  };
  return (
    <div>
      {parents.length > 0 ? (
        <div>
          <h5>Parent entry</h5>
          <ul>{parents.map(renderOrder)}</ul>
        </div>
      ) : null}
      {children.length > 0 ? (
        <div>
          <h5>Protective stop children</h5>
          <ul>{children.map(renderOrder)}</ul>
        </div>
      ) : null}
      {others.length > 0 ? (
        <div>
          <h5>Other orders</h5>
          <ul>{others.map(renderOrder)}</ul>
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
  if (haystack.includes("BROKER_TRAILING_STOP") || haystack.includes("NATIVE")) {
    return "broker-native HWM";
  }
  if (haystack.includes("STEPPED_TRAILING_STOP") || haystack.includes("STEPPED")) {
    return "stepped local anchor";
  }
  return raw.length > 0 ? raw : "unspecified anchor";
}

function CycleCard({ cycle }: { cycle: ManagedCycle }) {
  return (
    <li className="cycle-card">
      <strong>
        {cycle.cycle_id} [{cycle.state}]
      </strong>{" "}
      · strategy {cycle.entry_strategy_key ?? "—"} · opened {cycle.opened_at ?? "—"} · closed{" "}
      {cycle.closed_at ?? "—"} · holding{" "}
      {cycle.holding_seconds === null ? "—" : `${String(cycle.holding_seconds)}s`} · realized P&amp;L{" "}
      {cycle.realized_pnl ?? "—"}
      <span className="row-sub">
        entry ref {cycle.entry_reference_price ?? "—"} / avg {cycle.entry_average_price ?? "—"}{" "}
        / slippage {cycle.entry_slippage ?? "—"} · exit ref {cycle.exit_reference_price ?? "—"} /
        avg {cycle.exit_average_price ?? "—"} / slippage {cycle.exit_slippage ?? "—"}
      </span>
      {cycle.decision_reasons.length > 0 ? (
        <span className="row-sub">policy reasons: {cycle.decision_reasons.join("; ")}</span>
      ) : (
        <span className="row-sub">policy reasons: —</span>
      )}
    </li>
  );
}

export default function SymbolDetails({ accountId, symbol, onClose }: SymbolDetailsProps) {
  const [detail, setDetail] = useState<SymbolDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDetail(null);
    setError(null);
    let cancelled = false;
    const controller = new AbortController();
    (async () => {
      try {
        const body = await getSymbolDetail(accountId, symbol, controller.signal);
        if (cancelled || controller.signal.aborted) return;
        setDetail(body);
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        if (isAbort(err)) return;
        setError(errorText(err));
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [accountId, symbol]);

  const cycles = useMemo(() => detail?.managed_cycles.slice(0, 20) ?? [], [detail]);

  return (
    <section aria-label={`Symbol detail ${symbol}`} className="symbol-details">
      <div className="symbol-details-header">
        <h2>
          {symbol} · {accountId}
        </h2>
        <button type="button" onClick={onClose}>
          Close
        </button>
      </div>

      {error !== null ? (
        <p>
          Symbol detail unavailable: {error}. Retry with <code>make doctor</code>.
        </p>
      ) : detail === null ? (
        <p className="hint">Loading symbol detail…</p>
      ) : (
        <div>
          {!detail.fresh ? (
            <p className="stale-flag" role="status">
              STALE{detail.stale_reason !== null ? ` (${detail.stale_reason})` : ""} — last-known
              values, not fresh
            </p>
          ) : null}
          <p className="hint">
            Mode {detail.mode} · server time {detail.server_time}
          </p>

          <div className="detail-card">
            <h3>Signal timeline</h3>
            {detail.timeline.length === 0 ? (
              <p className="hint">No evaluations or signals recorded (NO_SIGNAL).</p>
            ) : (
              <ul>
                {detail.timeline.map((entry, index) => (
                  <li key={`${entry.kind}-${entry.at}-${String(index)}`}>
                    [{entry.kind}] {entry.at} · {timelineLabel(entry)}
                    {entry.strategy_key !== null ? ` · strategy ${entry.strategy_key}` : ""}
                    {entry.role !== null ? ` (${entry.role})` : ""}
                    {entry.bar_time !== null ? ` · bar ${entry.bar_time}` : ""}
                    {entry.reference_price !== null ? ` · ref ${entry.reference_price}` : ""}
                  </li>
                ))}
              </ul>
            )}
            <p className="hint">
              NO_SIGNAL summaries (evaluations with no resulting signal) are included above.
            </p>
          </div>

          <div className="detail-card">
            <h3>Evidence &amp; feed provenance</h3>
            <p>
              Feed: {detail.feed_provenance.feed ?? "—"} · provenance:{" "}
              {detail.feed_provenance.provenance ?? "—"} · consolidated:{" "}
              {detail.feed_provenance.consolidated === null
                ? "—"
                : String(detail.feed_provenance.consolidated)}
              {detail.feed_provenance.feed !== null &&
              detail.feed_provenance.feed.toLowerCase() === "iex"
                ? " (single-exchange IEX tape, not consolidated SIP)"
                : ""}
            </p>
            <p>
              Last bar: {detail.feed_provenance.last_bar_at ?? "—"} · recorded:{" "}
              {detail.feed_provenance.recorded_at ?? "—"}
            </p>
            {detail.evidence.length === 0 ? (
              <p className="hint">No trade-relevant evidence recorded.</p>
            ) : (
              <ul>
                {detail.evidence.map((entry) => (
                  <li key={entry.signal_id}>
                    {entry.signal_id} · {entry.generated_at} · {JSON.stringify(entry.provenance)}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="detail-card">
            <h3>Broker order tree</h3>
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
                      Remaining confirmed quantity {String(coverage.confirmed)} vs protected
                      quantity {String(coverage.protected)} · coverage:{" "}
                      <strong>{coverage.label}</strong>
                    </p>
                    <OrderList orders={tree.orders} />
                  </div>
                );
              })
            )}
          </div>

          <div className="detail-card">
            <h3>Protection &amp; trailing history</h3>
            {detail.protection_history.length === 0 ? (
              <p className="hint">No protection changes recorded.</p>
            ) : (
              <ul>
                {detail.protection_history.map((event, index) => (
                  <li key={`${event.at}-${event.cycle_id}-${String(index)}`}>
                    {event.at} · {event.cycle_id} · {event.event_type} · {trailingKind(event)} ·
                    previous {event.previous_stop_price ?? "—"} → new{" "}
                    {event.new_stop_price ?? "—"} · proposed {event.proposed_stop_price ?? "—"} vs
                    confirmed {event.confirmed_stop_price ?? "—"}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="detail-card">
            <h3>Managed cycles (last {String(cycles.length)} of up to 20)</h3>
            {cycles.length === 0 ? (
              <p className="hint">No managed cycles recorded.</p>
            ) : (
              <ul>
                {cycles.map((cycle) => (
                  <CycleCard key={cycle.cycle_id} cycle={cycle} />
                ))}
              </ul>
            )}
          </div>

          <p className="hint">
            Read-only view. No element here submits orders or changes configuration. Follow up
            with <code>make doctor</code>.
          </p>
        </div>
      )}
    </section>
  );
}
