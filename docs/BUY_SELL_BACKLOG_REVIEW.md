# BUY/SELL Backlog Batches A + B — Independent Review

* **Source commit reviewed:** `704a525` (`feature/gemini-buy-sell-backlog-b`, which contains Batch A, `c068613`, on top of `f35d388`).
* **Strategies reviewed:** `bollinger_band_mean_reversion`, `vwap_pullback`, `support_bounce` (Batch A);
  `vwap_band_reversion`, `intraday_consolidation_breakout` (Batch B); and the two equivalence
  decisions (`vwap_reclaim` → `vwap_bounce`, `bollinger_middle_band_exit` → `price_below_sma`).
* **Method:** the code, the indicator calculators it uses and the existing tests were read and
  exercised directly; the documented rules were compared with what the code computes. Regression tests
  are in `tests/unit/test_buy_sell_backlog_review.py`; every defect below has a test that fails against
  the original code (checked by running the new tests against the pre-review strategy modules).
* **Result:** four of the five strategies had genuine defects (corrected); `bollinger_band_mean_reversion`
  is correct. Neither equivalence decision is wrong, but one is narrower than the documentation said.
  Library counts are unchanged: **80 BUY, 40 SELL, 3 SHORT, 3 COVER = 126** (production and research).

Classification used for sources: **A** published indicator definition, **B** published trading
methodology, **C** engineering choice made here to get a deterministic signal from a qualitative
description. None of the five entry triggers is a type-B rule; all are type C built on type-A indicators
or pivots, and the documentation now says so.

## Defects found and corrected

### 1. `vwap_band_reversion` — the bands were never VWAP bands (critical)
`VWAPDeviationBandsCalculator.update()` reset its accumulators whenever its stored `_session_id` was the
"unset" sentinel. `Candle` objects carry no `session_id`, so the sentinel never changed and **every bar
reset the accumulators**: "VWAP" was the bar's own typical price, `std_dev` was always 0 and
`lower == TP`. The strategy therefore tested `close[t-1] < TP[t-1] and close[t] > TP[t]` — a weak-then-strong
candle pair — not a lower deviation band. The original tests passed because they built their expected
values with the same degenerate calculator, and the calculator tests always passed an explicit `session_id`.

*Fix:* the calculator resets only when a session identity is known and changes; bars without an identity
continue the accumulators. New helpers `session_key`, `session_vwap_series`, `session_bands_series` feed the
session date explicitly. *Regression tests:* a no-`session_id` run equals the explicit-id run and has real
dispersion; a scenario where the old rule signals but the prior close is far inside the real band.

**Price basis / consistency with production VWAP.** Both use typical price `(H+L+C)/3`, volume gating
(zero-volume bars add nothing) and the same 28-digit arithmetic context, so the band VWAP is bit-identical to
`core.VWAPCalculator` (verified over random bars including zero volume). The deviation is the
volume-weighted population standard deviation of bar typical prices about VWAP, checked against the direct
definition `sum(v·(tp−vwap)²)/sum(v)`; the variance is clamped at 0 and cannot be negative. `E[tp²]−VWAP²`
carries no practical cancellation problem at 28 digits for price-scale data.

**Anchoring.** The strategy recomputes the bands from the candles in the context, so a history that starts
mid-session (e.g. a short history after a restart) would produce an anchored VWAP different from the
production `vwap` / `vwap_prev` indicators. The strategy now requires the recomputed VWAP at `t` and `t−1`
to equal the shared indicators (relative tolerance 1e-9, `vwap_matches`) and returns `INSUFFICIENT_DATA`
otherwise, so it never trades on a VWAP inconsistent with the rest of the system. (In production and in
BACKTEST the candle history is 500 bars, more than a full session.)

**Snapshot/restore.** Two further calculator defects: `restore()` replaced a zero multiplier with 2.0
(`_load(...) or Decimal("2.0")`; zero is falsy) and `snapshot()` stringified any session id, so a non-`str`
id would come back as a `str` and trigger a false reset. Fixed (explicit `None` checks; non-`str` ids raise on
snapshot; malformed snapshots raise on restore). The strategy itself keeps no calculator state — it is a pure
function of the session's candles — so "state persists through ineligible periods" holds trivially; a
JSON-round-trip restore at arbitrary cut points equals uninterrupted processing (tested).

Verified correct: the `t−1` band is computed from bars `0..t−1` only (appending bar `t` leaves it unchanged);
the signal is an edge (prior strictly below, latest strictly above), not a level; session reset is complete;
zero-volume bars do not change any accumulator; missing minutes and zero-volume windows fail closed.

### 2. `vwap_pullback` — earlier trend bars compared with a later VWAP (point-in-time defect)
Confirmed. The trend test was `pre_setup_bars[-1].low > vwap_prev * (1 + tol)` and
`all(bar.close >= vwap_prev ...)`: **every** trend bar was compared with the setup bar's VWAP (`vwap_prev`),
a later point in time. The context exposes only the latest two VWAP values, not a per-bar series.
In a falling session VWAP drifts down, so a trend bar whose low never exceeded the VWAP that existed when it
traded was accepted (reproduced in `test_defect_each_trend_bar_is_compared_with_its_own_vwap_not_the_setup_bars`).

*Fix (reusing the existing VWAP infrastructure):* the per-bar session VWAP series is recomputed from the
session's candles with the production calculator (`core.session_vwap` via `session_vwap_series`); each trend
bar is compared with the VWAP as of its own close, the setup bar with `V[t−1]` and the signal bar with `V[t]`.
The recomputed `V[t]`, `V[t−1]` must equal the context's `vwap` / `vwap_prev` (same guard as above), which
also covers restart/short-history cases. The rule is otherwise unchanged: every trend bar closes at/above its
own VWAP and the last trend bar traded wholly above its own tolerance zone; setup `low <= V·(1+tol)` and
`close >= V`; signal `close > setup.high` and `> V[t]`. *Tests:* an independent oracle over 4,000 random
sessions × 3 parameter sets (including varying volume) agrees with the strategy on every case (≥20 signals);
boundary, session-reset, history-not-at-open, missing-bar, zero-volume and replay cases.

**vs `vwap_bounce`.** Distinct (bounce = crossing from at/below VWAP to above; pullback = trend above VWAP,
a touch that holds VWAP, close above the setup high). The documentation claimed full mutual exclusivity; that
is false at equality (setup close exactly equal to `vwap_prev` satisfies both) and is corrected.

### 3. `support_bounce` — broken support kept in force; gaps bridged
Verified correct: the pivot search uses `bars[-lookback-2:-2]`, i.e. only bars *before* the setup bar, and
`swing_pivots` reports a pivot only when all its right-hand bars lie inside that slice — so neither the
setup nor the signal bar can supply a right-hand bar, and the latest pivot is the latest *confirmed* one
(tested explicitly: a falling series whose minimum would become a pivot only with the setup bar as its
right neighbour produces no signal; the same series with one extra rising bar does).
Defects:
* **Invalidation:** only the setup bar was checked against `S·(1−tol)`. A bar between the pivot and the
  setup that traded below support but was too recent to be a confirmed pivot (or a plateau of equal lows,
  which is not a strict pivot) left the broken level in force and the strategy "bounced" off it. Now any bar
  after the pivot through the setup bar with `low < S·(1−tol)` voids the support.
* **Missing bars / sessions:** only the last two bars were required to be adjacent. A hole between the
  pivot and the setup, or a pivot from the previous session carried across the overnight gap, was used.
  Now every bar from the pivot to the signal bar must be consecutive (`INSUFFICIENT_DATA` otherwise), the
  same convention as `double_bottom_reversal`.
Boundaries (inclusive touch at `S·(1+tol)`, inclusive hold at `S·(1−tol)`, strict confirmation) and
distinctness from `double_bottom_reversal` (single pivot, no interim peak), `resistance_breakout` (swing
low, not high) and `previous_day_low_reversal` (intraday fractal, not prior-day low) were confirmed.

### 4. `intraday_consolidation_breakout` — repeated signals on a rising price; tautological prior-bar test
Verified correct: the breakout bar is excluded from the ceiling/floor (`window[:-1]`; a large-range breakout
bar neither widens the base nor blocks its own detection — tested), the denominator is the base floor
(documented), the range test is inclusive at exactly `max_range_pct`, the close test strict, and the window
must be one session of consecutive bars.
Defect: documented condition 3, `close[t−1] <= Ceiling`, used the *same* ceiling that includes bar `t−1`'s
high, so it was always true. In a slow steady advance each close exceeds the previous 15 highs while the
15-bar range stays under 0.75%, so the strategy signalled on **every** bar as the window rolled (reproduced:
>30 signals on a 60-bar drift). It was effectively an N-bar-high breakout inside a narrow band, repeating.
*Fix:* the prior bar must not itself have closed above the base that preceded it
(`close[t−1] <= max(high[t−1−N .. t−2])`), so only the first breakout bar signals; `required_bars` becomes
`N + 2` (17 by default). A flat consolidation that is rebuilt after a breakout can signal again; a drift does
not. The rule still requires compression (it differs from `donchian_channel_breakout`, which fires on the
wide base in the test), and note the documented limitation that a narrow range does not exclude a slow drift.
No additional filters were added. Compared with `opening_range_breakout` (anchored to 09:30),
`inside_bar_breakout`/`nr7_breakout` (single-bar ranges) and `momentum_breakout` (volume/return thresholds)
it remains distinct.

### 5. `bollinger_band_mean_reversion` — correct
`lower[t−1]` = lower band of `closes[t−period .. t−1]`, `lower[t]` = lower band of `closes[t−period+1 .. t]`
(population SD, `period+1` bars). The trigger `close[t−1] < lower[t−1] and close[t] > lower[t]` is strict on
both sides and an edge. Verified over a grid of about 3,300 scenarios against an independent implementation, and
that using the later band for the prior bar (the plausible bug) gives different answers on >20 of them.
Flat windows (SD 0) cannot signal; warmup `period+1`, adjacency, replay suppression and parameter bounds
(`period` 2–100, `deviations` 0.1–5) behave as documented. It is distinct from `bollinger_band_breakout`
(upper band), `bollinger_squeeze` and `keltner_channel_reversion` (EMA/ATR channel): on a 300-bar walk its
signal set differs from both. Only the *source* text was corrected (see below).

## Equivalence decisions

* **VWAP Reclaim → `vwap_bounce`: retained.** `vwap_bounce` fires on `prior.close <= vwap_prev` and
  `latest.close > vwap_latest` with a session reset, positive volume and warmed shared VWAP; no confirmation
  bar. That is a bullish reclaim. The only difference from a strictly-below reading is a prior close exactly
  equal to VWAP. The earlier claim that "technical analysis literature" defines reclaiming this way was
  unsupported and was reworded; no duplicate strategy is registered.
* **Bollinger Middle Band Exit → `price_below_sma`: retained for the level reading only, with a documented
  difference.** The middle band is exactly `SMA(close, period)` (verified bit-for-bit against
  `bollinger()` over random windows), so `price_below_sma(period=20)` evaluates `close < middle band`.
  But `price_below_sma` is a **level** rule (`LEVEL_ON_CLOSED_BAR`): it signals on every bar below the SMA,
  not on the crossing. Consequences: (a) it is not equivalent to a "crosses below the middle band" reading, which
  no existing strategy implements; (b) paired with the lower-band mean-reversion BUY it would exit on the first
  eligible bar after entry (the BUY enters below the middle band) — the pairing is unsuitable. The quotation and
  chapter number Gemini attributed to Bollinger could not be verified and were removed; no source is claimed for
  an exact middle-band exit trigger. The default period is 50, so 20 must be configured. No new strategy added.

## Source and reference corrections
* Removed unverifiable chapter-level citations and a quotation: Shannon "chapter on VWAP pullback entries",
  Aziz "Chapter 7: VWAP Trading", the Bollinger "Chapter 15 Method I" quotation, Shannon *Maximum Trading Gains
  With Anchored VWAP (2023)* for VWAP bands, and "StockCharts canonical" VWAP bands. The stray note about another
  author was removed. Book titles remain only as general context.
* Sources now state the classification: indicator definitions (type A) are cited; triggers, windows,
  tolerances and thresholds are labelled engineering choices (type C). Bulkowski/Crabel/Murphy references for the
  consolidation breakout are described as concept only (Crabel's full title is *Day Trading with Short Term
  Price Patterns and Opening Range Breakout*).
* Not changed (outside this review): other modules cite Murphy "ch. 7" for head-and-shoulders and flags; in the
  1999 edition those patterns appear under reversal/continuation patterns (chapters 5–6), so those references
  deserve a separate check against the book.

## Integration
All five are registered once in `app.build_runtime` and in the research registry (no duplicate ids; the
research function lists each class in its import and once in the registration tuple); each is `StrategyRole.BUY`
with valid defaults, `spec.warmup_bars == required_bars({})`, and composes into a SHADOW runtime as per-symbol BUY
strategies. Each returns `NOT_ELIGIBLE` unless the engine sets `eligibility == "WATCHING_BUY"`, so BUY entries
still pass through the engine's entry gate and the Market Status Monitor; no strategy reads the market state.
BACKTEST enters on exactly the bar a direct evaluation signals (representative run per strategy), with no
SELL/SHORT/COVER intents. No engine, broker, SHORT or COVER code was changed. `docs/STRATEGIES.md` was not edited
(its role-count sentence is stale for the BUY count and is left for the integration merge).

## Tests
* Added `tests/unit/test_buy_sell_backlog_review.py` (50 tests): calculator reset/consistency/math/restore,
  band strategy, pullback oracle and defect, support pivot timing/invalidation/gaps/boundaries, consolidation
  boundaries/first-breakout/drift, Bollinger band timing and distinctness, both equivalence decisions, registration
  and per-symbol composition, representative BACKTEST consistency.
* Existing Batch A/B tests were corrected where they were circular or built on inconsistent fixtures (pullback
  fixtures injected arbitrary `vwap` values unrelated to the bars; the band test derived expectations from the
  degenerate calculator; consolidation fixtures one bar short of the new warmup).

## Remaining limitations
* `vwap_pullback` and `vwap_band_reversion` need the session's candles from the open; right after a mid-session
  restart with a short history they return `INSUFFICIENT_DATA` until the history is warm (fail-closed by design).
  A per-bar VWAP series in `MarketContext` would remove the recomputation; that is an infrastructure change not made here.
* The VWAP bands use the dispersion of bar typical prices, not of individual trades, and the 2.0 multiplier,
  5-bar `min_session_bars`, tolerances and windows are engineering defaults, not tuned or backtested settings.
* `intraday_consolidation_breakout` does not exclude a slow drift inside the range limit (it signals once, at the
  first breakout bar), and the range is measured relative to its floor.
* `support_bounce` uses a single latest pivot; older stronger supports are ignored.
