# BUY/SELL backlog batches C and D — independent review

Branch `feature/buy-sell-backlog-cd-reviewed`, built on `origin/feature/strategy-library-176-integrated`
(`569da60c55421855e8cc87dcb6b79b52286b32c7`).

## Source commits merged

| Batch | Branch | Commit |
|---|---|---|
| C (Codex) | `origin/feature/buy-backlog-c-vwap-intraday` | `fe83e6fa6afad5ca725d39210843398d661912f5` |
| D (Gemini) | `origin/feature/gemini-buy-sell-backlog-d` | `a659557991d6e9455f0e38290b69beda162fe9c4` |

Both descend from the reviewed backlog branch already inside the 176 library, so no previously reviewed
correction was touched. Merge conflicts were textual only: the `build_runtime` / `_research_registry` import
and class lists (both sides kept, each class registered once), the inventory summary prose, and exact-count
assertions across the affected test files, which now state the final totals.

## Final counts

| Role | Count |
|---|---:|
| BUY | 85 |
| SELL | 41 |
| SHORT | 28 |
| COVER | 28 |
| **Total** | **182** |

Production (`mike_trade/app.py`) and research (`mike_trade/research/commands.py`) register exactly the same
unique classes; the 50-strategy SHORT/COVER expansion is intact.

## The six implemented strategies

| Strategy | Role | What it actually does |
|---|---|---|
| `anchored_vwap_breakout` | BUY | Anchor = first fractal low (`strength` bars each side, default 2) of the regular session. VWAP of typical price × volume from the pivot bar. BUY when a later close moves from at/below the prior anchored VWAP to strictly above the current one. |
| `vwap_rejection` | BUY | Prior bar's low is strictly above its own session VWAP; latest bar's low pierces its VWAP by at most 0.5% (default), closes strictly above it and is bullish. Single-bar rule, no confirmation bar. |
| `first_pullback_buy` | BUY | Phase machine SEEK → ADVANCE → PULLBACK → DONE. Impulse = 3 rising bars (close/high/low), ≥1% gain from first open, last bar volume ≥1.2× the mean of the earlier impulse bars. First lower-close bar starts the pullback (shallow ≤50% of the impulse range, volume below the impulse mean, ≤5 corrective bars); BUY on a close above the pullback high with volume ≥1.2× the pullback mean, before 11:00 ET. DONE is terminal for the session. |
| `relative_volume_momentum` | BUY | RVOL = volume / mean volume of the 20 bars immediately preceding (current bar excluded). BUY when RVOL crosses from <2.0 (using the prior bar's own earlier baseline) to ≥2.0, on a bullish bar, with 3-bar ROC ≥0.1%. |
| `moving_average_compression` | BUY | SMA 10/20/50. Each of the 5 bars preceding the signal bar has (max SMA − min SMA)/SMA50 ×100 ≤ 0.75, prior close ≤ prior max SMA, latest close > latest max SMA and > open. |
| `high_volume_reversal` | SELL (long exit only) | Prior 5-bar uptrend (`close[t-1] > close[t-6]`), volume ≥2.0× mean of preceding 20 bars, bearish candle (close<open, close<prior close) closing in the lower half of its range, and the prior bar did not itself show the same volume-plus-candle rejection. |

**Statistical definition of RVOL.** `relative_volume_momentum` uses a rolling previous-N-minute baseline. It is
*not* a same-minute-of-day multi-day RVOL (the vendor-scanner definition), so it does not adjust for the
intraday volume U-shape. Because the window must lie inside the regular session (defect 4), no signal is
possible in the first 22 minutes.

All numeric thresholds in the six strategies are engineering defaults, not published constants. Source
citations are concept-level only.

## Actual defects found and corrected

1. **Anchored VWAP: pre-market bars could confirm a "regular-session" pivot** (`anchored_vwap_breakout`).
   The pivot window was cut from all supplied bars, so the 09:30 bar could be a pivot using two pre-market
   bars as left neighbours, and a pivot could even be centred on a pre-market bar (anchoring VWAP in the
   pre-market). The window now uses only bars with `start >= session open`.
2. **First pullback: pre-market bars could form the morning impulse** (`first_pullback_buy`). The impulse
   group was checked only by calendar date, so pre-market bars could be impulse bars and arm the setup at 09:30.
   Group bars must now start at or after the session open.
3. **Relative volume momentum: zero prior baseline read as "RVOL below threshold"**. If the baseline of the
   prior bar was entirely zero, `RVOL[t-1]` was set to 0, so a persistent surge could be treated as a fresh
   crossing. The prior RVOL is now undefined in that case and the evaluation yields no signal.
4. **Batch D windows were only checked for adjacency, not for the regular session** (`relative_volume_momentum`,
   `moving_average_compression`, `high_volume_reversal`). The documented "regular-session M1 bars" was not
   enforced, so pre-market bars adjacent to the open entered the volume baseline / SMAs. They now use the
   shared `latest_session_bars` guard (window adjacent and wholly inside the latest bar's session), which
   also keeps overnight gaps out.
5. **Source / documentation corrections**: the First Pullback citation pointed at a Warrior Trading
   cash-account article that does not match the topic (removed; the remaining pull-back URL could not be
   retrieved during review and is marked unverified); unverifiable book-chapter citations (Murphy chapters,
   Bulkowski for volume reversal) were removed in favour of "concept only"; the Batch D document and the
   inventory said RVOL requires strictly positive volumes — the code does not (it only needs a non-zero
   baseline) so the text was corrected; the high-volume-reversal docstring claimed the prior bar had to be
   a full "qualifying reversal" although its uptrend test is deliberately not repeated (so a run of heavy
   bearish bars gives one signal) — now stated accurately; the inventory summary counts (Batch D branch had
   pending 5 / deferred 20 from a stale baseline) were recomputed from the rows (68 BUY + 24 SELL implemented,
   48 equivalents, 0 pending, 16 deferred = 156).

Reviewed and found correct (no change needed):

* **Anchored VWAP** — pivot confirmed only after `strength` right-hand bars close; no entry on the pivot or
  confirmation bar; VWAP is computed retroactively from the pivot bar through the confirmation bar *by
  explicit convention* (knowable at that moment, so not a look-ahead trade); prior/current VWAP comparison
  uses each bar's own value; session reset, restart from a snapshot and cold replay give identical signals;
  it differs from session VWAP (different anchor and cumulative window).
* **VWAP rejection** — the prior-bar condition (`low > VWAP`) makes it mutually exclusive with `vwap_bounce`
  (which needs the prior close at/below VWAP); it differs from `vwap_pullback` (needs a trend, a setup bar and
  a confirmation bar) and `vwap_band_reversion` (deviation bands). Equalities are strict where a "pierce"
  and a "wholly above" are required, so it is a genuinely distinct rule, not a renamed parameter.
* **First pullback** — impulse precedes the correction in the state machine; no future bars; first-pullback-only
  consumption holds because every failure path ends in the terminal DONE phase (a deeper, heavier or longer
  first pullback never lets a later pullback be relabelled "first"); snapshot/restore equals cold replay.
* **Moving average compression** — each historical bar's own SMAs, preceding-window excludes the signal bar,
  every one of the five bars must qualify, denominator is the slow SMA, an ordinary continuing uptrend does not
  fire because the prior close is already above the cluster.
* **High-volume reversal** — baseline excludes the current bar, inclusive equality at the multiple and at the
  50% close location, a zero-range bar cannot be bearish, run suppression described above. It is distinct from
  `bearish_engulfing_exit` / `shooting_star_reversal` (pattern rules, no volume), `momentum_reversal` (ROC) and
  `money_flow_index_reversal` (oscillator); the 2.0× multiple is an honest engineering threshold.

## Equivalence decisions

| Candidate | Mapped to | Verdict |
|---|---|---|
| VWAP Mean Reversion | `vwap_band_reversion` | **Exact for the selected reading only** (lower deviation-band recovery). The name is an umbrella; central-VWAP reclaim is `vwap_bounce` and support rejection is `vwap_rejection`. No duplicate registered. |
| VWAP Trend Continuation | `vwap_pullback` | **Exact for the selected reading only** (trend above VWAP, pullback, confirmation above the setup high). Other continuation forms were not specified. |
| Morning Momentum Continuation | `first_pullback_buy` | **Partial coverage, not exact.** `first_pullback_buy` is one morning continuation (first pullback only). Flag/consolidation and drive forms are covered by other registered strategies (`opening_range_breakout`, `opening_drive`, `intraday_consolidation_breakout`). The inventory row now says so. |
| Moving Average Mean Reversion | `z_score_mean_reversion`, `bollinger_band_mean_reversion`, `keltner_channel_reversion`, `moving_average_reclaim` | **Covered by a family**, not by one exact equivalent. The identifier in the inventory row is the representative member. |

No strategy was added to increase the count.

## Test evidence

New file `tests/unit/test_buy_sell_backlog_cd_review.py` (29 tests). Expected values come from independent
Decimal oracles written in the test file (anchored VWAP from raw bars, rolling RVOL, explicit SMAs, volume-
reversal rule, VWAP rejection rule), compared on seeded random sessions against both a continuously
state-fed engine and a cold restart at every bar (hundreds of signals exercised):

* anchored VWAP: oracle equality, no signal on or before the confirmation bar, pre-market pivot regression,
  anchor ≠ session VWAP, next-session reset;
* VWAP rejection: oracle equality, disjoint from `vwap_bounce`, boundary cases (no pierce, too deep, bearish
  close, prior bar not wholly above);
* first pullback: single signal and unconsumed second pullback, invalidated / heavy-volume setups stay dead,
  morning cutoff, missing bar, pre-market regression, restore equals cold replay;
* RVOL momentum: oracle equality, inclusive threshold, no repeat on a second heavy bar, zero-baseline regression;
* MA compression: oracle equality, flat-cluster breakout, uptrend and spike negatives;
* high-volume reversal: oracle equality, one signal per run of heavy bearish bars;
* pre-market / overnight window regression for the three Batch D strategies;
* registry parity, unique ids, exact 85/41/28/28 = 182, equivalence candidates unregistered;
* SHADOW composition of all six; BACKTEST runs (RVOL BUY → high-volume-reversal SELL fills, compression,
  VWAP rejection) with `trigger_bar_end <= time`.

The six regression tests for defects 1–4 fail on the pre-fix code and pass now. Directly affected suites
(batch C and D tests, all exact-count tests, SHORT/COVER integration tests) were run; the full repository
suite was deliberately not run.

## Remaining limitations

* Library-wide: other closed-bar strategies still rely on adjacency alone, so pre-market bars may enter
  their windows when a feed supplies extended-hours candles; only the Batch C/D strategies are guarded here.
* `first_pullback_buy`: a setup consumed while the eligibility gate is closed is spent without a signal; the
  entry cutoff is evaluated on bar start; impulse volume is the initial impulse-group mean even when the
  advance extends.
* `relative_volume_momentum` has no intraday-seasonality adjustment (see definition above).
* `high_volume_reversal` does not retest the prior bar's uptrend when suppressing repeats.
* A zero-volume minute makes the session-VWAP strategies (`vwap_rejection`, `anchored_vwap_breakout`,
  `first_pullback_buy`) fail closed for the rest of the session.
* The two Warrior Trading pull-back URLs could not be fetched during review (HTTP 403); the concept citation
  is unverified.
