# COVER Strategies — Batch B

Ten more **COVER** strategies: candlestick, chart-pattern, structure, volume and divergence exits for
an open short. Like Batch A (`docs/COVER_STRATEGIES_BATCH_A.md`) they extend `CoverStrategy`, declare
`StrategyRole.COVER`, and only report *"an existing short position should be closed."* They never open
a long, never start a short, never submit an order and never reverse a position. With Batch B the
library holds **75 BUY, 40 SELL, 3 SHORT and 28 COVER = 146** strategies. No profitability is claimed;
each rule restates a published or documented definition, mostly reusing the BUY rule it mirrors.

## What the engine owns (and the strategies do not)

Position confirmation, minimum hold, COVER arbitration, closing the full remaining short, order
submission, collateral and borrow accounting, protective stops and lifecycle transitions stay in the
engine. Market-state restrictions (NORMAL, WARNING, CRASH, STABILIZING, RECOVERY) apply to new entries
only: no COVER strategy reads the market state, so a COVER signal is available in every state, and the
extra `state` keys the engine may supply (`eligibility`, anything else) never change a result.

## Common behaviour

* **Completed M1 bars, regular session only.** `MarketContext` supplies only CLOSED bars, so the newest
  bar is the signal bar and nothing later is ever read. Stale data → `STALE_DATA`; a non-`REGULAR`
  session → `NOT_ELIGIBLE`.
* **Point-in-time.** A pattern is observable only when its completing bar closes. The signal's `bar_time`
  is that bar; nothing is back-dated to the pivot or trough that started the pattern, and evaluating the
  history one bar earlier never signals.
* **Pivots are causal.** A swing high/low is a Bill Williams fractal generalised to `strength` bars per
  side: strictly above (below) the highs (lows) of the `strength` bars on each side
  (`indicators.windows.swing_pivots`). A pivot is reported only when all `2·strength + 1` of its bars are
  consecutive and inside the window, so it needs `strength` closed bars *after* it. The chart-pattern rules
  (double bottom, inverse head and shoulders, trendline, previous swing high) compute their pivots on the
  bars **before** the signal bar, so the breakout bar never helps confirm the pattern it breaks.
* **Missing data.** The two bars of an edge must be adjacent; each chart pattern also requires its whole
  span (first pivot → signal bar) to be consecutive and returns `INSUFFICIENT_DATA` otherwise. A hole inside
  a pivot's own bars means that pivot is not recognised (nothing is bridged or interpolated).
* **Edge, once.** `last_signal_bar` suppresses a replay of the same completed bar, and each rule is a
  first-crossing/one-confirming-bar event (see below), so it does not repeat on following bars.
* **State.** Only `cover_obv_breakout` is recursive: it persists a JSON-safe `indicator_state` and resumes
  it bar by bar through `closed_bar.advance()` (also while the COVER is ineligible, via advance-only
  evaluation). Missing, corrupt, foreign-signature (changed parameter) or out-of-window state is discarded
  and re-seeded. The other nine are pure functions of the supplied finite window and persist only
  `last_signal_bar`.

`required_bars(params)` returns the exact history for a configured lookback; the figures below are the
defaults (`spec.warmup_bars`).

## The strategies

### 1. Cover Bullish Engulfing — `cover_bullish_engulfing`
* **Rule.** Bar `t-1` is bearish (`close < open`), bar `t` is bullish, and the real body of `t` engulfs that of
  `t-1`: `open[t] <= close[t-1]` and `close[t] >= open[t-1]`. Shadows are ignored (shared `engulfing` helper).
* **Parameters.** None. **History.** 2 bars. **Confirmation.** The pattern completes at the close of `t`.
  **State.** `last_signal_bar`.
* **Source.** StockCharts ChartSchool, *Candlestick Bullish Reversal Patterns*; same as `engulfing_candle_reversal` (BUY).
* **Limitations.** No trend or volume filter (none is in the BUY definition either); the pattern alone is not a
  confirmed reversal.

### 2. Cover Hammer Reversal — `cover_hammer_reversal`
* **Rule.** Hammer bar `h = t-1`: lower shadow `>= shadow_ratio ×` real body, upper shadow `<= max_upper_shadow ×`
  range, non-zero range. Prior decline: `close[h-1] < close[h-1-trend_bars]`. Confirmation: `close[t] > high[h]`.
* **Parameters.** `trend_bars` 5 (2–50); `shadow_ratio` 2 (1–10); `max_upper_shadow` 0.1 (0–0.5).
  **History.** `trend_bars + 3` = 8. **State.** `last_signal_bar`.
* **Confirmation / point-in-time.** The hammer alone never signals; Nison requires bullish confirmation, read here
  as the next bar closing strictly above the hammer's high. The signal bar is the confirming bar.
* **Source.** Steve Nison, *Japanese Candlestick Charting Techniques* (2nd ed.); shadow thresholds as TA-Lib's
  `CDLHAMMER`. Same pattern as `hammer_reversal` (BUY).
* **Methodology difference.** The BUY checks adjacency of the three pattern bars only; here the whole
  `trend_bars` decline window must also be consecutive (an overnight gap cannot masquerade as a decline).
* **Limitations.** Daily-bar candle adapted to M1 bars.

### 3. Cover Double Bottom Reversal — `cover_double_bottom_reversal`
* **Rule.** With the last two confirmed swing lows B1 (earlier) and B2 and the highest swing high P between them:
  `|B1 − B2| <= tolerance_pct` of the lower; `P >= higher low × (1 + min_rise_pct)`; no bar since B1 broke below
  the lower low by more than the tolerance; and the signal bar's close is above P with **no close above P since B2**.
* **Parameters.** `strength` 2 (1–10); `lookback` 100 (20–400, `>= 8·strength`); `tolerance_pct` 0.5 (0.05–10);
  `min_rise_pct` 1 (0.1–30). **History.** `lookback + 1` = 101. **State.** `last_signal_bar`.
* **Confirmation / point-in-time.** Both troughs and the peak are fractals confirmed from bars before the signal
  bar (B2 needs `strength` closed bars after it). The signal is the neckline-breaking close itself; a later
  evaluation of the same pattern after an earlier break does not signal.
* **Source.** Edwards & Magee; Bulkowski, *Encyclopedia of Chart Patterns*, Double Bottom. Same as
  `double_bottom_reversal` (BUY).
* **Limitations.** Intraday parameter defaults replace Bulkowski's daily percentages; no prior downtrend required.

### 4. Cover Inverse Head and Shoulders — `cover_inverse_head_and_shoulders`
* **Rule.** The last three confirmed swing lows are the left shoulder LS, head H and right shoulder RS: H lies below
  the lower shoulder by at least `min_head_depth_pct`, and `|LS − RS| <= shoulder_tolerance_pct` of the lower
  shoulder. The neckline is the straight line through the highest swing high between LS and H and the highest
  between H and RS, extended to the signal bar. Signal: the close is above the neckline and **no close since RS was**.
* **Parameters.** `strength` 2 (1–10); `lookback` 150 (30–500, `>= 12·strength`); `shoulder_tolerance_pct` 1
  (0.1–20); `min_head_depth_pct` 0.3 (0.05–20). **History.** `lookback + 1` = 151. **State.** `last_signal_bar`.
* **Confirmation / point-in-time.** All three lows and both neckline highs are confirmed from bars before the signal
  bar; the signal is the neckline-breaking bar. A sloping neckline is evaluated at the signal bar's own index.
* **Source.** Edwards & Magee; Bulkowski, *Head-and-Shoulders Bottom*; Murphy, *Technical Analysis of the
  Financial Markets*, ch. 7. Same as `inverse_head_and_shoulders` (BUY).
* **Limitations.** Qualitative published thresholds are explicit parameters; volume pattern and prior downtrend not required.

### 5. Cover Trendline Breakout — `cover_trendline_breakout`
* **Rule.** The last two confirmed swing highs H1 (earlier) and H2 with `H2 < H1` define the trendline through
  `(x1, H1)` and `(x2, H2)` in bar index; signal when the close is above the line extended to the signal bar and
  no close from H1 to the previous bar was.
* **Parameters.** `strength` 2 (1–10); `lookback` 100 (20–400, `>= 8·strength`). **History.** 101. **State.**
  `last_signal_bar`.
* **Confirmation / point-in-time.** Both pivots must be fractals confirmed before the signal bar (H2 needs
  `strength` closed bars after it), so the line is only used once it is knowable. The breakout bar is the signal;
  a close through the line earlier in the span means this is not the first break and nothing signals.
* **Source.** Edwards & Magee; Murphy ch. 4; StockCharts *Trendlines*. Same as `trendline_breakout` (BUY).
* **Limitations.** Two-point lines only (touches are not counted).

### 6. Cover Previous Swing High Breakout — `cover_previous_swing_high_breakout`
* **Rule.** `R` is the latest confirmed swing high within the `lookback` bars before the signal bar.
  `close[t-1] <= R` and `close[t] > R`.
* **Parameters.** `strength` 2 (1–10); `lookback` 50 (10–300, `>= 2·strength + 2`). **History.** 51. **State.** `last_signal_bar`.
* **Confirmation / point-in-time.** A pivot is used only after `strength` bars have closed to its right; the pivot
  search ends at the bar before the signal bar.
* **Distinction from the Donchian COVER.** `cover_donchian_breakout` compares the close with the *highest high* of the
  previous window; this rule uses the *most recent structural pivot*, which is lower whenever an older, higher peak
  exists (and is replaced by each newly confirmed pivot).
* **Source.** Edwards & Magee; Murphy ch. 4; Williams fractals. Same as `resistance_breakout` (BUY).
* **Limitations.** No pivot in the lookback means no signal; the most recent pivot is used even if an older, higher peak exists.

### 7. Cover Higher High / Higher Low — `cover_higher_high_higher_low`
* **Rule.** Over the last `lookback + 1` bars the latest swing low is above the previous swing low (HL), the latest
  swing high is above the previous swing high (HH), and the latest swing low is confirmed **on the signal bar**
  (its index is `t − strength`).
* **Parameters.** `strength` 2 (1–10); `lookback` 50 (10–300, `>= 4·strength + 2`). **History.** 51. **State.** `last_signal_bar`.
* **Confirmation / point-in-time.** The structure is known only when the higher low has `strength` closed bars after it;
  the signal fires exactly on that confirming bar, and not on the bar before or after.
* **Source.** Dow Theory (Murphy ch. 4); Williams fractals. Same as `higher_high_higher_low` (BUY).
* **Limitations.** The exit is intentionally `strength` bars late; both pivots' bars must be adjacent.

### 8. Cover Bull Flag Breakout — `cover_bull_flag_breakout`
* **Rule (read from bars before the signal bar `t`).** Pole top P = latest bar with the highest high of the `lookback`
  bars before `t`. Flagpole: the lowest low within `pole_bars` bars up to P is at least `pole_pct` below P's high.
  Flag: the `min_flag_bars`–`max_flag_bars` bars strictly between P and `t`, whose lowest low retraces no more than
  `max_retrace` of the pole height. Breakout: `close[t] >` the flag's highest high.
* **Parameters.** `lookback` 60 (20–300); `pole_bars` 15 (3–100); `pole_pct` 1 (0.1–30); `min_flag_bars` 3 (2–50);
  `max_flag_bars` 20 (3–100); `max_retrace` 0.5 (0.1–0.9); `min_flag_bars <= max_flag_bars`,
  `lookback > max_flag_bars + 1`. **History.** 61. **State.** `last_signal_bar`.
* **Methodology difference.** This is the `bull_flag_breakout` BUY pattern with one added edge condition: bar `t-1` must
  not itself have closed above the high of the flag bars before it. The BUY can fire again on a continuation bar after a
  breakout; the COVER signals only on the first breakout bar.
* **Source.** Bulkowski, *Flags*; Murphy ch. 7 (retracement no more than about half the pole).
* **Limitations.** Published descriptions give no minimum pole height, so `pole_pct` and the bar limits are parameters;
  pennants and volume contraction are not used.

### 9. Cover OBV Breakout — `cover_obv_breakout`
* **Rule.** OBV adds a bar's volume on an up close, subtracts it on a down close (the first bar adds nothing).
  `OBV[t-1] <= max(OBV of the period bars before t-1)` and `OBV[t] > max(OBV of the period bars before t)` — the
  threshold of each bar **excludes that bar itself**.
* **Parameters.** `period` 20 (2–200). **History.** `period + 2` = 22. **State.** `last_signal_bar`, `indicator_state`
  (running OBV, last close and the last `period` OBV values; resumed bar by bar, never recomputed from a moving window,
  so only the line's own recent history matters and its start level is irrelevant).
* **Source.** Granville (1963); StockCharts *On Balance Volume*. Same calculation as `obv_breakout` (BUY)
  (`CumulativeLineCalculator("obv", period)`).
* **Limitations.** Indicator-only; no price confirmation. Needs volume.

### 10. Cover Bullish Momentum Divergence — `cover_bullish_momentum_divergence`
* **Rule.** Over the last `lookback` bars, the latest swing low L2 is **confirmed on the signal bar** (index `t − strength`),
  `low[L2] < low[L1]` (a lower price pivot) and `M[L2] > M[L1]`, with `M[i] = close[i] − close[i − momentum_period]`
  evaluated at each pivot bar itself.
* **Parameters.** `momentum_period` 10 (2–100); `strength` 2 (1–10); `lookback` 50 (10–300, `>= 4·strength + 2`).
  **History.** `momentum_period + lookback` = 60 (the whole window must be adjacent). **State.** `last_signal_bar`.
* **Confirmation / point-in-time.** Momentum is read at the pivot's own timestamp, not at the later confirmation bar; the
  signal fires only on the bar that makes the divergence observable. A pair whose momentum is only higher at the confirming
  bar (a rebound after the pivot) does not qualify.
* **Source.** Murphy, momentum oscillator and divergence; Williams fractals. The pivot-low mirror of
  `bearish_momentum_divergence` (SELL).
* **Limitations.** Finite close-difference momentum (not RSI/MACD divergence); only the two latest pivots are compared.

## Registration

The ten classes live in `mike_trade/strategies/cover/` and are registered once each in `app.build_runtime` (production
composition: per-symbol `cover_strategies`, validation, SHADOW, BACKTEST) and in `research.commands._research_registry`
(research experiments, metadata, reporting), using the single `StrategyRegistry`; no duplicate ids.

## Execution limitations (all 10)

* Exit signals only: meaningful only while a managed short exists; the engine decides the order, quantity (the full
  remaining short) and any follow-on action. A COVER signal never implies a BUY.
* Completed M1 bars in the regular session only; stale data never signals; no stop, target or position handling in the strategy.
* Defaults are rule definitions applied to M1 bars, not tuned or backtested settings. The long-lookback chart patterns
  (101–151 bars) need that much contiguous history before they can signal.
