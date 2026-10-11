# BUY Backlog — Final Complex Chart Patterns

Four remaining BUY candidates were investigated against what the strategy contract can actually supply.
Result: **two implemented** (`gap_exhaustion`, `bull_pennant_breakout`) and **two left
`BLOCKED_BY_INFRASTRUCTURE`** (`volatility_contraction_pattern`, `cup_and_handle`).
Library: 82 BUY, 40 SELL, 28 SHORT, 28 COVER = **178** (production and research registries identical).

## What the data contract provides (inspected)

* **Timeframes.** `CandleTimeframe` is 1m/5m/15m/30m/60m. There is no daily or weekly candle. Production builds M1
  only (`CandleBuilder(max_history=500)`; the replay feed supplies M1 and M5), so a strategy sees at most about 500
  M1 bars - 1.3 regular sessions.
* **Session reference.** `SessionReferenceTracker` aggregates complete regular sessions from M1 bars. Until now it kept
  only the single previous session (OHLC). The production warmup already fetches 14 calendar days of 1-minute bars
  into it (about nine sessions) and then discarded all but the previous one.
* **Research data.** Stored research datasets are 1-minute bars; BACKTEST feeds the same tracker from the replayed bars.

## Outcomes

| Candidate | Outcome | ID |
|---|---|---|
| Gap Exhaustion | **IMPLEMENTED** (narrow session-history extension) | `gap_exhaustion` |
| Bull Pennant Breakout | **IMPLEMENTED** (intraday adaptation, labelled) | `bull_pennant_breakout` |
| Volatility Contraction Pattern | **BLOCKED_BY_INFRASTRUCTURE** | — |
| Cup and Handle | **BLOCKED_BY_INFRASTRUCTURE** | — |

No candidate was found to be an existing equivalent, and none was left as a pure source-definition gap.

## 1. Gap Exhaustion — `gap_exhaustion` (implemented)

**Infrastructure change (narrow).** `SessionReference` gains `history`: the bounded
(`MAX_HISTORY_SESSIONS = 10`) chain of fully observed regular sessions, oldest first, ending at `previous`;
`RegularSessionOHLC` gains an optional `volume` (sum of the session's M1 volumes). The chain is always consecutive
exchange sessions: an incomplete or entirely missing session cuts it, so a multi-session trend is never inferred across
a hole; it is empty whenever `previous` is unavailable, and - like every reference field - hidden when the tracker already
holds data later than `as_of`. No new data source, no ingestion change: production warmup and the BACKTEST replay already
feed the tracker. Existing fields and strategies are unchanged.

**Rule.** (The numbers are engineering defaults; see Sources.)
1. *Decline:* the last `trend_sessions` (4) session-to-session closes are each lower than the one before
   (needs `trend_sessions + 1` = 5 consecutive complete sessions), totalling at least `min_decline_pct` (3%) of the first close.
2. *Gap:* the session open is below the previous session's **low** by at least `min_gap_pct` (1%) of the previous close.
3. *Participation:* today's per-minute volume so far is at least `min_relative_volume` (1.5) times the trend sessions'
   per-minute average (0 disables; unknown session volume fails closed).
4. *Confirmation:* the completed M1 close that is the **first** to rise above the previous low (every earlier close of the
   session is at or below it). The signal is that bar; the reversal is therefore observed, not predicted.

Today's bars must run from the 09:30 open with no missing minute (`INSUFFICIENT_DATA` otherwise). Warmup `2` bars plus the
session history. Distinct from `gap_fill` (reclaim of the open after any gap-down), `gap_reversal` (opening range + VWAP) and
`previous_day_low_reversal` (single-bar sweep and reclaim): it adds the multi-session decline, an opening gap beyond the prior
low, abnormal participation and first closure.

**Sources.** Concept only: the exhaustion gap - a gap late in an extended move, usually on heavy volume, that is soon closed
and marks the end of the move - as described in the general chart-pattern literature (Edwards and Magee; Murphy). Those texts
recognise it from the gap closing and give no numeric thresholds; every number above is an engineering choice.

**Limitations.** Needs five fully observed prior sessions (a thin IEX feed that leaves minutes missing will not qualify); M1
history is what the tracker saw (production: about 14 calendar days); one signal per session at most.

## 2. Bull Pennant Breakout — `bull_pennant_breakout` (implemented, intraday adaptation)

**Geometry** (all read from the completed bars *before* the signal bar `t`; swing points are fractals with `strength` bars per
side, so each was confirmed by bars that had already closed):
* *Flagpole:* the pole top P is the latest bar with the highest high of the window; the lowest low within `pole_bars` (10) bars
  up to P is at least `pole_pct` (1.5%) below P's high.
* *Pennant:* `min_pennant_bars` (6) to `max_pennant_bars` (20) bars after P. Upper boundary = last two swing highs at/after P,
  strictly **falling**; lower boundary = last two swing lows after P, strictly **rising**; both extended to `t`, where the upper
  line must still be above the lower (breakout before the apex).
* *Invalidation:* the pennant's lowest low retraces no more than `max_retrace` (50%) of the pole; no close since the first upper
  pivot was above the upper line (first breakout); no close since the first lower pivot was below the lower line.
* *Breakout:* `close[t]` above the extended upper line.

`lookback` (60) bounds the search; `required_bars = lookback + 1`. Needs `lookback > max_pennant_bars + 1` and
`min_pennant_bars >= 4 * strength`.

**Distinctness.** `bull_flag_breakout` has no converging boundaries (parallel/horizontal range); `triangle_breakout` and
`ascending_triangle_breakout` have no flagpole and no duration limit. Tests show a rectangle, a broadening formation and a
flat-topped (ascending) shape do not qualify, and a long pole-less triangle fires `triangle_breakout` but not the pennant. A valid
pennant breakout can also satisfy `triangle_breakout`; the overlap is documented, not hidden.

**Sources.** Pattern concept: a pennant is a short, small symmetrical triangle after a sharp move (the flagpole) that resolves in
the direction of the move (Bulkowski, *Encyclopedia of Chart Patterns*; Murphy; Edwards and Magee). The literature is
qualitative about size and duration, so `pole_pct`, the bar limits and `max_retrace` are this library's intraday engineering
choices - **not** the authors' daily-chart thresholds. Two-point boundaries only.

## 3. Volatility Contraction Pattern — BLOCKED_BY_INFRASTRUCTURE

Minervini's VCP (*Trade Like a Stock Market Wizard*) is a base of successively tighter pullbacks inside a Stage-2 uptrend, with
volume drying up and a pivot breakout. The trend qualification is expressed with 150- and 200-day moving averages and the
52-week range, and the base spans weeks. The contract has no daily history (the longest available context is about 1.3 M1
sessions, or a bounded chain of at most ten recent sessions), so the trend template and the base cannot be evaluated. A Bollinger
squeeze or a narrow-range breakout is not a VCP, and an M1 re-scaling would not be Minervini's pattern; none was built.
**Needs:** daily OHLCV candles for at least one trading year (about 252 sessions) per symbol exposed to strategies as a timeframe,
warmed at start-up and replayed point-in-time in BACKTEST. Even then the contraction count/depth sequence would need an explicit
engineering rule, which should be specified with that data contract.

## 4. Cup and Handle — BLOCKED_BY_INFRASTRUCTURE

O'Neil's cup-with-handle (*How to Make Money in Stocks*) is a multi-week rounded base after an uptrend, with a short shallow
handle and a pivot breakout, defined on daily/weekly charts. The platform supplies no such candles. A V-shaped reversal or a
simple resistance breakout must not be renamed, and an intraday rounded-bottom rule would be a different, unsourced pattern. Even
with data, "roundness" has no published numeric curvature test, so that part would also need an explicit engineering definition.
**Needs:** daily (and ideally weekly) candles covering many weeks of base plus the preceding advance, with point-in-time replay.

## Tests

`tests/unit/test_buy_backlog_final_patterns.py` (18) and additions to `tests/unit/test_session_references.py` (5): history chain
(consecutive, cut at incomplete/missing sessions, bounded, hidden on future data, volume); gap exhaustion trigger and every
negative as a genuine `NO_SIGNAL`, boundaries, fail-closed history/volume/minute cases, the real tracker end to end, and a
multi-session BACKTEST replay; pennant trigger, exact boundary (`108.40` vs `108.41`), pole/duration/retrace limits, premature
breakout, lower-boundary break without a new pivot, apex condition, rectangle/broadening/ascending/triangle lookalikes,
unconfirmed-pivot look-ahead, gaps, warmup, parameters, BACKTEST; registration parity and the data-contract facts that block the
other two. Mutation checks confirmed that removing each rule condition fails at least one test. Existing batch tests with hard-coded
role totals were updated to 82/40/28/28 = 178.

## Remaining limitations
* Pennant/gap thresholds are untuned engineering defaults on M1 data; gap exhaustion needs a fully observed five-session history.
* VCP and Cup and Handle remain unimplemented until a daily-history contract exists.
* `tests/unit/test_backtest_repro.py::test_identical_runs_and_seed_changes_are_content_addressed` fails on the base branch as
  well (wall-clock timestamp in the manifest); unrelated to this work and not changed.
