# Test Plan — Layers, Commands, and Acceptance Mapping

Every `make` target named here exists in the `Makefile`; every pytest path
named here exists in this checkout. Paths that do not exist are explicitly
marked as such and are never presented as runnable.

## 1. Test layers

| Layer | Command | What it covers | Broker / credentials |
| :--- | :--- | :--- | :--- |
| Unit | `make test-unit` (`pytest tests/unit -q`) | Policy math and state machines: budgets, reservations, sizing, eligibility, lockouts, cooldowns, stops, trailing, order FSM, migrations, backup/restore, control idempotency, `make` target surface (`tests/unit/test_make_targets.py`) | None; hermetic |
| Integration | `make test-integration` (`pytest tests/integration -q`) | Wiring across modules with fakes/replay: order lifecycle, native-stop protection, stepped-trailing lifecycle, stop/close races, reconciliation, shadow isolation/restart, dashboard API/offline/read-only behavior, scheduler fanout, entry-policy restart | None; fixture fakes only |
| Contracts | Part of the default suite via `tests/contracts/` collection where configured | Broker-contract behavior (orders, capabilities, trade updates, HTTP mapping, signal/trading contracts) against recorded fixtures and doubles | None unless `paper`-marked (below) |
| Offline end-to-end | `pytest tests/end_to_end -q` (credentials-free) | Full product cycle and recovery on replayed data with the real runtime, persistence, and dashboard read service | None; replay feeds only |
| Credential-gated paper | `make test-paper` (`pytest -m paper -q`) | Live HTTP against the Alpaca **paper** endpoint only | Requires paper credentials **plus** explicit opt-in (see §4) |

The bounded default suite is `make test`
(`pytest tests/unit tests/integration -q`). Lint and types:
`make lint` (`ruff check .`), `make typecheck` (`mypy mike_trade`).
Environment setup: `make install` (`uv sync --frozen` plus CLI `doctor`).

## 2. How to run targeted tests

```sh
make test-unit                    # unit layer only
make test-integration             # integration layer only
make test-paper                   # paper-gated only (skips without credentials+opt-in)
python3 -m pytest tests/end_to_end -q                      # offline end-to-end
python3 -m pytest tests/unit/test_budget_reservations.py -q
python3 -m pytest tests/integration/test_native_stop_protection.py -q
python3 -m pytest tests/contracts/test_broker_capabilities.py -m paper -rs
```

`pytest -rs` surfaces skip reasons for gated tests. No test target places,
replaces, or cancels live orders, and no target requires paid data.

## 3. Acceptance-scenario mapping (final pass)

| Required acceptance scenario | Runnable test file(s) in this checkout | Notes |
| :--- | :--- | :--- |
| Full product cycle (signal → decision → intent → order → fill → protection → exit → report) | `tests/end_to_end/test_full_product_cycle.py` | Credentials-free `vwap_reference` product-cycle coverage on replay feeds; asserts live-trading authorization refusal paths |
| Recovery and offline behavior (restart mid-cycle, reconciliation, locks, shutdown, dashboard recovery) | `tests/end_to_end/test_recovery_offline.py` | Covers restart-mid-cycle Decimal-state preservation, startup reconciliation, writer locking, shutdown semantics, and dashboard reads while the engine is down |
| Protective / live-guard behavior (fixed, native trailing, stepped trailing, stop/close races) | `tests/integration/test_native_stop_protection.py`, `tests/integration/test_stepped_trailing_lifecycle.py`, `tests/integration/test_stop_close_races.py`, plus unit `tests/unit/test_fixed_stop_coverage.py`, `tests/unit/test_stepped_trailing.py`, `tests/unit/test_protection_policy.py` | The path `tests/end_to_end/test_protective_live_guards.py` **does not exist** in this checkout and is not runnable; the integration/unit files above are the implemented protective-guard coverage. No live orders are involved |
| Alpaca paper cycle (credential-gated paper verification) | `tests/contracts/test_broker_capabilities.py` (the `paper`-marked test `test_paper_readonly_account_clock_asset`) | Read-only paper check: asserts PAPER-only client, exercises account/clock/asset reads. The path `tests/paper_broker/test_alpaca_paper_cycle.py` **does not exist** in this checkout and is not runnable; paper order-flow verification is **pending** until performed by the operator (see §4) |

Supporting coverage frequently consulted during the final pass:
`tests/integration/test_order_lifecycle.py`,
`tests/integration/test_reconciliation.py`,
`tests/integration/test_entry_policy_restart.py`,
`tests/integration/test_dashboard_offline.py`,
`tests/integration/test_dashboard_readonly.py`,
`tests/integration/test_dashboard_api.py`,
`tests/unit/test_backup_restore.py`,
`tests/unit/test_engine_lifecycle.py`.

## 4. Paper gating and approval opt-in

The paper suite is deselected from default runs. It executes only when the
operator provides paper credentials **and** sets the explicit opt-in:

```sh
export ALPACA_PAPER_KEY_ID='<paper key>'
export ALPACA_PAPER_SECRET_KEY='<paper secret>'
export MIKE_TRADE_ALPACA_PAPER_IT=1
python3 -m pytest tests/contracts/test_broker_capabilities.py -m paper -rs
```

Rules:

- Without the opt-in variable and both paper credentials, the tests skip
  (they never fail safe into running against a broker).
- The implemented paper test is read-only (account, clock, asset lookup).
  It never references live endpoints and never places, replaces, or
  cancels orders.
- Paper order-flow verification (observing real paper fills for
  previously `UNPROVEN` capability combinations and recording them with
  `record_paper_verification()`) is **pending until performed by the
  operator**. This document does not claim it has passed.
- Paper fills, when they exist, do not prove live performance (see
  `docs/ALPACA.md` §7).

## 5. Live orders and the final comprehensive pass

- **No automated live orders are ever run.** No test suite, recipe, or
  CI step submits, replaces, cancels, or otherwise mutates live orders.
  LIVE mutations additionally require `--confirm-live YES` /
  `CONFIRM_LIVE=YES` plus `live_authorized=True` in code, and the paper
  matrix never auto-promotes live.
- **Final comprehensive pass policy:** exactly one deliberate final
  comprehensive pass (`make test`, the offline end-to-end directory, and —
  only with operator authorization, credentials, and opt-in — the
  `paper`-marked selection) is run, and only after explicit operator
  authorization. It is not re-run automatically, it is not a gate that
  rewrites execution history, and its results are recorded as observed
  evidence, not as proof of future performance.
