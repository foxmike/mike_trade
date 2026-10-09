# mike_trade Operator Runbook

This runbook documents every `make` target in the project `Makefile`.
Each target is a thin wrapper around the operator CLI
(`.venv/bin/python -m mike_trade.cli <subcommand>`); Make contains no
trading logic. Safety is enforced by the CLI itself, not by Make.

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

Every CLI command returns a stable exit code, which Make propagates
unchanged:

| Code | Meaning |
| ---- | ------- |
| `0`  | Ok |
| `2`  | Usage error (bad arguments or filters) |
| `3`  | Refused by a safety or confirmation guard |
| `4`  | Engine offline or unreachable for an action that needs it |
| `5`  | Unhealthy or check failed |

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
| `make start` | `ACCOUNT` `SCENARIO` | `make start ACCOUNT=PAPER:main` |
| `make stop` | `ACCOUNT` `SCENARIO` | `make stop ACCOUNT=PAPER:main` |
| `make restart` | `ACCOUNT` `SCENARIO` | `make restart ACCOUNT=PAPER:main` |
| `make status` | `ACCOUNT` `SCENARIO` `FORMAT` | `make status ACCOUNT=PAPER:main` |

### stop vs. flatten vs. close-all

These three commands are deliberately distinct:

- `make stop` stops the engine loop only. Open positions and protective
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
without friction: `make stop`, `make pause`, `make pause-symbol`,
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
make stop ACCOUNT=LIVE:main          # exempt: always allowed
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
engine back (`make start`), confirm health (`make health`), then reissue the
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
treated as unsafe and new entries for them are blocked; use `make status`,
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
| `make logs` | `ACCOUNT` `SCENARIO` `SINCE` `UNTIL` `LIMIT` `RUN_ID` `FORMAT` | `make logs LIMIT=50` |

History commands are bounded and read-only: results are capped (default 50
rows, hard maximum 1000, with clamping reported), timestamps accept ISO-8601
or New York session dates, output is redacted, and the database is opened
read-only. `make logs` reads a bounded window of structured log files and
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

## Database backup and restore

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
