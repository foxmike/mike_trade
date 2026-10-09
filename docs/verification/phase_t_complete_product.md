# Phase T — Final Acceptance Coverage Record (Complete Product)

**Phase:** T · **Type:** audit artifact (documentation only — no code or tests modified)
**Date (UTC):** 2026-10-09
**Method:** static inspection of phase-t artifacts listed below. No test suite was
executed, no backtest was run, no broker (paper or live) was contacted, and no
orders of any kind were placed during this pass. The `pytest`/`make` commands
named in the evidence column are pointers to the coverage that *would* prove
each row, not claims that it was run here.

## Status semantics used in this document

| Status | Meaning in this record |
| :--- | :--- |
| `PASS` | Directly confirmed by inspection of the cited artifact in this pass; no execution required (structural fact: file exists, code path or normative doc text states the behavior). |
| `FAIL` | Inspected in this pass and found not satisfied. |
| `PENDING-EXTERNAL` | Cannot be satisfied without operator-supplied credentials, explicit approval/opt-in, or an operator-witnessed run; no such evidence exists in the repo. |
| `NOT-RUN` | Behavioral coverage exists at the cited test file/test name (verified to exist and assert the behavior by reading it), but it was **not executed** in this documentation-only pass, so no pass/fail is claimed. |

## Artifacts inspected for this record (phase-t inputs)

- `deploy/systemd/mike-trade-engine.service`, `deploy/systemd/mike-trade-dashboard.service`
  (t.1 — both are optional future-use templates; headers state they were never
  copied, enabled, or started).
- `mike_trade/controls/service_manager.py` (t.1 — `systemctl` shell-out helpers only; no deployment).
- `tests/paper_broker/test_alpaca_paper_cycle.py`,
  `tests/end_to_end/test_protective_live_guards.py` (t.2).
- `mike_trade/app.py`, `mike_trade/runtime.py`,
  `tests/end_to_end/test_full_product_cycle.py`,
  `tests/end_to_end/test_recovery_offline.py` (t.3).
- `docs/REQUIREMENTS.md`, `docs/SAFETY_AND_RISK.md`, `docs/TEST_PLAN.md`,
  `docs/DASHBOARD.md`, `docs/ALPACA.md` (t.4).
- `Makefile` / `mike_trade/cli.py` (`CANONICAL_COMMANDS`), `mike_trade/persistence/migrations/`,
  `uv.lock` / `pyproject.toml`, `dashboard/frontend/` build configuration
  (`vite.config.ts` with `outDir: 'dist'`).

## Coverage table

| # | Requirement | Evidence (test file + test name, or doc section / file path) | Status | Notes |
| :--- | :--- | :--- | :--- | :--- |
| 1 | 1- and 10-symbol shared-feed fanout (one immutable snapshot fans out to all symbols/scenarios; acquisition is per symbol) | `tests/end_to_end/test_full_product_cycle.py::test_shared_snapshot_fanout_one_and_ten_symbols_and_scenario_isolation` (10 symbols `S0`–`S9`, `acquisitions == {symbol: 1}`, two scenarios) | NOT-RUN | Test file and assertions verified by reading; not executed in this pass. |
| 2 | Independent BUY/SELL rules and parameters (separate strategy classes, separate parameter mappings) | `tests/end_to_end/test_full_product_cycle.py::test_reference_pair_full_cycle_exact_once_and_protected` (asserts `evaluator.buy_parameters == {}`, `is not` `sell_parameters`); `mike_trade/app.py` (`VwapBounceBuy` / `VwapBreakdownSell`, `buy_parameters` / `sell_parameters`) | NOT-RUN | Implementation and assertions confirmed by reading; behavioral run not executed here. |
| 3 | Exactly one entry per cycle under repeated signals and partial fills (duplicate suppression; replay never submits a second order) | `tests/end_to_end/test_full_product_cycle.py::test_reference_pair_full_cycle_exact_once_and_protected` (duplicate `process()` of identical context, `len(entry_orders) == 1`) and `::test_partial_fill_replay_never_submits_second_entry` (identical-quote replay, `len(entries) == 1`, full fill `29`) | NOT-RUN | Both tests read and verified; not executed in this pass. |
| 4 | Full remaining exit after minimum hold (strategy exit submits full remaining quantity; ordinary SELL exits honor `minimum_hold`, protective exits bypass it) | `tests/end_to_end/test_full_product_cycle.py::test_reference_pair_full_cycle_exact_once_and_protected` (`minimum_hold=1min`, exit at `+2min`, `EXIT:` path, positions `{}`); `docs/SAFETY_AND_RISK.md` §2 (minimum-hold scope) | NOT-RUN | Assertions read; execution not performed here. |
| 5 | Daily entry-cycle count and restart/session reset (per-symbol per-session cap; counts reset at next session open) | `tests/end_to_end/test_full_product_cycle.py::test_limits_lockouts_budget_race_and_session_reset` (`MAX_ENTRY_CYCLES_REACHED`, `filled_entry_cycles(...) == 1` vs next-session `== 0`) | NOT-RUN | Test read and verified; not executed in this pass. |
| 6 | Symbol and buy-strategy loss lockouts (`LOSING_STREAK_LOCKOUT`, account-wide vs strategy-scoped) | `tests/end_to_end/test_full_product_cycle.py::test_limits_lockouts_budget_race_and_session_reset` (both the default and `lockout_scope_by_strategy=True` legs, `losing_streak("SPY","vwap_bounce") == 1` vs `other_buy == 0`) | NOT-RUN | Test read and verified; not executed in this pass. |
| 7 | Persistent cooldown (post-sale cooldown survives restart with identical expiry) | `tests/end_to_end/test_recovery_offline.py::test_cooldown_and_loss_lockout_survive_restart` (`cooldown_expiry` equality across restart); also asserted inline in `test_reference_pair_full_cycle_exact_once_and_protected` (`POST_SALE_COOLDOWN`, expiry after `+6min`) | NOT-RUN | Tests read and verified; not executed in this pass. |
| 8 | Concurrent budget race (exactly one of two simultaneous over-budget entries authorizes) | `tests/end_to_end/test_full_product_cycle.py::test_limits_lockouts_budget_race_and_session_reset` (`asyncio.gather` of two `authorize()` calls, `sum(authorized) == 1`) | NOT-RUN | Test read and verified; not executed in this pass. |
| 9 | Stop coverage and no oversell (protective order covers exactly the confirmed quantity; sells checked against `known_position_qty`) | `tests/end_to_end/test_full_product_cycle.py::test_reference_pair_full_cycle_exact_once_and_protected` (`protective[0].quantity == D("29")`); `docs/SAFETY_AND_RISK.md` §4 (remaining-quantity coverage, `OversellRejected`); `docs/ALPACA.md` §6 (sell quantities checked against authoritative `known_position_qty`) | NOT-RUN | Assertions and normative text read; behavioral run not executed here. |
| 10 | Native/stepped trailing init, monotonic/multi-step ratchet, replace/close races | `tests/end_to_end/test_protective_live_guards.py::test_long_stepped_stop_is_monotonic_on_rising_and_falling_observations` (monotonic ratchet over rising/falling observations) and `::test_overlapping_discretionary_exit_and_stop_never_submit_two_sells` (stop/exit race never double-sells); native-trailing submit path exercised only under paper credentials in `tests/paper_broker/test_alpaca_paper_cycle.py::test_real_alpaca_paper_buy_protect_and_flat_cycle[native_trailing]` | NOT-RUN | Offline tests read; native-trailing leg additionally requires paper credentials (see row 23). `docs/TEST_PLAN.md` §3 also names integration/unit trailing files (`test_native_stop_protection.py`, `test_stepped_trailing_lifecycle.py`, `test_stop_close_races.py`, unit trailing tests) that were **not** inspected in this pass. |
| 11 | Protection on partial fills (no protection on zero/unconfirmed fill; coverage tracks cumulative confirmed quantity via cancel/replace) | `tests/end_to_end/test_protective_live_guards.py::test_protection_waits_for_positive_fill_and_tracks_partial_fill_quantity` (zero/unconfirmed → `None`, `2` → `stop-1`, growth to `5` via cancel + replace) | NOT-RUN | Test read and verified; not executed in this pass. |
| 12 | Scenario/live isolation (mode composition, credential/endpoint/scope separation, live refuses to start unauthorized) | `tests/end_to_end/test_full_product_cycle.py::test_mode_composition_isolated_and_live_requires_authorization` (4 distinct DB paths, no Alpaca client in SHADOW/BACKTEST, `LiveTradingNotAuthorizedError`) and `tests/end_to_end/test_protective_live_guards.py::test_paper_and_live_credentials_endpoints_and_scopes_are_isolated` plus `::test_live_runtime_refuses_start_without_explicit_live_authorization` | NOT-RUN | Tests read and verified; not executed in this pass. Isolation design additionally confirmed in `mike_trade/app.py` (lazy provider imports; SHADOW cannot load Alpaca) and `docs/REQUIREMENTS.md` §2. |
| 13 | Deterministic shadow restart accounting (cash, position, stops, open orders preserved exactly across stop/restart) | `tests/end_to_end/test_recovery_offline.py::test_restart_mid_cycle_preserves_account_stop_and_exact_decimal_state` (cash/position/stop/open-order equality; shutdown adds no cancel/fill/close event) | NOT-RUN | Test read and verified; not executed in this pass. |
| 14 | Dashboard OFFLINE/stale (stale heartbeat → `OFFLINE`, `fresh=false` with `stale_reason`) | `tests/end_to_end/test_recovery_offline.py::test_stale_heartbeat_dashboard_reports_offline_and_stale_values` (`engine_state == "OFFLINE"`, `is_stale`, `entries_allowed is False`, `stale_reason == "engine_offline"`); rule specified in `docs/DASHBOARD.md` §4 | NOT-RUN | Test read and verified; not executed in this pass. |
| 15 | Restart with outstanding orders/stops/external trade (fill applied exactly once; external holdings `UNMANAGED`; orphan stops block startup; recovery never submits) | `tests/end_to_end/test_recovery_offline.py::test_recovery_reconciles_fill_once_and_blocks_external_and_orphan_stop` (`applied_fill_ids == ("broker-fill-googl",)`, then `()`, `UNMANAGED_HOLDING` for MSFT, `UNKNOWN_BROKER_ORDER` for orphan stop, `BLOCKED`, `cancel_calls == []`) | NOT-RUN | Test read and verified; not executed in this pass. |
| 16 | Early-close cutoff and default no-EOD-liquidation (cutoff tracks actual session close incl. early closes; EOD close is opt-in metadata, default off) | `mike_trade/app.py::AppSettings.__post_init__` (raises `ConfigurationError` if `auto_eod_liquidation=True`; default `False`); `tests/end_to_end/test_full_product_cycle.py::test_early_close_cutoff_default_no_eod_and_fractional_replay` (2026-11-27 early close, post-cutoff `actions == []`, no EOD registration on scheduler) | PASS | Default-off is structurally enforced in inspected code (opt-in raises at construction); early-close cutoff behavior is asserted by the cited test, which was read but not executed here. |
| 17 | Stop / pause / close / flatten distinction (six distinct operations with distinct effects, confirmation, and online requirements) | `docs/SAFETY_AND_RISK.md` §6 (normative table); `mike_trade/controls/service_manager.py` (stop never liquidates/cancels protection); `mike_trade/runtime.py::Runtime.stop/pause` (stop = drain + `STOPPED` heartbeat, no broker side effects); `tests/end_to_end/test_protective_live_guards.py::test_runtime_shutdown_leaves_valid_protective_stop_untouched` and `tests/end_to_end/test_recovery_offline.py::test_durable_pause_keeps_running_runtime_entry_blocked` | PASS | Normative doc text and implementing code inspected and consistent; the two cited behavioral tests were read but not executed here. |
| 18 | Ledger/report reconciliation for whole and fractional fills (exactly-once fill ledger, report ties to state, fractional replay restores exactly) | `tests/end_to_end/test_full_product_cycle.py::test_reference_pair_full_cycle_exact_once_and_protected` (whole-share: `report.ending_cash == cash`, `residual == 0`, `reconciled is True`) and `::test_early_close_cutoff_default_no_eod_and_fractional_replay` (fractional quantity restored exactly across restart, report reconciled); `tests/paper_broker/test_alpaca_paper_cycle.py::test_real_alpaca_paper_buy_protect_and_flat_cycle` (paper activities replayed through `FillProcessor` with `APPLIED` then `DUPLICATE`, ledger counts match) | NOT-RUN | Tests read and verified; none executed in this pass; the paper leg additionally requires credentials (row 23). |
| 19 | No-lookahead replay (bars hidden until available; identical seeds replay identically; contexts reject future bars) | `tests/end_to_end/test_full_product_cycle.py::test_deterministic_replay_hides_future_bars_and_context_rejects_them` (empty feed before availability, `ahead of the replay clock` refusal, two seeds equal, `closes after as_of` rejection) | NOT-RUN | Test read and verified; not executed in this pass. |
| 20 | Complete Make/CLI surface (every documented operator command exists in both `Makefile` and CLI) | `Makefile` (targets inspected: `doctor`, `broker-check`, `provider-check`, `data-check`, `live-preflight`, `live-enable`, `close`, `close-all`, `flatten`, `pause`, `entries-disable`, `reconcile`, `budget`, `db-check`, `db-backup`, `db-restore-dry-run`, `service-install/status`, `dashboard-start/health`, shadow/paper/backtest/report families) and `mike_trade/cli.py::CANONICAL_COMMANDS` | PASS | Surface confirmed by direct inspection in this pass (`grep` over `Makefile` and `cli.py`). Hermetic target-surface test `tests/unit/test_make_targets.py` (per `docs/TEST_PLAN.md` §1) was not executed here. |
| 21 | Reproducible repository/deployment artifacts (dependency locks, migrations, systemd units, compiled dashboard) | `uv.lock` + `pyproject.toml` (present, observed via `ls`); `mike_trade/persistence/migrations/0001_initial.sql` (present); `deploy/systemd/*.service` (present as templates; headers + `docs/DASHBOARD.md` §3 confirm never installed/enabled/started); `dashboard/frontend/` source + `vite.config.ts` (`outDir: 'dist'`) present but **no compiled `dist/` exists** (`dashboard/frontend/dist` and `mike_trade/dashboard/frontend/dist` both absent) | FAIL | FAIL because the compiled dashboard build output is absent: production serving requires it (`mike_trade/dashboard/server.py` serves `dist`; missing build yields `503` for UI routes per `docs/DASHBOARD.md` §2) and no build has been run in this checkout. Locks, migrations, and unit templates are present as observed. Operator must run `npm run build` per `docs/DASHBOARD.md` §2 before release. |
| 22 | `vwap_reference` end-to-end proof (full signal → decision → intent → order → fill → protection → exit → report cycle for the shipped BUY/SELL reference pair) | `tests/end_to_end/test_full_product_cycle.py::test_reference_pair_full_cycle_exact_once_and_protected[strategy]` and `[stop]` (entry `29` shares, protective armed, strategy-exit realized `−31.9000` vs stop-exit `−60.9000`, report reconciled) | NOT-RUN | Test file and both parametrized assertions verified by reading; not executed in this pass, so no end-to-end proof is claimed here. |
| 23 | Credential-gated Alpaca PAPER acceptance (real buy → protect → flat cycle with ledger reconciliation, plus blocked-combination guard) | `tests/paper_broker/test_alpaca_paper_cycle.py::test_real_alpaca_paper_buy_protect_and_flat_cycle[fixed]` and `[native_trailing]` (module skips without `ALPACA_PAPER_KEY_ID` + `ALPACA_PAPER_SECRET_KEY` + `MIKE_TRADE_PAPER_ORDERS_APPROVED=1` + `MIKE_TRADE_PAPER_SYMBOL`; aborts if any LIVE credential variable is set) and `::test_unverified_fractional_protection_and_stepped_trailing_are_blocked` | PENDING-EXTERNAL | No operator-supplied credentials, approval, or execution evidence exist in the repo; the module-level gate was read and would skip without them. Paper order-flow verification is therefore **pending**, not passed. |

## Known gaps, blocked/unverified provider combinations, and explicit statements

### Gaps found in this pass

1. **No test was executed in this pass.** Every `NOT-RUN` row above has coverage
   present in the checkout (verified by reading the cited test) but no observed
   result from this task. Prior phase verification docs under
   `docs/verification/` (e.g. `phase_p_shadow.md`, which records executed pytest
   commands for SHADOW restart behavior) exist, but re-running or re-claiming
   their results is out of scope for this documentation-only task; consult those
   documents for historical execution evidence.
2. **Compiled dashboard missing (row 21, `FAIL`).** No `dist/` output exists;
   the release checklist requires `npm run build` before the dashboard is deployable.
3. **`docs/TEST_PLAN.md` §3 is stale relative to this checkout.** It states that
   `tests/paper_broker/test_alpaca_paper_cycle.py` and
   `tests/end_to_end/test_protective_live_guards.py` **do not exist** and point to
   other files as the implemented coverage. Both files **do exist** in this
   checkout and were read in full for this record; their test names are cited in
   rows 10, 11, 12, 17, 18, and 23 above. `docs/TEST_PLAN.md` should be updated
   (outside this task's write permissions) before it is used as a release reference.
4. **Integration/unit protective files named in `docs/TEST_PLAN.md` §3**
   (`tests/integration/test_native_stop_protection.py`,
   `test_stepped_trailing_lifecycle.py`, `test_stop_close_races.py`, unit
   fixed-stop/stepped-trailing/policy tests) were **not inspected** in this pass
   and are cited nowhere above as evidence.

### Blocked or unverified provider combinations (Alpaca capability matrix)

Per `docs/ALPACA.md` §6 and the fail-closed guards read in
`tests/paper_broker/test_alpaca_paper_cycle.py::test_unverified_fractional_protection_and_stepped_trailing_are_blocked`
and
`tests/end_to_end/test_protective_live_guards.py::test_fractional_stop_and_trailing_combinations_fail_closed`:

- **Blocked pending paper demonstration (`UNPROVEN`, fail closed):** fractional
  stop sell; fractional trailing stop; stop-only `oto`; `bracket`/`oco` (any
  combination); fractional `market`/`limit` entry/exit; `notional` sizing with
  `submit`; stop-price / trailing-parameter / `oto` replace and quantity-based
  replace generally; extended TIFs (`opg`, `cls`, `ioc`, `fok`); fractional `gtc`.
- **Structurally blocked (`UNSUPPORTED`, can never be promoted):** `short` side
  (long-only desk); `trailing_stop` with order class other than `simple`;
  `notional` + `replace`.
- **Stepped trailing (`TRAILING_STEPPED`)** is engine-simulated and unavailable
  through the broker replace path: eligibility returns `STEPPED_NOT_AVAILABLE`
  for the PAPER check in the cited paper test.
- Baseline whole-share `market`/`limit` entry/exit and standalone whole-share
  `stop` / `trailing_stop` protection are doc-supported but **pending paper
  verification** — no `record_paper_verification()` evidence exists in the repo.

### Explicit statements

- **No live orders were part of automated verification.** No test suite, recipe,
  or CI step in this checkout submits, replaces, cancels, or otherwise mutates
  live orders (`docs/TEST_PLAN.md` §5). Nothing in this record claims otherwise,
  and **no row above claims that a live-order check passed**.
- **Paper fills do not prove live performance.** Paper execution quality, queue
  behavior, liquidity constraints, feed differences (default Basic IEX is not
  consolidated SIP, per `docs/ALPACA.md` §2), and fee treatment differ from live.
  Paper evidence, when obtained, establishes capability existence only — never
  profitability, edge, or expected live behavior (`docs/ALPACA.md` §7,
  `docs/REQUIREMENTS.md` preamble).
