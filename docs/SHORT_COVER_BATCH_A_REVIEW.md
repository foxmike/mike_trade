# Independent Technical Review and Integration: SHORT / COVER Batch A

## 1. Executive Summary

This document presents the independent technical review and integration report for **SHORT Batch A** (15 strategies authored by Codex) and **COVER Batch A** (15 strategies authored by Claude Sonnet). Both feature branches have been merged into the exclusive review and integration branch `feature/short-cover-batch-a-reviewed`.

All 30 new strategies were individually inspected for mathematical fidelity, causal boundary conditions, recursive state persistence/recovery, warmup correctness, parameter schema validation, and lifecycle contract conformance. Integration conflicts and hardcoded strategy count assertions across test suites and shared documentation have been resolved.

### Commit Provenance
- **Base Commit (`origin/main`)**: `f35d38823f34496a7e3972fb5b180f0ded493cb9`
- **SHORT Batch A Source**: `cf3c65ba10bf5f9e97c817f3a7c7505ed2e27c0c` (`origin/feature/short-strategies-batch-a`)
- **COVER Batch A Source**: `a4d0f1a23ebe3c34000b5ca59446c2d7320cb309` (`origin/feature/cover-strategies-batch-a`)
- **Merge Commit**: `5985ae9`
- **Branch**: `feature/short-cover-batch-a-reviewed`
- **Worktree**: `/home/mike/dev/mike/stocks/python/work/mike_trade-batch-a-review`

---

## 2. Integrated Strategy Inventory & Counts

Prior to integration, the repository held 121 strategies (75 BUY, 40 SELL, 3 SHORT, 3 COVER). With the addition of 15 SHORT and 15 COVER strategies, the combined registry totals **151 strategies**:

| Strategy Role | Prior Baseline | Batch A Additions | Reviewed Total |
| :--- | :---: | :---: | :---: |
| **BUY** | 75 | 0 | **75** |
| **SELL** | 40 | 0 | **40** |
| **SHORT** | 3 | 15 | **18** |
| **COVER** | 3 | 15 | **18** |
| **TOTAL** | **121** | **30** | **151** |

Both registration roots (`mike_trade/app.py::build_runtime` and `mike_trade/research/commands.py::_research_registry`) hold all 151 strategies without duplicates or symbol collisions.

---

## 3. Technical Review of the 15 SHORT Strategies

Each SHORT strategy inherits from `ShortStrategy`, emits `StrategyRole.SHORT`, enforces closed regular-session M1 bars, validates parameters, and requires `state["eligibility"] == "WATCHING_SHORT"`.

1. **`short_sma_bearish_crossover` (v1.0.0)**
   - *Rule*: `SMA(fast)[t-1] >= SMA(slow)[t-1]` and `SMA(fast)[t] < SMA(slow)[t]`.
   - *Warmup & Window*: `slow_period + 1` = 201 bars.
   - *Verification*: Strictly tests crossover down. Adjacent bar check enforced. No state persistence needed for finite SMA windows.

2. **`short_macd_bearish_crossover` (v1.0.0)**
   - *Rule*: `MACD[t-1] >= Signal[t-1]` and `MACD[t] < Signal[t]`.
   - *Warmup & State*: `slow_period + signal_period` = 35 bars. Persists `indicator_state` via `advance()`.
   - *Verification*: Correct unpack of `(fast, slow, macd, signal)` via `parts(adv.*, 4)`. Advances indicator state while ineligible.

3. **`short_macd_zero_line_breakdown` (v1.0.0)**
   - *Rule*: `MACD[t-1] >= 0` and `MACD[t] < 0`.
   - *Warmup & State*: `slow_period + 1` = 27 bars. Persists `indicator_state`.
   - *Verification*: Unpacks MACD line (`parts(..., 4)[2]`) and tests crossing below `Decimal(0)`.

4. **`short_rsi_centerline_breakdown` (v1.0.0)**
   - *Rule*: `RSI[t-1] >= 50` and `RSI[t] < 50`.
   - *Warmup & State*: `rsi_period + 2` = 16 bars. Persists `indicator_state`.
   - *Verification*: Correct Wilder smoothing resumption and centerline crossing.

5. **`short_dmi_bearish_crossover` (v1.0.0)**
   - *Rule*: `-DI[t-1] <= +DI[t-1]` and `-DI[t] > +DI[t]`.
   - *Warmup & State*: `period + 2` = 16 bars. Persists `indicator_state`.
   - *Verification*: Evaluated using `crossed_above(minus_prior, plus_prior, minus_latest, plus_latest)`. Direction is strictly bearish.

6. **`short_aroon_bearish_reversal` (v1.0.0)**
   - *Rule*: `Down[t-1] <= Up[t-1]` and `Down[t] > Up[t]`.
   - *Warmup*: `period + 2` = 27 bars. Finite window over `period + 1` bars.
   - *Verification*: Down crosses above Up. Tie handling follows Chande convention (most recent bar).

7. **`short_parabolic_sar_bearish_reversal` (v1.0.0)**
   - *Rule*: `trend[t-1] == UP` and `trend[t] == DOWN`.
   - *Warmup & State*: 3 bars minimum; recursive SAR state persisted via `indicator_state`.
   - *Verification*: `prior[1] and not latest[1]`. Price penetrating SAR from above triggers downtrend flip.

8. **`short_ichimoku_bearish_breakdown` (v1.0.0)**
   - *Rule*: `close[t-1] >= Cloud_Lower[t-1]` and `close[t] < Cloud_Lower[t]`.
   - *Displacement & Window*: Uses `ichimoku_cloud` with `displacement=26`. Cloud at bar `t` uses only observations ending 26 bars before `t`, completely eliminating forward look-ahead.
   - *Verification*: `SENKOU_B + DISPLACEMENT + 1` = 79 bars. Checks adjacency across entire window.

9. **`short_opening_range_breakdown` (v1.0.0)**
   - *Rule*: `prior.close >= range_low` and `latest.close < range_low`.
   - *Window & Timing*: Enforces all `opening_range_minutes` (default 30) from 09:30 ET are present and consecutive. Rejects evaluation before `window_end`.
   - *Verification*: Does not signal on incomplete opening ranges.

10. **`short_vwap_breakdown` (v1.0.0)**
    - *Rule*: `prior.close >= vwap_prev` and `latest.close < vwap_latest`.
    - *Session Semantics*: Rejects cross-session bars. Requires positive volume and warmed shared VWAP service readings.
    - *Verification*: Correct session filtering and adjacent bar validation.

11. **`short_bollinger_lower_band_breakdown` (v1.0.0)**
    - *Rule*: `prior.close >= lower_prev` and `latest.close < lower_now`.
    - *Formula*: Population standard deviation (`variance.sqrt() * deviations`).
    - *Verification*: Lower band computed per candle window (`[-(period+1):-1]` vs `[-period:]`). Correct downside breakdown.

12. **`short_keltner_channel_breakdown` (v1.0.0)**
    - *Rule*: `prior.close >= prior_lower` and `latest.close < latest_lower`.
    - *Variant*: Uses Chester Keltner's original SMA(typical price) +/- SMA(high-low range).
    - *Verification*: Preserves original variant definition.

13. **`short_obv_breakdown` (v1.0.0)**
    - *Rule*: `OBV[t-1] >= support[t-1]` and `OBV[t] < support[t]`.
    - *State & Support*: Cumulative OBV and rolling support window persisted via snapshot restoration.
    - *Verification*: Current bar is excluded from support definition. Whole-window adjacency enforced.

14. **`short_cci_zero_line_breakdown` (v1.0.0)**
    - *Rule*: `CCI[t-1] >= 0` and `CCI[t] < 0`.
    - *Warmup*: `period + 1` = 21 bars.
    - *Verification*: Zero mean absolute deviation is safely handled by `cci` indicator (returns `None`, preventing false triggers).

15. **`short_adx_bearish_trend_confirmation` (v1.0.0)**
    - *Rule*: `ADX[t-1] <= threshold` and `ADX[t] > threshold`, with `-DI > +DI`.
    - *Warmup & State*: `2 * period + 1` = 29 bars. Persists `indicator_state`.
    - *Verification*: Confirms trend strength rising above threshold while direction is confirmed bearish (`minus > plus`).

---

## 4. Technical Review of the 15 COVER Strategies

Each COVER strategy inherits from `CoverStrategy`, emits `StrategyRole.COVER`, and serves strictly as an exit trigger for open short positions. It carries no entry gating requirements and does not reverse positions.

1. **`cover_sma_bullish_crossover` (v1.0.0)**
   - *Rule*: `fast[t-1] <= slow[t-1]` and `fast[t] > slow[t]`.
   - *Warmup*: `slow_period + 1` = 201 bars.
   - *Verification*: Standard bullish crossover covering short.

2. **`cover_macd_bullish_crossover` (v1.0.0)**
   - *Rule*: `macd[t-1] <= signal[t-1]` and `macd[t] > signal[t]`.
   - *Warmup & State*: 35 bars. Persists `indicator_state`.
   - *Verification*: Signal-line bullish crossover edge.

3. **`cover_macd_zero_line_crossover` (v1.0.0)**
   - *Rule*: `macd[t-1] <= 0` and `macd[t] > 0`.
   - *Warmup & State*: 27 bars. Persists `indicator_state`.
   - *Verification*: Centerline bullish crossover edge.

4. **`cover_rsi_oversold_reversal` (v1.0.0)**
   - *Rule*: `RSI[t-1] <= oversold` and `RSI[t] > oversold` (default 30).
   - *Warmup & State*: 16 bars. Persists `indicator_state`.
   - *Verification*: Evaluates recovery from oversold. Inclusive prior boundary, strict current boundary.

5. **`cover_rsi_centerline_crossover` (v1.0.0)**
   - *Rule*: `RSI[t-1] <= 50` and `RSI[t] > 50`.
   - *Warmup & State*: 16 bars. Persists `indicator_state`.
   - *Verification*: Centerline upward crossover edge.

6. **`cover_dmi_bullish_crossover` (v1.0.0)**
   - *Rule*: `+DI[t-1] <= -DI[t-1]` and `+DI[t] > -DI[t]`.
   - *Warmup & State*: 16 bars. Persists `indicator_state`.
   - *Verification*: Bullish directional crossover edge.

7. **`cover_aroon_bullish_reversal` (v1.0.0)**
   - *Rule*: `Down[t-1] > Up[t-1]` and `Up[t] > Down[t]`.
   - *Warmup*: 27 bars.
   - *Verification*: Explicit methodology requiring strictly bearish prior bar (`Down > Up`) to avoid ambiguous ties.

8. **`cover_parabolic_sar_bullish_reversal` (v1.0.0)**
   - *Rule*: `trend[t-1] == DOWN` and `trend[t] == UP`.
   - *Warmup & State*: 3 bars. Persists `indicator_state`.
   - *Verification*: Upward reversal when high penetrates downtrend SAR.

9. **`cover_vwap_reclaim` (v1.0.0)**
   - *Rule*: `prior.close < vwap_prev` and `latest.close > vwap_latest`.
   - *Warmup*: 2 positive-volume session bars with valid VWAP.
   - *Verification*: Strict reclaim (prior must be strictly below VWAP).

10. **`cover_bollinger_lower_band_reversion` (v1.0.0)**
    - *Rule*: `prior.close < lower_prev` and `latest.close > lower_latest`.
    - *Warmup*: 21 bars.
    - *Verification*: Mean-reversion recovery into the band from below. Each close compared to the band calculated on its respective window.

11. **`cover_keltner_channel_reversion` (v1.0.0)**
    - *Rule*: `prior.close < lower_prev` and `latest.close > lower_latest`.
    - *Warmup & State*: 21 bars. Uses Raschke EMA/ATR Keltner channel calculator with persistent snapshot state.
    - *Verification*: Recovery from below lower band into the channel.

12. **`cover_cci_oversold_reversal` (v1.0.0)**
    - *Rule*: `CCI[t-1] <= oversold` and `CCI[t] > oversold` (default -100).
    - *Warmup*: 21 bars.
    - *Verification*: Oversold threshold recovery edge.

13. **`cover_stochastic_bullish_crossover` (v1.0.0)**
    - *Rule*: `%K[t-1] <= %D[t-1]` and `%K[t] > %D[t]`.
    - *Warmup*: `k_period + k_smooth + d_period - 1` = 19 bars.
    - *Verification*: Validated that 19 bars provides known %K and %D for both `t-1` and `t`.

14. **`cover_williams_r_bullish_reversal` (v1.0.0)**
    - *Rule*: `%R[t-1] <= oversold` and `%R[t] > oversold` (default -80).
    - *Warmup*: 15 bars.
    - *Verification*: Correct upward recovery through -80 boundary.

15. **`cover_chaikin_money_flow_recovery` (v1.0.0)**
    - *Rule*: `CMF[t-1] <= 0` and `CMF[t] > 0`.
    - *Warmup*: 21 bars. Requires volume.
    - *Verification*: Volume-weighted money flow multiplier transition from neutral/selling to accumulation.

---

## 5. Defects Found & Corrections Applied

### Defect 1: Merge Conflict in `tests/unit/test_short_cover_strategies.py`
- **Issue**: Short Batch A asserted `counts == {SHORT: 18, COVER: 3}` (total 136). Cover Batch A asserted `counts == {SHORT: 3, COVER: 18}` (total 136).
- **Correction**: Resolved conflict to reflect combined totals: `SHORT: 18, COVER: 18`, total 151. Defined `ALL_COVERS` alongside `ALL_SHORTS`.

### Defect 2: Hardcoded Count Assertions in Isolated Batch Test Files
- **Issue**: `tests/unit/test_short_strategies_batch_a.py` hardcoded `counts[StrategyRole.COVER] == 3` and 136 total. `tests/unit/test_cover_batch_a.py` hardcoded `counts[StrategyRole.SHORT] == 3` and 136 total.
- **Correction**: Updated both test suites to expect `StrategyRole.SHORT == 18`, `StrategyRole.COVER == 18`, and `len(REGISTRY.list_strategies()) == 151`.

### Defect 3: Outdated Counts in Documentation
- **Issue**: `docs/STRATEGIES.md` §18 stated 3 SHORT and 18 COVER strategies.
- **Correction**: Updated `docs/STRATEGIES.md` §18 heading and text to declare 18 SHORT and 18 COVER strategies (151 total).

---

## 6. Combined Integration Verification & Tests Added

A comprehensive integration test suite was created:
[`tests/unit/test_short_cover_batch_a_integration.py`](file:///home/mike/dev/mike/stocks/python/work/mike_trade-batch-a-review/tests/unit/test_short_cover_batch_a_integration.py).

The suite verifies:
1. Exact strategy distribution across all 4 roles (75 BUY, 40 SELL, 18 SHORT, 18 COVER = 151).
2. Registration of every Batch A strategy in `app.build_runtime` and `commands._research_registry`.
3. Simultaneous composition of all 18 SHORT and 18 COVER strategies in a single `SHADOW` runtime graph.
4. Cryptographic signal ID uniqueness across BUY, SELL, SHORT, COVER roles for identical bars.
5. Entry eligibility enforcement: all 15 SHORT strategies require `state["eligibility"] == "WATCHING_SHORT"`.
6. COVER role freedom from entry gating: all 15 COVER strategies evaluate without entry eligibility keys.

### Targeted Test Results
- `tests/unit/test_short_strategies_batch_a.py`: 57 passed
- `tests/unit/test_cover_batch_a.py`: 296 passed
- `tests/unit/test_short_cover_strategies.py`: 42 passed
- `tests/unit/test_short_cover_batch_a_integration.py`: 34 passed
- Combined core bidirectional engine / shadow / backtest suite: 120 passed

---

## 7. Remaining Technical Limitations

1. **Intraday Noise Sensitivity**: 50/200 SMA, 14-period RSI/CCI, and 20-period Bollinger parameters originated on daily equities. When applied to 1-minute intraday bars, whipsaws occur without higher-timeframe trend or regime filters.
2. **VWAP Session Boundaries**: `short_vwap_breakdown` and `cover_vwap_reclaim` rely on shared indicator service VWAP values that reset at the 09:30 ET session open. Bars must have positive volume and cannot span across sessions.
3. **Opening Range Rigidity**: `short_opening_range_breakdown` requires consecutive closed bars for all 30 regular-session minutes starting at 09:30 ET. Missing data or non-regular sessions suppress signals.
4. **Execution Safeguards**: All SHORT and COVER strategies emit advisory signals only. Position sizing, margin checks, locate authorization, borrow costs, stop loss orders, and broker submission remain strictly governed by the engine. Short selling is disabled for LIVE mode.
