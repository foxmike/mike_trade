# Final BUY/SELL Backlog: Reversal and Volume Strategies

## 1. Executive Summary

This document reports the technical investigation and implementation of the final four unassigned strategy candidates from the original technical strategy inventory:
1. **Intraday Exhaustion Reversal** (`intraday_exhaustion_reversal`, BUY)
2. **Volume Dry-Up Pullback** (`volume_dry_up_pullback`, BUY)
3. **Volatility Expansion Reversal** (`volatility_expansion_reversal`, SELL)
4. **Volume Climax Exit** (`volume_climax_exit`, SELL)

All four candidates have been successfully implemented with defensible, published classical technical analysis methodologies, clean distinction from existing library methods, transparent Type C engineering choices, strict causal boundary enforcement on completed regular-session M1 candles, and complete 1:1 production and research registry alignment.

### Repository & Branch Provenance
- **Base Commit**: `569da60c55421855e8cc87dcb6b79b52286b32c7` (`569da60`, `origin/feature/strategy-library-176-integrated`)
- **Feature Branch**: `feature/gemini-buy-sell-final-reversals`
- **Worktree**: `/home/mike/dev/mike/stocks/python/work/mike_trade-gemini-final-reversals`

### Strategy Library Counts
| Role | Starting Baseline | Final Reversals Additions | Final Total |
| :--- | :---: | :---: | :---: |
| **BUY** | 80 | +2 | **82** |
| **SELL** | 40 | +2 | **42** |
| **SHORT** | 28 | 0 | **28** |
| **COVER** | 28 | 0 | **28** |
| **TOTAL** | **176** | **+4** | **180** |

Both registration roots (`mike_trade/app.py::build_runtime` and `mike_trade/research/commands.py::_research_registry`) maintain 100% 1:1 parity across all 180 strategy classes and unique IDs without duplicates.

---

## 2. Technical Evaluation of Candidates

### Candidate 1 — Intraday Exhaustion Reversal (BUY)
- **Outcome**: **IMPLEMENTED**
- **Strategy ID**: `intraday_exhaustion_reversal`
- **Role**: `StrategyRole.BUY`
- **Published Source (Type B)**:
  - Al Brooks, *Reading Price Charts Bar by Bar: The Technical Analysis of Price Action for the Serious Trader* (Wiley, 2009), Chapter on Climactic Reversals and Exhaustion Bars.
  - Richard Wyckoff & Tom Williams, *Master the Markets* (1993), "Selling Climax and Stopping Volume".
  - Thomas Bulkowski, *Encyclopedia of Candlestick Charts* (Wiley, 2008), Exhaustion Bar Reversals.
- **Exact Trigger Condition**:
  BUY on the completed confirmation bar `c` (bar `t`) when:
  1. **Prior Decline**: The candle immediately preceding the exhaustion bar (`bars[-3]`) closed lower than the close `trend_bars` earlier (`bars[-3].close < bars[-3 - trend_bars].close`).
  2. **Exhaustion Bar `e` (bar `t-1`)**:
     - Breaks out to a new extreme low: `e.low < min(prior_lows)` over `lookback` preceding bars.
     - True Range / spread expansion: `e.high - e.low >= range_multiple * avg_atr` (finite ATR over `atr_period=14` bars preceding `e`).
     - Volume surge: `e.volume >= volume_multiple * avg_vol` (finite volume SMA over `volume_period=20` bars preceding `e`).
     - Absorption / lower rejection wick: Price rejects the lows, closing off the low: `(e.close - e.low) >= 0.35 * (e.high - e.low)`.
  3. **Bullish Confirmation on Completed Bar `c` (bar `t`)**:
     - Bullish candle: `c.close > c.open` and `c.close > e.close`.
     - Closes above the midpoint of the exhaustion candle: `c.close > (e.high + e.low) / 2`.
- **Engineering Choices (Type C)**:
  - Fixed baseline lookbacks: 20-bar volume SMA and 14-bar ATR ending at `bars[-3]` (strictly before the exhaustion bar).
  - Default `range_multiple = Decimal("1.5")`, default `volume_multiple = Decimal("1.5")`.
  - Midpoint recovery threshold (`c.close > e_mid`) ensures decisive institutional absorption rather than a weak tick.
- **Parameters & Defaults**:
  - `trend_bars`: int, default 4, bounds [2, 30]
  - `lookback`: int, default 10, bounds [5, 50]
  - `range_multiple`: Decimal, default 1.5, bounds [1.0, 5.0]
  - `volume_multiple`: Decimal, default 1.5, bounds [1.0, 5.0]
- **History & Warmup**:
  `_required_bars = max(22, 17, lookback + 2, trend_bars + 3)`. Default warmup: 22 bars.
- **Distinction from Existing Strategies**:
  - `rsi_oversold_reversal` / `stochastic_oversold_reversal`: Evaluate oscillator thresholds without price-action range expansion, volume surge, or candlestick structure.
  - `hammer_reversal`: Tests single hammer shape on `t-1`; requires no volume surge, no ATR range expansion, and no channel breakdown.
  - `keltner_channel_reversion`: Band crossing, not volume/spread climactic absorption.
  - `gap_reversal`: Requires session-open gap down, whereas `intraday_exhaustion_reversal` operates intraday on contiguous bars.

---

### Candidate 2 — Volume Dry-Up Pullback (BUY)
- **Outcome**: **IMPLEMENTED**
- **Strategy ID**: `volume_dry_up_pullback`
- **Role**: `StrategyRole.BUY`
- **Published Source (Type B)**:
  - William J. O'Neil, *How to Make Money in Stocks* (4th ed., McGraw-Hill, 2009), "Volume Contraction on Constructive Pullbacks".
  - Gil Morales & Chris Kacher, *Trade Like an O'Neil Disciple* (Wiley, 2010), "Volume Dry-Up (VDU) Setups and Pocket Pivots".
  - Mark Minervini, *Trade Like a Stock Market Wizard* (McGraw-Hill, 2013), "Low-Volume Pullbacks in Stage 2 Uptrends".
- **Exact Trigger Condition**:
  BUY on completed resumption bar `latest` (bar `t`) when:
  1. **Established Uptrend**: Close at the pre-pullback trend peak (`bars[-1 - pullback]`) was higher than at the trend start (`bars[-1 - pullback - trend]`).
  2. **Constructive Pullback**: Over `pullback_bars` (default 3), price corrects lower (`min_pullback_close < trend_peak.close`) but remains above the trend start low (`min_pullback_low > trend_start.low`).
  3. **Volume Dry-Up**:
     - Average volume during the pullback is at or below `dry_up_factor` (default 0.70, i.e. 70%) of the preceding 20-bar volume baseline SMA.
     - Final bar of the pullback had volume strictly below the baseline volume SMA (`bars[-2].volume < avg_baseline_vol`).
  4. **Bullish Resumption on Latest Bar**:
     - Bullish candle: `latest.close > latest.open` and `latest.close > pullback[-1].close`.
     - Breaks above prior pullback bar high: `latest.close > pullback[-1].high`.
     - Expanding volume: `latest.volume > pullback[-1].volume` and `latest.volume >= 0.85 * avg_baseline_vol`.
- **Engineering Choices (Type C)**:
  - 20-bar baseline volume SMA evaluated prior to the pullback window.
  - Default dry-up factor of 0.70; final pullback bar must finish below average volume to confirm seller exhaustion.
  - Resumption volume expansion requiring at least 85% of baseline average volume and exceeding the prior dry-up bar volume.
- **Parameters & Defaults**:
  - `trend_bars`: int, default 5, bounds [2, 30]
  - `pullback_bars`: int, default 3, bounds [2, 10]
  - `volume_period`: int, default 20, bounds [5, 100]
  - `dry_up_factor`: Decimal, default 0.70, bounds [0.20, 0.95]
- **History & Warmup**:
  `_required_bars = pullback_bars + max(volume_period, trend_bars) + 1`. Default warmup: 24 bars.
- **Distinction from Existing Strategies**:
  - `first_pullback_buy`: Restrained to opening 90 minutes (09:30–11:00 ET) and tracks a stateful single morning impulse. `volume_dry_up_pullback` evaluates continuous intraday rolling windows throughout the regular session without time-of-day lockouts.
  - `ema_pullback` / `holy_grail_pullback`: Pure price/moving-average touches with no volume contraction or dry-up evaluation.
  - `bull_flag_breakout`: Requires sharp impulsive flagpole (>= 1% jump within 15 bars) and channel boundary break.

---

### Candidate 3 — Volatility Expansion Reversal (SELL)
- **Outcome**: **IMPLEMENTED**
- **Strategy ID**: `volatility_expansion_reversal`
- **Role**: `StrategyRole.SELL`
- **Published Source (Type B)**:
  - Toby Crabel, *Day Trading with Short Term Price Patterns* (Traders Press, 1990), "Expansion Reversal and Outside Reversals".
  - Robert D. Edwards & John Magee, *Technical Analysis of Stock Trends* (5th/9th ed.), "The Key Reversal".
  - Perry Kaufman, *Trading Systems and Methods* (5th ed., Wiley), "Volatility Reversals and Outside Bars".
  - John J. Murphy, *Technical Analysis of the Financial Markets* (NYIF, 1999), "Key Reversal Days".
- **Exact Trigger Condition**:
  SELL (exit long) on the completed reversal bar `latest` (bar `t`) when:
  1. **Prior Uptrend**: Close at `t-1` was higher than close `trend_bars` earlier (`bars[-2].close > bars[-2 - trend_bars].close`).
  2. **Volatility Expansion**: True Range of `latest` is at least `expansion_multiple` (default 1.5) times the baseline ATR(14) ending at `t-1`.
  3. **Bearish Outside Key Reversal**:
     - Price thrusts to a higher high: `latest.high > bars[-2].high`.
     - Violent downside rejection: `latest.close < bars[-2].low`.
     - Bearish candle: `latest.close < latest.open`.
     - Closes in the lower 35% of its expanded range: `(latest.close - latest.low) <= 0.35 * (latest.high - latest.low)`.
- **Engineering Choices (Type C)**:
  - Finite ATR(14) calculated up to `t-1` avoids look-ahead expansion bias.
  - Lower 35% close location threshold enforces genuine bearish rejection settlement.
- **Parameters & Defaults**:
  - `trend_bars`: int, default 5, bounds [2, 30]
  - `atr_period`: int, default 14, bounds [2, 50]
  - `expansion_multiple`: Decimal, default 1.5, bounds [1.0, 5.0]
- **History & Warmup**:
  `_required_bars = max(atr_period + 1, trend_bars) + 2`. Default warmup: 17 bars.
- **Distinction from Existing Strategies**:
  - `bearish_engulfing_exit`: Only tests real bodies; does not require high > prior high, does not require close < prior low, and does not measure True Range or ATR expansion.
  - `shooting_star_reversal`: Requires small body at the bottom and 2x upper shadow on `t-1` with next-bar confirmation.
  - `bollinger_upper_band_reversal`: Tagging and falling back inside Bollinger bands.
  - `high_volume_reversal`: Evaluates volume surge (>= 2x volume SMA); does not measure True Range expansion over ATR and does not require an outside-bar breakdown (`high > prior.high` and `close < prior.low`).

---

### Candidate 4 — Volume Climax Exit (SELL)
- **Outcome**: **IMPLEMENTED**
- **Strategy ID**: `volume_climax_exit`
- **Role**: `StrategyRole.SELL`
- **Published Source (Type B)**:
  - Richard D. Wyckoff, *The Richard D. Wyckoff Method of Trading and Investing in Stocks* (1931), "The Buying Climax and Distribution".
  - Tom Williams, *Master the Markets* (1993), "Buying Climax and Up-Thrusts".
  - Jim Forte, CMT, *Anatomy of a Trading Range* (MTA Journal), "Wyckoff Phase A: Buying Climax and Automatic Reaction".
- **Exact Trigger Condition**:
  SELL (exit long) on completed Automatic Reaction bar `reaction` (bar `t`) when:
  1. **Prior Advance**: Close at `t-2` was higher than close `trend_bars` earlier (`bars[-3].close > bars[-3 - trend_bars].close`).
  2. **Buying Climax Bar `climax` (bar `t-1`)**:
     - Sets a new swing high over `lookback` preceding bars (`climax.high > max(prior_highs)`).
     - Climactic volume: `climax.volume >= climax_multiple * avg_vol` (volume baseline SMA over 20 bars ending at `t-2`).
     - Wide price spread: `climax.high - climax.low >= spread_multiple * avg_atr` (ATR baseline over 14 bars ending at `t-2`).
     - Failure to sustain highs: Upper rejection shadow >= 35% of spread, or close settled in the bottom 55% of the bar, confirming distribution.
  3. **Automatic Reaction Bar `reaction` (bar `t`)**:
     - Downward confirmation candle: `reaction.close < reaction.open` and `reaction.close < climax.close`.
     - Closes strictly below the midpoint of the climax candle: `reaction.close < (climax.high + climax.low) / 2`.
- **Engineering Choices (Type C)**:
  - Climax volume threshold: 2.5x 20-bar baseline volume SMA.
  - Spread threshold: 1.25x 14-bar baseline ATR.
  - Midpoint reaction penetration: `reaction.close < climax_mid` provides objective proof that supply has overtaken demand.
- **Parameters & Defaults**:
  - `trend_bars`: int, default 5, bounds [2, 30]
  - `lookback`: int, default 15, bounds [5, 50]
  - `climax_multiple`: Decimal, default 2.5, bounds [1.5, 10.0]
  - `spread_multiple`: Decimal, default 1.25, bounds [1.0, 5.0]
- **History & Warmup**:
  `_required_bars = max(22, 17, lookback + 2, trend_bars + 3)`. Default warmup: 22 bars.
- **Distinction from Existing Strategies**:
  - `high_volume_reversal`: In `high_volume_reversal`, the red bearish candle itself carries the high volume at `t`. In `volume_climax_exit`, the climactic volume occurs on the *thrust bar at `t-1`* (the Buying Climax) and the exit is triggered on the *Automatic Reaction at `t`* confirming institutional distribution.
  - `money_flow_index_reversal` / `chaikin_money_flow_reversal`: Oscillator thresholds, not price-spread climax geometry.

---

## 3. Inventory Reconciliation

The four rows in `docs/TECHNICAL_STRATEGY_INVENTORY.md` have been updated from `REQUIRES_SOURCE_DEFINITION` to `IMPLEMENTED`:
- Line 76: `Intraday Exhaustion Reversal` -> `intraday_exhaustion_reversal` (BUY)
- Line 134: `Volume Dry-Up Pullback` -> `volume_dry_up_pullback` (BUY)
- Line 183: `Volatility Expansion Reversal` -> `volatility_expansion_reversal` (SELL)
- Line 189: `Volume Climax Exit` -> `volume_climax_exit` (SELL)

The deferred notes section has been updated accordingly.

---

## 4. Focused Verification Results

All new and directly affected test suites execute cleanly:
- `tests/unit/test_buy_sell_final_reversals.py`: **20 passed**
  - Registration and StrategySpec attribute verification (unique ID, version, side, category)
  - Schema parameter bounds and validation errors
  - Positive triggers for all 4 strategies
  - No-trigger cases (lack of trend, insufficient volume, failed breakout, normal range)
  - Edge triggers and duplicate signal suppression via `last_signal_bar`
  - Missing-bar contiguity and session gap rejection
- `tests/unit/test_short_cover_strategies.py`: **42 passed**
  - Reconciled 180 total strategy distribution assertion (82 BUY, 42 SELL, 28 SHORT, 28 COVER)
  - Zero duplicate registrations

Total focused test suite: **62 tests passed, 0 failures**.

---

## 5. Execution Limitations

1. **Advisory Scope**: All four strategies emit advisory signals only (`SignalAction.ENTER_LONG` for BUY, `SignalAction.EXIT_LONG` for SELL). Order sizing, margin allocation, protective stop placement, and broker routing remain engine-governed.
2. **Intraday Granularity**: Adapting classical daily chart concepts to 1-minute intraday bars means microstructure noise can occasionally mimic climactic or expansion patterns.
3. **Contiguity Requirement**: All calculations strictly enforce adjacent bars (`contiguous(bars)`). Any missing data gap or overnight boundary returns `ReasonCode.INSUFFICIENT_DATA`.
