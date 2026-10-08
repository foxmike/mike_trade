# Phase L Entry Controls — Verification (l.1–l.4)

Integration coverage for budget/reservation (l.1), sizing (l.2), entry
eligibility (l.3), and session/market-filter (l.4) controls, exercised through
the real modules against a temporary SQLite WAL database with a deterministic
fake clock. No broker, network, or credentials are used.

- Test module: `tests/integration/test_entry_policy_restart.py`
- Scope keys: `shadow:ACCT:alpha`, `shadow:ACCT:beta`, `paper:ACCT`
- Sessions: THU = 2026-10-08 14:30 UTC (Thursday 10:30 ET, regular hours),
  FRI = 2026-10-09 14:30 UTC (next NY trading session),
  SAT = 2026-10-10 15:00 UTC (weekend, belongs to Friday's session)

## Control → test map

All references are `tests/integration/test_entry_policy_restart.py::<test_name>`.

### l.1 budgets / reservations (`mike_trade/portfolio/budgets.py`, `reservations.py`)

| Control | Covering test(s) |
|---|---|
| Total application budget cap (`total_budget_usd`) | `test_cross_scenario_isolation`, `test_simultaneous_signals_priority_ordered_without_overcommit` |
| Per-symbol budget cap (`symbol_budget_usd`) | `test_cross_scenario_isolation`, `test_simultaneous_signals_priority_ordered_without_overcommit` |
| One pending/open entry cycle per symbol | `test_partial_fill_semantics_end_to_end` (final cancel leaves OPEN cycle; zero-fill cancel closes), `test_entry_blocks_never_block_exits` (close_cycle releases) |
| Partial fill converts reservation → committed notional | `test_partial_fill_semantics_end_to_end` |
| Final cancel releases unused budget, keeps committed | `test_partial_fill_semantics_end_to_end` |
| Zero-fill cancel consumes nothing, closes cycle | `test_partial_fill_semantics_end_to_end` |
| Idempotent fill / terminal-event dedupe across restart | `test_restart_durability` (fill replay no-op after restart) |
| Deterministic priority-ordered `reserve_many` acceptance | `test_simultaneous_signals_priority_ordered_without_overcommit` (reversed input, identical outcome) |
| Shared account lock: concurrent reserves never overcommit | `test_simultaneous_signals_priority_ordered_without_overcommit` (`asyncio.gather` race, exactly one wins) |
| Full scope isolation (budgets, reservations) | `test_cross_scenario_isolation` |
| Reservation durability across fresh instances | `test_restart_durability` |

### l.2 sizing (`mike_trade/portfolio/sizing.py`)

| Control | Covering test(s) |
|---|---|
| Protected long sizing bounded by snapshot budgets | `test_cross_scenario_isolation` (full-size PAPER entry accepted), `test_partial_fill_semantics_end_to_end` (post-fill snapshot still sizes) |
| Protective-stop validation + capability gating | `test_cross_scenario_isolation`, `test_partial_fill_semantics_end_to_end` (FIXED_STOP whole-share capability) |

### l.3 entry eligibility (`mike_trade/risk/entry_eligibility.py`)

| Control | Covering test(s) |
|---|---|
| Daily entry-cycle limit (first positive fill consumes one slot; later partials consume nothing; unfilled entries consume nothing) | `test_cross_scenario_isolation`, `test_partial_fill_semantics_end_to_end` |
| Cycle counts reset at next NY session (weekend holds prior session) | `test_session_reset_clears_cycles_and_next_session_lockouts_but_not_manual` |
| Losing-streak lockout at threshold (loss increments, win resets) | `test_cross_scenario_isolation`, `test_session_reset_clears_cycles_and_next_session_lockouts_but_not_manual`, `test_entry_blocks_never_block_exits` |
| `next_session` lockout auto-releases with fresh streak | `test_session_reset_clears_cycles_and_next_session_lockouts_but_not_manual` |
| `manual` lockout persists across sessions AND restarts until audited reset | `test_session_reset_clears_cycles_and_next_session_lockouts_but_not_manual` |
| Audited reset requires operator/reason, is append-only + idempotent | `test_session_reset_clears_cycles_and_next_session_lockouts_but_not_manual` (reset_id replay, event log) |
| Post-sale cooldown from final closing fill; expiry unblocks | `test_cross_scenario_isolation`, `test_restart_durability`, `test_entry_blocks_never_block_exits` |
| Daily loss guard blocks all symbols for the session | `test_entry_blocks_never_block_exits` |
| Max-open-positions guard | `test_entry_blocks_never_block_exits` |
| Combined decision lists every blocking reason | `test_entry_blocks_never_block_exits` (5 reasons), `test_cross_scenario_isolation` (3 reasons) |
| Scope isolation (cycles, streaks, lockouts, cooldowns) | `test_cross_scenario_isolation` |
| Durability across fresh instances | `test_restart_durability` |
| Entry-only: ingestion / streak / cooldown recording never blocked | `test_entry_blocks_never_block_exits` |

### l.4 session policy / market filters (`mike_trade/risk/session_policy.py`, `market_filters.py`)

| Control | Covering test(s) |
|---|---|
| Regular-hours-only entry gate | `test_entry_blocks_never_block_exits` (THU allowed) |
| Entry cutoff before the real session close | `test_entry_blocks_never_block_exits` (15:45 ET blocked, `ENTRY_CUTOFF_REACHED`) |
| Minimum hold binds STRATEGY exits only; protective-stop / trailing / risk / daily-loss / manual / close-all / flatten / reconciliation / end-of-day exempt | `test_entry_blocks_never_block_exits` |
| Stale quote fails closed (`QUOTE_STALE`) | `test_entry_blocks_never_block_exits` |

## Targeted pytest commands and observed results

Canonical interpreter is the project venv (`.venv/bin/python`); the system
python has no async plugin and cannot run this suite (see note below).

Command:

```sh
.venv/bin/python -m pytest tests/integration/test_entry_policy_restart.py -v
```

Observed result (2026-10-08 run, copied from the actual run):

```text
tests/integration/test_entry_policy_restart.py::test_cross_scenario_isolation PASSED [ 16%]
tests/integration/test_entry_policy_restart.py::test_restart_durability PASSED [ 33%]
tests/integration/test_entry_policy_restart.py::test_session_reset_clears_cycles_and_next_session_lockouts_but_not_manual PASSED [ 50%]
tests/integration/test_entry_policy_restart.py::test_partial_fill_semantics_end_to_end PASSED [ 66%]
tests/integration/test_entry_policy_restart.py::test_simultaneous_signals_priority_ordered_without_overcommit PASSED [ 83%]
tests/integration/test_entry_policy_restart.py::test_entry_blocks_never_block_exits PASSED [100%]

============================== 6 passed in 0.98s ===============================
```

Per-test targeted runs (each `test_cross_scenario_isolation`,
`test_restart_durability` individually re-run during development) also passed;
no failures remain. No `xfail` markers were needed: no production defect was
found, so per the task contract nothing outside the two allowed files was
edited (`git status` shows no modifications under `mike_trade/`).

Note: an initial run with the harness python (`python -m pytest ...`)
failed to collect with "async def functions are not natively supported"
because that interpreter lacks `pytest-asyncio` (`asyncio_mode` unknown in
its config). This is an environment/interpreter mismatch, not a test
finding; the project venv (`asyncio_mode = "auto"`) runs the suite cleanly.

## Known gaps

- **Provisional fee handling**: `fee_status="estimated"/"unavailable"` streak
  provisionality is covered only by unit tests
  (`tests/unit/test_entry_eligibility.py`); this module always closes cycles
  with final fees.
- **Alpaca paper capabilities**: no broker interaction of any kind is
  demonstrated here by design (task forbids broker/network/credentials); live
  capability mapping remains unverified.
- **Single-writer scope**: the SQLite adapters serialize per scope through the
  ledger/evaluator async locks plus `BEGIN IMMEDIATE`; multi-process writer
  contention (`.writer.lock`) is not exercised.
- **Reserve/margin paths**: `reserve_usd` and `margin_enabled` budget branches
  are covered by unit tests, not by this module.
- **`scope_by_strategy` streaks**: the per-strategy lockout variant is covered
  by unit tests only.
- **BACKTEST/LIVE scopes**: only SHADOW×2 + PAPER isolation is demonstrated.
- **Intraday session edges**: reset is asserted THU→FRI (+weekend hold), not
  across holidays/early closes; cutoff uses the real calendar close for
  2026-10-08 via `SessionPolicy`.
