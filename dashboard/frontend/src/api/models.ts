/** Typed client for the read-only model status endpoint (/api/v2/models/status). */

import { getJson, type SignalOption } from "./client";

export interface ModelStrategyStatus {
  strategy: string;
  symbol: string;
  role: string;
  model: string;
  pinned_sha256: string | null;
  state: string;
}

export interface ModelLoadStatus {
  model: string;
  algorithm: string;
  role: string;
  feed: string;
  artifact_sha256: string;
  state: string;
  predictions: number;
  failures: number;
  last_prediction: { symbol: string; observation_end: string; predicted_at: string } | null;
  last_failure: { code: string; detail: string; symbol: string; at: string } | null;
}

export interface ModelDecisionSummary {
  strategy: string;
  symbol: string;
  decision: string;
  reason: string;
  observation_end: string | null;
  decided_at: string;
}

export interface ModelStatusResponse {
  account_id: string;
  as_of: string;
  published: boolean;
  component_status: string | null;
  heartbeat_at: string | null;
  inference_errors: number;
  strategies: ModelStrategyStatus[];
  models: ModelLoadStatus[];
  decisions: ModelDecisionSummary[];
  diagnostics: Record<string, unknown>;
  note: string;
}

export function getModelStatus(accountId: string, signal?: SignalOption): Promise<ModelStatusResponse> {
  return getJson<ModelStatusResponse>(`/api/v2/models/status?account_id=${encodeURIComponent(accountId)}`, signal);
}
