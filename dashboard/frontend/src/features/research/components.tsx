/** Small Research Lab building blocks shared by several tabs. */

import { useEffect, useState } from "react";
import { Link } from "react-router";
import type { FacetOption, ResearchRunRow, RunFacets, RunFilters } from "../../api/research";
import { MISSING, shortId } from "../../lib/format";
import { copyText, strategyRoles } from "./researchFormat";
import { activeFilterCount } from "./urlState";

export function runHref(selectionId: string, fromSearch?: string): string {
  const base = `/research/runs/${encodeURIComponent(selectionId)}`;
  return fromSearch ? `${base}?from=${encodeURIComponent(fromSearch)}` : base;
}

export function CopyId({ value, keep = 12 }: { value: string | null; keep?: number }) {
  const [copied, setCopied] = useState(false);
  if (value === null || value === "") return <span className="unavailable">Unavailable</span>;
  return (
    <span className="rl-copy">
      <code title={value}>{shortId(value, keep)}</code>
      <button
        type="button"
        className="rl-mini-button"
        aria-label={`Copy ${value}`}
        onClick={(event) => {
          event.stopPropagation();
          copyText(value);
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1200);
        }}
      >
        {copied ? "copied" : "copy"}
      </button>
    </span>
  );
}

export function StrategyChips({ run, compact = false }: { run: ResearchRunRow; compact?: boolean }) {
  const roles = strategyRoles(run).filter((role) => role.names.length > 0);
  if (roles.length === 0) return <span className="unavailable">Strategies not recorded</span>;
  return (
    <span className={`rl-strategies${compact ? " rl-strategies-compact" : ""}`}>
      {roles.map((role) => (
        <span key={role.role} className="rl-role-row">
          <span className={`rl-role rl-role-${role.role.toLowerCase()}`}>{role.role}</span>
          {role.names.map((name) => (
            <span key={name} className="rl-strategy">
              {name}
            </span>
          ))}
        </span>
      ))}
    </span>
  );
}

export function StopCell({ run }: { run: Pick<ResearchRunRow, "stop_policy"> }) {
  if (run.stop_policy === null) {
    return (
      <span className="unavailable" title="This run's artifacts do not record stop settings.">
        Not recorded
      </span>
    );
  }
  return <span className="rl-stop">{run.stop_policy}</span>;
}

export function Banner({
  tone,
  title,
  children,
}: {
  tone: "info" | "warning";
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className={`banner ${tone === "warning" ? "banner-warning" : "rl-banner-info"}`} role="status">
      <strong>{title}</strong>
      <span>{children}</span>
    </div>
  );
}

export function DebouncedInput({
  value,
  onCommit,
  delay = 350,
  ...rest
}: {
  value: string;
  onCommit: (value: string) => void;
  delay?: number;
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, "value" | "onChange">) {
  const [local, setLocal] = useState(value);
  useEffect(() => setLocal(value), [value]);
  useEffect(() => {
    if (local === value) return;
    const timer = window.setTimeout(() => onCommit(local), delay);
    return () => window.clearTimeout(timer);
  }, [local, value, delay, onCommit]);
  return <input {...rest} value={local} onChange={(event) => setLocal(event.target.value)} />;
}

function optionLabel(option: FacetOption): string {
  return `${option.value} (${String(option.count)})`;
}

function FacetSelect({
  label,
  value,
  options,
  onChange,
  emptyLabel,
}: {
  label: string;
  value: string | undefined;
  options: FacetOption[] | undefined;
  onChange: (value: string | undefined) => void;
  emptyLabel?: string;
}) {
  const list = options ?? [];
  const disabled = list.length === 0 && !value;
  return (
    <label>
      {label}
      <select
        value={value ?? ""}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value || undefined)}
      >
        <option value="">{disabled ? (emptyLabel ?? "None recorded") : "All"}</option>
        {value && !list.some((option) => option.value === value) ? <option value={value}>{value}</option> : null}
        {list.map((option) => (
          <option key={option.value} value={option.value}>
            {optionLabel(option)}
          </option>
        ))}
      </select>
    </label>
  );
}

function NumberInput({
  label,
  value,
  onChange,
  step,
  placeholder,
}: {
  label: string;
  value: number | undefined;
  onChange: (value: number | undefined) => void;
  step?: string;
  placeholder?: string;
}) {
  return (
    <label>
      {label}
      <DebouncedInput
        type="number"
        inputMode="decimal"
        step={step ?? "any"}
        value={value === undefined ? "" : String(value)}
        placeholder={placeholder ?? "Any"}
        onCommit={(raw) => {
          const parsed = raw === "" ? undefined : Number(raw);
          onChange(parsed !== undefined && Number.isFinite(parsed) ? parsed : undefined);
        }}
      />
    </label>
  );
}

/** Server-side filter controls. Options come from the facets endpoint, which
 * summarizes the whole catalog rather than the currently displayed page. */
export function FilterPanel({
  filters,
  facets,
  onPatch,
  onReset,
  facetsError,
}: {
  filters: RunFilters;
  facets: RunFacets | undefined;
  onPatch: (patch: Partial<RunFilters>) => void;
  onReset: () => void;
  facetsError?: string | null;
}) {
  const active = activeFilterCount(filters);
  const unrecordedStops = facets?.runs_without_stop_policy ?? 0;
  return (
    <div className="rl-filters">
      <div className="filter-bar">
        <label>
          Search
          <DebouncedInput
            value={filters.search ?? ""}
            onCommit={(value) => onPatch({ search: value || undefined })}
            placeholder="Symbol, strategy, run identity"
            size={26}
          />
        </label>
        <label>
          Symbol
          <DebouncedInput
            value={filters.symbol ?? ""}
            list="rl-symbols"
            onCommit={(value) => onPatch({ symbol: value.trim().toUpperCase() || undefined })}
            placeholder="Any"
            size={9}
          />
          <datalist id="rl-symbols">
            {(facets?.symbols ?? []).map((option) => (
              <option key={option.value} value={option.value}>
                {optionLabel(option)}
              </option>
            ))}
          </datalist>
        </label>
        <FacetSelect label="BUY strategy" value={filters.buyStrategy} options={facets?.buy_strategies} onChange={(buyStrategy) => onPatch({ buyStrategy })} />
        <FacetSelect label="SELL strategy" value={filters.sellStrategy} options={facets?.sell_strategies} onChange={(sellStrategy) => onPatch({ sellStrategy })} />
        <FacetSelect label="SHORT strategy" value={filters.shortStrategy} options={facets?.short_strategies} onChange={(shortStrategy) => onPatch({ shortStrategy })} emptyLabel="None in catalog" />
        <FacetSelect label="COVER strategy" value={filters.coverStrategy} options={facets?.cover_strategies} onChange={(coverStrategy) => onPatch({ coverStrategy })} emptyLabel="None in catalog" />
        <FacetSelect label="Stop policy" value={filters.stopPolicy} options={facets?.stop_policies} onChange={(stopPolicy) => onPatch({ stopPolicy })} emptyLabel="None recorded" />
        <FacetSelect label="Run purpose" value={filters.purpose} options={facets?.run_purposes} onChange={(purpose) => onPatch({ purpose })} emptyLabel="None recorded" />
        <FacetSelect label="Status" value={filters.status} options={facets?.statuses} onChange={(status) => onPatch({ status })} />
        <FacetSelect label="Period role" value={filters.periodRole} options={facets?.period_roles} onChange={(periodRole) => onPatch({ periodRole })} />
        <FacetSelect label="Exact historical window" value={filters.windowKey} options={facets?.windows} onChange={(windowKey) => onPatch({ windowKey })} />
      </div>
      <div className="filter-bar rl-filters-row2">
        <label>
          Window starts on/after
          <input
            type="date"
            value={filters.windowStart ?? ""}
            min={facets?.earliest_window_start?.slice(0, 10)}
            onChange={(event) => onPatch({ windowStart: event.target.value || undefined })}
          />
        </label>
        <label>
          Window ends on/before
          <input
            type="date"
            value={filters.windowEnd ?? ""}
            max={facets?.latest_window_end?.slice(0, 10)}
            onChange={(event) => onPatch({ windowEnd: event.target.value || undefined })}
          />
        </label>
        <NumberInput label="Min trades" value={filters.minTrades} step="1" onChange={(minTrades) => onPatch({ minTrades })} />
        <NumberInput label="Min win rate %" value={filters.minWinRate} onChange={(minWinRate) => onPatch({ minWinRate })} />
        <NumberInput label="Min return %" value={filters.minReturn} onChange={(minReturn) => onPatch({ minReturn })} />
        <NumberInput label="Min profit factor" value={filters.minProfitFactor} onChange={(minProfitFactor) => onPatch({ minProfitFactor })} />
        <NumberInput label="Max drawdown %" value={filters.maxDrawdown} onChange={(maxDrawdown) => onPatch({ maxDrawdown })} />
        <button type="button" className="button" onClick={onReset} disabled={active === 0}>
          Clear {active > 0 ? `${String(active)} filter${active === 1 ? "" : "s"}` : "filters"}
        </button>
      </div>
      <p className="hint rl-filter-hint">
        {facetsError
          ? `Filter options unavailable: ${facetsError}`
          : facets
            ? `Options are drawn from all ${facets.total_runs.toLocaleString("en-US")} indexed runs.${
                unrecordedStops > 0
                  ? ` ${unrecordedStops.toLocaleString("en-US")} run(s) pre-date stop recording and have no stop policy to filter on.`
                  : ""
              }`
            : "Loading filter options…"}
      </p>
    </div>
  );
}

export function RunLink({ run, from }: { run: ResearchRunRow; from?: string }) {
  return (
    <Link className="cell-link" to={runHref(run.selection_id, from)}>
      {run.symbols.join(", ") || MISSING}
    </Link>
  );
}
