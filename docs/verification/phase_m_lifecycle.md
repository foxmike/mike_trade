# Phase M Lifecycle — Verification (position cycle, crash restart)

End-to-end lifecycle coverage of the cycle FSM
(`mike_trade/portfolio/cycles.py`), the coordinator
(`mike_trade/portfolio/manager.py`), reconciliation
(`mike_trade/execution/reconcile.py`) and projections
(`mike_trade/portfolio/status.py`) as a unit, exercised through
`tests/integration/test_position_cycle_restart.py`.

- Test module: `tests/integration/test_position_cycle_restart.py`
- Scope keys: `paper:acct-1` (coordinator tests), `paper:EXT-CLOSE`,
  `paper:RECON-DRIFT`, `paper:UNMANAGED` (reconciler tests, one temporary
  SQLite database per test via `tmp_path`)
- Clock: deterministic in-memory fake clock pinned at
  2026-10-08 14:30 UTC; every broker port is an in-memory fake
  (no ` submitting ` broker, no network, no credentials).

**No live or paper broker orders were placed.** All fills, orders, positions,
and account snapshots are constructed in memory by fake ports; the only I/O is
temporary SQLite files under `tmp_path` (cycle-snapshot persistence and the
existing `initialize_database` / `WriterSession` persistence layer). No broker
credentials were read and no network connection was opened.

## Scenario → test → invariant map

All references are
`tests/integration/test_position_cycle_restart.py::<test_name>`.

| # | Scenario | Covering test | Invariant proved |
|---|---|---|---|
| 1 | Partial entry fill | `test_partial_entry_fill_consumes_one_slot_and_never_rebuys_remainder` | Buy N=10 fills k=4, remainder canceled: cycle retains 4, exactly one entry slot consumed (`record_entry_fill` once), protection required for exactly 4 (`ensure_protection((4, 0))`), state `PROTECTIVE_PENDING`, and a rebuy signal is `IGNORED/position_held` with no second entry order |
| 2 | Partial exit fill | `test_partial_exit_fill_updates_remaining_and_blocks_buys_until_flat` | Full-quantity discretionary sell fills 4/10: `remaining=6`, `filled_exit=4`, protection resized without overlapping the working sell (`ensure_protection((6, 6))`), BUY is `IGNORED/position_held`, roles are SELL-only, budget not released |
| 3 | Manual close bypasses minimum hold | `test_manual_close_bypasses_minimum_hold_and_closes_full_quantity` | Inside a 1-hour minimum hold, STRATEGY sell is `IGNORED/minimum_hold_not_elapsed` while operator `CLOSE_POSITION` is `ACCEPTED` and submits one `MANUAL_CLOSE` sell for the full remaining 10 |
| 4 | External/manual close detection | `test_external_manual_close_detected_and_audited_not_strategy_win` | Broker flat with no local sell: `EXTERNAL_MANUAL_EXIT` drift, cycle `CLOSED`, journal `ExternalManualExitDetected` carries `classification=external_manual_exit`, `strategy_exit=false`, `strategy_win=false`, zero `StrategyExit`/`StrategyWin` events, ACTIVE budget reservation `RELEASED`, rerun applies nothing and emits no second event |
| 5 | Crash restart mid-cycle | `test_crash_restart_applies_missing_fill_exactly_once_and_blocks_duplicate_entry` | `BUY_PENDING` snapshot persisted to a temporary SQLite file; coordinator rebuilt **from the file** (in-memory objects discarded): the downtime fill applies once (`ACCEPTED`, then `DUPLICATE` replay), `record_entry_fill` fires once, entries stay blocked pre- and post-protection (`IGNORED`), and the duplicate BUY creates no second entry (zero reserves, zero submits, `entry_order_ids` unchanged) |
| 6 | Crash restart, unknown order / missing stop | `test_crash_restart_unknown_order_or_missing_stop_stays_reconciling` | Unknown open broker order plus missing protective stop: symbol status `RECONCILING` with both `UNKNOWN_BROKER_ORDER` and `PROTECTION_COVERAGE_MISSING` reasons, `entries_permitted is False` |
| 7 | External holdings | `test_external_holding_is_unmanaged_and_not_adopted` | Pre-existing broker position with no managed cycle: `UNMANAGED` status, `entries_permitted is False`, zero `position_cycles` and zero `positions` rows (never adopted); the status projection renders `unmanaged=True`, `remaining_managed_quantity=0`, phase `unmanaged position` |
| 8 | Pause semantics | `test_pause_blocks_entries_but_fills_and_protection_still_apply` | With operator pause active, new entries are `BLOCKED/operator_paused` (flat symbols consult no roles) while strategy sells, partial sell fills, and protection updates are all `ACCEPTED`; the projection reports `entries_blocked` with a `paused` block reason |

## Targeted pytest command and observed results

Interpreter is the project venv (`.venv/bin/python`, `asyncio_mode = "auto"`).

Command:

```sh
.venv/bin/python -m pytest tests/integration/test_position_cycle_restart.py -v
```

Observed result (2026-10-08 run, copied from the actual run):

```text
tests/integration/test_position_cycle_restart.py::test_partial_entry_fill_consumes_one_slot_and_never_rebuys_remainder PASSED [ 12%]
tests/integration/test_position_cycle_restart.py::test_partial_exit_fill_updates_remaining_and_blocks_buys_until_flat PASSED [ 25%]
tests/integration/test_position_cycle_restart.py::test_manual_close_bypasses_minimum_hold_and_closes_full_quantity PASSED [ 37%]
tests/integration/test_position_cycle_restart.py::test_external_manual_close_detected_and_audited_not_strategy_win PASSED [ 50%]
tests/integration/test_position_cycle_restart.py::test_crash_restart_applies_missing_fill_exactly_once_and_blocks_duplicate_entry PASSED [ 62%]
tests/integration/test_position_cycle_restart.py::test_crash_restart_unknown_order_or_missing_stop_stays_reconciling PASSED [ 75%]
tests/integration/test_position_cycle_restart.py::test_external_holding_is_unmanaged_and_not_adopted PASSED [ 87%]
tests/integration/test_position_cycle_restart.py::test_pause_blocks_entries_but_fills_and_protection_still_apply PASSED [100%]

============================== 8 passed in 0.50s ===============================
```

No `xfail` markers were needed: every scenario passes against current
production code, so no open defects are reported and — per the task contract —
nothing outside the two allowed paths was modified (`git status` shows changes
only under `tests/integration/test_position_cycle_restart.py` and
`docs/verification/phase_m_lifecycle.md`; no files under `mike_trade/` were
touched).

## Notes

- During development, three first-draft assertions encoded wrong expectations
  about production behavior (since corrected in the tests, not in production):
  the status projection does not gate entries on a held `WATCHING_SELL` cycle
  (buy exclusion is expressed via `consulted_roles`, not `entries_blocked`);
  manager-level pause leaves SELL consultation intact for held symbols (only
  flat symbols consult nothing); and a fully-filled entry reservation is
  `CONSUMED`, so the external-close test seeds a partially-filled intent to
  observe the `ACTIVE → RELEASED` budget release.
- Per the task contract, no broad full-suite or backtest runs were performed;
  verification is limited to the targeted command above.
