# Release Go/No-Go Checklist (Operator)

**Scope:** operator release decision for `mike_trade`. Check each box only on
directly observed evidence (command output, file presence, reviewed report).
Any unchecked box or any `NO-GO` condition below means **NO-GO**.
Companion audit record: `docs/verification/phase_t_complete_product.md`
(which currently reports several `NOT-RUN` rows, one `FAIL` row for the missing
compiled dashboard, and `PENDING-EXTERNAL` paper acceptance — all of which must
be resolved before a GO).

## 1. Repository and dependency lock reproducibility

- [ ] `git status` is clean on the release commit; no uncommitted changes, no
  populated `.env` committed (see `docs/SAFETY_AND_RISK.md` §9).
- [ ] `uv.lock` + `pyproject.toml` present and `make install`
  (`uv sync --frozen` plus CLI `doctor`) succeeds from a clean clone
  (see `docs/TEST_PLAN.md` §1).
- [ ] `make lint` (`ruff check .`) and `make typecheck` (`mypy mike_trade`)
  pass (see `docs/TEST_PLAN.md` §1).

## 2. Migrations and DB integrity / backup / restore drill

- [ ] `make db-check` reports WAL mode, migration state clean (no pending,
  no checksum mismatch), and integrity OK
  (see `docs/SAFETY_AND_RISK.md` §10).
- [ ] `make db-backup OUT=<path>` produced a backup plus JSON manifest
  sidecar recording integrity state, migration version, environment, and
  database id (see `docs/SAFETY_AND_RISK.md` §10).
- [ ] `make db-restore-dry-run OUT=<backup>` verified the backup with zero
  writes before any restore (see `docs/SAFETY_AND_RISK.md` §10).
- [ ] Restore rules confirmed understood: no cross-environment or
  cross-`database_id` overwrites; pre-restore preservation and atomic install
  while holding the writer lock (see `docs/SAFETY_AND_RISK.md` §10).

## 3. Credential env files and permissions per mode

- [ ] `.env` (or equivalent environment) holds keys for **this host's mode
  only**: data credentials on dev/shadow hosts, paper keys only on the paper
  host, live keys only on the live host; `ALPACA_PAPER_*` and `ALPACA_LIVE_*`
  never share a host (see `docs/ALPACA.md` §4, `docs/SAFETY_AND_RISK.md` §9).
- [ ] `.env` owned by the operator account with mode `0600`
  (`chmod 600 .env`); state directories `0700`, database files `0600`
  (see `docs/ALPACA.md` §4, `docs/SAFETY_AND_RISK.md` §10).
- [ ] `make doctor` reports credential presence (without printing secrets)
  and `make config-show` shows the effective configuration with secrets
  redacted (see `docs/SAFETY_AND_RISK.md` §9).

## 4. systemd install/enable for the engine and the independent dashboard

- [ ] Compiled dashboard built: `npm run build` in `dashboard/frontend`
  produced `dashboard/frontend/dist/` (currently **absent** — see
  `docs/verification/phase_t_complete_product.md` row 21 and
  `docs/DASHBOARD.md` §2). Never point production at a Vite dev server.
- [ ] `deploy/systemd/mike-trade-engine.service` and
  `deploy/systemd/mike-trade-dashboard.service` reviewed as templates
  (placeholders `__OPERATOR_USER__`/`__REPO_DIR__`/`__VENV_DIR__`/`__ENV_FILE__`
  substituted in the installed copies, no credentials embedded) and installed
  via `make service-install`; status via `make service-status`
  (see `docs/DASHBOARD.md` §3, `docs/SAFETY_AND_RISK.md` §9).
- [ ] Dashboard unit has no lifecycle binding to the engine unit and binds
  localhost by default; any non-loopback bind uses the explicit opt-in plus
  private auth/TLS/VPN/reverse proxy (see `docs/DASHBOARD.md` §3).

## 5. doctor / provider / capability checks

- [ ] `make doctor` passes (see `docs/TEST_PLAN.md` §4,
  `docs/SAFETY_AND_RISK.md` §9).
- [ ] `make broker-check ACCOUNT=<env>:<acct>`, `make provider-check
  ACCOUNT=<env>:<acct>`, and `make data-check ACCOUNT=SHADOW:default` pass
  against the intended environment (see `docs/ALPACA.md` §1).
- [ ] Capability matrix reviewed (`docs/ALPACA.md` §6): every protective
  combination intended for use is `PROVEN` for the target environment, not
  `UNPROVEN`/`UNSUPPORTED`; structurally `UNSUPPORTED` combinations (short
  side, non-`simple` trailing, notional+replace) are not used.

## 6. Startup reconciliation clean with no drift

- [ ] Engine starts and `make reconcile` reports no drifts; `make health`
  is healthy with no critical alerts (see `docs/SAFETY_AND_RISK.md` §8).
- [ ] No `UNMANAGED_HOLDING` drifts unaccounted for; any external position
  is either adopted through the explicit `ADOPT_POSITION` control path or
  deliberately left `UNMANAGED` — never silently adopted or closed
  (see `docs/SAFETY_AND_RISK.md` §8).
- [ ] No `UNKNOWN_BROKER_ORDER` drifts; unknown protective-order states
  block new entries until resolved (see `docs/SAFETY_AND_RISK.md` §4–§5).

## 7. Dashboard reachable with the engine stopped (shows OFFLINE)

- [ ] With the engine stopped, the dashboard stays up (`make dashboard-health`
  / `make dashboard`) and reports engine `OFFLINE` from heartbeat age with
  values marked `fresh=false` and explicit `stale_reason`
  (see `docs/DASHBOARD.md` §4).
- [ ] Dashboard write surface confirmed absent: non-`GET`/`HEAD` methods get
  `405`; no order placement or config mutation path exists
  (see `docs/DASHBOARD.md` §1, §7).

## 8. Shadow reference scenario run reviewed

- [ ] A `vwap_reference` shadow run (BUY `vwap_bounce` / SELL `vwap_breakdown`)
  completed and its ledger report reviewed: ending cash, realized P&L, and
  residual reconcile (`make report-shadow`, `make report`,
  `make report-comparison`; see `docs/REQUIREMENTS.md` §5,
  `docs/SAFETY_AND_RISK.md` §11).
- [ ] Restart-mid-cycle accounting reviewed: cash, positions, stops, and open
  orders preserved exactly (see
  `docs/verification/phase_t_complete_product.md` row 13).

## 9. Paper lifecycle verified per supported protective combination

- [ ] `make test-paper` (credential-gated paper selection) executed with
  operator-supplied paper credentials
  (`ALPACA_PAPER_KEY_ID`/`ALPACA_PAPER_SECRET_KEY`) plus explicit opt-in and
  reviewed — currently **pending**, no execution evidence in the repo
  (see `docs/TEST_PLAN.md` §4, `docs/ALPACA.md` §7,
  `docs/verification/phase_t_complete_product.md` row 23).
- [ ] For **each** protective combination intended for use (fixed stop,
  native trailing), a real paper buy → protect → flat cycle was observed and
  recorded with `record_paper_verification()`; paper fills treated as
  capability existence only, never as live-performance proof
  (see `docs/ALPACA.md` §7).

## 10. Entry kill switch, pause, close, and flatten rehearsed in shadow/paper

- [ ] `make pause` / `make resume`, per-symbol `make pause-symbol` /
  `make resume-symbol`, and kill-switch `make entries-disable` /
  `make entries-enable` rehearsed; resume does not clear a kill-switch
  disable (see `docs/SAFETY_AND_RISK.md` §6).
- [ ] `make close SYMBOL=X`, `make close-all`, and `make flatten` rehearsed
  through the engine control channel; offline refusal (exit `4`) confirmed
  understood — closes are never queued blindly while the engine is offline
  (see `docs/SAFETY_AND_RISK.md` §6–§7).
- [ ] Stop semantics confirmed: `make engine-stop` drains and publishes `STOPPED`
  while leaving positions and broker-held protective stops in place
  (see `docs/SAFETY_AND_RISK.md` §6–§7).

## 11. Risk limits and budgets reviewed

- [ ] Per-symbol and total budgets reviewed (`make budget`); buying-power
  gate, planned-loss sizing, and `max_open_positions` understood
  (see `docs/SAFETY_AND_RISK.md` §1).
- [ ] Entry-cycle caps, symbol/strategy loss lockouts, post-sale cooldown,
  minimum-hold scope, session gates, and entry cutoff reviewed
  (`make entry-limits`, `make cooldown-status`, `make stop-status`,
  `make trail-status`; see `docs/SAFETY_AND_RISK.md` §2–§4).
- [ ] Stop-risk notes accepted: trigger price is not a guaranteed execution
  price or maximum loss; `DAY` protection expires overnight unless renewed
  (see `docs/SAFETY_AND_RISK.md` §4).

## 12. Live enablement prerequisites with explicit live confirmation

- [ ] Paper verification complete for **every** capability the live book will
  use (step 1), plus a separate explicit LIVE `operator_record` per
  capability via `with_live_promotion()` (step 2)
  (see `docs/ALPACA.md` §8).
- [ ] `make live-preflight ACCOUNT=LIVE:main` passes — it refuses (exit `3`)
  on any failed check and enables/starts nothing on failure (step 3)
  (see `docs/ALPACA.md` §8, `docs/SAFETY_AND_RISK.md` §5).
- [ ] `CONFIRM_LIVE=YES make live-enable` and `CONFIRM_LIVE=YES make
  live-start` run only after the above, with explicit confirmation
  (steps 4) (see `docs/ALPACA.md` §8).
- [ ] Ongoing monitoring (`make health`, `make reconcile`,
  `make stops-audit`, `make budget`) assigned; `make live-disable`
  (immediate, no confirmation hurdle) known to all operators (step 5)
  (see `docs/ALPACA.md` §8, `docs/SAFETY_AND_RISK.md` §11).

## 13. Rollback procedure

- [ ] Pre-change backup taken (`make db-backup OUT=<path>`) with manifest;
  restore rehearsed via `make db-restore-dry-run` before any restore
  (see `docs/SAFETY_AND_RISK.md` §10).
- [ ] De-risk path confirmed: `make pause` (or kill switch
  `make entries-disable`), then `make live-disable` for live, then assess
  with `make health` / `make positions` / `make reconcile`
  (see `docs/SAFETY_AND_RISK.md` §11).
- [ ] Engine rollback is restart of the prior known-good checkout/commit
  (`make engine-restart`, then `make health`); closes go through the reconciled
  control channel after restart, never around it
  (see `docs/SAFETY_AND_RISK.md` §11).

## NO-GO conditions (any one means NO-GO)

- Any `FAIL` status in `docs/verification/phase_t_complete_product.md`
  (currently: missing compiled dashboard build output).
- Any `NOT-RUN` row relied upon for release behavior without a witnessed
  execution recorded.
- Unresolved reconciliation drift (`make reconcile` dirty, `make health`
  critical, unknown order states, or unaccounted `UNMANAGED_HOLDING`s).
- Any protective combination intended for use that is still `UNPROVEN` or
  `UNSUPPORTED` in the capability matrix for the target environment.
- Missing paper verification before LIVE (no observed paper buy → protect →
  flat cycle per protective combination, or no LIVE `operator_record`).
- Credential or permission violations: cross-environment keys on one host,
  `.env` not mode-`0600`/operator-owned, state/database permissions wrong,
  or secrets ever committed, logged, or pasted outside the env file.
