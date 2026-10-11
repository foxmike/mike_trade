# Strategy Library — 176-Strategy Integration

Integration of the two reviewed strategy-development branches onto `origin/main`
(`f35d38823f34496a7e3972fb5b180f0ded493cb9`). No strategy class was reimplemented or replaced; no engine,
portfolio, broker, authorization or stop code was changed.

## Source branches

| Branch | Commit | Library |
|---|---|---|
| `feature/short-cover-batch-b-reviewed` (SHORT/COVER Batches A+B, reviewed) | `d37d79e2bdf3dee939fcf56ca1fc1f4843bd360f` | 75 BUY, 40 SELL, 28 SHORT, 28 COVER = 171 |
| `feature/buy-sell-backlog-reviewed` (5 BUY backlog strategies, reviewed) | `0f2a54eab58d9906b389d2056ffdbad42d1109a8` | 80 BUY, 40 SELL, 3 SHORT, 3 COVER = 126 |

The two branches touch disjoint strategy modules; the only shared source files are the registration lists
in `app.py` and `research/commands.py` (merged automatically, additions only).

## Final counts (production and research registries identical)

| Role | Count |
|---|---:|
| BUY | 80 |
| SELL | 40 |
| SHORT | 28 |
| COVER | 28 |
| **Total** | **176** |

All 176 strategy ids are unique. Every strategy is registered exactly once in `app.build_runtime` and once in
`research.commands._research_registry` (verified by capturing the production registrations and comparing the
classes with the research registry).

## Merge conflicts resolved

* **`tests/unit/test_short_cover_strategies.py`** (the only textual conflict): the SHORT/COVER branch asserted
  `75/40/28/28 = 171`, the backlog branch `80/40/3/3 = 126`. Resolved to the combined `80/40/28/28 = 176`
  (both sides of the exact-equality assertions kept; neither assertion weakened).
* **Semantic conflicts (no textual conflict, would have failed):** hard-coded per-branch distributions were
  updated to the combined library, keeping their exact-equality form:
  `test_short_cover_batch_a_integration.py`, `test_short_strategies_batch_a.py`, `test_short_strategies_batch_b.py`,
  `test_cover_batch_a.py`, `test_cover_batch_b.py` (BUY 75→80, total 171→176) and
  `test_buy_sell_backlog_batch_a.py`, `test_buy_sell_backlog_batch_b.py`, `test_buy_sell_backlog_review.py`
  (SHORT 3→28, COVER 3→28, total 126→176).
* `test_technical_strategies.py` (BUY 80 / SELL 40 inventory with the backlog id sets) merged cleanly.

## Reviewed fixes preserved

From the BUY/SELL review: VWAP deviation-band accumulators no longer reset on every bar; band
snapshot/restore (zero multiplier, non-`str` session id, malformed snapshots); band VWAP bit-identical to the
production session VWAP and required to match `vwap`/`vwap_prev`; point-in-time per-bar VWAP in `vwap_pullback`;
`support_bounce` invalidation and pivot-to-signal contiguity; first-breakout condition in
`intraday_consolidation_breakout`; non-circular regression fixtures; corrected source attributions
(`docs/BUY_SELL_BACKLOG_REVIEW.md`).
From the SHORT/COVER review: all 50 SHORT/COVER strategies with Gemini's corrections and the shared
registration/count/integration-test fixes (`docs/SHORT_COVER_BATCH_A_REVIEW.md`, `docs/SHORT_COVER_BATCH_B_REVIEW.md`).
The merged tree contains both sets of modules and tests unmodified.

## Integration defects corrected

1. Stale per-branch role-count assertions (above) — the only functional breakage of the merge.
2. Documentation counts: `docs/STRATEGIES.md` §18 now states 80 BUY, 40 SELL, 28 SHORT, 28 COVER (176) and
   points at the backlog and integration documents (the SELL batch-5 sentence is marked as a historical milestone);
   `docs/TECHNICAL_STRATEGY_INVENTORY.md` summary corrected to match its 156 rows: 63 implemented BUY, 23 implemented
   SELL, 44 existing equivalents, 3 pending, 23 deferred (Batch B had added rows without updating the summary).
   Milestone documents of the individual batches keep their own historical totals.

No cross-branch behavioural interaction was found: the branches share no engine, portfolio, indicator or
closed-bar code (the BUY branch adds `indicators/vwap_bands.py` only used by its two VWAP strategies).

## Trading behaviour (unchanged, re-verified)

BUY opens a long, SELL closes it, SHORT opens a short, COVER closes it; BUY and SHORT are armed by distinct
engine-owned eligibility values (`WATCHING_BUY` / `WATCHING_SHORT`) and SELL/COVER carry none. The Market Status
Monitor gates BUY and SHORT entries by state (NORMAL: both; WARNING/CRASH: SHORT only; STABILIZING: neither;
RECOVERY: BUY only) and never blocks SELL, COVER, protective stops or liquidation. Shorting stays opt-in
(`short_selling_enabled`). Live-trading authorization and PAPER/LIVE evaluation are untouched; no orders were placed.

## Focused verification

* New `tests/unit/test_strategy_library_176_integration.py` (14 tests): exact 176 registration and role bases;
  production/research class parity and uniqueness; all four roles composing per symbol (and a wrong-role binding
  rejected); representative BUY (`vwap_pullback`), SHORT Batch B (`short_bearish_engulfing`) and COVER Batch B
  (`cover_obv_breakout`) signals; BUY vs SHORT arming and ungated SELL/COVER; the market-state entry matrix with all
  exit actions always allowed; SHADOW round trips of a real short with a new Batch B cover; deterministic BACKTEST;
  a reviewed backlog BUY entering in BACKTEST while shorting stays gated off; the reviewed VWAP corrections.
* Directly affected existing suites run once: all test files changed by either branch, plus short/cover config,
  engine and SHADOW, market-status and short-market integration, signal/entry arbitration, fixed/shadow stop
  coverage, short BACKTEST execution, strategy library/presets/reports: **1,839 passed**.
  The full repository suite was not run.

## Remaining limitations

* Per-batch milestone documents retain their own historical totals (151, 171, 126) by design.
* Two reviewed-branch limitations carry over: `vwap_pullback` / `vwap_band_reversion` need the session's candles
  from the open (fail closed after a short-history restart), and SHORT/COVER strategies remain advisory signals
  whose locate, borrow, margin and order handling belong to the engine.
* The Murphy chapter citations used by several earlier BUY modules were flagged in the backlog review for a separate check.
