import { useEffect, useState } from "react";
import {
  ApiError,
  getAccountDetail,
  listAccounts,
} from "../api/client";
import type {
  AccountDetailResponse,
  AccountSummary,
  ActivityRow,
  Balances,
  DriftRow,
  OrderRow,
  PositionRow,
} from "../api/client";

export interface AccountSelectorProps {
  selectedAccountId: string | null;
  onSelect: (accountId: string) => void;
}

export interface AccountDetailProps {
  accountId: string;
}

function scenarioText(account: AccountSummary): string {
  return account.scenario !== null && account.scenario.length > 0 ? account.scenario : "—";
}

function modeGroupOrder(mode: string): number {
  const normalized = mode.toUpperCase();
  if (normalized === "LIVE") return 0;
  if (normalized === "PAPER") return 1;
  if (normalized === "SHADOW") return 2;
  return 3;
}

export function AccountSelector({ selectedAccountId, onSelect }: AccountSelectorProps) {
  const [accounts, setAccounts] = useState<AccountSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    (async () => {
      try {
        const body = await listAccounts(controller.signal);
        if (cancelled || controller.signal.aborted) return;
        setAccounts(body.accounts);
        setError(null);
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        setError(err instanceof ApiError ? err.message : "Account list load failed");
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, []);

  if (error !== null && accounts === null) {
    return (
      <p className="hint">
        Account list unavailable: {error}. Retry with <code>make doctor</code>.
      </p>
    );
  }

  if (accounts === null) {
    return <p className="hint">Loading accounts…</p>;
  }

  if (accounts.length === 0) {
    return <p className="hint">No accounts registered.</p>;
  }

  const modes = [...new Set(accounts.map((account) => account.mode.toUpperCase()))].sort(
    (a, b) => modeGroupOrder(a) - modeGroupOrder(b),
  );

  return (
    <div className="account-selector">
      {modes.map((mode) => (
        <div key={mode} className="account-mode-group">
          <h3 className="account-mode-heading">{mode}</h3>
          <ul className="account-list">
            {accounts
              .filter((account) => account.mode.toUpperCase() === mode)
              .map((account) => {
                const selected = account.account_id === selectedAccountId;
                return (
                  <li key={account.account_id}>
                    <button
                      type="button"
                      className={selected ? "account-button account-selected" : "account-button"}
                      aria-pressed={selected}
                      onClick={() => onSelect(account.account_id)}
                    >
                      <span className="account-mode">{account.mode}</span>
                      <span className="account-scenario">{scenarioText(account)}</span>
                      <span className="account-id">{account.account_id}</span>
                    </button>
                  </li>
                );
              })}
          </ul>
        </div>
      ))}
    </div>
  );
}

function staleTag(fresh: boolean, staleReason: string | null): string | null {
  if (fresh) return null;
  return staleReason !== null && staleReason.length > 0
    ? `STALE (${staleReason})`
    : "STALE";
}

function BalancesCard({ balances }: { balances: Balances }) {
  const stale = staleTag(balances.fresh, balances.stale_reason);
  const money = (value: string | null): string => (value === null ? "—" : value);
  return (
    <div className="detail-card">
      <h3>Balances (broker truth)</h3>
      {stale !== null ? (
        <p className="stale-flag" role="status">
          {stale} — not fresh broker data
        </p>
      ) : null}
      <dl className="detail-grid">
        <div>
          <dt>Cash</dt>
          <dd>{money(balances.cash)}</dd>
        </div>
        <div>
          <dt>Equity</dt>
          <dd>{money(balances.equity)}</dd>
        </div>
        <div>
          <dt>Buying power</dt>
          <dd>{money(balances.buying_power)}</dd>
        </div>
        <div>
          <dt>Portfolio value</dt>
          <dd>{money(balances.portfolio_value)}</dd>
        </div>
        <div>
          <dt>Source</dt>
          <dd>{balances.source ?? "—"}</dd>
        </div>
        <div>
          <dt>As-of</dt>
          <dd>{balances.as_of ?? "—"}</dd>
        </div>
      </dl>
    </div>
  );
}

function PositionsTable({ positions }: { positions: PositionRow[] }) {
  if (positions.length === 0) {
    return (
      <div className="detail-card">
        <h3>Positions (broker truth)</h3>
        <p className="hint">No managed positions reported by the broker.</p>
      </div>
    );
  }
  return (
    <div className="detail-card">
      <h3>Positions (broker truth)</h3>
      <table className="detail-table">
        <thead>
          <tr>
            <th scope="col">Symbol</th>
            <th scope="col">Cycle</th>
            <th scope="col">Direction</th>
            <th scope="col">Quantity</th>
            <th scope="col">Avg entry</th>
            <th scope="col">Market price / value</th>
            <th scope="col">Unrealized P&amp;L</th>
            <th scope="col">As-of / freshness</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((position) => {
            const stale = staleTag(position.fresh, position.stale_reason);
            const key = `${position.symbol}-${position.cycle_id ?? "no-cycle"}`;
            return (
              <tr key={key}>
                <td>{position.symbol}</td>
                <td>{position.cycle_id ?? "—"}</td>
                <td>{position.direction}</td>
                <td>{position.quantity}</td>
                <td>{position.average_entry_price}</td>
                <td>
                  {position.market_price ?? "—"} / {position.market_value ?? "—"}
                </td>
                <td>{position.unrealized_pl ?? "—"}</td>
                <td>
                  {position.as_of}{" "}
                  {stale !== null ? <span className="stale-flag">{stale}</span> : "fresh"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ExternalHoldings({ holdings }: { holdings: PositionRow[] }) {
  if (holdings.length === 0) return null;
  return (
    <div className="detail-card">
      <h3>External / UNMANAGED holdings</h3>
      <p className="hint">
        These holdings are outside managed control and require explicit adoption via CLI before
        they can be managed. No action here adopts them.
      </p>
      <ul>
        {holdings.map((holding) => {
          const stale = staleTag(holding.fresh, holding.stale_reason);
          const key = `${holding.symbol}-${holding.cycle_id ?? "no-cycle"}`;
          return (
            <li key={key}>
              {holding.symbol} · {holding.quantity} @ {holding.average_entry_price}
              {stale !== null ? ` · ${stale}` : ""}
            </li>
          );
        })}
      </ul>
      <p className="hint">
        Adopt explicitly via CLI; inspect first with <code>make doctor</code>.
      </p>
    </div>
  );
}

function OrdersTable({ orders }: { orders: OrderRow[] }) {
  if (orders.length === 0) {
    return (
      <div className="detail-card">
        <h3>Orders</h3>
        <p className="hint">No broker orders recorded for this account.</p>
      </div>
    );
  }
  return (
    <div className="detail-card">
      <h3>Orders</h3>
      <table className="detail-table">
        <thead>
          <tr>
            <th scope="col">Symbol</th>
            <th scope="col">Side / type</th>
            <th scope="col">Status</th>
            <th scope="col">Qty / filled</th>
            <th scope="col">Submitted / updated</th>
            <th scope="col">Freshness</th>
          </tr>
        </thead>
        <tbody>
          {orders.map((order) => {
            const stale = staleTag(order.fresh, order.stale_reason);
            return (
              <tr key={order.broker_order_id}>
                <td>{order.symbol}</td>
                <td>
                  {order.side} / {order.order_type}
                  {order.is_open ? "" : " (closed)"}
                </td>
                <td>{order.status}</td>
                <td>
                  {order.quantity} / {order.filled_quantity}
                </td>
                <td>
                  {order.submitted_at} / {order.updated_at}
                </td>
                <td>
                  {stale !== null ? <span className="stale-flag">{stale}</span> : "fresh"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ActivityList({ activity, totalFees }: { activity: ActivityRow[]; totalFees: string }) {
  return (
    <div className="detail-card">
      <h3>Fees / corporate activity</h3>
      <p>Total fees: {totalFees}</p>
      {activity.length === 0 ? (
        <p className="hint">No broker activity or cash events recorded.</p>
      ) : (
        <ul>
          {activity.map((entry, index) => (
            <li key={`${entry.kind}-${entry.activity_type}-${entry.occurred_at}-${String(index)}`}>
              [{entry.kind}] {entry.activity_type} · {entry.occurred_at} · amount{" "}
              {entry.amount ?? "—"}
              {entry.description !== null && entry.description.length > 0
                ? ` · ${entry.description}`
                : ""}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function DriftList({ drifts }: { drifts: DriftRow[] }) {
  if (drifts.length === 0) return <p className="hint">No unresolved drift.</p>;
  return (
    <ul>
      {drifts.map((drift) => (
        <li key={drift.drift_id}>
          {drift.drift_type} · {drift.entity_ref ?? "—"} · detected {drift.detected_at} ·
          expected {JSON.stringify(drift.expected)} · actual {JSON.stringify(drift.actual)}
        </li>
      ))}
    </ul>
  );
}

export default function AccountDetail({ accountId }: AccountDetailProps) {
  const [detail, setDetail] = useState<AccountDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Fetched data is keyed by accountId: switching accounts discards the
  // previous account's values and aborts in-flight requests.
  useEffect(() => {
    setDetail(null);
    setError(null);
    let cancelled = false;
    const controller = new AbortController();
    (async () => {
      try {
        const body = await getAccountDetail(accountId, controller.signal);
        if (cancelled || controller.signal.aborted) return;
        setDetail(body);
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        setError(err instanceof ApiError ? err.message : "Account detail load failed");
      }
    })();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [accountId]);

  if (error !== null) {
    return (
      <div className="detail-card">
        <h2>Account detail</h2>
        <p>
          Account detail unavailable: {error}. Retry with <code>make doctor</code>.
        </p>
      </div>
    );
  }

  if (detail === null) {
    return (
      <div className="detail-card">
        <h2>Account detail</h2>
        <p className="hint">Loading account detail…</p>
      </div>
    );
  }

  return (
    <div className="account-detail">
      <h2>
        Account detail · {detail.account_id} ({detail.mode})
      </h2>
      <p className="hint">Server time: {detail.server_time}</p>
      <BalancesCard balances={detail.balances} />
      <PositionsTable positions={detail.positions} />
      <ExternalHoldings holdings={detail.external_holdings} />
      <OrdersTable orders={detail.orders} />
      <ActivityList activity={detail.activity} totalFees={detail.total_fees} />
      <div className="detail-card">
        <h3>Reconciliation</h3>
        <p>
          Last run: {detail.reconciliation.last_run_at ?? "—"} · result:{" "}
          {detail.reconciliation.last_result ?? "—"} · unresolved drift:{" "}
          {String(detail.reconciliation.unresolved_drift_count)}
        </p>
        <DriftList drifts={detail.reconciliation.drifts} />
        {detail.reconciliation.unresolved_drift_count > 0 ? (
          <p>
            Resolve drift with <code>make doctor</code>.
          </p>
        ) : null}
      </div>
    </div>
  );
}
