/** Operations cockpit (Wave 2D): account summary, trading session state,
 * entry-block explanations, symbol pipeline, per-strategy activity, position
 * detail, orders and the operational event timeline. Diagnostics and raw
 * provenance live in expandable sections; every displayed value comes from
 * the engine's persisted tables — nothing is reconstructed or fabricated. */

import { Link } from "react-router";
import {
  useAccountDetail,
  useCockpit,
  useHealth,
  useKpis,
  useStatus,
} from "../../state/queries";
import { useSelectedAccount } from "../../state/AccountContext";
import {
  EmptyState,
  ErrorBanner,
  KpiTile,
  Loading,
  PageHeader,
  Section,
  StaleFlag,
  TechnicalDetails,
} from "../../components/ui";
import { isStale, sourcedAsOf, staleReason, sourcedProvenance, sourcedText } from "../../lib/sourced";
import { dateTimeEt, money, quantity } from "../../lib/format";
import PipelineSection, { EntriesExplainer } from "./PipelineSection";
import PositionsSection from "./PositionsSection";
import TimelineSection from "./TimelineSection";
import type { KpisResponse } from "../../api/client";
import "./operations.css";

function pnr(node: unknown): "positive" | "negative" | undefined {
  const n = Number(sourcedText(node).replace("+", ""));
  if (!Number.isFinite(n) || n === 0) return undefined;
  return n > 0 ? "positive" : "negative";
}

function KpiRibbon({ kpis }: { kpis: KpisResponse }) {
  const primary: Array<{ key: keyof KpisResponse; label: string; pnl?: boolean }> = [
    { key: "equity", label: "Equity" },
    { key: "cash", label: "Cash" },
    { key: "buying_power", label: "Buying power" },
    { key: "deployed_capital", label: "Deployed capital" },
    { key: "reserved_pending_buys", label: "Reserved pending buys" },
    { key: "daily_realized_pnl", label: "Daily realized P&L", pnl: true },
    { key: "daily_unrealized_pnl", label: "Daily unrealized P&L", pnl: true },
    { key: "open_position_count", label: "Open positions" },
    { key: "pending_order_count", label: "Pending orders" },
  ];
  return (
    <div className="kpi-ribbon">
      {primary.map((entry) => {
        const node = kpis[entry.key];
        const stale = isStale(node) ? staleReason(node) : null;
        const raw = sourcedText(node);
        const value = entry.pnl === true ? money(raw, true) : raw;
        return (
          <KpiTile
            key={entry.key}
            label={entry.label}
            value={value}
            stale={stale}
            tone={entry.pnl === true ? pnr(node) : undefined}
            meta={sourcedProvenance(node)}
          />
        );
      })}
    </div>
  );
}

function AllKpis({ kpis }: { kpis: KpisResponse }) {
  const secondary: Array<[keyof KpisResponse, string]> = [
    ["configured_total_budget", "Total budget"],
    ["lifetime_realized_pnl", "Lifetime realized P&L"],
    ["lifetime_pnl", "Lifetime P&L"],
    ["protection_error_count", "Protection errors"],
    ["data_error_count", "Data errors"],
    ["reconciliation_error_count", "Reconciliation errors"],
  ];
  return (
    <dl className="kv-grid">
      {secondary.map(([key, label]) => {
        const node = kpis[key];
        return (
          <div key={key}>
            <dt>
              {label}
              {isStale(node) ? <StaleFlag reason={staleReason(node)} /> : null}
            </dt>
            <dd>{sourcedText(node)}</dd>
            <dd className="kv-meta">
              as-of {dateTimeEt(sourcedAsOf(node))} · {sourcedProvenance(node) ?? "no provenance"}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

function OrdersPanel({
  orders,
}: {
  orders: Array<Record<string, unknown>>;
}) {
  if (orders.length === 0) return <EmptyState message="No broker orders recorded for this account." />;
  const open = orders.filter((order) => order.is_open === true);
  const closed = orders.filter((order) => order.is_open !== true);
  const render = (order: Record<string, unknown>, key: string) => (
    <tr key={key} className={order.is_open === true ? "row-open" : undefined}>
      <td className="cell-symbol">{String(order.symbol ?? "—")}</td>
      <td>
        {String(order.side ?? "—")} {String(order.order_type ?? "")}
      </td>
      <td>
        <span className="pill pill-neutral">{String(order.status ?? "—")}</span>
      </td>
      <td className="right">
        {quantity(order.filled_quantity as string)} / {quantity(order.quantity as string)}
      </td>
      <td>{dateTimeEt(order.submitted_at as string)}</td>
      <td>{dateTimeEt(order.updated_at as string)}</td>
      <td>{order.fresh === false ? <StaleFlag reason={String(order.stale_reason ?? "")} /> : null}</td>
    </tr>
  );
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Side / type</th>
            <th>Status</th>
            <th className="right">Filled / qty</th>
            <th>Submitted (ET)</th>
            <th>Updated (ET)</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {open.map((order) => render(order, String(order.broker_order_id ?? `open-${String(order.client_order_id)}`)))}
          {closed.length > 0 ? (
            <tr className="row-divider">
              <td colSpan={7}>Closed orders ({closed.length})</td>
            </tr>
          ) : null}
          {closed
            .slice(-10)
            .reverse()
            .map((order) => render(order, String(order.broker_order_id ?? `closed-${String(order.client_order_id)}`)))}
        </tbody>
      </table>
    </div>
  );
}

function ActivityList({
  activity,
  totalFees,
}: {
  activity: Array<Record<string, unknown>>;
  totalFees: string;
}) {
  if (activity.length === 0) {
    return <p className="hint">No broker activity or cash events recorded.</p>;
  }
  const recent = activity.slice(0, 8);
  return (
    <div className="table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th>When (ET)</th>
            <th>Kind</th>
            <th>Type</th>
            <th className="right">Amount</th>
            <th>Description</th>
          </tr>
        </thead>
        <tbody>
          {recent.map((entry, index) => (
            <tr key={`${String(entry.kind)}-${String(entry.activity_type)}-${index}`}>
              <td>{dateTimeEt(entry.occurred_at as string)}</td>
              <td>{String(entry.kind ?? "—")}</td>
              <td>{String(entry.activity_type ?? "—")}</td>
              <td className="right">{entry.amount === null ? "—" : String(entry.amount)}</td>
              <td className="cell-sub">{String(entry.description ?? "")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="table-note">
        Showing {recent.length} of {activity.length} entries · total fees {totalFees}
      </p>
    </div>
  );
}

function ReconciliationPanel({ reconciliation }: { reconciliation: Record<string, unknown> }) {
  const drifts = Array.isArray(reconciliation.drifts)
    ? (reconciliation.drifts as Array<Record<string, unknown>>)
    : [];
  const count = Number(reconciliation.unresolved_drift_count ?? drifts.length);
  return (
    <div>
      <dl className="kv-grid kv-compact">
        <div>
          <dt>Last run</dt>
          <dd>{dateTimeEt(reconciliation.last_run_at as string | null)}</dd>
        </div>
        <div>
          <dt>Result</dt>
          <dd>{String(reconciliation.last_result ?? "—")}</dd>
        </div>
        <div>
          <dt>Unresolved drift</dt>
          <dd className={count > 0 ? "text-negative" : undefined}>{count}</dd>
        </div>
      </dl>
      {count > 0 ? (
        <>
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Type</th>
                  <th>Entity</th>
                  <th>Expected</th>
                  <th>Actual</th>
                  <th>Detected (ET)</th>
                </tr>
              </thead>
              <tbody>
                {drifts.map((drift, index) => (
                  <tr key={String(drift.drift_id ?? index)}>
                    <td>{String(drift.drift_type ?? "—")}</td>
                    <td>{String(drift.entity_ref ?? "—")}</td>
                    <td className="cell-mono">{JSON.stringify(drift.expected)}</td>
                    <td className="cell-mono">{JSON.stringify(drift.actual)}</td>
                    <td>{dateTimeEt(drift.detected_at as string)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="hint">
            Resolve drift with <code>make doctor</code>.
          </p>
        </>
      ) : null}
    </div>
  );
}

export default function OperationsPage() {
  const { selectedAccountId } = useSelectedAccount();
  const statusQuery = useStatus(selectedAccountId);
  const kpisQuery = useKpis(selectedAccountId);
  const healthQuery = useHealth(selectedAccountId);
  const cockpitQuery = useCockpit(selectedAccountId);
  const detailQuery = useAccountDetail(selectedAccountId);

  if (selectedAccountId === null) return null;

  const status = statusQuery.data ?? null;
  const kpis = kpisQuery.data ?? null;
  const health = healthQuery.data ?? null;
  const cockpit = cockpitQuery.data ?? null;
  const detail = detailQuery.data ?? null;

  const loadError =
    statusQuery.isError || kpisQuery.isError || healthQuery.isError || cockpitQuery.isError
      ? (statusQuery.error ?? kpisQuery.error ?? healthQuery.error ?? cockpitQuery.error)
      : null;

  const errors = health?.errors ?? [];
  const engineOffline = (status?.engine_state ?? "OFFLINE") === "OFFLINE";

  return (
    <div className="page">
      <PageHeader
        title="Operations"
        subtitle={`${selectedAccountId} · ${status?.mode?.toUpperCase() ?? "—"}${
          status?.scenario ? ` · ${status.scenario}` : ""
        } · server time ${dateTimeEt(status?.server_time ?? null)}`}
        actions={
          <>
            <Link className="button" to="/watchlist">
              Watchlist
            </Link>
            <Link className="button" to="/health">
              Health & diagnostics
            </Link>
          </>
        }
      />

      {errors.length > 0 ? (
        <div className="banner banner-warning" role="alert">
          <strong>
            {errors.length} actionable health issue{errors.length === 1 ? "" : "s"}
          </strong>
          <span>
            {errors
              .slice(0, 3)
              .map((error) => `${error.code}: ${error.message}`)
              .join(" · ")}
            {errors.length > 3 ? " …" : ""}
          </span>
          <Link to="/health">Open Health</Link>
        </div>
      ) : null}

      {loadError !== null && status === null && kpis === null ? (
        <ErrorBanner
          title="Operations data unavailable"
          message={loadError instanceof Error ? loadError.message : "Unknown error"}
          hint="Retry with make doctor."
        />
      ) : null}

      {kpis === null && !kpisQuery.isError ? <Loading label="Loading account KPIs…" /> : null}
      {kpis !== null ? (
        <Section title="Account summary">
          <KpiRibbon kpis={kpis} />
          <TechnicalDetails summary="Full KPI set & provenance">
            <AllKpis kpis={kpis} />
          </TechnicalDetails>
        </Section>
      ) : null}

      {status !== null ? (
        <Section title="Trading session & engine">
          <dl className="kv-grid">
            <div>
              <dt>Engine</dt>
              <dd>{status.engine_state}</dd>
            </div>
            <div>
              <dt>Heartbeat</dt>
              <dd>{dateTimeEt(status.last_heartbeat_at_et ?? status.last_heartbeat_at)}</dd>
            </div>
            <div>
              <dt>Broker link</dt>
              <dd>
                {status.broker.status} · {status.broker.fresh ? "fresh" : "stale"}
              </dd>
            </div>
            <div>
              <dt>Data link</dt>
              <dd>
                {status.data.status} · {status.data.fresh ? "fresh" : "stale"}
              </dd>
            </div>
            <div>
              <dt>Market session</dt>
              <dd>
                {status.market} · {status.exchange_clock.session_status}
              </dd>
            </div>
            <div>
              <dt>Next entry cutoff</dt>
              <dd>
                {status.next_entry_cutoff.at_et ?? status.next_entry_cutoff.at ?? "—"}
                {status.next_entry_cutoff.passed_today ? " (passed today)" : ""}
              </dd>
            </div>
          </dl>
          <EntriesExplainer
            cockpit={{
              account_id: status.account_id,
              mode: status.mode,
              engine_state: status.engine_state,
              server_time: status.server_time,
              entries_allowed: status.entries_allowed,
              entries_blocked_reasons: status.entries_blocked_reasons,
              symbols: [],
            }}
          />
        </Section>
      ) : null}

      {cockpit !== null ? (
        <Section title={`Symbol pipeline (${cockpit.symbols.length})`}>
          <PipelineSection cockpit={cockpit} engineOffline={engineOffline} />
        </Section>
      ) : cockpitQuery.isPending && !cockpitQuery.isError ? (
        <Section title="Symbol pipeline">
          <Loading label="Loading pipeline…" />
        </Section>
      ) : null}

      {detail !== null ? (
        <Section title="Open positions">
          <PositionsSection positions={detail.positions} pipelines={cockpit?.symbols ?? []} />
        </Section>
      ) : null}

      {detail !== null ? (
        <Section title="Orders">
          <OrdersPanel orders={detail.orders as unknown as Array<Record<string, unknown>>} />
        </Section>
      ) : null}

      <Section title="Activity timeline">
        <TimelineSection accountId={selectedAccountId} cockpit={cockpit} />
      </Section>

      {detail !== null ? (
        <div className="panel-grid">
          <Section title="Recent broker activity">
            <ActivityList
              activity={detail.activity as unknown as Array<Record<string, unknown>>}
              totalFees={detail.total_fees}
            />
          </Section>
          <Section title="Reconciliation">
            <ReconciliationPanel
              reconciliation={detail.reconciliation as unknown as Record<string, unknown>}
            />
          </Section>
        </div>
      ) : null}

      {detail !== null && detail.external_holdings.length > 0 ? (
        <Section title="External / unmanaged holdings" tone="warning">
          <ul className="plain-list">
            {detail.external_holdings.map((holding, index) => (
              <li key={`${holding.symbol}-${index}`}>
                {holding.symbol} · {holding.quantity} @ {holding.average_entry_price} ·{" "}
                {holding.fresh ? "fresh" : "stale"} — adopt explicitly via CLI; inspect first with{" "}
                <code>make doctor</code>
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {health !== null ? (
        <TechnicalDetails summary="Internal diagnostics (feed, broker, persistence, scheduler)">
          <dl className="kv-grid">
            <div>
              <dt>Feed</dt>
              <dd>
                {health.feed.link.status} · age {sourcedText(health.feed.age_seconds)}s · reconnects{" "}
                {sourcedText(health.feed.reconnects)}
              </dd>
            </div>
            <div>
              <dt>Broker rate-limit errors</dt>
              <dd>{sourcedText(health.broker.rate_limit_errors)}</dd>
            </div>
            <div>
              <dt>Scheduler lag</dt>
              <dd>{sourcedText(health.scheduler_lag_seconds)}s</dd>
            </div>
            <div>
              <dt>Persistence</dt>
              <dd>
                schema v{health.persistence.schema_version ?? "—"} · {health.persistence.database_path}
              </dd>
            </div>
            <div>
              <dt>WAL size</dt>
              <dd>{health.persistence.wal_size_bytes ?? "—"} bytes</dd>
            </div>
            <div>
              <dt>Last backup</dt>
              <dd>
                {dateTimeEt(health.persistence.last_backup_at.value)} ·{" "}
                {health.persistence.last_backup_integrity_ok === null
                  ? "integrity unknown"
                  : health.persistence.last_backup_integrity_ok
                    ? "integrity ok"
                    : "integrity failed"}
              </dd>
            </div>
          </dl>
        </TechnicalDetails>
      ) : null}
    </div>
  );
}
