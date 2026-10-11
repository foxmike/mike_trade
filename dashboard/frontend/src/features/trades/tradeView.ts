/** Display-side derivations for the trade inspection page. Pure helpers kept
 * separate from React so the null/NaN handling can be unit tested. */

import type { TradeEvent, TradeInspection, TradeRecord } from "../../api/client";
import { epochSeconds, num } from "../charts/chartModel";

export function toneOf(value: string | null | undefined): "positive" | "negative" | undefined {
  const n = num(value);
  if (n === null || n === 0) return undefined;
  return n > 0 ? "positive" : "negative";
}

export function exitLabel(trade: TradeRecord): string {
  if (trade.exit_reason === "STRATEGY_EXIT") {
    if (!trade.exit_strategy_id) return "UNKNOWN strategy exit";
    return `${trade.exit_strategy_id}${trade.exit_strategy_version ? ` v${trade.exit_strategy_version}` : ""}`;
  }
  return trade.exit_reason ? trade.exit_reason : trade.exit_timestamp === null ? "Still open" : "UNKNOWN";
}

export function entryStrategyLabel(trade: TradeRecord): string | null {
  if (!trade.entry_strategy_id) return null;
  return `${trade.entry_strategy_id}${trade.entry_strategy_version ? ` v${trade.entry_strategy_version}` : ""}`;
}

/** Holding seconds from the record, else derived from entry/exit times. */
export function holdingSeconds(inspection: TradeInspection): number | null {
  const recorded = num(inspection.diagnostics.holding_duration_seconds) ?? num(inspection.trade.holding_duration_seconds);
  if (recorded !== null) return recorded;
  const start = epochSeconds(inspection.trade.entry_timestamp);
  const end = epochSeconds(inspection.trade.exit_timestamp);
  return start !== null && end !== null && end >= start ? end - start : null;
}

const SIGNAL_TYPES = new Set(["SIGNAL_GENERATED", "SIGNAL_ACCEPTED", "SIGNAL_REJECTED"]);

export const isSignalEvent = (event: Pick<TradeEvent, "event_type">): boolean => SIGNAL_TYPES.has(event.event_type);

export function describeEvent(event: TradeEvent): string {
  return [
    event.strategy_id ? `${event.strategy_id}${event.strategy_version ? ` v${event.strategy_version}` : ""}` : null,
    event.source,
    event.price !== null && num(event.price) !== null ? `@ ${event.price}` : null,
    event.quantity !== null && num(event.quantity) !== null ? `qty ${event.quantity}` : null,
    event.reason,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** Query-string suffix shared by trade deep links (source / run). */
export function contextQuery(params: URLSearchParams): string {
  const next = new URLSearchParams();
  const source = params.get("source");
  const run = params.get("run");
  if (source) next.set("source", source);
  if (run) next.set("run", run);
  const text = next.toString();
  return text ? `?${text}` : "";
}
