# mike_trade Operator Runbook

This runbook documents the operational `make` targets. Compose targets manage
the containerized runtime; application management and query targets wrap the
operator CLI (`.venv/bin/python -m mike_trade.cli <subcommand>`). Make contains
no trading logic. Safety is enforced by the CLI itself, not by Make.

Conventions used below:

- `ACCOUNT` selects exactly one account scope (for example
  `ACCOUNT=SHADOW:default`, `ACCOUNT=PAPER:main`, `ACCOUNT=LIVE:main`).
  `SCENARIO` optionally selects a shadow scenario.
- `FORMAT` is `table` (default) or `json`. JSON output redacts secrets and
  renders Decimal values as strings.
- `SYMBOL`, `STRATEGY`, `KEY`, `VALUE`, `SINCE`, `UNTIL`, `LIMIT`, `OUT`,
  and `RUN_ID` are command-specific pass-through values, forwarded only as
  CLI arguments.
- `CONFIRM_LIVE` is forwarded from the operator environment only. It is
  empty by default and Make never enables it for you (see
  [Live confirmation](#live-confirmation)).
- Run `make help` (the default goal) to list every target with its one-line
  description.

## CLI exit codes

CLI commands return stable exit codes, which the corresponding Make targets
propagate unchanged:

| Code | Meaning |
| ---- | ------- |
| `0`  | Ok |
| `2`  | Usage error (bad arguments or filters) |
| `3`  | Refused by a safety or confirmation guard |
| `4`  | Engine offline or unreachable for an action that needs it |
| `5`  | Unhealthy or check failed |

## Docker Compose lifecycle

Docker Compose is the primary runtime interface. Run `make up` to start
services without rebuilding, `make start` to start existing stopped
containers, `make stop` to stop while retaining containers and data, and
`make restart` to restart services. `make down` removes containers and the
network but preserves persistent data. Use `make build` to build images or
`make rebuild` to build and recreate the services. `make status`, `make logs`,
and `make ps` show service status, follow logs, and show container processes.
These targets do not configure host boot startup or scheduling. See
[`DOCKER.md`](DOCKER.md) for the stack details.

## Setup and configuration

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make install` | — | `make install` |
| `make update` | — | `make update` |
| `make doctor` | `ACCOUNT` `SCENARIO` `FORMAT` | `make doctor` |
| `make config-show` | `ACCOUNT` `SCENARIO` `FORMAT` | `make config-show ACCOUNT=PAPER:main` |

- `make install` runs `uv sync --frozen` against the lock file and then the
  CLI `doctor` check. Run it first after a clean clone.
- `make update` refreshes the lock file (`uv lock --upgrade`) and
  resynchronizes the environment. Nothing else in the Makefile touches
  dependencies.
- `make doctor` performs read-only environment checks (Python version,
  required packages, git-ignored state directory, safe default mode) and
  reports credential presence without printing secrets.
- `make config-show` prints the effective configuration for the chosen
  account scope.

## Providers and market data

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make providers` | `ACCOUNT` `FORMAT` | `make providers` |
| `make provider-check` | `ACCOUNT` `FORMAT` | `make provider-check ACCOUNT=PAPER:main` |
| `make broker-check` | `ACCOUNT` `FORMAT` | `make broker-check ACCOUNT=PAPER:main` |
| `make data-check` | `ACCOUNT` `SCENARIO` `FORMAT` | `make data-check ACCOUNT=SHADOW:default` |

These are read-only checks. A failing `-check` exits non-zero (`5`) so it
can gate further operator steps.

## Strategies

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make strategies` | `ACCOUNT` `FORMAT` | `make strategies` |
| `make strategy-info` | `ACCOUNT` `STRATEGY` `FORMAT` | `make strategy-info STRATEGY=vwap` |
| `make strategy-check` | `ACCOUNT` `STRATEGY` `FORMAT` | `make strategy-check STRATEGY=vwap` |
| `make strategy-enable` | `ACCOUNT` `STRATEGY` | `make strategy-enable STRATEGY=vwap ACCOUNT=PAPER:main` |
| `make strategy-disable` | `ACCOUNT` `STRATEGY` | `make strategy-disable STRATEGY=vwap ACCOUNT=PAPER:main` |
| `make strategy-set` | `ACCOUNT` `STRATEGY` (or `KEY`) `VALUE` | `make strategy-set STRATEGY=vwap VALUE='{"max_positions": 3}'` |

`strategy-enable`, `strategy-disable`, and `strategy-set` mutate
configuration and need live confirmation on LIVE accounts.

### Model-driven strategies

`xgboost_probability_buy|sell` and `lightgbm_probability_buy|sell` consume models trained with
`make model-train` (see `docs/MODEL_TRAINING_ARCHITECTURE.md`). Each strategy entry pins an exact
`model_id` and `model_version` (never `latest`) in its `parameters`; `service.model_root` names the
read-only repository (default `research/models`). Startup refuses a missing, corrupt, wrong-algorithm,
wrong-role or wrong-feed artifact. Training never changes a running deployment. Configuration, the
position features SELL models read, audit records and the checklist are in `docs/MODEL_STRATEGIES.md`;
a validated SHADOW example is `config/model_strategies.example.yaml`. Research experiments take
`model_root` (same-feed reference series such as SPY are loaded automatically). The market-status
monitor, protective stops and entry controls apply to these strategies unchanged.

The end-to-end operator procedure (prepare, train, report, walk-forward, campaign backtests, approval,
SHADOW, `make model-check`, `make model-health`) is `docs/MODEL_OPERATOR_GUIDE.md`. The engine and API
containers mount `MIKE_TRADE_MODEL_DIR` (default `./research/models`) read-only; training happens
offline with the CLI and never inside the engine container. The image includes `libgomp1` for both
libraries. Every mode requires an operator approval record for the pinned artifact; PAPER/LIVE
authorization is separate and unchanged.

## Short selling

Short selling is **opt-in and fail-closed** and is never activated for LIVE
(`docs/SAFETY_AND_RISK.md` §4a). Roles: SHORT opens a short, COVER closes it;
SELL still closes only a long. There is no automatic reversal.

**SHADOW.** Set `execution.short_selling_enabled: true` and list
`short_strategies` / `cover_strategies` per symbol (strategies declared with
`side: short` / `side: cover`; see `config/mike_trade.example.yaml`).
Configuration refuses short strategies without the switch and refuses the
switch for `account_mode: live`. A new SHADOW scenario account fixes
`short_margin_multiplier` (default 1.5: proceeds stay locked and 0.5 x notional
of own cash is required) and `short_borrow_bps_per_day` (default 0: an
*estimated* borrow assumption, charged on cover) at creation; an existing
scenario keeps its creation parameters. A short's proceeds are restricted
collateral (excluded from buying power), equity subtracts the short's
liability, and exposure ceilings and the session entry cap count shorts like
longs. Enabling shorting changes the config fingerprint (the same
acknowledgement as any strategy drift).

**BACKTEST / research.** `ExperimentSpec` takes `short_strategies`,
`cover_strategies`, `short_selling_enabled` (required for any short strategy)
and `short_borrow_bps_per_day`; `BacktestRequest.config` accepts those keys
plus `short_margin_multiplier` (default 1.5) and an optional
`max_gross_exposure` ceiling that applies to long and short entries alike.
Runs that include SHORT/COVER strategies
use lifecycle semantics `"3"` (long-only runs keep `"2"` and their hashes).
Artifacts add, for shorts only: fill `direction` and `borrow_cost`, cycle
`direction`, equity-curve `restricted_collateral` / `available_cash`, and
`ENTRY_REFUSED` decisions (`SHORT_SELLING_NOT_AUTHORIZED`,
`INSUFFICIENT_SHORT_MARGIN`, `GROSS_EXPOSURE_LIMIT`). The trade ledger pairs
`SELL_SHORT` with `BUY_TO_COVER` (`direction: SHORT`, gain = entry - cover).

**PAPER / LIVE.** The order, position, reconciliation and protection layers are
direction-aware and fail closed: a short entry needs the explicit switch, a
broker account that may short with buying power, a shortable and easy-to-borrow
asset, a recorded capability verification of the short order shapes, and
broker-held short-side protection; otherwise nothing is sent. PAPER/LIVE now evaluate
BUY/SELL strategies (see ALPACA.md §8) but are long-only: strategy shorts are advance-only,
and no paper or live short order flow has been verified.
Reconciliation reports `POSITION_DIRECTION_MISMATCH` when the broker holds the
opposite direction of a managed cycle and never nets or adopts it.

## Symbols

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make symbols` | `ACCOUNT` `FORMAT` | `make symbols` |
| `make symbols-add` | `ACCOUNT` `SYMBOL` `VALUE` | `make symbols-add SYMBOL=AAPL ACCOUNT=PAPER:main` |
| `make symbols-remove` | `ACCOUNT` `SYMBOL` `VALUE` | `make symbols-remove SYMBOL=AAPL ACCOUNT=PAPER:main` |
| `make symbols-enable` | `ACCOUNT` `SYMBOL` `VALUE` | `make symbols-enable SYMBOL=AAPL ACCOUNT=PAPER:main` |
| `make symbols-disable` | `ACCOUNT` `SYMBOL` | `make symbols-disable SYMBOL=AAPL ACCOUNT=PAPER:main` |

The `symbols-*` mutations need live confirmation on LIVE accounts.

## Engine lifecycle

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make engine-start` | `ACCOUNT` `SCENARIO` | `make engine-start ACCOUNT=PAPER:main` |
| `make engine-stop` | `ACCOUNT` `SCENARIO` | `make engine-stop ACCOUNT=PAPER:main` |
| `make engine-restart` | `ACCOUNT` `SCENARIO` | `make engine-restart ACCOUNT=PAPER:main` |
| `make engine-status` | `ACCOUNT` `SCENARIO` `FORMAT` | `make engine-status ACCOUNT=PAPER:main` |

### stop vs. flatten vs. close-all

These three commands are deliberately distinct:

- `make engine-stop` stops the engine loop only. Open positions and protective
  stops are left in place: anything already resting at the broker stays
  broker-protected, and nothing is cancelled or closed by `stop`.
- `make close-all` submits closing orders for all managed positions through
  the engine control channel and waits for acknowledgement.
- `make flatten` flattens the book, including protective handling, through
  the engine control channel and waits for acknowledgement. It is the
  strongest of the three: use it when the whole book must go flat, not when
  you only want the engine loop to halt.

`close-all` and `flatten` never run as a side effect of `stop`. If you need
both effects, run both targets explicitly.

## Dashboard and health

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make dashboard` | `ACCOUNT` `FORMAT` | `make dashboard` |
| `make dashboard-start` | `ACCOUNT` | `make dashboard-start` |
| `make dashboard-stop` | `ACCOUNT` | `make dashboard-stop` |
| `make dashboard-health` | `ACCOUNT` `FORMAT` | `make dashboard-health` |
| `make health` | `ACCOUNT` `SCENARIO` `FORMAT` | `make health ACCOUNT=PAPER:main` |

### Health and OFFLINE semantics

`make health` computes engine health from heartbeat age, feed age, and open
alert conditions. The engine is reported `ONLINE` only when the heartbeat is
fresh (stale beyond the offline threshold, 60 seconds by default, means
`OFFLINE`); otherwise it reports `OFFLINE` and exits `5`, listing each issue
with a remediation command. A stale feed, unresolved reconciliation drifts,
unknown order states, or live positions held while offline raise alerts or
critical issues. `make dashboard-health` applies the same idea to the
dashboard service.

## Shadow mode

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make shadow-start` | `ACCOUNT` `SCENARIO` | `make shadow-start ACCOUNT=SHADOW:default SCENARIO=reference_vwap` |
| `make shadow-stop` | `ACCOUNT` `SCENARIO` | `make shadow-stop ACCOUNT=SHADOW:default` |
| `make shadow-list` | `ACCOUNT` `FORMAT` | `make shadow-list` |
| `make shadow-create` | `ACCOUNT` `VALUE` | `make shadow-create VALUE=my_scenario` |
| `make shadow-reset` | `ACCOUNT` `SCENARIO` | `make shadow-reset ACCOUNT=SHADOW:default SCENARIO=my_scenario` |

Shadow runs simulate fills and fees (labeled as estimates) without touching
a broker.

## Paper mode

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make paper-start` | `ACCOUNT` | `make paper-start ACCOUNT=PAPER:main` |
| `make paper-stop` | `ACCOUNT` | `make paper-stop ACCOUNT=PAPER:main` |
| `make paper-status` | `ACCOUNT` `FORMAT` | `make paper-status ACCOUNT=PAPER:main` |

Paper mode routes orders to the broker paper endpoint. It uses paper
credentials only; paper operations never touch live credentials.

## Live mode

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make live-preflight` | `ACCOUNT` | `make live-preflight ACCOUNT=LIVE:main` |
| `make live-enable` | `ACCOUNT` | `CONFIRM_LIVE=YES make live-enable ACCOUNT=LIVE:main` |
| `make live-start` | `ACCOUNT` | `CONFIRM_LIVE=YES make live-start ACCOUNT=LIVE:main` |
| `make live-disable` | `ACCOUNT` | `make live-disable ACCOUNT=LIVE:main` |

`live-enable` and `live-start` first run the live preflight; if preflight
fails the operation is refused (exit `3`) and nothing is enabled or started.

### Live confirmation

Any mutating command scoped to a LIVE account requires explicit confirmation:
`--confirm-live YES` on the CLI, or `CONFIRM_LIVE=YES` in the environment.
Make only forwards the environment value; the default is empty, so a LIVE
mutation without explicit confirmation is refused with exit `3`. Confirmation
is enforced by the CLI, never by Make.

Risk-reducing halt commands are exempt so the operator can always de-risk
without friction: `make engine-stop`, `make pause`, `make pause-symbol`,
`make entries-disable`, and `make live-disable` need no confirmation even on
LIVE. Everything else that changes LIVE state still requires it, including
the order commands (`make close`, `make close-all`, `make flatten`),
re-enabling entries (`make resume`, `make resume-symbol`,
`make entries-enable`), configuration changes (`make strategy-enable`,
`make strategy-disable`, `make strategy-set`, `make symbols-add`,
`make symbols-remove`, `make symbols-enable`, `make symbols-disable`),
`make live-enable`, `make live-start`, `make trail-policy-set`,
`make trail-reconcile`, and `make entry-limits-reset`.

Example (LIVE):

```sh
CONFIRM_LIVE=YES make close SYMBOL=AAPL ACCOUNT=LIVE:main
make engine-stop ACCOUNT=LIVE:main   # exempt: always allowed
```

## Entry controls: pause vs. entries-disable

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make pause` | `ACCOUNT` `SCENARIO` | `make pause ACCOUNT=PAPER:main` |
| `make resume` | `ACCOUNT` `SCENARIO` | `make resume ACCOUNT=PAPER:main` |
| `make pause-symbol` | `ACCOUNT` `SYMBOL` | `make pause-symbol SYMBOL=AAPL ACCOUNT=PAPER:main` |
| `make resume-symbol` | `ACCOUNT` `SYMBOL` | `make resume-symbol SYMBOL=AAPL ACCOUNT=PAPER:main` |
| `make entries-disable` | `ACCOUNT` `SCENARIO` | `make entries-disable ACCOUNT=PAPER:main` |
| `make entries-enable` | `ACCOUNT` `SCENARIO` | `make entries-enable ACCOUNT=PAPER:main` |

- `make pause` (and `make pause-symbol`) is the temporary, reversible halt:
  it blocks new entries account-wide (or for one symbol) while leaving every
  protective and housekeeping function running. Fill handling, fixed stops,
  trailing management, and reconciliation continue while paused.
- `make entries-disable` is the stronger, sticky block: entries stay blocked
  until `make entries-enable` runs. Resuming from a pause does not clear an
  entries-disable.
- Both affect eligibility of new entries only; neither closes positions nor
  cancels protective stops. Re-enabling entries on LIVE needs confirmation;
  pausing and disabling never do.

## Order actions and offline refusal

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make close` | `ACCOUNT` `SYMBOL` | `make close SYMBOL=AAPL ACCOUNT=PAPER:main` |
| `make close-all` | `ACCOUNT` | `make close-all ACCOUNT=PAPER:main` |
| `make flatten` | `ACCOUNT` | `make flatten ACCOUNT=PAPER:main` |

`close`, `close-all`, and `flatten` go through the engine control channel,
which requires a fresh engine heartbeat for the selected account. If the
engine is offline or unreachable, the command is refused with exit `4` and
nothing is queued: no future intent is stored for later submission. Bring the
engine back (`make engine-start`), confirm health (`make health`), then reissue the
command explicitly.

## Protection: trailing stops, entry limits, fixed stops

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make trail-status` | `ACCOUNT` `FORMAT` | `make trail-status ACCOUNT=PAPER:main` |
| `make trail-history` | `ACCOUNT` `FORMAT` | `make trail-history ACCOUNT=PAPER:main` |
| `make trail-policy-show` | `ACCOUNT` `FORMAT` | `make trail-policy-show ACCOUNT=PAPER:main` |
| `make trail-policy-set` | `ACCOUNT` `KEY` (or `VALUE`) | `make trail-policy-set KEY=stepped VALUE=2 ACCOUNT=PAPER:main` |
| `make trail-reconcile` | `ACCOUNT` | `make trail-reconcile ACCOUNT=PAPER:main` |
| `make entry-limits` | `ACCOUNT` `FORMAT` | `make entry-limits ACCOUNT=PAPER:main` |
| `make entry-limits-reset` | `ACCOUNT` | `make entry-limits-reset ACCOUNT=PAPER:main` |
| `make cooldown-status` | `ACCOUNT` `FORMAT` | `make cooldown-status ACCOUNT=PAPER:main` |
| `make stop-status` | `ACCOUNT` `FORMAT` | `make stop-status ACCOUNT=PAPER:main` |
| `make stops-audit` | `ACCOUNT` `FORMAT` | `make stops-audit ACCOUNT=PAPER:main` |

`trail-policy-set`, `trail-reconcile`, and `entry-limits-reset` need live
confirmation on LIVE. `stops-audit` surfaces orphan stops, unsupported
placements, and expired stops for follow-up.

## Portfolio reads and reconciliation

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make positions` | `ACCOUNT` `FORMAT` | `make positions ACCOUNT=PAPER:main` |
| `make orders` | `ACCOUNT` `FORMAT` | `make orders ACCOUNT=PAPER:main` |
| `make balance` | `ACCOUNT` `FORMAT` | `make balance ACCOUNT=PAPER:main` |
| `make budget` | `ACCOUNT` `FORMAT` | `make budget ACCOUNT=PAPER:main` |
| `make reconcile` | `ACCOUNT` `FORMAT` | `make reconcile ACCOUNT=PAPER:main` |

### Startup reconciliation and entry blocking

The engine reconciles local state against the broker on startup and on
demand via `make reconcile`, reporting drifts that need operator attention.
While reconciliation (or an error state) is unresolved, affected cycles are
treated as unsafe and new entries for them are blocked; use `make engine-status`,
`make reconcile`, and `make entry-limits` to observe, and `make health` to
confirm the engine is healthy before re-enabling entries.

### UNMANAGED external positions

Positions that exist at the broker but belong to no managed cycle are tracked
as UNMANAGED external holdings. They appear separately from managed
positions, are excluded from managed budget commitments (broker buying power
already reflects them), and sell-side guards refuse to manage them. Handle
external holdings directly at the broker; the engine will not adopt, close,
or protect them on its own.

## History and logs

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make signals` | `ACCOUNT` `SCENARIO` `SYMBOL` `STRATEGY` `SINCE` `UNTIL` `LIMIT` `RUN_ID` `FORMAT` | `make signals ACCOUNT=SHADOW:default SYMBOL=AAPL LIMIT=20` |
| `make evaluations` | `ACCOUNT` `SCENARIO` `SYMBOL` `STRATEGY` `SINCE` `UNTIL` `LIMIT` `RUN_ID` `FORMAT` | `make evaluations ACCOUNT=SHADOW:default LIMIT=20` |
| `make events` | `ACCOUNT` `SCENARIO` `SINCE` `UNTIL` `LIMIT` `RUN_ID` `FORMAT` | `make events ACCOUNT=SHADOW:default LIMIT=20` |
| `make app-logs` | `ACCOUNT` `SCENARIO` `SINCE` `UNTIL` `LIMIT` `RUN_ID` `FORMAT` | `make app-logs LIMIT=50` |

History commands are bounded and read-only: results are capped (default 50
rows, hard maximum 1000, with clamping reported), timestamps accept ISO-8601
or New York session dates, output is redacted, and the database is opened
read-only. `make app-logs` reads a bounded window of structured log files and
reports when the window is truncated.

## Reports and exports

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make report` | `ACCOUNT` `SCENARIO` `FORMAT` | `make report ACCOUNT=PAPER:main` |
| `make report-daily` | `ACCOUNT` `SCENARIO` `FORMAT` | `make report-daily ACCOUNT=PAPER:main` |
| `make report-symbol` | `ACCOUNT` `SYMBOL` `FORMAT` | `make report-symbol SYMBOL=AAPL ACCOUNT=PAPER:main` |
| `make report-strategy` | `ACCOUNT` `STRATEGY` `FORMAT` | `make report-strategy STRATEGY=vwap ACCOUNT=PAPER:main` |
| `make report-pairs` | `ACCOUNT` `SCENARIO` `FORMAT` | `make report-pairs ACCOUNT=PAPER:main` |
| `make report-shadow` | `ACCOUNT` `SCENARIO` `FORMAT` | `make report-shadow ACCOUNT=SHADOW:default` |
| `make report-comparison` | `ACCOUNT` `SCENARIO` `FORMAT` | `make report-comparison ACCOUNT=PAPER:main` |
| `make report-export` | `ACCOUNT` `VALUE` `OUT` `FORMAT` | `make report-export OUT=/tmp/cycles.json ACCOUNT=PAPER:main` |

### Report and export formats

Reports render as `table` (default) or `json` via `FORMAT`. `make
report-export` writes versioned JSON (`VALUE` selects the dataset,
`OUT=path` selects the destination file) with per-cycle economics including
`fee_confirmed`, `fee_estimated`, `fee_status`, `fees_complete`, gross P&L,
and net P&L. `make report-comparison` requires two explicitly scoped
accounts.

### Reconciliation identity conventions

Reports and reconciliation join on stable identities, never on display
names:

- `account_scope_id` scopes every record to exactly one
  environment/account/scenario; ambiguous scopes are rejected rather than
  guessed.
- `client_order_id` is the idempotency key linking intents, orders, fills,
  and fees across the journal, the broker, and the reports.
- The database carries an identity row binding it to one environment; opening
  it under a mismatched environment is refused.
- Reconciliation runs and drifts are recorded per scope; `make reconcile`
  and `make health` surface unresolved drifts.

### Fee policy

Fees follow provenance, and provenance only:

- Confirmed fees on PAPER/LIVE come exclusively from broker-imported activity
  records. Activities that name no order are reported separately as
  unattributed confirmed fees.
- Orders with no linked broker activity receive labeled estimates
  (`ESTIMATED`) computed from the configured fee schedule.
- An order with neither confirmed nor estimated fees is flagged
  `fees_complete=false` (with the order listed as fee-unavailable) and
  contributes no fee to net P&L rather than a silently guessed zero.
- A fill row's own fee column has no provenance and is ignored.
- SHADOW fills and fees are simulated and always labeled `ESTIMATED`.
- Fees are never hand-loaded from files: there is no fee-file import path.
  Broker imports and labeled schedule estimates are the only sources.

## Backtests and cache

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make backtest` | `VALUE` | `make backtest VALUE=manifests/vwap.json` |
| `make backtest-status` | `FORMAT` | `make backtest-status` |
| `make backtest-report` | `STRATEGY` `VALUE` `FORMAT` | `make backtest-report STRATEGY=vwap` |
| `make backtest-list` | `FORMAT` | `make backtest-list` |
| `make cache-status` | `ACCOUNT` `FORMAT` | `make cache-status` |
| `make cache-refresh` | `ACCOUNT` | `make cache-refresh` |

Backtests run from manifests or datasets referenced by `VALUE` and never
touch broker credentials.

Historical Parquet/DuckDB research uses the separate Docker Compose research
commands. `make research-build` builds only the research image. Use
`make research-six START=... END=... WORKERS=auto` for the baseline batch or
`make research-batch JOBS=research/jobs.json WORKERS=4` for a general batch.
Run `make research-sync-r730` on DEV to copy new immutable research data and
results to the R730 over SSH.
Worker selection respects CPU affinity/quota and available memory; detailed
experiment schema, progress, artifact, and reproducibility behavior is in
[Historical Research](HISTORICAL_RESEARCH.md).

## Database backup and restore

### R730 automated backups

The R730 uses the same host-cron and mounted-SAS-drive pattern as BookieMonster.
`/dev/disk/by-uuid/b4883d76-a34f-4976-a5d1-ba973de4a837` is mounted at
`/run/media/mike/SAS_DRIVE_1` from `/etc/fstab`; the backup script refuses to
write unless this exact filesystem is mounted. Cron runs as `mike`:

```cron
20 3 * * * cd /home/mike/dev/mike/stocks/python/mike_trade && .venv/bin/python scripts/backup/r730_backup.py operational >> /home/mike/.local/state/mike_trade/backup.log 2>&1
20 4 * * 0 cd /home/mike/dev/mike/stocks/python/mike_trade && .venv/bin/python scripts/backup/r730_backup.py research >> /home/mike/.local/state/mike_trade/backup.log 2>&1
```

Daily operational backups use SQLite's online backup API and the existing
`verify_backup` integrity/hash check. Fourteen complete database and matching
configuration generations are retained. Configuration includes the private
`.env`, local YAML, Compose files, `/etc/fstab`, and the user's crontab; the
backup tree is mode 0700 and copies
are mode 0600. Weekly research backups contain the complete research artifact
tree and a read-only, locked DuckDB `EXPORT DATABASE` of the catalog. Each
Parquet snapshot is checked against its immutable source manifest, and the
export is imported into an in-memory database to check its table count. Eight
weekly generations are retained. Only completed generations are pruned; source
data is never pruned by this job.

Backups live outside the checkout at
`/run/media/mike/SAS_DRIVE_1/mike_trade/{operational,configuration,research}`.
The cron log and success/failure status live at
`/home/mike/.local/state/mike_trade/{backup.log,backup-status.json}`. The
dashboard mounts the backup and status directories read-only. `/api` health
verifies the matching operational backup and reports missing, old, or failed
backup status. `docker compose exec -T mike-trade-api python -m mike_trade.cli
doctor` checks that daily and weekly jobs last succeeded within their expected
windows. If the SAS disk is unavailable, the job fails nonzero and records the
failure in host status/log storage; it never falls back to the system disk.

To recover, first stop the engine and verify the selected operational backup
with `verify_backup` or `make db-restore-dry-run OUT=<backup-file>`. Preserve
the current database and its WAL/SHM files before any restore. Use the existing
`restore_backup` service with explicit `confirm_environment="SHADOW"` for the
selected verified SQLite copy. Restore `.env` and local configuration from the
matching configuration generation with mode 0600. Restore research files to
`research/` from a selected weekly generation, then use DuckDB `IMPORT DATABASE`
from that generation's `catalog-export/` into a new catalog path while no
research writer is running, or rebuild catalog metadata from the restored
immutable manifests with `research-data list`. Verify checksums and database
identity before starting the engine. Do not test restoration against the active
database.

For the SQLite part of a real recovery, after selecting a backup file:

```sh
docker compose stop mike-trade-engine mike-trade-api
export DB_BACKUP_FILE=/run/media/mike/SAS_DRIVE_1/mike_trade/operational/SELECTED.sqlite3
.venv/bin/python - <<'PY'
import os
from pathlib import Path
from mike_trade.persistence.maintenance import verify_backup, rehearse_restore, restore_backup

backup = Path(os.environ["DB_BACKUP_FILE"])
assert verify_backup(backup).ok
assert rehearse_restore(backup, Path("/tmp/mike_trade_restore_check"))["ok"]
restore_backup(backup, Path("state/r730/shadow.sqlite3"), confirm_environment="SHADOW")
PY
```

`restore_backup` preserves the previous active database and WAL/SHM sidecars
before installing the verified backup. Inspect its report and the restored
database identity before restarting the engine.

In SHADOW mode the dashboard's balance values are the isolated simulated
account's cash, equity, buying power, and portfolio value. The engine publishes
these to `equity_snapshots` at startup and at most once a minute while running.
The dashboard shows their source as `shadow:simulated`; Alpaca IEX data-feed
connectivity does not authenticate a broker trading account.

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make db-check` | `FORMAT` | `make db-check` |
| `make db-backup` | `OUT` (or `VALUE`) | `make db-backup OUT=/var/backups/mike_trade/mt.db` |
| `make db-restore-dry-run` | `OUT` (or `VALUE`) | `make db-restore-dry-run OUT=/var/backups/mike_trade/mt.db` |

- `make db-check` verifies integrity and migration state without writing.
- `make db-backup` takes an online backup and writes a JSON manifest sidecar
  (`<backup>.manifest.json`) next to it for later verification.
- `make db-restore-dry-run` verifies a backup (manifest plus integrity
  checks) without restoring anything: it performs no writes, and a backup
  that fails verification is reported as such and must not be restored.

## Host services (Ubuntu systemd)

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make service-install` | `ACCOUNT` | `sudo make service-install` |
| `make service-uninstall` | `ACCOUNT` | `sudo make service-uninstall` |
| `make service-status` | `ACCOUNT` `FORMAT` | `make service-status` |

- `make service-install` installs and enables the Ubuntu systemd units for
  the engine and dashboard so they start with the host.
- `make service-uninstall` stops, disables, and removes those units.
- `make service-status` reports unit state for the engine and dashboard.
  Installing or removing system units requires elevated privileges; status
  and all other targets do not.

## Tests, lint, and type checks

| Target | Variables | Example |
| ------ | --------- | ------- |
| `make test` | — | `make test` |
| `make test-unit` | — | `make test-unit` |
| `make test-integration` | — | `make test-integration` |
| `make test-paper` | — | `make test-paper` |
| `make lint` | — | `make lint` |
| `make typecheck` | — | `make typecheck` |

Test tiers:

- `make test` runs the bounded default suite: the `tests/unit` and
  `tests/integration` paths only.
- `make test-unit` runs `tests/unit` only; `make test-integration` runs
  `tests/integration` only.
- `make test-paper` runs the `paper`-marked tests, which need paper broker
  credentials. The suite skips them when paper credentials are absent, and
  they never use live credentials.
- `make lint` runs the configured linter (`ruff check`); `make typecheck`
  runs the configured type checker (`mypy`).

Ordinary tests never use live credentials.
