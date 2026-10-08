# Database

This document is the operator and developer contract for the `mike_trade`
SQLite persistence layer: files, isolation, pragmas, migrations, journal and
append-only semantics, numeric/timestamp encoding, and the backup / verify /
rehearsal / restore / maintenance procedures. Implementation lives in
`mike_trade/persistence/` (`schema.py`, `transaction.py`, `journal.py`,
`repositories.py`, `readstore.py`, `projections.py`, `maintenance.py`).

> Operator note: CLI and Makefile entrypoints for integrity checks, backups,
> verification, rehearsal, restore, checkpointing, and optimize are thin
> recipes that delegate to `mike_trade.persistence.maintenance` and contain
> no database business logic of their own.

## 1. One database per operational environment

There is one SQLite WAL database file per operational environment:

| Environment | Database | Ledger |
| :--- | :--- | :--- |
| `LIVE` | One live database bound to the live broker account | Broker live account plus the local live ledger mirror |
| `PAPER` | One paper database bound to the paper broker account | Broker paper account plus the local paper ledger mirror |
| `SHADOW` | One database holding multiple named scenarios (`scenario_name`) on one shared live feed, or one database per scenario by deployment convention | Persistent per-scenario simulated ledger |
| `BACKTEST` | One database per run (`run_id`), or an ephemeral run manifest | Deterministic, isolated run ledger; no broker access |

### 1.1 `db_identity` and `account_scopes` isolation rules

- `db_identity` holds exactly one row (`id = 1`): `environment`,
  `database_id` (a UUID assigned at initialization), and `created_at`. It is
  immutable: update and delete triggers abort any mutation.
- `initialize_database` binds a file permanently to one environment. Opening
  a file bound to another environment raises `DatabaseIdentityMismatchError`.
- Every trade-affecting row carries `account_scope_id`, an
  environment-qualified key (`paper:ACCT1`, `shadow:ACCT1:trend-v3`,
  `backtest:ACCT1:run-9`). Scope ids embed the environment prefix.
- `account_scopes.environment` must equal the database identity environment
  (enforced by the `account_scopes_environment_guard` trigger). Additional
  checks apply: `SHADOW` scopes require `scenario_name`, `BACKTEST` scopes
  require `run_id`, and `LIVE`/`PAPER` scopes require `broker_account_ref`.
- The write path (`WriterSession.bind_scope`) refuses scopes whose
  environment prefix differs from the database identity, and
  `ScopedWriter.require_scope` refuses entities from any other scope. Reads
  (`ReadStore.snapshot`, all `projections`) always filter by scope.
- Cross-environment and cross-`database_id` restores are refused (see
  §8.4). A `PAPER` or `SHADOW` backup can never overwrite a `LIVE` ledger,
  and vice versa.

## 2. Single-writer model and read-only projections

- Exactly one engine process owns the write path for a database.
  `WriterLock` holds an exclusive non-blocking `flock` on
  `<db_path>.writer.lock`; `WriterSession` pairs that lock with the single
  write connection. A second writer gets `WriterLockHeldError`.
- All mutation runs inside `unit_of_work()` (`BEGIN IMMEDIATE` … commit,
  otherwise rollback). Nested units of work are forbidden.
- The dashboard, reports, and CLI status reads never take the writer lock.
  They open the database through `ReadStore` (read-only URI plus
  `query_only`, an authorizer that denies writes, and a validated
  `BEGIN DEFERRED … COMMIT` snapshot per scope) and render through
  `projections.py`. The dashboard never mutates state, configuration, or
  orders; engine-to-dashboard communication is WAL reads plus a heartbeat
  row.

## 3. PRAGMA settings

Write connections (`schema.open_connection`):

- `PRAGMA foreign_keys = ON`
- `PRAGMA journal_mode = WAL` (refused if the database does not stay in WAL)
- `PRAGMA synchronous = FULL`
- `PRAGMA busy_timeout = 5000`

Read-only connections (`open_connection(..., read_only=True)`):

- Read-only URI (`mode=ro`), `PRAGMA foreign_keys = ON`,
  `PRAGMA busy_timeout = 5000`, `PRAGMA query_only = ON`.

`check_integrity` additionally asserts the live `journal_mode` reads back
`wal`; anything else fails the report.

## 4. Forward-only migrations and checksum rules

- Migrations live in `mike_trade/persistence/migrations/` as
  `NNNN_name.sql`, numbered consecutively from `0001`. Discovery rejects gaps
  and duplicates.
- `apply_migrations` first verifies every already-applied migration's
  recorded SHA-256 against the file on disk (`MigrationChecksumError` on
  tamper), rejects databases newer than the known set (`SchemaTooNewError`,
  including unknown applied versions), then applies each pending file
  atomically (`BEGIN IMMEDIATE` … insert into `schema_migrations` … commit)
  with rollback on failure.
- `schema_migrations` stores `(version, name, sha256, applied_at)`; `name`
  omits the numeric prefix and `.sql` suffix (for example `initial`).
- `migration_status` (maintenance service) reports `applied_versions`,
  `pending_versions`, `checksum_mismatches`, `current_version`, and
  `latest_version`. `apply_pending_migrations` holds the `WriterLock` while
  applying and returns the post-apply status.
- There are no destructive rewrites of trade history. A migration never
  deletes ledger rows (see §5).

## 5. Append-only tables and journal semantics

Append-only tables (each guarded by `BEFORE UPDATE` / `BEFORE DELETE`
triggers that abort): `domain_events`, `fills`, `broker_order_events`,
`broker_activities`, `cash_events`, `trailing_stop_events`,
`operator_commands`, `reconciliation_drifts`, `equity_snapshots`. No
maintenance function deletes or prunes these rows.

### 5.1 `DomainEventJournal`

- One atomic batch per call: `append` opens `BEGIN IMMEDIATE` and commits or
  rolls back all-or-nothing; `append_in_transaction` joins the caller's unit
  of work under a savepoint. A batch never spans account scopes
  (`CrossScopeJournalError`).
- Optimistic streams: `expected_versions[(stream_type, stream_id)]` must
  match the committed head or a `StreamVersionConflictError` is raised.
- Idempotency: reusing an `event_id` for byte-identical content returns the
  durable record as a duplicate; reusing it for different content raises
  `DuplicateEventIdError`. Reusing an idempotency key for different content
  raises `IdempotencyConflictError`.
- Payloads are canonical JSON (`canonical_payload_json`): sorted keys,
  `Decimal` as string, datetimes as UTC microsecond text, no floats, no
  non-finite decimals, no naive datetimes. Each row stores `payload_sha256`.
- `verify_chain(scope)` reports payload-hash mismatches and non-contiguous
  stream versions. It never repairs history; `check_integrity` surfaces its
  findings and fails `ok` when any scope reports problems.

### 5.2 Intent-before-submit

Order intents are committed (with their budget reservation and
`OrderIntentRecorded` journal event) before any broker submit callable runs.
`submit_recorded_intent` journals `OrderSubmitRequested` under an
idempotency key, then invokes the submit effect exactly once via
`commit_then_effect`: the effect never runs if persistence fails, and if the
effect raises, the durable intent remains (marked `SUBMIT_FAILED` on broker
rejection with the reservation released, or `UNKNOWN` otherwise with the
reservation kept). A `RECORDED` intent with a prior submit request is never
resubmitted blindly.

### 5.3 Exactly-once fill ingestion by external activity ID

- `Fill.fill_id` is the broker's `external_activity_id`; the `fills` row key
  is the surrogate `"<scope>:<activity id>"` with a unique
  `(account_scope_id, external_activity_id)` constraint.
- `FillRepository.apply_fill` returns `applied` or `duplicate` (already
  recorded activity ids change nothing). Each application atomically writes
  the fill, advances the broker order's ledger-derived filled quantity,
  transitions the position cycle, consumes or releases the budget
  reservation, upserts the open-position row, resizes trailing coverage, and
  emits `FillApplied`. Cumulative fill quantity can never exceed the broker
  order quantity.

## 6. Reconciliation drift and resolution tables

The broker is authoritative for live orders and positions. Local/broker
discrepancies are explicit rows, never silent rewrites:

- `reconciliation_runs`: one row per run (`STARTED`, `CLEAN`, `DRIFTED`,
  `ERROR`) with start/completion timestamps and a summary JSON blob.
- `reconciliation_drifts`: append-only drift items per run (`drift_type`,
  `entity_ref`, expected vs actual JSON).
- `reconciliation_resolutions`: one row per resolved drift (`resolution_kind`,
  details, resolver, timestamp); unresolved drifts (left-joined misses) block
  new entries and surface in `engine_status` / `reconciliation_status`.

## 7. Data-retention and encoding policies

- **No raw tick archive.** The cache keeps only the latest quotes/trades,
  decision evidence, candle inputs, completed candles, rolling indicator
  state, and freshness metadata. Full-session raw tick recording is out of
  scope.
- **Decimal-as-TEXT.** All monetary and quantity columns are `TEXT`.
  Writers use `decimal_to_db` (finite `Decimal` only, never float);
  non-finite values are rejected on both write and read.
- **UTC timestamps.** All timestamps are fixed-width microsecond UTC text
  (`%Y-%m-%dT%H:%M:%S.%fZ`, for example `2026-10-08T14:30:01.123456Z`).
  Naive or non-UTC datetimes are rejected; malformed stored text raises on
  read.

## 8. Backup, verify, rehearsal, and restore

All procedures delegate to `mike_trade.persistence.maintenance`.

### 8.1 Backup (`create_backup`)

1. Reads identity, scope ids, and schema version through a read-only
   connection.
2. Copies the live database with the `sqlite3.Connection.backup` online API
   from a read connection, so backups are safe while the engine holds the
   writer lock and keeps writing.
3. Writes `<environment>-<database_id>-<UTC stamp>.sqlite3` into the backup
   directory plus a JSON manifest sidecar `<backup>.manifest.json`
   containing `environment`, `database_id`, `account_scope_ids`,
   `schema_version`, `sha256` of the backup file, `size_bytes`, `created_at`,
   and the integrity result of the backup copy.
4. Runs `check_integrity` on the backup copy and refuses to finalize
   (removing the partial file and raising `IntegrityCheckFailedError`) when
   the backup's integrity check fails.

### 8.2 Verify (`verify_backup`)

Recomputes the file SHA-256 against the manifest and runs `integrity_check`.
Any SHA mismatch, integrity failure, unreadable manifest, or missing
manifest is a failure (`BackupVerification.ok is False` with an explanatory
`error`). Verification never mutates the backup or any live database.

### 8.3 Rehearsal (`rehearse_restore`)

Copies the backup into a scratch directory (`<stem>.rehearsal.sqlite3` plus
its manifest sidecar), then runs `check_integrity` and the identity/version
checks there. It never opens or writes the live database file; tests assert
the source database remains byte-identical. The returned report bundles the
verification, the integrity report, the migration status, the rehearsed
path, and an overall `ok` flag.

### 8.4 Restore (`restore_backup`)

1. Verifies the backup first; verification failure raises
   `BackupVerificationError` and nothing is touched.
2. Requires `confirm_environment` to equal the manifest's `environment`;
   any other value raises `RestoreRefusedError`.
3. Refuses while another process holds the target's `WriterLock`
   (`RestoreRefusedError`).
4. Refuses when the existing target's `db_identity` environment or
   `database_id` differs from the manifest — cross-environment and
   cross-identity restores are forbidden.
5. Preserves the existing target: moves the database file plus its `-wal`
   and `-shm` sidecars into a timestamped sibling directory
   `<target name>.pre-restore-<UTC stamp>/` (the writer-lock file itself is
   never moved). Nothing is deleted.
6. Installs the backup atomically (stream to a temp file in the target
   directory, `fsync`, `os.replace`) and re-runs `check_integrity`,
   raising `IntegrityCheckFailedError` if the restored copy fails.

### 8.5 Checkpoint and optimize

- `checkpoint(db_path, mode='TRUNCATE')` validates the mode (`PASSIVE`,
  `FULL`, `RESTART`, `TRUNCATE`), acquires the `WriterLock`, and runs
  `PRAGMA wal_checkpoint(mode)`. It is refused (lock error) while another
  writer holds the lock.
- `optimize(db_path)` acquires the `WriterLock` and runs `PRAGMA optimize`
  plus `ANALYZE`.

## 9. Corruption detection and response

Detection (all surfaced by `check_integrity`, whose `ok` is true only when
every check passes):

- `PRAGMA integrity_check` must return exactly `ok`.
- `PRAGMA foreign_key_check` must return zero rows.
- `journal_mode` must read back `wal`.
- Schema version must equal the highest known migration with no checksum
  mismatches.
- `DomainEventJournal.verify_chain` must report no payload-hash mismatches
  or stream-version gaps for any account scope.

Response steps for an operator (via the delegating CLI/Makefile recipes):

1. Stop the writer and quarantine the file: copy the database plus `-wal` /
   `-shm` aside before any repair attempt. Never delete ledger rows.
2. Run `check_integrity` and `migration_status` to classify the failure
   (page corruption, foreign-key violation, journal tampering, schema or
   checksum drift).
3. For journal or FK failures, treat the finding as tampering evidence:
   investigate the offending scope/stream/row, reconcile against the broker,
   and record the outcome in the reconciliation tables.
4. Restore service from the newest verified backup with `rehearse_restore`
   followed by `restore_backup`, confirming the environment explicitly and
   keeping the `.pre-restore-*` copy until reconciliation is clean.
5. Do not replay unverified edits into the ledger; re-ingest broker truth
   through the exactly-once fill/activity paths instead.
