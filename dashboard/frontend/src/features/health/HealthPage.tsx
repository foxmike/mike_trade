/** System health page: feed, broker, scheduler, persistence, reconciliation,
 * and the actionable error list with remediation commands. */

import { useHealth } from "../../state/queries";
import { useSelectedAccount } from "../../state/AccountContext";
import { ErrorBanner, Loading, PageHeader, Section, StaleFlag, TechnicalDetails } from "../../components/ui";
import { sourcedText } from "../../lib/sourced";
import { bytes, dateTimeEt } from "../../lib/format";

export default function HealthPage() {
  const { selectedAccountId } = useSelectedAccount();
  const query = useHealth(selectedAccountId);
  const health = query.data ?? null;

  const error = query.isError
    ? query.error instanceof Error
      ? query.error.message
      : "Health load failed"
    : null;

  if (selectedAccountId === null) return null;

  const errors = health?.errors ?? [];

  return (
    <div className="page">
      <PageHeader
        title="Health & diagnostics"
        subtitle={`${selectedAccountId} · component liveness, persistence and remediation`}
      />

      {error !== null ? (
        <ErrorBanner title="Health unavailable" message={error} hint="Retry with make doctor." />
      ) : null}
      {query.isPending && !query.isError ? <Loading label="Loading health…" /> : null}

      {health !== null ? (
        <>
          {errors.length > 0 ? (
            <Section title={`Actionable errors (${errors.length})`} tone={errors.some((e) => e.severity === "critical") ? "critical" : "warning"}>
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Code</th>
                      <th>Severity</th>
                      <th>Message</th>
                      <th>Remediation</th>
                    </tr>
                  </thead>
                  <tbody>
                    {errors.map((entry, index) => (
                      <tr key={`${entry.code}-${index}`}>
                        <td className="cell-mono">{entry.code}</td>
                        <td>
                          <span className={`pill ${entry.severity === "critical" ? "pill-red" : entry.severity === "warning" ? "pill-yellow" : "pill-neutral"}`}>
                            {entry.severity.toUpperCase()}
                          </span>
                        </td>
                        <td>{entry.message}</td>
                        <td>
                          <code>{entry.command.length > 0 ? entry.command : "make doctor"}</code>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Section>
          ) : (
            <Section title="Actionable errors">
              <p className="hint">None. No remediation required.</p>
            </Section>
          )}

          <div className="panel-grid">
            <Section title="Feed">
              <dl className="kv-grid kv-compact">
                <div>
                  <dt>Link</dt>
                  <dd>{health.feed.link.status}</dd>
                </div>
                <div>
                  <dt>Age</dt>
                  <dd>{sourcedText(health.feed.age_seconds)}s</dd>
                </div>
                <div>
                  <dt>Reconnects</dt>
                  <dd>{sourcedText(health.feed.reconnects)}</dd>
                </div>
                <div>
                  <dt>Subscriptions</dt>
                  <dd>{sourcedText(health.feed.subscription_count)}</dd>
                </div>
              </dl>
            </Section>

            <Section title="Broker">
              <dl className="kv-grid kv-compact">
                <div>
                  <dt>Link</dt>
                  <dd>{health.broker.link.status}</dd>
                </div>
                <div>
                  <dt>Rate-limit errors</dt>
                  <dd>{sourcedText(health.broker.rate_limit_errors)}</dd>
                </div>
              </dl>
            </Section>

            <Section title="Scheduler & reconciliation">
              <dl className="kv-grid kv-compact">
                <div>
                  <dt>Scheduler lag</dt>
                  <dd>{sourcedText(health.scheduler_lag_seconds)}s</dd>
                </div>
                <div>
                  <dt>Last reconciliation</dt>
                  <dd>{dateTimeEt(health.last_reconciliation_at.value)}</dd>
                </div>
              </dl>
            </Section>

            <Section title="Persistence & backup">
              <dl className="kv-grid kv-compact">
                <div>
                  <dt>Database</dt>
                  <dd className="cell-mono">{health.persistence.database_path}</dd>
                </div>
                <div>
                  <dt>Schema version</dt>
                  <dd>{health.persistence.schema_version ?? "—"}</dd>
                </div>
                <div>
                  <dt>Size</dt>
                  <dd>{bytes(health.persistence.size_bytes)}</dd>
                </div>
                <div>
                  <dt>WAL size</dt>
                  <dd>{bytes(health.persistence.wal_size_bytes)}</dd>
                </div>
                <div>
                  <dt>Last backup</dt>
                  <dd>
                    {dateTimeEt(health.persistence.last_backup_at.value)}
                    {health.persistence.last_backup_at.fresh === false ? (
                      <StaleFlag reason={health.persistence.last_backup_at.stale_reason} />
                    ) : null}
                  </dd>
                </div>
                <div>
                  <dt>Backup integrity</dt>
                  <dd>
                    {health.persistence.last_backup_integrity_ok === null
                      ? "unknown"
                      : health.persistence.last_backup_integrity_ok
                        ? "ok"
                        : "FAILED"}
                  </dd>
                </div>
              </dl>
            </Section>
          </div>

          <TechnicalDetails summary="Raw health payload">
            <pre>{JSON.stringify(health, null, 2)}</pre>
          </TechnicalDetails>
        </>
      ) : null}
    </div>
  );
}
