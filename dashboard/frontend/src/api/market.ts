/** Typed client for Market Status Monitor v2 API (/api/v2/market-status/).
 * Read-only, same-origin GET endpoints.
 */

import { getJson, type SignalOption } from "./client";

export type MarketState = "NORMAL" | "WARNING" | "CRASH" | "STABILIZING" | "RECOVERY";
export type PermissionLevel = "ALLOWED" | "RESTRICTED" | "BLOCKED";

export interface TradingAuthorization {
  state: MarketState;
  evaluated_at: string;
  buy_allowed: boolean;
  short_allowed: boolean;
  sell_allowed: boolean;
  cover_allowed: boolean;
  protective_stops_allowed: boolean;
  emergency_liquidation_allowed: boolean;
  buy_permission: PermissionLevel;
  short_permission: PermissionLevel;
  reasons: string[];
}

export interface PolicyRules {
  warning_restricts_buy: boolean;
  stabilizing_blocks_buy: boolean;
  stabilizing_restricts_short: boolean;
  recovery_allows_buy: boolean;
  recovery_restricts_short: boolean;
  crash_allows_short: boolean;
}

export interface CompositeMetrics {
  composite_drawdown_percent: number | null;
  worst_drawdown_percent: number | null;
  realized_volatility_bps: number | null;
  momentum_bps: number | null;
  breadth_weakness_ratio: number | null;
  trough_drawdown_percent: number | null;
  bars_since_trough: number;
  peak_volatility_bps: number | null;
  rebound_from_trough_percent: number | null;
  vix_level: number | null;
  vix_timestamp: string | null;
}

export interface BenchmarkSymbol {
  symbol: string;
  latest_price: number | null;
  peak_price: number | null;
  drawdown_percent: number | null;
  realized_volatility_bps: number | null;
  momentum_bps: number | null;
  last_bar_timestamp: string | null;
  bar_count: number;
  is_stale: boolean;
  warning_breached: boolean;
  crash_breached: boolean;
  flash_crash_breached: boolean;
}

export interface ThresholdsConfig {
  reference_symbols: string[];
  min_required_symbols: number;
  warning_drawdown_percent: number;
  crash_drawdown_percent: number;
  flash_crash_drawdown_percent: number;
  warning_cleared_percent: number;
  stabilizing_trough_rebound_percent: number;
  recovery_drawdown_percent: number;
  normal_drawdown_percent: number;
  warning_volatility_bps: number;
  crash_volatility_bps: number;
  stabilizing_volatility_decay_ratio: number;
  warning_breadth_ratio: number;
  crash_breadth_ratio: number;
  recovery_breadth_ratio: number;
  warning_confirmation_bars: number;
  warning_cleared_confirmation_bars: number;
  crash_confirmation_bars: number;
  stabilizing_min_bars_in_crash: number;
  stabilizing_no_new_lows_bars: number;
  stabilizing_confirmation_bars: number;
  recovery_min_bars_in_stabilizing: number;
  recovery_confirmation_bars: number;
  normal_confirmation_bars: number;
  relapse_to_crash_bars: number;
  peak_lookback_bars: number;
  volatility_window_bars: number;
  momentum_window_bars: number;
  max_observation_age_seconds: number;
}

export interface MarketStatusOverview {
  account_id: string;
  market_state: MarketState;
  previous_state: MarketState | null;
  state_since: string;
  duration_seconds: number;
  bars_in_state: number;
  last_evaluation_time: string | null;
  last_step_end: string | null;
  engine_online: boolean;
  engine_heartbeat_at: string | null;
  is_stale: boolean;
  data_available: boolean;
  fail_closed: boolean;
  fail_closed_reason: string | null;
  reason: string;
  recovery_requirements: string;
  authorization: TradingAuthorization;
  policy_rules: PolicyRules;
  composite_metrics: CompositeMetrics;
  benchmarks: BenchmarkSymbol[];
}

export interface TransitionRecord {
  from_state: string;
  to_state: string;
  timestamp: string;
  reason: string;
  metrics_summary: Record<string, unknown>;
}

export interface TransitionsList {
  account_id: string;
  transitions: TransitionRecord[];
  total: number;
}

export interface MarketBarItem {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  vwap: number | null;
}

export interface SymbolBars {
  symbol: string;
  timeframe: string;
  bars: MarketBarItem[];
}

export interface MarketBarsResponse {
  account_id: string;
  as_of: string;
  symbols: SymbolBars[];
}

function buildQuery(params: Record<string, string | number | undefined | null>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") {
      parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
    }
  }
  return parts.length > 0 ? `?${parts.join("&")}` : "";
}

export async function getMarketOverview(
  accountId?: string | null,
  signal?: SignalOption,
): Promise<MarketStatusOverview> {
  const qs = buildQuery({ account_id: accountId });
  return getJson<MarketStatusOverview>(`/api/v2/market-status/state${qs}`, signal);
}

export async function getMarketBenchmarks(
  accountId?: string | null,
  signal?: SignalOption,
): Promise<BenchmarkSymbol[]> {
  const qs = buildQuery({ account_id: accountId });
  return getJson<BenchmarkSymbol[]>(`/api/v2/market-status/benchmarks${qs}`, signal);
}

export async function getMarketTransitions(
  accountId?: string | null,
  limit: number = 50,
  signal?: SignalOption,
): Promise<TransitionsList> {
  const qs = buildQuery({ account_id: accountId, limit });
  return getJson<TransitionsList>(`/api/v2/market-status/transitions${qs}`, signal);
}

export async function getMarketBars(
  accountId?: string | null,
  symbol?: string | null,
  limit: number = 120,
  signal?: SignalOption,
): Promise<MarketBarsResponse> {
  const qs = buildQuery({ account_id: accountId, symbol, limit });
  return getJson<MarketBarsResponse>(`/api/v2/market-status/bars${qs}`, signal);
}

export async function getMarketConfig(
  signal?: SignalOption,
): Promise<ThresholdsConfig> {
  return getJson<ThresholdsConfig>("/api/v2/market-status/config", signal);
}
