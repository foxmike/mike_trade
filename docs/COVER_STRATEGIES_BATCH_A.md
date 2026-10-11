# COVER Strategies — Batch A

Fifteen additional **COVER** strategies for the bidirectional engine. A COVER strategy reports one
thing: *"an existing short position should be closed."* It never opens a long, never submits an
order and never reverses a position. All 15 extend `CoverStrategy`, declare `StrategyRole.COVER` and
are registered next to the original three (`cover_ema_bullish_crossover`,
`cover_supertrend_bullish_flip`, `cover_donchian_breakout`), giving **75 BUY, 40 SELL, 3 SHORT and
18 COVER = 136** strategies. Nothing here claims profitability; each rule restates a published or
documented definition.

## What the engine owns (and the strategies do not)

The strategies contain no position, order, sizing, borrow, margin, stop or market-state logic. The
engine (`docs/STRATEGIES.md` §18) is responsible for confirming that a managed short exists,
the minimum hold, COVER arbitration, closing the full remaining short quantity, order submission,
collateral and borrow accounting, protective stops and lifecycle transitions.

**Market Status Monitor.** Market-state restrictions (NORMAL, WARNING, CRASH, STABILIZING, RECOVERY)
apply to new entries, not to COVER exits. No COVER strategy reads or checks the market state, so every
COVER signal is available in every state.

## Common behaviour

* **Completed bars only.** Each rule runs on completed regular-session 1-minute bars
  (`trigger_timeframe = M1`, `completed_bars_only = True`). `MarketContext` supplies only CLOSED bars, so
  the newest bar is the signal bar and nothing later is ever read.
* **Edge, not level.** Every rule is a transition between the prior completed bar `t-1` and the latest
  completed bar `t`; a condition that merely persists does not signal again.
* **Adjacent bars.** The two bars of an edge must be consecutive minutes (`latest.start == prior.end`).
  A missing minute, an overnight gap or a bar outside the regular session yields `INSUFFICIENT_DATA`
  (or `NOT_ELIGIBLE`); nothing is interpolated.
* **Gates.** Stale data → `STALE_DATA`; a non-`REGULAR` session → `NOT_ELIGIBLE`. COVER has no
  `eligibility` key: the engine evaluates it only while it holds a short, and extra keys in `state`
  (`WATCHING_BUY`, `WATCHING_SHORT`, anything) do not change the result.
* **Duplicate suppression.** `last_signal_bar` records the bar that fired; replaying the same completed
  bar returns `NO_SIGNAL`. Signal ids are `make_signal_id(strategy, version, symbol, COVER, bar start)`,
  distinct from a BUY/SELL/SHORT id for the same bar.
* **Recursive indicators.** MACD (×2), RSI (×2), DMI, Parabolic SAR and Keltner persist a JSON-safe
  snapshot under `indicator_state` and resume it bar by bar through `closed_bar.advance()`; the value
  never depends on how many candles a bounded history window holds. Missing, corrupt, foreign-signature
  (changed parameters) or out-of-window state is discarded and re-seeded from the supplied history.
  While a COVER is ineligible (stale, outside the session) the engine still evaluates it advance-only
  and each blocked result carries the advanced `indicator_state`, so the calculator never stops
  advancing. Finite-window rules (SMA, Aroon, Bollinger, CCI, Stochastic, Williams %R, CMF, VWAP) are a
  pure function of the supplied bars and persist only `last_signal_bar` (VWAP also `session_id`).
* **Determinism.** Decimal arithmetic only; the same bar history and state always give the same result
  in SHADOW, BACKTEST and replay.

Warmup below is the default `required_bars({})` (`spec.warmup_bars`); `required_bars(params)` returns the
exact requirement of a configured lookback.

## The strategies

### 1. Cover SMA Bullish Crossover — `cover_sma_bullish_crossover`
* **Signal.** `SMA_fast[t-1] <= SMA_slow[t-1]` and `SMA_fast[t] > SMA_slow[t]` on closes.
* **Parameters.** `fast_period` 50 (2–200); `slow_period` 200 (3–400); `fast_period < slow_period`.
* **Indicators.** `sma` (arithmetic mean). **Warmup.** `slow_period + 1` = 201. **State.** `last_signal_bar`.
* **Source.** StockCharts ChartSchool, *Moving Averages – Simple and Exponential* (two-average
  crossover). Same calculation as `sma_crossover` (BUY).
* **Limitations.** A 50/200 pair on M1 bars needs 201 completed bars before it can signal.

### 2. Cover MACD Bullish Crossover — `cover_macd_bullish_crossover`
* **Signal.** `MACD[t-1] <= signal[t-1]` and `MACD[t] > signal[t]`; `MACD = EMA(fast) − EMA(slow)`,
  `signal = EMA(signal_period)` of MACD.
* **Parameters.** `fast_period` 12 (2–100); `slow_period` 26 (3–200); `signal_period` 9 (2–100).
* **Indicators.** `MACDCalculator`. **Warmup.** `slow_period + signal_period` = 35.
  **State.** `last_signal_bar`, `indicator_state`.
* **Source.** Gerald Appel's MACD, StockCharts ChartSchool *MACD*. Same as `macd_signal_crossover` (BUY).
* **Limitations.** Lagging; the signal line exists only after `slow_period + signal_period − 1` closes.

### 3. Cover MACD Zero-Line Crossover — `cover_macd_zero_line_crossover`
* **Signal.** `MACD[t-1] <= 0` and `MACD[t] > 0`.
* **Parameters.** `fast_period` 12 (2–100); `slow_period` 26 (3–200).
* **Indicators.** `MACDCalculator` (no signal line). **Warmup.** `slow_period + 1` = 27.
  **State.** `last_signal_bar`, `indicator_state`.
* **Source.** Appel's MACD centerline crossover. Same as `macd_zero_line_crossover` (BUY).
* **Limitations.** A flat series sits exactly on zero, so its first uptick qualifies.

### 4. Cover RSI Oversold Reversal — `cover_rsi_oversold_reversal`
* **Signal.** Wilder `RSI[t-1] <= oversold` and `RSI[t] > oversold` (default 30): the oversold level is
  recovered. This is the BUY rule `rsi_oversold_reversal`, **not** the inverse of an overbought exit.
* **Parameters.** `rsi_period` 14 (2–100); `oversold` 30 (1–50).
* **Indicators.** `RSICalculator` (SMA seed, Wilder smoothing). **Warmup.** `rsi_period + 2` = 16.
  **State.** `last_signal_bar`, `indicator_state`.
* **Source.** Wilder, *New Concepts in Technical Trading Systems* (1978); StockCharts *RSI*.
* **Limitations.** Single-step re-cross only; no divergence or failure-swing confirmation.

### 5. Cover RSI Centerline Crossover — `cover_rsi_centerline_crossover`
* **Signal.** `RSI[t-1] <= 50` and `RSI[t] > 50`.
* **Parameters.** `rsi_period` 14 (2–100). **Indicators.** `RSICalculator`. **Warmup.** 16.
  **State.** `last_signal_bar`, `indicator_state`.
* **Source.** Wilder / StockCharts *RSI* (50 centerline). Same as `rsi_centerline_crossover` (BUY).
* **Limitations.** The centerline is fixed; RSI of a flat series is exactly 50, so its first uptick qualifies.

### 6. Cover DMI Bullish Crossover — `cover_dmi_bullish_crossover`
* **Signal.** `+DI[t-1] <= −DI[t-1]` and `+DI[t] > −DI[t]` (Wilder smoothing of +DM, −DM and TR).
* **Parameters.** `period` 14 (2–100). **Indicators.** `DMICalculator`. **Warmup.** `period + 2` = 16.
  **State.** `last_signal_bar`, `indicator_state`.
* **Source.** Wilder (1978); StockCharts *ADX* (+DI/−DI crossover). Same as `dmi_crossover` (BUY).
* **Limitations.** No ADX trend-strength filter, so a cross in a trendless market also signals.

### 7. Cover Aroon Bullish Reversal — `cover_aroon_bullish_reversal`
* **Signal.** `Down[t-1] > Up[t-1]` and `Up[t] > Down[t]`. Aroon-Up/Down =
  `100 · (n − bars since highest high / lowest low) / n` over the latest `n + 1` bars (ties → most recent).
* **Parameters.** `period` 25 (2–200). **Indicators.** `aroon`. **Warmup.** `period + 2` = 27.
  **State.** `last_signal_bar`.
* **Source.** Tushar Chande, *Aroon* (1995); StockCharts *Aroon*.
* **Methodology decision.** "After bearish conditions" is made deterministic as a **strictly bearish prior
  bar** (Aroon-Down above Aroon-Up). The `aroon_trend` BUY accepts `Up[t-1] <= Down[t-1]`, which includes a tie;
  a tie is neutral, not bearish, so it does not qualify here.
* **Limitations.** A bearish → tie → bullish path does not signal; the 50/70/100 strength levels are unused.

### 8. Cover Parabolic SAR Bullish Reversal — `cover_parabolic_sar_bullish_reversal`
* **Signal.** The SAR trend of bar `t-1` is down and that of bar `t` is up (a high above a downtrend SAR
  reverses it: new SAR = prior extreme point, AF reset to `step`).
* **Parameters.** `step` 0.02 (0.001–0.5); `maximum` 0.20 (0.001–1); `maximum >= step`.
* **Indicators.** `ParabolicSARCalculator`. **Warmup.** 3. **State.** `last_signal_bar`, `indicator_state`.
* **Source.** Wilder (1978); StockCharts *Parabolic SAR*. Same as `parabolic_sar` (BUY).
* **Limitations.** SAR, extreme point, AF, trend and the last two highs/lows are recursive and always
  resumed from persisted state (a bounded window would change the seed). The initial trend is the shared
  calculator's convention (second close vs first), so a flip shortly after a cold start can reflect that seed.
  The SAR is not used as a stop.

### 9. Cover VWAP Reclaim — `cover_vwap_reclaim`
* **Signal.** `close[t-1] < VWAP[t-1]` and `close[t] > VWAP[t]`, both bars in the same New York session
  and consecutive minutes. VWAP is read from the shared indicator service (`vwap`, `vwap_prev`); the
  strategy never computes it.
* **Parameters.** None. **Warmup.** 2 completed session bars with a warmed VWAP. **State.**
  `last_signal_bar`, `session_id`.
* **Source.** Session-VWAP rule on the shared VWAP service (same data path as `vwap_bounce` / `vwap_breakdown`);
  no external publication defines this exact trigger.
* **Methodology decision.** A *reclaim* is strict: the prior close must be **below** VWAP. `vwap_bounce` (BUY)
  accepts `close[t-1] <= VWAP[t-1]`; a close exactly on VWAP has not been below it and does not qualify.
* **Limitations.** Needs positive-volume bars; a missing/invalid bar, zero volume or unwarmed/invalid VWAP is
  `INSUFFICIENT_DATA`. A prior-session bar is never the prior observation (VWAP resets each session).

### 10. Cover Bollinger Lower-Band Reversion — `cover_bollinger_lower_band_reversion`
* **Signal.** `close[t-1] < lower[t-1]` and `close[t] > lower[t]`, each bar against the band computed on it.
  `lower = SMA(n) − k · σ` with the population standard deviation.
* **Parameters.** `period` 20 (2–200); `deviations` 2 (0.1–5). **Indicators.** `bollinger`.
  **Warmup.** `period + 1` = 21. **State.** `last_signal_bar`.
* **Source.** John Bollinger, *Bollinger on Bollinger Bands*; bollingerbands.com. Same band calculation as the
  `bollinger_upper_band_reversal` SELL.
* **Limitations.** Mean-reversion rule, not the upper-band breakout of `bollinger_band_breakout`. A band tag
  alone is not a signal: a completed close below the band then back above it is required. A zero-width band
  cannot be crossed strictly.

### 11. Cover Keltner Channel Reversion — `cover_keltner_channel_reversion`
* **Signal.** `close[t-1] < lower[t-1]` and `close[t] > lower[t]`; `lower = EMA(ema_period) − multiplier · ATR(atr_period)`
  (Raschke's modification, Wilder ATR).
* **Parameters.** `ema_period` 20 (2–200); `atr_period` 10 (1–100); `multiplier` 2 (0.5–10).
  **Indicators.** `KeltnerChannelCalculator`. **Warmup.** `max(ema_period, atr_period) + 1` = 21.
  **State.** `last_signal_bar`, `indicator_state`.
* **Source.** Keltner (1960) as modified by Raschke; StockCharts *Keltner Channels*. Same channel as
  `keltner_channel_reversion` (BUY).
* **Limitations.** Re-entry above the lower band confirms the reversion; it does not predict a move to the centre.

### 12. Cover CCI Oversold Reversal — `cover_cci_oversold_reversal`
* **Signal.** `CCI[t-1] <= oversold` and `CCI[t] > oversold` (default −100);
  `CCI = (TP − SMA(TP)) / (0.015 · mean deviation)`, `TP = (H + L + C) / 3`.
* **Parameters.** `period` 20 (2–50); `oversold` −100 (−300 to −50). **Indicators.** `cci`.
  **Warmup.** `period + 1` = 21. **State.** `last_signal_bar`.
* **Source.** Donald Lambert; StockCharts *CCI*. Same as `cci_reversion` (BUY).
* **Limitations.** Zero mean deviation (flat window) is undefined and never signals.

### 13. Cover Stochastic Bullish Crossover — `cover_stochastic_bullish_crossover`
* **Signal.** `%K[t-1] <= %D[t-1]` and `%K[t] > %D[t]` (Full Stochastic: `%K = SMA(raw %K, k_smooth)`,
  `%D = SMA(%K, d_period)`).
* **Parameters.** `k_period` 14 (2–100); `k_smooth` 3 (1–20); `d_period` 3 (1–20). **Indicators.** `stochastic`.
  **Warmup.** `k_period + k_smooth + d_period − 1` = 19. **State.** `last_signal_bar`.
* **Source.** George Lane; StockCharts *Stochastic Oscillator* (14,3,3 signal-line cross). Same as
  `stochastic_crossover` (BUY).
* **Methodology decision.** The published bare signal-line cross is used with **no oversold-zone filter**
  (as in the BUY); StockCharts defines the cross itself as the signal.
* **Limitations.** A cross at a high level also signals. A flat range leaves %K undefined and suppresses the signal.

### 14. Cover Williams %R Bullish Reversal — `cover_williams_r_bullish_reversal`
* **Signal.** `%R[t-1] <= oversold` and `%R[t] > oversold` (default −80);
  `%R = −100 · (HH(n) − C) / (HH(n) − LL(n))`.
* **Parameters.** `period` 14 (2–100); `oversold` −80 (−100 to −50). **Indicators.** `williams_r`.
  **Warmup.** `period + 1` = 15. **State.** `last_signal_bar`.
* **Source.** Larry Williams; StockCharts *Williams %R*; the −80 re-cross trigger follows cTrader/TrendSpider
  documentation (sources differ). Same as `williams_r_reversion` (BUY).
* **Limitations.** %R can also rise through −80 when the window's highest high rolls off while the close is
  unchanged; that is part of the definition. A flat range is undefined. No −50 follow-through variant.

### 15. Cover Chaikin Money Flow Recovery — `cover_chaikin_money_flow_recovery`
* **Signal (recovery trigger).** `CMF[t-1] <= 0` and `CMF[t] > 0`, where each CMF is the `period`-bar window ending
  on bar `t-1` / `t`: `CMF = Σ(multiplier · volume) / Σ(volume)`, `multiplier = ((C − L) − (H − C)) / (H − L)`
  (0 for a zero-range bar). Money flow moves from neutral-or-selling to accumulation.
* **Parameters.** `period` 20 (2–50). **Indicators.** `chaikin_money_flow`. **Warmup.** `period + 1` = 21.
  **State.** `last_signal_bar`. Requires volume.
* **Source.** Marc Chaikin; StockCharts *Chaikin Money Flow*. Same calculation and zero crossing as
  `chaikin_money_flow` (BUY).
* **Limitations.** An exact zero on the prior window counts as the non-positive side. A window with no volume is
  undefined and never signals.

## Registration

The 15 classes live in `mike_trade/strategies/cover/` and are registered once each in
`app.build_runtime` (production composition; available to per-symbol `cover_strategies`, strategy
validation, SHADOW and BACKTEST) and in `research.commands._research_registry` (research experiments,
metadata and reporting). Both use the single `StrategyRegistry`; there are no duplicate ids.

## Execution limitations (all 15)

* Exit signals only: a COVER signal is meaningful only while a managed short exists, and the engine decides the
  cover order, quantity (the full remaining short) and any follow-on action. It never implies a BUY.
* One timeframe (completed M1 bars) and the regular session only; stale data never signals.
* No stop, target or position handling in the strategy; the engine's protective stops and minimum hold apply.
* The default periods are the published ones (tuned for daily/intraday charts) applied to M1 bars; they are
  rule definitions, not tuned or backtested settings.
