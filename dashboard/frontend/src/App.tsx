import { useEffect, useRef, useState } from "react";
import { ApiError, getHealth, getKpis, getStatus, getWatchlist } from "./api/client";
import AccountDetail, { AccountSelector } from "./components/AccountDetail";
import SymbolDetails from "./components/SymbolDetails";
import Watchlist from "./components/Watchlist";

// Bounded polling interval for account-scoped dashboard reads (GET only).
const POLL_INTERVAL_MS = 5000;
// Client-side tick for the heartbeat age between polls.
const TICK_MS = 1000;
// Mirrors DashboardSettings.heartbeat_offline_seconds (the API default).
// The API reports heartbeat age but not the threshold itself, so the header
// turns OFFLINE once the ticking client-side age exceeds this bound even if
// the next poll has not arrived yet.
const OFFLINE_AFTER_SECONDS = 60;

type StatusResponse = Awaited<ReturnType<typeof getStatus>>;
type KpisResponse = Awaited<ReturnType<typeof getKpis>>;
type WatchlistResponse = Awaited<ReturnType<typeof getWatchlist>>;
type HealthResponse = Awaited<ReturnType<typeof getHealth>>;

// Read-only helper: render a Sourced<T> value or a plain scalar as text.
// Decimal strings from the API are displayed as-is; no float arithmetic is
// performed on money values here.
function sourcedText(node: unknown): string {
  const inner =
    node !== null && typeof node === "object" && "value" in (node as Record<string, unknown>)
      ? (node as Record<string, unknown>).value
      : node;
  if (inner === null || inner === undefined) return "—";
  if (Array.isArray(inner)) {
    if (inner.length === 0) return "—";
    return inner
      .map((item) =>
        item !== null && typeof item === "object" ? JSON.stringify(item) : String(item),
      )
      .join(", ");
  }
  if (typeof inner === "object") return JSON.stringify(inner);
  return String(inner);
}

function sourcedAsOf(node: unknown): string {
  if (node !== null && typeof node === "object" && "as_of" in (node as Record<string, unknown>)) {
    const asOf = (node as Record<string, unknown>).as_of;
    return asOf === null || asOf === undefined ? "—" : String(asOf);
  }
  return "—";
}

function sourcedProvenance(node: unknown): string {
  if (
    node !== null &&
    typeof node === "object" &&
    "provenance" in (node as Record<string, unknown>)
  ) {
    const provenance = (node as Record<string, unknown>).provenance;
    return provenance === null || provenance === undefined ? "—" : String(provenance);
  }
  return "—";
}

function isStaleNode(node: unknown): boolean {
  return (
    node !== null &&
    typeof node === "object" &&
    "fresh" in (node as Record<string, unknown>) &&
    (node as Record<string, unknown>).fresh === false
  );
}

function staleReasonOf(node: unknown): string | null {
  if (node !== null && typeof node === "object") {
    const reason = (node as Record<string, unknown>).stale_reason;
    if (typeof reason === "string" && reason.length > 0) return reason;
  }
  return null;
}

function linkText(link: unknown): string {
  if (link === null || link === undefined || typeof link !== "object") return "—";
  const record = link as Record<string, unknown>;
  const status = typeof record.status === "string" ? record.status : null;
  const connected = record.connected;
  const label = status ?? (connected === true ? "CONNECTED" : connected === false ? "DISCONNECTED" : "UNKNOWN");
  const fresh = record.fresh === false ? "stale" : "fresh";
  const reason = typeof record.stale_reason === "string" ? record.stale_reason : null;
  return reason ? `${label} (${fresh}, ${reason})` : `${label} (${fresh})`;
}

function modeClass(mode: unknown): string {
  const normalized = typeof mode === "string" ? mode.toUpperCase() : "";
  if (normalized === "LIVE") return "mode-badge mode-live";
  if (normalized === "SHADOW") return "mode-badge mode-shadow";
  return "mode-badge mode-paper";
}

function engineClass(engine: string): string {
  const normalized = engine.toUpperCase();
  if (normalized === "OFFLINE") return "engine-badge engine-offline";
  if (normalized === "DEGRADED") return "engine-badge engine-degraded";
  if (normalized === "PAUSED") return "engine-badge engine-paused";
  if (normalized === "RECONCILING") return "engine-badge engine-reconciling";
  return "engine-badge engine-running";
}

function asRecord(value: unknown): Record<string, unknown> {
  if (value !== null && typeof value === "object") return value as Record<string, unknown>;
  return {};
}

export default function App() {
  const [selectedAccountId, setSelectedAccountId] = useState<string | null>(null);
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [kpis, setKpis] = useState<KpisResponse | null>(null);
  const [watchlist, setWatchlist] = useState<WatchlistResponse | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [apiUnreachable, setApiUnreachable] = useState(false);
  const [lastError, setLastError] = useState<string | null>(null);
  // Client-side clock so the heartbeat age keeps ticking between polls.
  const [, setTick] = useState(0);
  // Generation counter: stale (aborted / superseded) polls never commit state.
  const generationRef = useRef(0);
  const pollAtClientRef = useRef<number>(0);
  const abortRef = useRef<AbortController | null>(null);

  // Keep the heartbeat age ticking client-side between polls.
  useEffect(() => {
    const timer = window.setInterval(() => setTick((tick) => tick + 1), TICK_MS);
    return () => window.clearInterval(timer);
  }, []);

  // Bounded polling of the four account-scoped GET endpoints. Switching
  // accounts aborts in-flight requests and discards previously fetched
  // account-scoped state before any new data renders.
  useEffect(() => {
    if (selectedAccountId === null) return;
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    const accountId = selectedAccountId;

    const load = async (signal: AbortSignal) => {
      try {
        const withSignal = { signal };
        const [statusBody, kpisBody, watchlistBody, healthBody] = await Promise.all([
          (getStatus as (id: string, opts?: { signal?: AbortSignal }) => Promise<StatusResponse>)(
            accountId,
            withSignal,
          ),
          (getKpis as (id: string, opts?: { signal?: AbortSignal }) => Promise<KpisResponse>)(
            accountId,
            withSignal,
          ),
          (getWatchlist as (
            id: string,
            opts?: { signal?: AbortSignal },
          ) => Promise<WatchlistResponse>)(accountId, withSignal),
          (getHealth as (id: string, opts?: { signal?: AbortSignal }) => Promise<HealthResponse>)(
            accountId,
            withSignal,
          ),
        ]);
        if (signal.aborted || generationRef.current !== generation) return;
        pollAtClientRef.current = Date.now();
        setStatus(statusBody);
        setKpis(kpisBody);
        setWatchlist(watchlistBody);
        setHealth(healthBody);
        setApiUnreachable(false);
        setLastError(null);
      } catch (error) {
        if (signal.aborted || generationRef.current !== generation) return;
        if (error instanceof ApiError) {
          // ApiError without an HTTP status means the dashboard API itself
          // could not be reached (network failure / connection refused).
          if (error.status === null || error.status === undefined) {
            setApiUnreachable(true);
            setLastError(error.message);
          } else {
            setApiUnreachable(false);
            setLastError(`Request failed (HTTP ${String(error.status)}): ${error.message}`);
          }
        } else if (error instanceof DOMException && error.name === "AbortError") {
          return;
        } else if (error instanceof TypeError) {
          setApiUnreachable(true);
          setLastError(error.message);
        } else {
          setLastError(error instanceof Error ? error.message : "Unknown dashboard error");
        }
      }
    };

    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;
    void load(controller.signal);
    const timer = window.setInterval(() => {
      if (!controller.signal.aborted) void load(controller.signal);
    }, POLL_INTERVAL_MS);
    return () => {
      window.clearInterval(timer);
      controller.abort();
    };
  }, [selectedAccountId]);

  const handleSelectAccount = (accountId: string) => {
    // Discard every account-scoped value before rendering the new account so
    // values from different accounts/modes/scenarios are never mixed.
    generationRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    setSelectedAccountId(accountId);
    setSelectedSymbol(null);
    setStatus(null);
    setKpis(null);
    setWatchlist(null);
    setHealth(null);
    setApiUnreachable(false);
    setLastError(null);
    pollAtClientRef.current = 0;
  };

  const handleSelectSymbol = (symbol: string) => {
    setSelectedSymbol(symbol);
  };

  const statusRecord = asRecord(status);
  const serverEngine =
    typeof statusRecord.engine_state === "string" ? statusRecord.engine_state : null;
  const baseAge =
    typeof statusRecord.heartbeat_age_seconds === "number"
      ? statusRecord.heartbeat_age_seconds
      : null;
  const elapsedSeconds =
    pollAtClientRef.current > 0 ? (Date.now() - pollAtClientRef.current) / 1000 : 0;
  const tickingAge = baseAge === null ? null : Math.max(0, baseAge + elapsedSeconds);
  // Client-side OFFLINE promotion: once the ticking age exceeds the API
  // threshold the header shows OFFLINE without waiting for a new engine flag.
  const clientOffline =
    tickingAge !== null && tickingAge > OFFLINE_AFTER_SECONDS ? true : false;
  const effectiveEngine = clientOffline ? "OFFLINE" : (serverEngine ?? "—");
  const showStaleWatermark =
    effectiveEngine === "OFFLINE" ||
    statusRecord.fresh === false ||
    statusRecord.is_stale === true;

  const kpisRecord = asRecord(kpis);
  const healthRecord = asRecord(health);
  const watchlistSymbols: Array<Record<string, unknown>> = Array.isArray(
    (watchlistRecordSafe(watchlist) as Record<string, unknown>).symbols,
  )
    ? ((watchlistRecordSafe(watchlist) as Record<string, unknown>).symbols as Array<
        Record<string, unknown>
      >)
    : [];

  const accountLabel =
    typeof statusRecord.account_id === "string"
      ? statusRecord.account_id
      : (selectedAccountId ?? "—");
  const scenarioLabel =
    typeof statusRecord.scenario === "string" && statusRecord.scenario.length > 0
      ? statusRecord.scenario
      : "—";
  const modeLabel =
    typeof statusRecord.mode === "string" && statusRecord.mode.length > 0
      ? statusRecord.mode.toUpperCase()
      : "—";

  const exchangeClock = asRecord(statusRecord.exchange_clock);
  const nextCutoff = asRecord(statusRecord.next_entry_cutoff);
  const entriesBlocked = Array.isArray(statusRecord.entries_blocked_reasons)
    ? (statusRecord.entries_blocked_reasons as Array<unknown>)
    : [];

  const kpiEntries: Array<{ key: string; label: string }> = [
    { key: "cash", label: "Cash" },
    { key: "buying_power", label: "Buying power" },
    { key: "equity", label: "Equity" },
    { key: "configured_total_budget", label: "Total budget" },
    { key: "reserved_pending_buys", label: "Reserved pending buys" },
    { key: "deployed_capital", label: "Deployed capital" },
    { key: "open_position_count", label: "Open positions" },
    { key: "pending_order_count", label: "Pending orders" },
    { key: "daily_realized_pnl", label: "Daily realized P&L" },
    { key: "daily_unrealized_pnl", label: "Daily unrealized P&L" },
    { key: "lifetime_realized_pnl", label: "Lifetime realized P&L" },
    { key: "lifetime_pnl", label: "Lifetime P&L" },
    { key: "protection_error_count", label: "Protection errors" },
    { key: "data_error_count", label: "Data errors" },
    { key: "reconciliation_error_count", label: "Reconciliation errors" },
  ];

  const feedHealth = asRecord(healthRecord.feed);
  const brokerHealth = asRecord(healthRecord.broker);
  const persistence = asRecord(healthRecord.persistence);
  const healthErrors: Array<Record<string, unknown>> = Array.isArray(healthRecord.errors)
    ? (healthRecord.errors as Array<Record<string, unknown>>)
    : [];

  return (
    <div className="app-shell">
      <header className="global-header">
        <div className="header-row">
          <div>
            <h1>mike_trade operations dashboard</h1>
            <p className="account-line">
              Account <strong>{accountLabel}</strong> · Scenario <strong>{scenarioLabel}</strong>
            </p>
          </div>
          <div className="badge-row">
            <span className={modeClass(statusRecord.mode)}>{modeLabel}</span>
            <span className={engineClass(effectiveEngine)}>{effectiveEngine}</span>
          </div>
        </div>

        {apiUnreachable ? (
          <div className="banner banner-unreachable" role="alert">
            DASHBOARD API UNREACHABLE — the dashboard backend did not respond. Last-known values
            are shown below where available. Diagnose with <code>make doctor</code> or{" "}
            <code>mike-trade doctor</code>.
          </div>
        ) : null}

        {showStaleWatermark && selectedAccountId !== null ? (
          <div className="stale-watermark" role="status">
            STALE — showing last-known values
          </div>
        ) : null}

        {selectedAccountId === null ? (
          <p className="hint">Select an account to begin monitoring.</p>
        ) : status === null ? (
          <p className="hint">
            Loading account status…{lastError !== null ? ` Last error: ${lastError}` : ""}
          </p>
        ) : (
          <dl className="status-grid">
            <div>
              <dt>Engine</dt>
              <dd className={engineClass(effectiveEngine)}>{effectiveEngine}</dd>
            </div>
            <div>
              <dt>Heartbeat age</dt>
              <dd>
                {tickingAge === null ? "—" : `${tickingAge.toFixed(1)}s`}
                {clientOffline ? " (client-side OFFLINE)" : ""}
              </dd>
            </div>
            <div>
              <dt>Last heartbeat</dt>
              <dd>{String(statusRecord.last_heartbeat_at ?? "—")}</dd>
            </div>
            <div>
              <dt>Broker</dt>
              <dd>{linkText(statusRecord.broker)}</dd>
            </div>
            <div>
              <dt>Data</dt>
              <dd>{linkText(statusRecord.data)}</dd>
            </div>
            <div>
              <dt>Market</dt>
              <dd>{String(statusRecord.market ?? "—")}</dd>
            </div>
            <div>
              <dt>Exchange clock (America/New_York)</dt>
              <dd>
                {String(exchangeClock.now_et ?? "—")} ·{" "}
                {String(exchangeClock.session_status ?? "—")}
              </dd>
            </div>
            <div>
              <dt>Next entry cutoff</dt>
              <dd>
                {String(nextCutoff.at_et ?? nextCutoff.at ?? "—")}
                {nextCutoff.passed_today === true ? " (passed today)" : ""}
              </dd>
            </div>
            <div>
              <dt>Entries allowed</dt>
              <dd>
                {statusRecord.entries_allowed === true ? "yes" : "no"}
                {entriesBlocked.length > 0 ? `: ${entriesBlocked.map(String).join(", ")}` : ""}
              </dd>
            </div>
          </dl>
        )}
      </header>

      <section className="account-picker" aria-label="Account selection">
        <AccountSelector selectedAccountId={selectedAccountId} onSelect={handleSelectAccount} />
      </section>

      {selectedAccountId !== null ? (
        <main>
          <section aria-label="Account KPIs" className="kpi-section">
            <h2>Account KPIs</h2>
            {kpis === null ? (
              <p className="hint">Loading KPIs…</p>
            ) : (
              <ul className="kpi-ribbon">
                {kpiEntries.map((entry) => {
                  const node = kpisRecord[entry.key];
                  const stale = isStaleNode(node);
                  const reason = staleReasonOf(node);
                  return (
                    <li key={entry.key} className={stale ? "kpi-card kpi-stale" : "kpi-card"}>
                      <span className="kpi-label">{entry.label}</span>
                      <span className="kpi-value">{sourcedText(node)}</span>
                      <span className="kpi-meta">
                        as-of {sourcedAsOf(node)} · {sourcedProvenance(node)}
                      </span>
                      {stale ? (
                        <span className="kpi-meta kpi-stale-flag">
                          STALE{reason !== null ? ` (${reason})` : ""}
                        </span>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            )}
          </section>

          <section aria-label="Health summary" className="health-section">
            <h2>Health</h2>
            {health === null ? (
              <p className="hint">Loading health…</p>
            ) : (
              <div className="health-grid">
                <div className="health-card">
                  <h3>Feed</h3>
                  <p>Link: {linkText(feedHealth.link)}</p>
                  <p>Age: {sourcedText(feedHealth.age_seconds)}</p>
                  <p>Reconnects: {sourcedText(feedHealth.reconnects)}</p>
                  <p>Subscriptions: {sourcedText(feedHealth.subscription_count)}</p>
                </div>
                <div className="health-card">
                  <h3>Broker</h3>
                  <p>Link: {linkText(brokerHealth.link)}</p>
                  <p>Rate-limit errors: {sourcedText(brokerHealth.rate_limit_errors)}</p>
                </div>
                <div className="health-card">
                  <h3>Scheduler</h3>
                  <p>Lag: {sourcedText(healthRecord.scheduler_lag_seconds)}</p>
                  <p>
                    Investigate with <code>make doctor</code>
                  </p>
                </div>
                <div className="health-card">
                  <h3>Persistence / backup</h3>
                  <p>Schema: {String(persistence.schema_version ?? "—")}</p>
                  <p>Size: {String(persistence.size_bytes ?? "—")} bytes</p>
                  <p>Last backup: {sourcedText(persistence.last_backup_at)}</p>
                  <p>
                    Integrity ok: {String(persistence.last_backup_integrity_ok ?? "—")} · Check with{" "}
                    <code>mike-trade doctor</code>
                  </p>
                </div>
                <div className="health-card">
                  <h3>Reconciliation</h3>
                  <p>Last run: {sourcedText(healthRecord.last_reconciliation_at)}</p>
                  <p>
                    Resolve drift with <code>make doctor</code>
                  </p>
                </div>
                <div className="health-card health-errors">
                  <h3>Actionable errors ({String(healthErrors.length)})</h3>
                  {healthErrors.length === 0 ? (
                    <p>None.</p>
                  ) : (
                    <ul>
                      {healthErrors.map((error, index) => (
                        <li key={`${String(error.code ?? "error")}-${String(index)}`}>
                          <strong>
                            {String(error.code ?? "UNKNOWN")} [{String(error.severity ?? "info")}]
                          </strong>
                          <span> {String(error.message ?? "")}</span>
                          {typeof error.command === "string" && error.command.length > 0 ? (
                            <span>
                              {" "}
                              Run <code>{error.command}</code>
                            </span>
                          ) : (
                            <span>
                              {" "}
                              Run <code>make doctor</code>
                            </span>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            )}
          </section>

          <section aria-label="Stock watchlist" className="watchlist-section">
            <h2>Watchlist</h2>
            {watchlist === null ? (
              <p className="hint">Loading watchlist…</p>
            ) : (
              <Watchlist
                accountId={selectedAccountId}
                symbols={watchlistSymbols}
                onSelectSymbol={handleSelectSymbol}
              />
            )}
          </section>

          <section aria-label="Account detail" className="detail-section">
            <AccountDetail accountId={selectedAccountId} />
          </section>

          {selectedSymbol !== null ? (
            <SymbolDetails
              accountId={selectedAccountId}
              symbol={selectedSymbol}
              onClose={() => setSelectedSymbol(null)}
            />
          ) : null}
        </main>
      ) : null}

      <footer className="app-footer">
        <p>
          Read-only dashboard. No control here submits orders or changes configuration. Operator
          actions: <code>make doctor</code> · <code>mike-trade doctor</code> ·{" "}
          <code>mike-trade version</code>
        </p>
      </footer>
    </div>
  );
}

function watchlistRecordSafe(watchlist: WatchlistResponse | null): WatchlistResponse | Record<string, unknown> {
  return (watchlist ?? {}) as Record<string, unknown>;
}
