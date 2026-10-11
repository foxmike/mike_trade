/** Account detail page: broker-truth balances, positions, external holdings,
 * orders, fees/activity and reconciliation. */

import { useAccountDetail } from "../../state/queries";
import { useSelectedAccount } from "../../state/AccountContext";
import { ErrorBanner, Loading, PageHeader, Section, StaleFlag } from "../../components/ui";
import { dateTimeEt, money } from "../../lib/format";

function staleTag(fresh: boolean, staleReason: string | null): string | null {
  if (fresh) return null;
  return staleReason !== null && staleReason.length > 0 ? staleReason : "stale";
}

export default function AccountDetailPage() {
  const { selectedAccountId } = useSelectedAccount();
  const query = useAccountDetail(selectedAccountId);
  const detail = query.data ?? null;

  const error = query.isError
    ? query.error instanceof Error
      ? query.error.message
      : "Account detail load failed"
    : null;

  if (selectedAccountId === null) return null;

  return (
    <div className="page page-wide">
      <PageHeader
        title="Account detail"
        subtitle={`${selectedAccountId} · broker-truth balances and lifecycle · server time ${dateTimeEt(detail?.server_time ?? null)}`}
      />

      {error !== null ? (
        <ErrorBanner title="Account detail unavailable" message={error} hint="Retry with make doctor." />
      ) : null}
      {query.isPending && !query.isError ? <Loading label="Loading account detail…" /> : null}

      {detail !== null ? (
        <>
          <Section title="Balances">
            {(() => {
              const stale = staleTag(detail.balances.fresh, detail.balances.stale_reason);
              return (
                <>
                  {stale !== null ? (
                    <p className="stale-flag" role="status">
                      {stale} — not fresh broker data
                    </p>
                  ) : null}
                  <dl className="kv-grid">
                    <div>
                      <dt>Cash</dt>
                      <dd>{money(detail.balances.cash)}</dd>
                    </div>
                    <div>
                      <dt>Equity</dt>
                      <dd>{money(detail.balances.equity)}</dd>
                    </div>
                    <div>
                      <dt>Buying power</dt>
                      <dd>{money(detail.balances.buying_power)}</dd>
                    </div>
                    <div>
                      <dt>Portfolio value</dt>
                      <dd>{money(detail.balances.portfolio_value)}</dd>
                    </div>
                    <div>
                      <dt>Source</dt>
                      <dd>{detail.balances.source ?? "—"}</dd>
                    </div>
                    <div>
                      <dt>As-of (ET)</dt>
                      <dd>{dateTimeEt(detail.balances.as_of)}</dd>
                    </div>
                  </dl>
                </>
              );
            })()}
          </Section>

          <Section title="Positions">
            {detail.positions.length === 0 ? (
              <p className="hint">No managed positions reported by the broker.</p>
            ) : (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Symbol</th>
                      <th>Cycle</th>
                      <th>Direction</th>
                      <th className="right">Quantity</th>
                      <th className="right">Avg entry</th>
                      <th className="right">Mark / value</th>
                      <th className="right">Unrealized P&L</th>
                      <th>As-of (ET) / freshness</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.positions.map((position) => {
                      const stale = staleTag(position.fresh, position.stale_reason);
                      const key = `${position.symbol}-${position.cycle_id ?? "no-cycle"}`;
                      return (
                        <tr key={key} className={stale !== null ? "row-stale" : undefined}>
                          <td className="cell-symbol">{position.symbol}</td>
                          <td className="cell-mono">{position.cycle_id ?? "—"}</td>
                          <td>{position.direction}</td>
                          <td className="right">{position.quantity}</td>
                          <td className="right">{position.average_entry_price}</td>
                          <td className="right">
                            {position.market_price ?? "—"} / {position.market_value ?? "—"}
                          </td>
                          <td className="right">{position.unrealized_pl ?? "—"}</td>
                          <td>
                            {dateTimeEt(position.as_of)}{" "}
                            {stale !== null ? <StaleFlag reason={stale} /> : "fresh"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
            {detail.external_holdings.length > 0 ? (
              <p className="hint">
                {detail.external_holdings.length} external/unmanaged holding(s) — adopt explicitly
                via CLI; inspect first with <code>make doctor</code>.
              </p>
            ) : null}
          </Section>

          <Section title="Orders">
            {detail.orders.length === 0 ? (
              <p className="hint">No broker orders recorded for this account.</p>
            ) : (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Symbol</th>
                      <th>Side / type</th>
                      <th>Status</th>
                      <th className="right">Qty / filled</th>
                      <th>Submitted (ET)</th>
                      <th>Updated (ET)</th>
                      <th>Freshness</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.orders.map((order) => {
                      const stale = staleTag(order.fresh, order.stale_reason);
                      return (
                        <tr key={order.broker_order_id}>
                          <td className="cell-symbol">{order.symbol}</td>
                          <td>
                            {order.side} / {order.order_type}
                            {order.is_open ? "" : " (closed)"}
                          </td>
                          <td>{order.status}</td>
                          <td className="right">
                            {order.quantity} / {order.filled_quantity}
                          </td>
                          <td>{dateTimeEt(order.submitted_at)}</td>
                          <td>{dateTimeEt(order.updated_at)}</td>
                          <td>{stale !== null ? <StaleFlag reason={stale} /> : "fresh"}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Section>

          <Section title="Fees & corporate activity">
            <p className="hint">Total fees: {detail.total_fees}</p>
            {detail.activity.length === 0 ? (
              <p className="hint">No broker activity or cash events recorded.</p>
            ) : (
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
                    {detail.activity.map((entry, index) => (
                      <tr key={`${entry.kind}-${entry.activity_type}-${entry.occurred_at}-${index}`}>
                        <td>{dateTimeEt(entry.occurred_at)}</td>
                        <td>{entry.kind}</td>
                        <td>{entry.activity_type}</td>
                        <td className="right">{entry.amount ?? "—"}</td>
                        <td>{entry.description ?? ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Section>

          <Section title="Reconciliation">
            <dl className="kv-grid kv-compact">
              <div>
                <dt>Last run</dt>
                <dd>{dateTimeEt(detail.reconciliation.last_run_at)}</dd>
              </div>
              <div>
                <dt>Result</dt>
                <dd>{detail.reconciliation.last_result ?? "—"}</dd>
              </div>
              <div>
                <dt>Unresolved drift</dt>
                <dd className={detail.reconciliation.unresolved_drift_count > 0 ? "text-negative" : undefined}>
                  {detail.reconciliation.unresolved_drift_count}
                </dd>
              </div>
            </dl>
            {detail.reconciliation.drifts.length === 0 ? (
              <p className="hint">No unresolved drift.</p>
            ) : (
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
                    {detail.reconciliation.drifts.map((drift) => (
                      <tr key={drift.drift_id}>
                        <td>{drift.drift_type}</td>
                        <td>{drift.entity_ref ?? "—"}</td>
                        <td className="cell-mono">{JSON.stringify(drift.expected)}</td>
                        <td className="cell-mono">{JSON.stringify(drift.actual)}</td>
                        <td>{dateTimeEt(drift.detected_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {detail.reconciliation.unresolved_drift_count > 0 ? (
              <p className="hint">
                Resolve drift with <code>make doctor</code>.
              </p>
            ) : null}
          </Section>
        </>
      ) : null}
    </div>
  );
}
