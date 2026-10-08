# Phase P — SHADOW Restart Ledger Balance & Live-Nonexecution Verification Audit

**Scope:** verification-only acceptance tests for the phase P SHADOW implementation:
restart ledger balance, scenario isolation on restart, live-nonexecution, and
estimated/not-broker-confirmed labeling.
**Test file:** `tests/integration/test_shadow_restart.py`
**Production code modified:** none (verification-only task; `git status` shows no
changes under `mike_trade/` — the only new tracked file besides harness artifacts
is the test file itself).
**Date (UTC):** 2026-10-08
**Interpreter:** `.venv/bin/python` (CPython 3.12; required — the codebase uses
PEP 695 generics and `enum.StrEnum`, which do not parse on the default 3.11).

## 1. What was verified

| Acceptance criterion | Proving test function(s) |
|---|---|
| Restart reproduces cash, positions, open orders, stop state, realized/unrealized P&L, per-symbol session entry counts, loss streaks, cooldown expiry as identical Decimal/UTC values | `test_shadow_restart_ledger_balance` (full pre/post snapshot equality for two scenarios) |
| Starting cash is not re-applied and positions are not reset (two independent scenarios) | `test_shadow_restart_ledger_balance` (reopen uses a deliberately wrong `starting_cash=D("1")`; original `50000` kept, cash stays `49190`; MSFT/beta positions survive) |
| Live-nonexecution: full shadow run with Alpaca constructors/order-submission patched to raise and credential env vars poisoned completes with zero calls | `test_shadow_live_nonexecution` |
| SHADOW ledgers reject PAPER/LIVE account identifiers | `test_shadow_ledger_rejects_paper_live_ids` |
| Every shadow fill/stop record is `estimated=True`, `broker_confirmed=False` with simulator version and feed provenance | `test_shadow_records_estimated_with_provenance` |

Lifecycle coverage in the deterministic quote sequence (`drive_alpha`): buy fill,
partial fill (6 of 10, participation-capped), stop arming (95 = 100 − 5),
trailing-stop update (95 → 97 on a 102 bid), stop-triggered exit at a loss
(realized −60), cash adjustment (+250), loss-streak counter 0 → 1, and a 10-minute
post-sale cooldown expiry.

## 2. Exact focused commands run

From `/home/mike/dev/mike/stocks/python/mike_trade`:

```
.venv/bin/python -m pytest tests/integration/test_shadow_restart.py -v
```

Result: **4 passed in ~0.5–0.7s** (repeat runs identical):

```
tests/integration/test_shadow_restart.py::test_shadow_restart_ledger_balance PASSED [ 25%]
tests/integration/test_shadow_restart.py::test_shadow_records_estimated_with_provenance PASSED [ 50%]
tests/integration/test_shadow_restart.py::test_shadow_live_nonexecution PASSED [ 75%]
tests/integration/test_shadow_restart.py::test_shadow_ledger_rejects_paper_live_ids PASSED [100%]
```

Write-policy check:

```
git status --porcelain
```

shows no modifications under `mike_trade/` (only the new test file plus
`.abracapocus/` harness artifacts). No broad suites, backtests, network access,
or credentials were used.

## 3. Simulator assumptions (what the numbers mean)

* **Quote side:** buys fill against the ask, sells against the bid; bars/candles
  are never fill evidence (`TypeError` if offered).
* **Latency:** 0. **Slippage:** 0 bps + 0 ticks (fill price equals the touched
  quote; stop exits fill at the triggering bid). **Participation:** 1.0 × displayed
  size (the 6-share partial fill comes from a 6-share quote).
* **Stop trigger:** `BID` source, `max_quote_age` 5 s; execution at available bid
  minus `stop_slippage` (0). Regular NY session only.
* **Economics:** zero fees; whole-share quantities; LIMIT entries (100.50/200.50/
  50.50) filled at 100.00/200.00; trailing policy `BROKER_TRAILING_STOP −5`.
* **Feed provenance:** `iex` (not consolidated) throughout; fill-simulator fills
  labeled `iex:not_consolidated`, stop fills and stop evaluations cite the
  triggering `iex:quote(...)`.

## 4. Guarantees and evidence

* **Restart balance:** pre/post snapshots compare cash, starting cash, realized
  P&L, unrealized P&L and equity under fixed marks, full position detail, full
  open-order tuples, restored `ShadowStopState`s, session entry counts, loss
  streaks, cooldown expiries, and closed-cycle counts — `alpha_after == alpha_before`,
  `beta_after == beta_before`, `alpha_after != beta_after`. Exact economics
  asserted: cash `49190`, realized `-60`, unrealized `50`, beta cash `6700`.
* **Isolation:** `restart-alpha` and `restart-beta` run on one shared WAL file in
  separate ledger streams; alpha's AAPL loss (streak 1, cooldown set) leaves beta's
  AAPL streak 0 with no cooldown.
* **Nonexecution:** `AlpacaHttpClient.__init__`, `AlpacaOrderService.__init__`,
  `AlpacaOrderService.submit_order`, and `AlpacaAccountService.__init__` are
  patched to count-and-raise, and all paper/live/data credential env vars
  (`ALPACA_*`, `APCA_*`, `MIKE_TRADE_ALPACA_*`) are poisoned; a full shadow run
  (strategy-driven entry, fill, stop arm + trigger, 10-share position opened and
  closed) completes with all four counters at 0.
* **Mode rejection:** `ScenarioConfig` refuses `PAPER`/`LIVE` (any case) as
  `scenario_id` or `account_id`; `ShadowLedger` raises `ModeIsolationError` for
  PAPER/LIVE scopes.
* **Labeling:** every `SimulatedFill`, `ShadowStopState`, ledger envelope, fill
  event, and stop audit record asserts `estimated is True`,
  `broker_confirmed is False`, `simulator_version == "shadow-sim/1"`, with
  non-empty feed/provenance.

## 5. Gaps and open defects

* **None found — no open defects.** All four acceptance tests pass against the
  current implementation on a clean temporary database.
* Known non-issues recorded for future readers: in-memory quote marks
  (`update_quote`) are inputs, not ledger facts, so post-restart unrealized P&L is
  compared under explicitly supplied marks; stop order/broker IDs embed random
  hex but are ledger-persisted, so pre/post equality still holds exactly.

## 6. Required statement

**Shadow results are estimates, not broker-confirmed, and imply no profitability
claim.** Every record produced here carries `estimated=True` /
`broker_confirmed=False` with simulator version `shadow-sim/1` and feed
provenance; the asserted P&L figures (−60 realized, 50 unrealized under fixed
marks) validate ledger accounting only — they say nothing about real-market
fills, costs, or future performance.
