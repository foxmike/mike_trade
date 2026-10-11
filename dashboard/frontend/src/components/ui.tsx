/** Shared console primitives: status pills, banners, loading/empty/error
 * states, sections, KPI tiles, technical-details disclosures, pagination. */

import type { ReactNode } from "react";
import { engineClass, linkText, modeClass, sourcedText } from "../lib/sourced";

// ---------------------------------------------------------------------------
// Pills
// ---------------------------------------------------------------------------

export function ModeBadge({ mode }: { mode: string | null | undefined }) {
  if (mode === null || mode === undefined || mode === "" || mode === "—") {
    return <span className="pill pill-neutral">—</span>;
  }
  return <span className={`pill mode-badge ${modeClass(mode)}`}>{mode.toUpperCase()}</span>;
}

export function EngineBadge({ engine }: { engine: string }) {
  return (
    <span className={`pill engine-badge ${engineClass(engine)}`}>
      <span className="pill-dot" aria-hidden="true" />
      {engine.toUpperCase()}
    </span>
  );
}

export function MarketBadge({ market }: { market: string | null | undefined }) {
  const value = (market ?? "—").toUpperCase();
  const klass =
    value === "OPEN" ? "pill-green" : value === "CLOSED" ? "pill-neutral" : "pill-red";
  return <span className={`pill ${klass}`}>{value === "—" ? "MARKET —" : value}</span>;
}

export function SessionBadge({ session }: { session: string | null | undefined }) {
  if (session === null || session === undefined || session === "") {
    return <span className="pill pill-neutral">—</span>;
  }
  return <span className="pill pill-neutral">{session}</span>;
}

export function LinkStatusBadge({ link, label }: { link: unknown; label: string }) {
  const text = linkText(link);
  const stale = text.includes("stale");
  const disconnected = text.startsWith("DISCONNECTED") || text.startsWith("UNKNOWN");
  return (
    <span className={`pill ${disconnected ? "pill-red" : stale ? "pill-yellow" : "pill-green"}`}>
      {label} {text}
    </span>
  );
}

export function StaleFlag({ reason }: { reason?: string | null }) {
  return <span className="stale-flag">STALE{reason ? ` (${reason})` : ""}</span>;
}

// ---------------------------------------------------------------------------
// States
// ---------------------------------------------------------------------------

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="state-block" role="status">
      <span className="spinner" aria-hidden="true" />
      {label}
    </div>
  );
}

export function EmptyState({ message, hint }: { message: string; hint?: string }) {
  return (
    <div className="state-block state-empty">
      <p>{message}</p>
      {hint !== undefined ? <p className="hint">{hint}</p> : null}
    </div>
  );
}

export function ErrorBanner({
  message,
  title = "Request failed",
  hint,
}: {
  message: string;
  title?: string;
  hint?: string;
}) {
  return (
    <div className="banner banner-error" role="alert">
      <strong>{title}</strong>
      <span>{message}</span>
      {hint !== undefined ? <span className="hint">{hint}</span> : null}
    </div>
  );
}

export function UnreachableBanner() {
  return (
    <div className="banner banner-unreachable" role="alert">
      <strong>DASHBOARD API UNREACHABLE</strong>
      <span>
        The dashboard backend did not respond. Last-known values are shown where available.
        Diagnose with <code>make doctor</code> or <code>mike-trade doctor</code>.
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Layout
// ---------------------------------------------------------------------------

export function Section({
  title,
  actions,
  children,
  tone,
}: {
  title: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  tone?: "critical" | "warning";
}) {
  return (
    <section className={`panel${tone === "critical" ? " panel-critical" : tone === "warning" ? " panel-warning" : ""}`}>
      <div className="panel-heading">
        <h2>{title}</h2>
        {actions !== undefined ? <div className="panel-actions">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle !== undefined ? <p className="page-subtitle">{subtitle}</p> : null}
      </div>
      {actions !== undefined ? <div className="page-actions">{actions}</div> : null}
    </div>
  );
}

export function KpiTile({
  label,
  value,
  meta,
  stale,
  tone,
}: {
  label: string;
  value: ReactNode;
  meta?: string | null;
  stale?: string | null;
  tone?: "positive" | "negative";
}) {
  return (
    <div className={`kpi-tile${tone === "positive" ? " kpi-positive" : tone === "negative" ? " kpi-negative" : ""}${stale ? " kpi-stale" : ""}`}>
      <span className="kpi-label">{label}</span>
      <span className="kpi-value">{value}</span>
      {meta !== undefined && meta !== null ? <span className="kpi-meta">{meta}</span> : null}
      {stale ? <StaleFlag reason={stale} /> : null}
    </div>
  );
}

/** Expandable provenance/diagnostics area — kept out of the main flow. */
export function TechnicalDetails({
  summary = "Technical details",
  children,
}: {
  summary?: string;
  children: ReactNode;
}) {
  return (
    <details className="tech-details">
      <summary>{summary}</summary>
      <div className="tech-details-body">{children}</div>
    </details>
  );
}

export function Pagination({
  page,
  pages,
  total,
  onPage,
  pageSize,
  onPageSize,
}: {
  page: number;
  pages: number;
  total: number;
  onPage: (page: number) => void;
  pageSize?: number;
  onPageSize?: (size: number) => void;
}) {
  return (
    <div className="pagination">
      <button type="button" disabled={page <= 1} onClick={() => onPage(page - 1)}>
        Previous
      </button>
      <span>
        Page {page} of {Math.max(1, pages)} · {total} rows
      </span>
      <button type="button" disabled={page >= pages} onClick={() => onPage(page + 1)}>
        Next
      </button>
      {pageSize !== undefined && onPageSize !== undefined ? (
        <label className="page-size">
          Rows
          <select value={pageSize} onChange={(event) => onPageSize(Number(event.target.value))}>
            {[10, 25, 50, 100].map((size) => (
              <option key={size} value={size}>
                {size}
              </option>
            ))}
          </select>
        </label>
      ) : null}
    </div>
  );
}

export function SortHeader({
  label,
  field,
  sort,
  direction,
  onSort,
  align,
}: {
  label: ReactNode;
  field: string;
  sort: string | undefined;
  direction: string;
  onSort: (field: string) => void;
  align?: "right";
}) {
  const active = sort === field;
  return (
    <th scope="col" className={active ? "sort-active" : undefined} style={align === "right" ? { textAlign: "right" } : undefined}>
      <button
        type="button"
        className="sort-button"
        onClick={() => onSort(field)}
        aria-label={`Sort by ${typeof label === "string" ? label : field}`}
      >
        {label}
        <span className="sort-arrow" aria-hidden="true">
          {active ? (direction === "asc" ? "▲" : "▼") : "↕"}
        </span>
      </button>
    </th>
  );
}

/** Sourced<T> cell: value text with a stale marker when fresh === false. */
export function SourcedCell({ node }: { node: unknown }) {
  const stale = node !== null && typeof node === "object" && (node as Record<string, unknown>).fresh === false;
  return (
    <>
      {sourcedText(node)}
      {stale ? <StaleFlag reason={null} /> : null}
    </>
  );
}
