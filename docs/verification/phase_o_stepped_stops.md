# Phase O — Stepped Stops Verification Audit

**Scope:** stepped trailing protection lifecycle and fault regression tests.
**Test file:** `tests/integration/test_stepped_trailing_lifecycle.py`
**Production code modified:** none (verification-only task; `git status` shows no
changes under `mike_trade/` and no changes to o.1–o.4 test files).
**Date (UTC):** 2026-10-08
**Interpreter:** `python3.12` (required: the codebase uses PEP 695 generics and
`enum.StrEnum`, which do not parse on 3.11).

## 1. Requirement → test mapping

| Phase-O requirement | Proving test function(s) |
|---|---|
| Monotone ratchet (stop never moves down) | `test_stop_never_moves_down_seeded_path`, `test_canonical_lifecycle_exact_stops_and_replace_counts` |
| Canonical example (entry 100, distance 2, step 1 → 98 / 99 / 101) | `test_canonical_lifecycle_exact_stops_and_replace_counts` |
| Rounding-before-compare | `test_rounding_before_compare_never_submits_equal_or_lower`, `test_stop_never_moves_down_seeded_path` |
| Broker-confirmed effectiveness (only CONFIRMED moves protection) | `test_canonical_lifecycle_exact_stops_and_replace_counts`, `test_parity_pure_ratchet_vs_confirmed_sequence`, `test_rejected_replace_retains_stop_blocks_entries`, `test_unknown_replace_lookup_reconciliation_resumes` |
| Intent-before-submit (persist precedes broker call) | `test_canonical_lifecycle_exact_stops_and_replace_counts` (via `TrailHarness` intent recorder asserting persist-then-replace ordering through the coordinator), `test_unknown_replace_lookup_reconciliation_resumes` |
| No duplicate pending (coalescing) | `test_no_duplicate_pending_under_rapid_highs` |
| Unknown / rejected handling | `test_rejected_replace_retains_stop_blocks_entries`, `test_unknown_replace_lookup_reconciliation_resumes` |
| Fill-during-replace (full and partial) | `test_stop_fill_during_replace_closes_cycle`, `test_partial_stop_fill_during_replace_protects_remainder` |
| Close race / no-short | `test_discretionary_close_races_pending_replace_no_short` |
| Orphan detection (stop after flat) | `test_orphan_stop_after_flat_detected` |
| Restart (blocked until reconciliation, resume from watermark/anchor) | `test_restart_blocks_until_reconciliation_then_resumes` |
| Offline `TRAIL_STALE` (retain stop, no advance, no submit) | `test_engine_offline_stale_retains_stop_and_blocks_advance` |
| Fail-closed capability (unverified replace) | `test_fail_closed_capability_without_verified_replace` |
| Protection only after confirmed positive fill; protected qty = remaining filled qty | `test_protection_requires_confirmed_positive_fill`, `test_partial_stop_fill_during_replace_protects_remainder` |
| Parity (pure ratchet vs persisted/confirmed sequence) | `test_parity_pure_ratchet_vs_confirmed_sequence` |

Notes on coverage details:

* Canonical test asserts confirmed stops are exactly `Decimal(98)`, `Decimal(99)`,
  `Decimal(101)`; the stop remains `101` after a decline; and exactly two replace
  requests (`99`, `101`) occur — none for high `100.80` or the decline.
* Seeded regression (`random.Random(20261008)`, 80 highs) asserts the confirmed
  series is non-decreasing and every replace-call price is strictly above the
  prior confirmed stop after cent-floor rounding.
* Rejected replace asserts the last verified stop (`98`) is retained, the outcome
  carries `TRAIL_UPDATE_UNCONFIRMED`, and `entries_blocked` +
  `reconciliation_required` are true.
* Unknown replace asserts advancement stays blocked (`RuntimeError` on
  `record_candidate`) until lookup-based reconciliation confirms `99`, after which
  the next high advances to `101`.
* Full stop fill asserts `filled=10`/`remaining=0` with zero broker replace calls
  and the last verified stop retained; partial fill asserts `filled=3.5` /
  `remaining=6.5` with `remaining == 10 - filled`.
* Close race asserts exactly one exit at `<= 10` and broker position `>= 0`
  (fake-broker no-short guard active) while a replacement is pending.
* Restart asserts post-reload `requires_reconciliation`, `RuntimeError` on
  `record_candidate`, persisted `high_watermark=103.20` / `ratchet_anchor=103`,
  and post-`reconcile` advancement to candidate `102`.
* Offline test asserts status `TRAIL_STALE`, retained stop `99`, unchanged
  `high_watermark=101`, and zero new intents/replace calls for high `110`.

## 2. Targeted commands and observed results

Exact commands run (from `/home/mike/dev/mike/stocks/python/mike_trade`):

```
python3.12 -m pytest tests/integration/test_stepped_trailing_lifecycle.py -q
python3.12 -m pytest tests/integration/test_stepped_trailing_lifecycle.py -v
python3.12 -m pytest tests/integration/test_stepped_trailing_lifecycle.py --collect-only -q
```

Observed results:

* `-q`: `15 passed in 2.11s` (also `15 passed in 2.09s` on a repeat run).
* `-v`: all 15 tests `PASSED`, 0 failed, 0 skipped, 0 xfailed
  (see mapping table for the full name list).
* `--collect-only`: 15 tests collected.

No test is marked `xfail`: the suite exposed **no production defect**, so there
is nothing to record as a defect and no `xfail(strict=True)` was needed. If a
future run exposes a production defect, the policy is to mark that case
`xfail(strict=True)` with a reason and append the defect here — not to fix
production code in this task.

## 3. Limitations (explicit)

1. **Fakes are not broker integration evidence.** All broker boundaries are
   in-memory fakes (`InMemoryReplacePort`, `FakeCloseBroker`, scripted
   `ReplacementOutcome` values). Passing here proves the wiring, sequencing, and
   regression properties of the ratchet/coordinator/store composition — not that
   any live or paper broker behaves the same way.
2. **Alpaca paper verification is credential-gated and not performed here.** No
   credentials were accessed, and no live or paper orders were created. Paper
   verification remains pending outside this task.
3. **No profitability claim is made.** These tests assert protection mechanics
   (monotonicity, confirmation gating, race safety, persistence) only. They say
   nothing about returns, win rates, or strategy quality.

## 4. Change blast radius

* Created: `tests/integration/test_stepped_trailing_lifecycle.py`
* Created: `docs/verification/phase_o_stepped_stops.md` (this file)
* Modified production files under `mike_trade/`: **none**
* Modified o.1–o.4 test files: **none**
* Planning artifacts (`PLAN_SKELETON.json`, etc.): untouched
