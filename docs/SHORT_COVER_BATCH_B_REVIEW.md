# Independent Technical Review and Integration: SHORT / COVER Batch B

## 1. Executive Summary

This document reports the independent technical review and integration of **SHORT Batch B** (10 strategies authored by Codex) and **COVER Batch B** (10 strategies authored by Claude Sonnet). Both feature branches have been merged into the exclusive review and integration branch `feature/short-cover-batch-b-reviewed`, building upon the previously reviewed and verified Batch A integration baseline.

All 20 Batch B strategies were inspected for mathematical correctness, causal pivot and breakout triggers, pattern geometry, neckline projections, indicator state persistence, lookback and session boundary behavior, duplicate signal suppression, and lifecycle contract conformance. All merge conflicts, registration parity discrepancies, and hardcoded test count assertions have been resolved.

### Commit Provenance
- **Reviewed Base Branch**: `origin/feature/short-cover-batch-a-reviewed`
- **Base Commit**: `821690a8458e37a67336fad6805fcabf8a19af87` (`821690a`)
- **SHORT Batch B Source**: `5e92bfb7fedc8366e1da4e52a60d9395e2b78ae6` (`origin/feature/short-strategies-batch-b`)
- **COVER Batch B Source**: `591df765b632c00172be5aa6a74f91748162ecef` (`origin/feature/cover-strategies-batch-b`)
- **SHORT Batch B Merge Commit**: `5bc0c9d`
- **COVER Batch B Merge Commit**: `5aa964d`
- **Integration Branch**: `feature/short-cover-batch-b-reviewed`
- **Worktree**: `/home/mike/dev/mike/stocks/python/work/mike_trade-short-cover-batch-b-review`

---

## 2. Integrated Strategy Inventory & Counts

Prior to Batch B integration, the reviewed Batch A baseline held 151 strategies (75 BUY, 40 SELL, 18 SHORT, 18 COVER). With the addition of 10 SHORT and 10 COVER Batch B strategies, the combined registry totals **171 strategies**:

| Strategy Role | Batch A Baseline | Batch B Additions | Integrated Total |
| :--- | :---: | :---: | :---: |
| **BUY** | 75 | 0 | **75** |
| **SELL** | 40 | 0 | **40** |
| **SHORT** | 18 | 10 | **28** |
| **COVER** | 18 | 10 | **28** |
| **TOTAL** | **151** | **20** | **171** |

Both registration roots (`mike_trade/app.py::build_runtime` and `mike_trade/research/commands.py::_research_registry`) maintain 100% 1:1 parity across all 171 strategy classes and IDs without duplicates.

---

## 3. Technical Review of the 10 SHORT Batch B Strategies

Each SHORT strategy inherits from `ShortStrategy`, emits `StrategyRole.SHORT`, enforces closed regular-session M1 bars, validates parameters, and requires `state["eligibility"] == "WATCHING_SHORT"`.

1. **`short_bearish_engulfing` (v1.0.0)**
   - *Rule*: Prior completed candle has a bullish real body; latest completed candle has a bearish real body strictly engulfing the prior real body (`latest.open >= prior.close` and `latest.close <= prior.open` with at least one strict inequality). Requires prior close-to-close uptrend over `trend_bars` (default 3).
   - *History*: `trend_bars + 2` = 5 bars.
   - *Verification*: Excludes current bar from trend check. Correctly applies `indicators.finite.engulfing`. Confirmed contiguous bar requirement.

2. **`short_shooting_star_reversal` (v1.0.0)**
   - *Rule*: Steve Nison shooting star candlestick on bar `t-1` following a 5-bar prior advance, followed by bearish confirmation on bar `t` where `close[t] < low[t-1]`. Star geometry: upper shadow ≥ `shadow_ratio` × real body (default 2.0) and lower shadow ≤ `max_lower_shadow` × candle range (default 0.10).
   - *History*: `trend_bars + 3` = 8 bars.
   - *Verification*: Evaluated strictly on the confirmation bar `t`, not the star candle itself. Verifies next-bar close is strictly below the star's low.

3. **`short_double_top_breakdown` (v1.0.0)**
   - *Rule*: Two confirmed swing high peaks within `peak_tolerance` (3%); intervening confirmed reaction trough at least `min_decline` (10%) below the lower peak; latest completed close crosses from at/above to strictly below the trough support level.
   - *History*: `lookback + 1` = 61 bars.
   - *Verification*: Structural Bill Williams fractals (`strength=2`) ensure both peaks and trough are strictly confirmed before the breakout. Causal pivot confirmation avoids look-ahead bias.

4. **`short_head_and_shoulders_breakdown` (v1.0.0)**
   - *Rule*: Four confirmed swing highs forming rising predecessor, left shoulder, higher head, and lower right shoulder. Two confirmed intervening reaction lows define a linearly projected neckline. Close crosses from at/above to strictly below the projected neckline.
   - *History*: `lookback + 1` = 61 bars.
   - *Verification*: Linearly projects neckline to both `t-1` and `t`. Verifies that no intermediate candle between right shoulder and breakout exceeds the head high. Neckline reaction lows are strictly confirmed fractals.

5. **`short_trendline_breakdown` (v1.0.0)**
   - *Rule*: Two latest confirmed swing lows are ascending (`low2 > low1`). Straight trendline projected forward to `t-1` and `t`. Close crosses from at/above projected trendline at `t-1` to strictly below projected trendline at `t`.
   - *History*: `lookback + 1` = 31 bars.
   - *Verification*: Disallows flat or descending trendlines. Enforces strictly confirmed 5-bar pivot fractals.

6. **`short_previous_swing_low_break` (v1.0.0)**
   - *Rule*: Close crosses from at/above to strictly below the latest confirmed 5-bar swing low in the lookback window.
   - *History*: `lookback + 1` = 21 bars.
   - *Verification*: Structural fractal support, distinct from rolling Donchian channel lows. Current bar cannot be part of the pivot low reference range.

7. **`short_lower_low_confirmation` (v1.0.0)**
   - *Rule*: The fractal low centered at `t - strength` newly confirms on the latest completed bar `t` and is strictly below the preceding confirmed pivot low.
   - *History*: `lookback + 1` = 31 bars.
   - *Verification*: Triggers exactly and only on the confirmation bar `t`. Prevents duplicate signals on subsequent continuation candles.

8. **`short_failed_breakout_reversal` (v1.0.0)**
   - *Rule*: Thomas Bulkowski breakout failure. On bar `t-1`, close broke strictly above the 20-bar Donchian high (evaluated over `t-21 .. t-2`); on bar `t`, close falls strictly below that exact breakout level.
   - *History*: `period + 2` = 22 bars.
   - *Verification*: Breakout level reference strictly excludes both the breakout bar `t-1` and the reversal bar `t`. Reversal must occur immediately on the subsequent candle.

9. **`short_bear_flag_breakdown` (v1.0.0)**
   - *Rule*: Flags/pennants per Murphy & Bulkowski. Sharp downward pole drop ≥ `pole_pct` (1%) over up to `pole_bars` (15); upward consolidation flag over 3–20 bars retracing ≤ 50% of the pole drop while remaining above the pole low; latest completed close breaks strictly below the flag consolidation low.
   - *History*: `lookback + 1` = 61 bars.
   - *Verification*: Enforces true first breakout edge (`prior.close >= flag_low` and `latest.close < flag_low`), suppressing repeat triggers during downward continuation.

10. **`short_bearish_momentum_divergence` (v1.0.0)**
    - *Rule*: Higher price swing high (`price_high2 > price_high1`) where close-difference momentum (`close[p] - close[p - momentum_period]`) is lower at the second pivot than the first.
    - *History*: `momentum_period + lookback` = 60 bars.
    - *Verification*: Momentum is sampled at the exact timestamps of the confirmed price pivots, eliminating arbitrary peak pairing. Fires on the confirmation bar `t` of the second pivot.

---

## 4. Technical Review of the 10 COVER Batch B Strategies

Each COVER strategy inherits from `CoverStrategy`, emits `StrategyRole.COVER`, and serves strictly as an exit trigger for open short positions. It carries no entry gating requirements and does not reverse positions.

1. **`cover_bullish_engulfing` (v1.0.0)**
   - *Rule*: Mirrors BUY bullish engulfing. Prior candle bearish real body engulfed by latest bullish real body after a `trend_bars` (default 3) downward close-to-close trend.
   - *History*: `trend_bars + 2` = 5 bars.
   - *Verification*: Strict short exit. Rejects non-adjacent candles.

2. **`cover_hammer_reversal` (v1.0.0)**
   - *Rule*: Hammer candlestick on bar `t-1` after a 5-bar prior decline, confirmed by completed close on bar `t` strictly exceeding the hammer's high. Hammer geometry: lower shadow ≥ `shadow_ratio` × real body (default 2.0) and upper shadow ≤ `max_upper_shadow` × range (default 0.10).
   - *History*: `trend_bars + 3` = 8 bars.
   - *Verification*: Causal confirmation requirement prevents pre-mature exits on unconfirmed hammer shadows.

3. **`cover_double_bottom_reversal` (v1.0.0)**
   - *Rule*: Two confirmed swing lows within 3% tolerance; intervening confirmed peak at least 10% above the higher low; latest close crosses from at/below to strictly above the peak resistance level.
   - *History*: `lookback + 1` = 61 bars.
   - *Verification*: Bill Williams confirmed fractals ensure no retrospective troughs are used.

4. **`cover_inverse_head_and_shoulders` (v1.0.0)**
   - *Rule*: Four confirmed swing lows: declining predecessor, left shoulder, lowest head, higher right shoulder. Two confirmed intervening reaction peaks define the downward/sloped neckline. Close crosses above the linearly projected neckline.
   - *History*: `lookback + 1` = 151 bars.
   - *Verification*: Neckline linearly projected to `t-1` and `t`. Verifies that no intermediate close between right shoulder and breakout violates the head low.

5. **`cover_trendline_breakout` (v1.0.0)**
   - *Rule*: Two latest confirmed swing highs are descending (`high2 < high1`). Straight trendline projected forward to `t-1` and `t`. Close crosses from at/below to strictly above the downward trendline.
   - *History*: `lookback + 1` = 31 bars.
   - *Verification*: Disallows rising or flat trendlines. Requires 5-bar causal fractals.

6. **`cover_previous_swing_high_breakout` (v1.0.0)**
   - *Rule*: Close crosses from at/below to strictly above the latest confirmed 5-bar swing high in the lookback window.
   - *History*: `lookback + 1` = 21 bars.
   - *Verification*: Pivot high excludes the breakout bar. Strict upward crossing edge.

7. **`cover_higher_high_higher_low` (v1.0.0)**
   - *Rule*: Confirms on bar `t` when a newly confirmed 5-bar swing high is strictly above the previous swing high, and the preceding confirmed swing low was strictly above its predecessor.
   - *History*: `lookback + 1` = 31 bars.
   - *Verification*: Classic Dow bullish trend confirmation. Emits signal only on the confirmation bar of the second swing high.

8. **`cover_bull_flag_breakout` (v1.0.0)**
   - *Rule*: Upward pole rise ≥ `pole_pct` (1%) over up to `pole_bars` (15); downward consolidation flag over 3–20 bars retracing ≤ 50% of the pole rise; latest completed close breaks strictly above the flag consolidation high.
   - *History*: `lookback + 1` = 61 bars.
   - *Verification*: Strictly tests the first breakout candle (`prior.close <= flag_high` and `latest.close > flag_high`), preventing repeat exit triggers on continuation bars.

9. **`cover_obv_breakout` (v1.0.0)**
   - *Rule*: Cumulative On-Balance Volume crosses strictly above its rolling resistance level (highest OBV in preceding `period` bars).
   - *History*: `period + 2` = 22 bars.
   - *Verification*: Persists `indicator_state` via snapshot/restore. Advances cumulative state even when ineligible/unarmed. Resistance window excludes the current bar.

10. **`cover_bullish_momentum_divergence` (v1.0.0)**
    - *Rule*: Lower price swing low (`price_low2 < price_low1`) with higher momentum (`close[p] - close[p - momentum_period]`) at the second pivot.
    - *History*: `momentum_period + lookback` = 60 bars.
    - *Verification*: Evaluates momentum at the exact timestamps of the confirmed price troughs. Triggers on confirmation bar `t`.

---

## 5. Investigation: Regular-Session & Premarket Lookback Semantics

### Question Investigated
Codex highlighted that `gate()` checks the evaluation context session status, whereas contiguous historical lookback windows can contain premarket candles. The review investigated whether published methodologies require regular-session-only lookbacks or whether extended-hours history is intentionally valid, and whether session boundaries can create false chart patterns.

### Findings
1. **Regular Session Gate Enforcement**:
   - `closed_bar.gate()` strictly validates that `context.session_status is SessionStatus.REGULAR`. Outside regular hours (09:30–16:00 ET), evaluate returns `ReasonCode.NOT_ELIGIBLE`, preventing any signals outside the regular session.
2. **Contiguity and Adjacency Enforcement**:
   - All 20 Batch B strategies verify bar contiguity via `adjacent(prior, latest)` or whole-window adjacency `curr.start == prev.end`.
   - Between the 16:00 ET regular close and the 09:30 ET regular open, there is a 17.5-hour overnight gap. In feeds without premarket bars, `adjacent()` detects this gap and immediately returns `ReasonCode.INSUFFICIENT_DATA`. Missing session boundaries cannot accidentally create false chart patterns.
3. **Premarket Observations in Finite Lookback Windows**:
   - If a continuous premarket feed is supplied (09:00–09:30 ET contiguous 1-minute bars), those bars may legitimately provide lookback context for moving averages, Donchian channels, or swing fractals evaluated at 09:31 ET.
   - This conforms to classical technical analysis principles, where overnight and premarket price discovery informs opening levels, and is explicitly documented in `docs/SHORT_STRATEGIES_BATCH_B.md` and `docs/COVER_STRATEGIES_BATCH_B.md`.
   - Missing bars in premarket immediately trigger `INSUFFICIENT_DATA`, preventing partial or corrupted pattern recognition.

---

## 6. Defects Found & Corrections Applied

### Defect 1: Registry Merge Conflicts in Production and Research Registries
- **Files**: `mike_trade/app.py`, `mike_trade/research/commands.py`
- **Issue**: Independent feature branches registered 10 SHORT Batch B strategies and 10 COVER Batch B strategies separately into their respective registries.
- **Correction**: Reconciled registrations to include all 20 strategies across both `build_runtime` and `_research_registry()`, maintaining exact 1:1 parity (171 strategies total).

### Defect 2: Hardcoded Strategy Count Assertions in Isolated Test Suites
- **Files**: `tests/unit/test_short_cover_strategies.py`, `tests/unit/test_short_strategies_batch_a.py`, `tests/unit/test_cover_batch_a.py`, `tests/unit/test_short_strategies_batch_b.py`, `tests/unit/test_cover_batch_b.py`
- **Issue**: Each branch had asserted its own partial totals (e.g., 146 or 161 total strategies, 3 SHORT or 3 COVER).
- **Correction**: Reconciled all assertions across all 5 test files to assert the combined total: `BUY: 75, SELL: 40, SHORT: 28, COVER: 28 = 171`. Added all 10 Batch B strategies to `ALL_SHORTS` and `ALL_COVERS` in `test_short_cover_strategies.py`.

### Defect 3: Batch A Integration Test Count Failure
- **File**: `tests/unit/test_short_cover_batch_a_integration.py`
- **Issue**: Lines 103–112 and 171–174 asserted the Batch A milestone count of 151 strategies (18 SHORT, 18 COVER). When all 28 SHORT and 28 COVER strategies were composed into SHADOW runtime, the test failed with `assert 28 == 18`.
- **Correction**: Updated `test_combined_171_strategy_registration_and_distribution` and `test_concurrent_runtime_composition_of_all_shorts_and_covers` to assert 171 total strategies and all 28 SHORT / 28 COVER collections in SHADOW runtime.

### Defect 4: Shared Documentation Strategy Count Reconciliations
- **Files**: `docs/STRATEGIES.md`, `docs/COVER_STRATEGIES_BATCH_B.md`
- **Issue**: Section 18 of `docs/STRATEGIES.md` and intro of `docs/COVER_STRATEGIES_BATCH_B.md` referenced intermediate counts (146 strategies).
- **Correction**: Reconciled both documents to state 75 BUY, 40 SELL, 28 SHORT, and 28 COVER = 171 strategies.

---

## 7. Verification & Targeted Test Suite Results

A total of **721 targeted tests** were executed and passed cleanly:

| Test Module | Tests Passed | Focus Area |
| :--- | :---: | :--- |
| `tests/unit/test_short_strategies_batch_b.py` | 55 | 10 SHORT Batch B strategies, edge triggers, fractals, gaps |
| `tests/unit/test_cover_batch_b.py` | 191 | 10 COVER Batch B strategies, OBV state, flag breakouts |
| `tests/unit/test_short_cover_strategies.py` | 42 | Full 171 registry parity, unique signal IDs, side validation |
| `tests/unit/test_short_strategies_batch_a.py` | 57 | Preserved 15 SHORT Batch A strategies & runtime bindings |
| `tests/unit/test_cover_batch_a.py` | 296 | Preserved 15 COVER Batch A strategies & runtime bindings |
| `tests/unit/test_short_cover_batch_a_integration.py` | 34 | Combined 171 registry distribution & SHADOW runtime composition |
| `tests/unit/test_short_cover_engine.py` | 23 | Bidirectional lifecycle, stops, arbitration, borrow accounting |
| `tests/unit/test_short_market_integration.py` | 23 | Market status monitor authorization, short restrictions |
| **TOTAL** | **721** | **All targeted tests passing (0 failures)** |

---

## 8. Remaining Technical Limitations

1. **Intraday Granularity & Fractal Noise**: Classical chart patterns (e.g., Head & Shoulders, Double Top/Bottom, Bear/Bull Flags) were originally formulated for daily and weekly equity charts. On 1-minute intraday bars, high-frequency noise and sudden microstructure spikes can form transient fractal geometries that do not carry multi-day continuation significance.
2. **Fixed Percentage Thresholds**: Parameters such as `min_decline=0.10` (10% trough decline in Double Top) or `pole_pct=0.01` (1% flag pole height) are fixed. Low-beta instruments (e.g. large-cap indices) may rarely produce a 10% intraday decline between peaks within a 60-bar window, while high-beta meme stocks may breach them frequently.
3. **Premarket Contiguity Requirement**: If an execution environment lacks premarket data feeds, strategies requiring 30–61 contiguous bars cannot fire during the opening 30–60 minutes of the regular session due to strict adjacency validation across session boundaries.
4. **Advisory Execution Boundary**: All 28 SHORT and 28 COVER strategies emit advisory signals only. Short entry locate authorization, margin availability, borrow fees, order routing, protective stop placement, and buy-to-cover execution remain strictly engine-owned.
