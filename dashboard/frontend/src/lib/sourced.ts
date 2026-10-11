/** Helpers for the API's `Sourced<T>` envelope: value + as_of + fresh +
 * stale_reason + provenance. Rendering keeps stale indicators truthful. */

export interface SourcedEnvelope {
  value?: unknown;
  as_of?: unknown;
  fresh?: unknown;
  stale_reason?: unknown;
  provenance?: unknown;
}

export function isSourced(node: unknown): node is SourcedEnvelope {
  return node !== null && typeof node === "object" && "value" in (node as object);
}

export function sourcedValue(node: unknown): unknown {
  return isSourced(node) ? (node as SourcedEnvelope).value : node;
}

/** Render a Sourced value or plain scalar as display text; no float arithmetic. */
export function sourcedText(node: unknown): string {
  const inner = sourcedValue(node);
  if (inner === null || inner === undefined) return "\u2014";
  if (Array.isArray(inner)) {
    if (inner.length === 0) return "\u2014";
    return inner
      .map((item) =>
        item !== null && typeof item === "object" ? JSON.stringify(item) : String(item),
      )
      .join(", ");
  }
  if (typeof inner === "object") return JSON.stringify(inner);
  return String(inner);
}

export function sourcedAsOf(node: unknown): string | null {
  if (isSourced(node)) {
    const asOf = (node as SourcedEnvelope).as_of;
    return typeof asOf === "string" ? asOf : null;
  }
  return null;
}

export function sourcedProvenance(node: unknown): string | null {
  if (isSourced(node)) {
    const provenance = (node as SourcedEnvelope).provenance;
    return typeof provenance === "string" && provenance.length > 0 ? provenance : null;
  }
  return null;
}

/** A node is stale only when the API says fresh === false. */
export function isStale(node: unknown): boolean {
  return isSourced(node) && (node as SourcedEnvelope).fresh === false;
}

export function staleReason(node: unknown): string | null {
  if (isSourced(node)) {
    const reason = (node as SourcedEnvelope).stale_reason;
    return typeof reason === "string" && reason.length > 0 ? reason : null;
  }
  return null;
}

/** Text for a broker/data LinkStatus object. */
export function linkText(link: unknown): string {
  if (link === null || link === undefined || typeof link !== "object") return "\u2014";
  const record = link as Record<string, unknown>;
  const status = typeof record.status === "string" ? record.status : null;
  const connected = record.connected;
  const label =
    status ?? (connected === true ? "CONNECTED" : connected === false ? "DISCONNECTED" : "UNKNOWN");
  const fresh = record.fresh === false ? "stale" : "fresh";
  const reason = typeof record.stale_reason === "string" && record.stale_reason.length > 0
    ? record.stale_reason
    : null;
  return reason ? `${label} (${fresh}, ${reason})` : `${label} (${fresh})`;
}

export type EngineState = "RUNNING" | "PAUSED" | "DEGRADED" | "RECONCILING" | "OFFLINE" | string;

export function engineClass(engine: string): string {
  const normalized = engine.toUpperCase();
  if (normalized === "OFFLINE") return "engine-offline";
  if (normalized === "DEGRADED") return "engine-degraded";
  if (normalized === "PAUSED") return "engine-paused";
  if (normalized === "RECONCILING") return "engine-reconciling";
  if (normalized === "RUNNING") return "engine-running";
  return "engine-unknown";
}

export function modeClass(mode: string): string {
  const normalized = mode.toUpperCase();
  if (normalized === "LIVE") return "mode-live";
  if (normalized === "SHADOW") return "mode-shadow";
  if (normalized === "PAPER") return "mode-paper";
  return "mode-other";
}
