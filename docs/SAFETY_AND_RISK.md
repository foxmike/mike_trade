# Safety and Risk Controls — Operator Guide

This is the operator reference for every safety and risk control in the
implemented product. All controls below are enforced by code; the
descriptions name the implementing modules and the operator commands that
surface them. Every `make` target named here exists in the `Makefile` and
every CLI subcommand exists in `mike_trade/cli.py` (`CANONICAL_COMMANDS`).

No statement here is a profitability or performance claim. Stops do not
guarantee execution prices or maximum losses.

## 1. Budget and sizing gates (entries only)

Implemented in `mike_trade/domain/trading.py` (`BudgetPolicy`),
`mike_trade/portfolio/budgets.py`, `mike_trade/portfolio/reservations.py`,
and gated pre-entry by `mike_trade/risk/entry_eligibility.py`.

- **Per-symbol budget** (`per_symbol_budget` / `symbol_budget_usd`): caps the
  managed open-position entry notional plus pending reservations for one
  symbol. Must not exceed the total budget. Inspect with `make budget`.
- **Total application budget** (`total_application_budget` /
  `total_budget_usd`): caps managed open-position entry notional plus
  pending reservations across the account. Remaining budget is
  `total − committed − pending`. Inspect with `make budget`.
- **Broker buying-power check** (`require_broker_buying_power`, default
  true): an entry must also fit the broker-reported buying power (including
  non-margin / non-marginable buying-power fields where the broker
  supplies them). The caller remains responsible for provenance; inspect
  with `make balance`.
- **Planned-loss sizing** (`planned_loss_per_trade`, optional): when set,
  entry quantity is derived from the planned loss per trade against the
  protective distance, so a wider stop means a smaller position. Unit
  coverage: `tests/unit/test_position_sizing.py`,
  `tests/unit/test_budget_reservations.py`.
- **Maximum open positions** (`max_open_positions`, minimum 1): entries are
  refused with `MAX_OPEN_POSITIONS_REACHED` once the count of managed open
  positions reaches the limit. Inspect with `make entry-limits`.
- All budget math is `Decimal`; reservations are durable and restart-safe;
  every block decision carries an explicit reason
  (`EntryBlockReason`: `MAX_ENTRY_CYCLES_REACHED`,
  `LOSING_STREAK_LOCKOUT`, `POST_SALE_COOLDOWN`,
  `DAILY_LOSS_LIMIT_REACHED`, `MAX_OPEN_POSITIONS_REACHED`).

## 2. Loss guards, entry-cycle caps, lockouts, cooldowns

Durable, restart-safe, per account/scenario scope, persisted as
JSON-compatible section documents (`entry_cycles`, `losing_streaks`,
`post_sale_cooldowns`) in `mike_trade/risk/entry_eligibility.py`. The
trading day is the NY exchange session: counts reset at the next session
open, never at midnight or on weekends. View with `make entry-limits` and
`make cooldown-status`; audited reset with `make entry-limits-reset`
(needs live confirmation on LIVE).

- **Daily loss guard** (`daily_loss_limit`, optional, must be > 0): when the
  session realized P&L reaches `−daily_loss_limit`, new entries are refused
  with `DAILY_LOSS_LIMIT_REACHED`. It gates entries only; it never blocks
  exits, protection, reconciliation, or manual close.
- **Daily entry-cycle cap** (`max_entry_cycles_per_day` /
  `max_filled_entry_cycles_per_symbol_per_session`, optional): caps filled
  entry cycles per symbol per session. Refusals report
  `MAX_ENTRY_CYCLES_REACHED` with used-of-limit counts.
- **Symbol loss lockout** and **buy-strategy loss lockout**
  (`LosingStreakTracker`): consecutive losing cycles per
  (symbol) / (symbol, buy strategy) arm a lockout. A cycle closed without
  final fees still updates the streak from computed net but is recorded as
  provisional, and the decision reports `provisional=true` while a lockout
  rests on provisional data. Lockouts reset at the next session
  (`NEXT_SESSION`) or manually (`make entry-limits-reset`).
- **Post-sale cooldown** (`PostSaleCooldown`, `post_sale_cooldown_seconds`):
  after a sale on a symbol, re-entry on that symbol is refused with
  `POST_SALE_COOLDOWN` until the deadline passes. `0` disables it.
- **Minimum hold** (`MinimumHoldPolicy` in
  `mike_trade/risk/session_policy.py`): applies to **ordinary strategy SELL
  exits only**. Protective stops, trailing stops, risk/safety exits, daily
  loss exits, manual close, close-all, flatten, reconciliation, and
  end-of-day exits bypass the minimum hold
  (`mike_trade/portfolio/manager.py`: "Manual/risk exits bypass the minimum
  hold").

## 3. Session gates

`mike_trade/risk/session_policy.py` (`SessionPolicy`, `EntryCutoffPolicy`):

- **Regular-hours only** (`regular_hours_only`, default true): entries are
  allowed only during `SessionStatus.REGULAR`. Outside regular hours the
  decision is `OUTSIDE_REGULAR_HOURS`.
- **Entry cutoff** (`entry_cutoff.minutes_before_close`): stops accepting
  entries N minutes before the actual session close, including early closes.
  At or past the cutoff the decision is `ENTRY_CUTOFF_REACHED` with the
  computed `cutoff_at`.
- **Early close:** handled through the real session calendar
  (`mike_trade/scheduling/calendar.py`), so the cutoff tracks the actual
  close of the day.
- **Optional end-of-day close** (`end_of_day_close_enabled`, default
  **false**): position closing at end of day is opt-in metadata only and is
  disabled unless the operator explicitly enables it.

## 4. Protective stop coverage

Coordinated by `mike_trade/risk/protection.py` (`ProtectionCoordinator`)
under policy from `mike_trade/risk/stop_policy.py`, state in
`mike_trade/risk/stop_state.py`, `trailing_state.py`, `trailing.py`,
`fixed_stop.py`. Capability-gated: unknown combinations fail closed and
LIVE execution additionally requires PAPER-demonstrated combinations (see
`docs/ALPACA.md` §5).

- **Modes** (`ProtectionMode`): `FIXED` (exactly one fixed distance, one
  fixed reference — default `CONFIRMED_FILL_PRICE`), `TRAILING_NATIVE`
  (broker-native trailing stop), `TRAILING_STEPPED` (engine-stepped
  trailing stop re-anchored through replace operations). `FIXED` rejects
  trailing distances; `TRAILING_NATIVE` rejects fixed distances.
- **Post-fill start:** protective coverage is placed only after a confirmed
  entry fill, anchored to the confirmed average fill price (or the
  configured fixed reference). There is no protective order before there
  is a confirmed position.
- **Remaining-quantity coverage:** stops cover the remaining confirmed
  quantity; fills update coverage and partial-fill states reconcile to the
  residual. Sell quantities are checked against the authoritative
  `known_position_qty` — oversells are rejected (`OversellRejected`),
  so a stop can never promise more shares than the position holds.
- **Never downward:** trailing coverage ratchets in the favorable direction
  only. A trailing stop is never moved to a worse (lower, for a long sale)
  trigger; stepped-trailing replaces move the stop up or leave it in
  place, and every replacement is recorded in the trailing-stop event
  history (`make trail-history`).
- **Offline survival and its limits:** broker-held (native) stops survive
  an engine stop, crash, or restart — a normal stop never cancels them
  (§7). Limits: `DAY` time-in-force expires and leaves an overnight
  residual unless protection is renewed (`_RISK_NOTES` in
  `mike_trade/risk/stop_policy.py`); `GTC` persists per broker rules.
  After any stream disconnect/gap, REST reconciliation is required before
  the stream is trusted again. While the engine is offline, `close`,
  `close-all`, and `flatten` are refused (exit `4`) rather than queued
  blindly.
- **No oversell:** see remaining-quantity coverage above; the order service
  additionally requires fractionable assets and `DAY` time-in-force for
  fractional/notional sizing.
- **Risk notes the operator must accept:** a stop trigger price is not a
  guaranteed execution price or maximum loss; a stop-limit order may not
  fill; `DAY` protection expires overnight unless renewed.
- Operator views (all exist): `make stop-status`, `make stops-audit`
  (orphans and unsupported placements), `make trail-status`,
  `make trail-history`, `make trail-policy-show`, `make trail-policy-set`
  (live confirmation on LIVE), `make trail-reconcile` (live confirmation
  on LIVE).

## 5. Fail-closed conditions

The system refuses rather than guesses. CLI exit codes are stable: `0` ok,
`2` usage, `3` refused by a safety/confirmation guard, `4` engine offline
or unreachable, `5` unhealthy or check failed.

- Unknown capability combinations resolve to `UNPROVEN` and raise
  `CapabilityUnavailable`; unknown legacy capability names fail closed
  (`False`); structurally impossible descriptors (`short` side,
  non-`simple` trailing class, notional replace) resolve to `UNSUPPORTED`
  and can never be promoted.
- Invalid strategy parameters never produce a signal (validation fails
  closed with explicit codes).
- Missing credentials raise; paper never falls back to live variables and
  vice versa; a persisted-fingerprint mismatch refuses to start (catches
  paper/live database mix-ups).
- Mutations on a non-idempotent request that time out raise
  `AlpacaUnknownOutcome`: the outcome is resolved by `client_order_id`
  lookup, never by blind re-POST.
- `make live-preflight` refuses (exit `3`) on any failed check and nothing
  is enabled or started; `make live-enable` / `make live-start` run the
  preflight first.
- `make close`, `make close-all`, `make flatten` are refused while the
  engine is offline (exit `4`).

## 6. Stop, pause, entry kill switch, close, close-all, flatten

These six operations are deliberately distinct. Confusing them is an
operational error; this table is normative.

| Operation | Command | Effect | Needs live confirmation on LIVE | Needs engine online |
| :--- | :--- | :--- | :--- | :--- |
| **Stop** (engine halt) | `make engine-stop` / CLI `stop` | Stops the engine loop only (SIGTERM drain, bounded, publishes a `STOPPED` heartbeat). Open positions stay open; broker-held protective stops stay in place; nothing is cancelled or closed. `make engine-restart` relaunches. | No (risk-reducing) | No |
| **Pause** (account-wide entry halt) | `make pause` / CLI `pause`; resume with `make resume` | Blocks **new entries** account-wide. Protection, exits, reconciliation, and fill ingestion keep running. | `pause`: no; `resume`: yes | Pause works through the durable control inbox even when degraded; executable closes still need the engine |
| **Pause-symbol** | `make pause-symbol SYMBOL=X` / `make resume-symbol SYMBOL=X` | Blocks new entries for one symbol only. | Pause: no; resume: yes | Same as pause |
| **Entry kill switch** | `make entries-disable` / CLI `entries-disable`; re-enable with `make entries-enable` | Disables entries until explicitly re-enabled. **Survives a `resume`**: resuming a pause does not clear a kill-switch disable. | Disable: no; enable: yes | Same as pause |
| **Close** (one position) | `make close SYMBOL=X` | Submits one full-remaining closing order for the managed position through the engine control channel and waits for acknowledgement. | Yes | **Yes** — refused offline (exit `4`) |
| **Close-all** | `make close-all` | Submits closing orders for **all managed positions** through the control channel and waits for acknowledgement. Not a side effect of `stop`. | Yes | **Yes** — refused offline (exit `4`) |
| **Flatten** | `make flatten` | Flattens the book **including protective handling** (cancels/replaces protection as needed so the book goes flat without orphan stops). Strongest of the three; not a side effect of `stop`. | Yes | **Yes** — refused offline (exit `4`) |

Related: `make engine-status` (engine status), `make health` (ONLINE/OFFLINE with
alerts), `make positions` (external holdings marked `UNMANAGED`),
`make reconcile` (report drifts), `make live-disable` (immediately disables
live trading; risk-reducing, no confirmation hurdle).

## 7. Shutdown semantics

- Graceful stop is `SIGTERM` (the default in
  `deploy/systemd/mike-trade-engine.service`): the engine drains
  in-process work within a bound (`TimeoutStopSec=90`) and publishes a
  `STOPPED` heartbeat.
- **Shutdown does not liquidate positions and does not cancel valid
  protective stops.** `stop` / `pause` / `entries-disable` affect new
  entries only. Broker-held stops remain broker-protected across the stop.
- Restart policy in the template units is bounded (`Restart=on-failure`
  with burst limits) so a repeatedly crashing engine does not hot-loop.
- The dashboard is independent of engine lifecycle: it stays up when the
  engine fails or is stopped and reports engine `OFFLINE` from heartbeat
  age (see `docs/DASHBOARD.md` §4).

## 8. Startup reconciliation and UNMANAGED positions

- On startup the engine binds its database identity (environment +
  `database_id`), takes the single-writer lock (a second writer gets
  `WriterLockHeldError`), applies pending migrations, replays durable
  control/eligibility/protection state, and reconciles local state against
  the broker: open orders, fills/activities since last seen, and position
  quantities. Unresolved drifts and unknown order states raise alerts;
  `make reconcile` reports drifts and `make health` exits `5` with
  remediation commands while critical issues persist.
- **UNMANAGED external positions:** broker holdings with no corresponding
  managed cycle are reported as `UNMANAGED` (never silently adopted or
  closed). `make positions` marks them; the reconciler lists them as
  `UNMANAGED_HOLDING` drifts. Taking responsibility for one requires an
  explicit operator action through the control vocabulary (`ADOPT_POSITION`
  path), never automatic adoption.

## 9. Live confirmation and credentials

- Any mutating command scoped to a LIVE account requires explicit
  confirmation: `--confirm-live YES` on the CLI or `CONFIRM_LIVE=YES` in
  the environment. `make` only forwards the environment value and never
  enables it by default. Without it the command is refused (exit `3`).
- Risk-reducing halt commands are exempt so the operator can always
  de-risk without friction: `make engine-stop`, `make pause`,
  `make pause-symbol`, `make entries-disable`, `make live-disable`.
  Everything that widens what the system may do needs confirmation on
  LIVE, including `make close`, `make close-all`, `make flatten`,
  `make resume`, `make resume-symbol`, `make entries-enable`,
  configuration changes (`make strategy-enable`, `make strategy-disable`,
  `make strategy-set`, `make symbols-add`, `make symbols-remove`,
  `make symbols-enable`, `make symbols-disable`), `make live-enable`,
  `make live-start`, `make trail-policy-set`, `make trail-reconcile`, and
  `make entry-limits-reset`.
- LIVE broker clients additionally require `live_authorized=True` at
  construction and LIVE order flow requires an explicit operator-recorded
  capability entry (see `docs/ALPACA.md` §5–§6).
- **Credential storage (as implemented):**
  - Broker credentials are read from environment variables only, never
    from YAML: paper from `ALPACA_PAPER_KEY_ID` /
    `ALPACA_PAPER_SECRET_KEY`, live from `ALPACA_LIVE_KEY_ID` /
    `ALPACA_LIVE_SECRET_KEY`, with no cross-environment fallback
    (`mike_trade/brokers/alpaca/auth.py`). Secrets travel only in the
    `APCA-API-SECRET-KEY` header, are never logged (only the redacted
    header mapping is log-safe), and `repr` exposes environment and
    fingerprint — never the secret.
  - In development the variables live in the operator's normal repo `.env`
    (gitignored; copy `.env.example` to `.env` and fill in locally; never
    commit a populated `.env`). Keep the file owned by the operator
    account with mode `0600` (`chmod 600 .env`) and keep paper and live
    keys strictly separated per host: shadow/dev hosts carry data
    credentials only; paper credentials only on the paper host; live
    credentials only on the live host.
  - The `deploy/systemd/*.service` files are **optional future-use
    templates only** (deployment artifacts from t.1): they are never
    copied to `/etc/systemd/system`, never enabled, never started by this
    repository. When a future operator-approved deployment uses them, the
    `EnvironmentFile=` points at the operator's normal `.env` for that
    checkout (owned by the existing operator account, mode `0600`); the
    templates deliberately create no new logins, no separate secret
    stores, and embed no credentials. Per-mode separation is by distinct
    variable names and distinct state, never by sharing one key across
    environments.
  - `make doctor` reports credential presence without printing secrets;
    `make config-show` prints the effective configuration with secrets
    redacted; JSON CLI output redacts secret keys and renders Decimals as
    strings.

## 10. State directories, backup/restore, integrity checks

- **State directory permissions:** local state lives under `state/`
  (gitignored) by convention, overridable with `MIKE_TRADE_DATA_DIR`
  (plus `MIKE_TRADE_CONFIG`, `MIKE_TRADE_MODE_CONFIG` for config
  selection). Directories and database files are owned by the operator
  account with no group/other access (mode `0700` for directories,
  `0600` for database and env files). Exactly one engine writer per
  database (`<db>.writer.lock`); the dashboard and CLI reads never take
  the writer lock.
- **Backup:** `make db-backup OUT=<path>` (or `VALUE=<path>`) creates an
  online backup with a JSON manifest sidecar (`<backup>.manifest.json`)
  recording integrity state, migration version, environment, database id,
  and scope. Run it before upgrades, host moves, and any live change.
- **Verify without restoring:** `make db-restore-dry-run OUT=<backup>`
  verifies the backup and its manifest with zero writes
  (`rehearse_restore` / `verify_backup` in
  `mike_trade/persistence/maintenance.py`). A backup that fails
  verification must not be restored (`BackupVerificationError`).
- **Integrity check:** `make db-check` asserts WAL mode, migration state
  (applied vs. pending, checksum mismatches), and database integrity, and
  reports through `IntegrityReport` / `migration_status`. `make health`
  and `make dashboard-health` add heartbeat/feed/reconciliation views.
- **Restore rules (fail-closed):** restores refuse cross-environment and
  cross-`database_id` overwrites — a paper or shadow backup can never
  overwrite a live ledger and vice versa. Pre-restore preservation moves
  the live file plus `-wal`/`-shm` sidecars to
  `<name>.pre-restore-<UTC-stamp>`; the writer-lock file is never moved;
  the restored file is installed atomically (temp file plus rename) and
  only while holding the writer lock.
- **Maintenance:** checkpoint / optimize paths also prove single-writer
  ownership and are refused without the lock. No maintenance function
  deletes or prunes append-only audit rows.

## 11. Incident runbook

1. **Stop new risk first:** `make pause` (or `make pause-symbol SYMBOL=X`,
   or the kill switch `make entries-disable`). These are risk-reducing and
   need no live confirmation.
2. **Assess:** `make health`, `make engine-status`, `make positions`,
   `make orders`, `make reconcile`, `make stops-audit`,
   `make trail-status`. `make health` exits `5` with per-issue remediation
   commands while anything is critical.
3. **Protective coverage:** if stops are orphaned or unsupported, use
   `make stops-audit` and `make trail-reconcile` (the latter needs live
   confirmation on LIVE). Never hand-edit the database.
4. **Reduce exposure deliberately:** `make close SYMBOL=X`,
   `make close-all`, or `make flatten` — each requires the engine online
   and live confirmation on LIVE. If the engine is offline (exit `4`),
   restart first (`make engine-restart`, then `make health`) so closes go through
   the reconciled control channel rather than around it.
5. **Kill live trading if needed:** `make live-disable` (immediate,
   no confirmation hurdle).
6. **Preserve evidence:** `make db-backup OUT=<path>`, plus bounded
   histories `make signals`, `make evaluations`, `make events`,
   `make app-logs` with `SYMBOL`/`SINCE`/`UNTIL`/`LIMIT`/`RUN_ID` filters, and
   trade reports (`make report`, `make report-daily`, `make report-symbol`,
   `make report-strategy`, `make report-pairs`).
7. **Recover:** `make db-check`, `make db-restore-dry-run` before any
   restore; `make doctor` before resuming; `make resume` /
   `make entries-enable` only after the cause is understood (both need
   live confirmation on LIVE).
8. **Report:** record what happened, which commands were run, and the
   backup path. Never claim the incident "proved" anything about future
   performance.
