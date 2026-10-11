# mike_trade Product Requirements (as implemented)

This is the self-contained product contract for the implemented `mike_trade`
system. It describes what the software does today, not a roadmap. For
operations see `docs/OPERATIONS.md`; for risk controls see
`docs/SAFETY_AND_RISK.md`; for the broker see `docs/ALPACA.md`; for the UI
see `docs/DASHBOARD.md`; for verification see `docs/TEST_PLAN.md`. For
module boundaries see `docs/ARCHITECTURE.md` and for storage see
`docs/DATABASE.md`.

No statement in this file is a profitability, edge, or performance claim.
Paper, shadow, and backtest results never prove live performance.

## 1. Scope

- **Universe:** 1–10 U.S. equities. The dashboard watchlist renders at most
  10 symbols (`_MAX_WATCHLIST = 10` in `mike_trade/dashboard/api.py`).
  Operator commands: `make symbols`, `make symbols-add`,
  `make symbols-remove`, `make symbols-enable`, `make symbols-disable`.
- **Direction:** long and short, as four independent strategy roles: BUY
  opens a long, SELL closes a long, SHORT opens a short, COVER closes a
  short. A SELL never closes a short and a COVER never closes a long; there
  is no automatic reversal and never a simultaneous long and short in one
  symbol. Short selling is **opt-in and fails closed**
  (`execution.short_selling_enabled`): it is simulated in SHADOW and BACKTEST,
  requires a recorded capability verification plus every broker/account gate
  for PAPER (`docs/ALPACA.md` §6, `docs/SAFETY_AND_RISK.md` §4a), and is never
  activated for LIVE.
- **Position cycle:** one intentional entry (long or short) and then one full
  discretionary exit per cycle (`mike_trade/portfolio/`). There is no
  averaging down, no pyramiding, and no planned partial exits
  (`mike_trade/domain/trading.py`: "planned partial exits are outside" the
  contract). Exits submit the full remaining quantity
  (`mike_trade/portfolio/manager.py`: "Submit one full-remaining exit").
- **Strategies:** independently configurable BUY and SELL plugin classes
  evaluated as pure functions of a shared, warmed, fresh, completed-bar
  `MarketContext`. Strategies never submit orders, never call brokers, never
  open connections, and never fetch data (`docs/ARCHITECTURE.md` §3.4,
  `docs/STRATEGIES.md`).
- **Processes:** exactly two OS processes on one host — the trading engine
  (single writer) and the dashboard (separate read-only FastAPI process).
  See `docs/DASHBOARD.md` and `docs/ARCHITECTURE.md` §1.

## 2. The four modes and their isolation

`mike_trade/runtime.py` defines exactly four `ExecutionMode` values. There
is no fifth mode and no fallback between modes.

| Mode | What it does | Orders go to | Credentials needed |
| :--- | :--- | :--- | :--- |
| `BACKTEST` | Deterministic historical replay over versioned bars from a run manifest | Nowhere (no broker access) | None |
| `SHADOW` | Forward emulation with a persistent simulated broker and named scenarios on one shared live feed; fills/fees labeled estimates | Simulated broker only | Data credentials only |
| `PAPER` | Live-market trading against the Alpaca paper endpoint | `https://paper-api.alpaca.markets` | Paper credentials only |
| `LIVE` | Live-market trading against the Alpaca live endpoint | `https://api.alpaca.markets` | Live credentials plus explicit authorization |

Isolation rules (enforced, not conventional):

- One SQLite WAL database per operational environment; `db_identity` binds a
  file permanently to one environment and cross-environment opens and
  cross-environment restores are refused (`docs/DATABASE.md` §1, §8.4).
- Every trade-affecting row carries an environment-qualified
  `account_scope_id` (`paper:ACCT`, `shadow:ACCT:scenario`,
  `backtest:ACCT:run`, `live:ACCT`); reads always filter by scope.
- Alpaca paper clients load **only** `ALPACA_PAPER_KEY_ID` /
  `ALPACA_PAPER_SECRET_KEY`; live clients load **only** `ALPACA_LIVE_KEY_ID` /
  `ALPACA_LIVE_SECRET_KEY`. A missing variable raises; it never falls back
  to the other environment's variables
  (`mike_trade/brokers/alpaca/auth.py`). State directories, journals, and
  ledgers are namespaced per `(environment, credential fingerprint)`.
- A client constructed for one Alpaca environment refuses base URLs of the
  other (`EnvironmentMismatchError`); `broker-api.*` hosts are rejected
  outright.
- `MIKE_TRADE_ACCOUNT_MODE` cannot select live; live must be declared in a
  config file plus explicit CLI live confirmation (`.env.example`).
- Operator scope selection: `--account` / `ACCOUNT=` (e.g.
  `ACCOUNT=SHADOW:default`, `ACCOUNT=PAPER:main`, `ACCOUNT=LIVE:main`) with
  optional `--scenario` / `SCENARIO=` for shadow. The safe CLI default is a
  shadow scope.

Mode entrypoints (all exist in `Makefile` and `mike_trade/cli.py`):

- Backtest: `make backtest`, `make backtest-status`, `make backtest-report`,
  `make backtest-list`.
- Shadow: `make shadow-start`, `make shadow-stop`, `make shadow-list`,
  `make shadow-create`, `make shadow-reset`, `make report-shadow`.
- Paper: `make paper-start`, `make paper-stop`, `make paper-status`,
  `make broker-check ACCOUNT=PAPER:main`.
- Live: `make live-preflight`, `make live-enable`, `make live-start`,
  `make live-disable` (the last is risk-reducing and needs no confirmation).

## 3. Numeric and time conventions

- **Money, quantity, price, P&L are `Decimal`, never `float`.** JSON output
  renders Decimals as strings; secrets are redacted. Strategy `DECIMAL`
  parameters accept `Decimal` or `int` only; `float` is rejected and
  non-finite values (`NaN`, `Infinity`) are rejected (`docs/STRATEGIES.md`
  §1.3). HTTP bodies decode numbers to `Decimal`
  (`mike_trade/brokers/alpaca/http.py`).
- **Timestamps are timezone-aware UTC.** Persistence serializes datetimes as
  fixed-width microsecond UTC text (`mike_trade/persistence/schema.py`); the
  dashboard additionally renders exchange-local (`*_et`) siblings and all
  session logic uses the `America/New_York` calendar
  (`mike_trade/scheduling/calendar.py`, `mike_trade/risk/session_policy.py`).

## 4. Persistence

- One SQLite WAL database per environment (§2); single writer enforced by
  `WriterLock` (`<db>.writer.lock`); all mutation inside `unit_of_work()`
  (`BEGIN IMMEDIATE` … commit, else rollback).
- `PRAGMA journal_mode = WAL`, `synchronous = FULL`, `foreign_keys = ON`,
  `busy_timeout = 5000` on write connections; read-only URI plus
  `query_only` and a write-denying authorizer on read connections.
- Forward-only versioned migrations (`mike_trade/persistence/migrations/`,
  `NNNN_name.sql`, consecutively numbered, SHA-256 verified, no destructive
  rewrites of trade history).
- Append-only audit tables guarded by `BEFORE UPDATE` / `BEFORE DELETE`
  triggers that abort: `domain_events`, `fills`, `broker_order_events`,
  `broker_activities`, `cash_events`, `trailing_stop_events`,
  `operator_commands`, `reconciliation_drifts`, `equity_snapshots`. Operator
  commands are journaled; reconciliation drifts are persisted per run.
- **No raw tick archive.** The system persists candles, quotes/snapshots as
  needed for context, signals, evaluations, decisions, intents, orders,
  fills, protection events, and reports — not an archival raw-tick store.
- Operator tools (all exist): `make db-check` (integrity + migration
  state), `make db-backup` (online backup with manifest sidecar),
  `make db-restore-dry-run` (verify without writing). See
  `docs/SAFETY_AND_RISK.md` §10 for the backup/restore procedure.

## 5. Strategies (shipped semantics)

Strategy evaluation is edge-triggered on the latest **completed,
regular-session 1-minute bar** with warmed shared indicators; developing
bars never signal and volume is always checked. Full specs live in
`docs/STRATEGIES.md` and `mike_trade/strategies/`. Introspection:
`make strategies`, `make strategy-info STRATEGY=<id>`,
`make strategy-check STRATEGY=<id>`.

Shipped strategy ids (`STRATEGY_ID` in source):

| Id | Side | Semantics |
| :--- | :--- | :--- |
| `vwap_bounce` | BUY | Session-VWAP upward-crossing reference: prior completed close at or below prior-bar session VWAP (`vwap_prev`) and latest completed close strictly above latest-bar session VWAP (`vwap`) → `SIGNAL`. |
| `vwap_breakdown` | SELL (advisory exit trigger) | Session-VWAP downward-crossing reference: prior completed close at or above `vwap_prev` and latest completed close strictly below `vwap` → exit trigger. Advisory only: no order submission. |
| `momentum_breakout` | BUY | Breakout entry-signal plugin (see `docs/STRATEGIES.md` and `make strategy-info`). |
| `opening_range_breakout` | BUY | Opening-range breakout entry-signal plugin. |
| `momentum_reversal` | SELL (advisory) | Reversal exit-signal plugin. |
| `rsi_reversal` | SELL (advisory) | RSI-based exit-signal plugin. |

Notes:

- `vwap_bounce` / `vwap_breakdown` are the shipped session-VWAP reference
  pair. "vwap_reference" in planning documents refers to these shared
  reference semantics (warmed, fresh, session-reset `vwap` / `vwap_prev`
  supplied by the indicator service; strategies never compute VWAP); it is
  not a separate registered strategy id in this checkout.
- BUY strategies emit entry signals only (no sizing, stops, targets, or
  order submission); SELL strategies emit advisory exit triggers only.
  Sizing, risk gating, and submission happen outside strategies.
- Duplicate suppression: the same completed bar never emits twice
  (`last_signal_bar`); a new session resets firing state; evaluation
  windows never span sessions.

### Strategy collections per symbol

Each watched symbol configures an independent, arbitrary-length collection of
BUY strategies and of SELL strategies (`symbols.<TICKER>.buy_strategies` /
`sell_strategies`; the singular `buy_strategy` / `sell_strategy` spellings
remain accepted as one-element collections). Each name refers to an entry in
`strategies:` (plugin, side, parameters), so the same plugin can be tuned
differently on different symbols by defining several named entries. Rules:

- BUY and SELL collections are independent; SELL strategies are never paired
  with a particular BUY strategy. The SELL collection may be empty because the
  mandatory `stop_policy` protects the position without any SELL strategy.
- One plugin may serve a symbol at most once per side (signal ids, persisted
  state and arbitration are keyed by plugin id); a BUY/SELL role mismatch, an
  unknown plugin, unknown or invalid parameters fail closed.
- Flat symbol: every BUY strategy is evaluated and at most one entry is
  selected (`signals.decisions.arbitrate_entry_decisions`: completed-bar time,
  then `strategy_id`, then `signal_id`). A held position or a working entry
  order rejects all candidates, so simultaneous BUY signals never duplicate a
  purchase or scale a position.
- Held symbol: every SELL strategy is evaluated and at most one full-remainder
  exit is selected (`arbitrate_exit_decisions`), subject to minimum hold and an
  already-working exit; protective stops outrank strategy exits and are
  evaluated first.
- The SHADOW stream currently builds the 1-minute shared context only, so
  strategies that need other timeframes (`rsi_reversal`, 5-minute) are refused
  at start-up rather than silently never firing.

## 6. Explicitly out of scope

- LIVE short selling, unrestricted margin or leverage, margin semantics
  beyond broker-reported buying power, options, futures, forex, crypto, or
  non-U.S. venues.
- Averaging down, pyramiding, or planned partial exits.
- Raw-tick archival, charting-terminal UI, or price prediction.
- Enterprise Alpaca Broker API (`broker-api.*` hosts are rejected).
- Consolidated SIP data on the default feed (default is Basic IEX; see
  `docs/ALPACA.md` §2).
- Automatic live promotion from paper results; profitability or
  live-performance claims from paper/shadow/backtest output.
- Dashboard order placement or configuration mutation (read-only by
  architecture; see `docs/DASHBOARD.md`).
- Automated live orders in any test suite (see `docs/TEST_PLAN.md`).
- Installed systemd units in development: `deploy/systemd/*.service` are
  optional future-use templates only, never copied, enabled, or started by
  this repository (see `docs/SAFETY_AND_RISK.md` §9 and
  `docs/DASHBOARD.md` §3).
