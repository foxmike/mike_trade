/** Display helpers for the Research Lab. API money/ratio values are Decimal
 * strings or JSON numbers; formatting happens only at the display boundary. */

import type { ResearchRunRow } from "../../api/research";
import { MISSING } from "../../lib/format";

export function toNum(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function signClass(value: string | number | null | undefined): string {
  const parsed = toNum(value);
  if (parsed === null || parsed === 0) return "";
  return parsed > 0 ? "text-positive" : "text-negative";
}

const nf = (digits: number) =>
  new Intl.NumberFormat("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const nf2 = nf(2);
const nf1 = nf(1);

export function fixed(value: string | number | null | undefined, digits = 2): string {
  const parsed = toNum(value);
  return parsed === null ? MISSING : nf(digits).format(parsed);
}

export function signedPercent(value: string | number | null | undefined, digits = 2): string {
  const parsed = toNum(value);
  if (parsed === null) return MISSING;
  const text = nf(digits).format(parsed);
  return `${parsed > 0 ? "+" : ""}${text}%`;
}

export function plainPercent(value: string | number | null | undefined, digits = 1): string {
  const parsed = toNum(value);
  return parsed === null ? MISSING : `${nf(digits).format(parsed)}%`;
}

export function signedMoney(value: string | number | null | undefined): string {
  const parsed = toNum(value);
  if (parsed === null) return MISSING;
  const text = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(parsed);
  return parsed > 0 ? `+${text}` : text;
}

export function profitFactor(value: string | number | null | undefined): string {
  const parsed = toNum(value);
  return parsed === null ? MISSING : nf2.format(parsed);
}

/** Compact axis money: 10,100 -> "$10.1k". */
export function axisMoney(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `$${nf2.format(value / 1_000_000)}M`;
  if (abs >= 10_000) return `$${nf1.format(value / 1_000)}k`;
  return `$${nf(abs >= 100 ? 0 : 2).format(value)}`;
}

export function shortDate(value: string | null | undefined): string {
  if (!value) return MISSING;
  return value.slice(0, 10);
}

export function dateOnly(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

export function holdingText(seconds: string | number | null | undefined): string {
  const total = toNum(seconds);
  if (total === null) return MISSING;
  if (total >= 86_400) return `${nf1.format(total / 86_400)} d`;
  if (total >= 3_600) return `${nf1.format(total / 3_600)} h`;
  if (total >= 60) return `${nf(0).format(total / 60)} min`;
  return `${nf(0).format(total)} s`;
}

export function windowText(run: Pick<ResearchRunRow, "window_start" | "window_end">): string {
  if (!run.window_start && !run.window_end) return "Unavailable";
  return `${shortDate(run.window_start)} → ${shortDate(run.window_end)}`;
}

export function windowDays(run: Pick<ResearchRunRow, "window_start" | "window_end">): number | null {
  if (!run.window_start || !run.window_end) return null;
  const ms = Date.parse(run.window_end) - Date.parse(run.window_start);
  return Number.isFinite(ms) ? Math.round(ms / 86_400_000) : null;
}

export interface StrategyRoleList {
  role: "BUY" | "SELL" | "SHORT" | "COVER";
  names: string[];
}

export function strategyRoles(run: ResearchRunRow): StrategyRoleList[] {
  return [
    { role: "BUY" as const, names: run.buy_strategies },
    { role: "SELL" as const, names: run.sell_strategies },
    { role: "SHORT" as const, names: run.short_strategies },
    { role: "COVER" as const, names: run.cover_strategies },
  ];
}

/** Human description of the stop recorded for a run; never invents a default. */
export function stopText(run: Pick<ResearchRunRow, "stop_policy">): string | null {
  return run.stop_policy ?? null;
}

export function runLabel(run: ResearchRunRow): string {
  const strategies = [...run.buy_strategies, ...run.short_strategies];
  const head = strategies.length > 0 ? strategies.join(" + ") : "unattributed";
  return `${run.symbols.join(", ") || "no symbol"} · ${head}`;
}

export function copyText(value: string): void {
  try {
    void navigator.clipboard.writeText(value);
  } catch {
    // Clipboard may be unavailable (insecure context); the id stays selectable.
  }
}

export const COMPARE_COLORS = ["#4da3ff", "#f0a93b", "#b48cf2", "#4cc9a6", "#ef6f9a"] as const;
