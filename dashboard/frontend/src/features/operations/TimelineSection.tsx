/** Operational activity timeline (Feature 5): the recorded sequence —
 * strategy evaluation → signal → decision → order → fill → protective stop →
 * exit — from /api/accounts/{id}/activity, with filters and expandable raw
 * payloads. Only stages with recorded evidence appear. */

import { useEffect, useState } from "react";
import {
  getActivity,
  type ActivityCategory,
  type ActivityEntry,
  type ActivityResponse,
  type CockpitResponse,
} from "../../api/client";
import { EmptyState, Loading } from "../../components/ui";
import { dateTimeEt, decimal, quantity, timeEt } from "../../lib/format";

const CATEGORIES: Array<{ value: string; label: string }> = [
  { value: "", label: "All events" },
  { value: "STRATEGY", label: "Strategy state" },
  { value: "EVALUATION", label: "Evaluations" },
  { value: "SIGNAL", label: "Signals & decisions" },
  { value: "ORDER", label: "Orders" },
  { value: "FILL", label: "Fills" },
  { value: "PROTECTION", label: "Protective stops" },
  { value: "SYSTEM", label: "System" },
];

const CATEGORY_CLASS: Record<string, string> = {
  STRATEGY: "pill-dim",
  EVALUATION: "pill-neutral",
  SIGNAL: "pill-blue",
  ORDER: "pill-accent",
  FILL: "pill-green",
  PROTECTION: "pill-yellow",
  SYSTEM: "pill-muted",
  LIFECYCLE: "pill-neutral",
};

interface TimelineFilters {
  symbol: string;
  category: string;
  strategy: string;
  since: string;
}

export default function TimelineSection({
  accountId,
  cockpit,
}: {
  accountId: string;
  cockpit: CockpitResponse | null;
}) {
  const [filters, setFilters] = useState<TimelineFilters>({ symbol: "", category: "", strategy: "", since: "" });
  const query = {
    symbol: filters.symbol || undefined,
    category: filters.category || undefined,
    strategy: filters.strategy || undefined,
    since: filters.since ? new Date(`${filters.since}T00:00:00Z`).toISOString() : undefined,
    limit: 100,
  };
  const [page, setPage] = useState<ActivityResponse | null>(null);
  const [older, setOlder] = useState<ActivityEntry[]>([]);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const symbols = cockpit?.symbols.map((pipeline) => pipeline.symbol) ?? [];

  useEffect(() => {
    const controller = new AbortController();
    setPage(null);
    setOlder([]);
    getActivity(accountId, query, controller.signal)
      .then(setPage)
      .catch(() => undefined);
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId, filters.symbol, filters.category, filters.strategy, filters.since]);

  const loadOlder = async () => {
    if (page?.next_before === null || page === null) return;
    setLoadingOlder(true);
    try {
      const next = await getActivity(accountId, { ...query, before: page.next_before ?? undefined });
      setOlder((current) => [...current, ...next.entries.filter(
        (entry) => ![...page.entries, ...older].some((existing) => existing.at === entry.at && existing.ref_id === entry.ref_id),
      )]);
      setPage({ ...page, next_before: next.next_before });
    } catch {
      // keep current entries; the button stays available
    } finally {
      setLoadingOlder(false);
    }
  };

  const toggle = (key: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const entries: ActivityEntry[] = page === null ? [] : [...page.entries, ...older];

  return (
    <div>
      <div className="filter-bar timeline-filters">
        <label>
          Symbol
          <select value={filters.symbol} onChange={(event) => setFilters({ ...filters, symbol: event.target.value })}>
            <option value="">All</option>
            {symbols.map((symbol) => (
              <option key={symbol}>{symbol}</option>
            ))}
          </select>
        </label>
        <label>
          Event type
          <select value={filters.category} onChange={(event) => setFilters({ ...filters, category: event.target.value })}>
            {CATEGORIES.map((category) => (
              <option key={category.value} value={category.value}>
                {category.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Strategy
          <input
            value={filters.strategy}
            onChange={(event) => setFilters({ ...filters, strategy: event.target.value })}
            placeholder="e.g. vwap_bounce"
            size={12}
          />
        </label>
        <label>
          Since (ET date)
          <input
            type="date"
            value={filters.since}
            onChange={(event) => setFilters({ ...filters, since: event.target.value })}
          />
        </label>
        <span className="hint">
          Recorded operational history only — the engine's event journal and ledger tables.
        </span>
      </div>

      {page === null ? <Loading label="Loading activity…" /> : null}
      {page !== null && entries.length === 0 ? (
        <EmptyState
          message="No recorded activity matches these filters."
          hint="Only stages with recorded evidence are shown; nothing is reconstructed."
        />
      ) : null}

      {entries.length > 0 ? (
        <ol className="activity-timeline">
          {entries.map((entry, index) => {
            const key = `${entry.at}-${entry.event_type}-${entry.ref_id ?? index}`;
            const isOpen = expanded.has(key);
            return (
              <li key={key} className={`activity-${entry.category.toLowerCase()}`}>
                <div className="activity-row" tabIndex={0}
                  onClick={() => toggle(key)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      toggle(key);
                    }
                  }}
                >
                  <time>{dateTimeEt(entry.at)}</time>
                  <span className={`pill ${CATEGORY_CLASS[entry.category] ?? "pill-neutral"}`}>
                    {entry.category}
                  </span>
                  <span className="activity-symbol">{entry.symbol ?? ""}</span>
                  <span className="activity-summary">{entry.summary}</span>
                  <button
                    type="button"
                    className="link-button expander"
                    aria-expanded={isOpen}
                    onClick={(event) => {
                      event.stopPropagation();
                      toggle(key);
                    }}
                  >
                    {isOpen ? "▾ raw" : "▸ raw"}
                  </button>
                </div>
                {isOpen ? (
                  <div className="activity-detail">
                    <dl className="kv-grid kv-compact">
                      <div>
                        <dt>Event type</dt>
                        <dd>{entry.event_type}</dd>
                      </div>
                      {entry.strategy_key ? (
                        <div>
                          <dt>Strategy</dt>
                          <dd>
                            {entry.strategy_key}
                            {entry.strategy_role ? ` (${entry.strategy_role})` : ""}
                          </dd>
                        </div>
                      ) : null}
                      {entry.cycle_id ? (
                        <div>
                          <dt>Cycle</dt>
                          <dd className="cell-mono">{entry.cycle_id}</dd>
                        </div>
                      ) : null}
                      {entry.ref_id ? (
                        <div>
                          <dt>Reference</dt>
                          <dd className="cell-mono">{entry.ref_id}</dd>
                        </div>
                      ) : null}
                      {entry.side ? (
                        <div>
                          <dt>Side</dt>
                          <dd>{entry.side}</dd>
                        </div>
                      ) : null}
                      {entry.quantity !== null ? (
                        <div>
                          <dt>Quantity</dt>
                          <dd>{quantity(entry.quantity)}</dd>
                        </div>
                      ) : null}
                      {entry.price !== null ? (
                        <div>
                          <dt>Price</dt>
                          <dd>{decimal(entry.price)}</dd>
                        </div>
                      ) : null}
                      {entry.stop_price !== null ? (
                        <div>
                          <dt>Stop</dt>
                          <dd>{decimal(entry.stop_price)}</dd>
                        </div>
                      ) : null}
                    </dl>
                    <details className="tech-details">
                      <summary>Raw payload</summary>
                      <pre>{JSON.stringify(entry.payload, null, 2)}</pre>
                    </details>
                  </div>
                ) : null}
              </li>
            );
          })}
        </ol>
      ) : null}

      {page?.next_before != null ? (
        <div className="pagination">
          <button type="button" disabled={loadingOlder} onClick={() => void loadOlder()}>
            {loadingOlder ? "Loading…" : "Load older events"}
          </button>
          <span>before {timeEt(page.next_before)}</span>
        </div>
      ) : null}
    </div>
  );
}
