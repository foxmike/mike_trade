# Strategy library — 184-strategy pattern review

Branch `feature/strategy-library-184-pattern-reviewed`, built on
`origin/feature/buy-sell-backlog-cd-reviewed` (`251a33e9e3963e44bc319fdde20066885dfb7ab1`).

## Source commits

| Branch | Commit |
|---|---|
| base: reviewed 182 library | `251a33e9e3963e44bc319fdde20066885dfb7ab1` |
| input: `origin/feature/buy-backlog-final-patterns` | `4a170ad83aac1a6eaffe120886cfa34e34c68313` (adds `gap_exhaustion`, `bull_pennant_breakout`, the `SessionReference.history` extension) |

## Final counts

| Role | Count |
|---|---:|
| BUY | 87 |
| SELL | 41 |
| SHORT | 28 |
| COVER | 28 |
| **Total** | **184** |

Production (`app.build_runtime`) and research (`_research_registry`) register the same 184 unique classes (checked
by capturing `StrategyRegistry.register` during `build_runtime`). All six batch C/D strategies, the 50 SHORT/COVER
strategies and their reviewed corrections are present; the Market Status Monitor tests (BUY and SHORT entries gated
independently, SELL and COVER unaffected) pass unchanged.

## Merge conflicts

* `mike_trade/app.py`, `mike_trade/research/commands.py`: import and class-list hunks; both sides kept, each class
  registered once.
* Exact-count assertions across the affected test files (the input branch was 82/40/28/28 = 178, the base 85/41/28/28 = 182): set to
  87/41/28/28 = 184. `test_technical_strategies.py` also gained the `FINAL_PATTERN_BUY` set beside `BACKLOG_BUY_C/D`.
* `docs/TECHNICAL_STRATEGY_INVENTORY.md`: the input branch's stale summary and its `First Pullback Buy` =
  `REQUIRES_SOURCE_DEFINITION` row were discarded (the reviewed base has it implemented); its `Gap Exhaustion`,
  `Bull Pennant Breakout`, VCP and Cup-and-Handle rows were kept. Summary recomputed from the rows: 70 BUY + 24 SELL
  implemented, 48 equivalents, 0 pending, 14 deferred = 156.
* `docs/STRATEGIES.md` §18 reconciled to 87/41/28/28 = 184.

## Session-history findings (`SessionReferenceTracker`)

Checked against hard-coded exchange dates and an independent oracle (random missing minutes, missing sessions,
early closes and holidays over mid-November 2026 to January 2027):

1. A session enters the history only when the first bar of a *later* session arrives, so the current session is never
   in its own history. Its OHLC and volume are accumulated bar by bar from M1 candles, `open` only from the bar that
   starts at the session open.
2. Every expected minute must be present, in order, with one feed identity. A missing first/last/middle minute, a late
   start, a stray feed change or a missing session invalidates the session; the invalid or missing session cuts the chain
   (everything older is dropped), so a trend is never inferred across a hole.
3. Order is chronological by construction; continuity uses the exchange calendar, so weekends, Thanksgiving, Christmas,
   and New Year's Day do not break a chain, early closes (Black Friday, Dec 24, July 3) are complete at
   13:00, and a really absent session does.
4. Point-in-time: `reference(as_of)` returns nothing derived from bars later than `as_of` (everything fails closed), so a
   replay cannot see future sessions or the unfinished current session. Overlapping REST warmup and live bars are
   idempotent. Pre-market/after-hours bars are ignored. `previous` and its OHLC for existing strategies are unchanged
   (verified against independently computed OHLC), and a `SessionReference` built without `history` stays valid.
5. Zero volume: a zero-volume session is recorded with volume 0 and `gap_exhaustion` treats a zero baseline as
   `INSUFFICIENT_DATA` (unless the volume test is disabled); unknown volume is the same.
6. Snapshot/restart: the tracker is not persisted; SHADOW rebuilds it from the 14-calendar-day warmup (about nine
   sessions, the first one partial and dropped), so no snapshot logic is needed.

### End-to-end readiness of `gap_exhaustion`

* **SHADOW.** The tracker is fed by warmup and the live stream and handed to `ContextBuilder`; a test drives
  `ProductionMarketData` through the same calls as `warm()`/`_on_bar` and the strategy receives all five prior
  sessions and signals. The 14-day warmup yields about eight to nine complete prior sessions (holiday weeks included),
  more than the five needed; `MAX_HISTORY_SESSIONS = 10` caps `trend_sessions = 8` + 1 at nine.
* **Real defect found and fixed — mid-session restart.** `warm()` fed the candle builder only the last
  `market_data_warmup_bars + 1` (61) bars, so after a restart during the session the context had no complete-from-open
  prefix and `current_session_bars` (the first-closure test) failed closed until the next day. `app._warmup_candle_bars`
  now extends that slice back to today's regular-session open on a trading day. It only returns bars the venue reported.
* **BACKTEST.** The runner feeds the tracker only from events inside `[window_start, window_end]`. A test across a
  weekend and Thanksgiving signals with the full history, and the same data with a window starting after two of the
  sessions never invents them. Limitation: a window must itself contain `trend_sessions + 1` prior sessions; the runner
  has no pre-window reference warmup (adding one would interact with holdout locks and was not changed).
* **IEX data.** Fail-closed by design: one missing minute in a session removes it, and so everything older, from the
  chain. Liquid ETFs (SPY/QQQ/IWM) normally have every minute; thinly traded symbols often will not and will rarely
  become eligible. Nothing is interpolated.
* **500-bar context.** Enough for a whole regular session (390 bars); session history does not come from candles.
* **Observation (not changed).** `CandleBuilder.on_minute_bar` finalises a minute bar only when the next minute's bar
  arrives, so SHADOW strategies see a completed candle about one minute after it ends. That is existing engine
  behaviour affecting every strategy equally and is out of scope here.

## Mathematical findings and defects

`gap_exhaustion`
* Decline: four strictly lower consecutive closes over five consecutive sessions, drop measured on the first close:
  correct. Opening price: the tracker's `current_open` is the first regular-session bar's open; the strategy now also
  refuses to evaluate if it differs from the open of the bars it is given.
* **Defect — gap denominator.** The gap was measured as a percentage of the previous *close*, while the rule, the
  methodology text and the task say "at least 1% below the previous *low*". Corrected to a percentage of the low
  (the old test accepted open 98.99 for 1% where the low was 100; the corrected rule accepts 99.00 and rejects 99.01).
* **Defect — first minute could never confirm.** The strategy required two bars, so a session whose very first
  minute already closed above the previous low (the first qualifying candle) was skipped, and the second bar then
  failed the first-closure test. One bar now suffices (`warmup_bars = 1`).
* Volume: today's mean per-minute volume against the five trend sessions' total volume divided by their real
  minutes (an early-close session counts 210, not 390 — a test fails if 390 is assumed); zero/unknown baseline fails
  closed. Early-session volume is front-loaded, so the ratio is biased upward at the open (an engineering default).
* First-closure edge: the signal bar is the first completed close above the previous low; later bars and replays of
  the same bar do not repeat it.

`bull_pennant_breakout`
* Reviewed against an independent exact-fraction oracle over 300 noisy converging series (both signal and
  no-signal outcomes exercised): pole height, duration, retrace, swing-pivot confirmation (all pivots come from bars
  before the signal bar), strictly falling highs and rising lows, line extrapolation, apex condition, first-breakout
  and lower-boundary invalidation all agree. Rectangle, broadening, ascending (flat top), falling channel and
  pole-less shapes do not fire; a valid pennant breakout can also satisfy `triangle_breakout` (documented overlap).
  Appending later bars never changes an earlier decision; a replay of the signal bar is suppressed.
* **Defect — session boundaries.** Adjacency was the only check, so pre-market bars could form the pole or pennant.
  The pole-plus-pennant region must now lie wholly inside the signal bar's regular session (same guard as the batch
  C/D corrections); a pennant wholly inside the second session still works after an overnight gap before it.
* Documentation keeps the explicit statement that this is an intraday adaptation with engineering defaults, not the
  authors' daily-chart parameters.

## Focused tests

* `tests/unit/test_strategy_library_184_pattern_review.py` (20 tests): 184 counts/parity/preservation; tracker across
  weekend, Thanksgiving + half day, Christmas/New Year, missing session/minute/first/last minute, mixed feed, zero
  volume, point-in-time hiding, oracle fuzz; gap exhaustion early-close denominator, low-based gap, first-minute
  confirmation, zero/unknown volume, open disagreement, hole in today's prefix; SHADOW composition and warmup,
  mid-session restart (old slice fails closed, new slice signals), warmup-slice helper; BACKTEST replay across
  Thanksgiving and a too-short window; pennant oracle fuzz, causality, lookalikes, regular-session guard.
* Updated `test_buy_backlog_final_patterns.py` (gap boundary now against the low) and all exact-count tests.
* Run: the new tests plus every directly affected suite (batch C/D, short/cover, market status, session references,
  library integration, technical strategies): 1,723 passed. The full repository suite was not run. Six of the new
  tests fail on the pre-review source.

## Remaining limitations

* Thresholds of both strategies are untuned engineering defaults; the published sources are concept-level only.
* `gap_exhaustion` needs five fully observed prior sessions and a minute-complete current session; sparse IEX symbols
  will seldom qualify. One signal per session at most.
* BACKTEST has no pre-window reference warmup; SHADOW depends on a 14-day warmup and a complete first minute.
* Pennant: two-point boundaries, pole top must be the highest high of the 60-bar window (an older higher peak hides a
  later pennant until it ages out), and the pennant can overlap `triangle_breakout`.
* Other library strategies still rely on adjacency alone for pre-market bars (only the batch C/D strategies and the
  pennant were guarded).
* Candle finalisation latency of one bar in SHADOW (above) is unchanged.
