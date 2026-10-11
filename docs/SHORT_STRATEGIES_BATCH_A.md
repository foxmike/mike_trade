# SHORT Strategies — Batch A

This batch adds 15 bearish entry strategies. Every strategy emits `StrategyRole.SHORT` only;
none can close a long. The engine retains ownership of BUY/SHORT arbitration, position state,
borrow and margin eligibility, sizing, protective stops, and order submission.

## Shared contract

- Inputs are closed, ascending M1 candles from `MarketContext`. Signals use the latest completed
  candle and never a future observation.
- Evaluation is limited to a fresh regular-session context whose engine-owned state contains
  `eligibility = WATCHING_SHORT`.
- Crosses use an inclusive prior boundary and a strict current boundary. For example, a bearish
  cross is `a[t-1] >= b[t-1]` and `a[t] < b[t]`.
- The two crossing candles must be adjacent. Strategies whose reference definition requires a
  continuous session/window also reject a gap anywhere in that window.
- `last_signal_bar` suppresses repeated emission for the same completed candle. Recursive
  indicators persist JSON-safe calculator snapshots and advance even when SHORT is temporarily
  ineligible; corrupt, incompatible, or unreachable state is conservatively reseeded from the
  supplied history.
- Defaults below determine `StrategySpec.warmup_bars`; `required_bars(params)` reports the exact
  configured requirement. No strategy makes a profitability claim.

## Implementations

### `short_sma_bearish_crossover` (1.0.0)

- Trigger: `SMA(fast)[t-1] >= SMA(slow)[t-1]` and
  `SMA(fast)[t] < SMA(slow)[t]`.
- Formula: arithmetic mean of the latest `n` closes, using the existing `sma` calculator.
- Parameters: `fast_period=50` (2–200), `slow_period=200` (3–400), with `fast < slow`.
- History: `slow_period + 1` bars; 201 by default.
- Source: the existing `sell/sma_bearish_crossover.py`, based on StockCharts ChartSchool's
  moving-average crossover definition. The SHORT version changes only the role and entry
  ownership.
- Limitations: a 50/200 rule on M1 bars is an intraday adaptation and may whipsaw.

### `short_macd_bearish_crossover` (1.0.0)

- Trigger: MACD was at or above its signal line and is now strictly below it.
- Formula: existing `MACDCalculator`; EMA fast minus EMA slow, with an EMA of MACD as signal.
- Parameters: `fast_period=12` (2–100), `slow_period=26` (3–200),
  `signal_period=9` (2–100), with `fast < slow`.
- History: `slow_period + signal_period`; 35 bars by default.
- Source: `sell/macd_bearish_crossover.py`, following Gerald Appel's standard 12/26/9 MACD.
- Limitations: EMA/MACD state depends on continuous observed history and is snapshot-restored.

### `short_macd_zero_line_breakdown` (1.0.0)

- Trigger: `MACD[t-1] >= 0` and `MACD[t] < 0`.
- Formula: existing `MACDCalculator` without a signal EMA.
- Parameters: `fast_period=12` (2–100), `slow_period=26` (3–200), with `fast < slow`.
- History: `slow_period + 1`; 27 bars by default.
- Source: `sell/macd_zero_line_breakdown.py`, using the standard MACD zero-line interpretation.
- Limitations: no signal-line or additional trend confirmation is imposed.

### `short_rsi_centerline_breakdown` (1.0.0)

- Trigger: Wilder RSI crosses from at-or-above 50 to below 50.
- Formula: existing snapshot-capable `RSICalculator` using Wilder smoothing.
- Parameter: `rsi_period=14` (2–100).
- History: `rsi_period + 2`; 16 bars by default.
- Source: `sell/rsi_centerline_breakdown.py`, based on J. Welles Wilder's RSI and the standard
  centerline interpretation.
- Limitations: no overbought prerequisite is added; a flat seed follows the shared calculator's
  defined handling.

### `short_dmi_bearish_crossover` (1.0.0)

- Trigger: `-DI[t-1] <= +DI[t-1]` and `-DI[t] > +DI[t]`.
- Formula: existing `DMICalculator` with Wilder true range and directional-movement smoothing.
- Parameter: `period=14` (2–100).
- History: `period + 2`; 16 bars by default.
- Source: `sell/dmi_bearish_crossover.py`, based on Wilder's Directional Movement System.
- Limitations: this is direction only; unlike the ADX strategy below, it imposes no trend-strength
  threshold.

### `short_aroon_bearish_reversal` (1.0.0)

- Trigger: Aroon Down crosses above Aroon Up.
- Formula: existing finite `aroon` calculator; recency of the most recent low/high over the
  configured interval.
- Parameter: `period=25` (2–200).
- History: `period + 2`; 27 bars by default.
- Source: `sell/aroon_bearish_reversal.py`, based on Tushar Chande's Aroon indicator.
- Limitations: equal extrema follow the established calculator's most-recent-extreme convention.

### `short_parabolic_sar_bearish_reversal` (1.0.0)

- Trigger: the shared Parabolic SAR direction changes from bullish to bearish on the latest bar.
- Formula: existing `ParabolicSARCalculator`, including acceleration-factor state and reversal
  rules.
- Parameters: `step=0.02` (0.001–0.5), `maximum=0.20` (0.001–1), with `maximum >= step`.
- History: 3 bars; recursive state continues thereafter.
- Source: `sell/parabolic_sar_reversal.py`, based on Wilder's Parabolic Time/Price System.
- Limitations: SAR is used only to identify the entry edge; it is not installed as a stop.

### `short_ichimoku_bearish_breakdown` (1.0.0)

- Trigger: close crosses below the lower of Senkou Span A and B as plotted at each signal bar.
- Formula: existing `ichimoku_cloud` helper. A cloud plotted at bar `t` is computed only from the
  window ending `displacement` bars before `t`; this prevents displaced-component look-ahead.
- Parameters: `tenkan_period=9` (2–100), `kijun_period=26` (3–200),
  `senkou_b_period=52` (4–400), `displacement=26` (1–200), with
  `tenkan <= kijun <= senkou_b`.
- History: `max(tenkan, kijun, senkou_b) + displacement + 1`; 79 bars by default.
- Source: `sell/ichimoku_bearish_exit.py`, based on Goichi Hosoda's Ichimoku Kinko Hyo and the
  StockCharts ChartSchool cloud convention.
- Intentional difference: the SELL reference fixes 9/26/52/26; the SHORT version exposes those
  canonical values as validated defaults without changing the time alignment.
- Limitations: Chikou Span is excluded because its plotted displacement is unsuitable for this
  causal entry test.

### `short_opening_range_breakdown` (1.0.0)

- Trigger: after the opening window has completed, close crosses below the low of every M1 bar in
  the 09:30 America/New_York opening range.
- Parameter: `opening_range_minutes=30` (1–120).
- History: `opening_range_minutes + 1`; 31 bars by default.
- Session semantics: all expected opening-range minutes must exist, and no signal is evaluated
  before `09:30 + opening_range_minutes`.
- Source: `sell/opening_range_breakdown.py`, an entry-role adaptation of Toby Crabel's opening
  range breakout method.
- Limitations: US-equity regular-session M1 bars only; an absent range minute suppresses the signal.

### `short_vwap_breakdown` (1.0.0)

- Trigger: `close[t-1] >= vwap_prev` and `close[t] < vwap`.
- Formula: consumes the existing shared indicator-service values `vwap_prev` and `vwap`; it does
  not calculate an independent VWAP.
- Parameters: none; the production session VWAP definition and reset policy are shared services.
- History: 2 positive-volume bars from the current session.
- Session semantics: the two crossing bars must be adjacent and from the same New York session;
  shared VWAP resets at the session boundary.
- Source: `sell/vwap_breakdown.py` and its documented textbook session-VWAP crossing rule.
- Limitations: missing, non-positive, stale, or unwarmed VWAP/bar input suppresses the signal.

### `short_bollinger_lower_band_breakdown` (1.0.0)

- Trigger: close moves from at-or-above the lower band to strictly below the lower band. This is
  a downside breakout, not a lower-band mean-reversion entry.
- Formula: existing `bollinger` helper: SMA plus/minus `deviations` times population standard
  deviation.
- Parameters: `period=20` (2–50), `deviations=2` (0.1–5).
- History: `period + 1`; 21 bars by default.
- Source: John Bollinger, *Bollinger on Bollinger Bands*, and the established repository Bollinger
  implementation used by `buy/bollinger_band_breakout.py`.
- Limitations: no squeeze or volume confirmation is added.

### `short_keltner_channel_breakdown` (1.0.0)

- Trigger: close crosses below the lower Keltner channel.
- Formula: existing `original_keltner` helper: SMA of typical price minus SMA of high-low range.
- Parameter: `period=10` (2–100).
- History: `period + 1`; 11 bars by default.
- Source: `sell/keltner_channel_breakdown.py`, preserving Chester Keltner's original channel
  variant as documented by StockCharts ChartSchool.
- Limitations: deliberately not the later EMA/ATR Keltner variant.

### `short_obv_breakdown` (1.0.0)

- Trigger: cumulative OBV crosses below the lowest OBV reading in the preceding
  `support_period` observations; the current observation is excluded from support.
- Formula: existing `OnBalanceVolumeCalculator`; add volume on an up close, subtract on a down
  close, and leave unchanged on an equal close.
- Parameter: `support_period=20` (2–200).
- History: `support_period + 2`; 22 bars by default.
- Source: `sell/obv_breakdown.py`, based on Joseph Granville's OBV support-break interpretation.
- Limitations: OBV's arbitrary additive origin is harmless to the relative break, but cumulative
  and support-window state must be restored; zero-volume observations are valid.

### `short_cci_zero_line_breakdown` (1.0.0)

- Trigger: `CCI[t-1] >= 0` and `CCI[t] < 0`.
- Formula: existing `cci` helper using typical price, its SMA, mean absolute deviation, and the
  0.015 Lambert constant.
- Parameter: `period=20` (2–200).
- History: `period + 1`; 21 bars by default.
- Source: `sell/cci_zero_line_breakdown.py`, based on Donald Lambert's Commodity Channel Index.
- Limitations: zero mean deviation leaves CCI undefined and suppresses the signal; no ±100 filter
  is added.

### `short_adx_bearish_trend_confirmation` (1.0.0)

- Trigger: ADX crosses from at-or-below to above `threshold` while the latest `-DI > +DI`.
- Formula: the same existing `ADXCalculator` used by `buy/adx_trend_following.py`: Wilder DMI,
  DX, SMA-seeded ADX, and Wilder recursive smoothing.
- Parameters: `period=14` (2–100), `threshold=25` (5–60).
- History: `2 * period + 1`; 29 bars by default.
- Source: J. Welles Wilder, *New Concepts in Technical Trading Systems* (1978), and the existing
  BUY implementation's StockCharts ChartSchool interpretation.
- Intentional difference: direction confirmation is inverted from `+DI > -DI` to `-DI > +DI`;
  the ADX edge remains an upward threshold crossing. A falling ADX never triggers this strategy.
- Limitations: when smoothed true range is zero, DI is unavailable and the shared calculator
  contributes zero DX during ADX seeding.

## Integration

All strategies are registered in both the production composition root and the research registry.
They therefore use the existing per-symbol SHORT collections and the same SHADOW, BACKTEST,
historical replay, reporting, persistence, and metadata paths as the initial SHORT strategies.
