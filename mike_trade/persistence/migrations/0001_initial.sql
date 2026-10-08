CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  applied_at TEXT NOT NULL
);

CREATE TABLE db_identity (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  environment TEXT NOT NULL CHECK (environment IN ('LIVE','PAPER','SHADOW','BACKTEST')),
  database_id TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TRIGGER db_identity_no_update
BEFORE UPDATE ON db_identity
BEGIN
  SELECT RAISE(ABORT, 'immutable: db_identity');
END;

CREATE TRIGGER db_identity_no_delete
BEFORE DELETE ON db_identity
BEGIN
  SELECT RAISE(ABORT, 'immutable: db_identity');
END;

CREATE TABLE account_scopes (
  account_scope_id TEXT PRIMARY KEY,
  environment TEXT NOT NULL CHECK (environment IN ('LIVE','PAPER','SHADOW','BACKTEST')),
  broker TEXT,
  broker_account_ref TEXT,
  scenario_name TEXT,
  run_id TEXT,
  created_at TEXT NOT NULL,
  CHECK (environment <> 'SHADOW' OR scenario_name IS NOT NULL),
  CHECK (environment <> 'BACKTEST' OR run_id IS NOT NULL),
  CHECK (environment NOT IN ('LIVE','PAPER') OR broker_account_ref IS NOT NULL)
);

CREATE TRIGGER account_scopes_environment_guard
BEFORE INSERT ON account_scopes
WHEN (SELECT environment FROM db_identity WHERE id = 1) IS NULL
  OR NEW.environment <> (SELECT environment FROM db_identity WHERE id = 1)
BEGIN
  SELECT RAISE(ABORT, 'account scope environment differs from database identity');
END;

CREATE INDEX account_scopes_created_at_idx
ON account_scopes(account_scope_id, created_at);

CREATE TABLE run_configs (
  run_config_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  config_fingerprint_sha256 TEXT NOT NULL,
  config_json TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  UNIQUE (account_scope_id, config_fingerprint_sha256)
);
CREATE INDEX run_configs_recorded_at_idx ON run_configs(account_scope_id, recorded_at);

CREATE TABLE operator_commands (
  command_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  command_kind TEXT NOT NULL,
  symbol TEXT,
  reason TEXT NOT NULL,
  issued_by TEXT NOT NULL,
  live_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (live_confirmed IN (0,1)),
  payload_json TEXT NOT NULL DEFAULT '{}',
  issued_at TEXT NOT NULL,
  processed_at TEXT,
  idempotency_key TEXT,
  UNIQUE (account_scope_id, idempotency_key)
);
CREATE INDEX operator_commands_issued_at_idx ON operator_commands(account_scope_id, issued_at);

CREATE TABLE service_health (
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  component TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('HEALTHY','DEGRADED','OFFLINE','UNKNOWN')),
  heartbeat_at TEXT,
  observed_at TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (account_scope_id, component)
);
CREATE INDEX service_health_observed_at_idx ON service_health(account_scope_id, observed_at);

CREATE TABLE instruments (
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  symbol TEXT NOT NULL,
  asset_class TEXT NOT NULL DEFAULT 'US_EQUITY',
  exchange TEXT,
  currency TEXT NOT NULL DEFAULT 'USD',
  tradable INTEGER NOT NULL DEFAULT 1 CHECK (tradable IN (0,1)),
  observed_at TEXT NOT NULL,
  PRIMARY KEY (account_scope_id, symbol)
);
CREATE INDEX instruments_observed_at_idx ON instruments(account_scope_id, observed_at);

CREATE TABLE instrument_capabilities (
  account_scope_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  supports_fractional INTEGER NOT NULL CHECK (supports_fractional IN (0,1)),
  supports_notional INTEGER NOT NULL CHECK (supports_notional IN (0,1)),
  supports_trailing_stop INTEGER NOT NULL CHECK (supports_trailing_stop IN (0,1)),
  supported_order_types_json TEXT NOT NULL,
  capability_source TEXT NOT NULL,
  demonstrated_in_paper INTEGER NOT NULL DEFAULT 0 CHECK (demonstrated_in_paper IN (0,1)),
  observed_at TEXT NOT NULL,
  PRIMARY KEY (account_scope_id, symbol),
  FOREIGN KEY (account_scope_id, symbol)
    REFERENCES instruments(account_scope_id, symbol)
);
CREATE INDEX instrument_capabilities_observed_at_idx
ON instrument_capabilities(account_scope_id, observed_at);

CREATE TABLE market_bars (
  market_bar_id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  bar_start TEXT NOT NULL,
  open_price TEXT NOT NULL CHECK (typeof(open_price) = 'text'),
  high_price TEXT NOT NULL CHECK (typeof(high_price) = 'text'),
  low_price TEXT NOT NULL CHECK (typeof(low_price) = 'text'),
  close_price TEXT NOT NULL CHECK (typeof(close_price) = 'text'),
  vwap TEXT CHECK (vwap IS NULL OR typeof(vwap) = 'text'),
  volume TEXT NOT NULL CHECK (typeof(volume) = 'text'),
  feed TEXT NOT NULL,
  provenance TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  UNIQUE (account_scope_id, symbol, timeframe, bar_start, feed)
);
CREATE INDEX market_bars_bar_start_idx ON market_bars(account_scope_id, bar_start);
CREATE INDEX market_bars_recorded_at_idx ON market_bars(account_scope_id, recorded_at);

CREATE TABLE strategy_instances (
  instance_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  strategy_key TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('BUY','SELL')),
  symbol TEXT NOT NULL,
  parameters_json TEXT NOT NULL,
  enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
  pair_key TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (account_scope_id, instance_id)
);
CREATE INDEX strategy_instances_created_at_idx ON strategy_instances(account_scope_id, created_at);

CREATE TABLE strategy_evaluations (
  evaluation_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  instance_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  bar_time TEXT,
  status TEXT NOT NULL CHECK (status IN ('NO_SIGNAL','SIGNAL','INSUFFICIENT_DATA','STALE_DATA','ERROR','NOT_DUE')),
  reason TEXT NOT NULL,
  error_detail TEXT,
  evaluated_at TEXT NOT NULL,
  FOREIGN KEY (account_scope_id, instance_id)
    REFERENCES strategy_instances(account_scope_id, instance_id)
);
CREATE INDEX strategy_evaluations_evaluated_at_idx
ON strategy_evaluations(account_scope_id, evaluated_at);

CREATE TABLE signals (
  signal_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  instance_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('ENTER_LONG','EXIT_LONG','ENTER_SHORT','COVER_SHORT')),
  reference_price TEXT CHECK (reference_price IS NULL OR typeof(reference_price) = 'text'),
  confidence TEXT CHECK (confidence IS NULL OR typeof(confidence) = 'text'),
  reason TEXT NOT NULL,
  provenance_json TEXT NOT NULL,
  bar_time TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  UNIQUE (account_scope_id, signal_id),
  FOREIGN KEY (account_scope_id, instance_id)
    REFERENCES strategy_instances(account_scope_id, instance_id)
);
CREATE INDEX signals_generated_at_idx ON signals(account_scope_id, generated_at);

CREATE TABLE decisions (
  decision_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  signal_id TEXT,
  outcome TEXT NOT NULL,
  reason TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  decided_at TEXT NOT NULL,
  FOREIGN KEY (account_scope_id, signal_id)
    REFERENCES signals(account_scope_id, signal_id)
);
CREATE INDEX decisions_decided_at_idx ON decisions(account_scope_id, decided_at);

CREATE TABLE position_cycles (
  cycle_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  symbol TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('LONG','SHORT')),
  state TEXT NOT NULL CHECK (state IN ('PENDING_ENTRY','OPEN','EXITING','CLOSED','ABANDONED')),
  entry_strategy_key TEXT,
  opened_at TEXT,
  closed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (account_scope_id, cycle_id)
);
CREATE INDEX position_cycles_created_at_idx ON position_cycles(account_scope_id, created_at);
CREATE INDEX position_cycles_updated_at_idx ON position_cycles(account_scope_id, updated_at);

CREATE TABLE budget_reservations (
  reservation_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  cycle_id TEXT,
  amount TEXT NOT NULL CHECK (typeof(amount) = 'text'),
  currency TEXT NOT NULL DEFAULT 'USD',
  state TEXT NOT NULL CHECK (state IN ('ACTIVE','CONSUMED','RELEASED')),
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  FOREIGN KEY (account_scope_id, cycle_id)
    REFERENCES position_cycles(account_scope_id, cycle_id)
);
CREATE INDEX budget_reservations_created_at_idx
ON budget_reservations(account_scope_id, created_at);

CREATE TABLE order_intents (
  intent_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  cycle_id TEXT NOT NULL,
  signal_id TEXT,
  client_order_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
  purpose TEXT NOT NULL CHECK (purpose IN ('ENTRY','STRATEGY_EXIT','PROTECTIVE_STOP','MANUAL_CLOSE','SAFETY_EXIT','EOD_CLOSE')),
  order_type TEXT NOT NULL CHECK (order_type IN ('MARKET','LIMIT','STOP','STOP_LIMIT','TRAILING_STOP')),
  time_in_force TEXT NOT NULL CHECK (time_in_force IN ('DAY','GTC')),
  quantity TEXT CHECK (quantity IS NULL OR typeof(quantity) = 'text'),
  notional TEXT CHECK (notional IS NULL OR typeof(notional) = 'text'),
  limit_price TEXT CHECK (limit_price IS NULL OR typeof(limit_price) = 'text'),
  stop_price TEXT CHECK (stop_price IS NULL OR typeof(stop_price) = 'text'),
  trail_price TEXT CHECK (trail_price IS NULL OR typeof(trail_price) = 'text'),
  trail_percent TEXT CHECK (trail_percent IS NULL OR typeof(trail_percent) = 'text'),
  status TEXT NOT NULL CHECK (status IN ('RECORDED','SUBMITTED','SUBMIT_FAILED','UNKNOWN')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (account_scope_id, intent_id),
  UNIQUE (account_scope_id, client_order_id),
  FOREIGN KEY (account_scope_id, cycle_id)
    REFERENCES position_cycles(account_scope_id, cycle_id),
  FOREIGN KEY (account_scope_id, signal_id)
    REFERENCES signals(account_scope_id, signal_id),
  CHECK ((quantity IS NULL) <> (notional IS NULL))
);
CREATE INDEX order_intents_created_at_idx ON order_intents(account_scope_id, created_at);
CREATE INDEX order_intents_updated_at_idx ON order_intents(account_scope_id, updated_at);

CREATE TABLE broker_orders (
  broker_order_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  broker_order_id TEXT NOT NULL,
  intent_id TEXT,
  client_order_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
  order_type TEXT NOT NULL CHECK (order_type IN ('MARKET','LIMIT','STOP','STOP_LIMIT','TRAILING_STOP')),
  status TEXT NOT NULL CHECK (status IN ('NEW','PENDING_NEW','ACCEPTED','PARTIALLY_FILLED','FILLED','PENDING_CANCEL','CANCELED','EXPIRED','REJECTED','REPLACED','UNKNOWN')),
  quantity TEXT NOT NULL CHECK (typeof(quantity) = 'text'),
  filled_quantity TEXT NOT NULL CHECK (typeof(filled_quantity) = 'text'),
  filled_avg_price TEXT CHECK (filled_avg_price IS NULL OR typeof(filled_avg_price) = 'text'),
  limit_price TEXT CHECK (limit_price IS NULL OR typeof(limit_price) = 'text'),
  stop_price TEXT CHECK (stop_price IS NULL OR typeof(stop_price) = 'text'),
  trail_price TEXT CHECK (trail_price IS NULL OR typeof(trail_price) = 'text'),
  trail_percent TEXT CHECK (trail_percent IS NULL OR typeof(trail_percent) = 'text'),
  submitted_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  raw_json TEXT,
  UNIQUE (account_scope_id, broker_order_id),
  FOREIGN KEY (account_scope_id, intent_id)
    REFERENCES order_intents(account_scope_id, intent_id)
);
CREATE INDEX broker_orders_submitted_at_idx ON broker_orders(account_scope_id, submitted_at);
CREATE INDEX broker_orders_updated_at_idx ON broker_orders(account_scope_id, updated_at);

CREATE TABLE broker_order_events (
  broker_order_event_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  broker_order_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  status TEXT,
  payload_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  FOREIGN KEY (account_scope_id, broker_order_id)
    REFERENCES broker_orders(account_scope_id, broker_order_id)
);
CREATE INDEX broker_order_events_occurred_at_idx
ON broker_order_events(account_scope_id, occurred_at);
CREATE INDEX broker_order_events_recorded_at_idx
ON broker_order_events(account_scope_id, recorded_at);

CREATE TABLE fills (
  fill_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  external_activity_id TEXT NOT NULL,
  broker_order_id TEXT NOT NULL,
  cycle_id TEXT,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
  quantity TEXT NOT NULL CHECK (typeof(quantity) = 'text'),
  price TEXT NOT NULL CHECK (typeof(price) = 'text'),
  fee TEXT NOT NULL CHECK (typeof(fee) = 'text'),
  filled_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  UNIQUE (account_scope_id, external_activity_id),
  FOREIGN KEY (account_scope_id, broker_order_id)
    REFERENCES broker_orders(account_scope_id, broker_order_id),
  FOREIGN KEY (account_scope_id, cycle_id)
    REFERENCES position_cycles(account_scope_id, cycle_id)
);
CREATE INDEX fills_filled_at_idx ON fills(account_scope_id, filled_at);
CREATE INDEX fills_recorded_at_idx ON fills(account_scope_id, recorded_at);

CREATE TABLE broker_activities (
  broker_activity_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  external_activity_id TEXT NOT NULL,
  activity_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  UNIQUE (account_scope_id, external_activity_id)
);
CREATE INDEX broker_activities_occurred_at_idx
ON broker_activities(account_scope_id, occurred_at);
CREATE INDEX broker_activities_recorded_at_idx
ON broker_activities(account_scope_id, recorded_at);

CREATE TABLE protection_orders (
  protection_order_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  cycle_id TEXT NOT NULL,
  broker_order_id TEXT,
  kind TEXT NOT NULL CHECK (kind IN ('FIXED_STOP','BROKER_TRAILING_STOP','STEPPED_TRAILING_STOP')),
  status TEXT NOT NULL CHECK (status IN ('PENDING','ACTIVE','REPLACING','UNKNOWN','CANCELED','TRIGGERED','UNSUPPORTED')),
  covered_quantity TEXT NOT NULL CHECK (typeof(covered_quantity) = 'text'),
  stop_price TEXT CHECK (stop_price IS NULL OR typeof(stop_price) = 'text'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (account_scope_id, protection_order_id),
  FOREIGN KEY (account_scope_id, cycle_id)
    REFERENCES position_cycles(account_scope_id, cycle_id),
  FOREIGN KEY (account_scope_id, broker_order_id)
    REFERENCES broker_orders(account_scope_id, broker_order_id)
);
CREATE INDEX protection_orders_created_at_idx
ON protection_orders(account_scope_id, created_at);
CREATE INDEX protection_orders_updated_at_idx
ON protection_orders(account_scope_id, updated_at);

CREATE TABLE trailing_stop_states (
  trailing_stop_state_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  cycle_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('BROKER_TRAILING_STOP','STEPPED_TRAILING_STOP')),
  status TEXT NOT NULL CHECK (status IN ('PENDING','ACTIVE','REPLACING','UNKNOWN','CANCELED','TRIGGERED','UNSUPPORTED')),
  covered_quantity TEXT NOT NULL CHECK (typeof(covered_quantity) = 'text'),
  stop_price TEXT NOT NULL CHECK (typeof(stop_price) = 'text'),
  high_water_mark TEXT NOT NULL CHECK (typeof(high_water_mark) = 'text'),
  broker_order_id TEXT,
  last_confirmed_at TEXT,
  updated_at TEXT NOT NULL,
  UNIQUE (account_scope_id, cycle_id),
  FOREIGN KEY (account_scope_id, cycle_id)
    REFERENCES position_cycles(account_scope_id, cycle_id),
  FOREIGN KEY (account_scope_id, broker_order_id)
    REFERENCES broker_orders(account_scope_id, broker_order_id)
);
CREATE INDEX trailing_stop_states_updated_at_idx
ON trailing_stop_states(account_scope_id, updated_at);

CREATE TABLE trailing_stop_events (
  trailing_stop_event_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  cycle_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  previous_stop_price TEXT CHECK (previous_stop_price IS NULL OR typeof(previous_stop_price) = 'text'),
  new_stop_price TEXT CHECK (new_stop_price IS NULL OR typeof(new_stop_price) = 'text'),
  payload_json TEXT NOT NULL DEFAULT '{}',
  occurred_at TEXT NOT NULL,
  FOREIGN KEY (account_scope_id, cycle_id)
    REFERENCES position_cycles(account_scope_id, cycle_id)
);
CREATE INDEX trailing_stop_events_occurred_at_idx
ON trailing_stop_events(account_scope_id, occurred_at);

CREATE TABLE positions (
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  symbol TEXT NOT NULL,
  cycle_id TEXT,
  management TEXT NOT NULL DEFAULT 'UNMANAGED' CHECK (management IN ('MANAGED','UNMANAGED','ADOPTED')),
  direction TEXT NOT NULL CHECK (direction IN ('LONG','SHORT')),
  quantity TEXT NOT NULL CHECK (typeof(quantity) = 'text'),
  average_entry_price TEXT NOT NULL CHECK (typeof(average_entry_price) = 'text'),
  market_price TEXT CHECK (market_price IS NULL OR typeof(market_price) = 'text'),
  market_value TEXT CHECK (market_value IS NULL OR typeof(market_value) = 'text'),
  unrealized_pl TEXT CHECK (unrealized_pl IS NULL OR typeof(unrealized_pl) = 'text'),
  as_of TEXT NOT NULL,
  PRIMARY KEY (account_scope_id, symbol),
  FOREIGN KEY (account_scope_id, cycle_id)
    REFERENCES position_cycles(account_scope_id, cycle_id)
);
CREATE INDEX positions_as_of_idx ON positions(account_scope_id, as_of);

CREATE TABLE cash_events (
  cash_event_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  event_type TEXT NOT NULL,
  amount TEXT NOT NULL CHECK (typeof(amount) = 'text'),
  currency TEXT NOT NULL DEFAULT 'USD',
  external_activity_id TEXT,
  description TEXT NOT NULL DEFAULT '',
  occurred_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  UNIQUE (account_scope_id, external_activity_id)
);
CREATE INDEX cash_events_occurred_at_idx ON cash_events(account_scope_id, occurred_at);
CREATE INDEX cash_events_recorded_at_idx ON cash_events(account_scope_id, recorded_at);

CREATE TABLE equity_snapshots (
  equity_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  cash TEXT NOT NULL CHECK (typeof(cash) = 'text'),
  equity TEXT NOT NULL CHECK (typeof(equity) = 'text'),
  buying_power TEXT NOT NULL CHECK (typeof(buying_power) = 'text'),
  portfolio_value TEXT CHECK (portfolio_value IS NULL OR typeof(portfolio_value) = 'text'),
  source TEXT NOT NULL,
  as_of TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  UNIQUE (account_scope_id, as_of, source)
);
CREATE INDEX equity_snapshots_as_of_idx ON equity_snapshots(account_scope_id, as_of);
CREATE INDEX equity_snapshots_recorded_at_idx
ON equity_snapshots(account_scope_id, recorded_at);

CREATE TABLE reconciliation_runs (
  reconciliation_run_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  status TEXT NOT NULL CHECK (status IN ('STARTED','CLEAN','DRIFTED','ERROR')),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  summary_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE (account_scope_id, reconciliation_run_id)
);
CREATE INDEX reconciliation_runs_started_at_idx
ON reconciliation_runs(account_scope_id, started_at);

CREATE TABLE reconciliation_drifts (
  drift_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  reconciliation_run_id TEXT NOT NULL,
  drift_type TEXT NOT NULL,
  entity_ref TEXT,
  expected_json TEXT,
  actual_json TEXT,
  detected_at TEXT NOT NULL,
  FOREIGN KEY (account_scope_id, reconciliation_run_id)
    REFERENCES reconciliation_runs(account_scope_id, reconciliation_run_id),
  UNIQUE (account_scope_id, drift_id)
);
CREATE INDEX reconciliation_drifts_detected_at_idx
ON reconciliation_drifts(account_scope_id, detected_at);

CREATE TABLE reconciliation_resolutions (
  resolution_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  drift_id TEXT NOT NULL,
  resolution_kind TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  resolved_by TEXT NOT NULL,
  resolved_at TEXT NOT NULL,
  FOREIGN KEY (account_scope_id, drift_id)
    REFERENCES reconciliation_drifts(account_scope_id, drift_id)
);
CREATE INDEX reconciliation_resolutions_resolved_at_idx
ON reconciliation_resolutions(account_scope_id, resolved_at);

CREATE TABLE strategy_state (
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  instance_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  state_json TEXT NOT NULL,
  armed INTEGER NOT NULL DEFAULT 1 CHECK (armed IN (0,1)),
  rearm_required INTEGER NOT NULL DEFAULT 0 CHECK (rearm_required IN (0,1)),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (account_scope_id, instance_id, symbol),
  FOREIGN KEY (account_scope_id, instance_id)
    REFERENCES strategy_instances(account_scope_id, instance_id)
);
CREATE INDEX strategy_state_updated_at_idx ON strategy_state(account_scope_id, updated_at);

CREATE TABLE entry_eligibility_counters (
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  symbol TEXT NOT NULL,
  session_date TEXT NOT NULL,
  originating_buy_strategy TEXT NOT NULL DEFAULT '',
  filled_entry_cycles INTEGER NOT NULL DEFAULT 0 CHECK (filled_entry_cycles >= 0),
  consecutive_losses INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_losses >= 0),
  updated_at TEXT NOT NULL,
  PRIMARY KEY (account_scope_id, symbol, session_date, originating_buy_strategy)
);
CREATE INDEX entry_eligibility_counters_updated_at_idx
ON entry_eligibility_counters(account_scope_id, updated_at);

CREATE TABLE entry_lockouts (
  entry_lockout_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  symbol TEXT NOT NULL,
  originating_buy_strategy TEXT,
  reason TEXT NOT NULL,
  locked_at TEXT NOT NULL,
  locked_until TEXT,
  cleared_at TEXT
);
CREATE INDEX entry_lockouts_locked_at_idx ON entry_lockouts(account_scope_id, locked_at);
CREATE INDEX entry_lockouts_locked_until_idx ON entry_lockouts(account_scope_id, locked_until);

CREATE TABLE entry_cooldowns (
  entry_cooldown_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  symbol TEXT NOT NULL,
  originating_buy_strategy TEXT,
  reason TEXT NOT NULL,
  started_at TEXT NOT NULL,
  cooldown_until TEXT NOT NULL,
  cleared_at TEXT
);
CREATE INDEX entry_cooldowns_started_at_idx ON entry_cooldowns(account_scope_id, started_at);
CREATE INDEX entry_cooldowns_until_idx ON entry_cooldowns(account_scope_id, cooldown_until);

CREATE TABLE entry_controls (
  account_scope_id TEXT PRIMARY KEY REFERENCES account_scopes(account_scope_id),
  kill_switch_enabled INTEGER NOT NULL DEFAULT 0 CHECK (kill_switch_enabled IN (0,1)),
  paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1)),
  reason TEXT NOT NULL DEFAULT '',
  updated_by TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX entry_controls_updated_at_idx ON entry_controls(account_scope_id, updated_at);

CREATE TABLE backtest_manifests (
  backtest_manifest_id TEXT PRIMARY KEY,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  run_id TEXT NOT NULL,
  config_fingerprint_sha256 TEXT NOT NULL,
  data_fingerprint_sha256 TEXT NOT NULL,
  code_version TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE (account_scope_id, run_id)
);
CREATE INDEX backtest_manifests_started_at_idx
ON backtest_manifests(account_scope_id, started_at);

CREATE TABLE domain_events (
  event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  account_scope_id TEXT NOT NULL REFERENCES account_scopes(account_scope_id),
  stream_type TEXT NOT NULL,
  stream_id TEXT NOT NULL,
  stream_version INTEGER NOT NULL CHECK (stream_version >= 1),
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  idempotency_key TEXT,
  causation_id TEXT,
  correlation_id TEXT,
  occurred_at TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  UNIQUE (account_scope_id, stream_type, stream_id, stream_version),
  UNIQUE (account_scope_id, idempotency_key)
);

CREATE TRIGGER domain_events_no_update
BEFORE UPDATE ON domain_events
BEGIN
  SELECT RAISE(ABORT, 'append-only: domain_events');
END;

CREATE TRIGGER domain_events_no_delete
BEFORE DELETE ON domain_events
BEGIN
  SELECT RAISE(ABORT, 'append-only: domain_events');
END;

CREATE INDEX domain_events_recorded_at_idx
ON domain_events(account_scope_id, recorded_at);
CREATE INDEX domain_events_event_type_occurred_at_idx
ON domain_events(account_scope_id, event_type, occurred_at);

CREATE TRIGGER fills_no_update BEFORE UPDATE ON fills
BEGIN SELECT RAISE(ABORT, 'append-only: fills'); END;
CREATE TRIGGER fills_no_delete BEFORE DELETE ON fills
BEGIN SELECT RAISE(ABORT, 'append-only: fills'); END;

CREATE TRIGGER broker_order_events_no_update BEFORE UPDATE ON broker_order_events
BEGIN SELECT RAISE(ABORT, 'append-only: broker_order_events'); END;
CREATE TRIGGER broker_order_events_no_delete BEFORE DELETE ON broker_order_events
BEGIN SELECT RAISE(ABORT, 'append-only: broker_order_events'); END;

CREATE TRIGGER broker_activities_no_update BEFORE UPDATE ON broker_activities
BEGIN SELECT RAISE(ABORT, 'append-only: broker_activities'); END;
CREATE TRIGGER broker_activities_no_delete BEFORE DELETE ON broker_activities
BEGIN SELECT RAISE(ABORT, 'append-only: broker_activities'); END;

CREATE TRIGGER cash_events_no_update BEFORE UPDATE ON cash_events
BEGIN SELECT RAISE(ABORT, 'append-only: cash_events'); END;
CREATE TRIGGER cash_events_no_delete BEFORE DELETE ON cash_events
BEGIN SELECT RAISE(ABORT, 'append-only: cash_events'); END;

CREATE TRIGGER trailing_stop_events_no_update BEFORE UPDATE ON trailing_stop_events
BEGIN SELECT RAISE(ABORT, 'append-only: trailing_stop_events'); END;
CREATE TRIGGER trailing_stop_events_no_delete BEFORE DELETE ON trailing_stop_events
BEGIN SELECT RAISE(ABORT, 'append-only: trailing_stop_events'); END;

CREATE TRIGGER operator_commands_no_update BEFORE UPDATE ON operator_commands
BEGIN SELECT RAISE(ABORT, 'append-only: operator_commands'); END;
CREATE TRIGGER operator_commands_no_delete BEFORE DELETE ON operator_commands
BEGIN SELECT RAISE(ABORT, 'append-only: operator_commands'); END;

CREATE TRIGGER reconciliation_drifts_no_update BEFORE UPDATE ON reconciliation_drifts
BEGIN SELECT RAISE(ABORT, 'append-only: reconciliation_drifts'); END;
CREATE TRIGGER reconciliation_drifts_no_delete BEFORE DELETE ON reconciliation_drifts
BEGIN SELECT RAISE(ABORT, 'append-only: reconciliation_drifts'); END;

CREATE TRIGGER equity_snapshots_no_update BEFORE UPDATE ON equity_snapshots
BEGIN SELECT RAISE(ABORT, 'append-only: equity_snapshots'); END;
CREATE TRIGGER equity_snapshots_no_delete BEFORE DELETE ON equity_snapshots
BEGIN SELECT RAISE(ABORT, 'append-only: equity_snapshots'); END;
