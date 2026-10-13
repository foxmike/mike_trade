/** System health page: feed, broker, scheduler, persistence, reconciliation,
 * and the actionable error list with remediation commands. */

import { useHealth, useModelStatus } from "../../state/queries";
import { useSelectedAccount } from "../../state/AccountContext";
import { ErrorBanner, Loading, PageHeader, Section, StaleFlag, TechnicalDetails } from "../../components/ui";
import { sourcedText } from "../../lib/sourced";
import { bytes, dateTimeEt } from "../../lib/format";

export default function HealthPage() {
  const { selectedAccountId } = useSelectedAccount();
  const query = useHealth(selectedAccountId);
  const health = query.data ?? null;
  const models = useModelStatus(selectedAccountId).data ?? null;

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

          {models !== null && models.published ? (
            <Section
              title={`Models (${models.models.length})`}
              tone={models.inference_errors > 0 || models.strategies.some((s) => s.state !== "LOADED") ? "warning" : undefined}
            >
              <p className="hint">
                Read-only. {models.inference_errors} inference problem(s) reported; component {models.component_status ?? "unknown"}.
              </p>
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Model</th>
                      <th>Algorithm</th>
                      <th>Role</th>
                      <th>Feed</th>
                      <th>State</th>
                      <th>Predictions</th>
                      <th>Failures</th>
                      <th>Last prediction</th>
                      <th>Last failure</th>
                    </tr>
                  </thead>
                  <tbody>
                    {models.models.map((m) => (
                      <tr key={m.model}>
                        <td className="cell-mono" title={m.artifact_sha256}>{m.model}</td>
                        <td>{m.algorithm}</td>
                        <td>{m.role}</td>
                        <td>{m.feed.toUpperCase()}</td>
                        <td>{m.state}</td>
                        <td>{m.predictions}</td>
                        <td>{m.failures}</td>
                        <td>{m.last_prediction ? `${m.last_prediction.symbol} · ${dateTimeEt(m.last_prediction.predicted_at)}` : "—"}</td>
                        <td>{m.last_failure ? `${m.last_failure.code} (${m.last_failure.symbol})` : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Strategy</th>
                      <th>Symbol</th>
                      <th>Model</th>
                      <th>State</th>
                      <th>Last decision</th>
                      <th>Reason</th>
                      <th>Decided</th>
                    </tr>
                  </thead>
                  <tbody>
                    {models.strategies.map((s) => {
                      const d = models.decisions.find((x) => x.strategy === s.strategy && x.symbol === s.symbol);
                      return (
                        <tr key={`${s.strategy}-${s.symbol}`}>
                          <td className="cell-mono">{s.strategy}</td>
                          <td>{s.symbol}</td>
                          <td className="cell-mono">{s.model}</td>
                          <td>{s.state}</td>
                          <td>{d ? d.decision : "—"}</td>
                          <td className="cell-mono">{d ? d.reason : "—"}</td>
                          <td>{d ? dateTimeEt(d.decided_at) : "—"}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </Section>
          ) : null}

          <TechnicalDetails summary="Raw health payload">
            <pre>{JSON.stringify(health, null, 2)}</pre>
          </TechnicalDetails>
        </>
      ) : null}
    </div>
  );
}
