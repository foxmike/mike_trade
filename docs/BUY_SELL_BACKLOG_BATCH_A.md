# BUY/SELL Backlog — Gemini Batch A

This document details the outcomes, methodologies, references, parameters, state requirements, and equivalence determinations for the four candidates assigned from the original technical strategy inventory.

---

## Executive Summary

| Candidate Name | Role | Proposed ID | Determination | Implementation / Mapping ID | Notes |
|---|---|---|---|---|---|
| Bollinger Band Mean Reversion | BUY | `bollinger_band_mean_reversion` | **IMPLEMENTED** | `bollinger_band_mean_reversion` | Lower-band penetration followed by confirmed re-entry above the band. |
| VWAP Pullback | BUY | `vwap_pullback` | **IMPLEMENTED** | `vwap_pullback` | Intraday trend pullback to session VWAP holding support, confirmed by close > setup high. Distinct from `vwap_bounce`. |
| Support Bounce | BUY | `support_bounce` | **IMPLEMENTED** | `support_bounce` | Point-in-time confirmed swing low support tested within tolerance, confirmed by close > setup high and > support. |
| Bollinger Middle Band Exit | SELL | `bollinger_middle_band_exit` | **EXISTING_EQUIVALENT** | `price_below_sma` | Middle band is mathematically identical to SMA(period). Long exit on close < middle band is functionally equivalent to `PriceBelowSmaSell` with `period=20`. |

With 3 newly implemented BUY strategies and 1 documented equivalent SELL candidate:
- **BUY strategies**: 75 + 3 = **78**
- **SELL strategies**: 40 + 0 = **40**
- **SHORT strategies**: **3**
- **COVER strategies**: **3**
- **Total Registered Library**: **124**

---

## Implemented Strategies

### 1. Bollinger Band Mean Reversion

- **Exact Name**: Bollinger Band Mean Reversion
- **Strategy ID**: `bollinger_band_mean_reversion`
- **Role**: `StrategyRole.BUY`
- **Category / Subcategory**: `technical` / `mean_reversion`
- **File**: [`mike_trade/strategies/buy/bollinger_band_mean_reversion.py`](file:///home/mike/dev/mike/stocks/python/work/mike_trade-gemini-buy-sell-backlog-a/mike_trade/strategies/buy/bollinger_band_mean_reversion.py)
- **Semantic Version**: `1.0.0`

#### Signal Methodology
Identifies an oversold price excursion below the lower Bollinger Band followed by an edge-triggered recovery back inside the band:
1. **Excursion**: Prior completed close was strictly below the prior lower Bollinger Band: `close[t-1] < lower_band[t-1]`.
2. **Re-entry**: Latest completed close crosses back strictly above the latest lower Bollinger Band: `close[t] > lower_band[t]`.
3. **Execution**: Evaluated on adjacent completed M1 bars.

#### Source / Reference
- John Bollinger, *Bollinger on Bollinger Bands* (McGraw-Hill, 2001).
- Official band formulas and reversal interpretations: [Bollinger Bands Official](https://www.bollingerbands.com/bollinger-bands) and [StockCharts ChartSchool Bollinger Bands](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/bollinger-bands).

#### Parameters and Defaults
- `period` (INT): Default `20`, min `2`, max `100`. Bollinger SMA window length.
- `deviations` (DECIMAL): Default `Decimal("2")`, min `Decimal("0.1")`, max `Decimal("5")`. Population standard deviation multiplier for band width.

#### Warmup & Required Bars
- `required_bars(params) = period + 1` (default 21 completed bars).

#### Session Behavior & Data Contracts
- Evaluated on completed regular-session 1-minute bars (`CandleTimeframe.M1`).
- Non-regular sessions (`SessionStatus != REGULAR`) or stale data (`freshness.stale`) yield `NOT_ELIGIBLE` or `STALE_DATA`.
- Bars must be adjacent completed bars (`adjacent(bars[-2], bars[-1])`).

#### State Requirements
- Persistent state keys: `("last_signal_bar",)` for same-bar replay suppression.

#### Distinguishing Features & Known Limitations
- Distinct from `bollinger_band_breakout` (which enters on upper-band breakouts) and `bollinger_squeeze` (which requires a 125-bar BandWidth low squeeze before breakout).
- Distinct from `keltner_channel_reversion` (which uses Wilder ATR and EMA channels rather than SMA and population standard deviation).
- Mirrors the existing `BollingerUpperBandReversalSell` exit strategy.
- Advisory BUY entry signal only: position sizing, risk controls, and protective stops are owned by the engine.

---

### 2. VWAP Pullback

- **Exact Name**: VWAP Pullback
- **Strategy ID**: `vwap_pullback`
- **Role**: `StrategyRole.BUY`
- **Category / Subcategory**: `technical` / `vwap`
- **File**: [`mike_trade/strategies/buy/vwap_pullback.py`](file:///home/mike/dev/mike/stocks/python/work/mike_trade-gemini-buy-sell-backlog-a/mike_trade/strategies/buy/vwap_pullback.py)
- **Semantic Version**: `1.0.0`

#### Signal Methodology
Captures a pullback entry toward session VWAP during an established intraday bullish trend:
1. **Prior Trend**: Pre-setup session bars (`t - trend_bars - 1` to `t - 2`) were trading strictly above VWAP (`low > vwap_prev * (1 + tolerance_pct/100)` and `close >= vwap_prev`).
2. **Pullback Test (Setup Bar `t-1`)**: Price pulls back to test session VWAP within tolerance (`low <= vwap_prev * (1 + tolerance_pct/100)`), while close holds VWAP support (`close >= vwap_prev`).
3. **Confirmation (Signal Bar `t`)**: Latest completed bar confirms continuation by closing strictly above the setup bar's high (`close > setup.high`) and strictly above current session VWAP (`close > vwap_latest`).

#### Source / Reference
- Brian Shannon, CMT, *Technical Analysis Using Multiple Timeframes* (2008), Chapter on VWAP pullback entries.
- Brian Pezim, *How to Day Trade for a Living* (2016), "VWAP Pullback Strategy".
- Andrew Aziz, *How to Day Trade for a Living* (2016), Chapter 7: "VWAP Trading".

#### Parameters and Defaults
- `trend_bars` (INT): Default `3`, min `1`, max `20`. Minimum prior completed session bars establishing the uptrend above VWAP before the pullback.
- `tolerance_pct` (DECIMAL): Default `Decimal("0.1")`, min `Decimal("0.0")`, max `Decimal("2.0")`. Maximum distance between the setup bar's low and VWAP as a percentage of VWAP.

#### Warmup & Required Bars
- `required_bars(params) = trend_bars + 2` (default 5 completed session bars).

#### Session Behavior & Data Contracts
- Regular session only; completed 1-minute bars with strictly positive volume (`volume > 0`).
- Session isolation: bars and VWAP windows never span sessions; VWAP resets at the start of each regular session.
- Context indicators: reads warmed per-bar session VWAP (`"vwap"` and `"vwap_prev"`) from `MarketContext`.

#### State Requirements
- Persistent state keys: `("session_id", "last_signal_bar")`.

#### Distinguishing Features from `vwap_bounce`
- **Precondition**: `vwap_bounce` requires `prior.close <= vwap_prev` (price was below or at VWAP, an upward crossing/reclaim). In contrast, `vwap_pullback` requires an established trend above VWAP where setup bar `close >= vwap_prev` (support held).
- **Trigger**: `vwap_bounce` triggers on any bar crossing VWAP. `vwap_pullback` requires a pullback touch followed by a subsequent bar closing above the pullback bar's high (`close > setup.high`).
- **Mutual Exclusivity**: If `prior.close <= vwap_prev`, `vwap_pullback` never signals; if `prior.close >= vwap_prev`, `vwap_bounce` never signals.

---

### 3. Support Bounce

- **Exact Name**: Support Bounce
- **Strategy ID**: `support_bounce`
- **Role**: `StrategyRole.BUY`
- **Category / Subcategory**: `technical` / `price_action`
- **File**: [`mike_trade/strategies/buy/support_bounce.py`](file:///home/mike/dev/mike/stocks/python/work/mike_trade-gemini-buy-sell-backlog-a/mike_trade/strategies/buy/support_bounce.py)
- **Semantic Version**: `1.0.0`

#### Signal Methodology
Buys a confirmed rebound from previously established technical support:
1. **Point-in-Time Support Level `S`**: Identifies the latest confirmed fractal swing low (`strength` bars on each side) in the lookback window strictly before the setup bar (`bars[-lookback - 2 : -2]`).
2. **Support Test (Setup Bar `t-1`)**: Price tests the support zone: `setup.low <= S * (1 + tolerance_pct/100)`, while holding support without breaking down: `setup.close >= S * (1 - tolerance_pct/100)` and `setup.low >= S * (1 - tolerance_pct/100)`.
3. **Rebound Confirmation (Signal Bar `t`)**: Confirms the bounce by closing strictly above the setup bar's high (`close > setup.high`) and strictly above the support level (`close > S`).

#### Source / Reference
- Robert D. Edwards and John Magee, *Technical Analysis of Stock Trends* (support zones and rebound confirmation).
- John J. Murphy, *Technical Analysis of the Financial Markets* (1999), Chapter 4: "Support and Resistance".
- Bill Williams, *Trading Chaos* (Fractal swing lows as structural support).

#### Parameters and Defaults
- `strength` (INT): Default `2`, min `1`, max `10`. Completed bars on each side required to confirm a fractal swing low.
- `lookback` (INT): Default `50`, min `10`, max `200`. Completed bars searched prior to setup for the support pivot. Must satisfy `lookback >= 2 * strength + 2`.
- `tolerance_pct` (DECIMAL): Default `Decimal("0.2")`, min `Decimal("0.0")`, max `Decimal("2.0")`. Maximum distance from support for the test bar, percent.

#### Warmup & Required Bars
- `required_bars(params) = lookback + 2` (default 52 completed bars).

#### Session Behavior & Data Contracts
- Evaluated on adjacent completed M1 bars during regular market sessions.
- No look-ahead bias: the pivot low is confirmed strictly in the past (at least `strength` bars prior to setup).

#### State Requirements
- Persistent state keys: `("last_signal_bar",)`.

#### Distinguishing Features & Known Limitations
- Distinct from `double_bottom_reversal`: does not require two lows or an interim peak breakout; enters directly at support upon bounce confirmation.
- Distinct from `resistance_breakout`: operates on swing low support rather than swing high resistance.
- Distinct from `previous_swing_low_break` (which is a SELL exit on support breakdowns).
- Distinct from `previous_day_low_reversal` (which uses prior day's session low rather than intraday fractal support).

---

## Equivalent / Unresolved Candidates

### 4. Bollinger Middle Band Exit

- **Candidate Name**: Bollinger Middle Band Exit
- **Proposed ID**: `bollinger_middle_band_exit`
- **Role**: `StrategyRole.SELL`
- **Category / Subcategory**: `technical` / `moving_average` (originally classified under `volatility_channel`)
- **Status**: **`EXISTING_EQUIVALENT`**
- **Mapped Implementation ID**: `price_below_sma` (implemented in [`mike_trade/strategies/sell/price_below_sma.py`](file:///home/mike/dev/mike/stocks/python/work/mike_trade-gemini-buy-sell-backlog-a/mike_trade/strategies/sell/price_below_sma.py))

#### Analysis & Reason for Equivalence Determination
1. **Mathematical Definition**: The middle band of Bollinger Bands is universally defined by John Bollinger as a simple moving average: `middle_band = SMA(close, period)` (default period 20). It contains no standard deviation multiplier, no true range component, and no volatility offset.
2. **Source Specification**: In John Bollinger's *Bollinger on Bollinger Bands* (McGraw-Hill, 2001, Chapter 15 "Method I: Volatility Breakout"), the middle band exit rule states: *"We exit when the close crosses below the 20-day moving average [middle band]."*
3. **Existing Implementation**: Mike_Trade already provides `PriceBelowSmaSell` (`price_below_sma`), which evaluates `close[t] < SMA(period)[t]` on completed closed bars, with configurable `period` (default 50, supporting range 2 to 400).
4. **Duplicate Avoidance**: A dedicated `BollingerMiddleBandExitSell` class checking `close < middle_band` would be line-for-line identical to `PriceBelowSmaSell` configured with `period=20`. Standard deviation and bandwidth are completely unused in the middle-band exit.
5. In accordance with system design principles and backlog instructions ("Do not create a renamed duplicate of Price Below SMA... Keep the implementation count truthful"), `Bollinger Middle Band Exit` is classified as `EXISTING_EQUIVALENT` to `price_below_sma`.
