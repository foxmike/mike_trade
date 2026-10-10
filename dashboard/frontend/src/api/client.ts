/** Typed read-only client for the mike_trade dashboard API (GET only, same-origin). */

export class ApiError extends Error {
  readonly status: number | null;

  constructor(status: number | null, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** A bare AbortSignal or an options bag carrying one; both call styles are accepted. */
export type SignalOption = AbortSignal | { signal?: AbortSignal };

function resolveSignal(option?: SignalOption): AbortSignal | undefined {
  if (option === undefined || option === null) return undefined;
  if (typeof AbortSignal !== "undefined" && option instanceof AbortSignal) return option;
  const candidate = (option as { signal?: AbortSignal }).signal;
  return candidate ?? undefined;
}

async function getJson<T>(path: string, signal?: SignalOption): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { method: "GET", signal: resolveSignal(signal) });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(null, error instanceof Error ? error.message : "Network request failed");
  }
  if (!response.ok) {
    let message = response.statusText || `Request failed with status ${String(response.status)}`;
    try {
      const body: unknown = await response.json();
      if (body !== null && typeof body === "object" && "detail" in body) {
        const detail = (body as Record<string, unknown>).detail;
        if (typeof detail === "string" && detail.length > 0) message = detail;
      }
    } catch {
      // Fall back to the status text when the error body is not JSON.
    }
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as T;
}

function enc(segment: string): string {
  return encodeURIComponent(segment);
}

// ---------------------------------------------------------------------------
// Shared primitives (mirror mike_trade/dashboard/api.py response models).
// Decimal fields serialize as JSON strings and are typed as string here.
// ---------------------------------------------------------------------------

export interface Sourced<T> {
  value: T | null;
  as_of: string | null;
  fresh: boolean;
  stale_reason: string | null;
  provenance: string | null;
}

export interface AccountSummary {
  account_id: string;
  mode: string;
  scenario: string | null;
  label: string;
}

export interface AccountsResponse {
  accounts: AccountSummary[];
  server_time: string;
}

export interface LinkStatus {
  connected: boolean | null;
  status: "CONNECTED" | "DISCONNECTED" | "UNKNOWN";
  as_of: string | null;
  age_seconds: number | null;
  fresh: boolean;
  stale_reason: string | null;
  provenance: string | null;
}

export interface ExchangeClock {
  exchange: string;
  timezone: string;
  now_et: string;
  session_date: string | null;
  session_status: string;
  is_early_close: boolean | null;
  close_at: string | null;
  close_at_et: string | null;
}

export interface EntryCutoff {
  at: string | null;
  at_et: string | null;
  session_date: string | null;
  passed_today: boolean;
}

export interface AccountStatus {
  account_id: string;
  mode: string;
  scenario: string | null;
  engine_state: "RUNNING" | "PAUSED" | "DEGRADED" | "OFFLINE" | "RECONCILING";
  last_heartbeat_at: string | null;
  last_heartbeat_at_et: string | null;
  heartbeat_age_seconds: number | null;
  is_stale: boolean;
  fresh: boolean;
  stale_reason: string | null;
  broker: LinkStatus;
  data: LinkStatus;
  market: "OPEN" | "CLOSED" | "HALTED";
  exchange_clock: ExchangeClock;
  next_entry_cutoff: EntryCutoff;
  entries_allowed: boolean;
  entries_blocked_reasons: string[];
  server_time: string;
  server_time_et: string;
}

export interface KpisResponse {
  account_id: string;
  mode: string;
  engine_state: string;
  server_time: string;
  cash: Sourced<string>;
  buying_power: Sourced<string>;
  equity: Sourced<string>;
  configured_total_budget: Sourced<string>;
  reserved_pending_buys: Sourced<string>;
  deployed_capital: Sourced<string>;
  open_position_count: Sourced<number>;
  pending_order_count: Sourced<number>;
  daily_realized_pnl: Sourced<string>;
  daily_unrealized_pnl: Sourced<string>;
  lifetime_realized_pnl: Sourced<string>;
  lifetime_pnl: Sourced<string>;
  protection_error_count: Sourced<number>;
  data_error_count: Sourced<number>;
  reconciliation_error_count: Sourced<number>;
}

export interface EvaluationSummary {
  evaluated_at: string;
  status: string;
  reason: string;
  strategy_key: string | null;
  role: string | null;
  bar_time: string | null;
}

export interface SignalSummary {
  signal_id: string;
  action: string;
  reference_price: string | null;
  reason: string;
  generated_at: string;
  disposition: "ACCEPTED" | "REJECTED" | "UNDECIDED";
  disposition_reason: string | null;
}

export interface WatchRow {
  symbol: string;
  configured: boolean;
  price: Sourced<string>;
  bid: Sourced<string>;
  ask: Sourced<string>;
  data_age_seconds: Sourced<number>;
  data_stale: boolean;
  feed: Sourced<string>;
  phase: Sourced<string>;
  active_buy_strategies: Sourced<string[]>;
  active_sell_strategies: Sourced<string[]>;
  next_due_trigger: Sourced<string>;
  last_evaluation: Sourced<EvaluationSummary>;
  last_signal: Sourced<SignalSummary>;
  confirmed_quantity: Sourced<string>;
  cost_basis: Sourced<string>;
  purchase_time: Sourced<string>;
  symbol_budget: Sourced<string>;
  commitment: Sourced<string>;
  planned_loss: Sourced<string>;
  daily_entry_count: Sourced<number>;
  daily_entry_cap: Sourced<number>;
  loss_streak: Sourced<number>;
  lockout_active: Sourced<boolean>;
  lockout_until: Sourced<string>;
  loss_reset_mode: Sourced<string>;
  cooldown_until: Sourced<string>;
  next_eligible_at: Sourced<string>;
  min_hold_remaining_seconds: Sourced<number>;
  entry_cutoff: Sourced<string>;
  protection_mode: Sourced<string>;
  protection_status: Sourced<string>;
  protection_price: Sourced<string>;
  protection_quantity: Sourced<string>;
  native_broker_hwm: Sourced<string>;
  stepped_anchor: Sourced<string>;
  desired_stop: Sourced<string>;
  confirmed_stop: Sourced<string>;
  last_protection_update: Sourced<string>;
  realized_pnl_today: Sourced<string>;
  unrealized_pnl: Sourced<string>;
  lifetime_realized_pnl: Sourced<string>;
  lifetime_pnl: Sourced<string>;
}

export interface WatchlistResponse {
  account_id: string;
  mode: string;
  engine_state: string;
  server_time: string;
  symbols: WatchRow[];
}

export interface TimelineEntry {
  kind: "EVALUATION" | "SIGNAL";
  at: string;
  status: string;
  reason: string;
  strategy_key: string | null;
  role: string | null;
  bar_time: string | null;
  action: string | null;
  reference_price: string | null;
  disposition: string | null;
}

export interface EvidenceEntry {
  signal_id: string;
  generated_at: string;
  provenance: Record<string, unknown>;
}

export interface FeedProvenance {
  feed: string | null;
  provenance: string | null;
  consolidated: boolean | null;
  last_bar_at: string | null;
  recorded_at: string | null;
}

export interface OrderNode {
  broker_order_id: string;
  client_order_id: string;
  parent_broker_order_id: string | null;
  purpose: string | null;
  side: string;
  order_type: string;
  status: string;
  quantity: string;
  filled_quantity: string;
  filled_avg_price: string | null;
  limit_price: string | null;
  stop_price: string | null;
  submitted_at: string;
  updated_at: string;
}

export interface CycleOrderTree {
  cycle_id: string;
  state: string;
  orders: OrderNode[];
}

export interface ProtectionEvent {
  at: string;
  cycle_id: string;
  event_type: string;
  previous_stop_price: string | null;
  new_stop_price: string | null;
  proposed_stop_price: string | null;
  confirmed_stop_price: string | null;
  payload: Record<string, unknown>;
}

export interface ManagedCycle {
  cycle_id: string;
  state: string;
  entry_strategy_key: string | null;
  opened_at: string | null;
  closed_at: string | null;
  holding_seconds: number | null;
  realized_pnl: string | null;
  entry_reference_price: string | null;
  entry_average_price: string | null;
  entry_slippage: string | null;
  exit_reference_price: string | null;
  exit_average_price: string | null;
  exit_slippage: string | null;
  decision_reasons: string[];
}

export interface SymbolDetailResponse {
  account_id: string;
  mode: string;
  symbol: string;
  server_time: string;
  fresh: boolean;
  stale_reason: string | null;
  timeline: TimelineEntry[];
  evidence: EvidenceEntry[];
  feed_provenance: FeedProvenance;
  order_tree: CycleOrderTree[];
  protection_history: ProtectionEvent[];
  managed_cycles: ManagedCycle[];
}

export interface PositionRow {
  symbol: string;
  cycle_id: string | null;
  management: string;
  direction: string;
  quantity: string;
  average_entry_price: string;
  market_price: string | null;
  market_value: string | null;
  unrealized_pl: string | null;
  as_of: string;
  fresh: boolean;
  stale_reason: string | null;
}

export interface Balances {
  cash: string | null;
  equity: string | null;
  buying_power: string | null;
  portfolio_value: string | null;
  source: string | null;
  as_of: string | null;
  fresh: boolean;
  stale_reason: string | null;
}

export interface OrderRow {
  broker_order_id: string;
  client_order_id: string;
  symbol: string;
  side: string;
  order_type: string;
  status: string;
  quantity: string;
  filled_quantity: string;
  submitted_at: string;
  updated_at: string;
  is_open: boolean;
  fresh: boolean;
  stale_reason: string | null;
}

export interface ActivityRow {
  kind: "BROKER_ACTIVITY" | "CASH_EVENT";
  activity_type: string;
  occurred_at: string;
  amount: string | null;
  description: string | null;
  payload: Record<string, unknown>;
}

export interface DriftRow {
  drift_id: string;
  drift_type: string;
  entity_ref: string | null;
  expected: unknown;
  actual: unknown;
  detected_at: string;
}

export interface ReconciliationDetail {
  last_run_at: string | null;
  last_result: string | null;
  unresolved_drift_count: number;
  drifts: DriftRow[];
}

export interface AccountDetailResponse {
  account_id: string;
  mode: string;
  server_time: string;
  balances: Balances;
  positions: PositionRow[];
  external_holdings: PositionRow[];
  orders: OrderRow[];
  total_fees: string;
  activity: ActivityRow[];
  reconciliation: ReconciliationDetail;
}

export interface FeedHealth {
  link: LinkStatus;
  age_seconds: Sourced<number>;
  reconnects: Sourced<number>;
  subscription_count: Sourced<number>;
}

export interface BrokerHealth {
  link: LinkStatus;
  rate_limit_errors: Sourced<number>;
}

export interface PersistenceHealth {
  database_path: string;
  database_id: string | null;
  schema_version: number | null;
  size_bytes: number | null;
  wal_size_bytes: number | null;
  last_backup_at: Sourced<string>;
  last_backup_integrity_ok: boolean | null;
}

export interface ActionableError {
  code: string;
  severity: "critical" | "warning" | "info";
  message: string;
  command: string;
}

export interface HealthResponse {
  account_id: string;
  mode: string;
  engine_state: string;
  server_time: string;
  feed: FeedHealth;
  broker: BrokerHealth;
  scheduler_lag_seconds: Sourced<number>;
  persistence: PersistenceHealth;
  last_reconciliation_at: Sourced<string>;
  errors: ActionableError[];
}

export interface TradeRecord {
  trade_id: string; execution_mode: string; account_id: string; scenario_id: string | null;
  research_run_id: string | null; symbol: string; cycle_id: string;
  entry_timestamp: string | null; exit_timestamp: string | null;
  entry_price: string | null; exit_price: string | null; filled_quantity: string;
  exited_quantity: string; open_quantity: string; entry_notional: string | null;
  exit_notional: string | null; realized_pnl: string | null; unrealized_pnl: string | null;
  realized_return_percent: string | null; position_status: string;
  entry_strategy_id: string | null; entry_strategy_version: string | null;
  entry_signal_id: string | null; entry_signal_timestamp: string | null;
  entry_decision: string | null; exit_strategy_id: string | null;
  exit_strategy_version: string | null; exit_signal_id: string | null;
  exit_signal_timestamp: string | null; exit_reason: string | null;
  protective_stop_mode: string | null; initial_stop_price: string | null;
  final_stop_price: string | null; holding_duration_seconds: string | null;
  data_provenance: string | null; attribution_complete: boolean;
}

export interface TradeListResponse {
  items: TradeRecord[]; page: number; page_size: number; total: number; pages: number;
  available_entry_strategies: string[]; available_exit_reasons: string[];
}

export interface ResearchRun {
  run_id: string; created_at: string | null; symbols: string[]; period_role: string | null;
  window_start: string | null; window_end: string | null; dataset_id: string | null;
  config_fingerprint: string | null; artifact_version: string | null;
}

export interface TradeFill {
  fill_id: string; order_id: string | null; side: string; timestamp: string;
  price: string; quantity: string; fee: string;
}
export interface TradeBar {
  timestamp: string; timestamp_et: string; open: string; high: string; low: string;
  close: string; vwap: string | null; volume: string; feed: string | null;
  provenance: string | null;
}
export interface TradeEvent {
  timestamp: string; timestamp_et: string; event_type: string; source: string | null;
  strategy_id: string | null; strategy_version: string | null; signal_id: string | null;
  order_id: string | null; price: string | null; quantity: string | null;
  reason: string | null; details: Record<string, unknown>;
}
export interface TradeDiagnostics {
  basis: string; observations_available: boolean; observations_through: string | null;
  entry_price: string | null; exit_price: string | null; quantity: string;
  realized_pnl: string | null; unrealized_pnl: string | null;
  realized_return_percent: string | null; holding_duration_seconds: string | null;
  maximum_favorable_excursion: string | null; maximum_adverse_excursion: string | null;
  maximum_unrealized_gain: string | null; maximum_unrealized_loss: string | null;
  best_price: string | null; worst_price: string | null; initial_stop_distance: string | null;
  final_stop_distance: string | null; exit_reason: string | null; limitation: string | null;
}
export interface TradeInspection {
  trade: TradeRecord; fills: TradeFill[]; bars: TradeBar[]; events: TradeEvent[];
  diagnostics: TradeDiagnostics; bars_available: boolean; bars_limitation: string | null;
  timezone: "America/New_York";
}

export interface TradeFilters {
  page?: number; pageSize?: number; symbol?: string; dateFrom?: string; dateTo?: string;
  entryStrategy?: string; exitFilter?: string; outcome?: string; positionStatus?: string;
  sort?: string; direction?: string;
}

function tradeParams(filters: TradeFilters): string {
  const params = new URLSearchParams();
  if (filters.page) params.set("page", String(filters.page));
  if (filters.pageSize) params.set("page_size", String(filters.pageSize));
  if (filters.symbol) params.set("symbol", filters.symbol);
  if (filters.dateFrom) params.set("date_from", filters.dateFrom);
  if (filters.dateTo) params.set("date_to", filters.dateTo);
  if (filters.entryStrategy) params.set("entry_strategy", filters.entryStrategy);
  if (filters.exitFilter) params.set("exit_filter", filters.exitFilter);
  if (filters.outcome) params.set("outcome", filters.outcome);
  if (filters.positionStatus) params.set("position_status", filters.positionStatus);
  if (filters.sort) params.set("sort", filters.sort);
  if (filters.direction) params.set("direction", filters.direction);
  const value = params.toString(); return value ? `?${value}` : "";
}

// ---------------------------------------------------------------------------
// GET endpoints (same-origin relative /api URLs only).
// ---------------------------------------------------------------------------

export function listAccounts(signal?: SignalOption): Promise<AccountsResponse> {
  return getJson<AccountsResponse>("/api/accounts", signal);
}

export function getStatus(accountId: string, signal?: SignalOption): Promise<AccountStatus> {
  return getJson<AccountStatus>(`/api/accounts/${enc(accountId)}/status`, signal);
}

export function getKpis(accountId: string, signal?: SignalOption): Promise<KpisResponse> {
  return getJson<KpisResponse>(`/api/accounts/${enc(accountId)}/kpis`, signal);
}

export function getWatchlist(accountId: string, signal?: SignalOption): Promise<WatchlistResponse> {
  return getJson<WatchlistResponse>(`/api/accounts/${enc(accountId)}/watchlist`, signal);
}

export function getHealth(accountId: string, signal?: SignalOption): Promise<HealthResponse> {
  return getJson<HealthResponse>(`/api/accounts/${enc(accountId)}/health`, signal);
}

export function getSymbolDetail(
  accountId: string,
  symbol: string,
  signal?: SignalOption,
): Promise<SymbolDetailResponse> {
  return getJson<SymbolDetailResponse>(
    `/api/accounts/${enc(accountId)}/symbols/${enc(symbol)}`,
    signal,
  );
}

export function getAccountDetail(
  accountId: string,
  signal?: SignalOption,
): Promise<AccountDetailResponse> {
  return getJson<AccountDetailResponse>(`/api/accounts/${enc(accountId)}/detail`, signal);
}

export function getTrades(accountId: string, filters: TradeFilters = {}, signal?: SignalOption) {
  return getJson<TradeListResponse>(`/api/accounts/${enc(accountId)}/trades${tradeParams(filters)}`, signal);
}
export function getTrade(accountId: string, tradeId: string, signal?: SignalOption) {
  return getJson<TradeInspection>(`/api/accounts/${enc(accountId)}/trades/${enc(tradeId)}`, signal);
}
export function getResearchRuns(signal?: SignalOption) {
  return getJson<{ runs: ResearchRun[] }>("/api/research/runs", signal);
}
export function getResearchTrades(runId: string, filters: TradeFilters = {}, signal?: SignalOption) {
  return getJson<TradeListResponse>(`/api/research/runs/${enc(runId)}/trades${tradeParams(filters)}`, signal);
}
export function getResearchTrade(runId: string, tradeId: string, signal?: SignalOption) {
  return getJson<TradeInspection>(`/api/research/runs/${enc(runId)}/trades/${enc(tradeId)}`, signal);
}
