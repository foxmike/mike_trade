# Daily / Weekly Historical Data and the Two Daily-Pattern Strategies

Branch `feature/daily-weekly-pattern-strategies` (base `feature/strategy-library-176-integrated`, 569da60).
This adds completed **D1** (daily) and **W1** (weekly) candle support to the existing market-data
path and implements two BUY strategies on top of it: `volatility_contraction_pattern` and
`cup_and_handle`. The library is now 82 BUY, 40 SELL, 28 SHORT, 28 COVER = **178**.

No second framework was created. D1/W1 are two more members of `CandleTimeframe`, they are
delivered through the existing `MarketContext` / `ContextBuilder`, SHADOW warms them through
`ProductionMarketData`, and BACKTEST replays them through `BacktestRunner`. A strategy that does not
ask for D1/W1 sees exactly the same context and produces exactly the same decisions as before.

## 1. Contracts

### 1.1 Candle types

* `CandleTimeframe.D1 = "1d"` and `CandleTimeframe.W1 = "1w"`. Their span is calendar-defined, so
  `.duration` raises for them (it is only defined for intraday frames). `INTRADAY_TIMEFRAMES` and
  `SESSION_TIMEFRAMES` partition the enum; `CandleBuilder` refuses session frames (D1/W1 are never
  built from live ticks).
* A **D1 candle** spans the regular session open to the regular session close of one NYSE trading
  day. `Candle.__post_init__` validates the span against `scheduling.calendar`, so holidays are
  rejected, early closes are honoured (13:00 ET close), and a candle that does not equal its
  session is refused.
* A **W1 candle** spans the first session open of an ISO week to its last session close. Weeks
  shortened by holidays are valid; the candle's own span says where the week ended.
* `PriceBasis.RAW` only. Adjusted daily bars are refused (section 3).

### 1.2 Point in time

A D1 candle becomes visible when `candle.end + settle_delay <= as_of` (`settle_delay` = 1 hour: the
provider's end-of-day bar is final and the day's last M1 bars have been consumed). Therefore:

* During the session, the developing day is never visible, in any form. There is no "partial day".
* A W1 candle is **derived from D1** (`aggregate_weeks`) and is visible only when **every trading
  session of that week is present** and the last of them has settled. A week with a missing session is
  not emitted rather than emitted short. On Friday intraday the visible newest week is the previous
  one; it appears after Friday settles.
* `as_of` is the SHADOW clock in production and `ev.available_at` in BACKTEST, so both paths use
  the same function (`SessionCandleStore.get_completed_candles(symbol, tf, limit, *, as_of)`).

### 1.3 Declaring the need

`StrategySpec.history_bars = ((D1, 252),)` declares how many completed sessions of a frame the strategy needs.
A strategy that lists D1/W1 in `required_timeframes` must declare it, and its `trigger_timeframe` must be
intraday (so a signal is always dated on a completed M1 bar). `_Strategy.required_history(params)` turns
parameters into the actual requirement (for example `base_sessions=250` needs 253 sessions).

## 2. Architecture

| Piece | Where | Role |
|---|---|---|
| `SessionCandleStore` | `market_data/session_candles.py` | Bounded (640 sessions/symbol) per-symbol D1 store. First bar wins; a later differing bar is counted as a conflict and ignored. A provenance lock refuses mixing feeds for one symbol. Serves D1 and derived W1 point-in-time. `fingerprint()` hashes the stored history. |
| `daily_candle_from_bar` / `settled_daily_candles` | same | Alpaca daily bars are stamped at midnight New York; this converts to the session span and refuses any other stamp, any non-session day, and any non-RAW basis. |
| `ContextBuilder(session_candles=...)` | `market_data/context.py` | Merges D1/W1 from the source with `as_of`; raises if a frame is requested but no source exists. |
| `ProductionMarketData.configure_history` | `app.py` | SHADOW: per-symbol frames and session counts, built from the bound strategies (`VwapReferenceLifecycle.history_requirements()`). |
| `AlpacaProductionMarketData` | `app.py` | Warm-up fetch and one background refresh per settled session. |
| `BacktestRunner(session_candles=...)` | `backtest/runner.py` | Supplies D1/W1 as of each event's `available_at`; the store fingerprint is part of the run identity. |
| `HistoricalDatasetRepository.acquire_daily` / `daily_candles` | `research/datasets.py` | Immutable `1d` snapshots with declared coverage. |
| `research-data acquire --timeframe 1d` | `research/commands.py` | CLI acquisition. |

## 3. Price basis and corporate actions

**Policy: RAW daily bars, consistent with RAW M1.** The system trades and replays RAW prices. Mixing
split-adjusted daily candles with RAW M1 bars would put the pivot (daily scale) and the breakout bar (M1
scale) on different bases, and adjustment factors computed after a split are *future* information for any
decision before that split. Alpaca adjustment `all`/`split` is therefore refused (`acquire_daily` and the
adapter both require `raw`).

The cost: a split inside the lookback appears as an overnight discontinuity in RAW daily bars. This is
**not** corrected; it is *detected*. `suspect_corporate_action` flags any session whose open differs from the
prior close by more than `max_overnight_move_pct` (default 25%), and both strategies then return
`INSUFFICIENT_DATA` (fail closed). A genuine >25% gap trips the same guard, which only suppresses a signal.
The guard is a heuristic, not a corporate-action feed; a split smaller than the threshold (for example 5:4) is
not detected and is listed under limitations. No future corporate-action information is consulted anywhere.

## 4. Provider findings (Alpaca)

* Used: the existing `AlpacaHistoricalBars` adapter and the existing REST client. No new broker
  dependency; credentials come from the existing environment mechanism; tests use an in-memory fake.
* Daily bars (`timeframe=1Day`, `adjustment=raw`) are expected at midnight New York of the trading date.
  The adapter converts this to the session and **fails closed** on any other stamp.
* **Not verified live.** This work had no network access, so the IEX free-feed daily capability (that daily
  bars exist for the requested symbols, how far back, and the stamp) was *not* confirmed against Alpaca.
  Nothing assumes it: if the feed does not return conforming bars, SHADOW warm-up leaves the symbol without
  D1 history, the strategies emit no signal, and acquisition raises. A live check should be made before
  relying on the strategies in SHADOW (`research-data acquire --timeframe 1d` is the quickest way).
* IEX volume is a fraction of consolidated volume. Volume ratios in the strategies (dry-up, breakout) are
  ratios of IEX volume to IEX volume, so they stay self-consistent, but absolute IEX volume is small and noisy
  for thin symbols.
* Weekly bars are **not** fetched: they are derived from D1, so there is one source of truth and no
  second provider stamp convention.

## 5. SHADOW

* `build_runtime` collects `history_requirements()` from the bound strategies and calls
  `market_data.configure_history(...)`. If the market-data service cannot supply history it raises
  `ConfigurationError(STRATEGY_HISTORY_UNSUPPORTED)` instead of running a daily strategy blind.
* `warm()` fetches daily history once per configured symbol (`timeframe=1Day`, RAW, configured feed) after the M1 warm-up and
  ingests only settled sessions; today's developing bar, if returned, is discarded by the store.
* `_on_bar` schedules a refresh when the newest settled session is newer than the store (`needs_refresh`). The
  refresh is a single-flight background task, throttled to one attempt per 5 minutes per symbol, off the
  evaluation path. A failure is logged and swallowed; the stored history is unchanged and the staleness
  guard (`history_issue`: "daily history is not up to date") makes strategies fail closed.
* Duplicate requests are avoided by the throttle, the single-flight set, and ingest idempotence. Unconfigured
  symbols and M1-only strategies make no daily requests.
* Memory: at most 640 daily candles per symbol plus a derived weekly cache; the context carries only the
  requested number of sessions (252 / 241 by default).

## 6. BACKTEST

* `BacktestRunner(session_candles=store)` serves D1/W1 to each evaluation as of `ev.available_at`. A
  D1/W1-requiring strategy without a source raises `ValueError` before the run starts.
* The store fingerprint is added to the config hash, so a different daily history is a different run.
* The research CLI loads the immutable `1d` snapshot (`_daily_history`) only when a selected strategy
  needs D1/W1, with enough lookback before the window start; missing coverage raises rather than
  guessing. Runs of M1-only strategies do not touch any of this (verified: the same decisions with and
  without a store attached).
* `results_hash` embeds a per-execution uuid in this codebase (pre-existing), so determinism is verified on
  the ledgers (evaluations, signals, intents, fills, equity curve) and `run_id`.

## 7. Signal timing contract (both strategies)

1. The pattern is computed from the **completed D1 candles through the last settled session only**. It is
   cached per settled session in strategy state (`daily_pattern`, keyed by session and parameter signature),
   so it is not recomputed on every M1 bar and cannot change within a session.
2. The trigger is the **first completed M1 close of the current session above the pivot**. The signal is
   dated on that M1 bar (`bar_time`), strictly after the last D1 candle's end. Equality with the pivot is not a
   break. A gap-up open fires on the first bar of the session.
3. A close above the pivot on any earlier day yields `already_broken_out`; within the session, after the first
   break, later bars do not repeat (`last_signal_bar` state).
4. Optional breakout-volume test: the session's volume so far must be at least
   `breakout_volume_ratio` times the average daily volume of the last 50 sessions, pro-rated by the fraction of the session elapsed
   (set the ratio to 0 to disable).
5. The pivot is defined only from sessions already complete; swing highs need `strength` bars on each side, so
   the newest confirmable swing lags by `strength` sessions. There is no future pivot and no hindsight.

## 8. `volatility_contraction_pattern`

Source concept: Mark Minervini, *Trade Like a Stock Market Wizard* (the trend template and the VCP). The
published description is qualitative about counts and ratios; **every threshold below is an engineering
default, not a published number**, except the trend-template percentages, which are Minervini's stated values.

1. **Trend template** (completed D1): close > 150 and 200 SMA; 150 > 200 SMA; 200 SMA rising versus 21 sessions
   earlier; 50 > 150 and 200 SMA; close > 50 SMA; close >= 130% of the 252-session low; close within 25% of the 252-session
   high.
2. **Contractions**: swing highs (fractal, `strength` 2) and the lowest low after each; contraction depth =
   (high - low) / high. Take the latest run of 2..6 contractions in the last `base_sessions` (120) whose
   highs are within 10% of one another, each depth <= 0.85 x the previous, first <= 35%, last <= 10%, every depth
   >= 1%.
3. **Volume dry-up**: mean volume of the last 10 sessions <= 0.9 x mean of the last 50.
4. **Pivot**: the high of the last contraction; the last close must be below it, but within 5% of it.
   Zero-volume baselines are rejected (`no_volume`).

Rejection reasons are specific: `too_few_swing_highs`, `no_pullback_after_last_swing_high`, `contractions_not_successively_smaller`,
`contraction_too_shallow`, `first_contraction_too_deep`, `last_contraction_too_loose`, `contraction_highs_drift`, `base_length`,
`already_broken_out`, `far_from_pivot`, `insufficient_volume_history`, `no_volume`, `volume_not_drying_up`, or the failed trend-template condition.

Parameters (defaults): `fast_ma` 50, `mid_ma` 150, `slow_ma` 200, `rising_sessions` 21, `range_sessions` 252,
`min_above_low_pct` 30, `max_below_high_pct` 25, `strength` 2, `base_sessions` 120, `min_base_sessions` 15,
`min_contractions` 2, `max_contractions` 6, `max_first_depth_pct` 35, `max_last_depth_pct` 10, `min_depth_pct` 1,
`contraction_ratio` 0.85, `high_tolerance_pct` 10, `max_pivot_distance_pct` 5, `dry_up_sessions` 10,
`volume_sessions` 50, `dry_up_ratio` 0.9, `breakout_volume_ratio` 1.4, `max_overnight_move_pct` 25.
History: 252 completed D1 sessions.

## 9. `cup_and_handle`

Source concept: William J. O'Neil, *How to Make Money in Stocks* (cup with handle). O'Neil gives typical week
ranges and depths, and describes the cup as rounded rather than V-shaped, but gives **no numeric rounding test**.
The test below is an explicit engineering definition and is documented as such.

1. **Prior advance**: the left rim high is >= 30% above the lowest low of the 80 sessions before it (`no_prior_uptrend` otherwise).
2. **Cup**: left rim (swing high) to right rim (swing high) 35..130 sessions; depth 12..35% (rim high to lowest low);
   rim highs within 8% of each other.
3. **Rounding** (all must hold on the cup's closes):
   * *Time in the lower half*: at least 60% of cup closes lie in the lower half of the cup's depth (a V spends only about
     half its time there; a flat U spends most).
   * *Bottom position*: the lowest close is within the central 20%..80% of the cup's duration (not at an edge).
   * *Smooth sides*: on each side the largest counter-move against the trend is at most 0.35 x the depth (this rejects
     a W/double-bottom, whose left side rebounds sharply).
   A diagnostic quadratic fit (`quadratic_bottom_fit`) exists for analysis only; it does not gate signals.
4. **Handle**: 5..25 sessions after the right rim; depth 2..15% and no more than half the cup depth; its low in the upper
   half of the cup; mean handle volume <= 0.9 x mean cup volume.
5. **Pivot**: the highest high from the right rim to the last completed session; last close below it. A close already above
   it yields `already_broken_out`.

Parameters mirror `CupParams` (percent parameters are written as percentages): `strength` 3, `min_cup_sessions` 35,
`max_cup_sessions` 130, `min_depth_pct` 12, `max_depth_pct` 35, `max_rim_difference_pct` 8, `min_u_fraction` 0.6,
`min_bottom_position` 0.2, `max_counter_move` 0.35, `min_handle_sessions` 5, `max_handle_sessions` 25,
`min_handle_depth_pct` 2, `max_handle_depth_pct` 15, `handle_in_upper` 0.5, `handle_volume_ratio` 0.9,
`prior_uptrend_sessions` 80, `min_prior_advance_pct` 30, `breakout_volume_ratio` 1.4, `max_overnight_move_pct` 25.
History: 241 completed D1 sessions at default parameters.

## 10. Evidence (tests)

* `tests/unit/test_session_candles.py` (21): timeframe/candle contract, Alpaca stamp conversion across EDT/EST, refusals,
  weekly aggregation against independently computed OHLCV, holiday and early-close weeks, point-in-time visibility,
  duplicates/conflicts, provenance lock, retention, `history_issue`, ContextBuilder with D1/W1.
* `tests/unit/test_daily_weekly_strategies.py` (32): hand-derived VCP depths/pivot and cup geometry, rejection tables (V, W,
  shallow, deep, edge-bottom cups; widening/equal/squeezed contractions), trend-template oracle and boundaries, pivot
  confirmation lag, fires on the exact M1 bar, no repeat, gap-up, volume pro-rating, fail-closed history cases, pattern cache,
  prefix (no look-ahead) property, parameter validation.
* `tests/unit/test_daily_weekly_integration.py` (12): fake-Alpaca dataset acquisition (developing day dropped, RAW only,
  mis-stamp refusal, idempotence), BACKTEST point-in-time probe over days and weeks, refusal without a source, M1 runs unchanged,
  VCP and Cup end-to-end signal on the exact breakout bar with determinism and run-identity checks, SHADOW warm / once-per-symbol
  request / failure isolation / refresh, `build_runtime` configuration and `STRATEGY_HISTORY_UNSUPPORTED`, and a full SHADOW
  lifecycle that opens a long on the intraday breakout.
* A mutation check (each of: contraction ratio, volume baseline guard, bottom-position test, handle depth test,
  lower-half rounding count, settle delay, near-52-week-high condition) is detected by at least one test.

## 11. Limitations

* IEX daily capability and stamp convention are unverified against the live provider (section 4).
* IEX-only volume; ratios are internally consistent but noisy for thin symbols.
* RAW daily history with a heuristic split guard: a split under the overnight-move threshold is undetected, and a genuine large gap
  suppresses signals for that symbol until it ages out of the lookback.
* A single missing or late daily bar makes the history untrusted (fail closed), by design.
* W1 is derived from D1 and needs a fully observed week; neither strategy currently consumes W1 (it is available and tested for later strategies).
* Chart patterns are inherently subjective; the thresholds are defensible engineering defaults, not optimised and not
  validated for profitability. No performance claim is made.
* Pre-existing unrelated failures remain in `backtest_repro`, `backtest_lifecycle` and `integration/test_backtest_integrity`; they
  fail identically on the base branch and are untouched.
* The full repository suite was intentionally not run; only new and directly affected tests were.
