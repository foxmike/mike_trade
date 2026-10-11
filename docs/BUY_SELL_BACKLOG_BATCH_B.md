# BUY/SELL Backlog — Gemini Batch B

This document details the outcomes, methodologies, references, parameters, state requirements, and equivalence determinations for the four candidates assigned in Batch B from the authoritative technical strategy inventory.

---

## Executive Summary

| Candidate Name | Role | Proposed ID | Determination | Implementation / Mapping ID | Notes |
|---|---|---|---|---|---|
| VWAP Reclaim | BUY | `vwap_reclaim` | **EXISTING_EQUIVALENT** | `vwap_bounce` | Bullish reclaim from below session VWAP is mathematically identical to the existing `VwapBounceBuy` upward crossing rule. |
| VWAP Band Reversion | BUY | `vwap_band_reversion` | **IMPLEMENTED** | `vwap_band_reversion` | Point-in-time session cumulative volume-weighted deviation bands ($VWAP - M \cdot \sigma$); lower-band penetration and recovery. |
| Intraday Consolidation Breakout | BUY | `intraday_consolidation_breakout` | **IMPLEMENTED** | `intraday_consolidation_breakout` | Rectangular range compression over $N$ completed bars strictly preceding the breakout bar; confirmed upside close above ceiling. |
| High Volume Reversal | SELL | `high_volume_reversal` | **REQUIRES_SOURCE_DEFINITION** | — | Published literature treats volume climaxes contextually without a canonical threshold/pattern combination; unresolved. |

With 2 newly implemented BUY strategies, 1 documented equivalent candidate, and 1 unresolved candidate:
- **BUY strategies**: 78 + 2 = **80**
- **SELL strategies**: 40 + 0 = **40**
- **SHORT strategies**: **3**
- **COVER strategies**: **3**
- **Total Registered Library**: **126**

---

## Candidate 1: VWAP Reclaim (BUY) — Equivalent

### Determination: `EXISTING_EQUIVALENT` to `vwap_bounce`

#### Analysis & Equivalence
- **Concept**: Identify a bullish reclaim of VWAP after price traded at or below it.
- **Existing Strategy Inspection**: `VwapBounceBuy` ([`mike_trade/strategies/buy/vwap_bounce.py`](file:///home/mike/dev/mike/stocks/python/work/mike_trade-gemini-buy-sell-backlog-b/mike_trade/strategies/buy/vwap_bounce.py)) evaluates:
  $$\text{prior.close} \le \text{vwap\_prev} \quad \text{AND} \quad \text{latest.close} > \text{vwap\_latest}$$
- **Equivalence**: In technical analysis literature, "reclaiming" VWAP specifically refers to price crossing from at-or-below VWAP back above VWAP. This is identical to the exact condition already implemented by `VwapBounceBuy`.
- **Decision**: To avoid creating duplicate strategies under different names or fabricating arbitrary extra conditions merely to manufacture artificial difference, `vwap_reclaim` is recorded as equivalent to `vwap_bounce`. No redundant strategy class is registered.

---

## Candidate 2: VWAP Band Reversion (BUY) — Implemented

### 1. Specification

- **Exact Name**: VWAP Band Reversion
- **Strategy ID**: `vwap_band_reversion`
- **Role**: `StrategyRole.BUY`
- **Category / Subcategory**: `technical` / `vwap`
- **File**: [`mike_trade/strategies/buy/vwap_band_reversion.py`](file:///home/mike/dev/mike/stocks/python/work/mike_trade-gemini-buy-sell-backlog-b/mike_trade/strategies/buy/vwap_band_reversion.py)
- **Reusable Calculator**: [`mike_trade/strategies/indicators/vwap_bands.py`](file:///home/mike/dev/mike/stocks/python/work/mike_trade-gemini-buy-sell-backlog-b/mike_trade/strategies/indicators/vwap_bands.py)
- **Semantic Version**: `1.0.0`

### 2. Exact Mathematical Calculation

Session VWAP and volume-weighted standard deviation bands are computed point-in-time from regular session completed 1-minute bars (accumulators reset to zero at the start of each regular trading session):

For session bars $i = 0, \dots, k$:
$$TP_i = \frac{\text{High}_i + \text{Low}_i + \text{Close}_i}{3}$$
$$V_i = \text{Volume}_i$$

Accumulators:
$$CV_k = \sum_{i=0}^k V_i, \quad CVP_k = \sum_{i=0}^k (TP_i \cdot V_i), \quad CVPSq_k = \sum_{i=0}^k (TP_i^2 \cdot V_i)$$

Point-in-Time Statistics at bar $k$:
$$VWAP_k = \frac{CVP_k}{CV_k}$$
$$\sigma_k^2 = \max\left(0, \frac{CVPSq_k}{CV_k} - VWAP_k^2\right), \quad \sigma_k = \sqrt{\sigma_k^2}$$
$$\text{LowerBand}_k = VWAP_k - \text{multiplier} \cdot \sigma_k$$
$$\text{UpperBand}_k = VWAP_k + \text{multiplier} \cdot \sigma_k$$

Zero-volume bars do not increment accumulators. If $CV_k = 0$, the calculation returns `None`.

### 3. Signal Methodology

On completed regular-session 1-minute bars:
1. **Oversold Excursion**: Prior completed close was strictly below the lower VWAP deviation band:
   $$\text{close}[t-1] < \text{LowerBand}[t-1]$$
2. **Reclaim / Recovery**: Latest completed close crosses back strictly above the lower VWAP deviation band:
   $$\text{close}[t] > \text{LowerBand}[t]$$

### 4. Source / Reference
- Brian Shannon, CMT, *Technical Analysis Using Multiple Timeframes* (2008) and *Maximum Trading Gains With Anchored VWAP* (2023) (standard deviation bands anchored to session VWAP).
- TradingView / StockCharts canonical volume-weighted session standard deviation band calculations.

### 5. Parameters and Defaults
- `multiplier` (DECIMAL): Default `Decimal("2.0")`, min `Decimal("0.5")`, max `Decimal("4.0")`. Standard deviation multiplier for lower band.
- `min_session_bars` (INT): Default `5`, min `2`, max `60`. Minimum completed session bars before evaluating band reversion, ensuring bands have formed a stable distribution.

### 6. Distinguishing Features
- **Distinct from `vwap_bounce`**: `vwap_bounce` triggers on crossing the central session VWAP line from below. `vwap_band_reversion` triggers on recovering above the lower deviation band (typically 2 standard deviations below VWAP).
- **Distinct from `bollinger_band_mean_reversion`**: Bollinger Bands use an unweighted rolling SMA and unweighted standard deviation over a fixed finite window (e.g. 20 bars). `vwap_band_reversion` uses cumulative volume-weighted statistics resetting at each session open.
- **Distinct from `vwap_mean_reversion`**: Anchors specifically to statistical volume-weighted deviation bands rather than arbitrary percentage thresholds or distance from the central mean.

---

## Candidate 3: Intraday Consolidation Breakout (BUY) — Implemented

### 1. Specification

- **Exact Name**: Intraday Consolidation Breakout
- **Strategy ID**: `intraday_consolidation_breakout`
- **Role**: `StrategyRole.BUY`
- **Category / Subcategory**: `technical` / `breakout`
- **File**: [`mike_trade/strategies/buy/intraday_consolidation_breakout.py`](file:///home/mike/dev/mike/stocks/python/work/mike_trade-gemini-buy-sell-backlog-b/mike_trade/strategies/buy/intraday_consolidation_breakout.py)
- **Semantic Version**: `1.0.0`

### 2. Signal Methodology & Breakout Boundary

1. **Consolidation Base Window**:
   Evaluates a rectangular base formed over the preceding $N$ completed bars strictly prior to the breakout bar (bars $t - N$ to $t - 1$):
   $$\text{Ceiling} = \max_{i=t-N}^{t-1} \text{High}_i$$
   $$\text{Floor} = \min_{i=t-N}^{t-1} \text{Low}_i$$
   **Important**: The breakout candle ($t$) strictly **does not participate** in defining the consolidation range, ensuring point-in-time verification against established resistance.
2. **Compression Qualification**:
   The consolidation base must satisfy the maximum width threshold:
   $$\text{RangePct} = \frac{\text{Ceiling} - \text{Floor}}{\text{Floor}} \times 100 \le \text{max\_range\_pct}$$
3. **Prior Position**:
   The prior completed bar ($t-1$) closed within or at the consolidation ceiling:
   $$\text{Close}_{t-1} \le \text{Ceiling}$$
4. **Breakout Confirmation**:
   The latest completed bar ($t$) confirms the upside breakout by closing strictly above the ceiling:
   $$\text{Close}_t > \text{Ceiling}$$

### 3. Source / Reference
- Thomas Bulkowski, *Encyclopedia of Chart Patterns* (2021), "Rectangles / Consolidation Ranges".
- Toby Crabel, *Day Trading with Short Term Price Patterns* (1990) (range compression preceding directional expansion).
- John J. Murphy, *Technical Analysis of the Financial Markets* (1999), Chapter 6: "Continuation Patterns - Consolidation Rectangles".

### 4. Parameters and Defaults
- `consolidation_bars` (INT): Default `15`, min `5`, max `60`. Number of completed bars forming the consolidation window strictly preceding the breakout bar.
- `max_range_pct` (DECIMAL): Default `Decimal("0.75")`, min `Decimal("0.1")`, max `Decimal("3.0")`. Maximum width of the consolidation base as a percentage of floor support.

### 5. Distinguishing Features
- **Distinct from `opening_range_breakout`**: ORB is anchored exclusively to 09:30 market open. `intraday_consolidation_breakout` evaluates rolling intraday bases formed at any time during the regular session.
- **Distinct from `donchian_channel_breakout`**: Donchian breakouts trigger on any rolling channel high regardless of channel width. `intraday_consolidation_breakout` strictly enforces a compression threshold (`max_range_pct`).
- **Distinct from `volatility_squeeze_breakout`**: Volatility squeeze relies on Bollinger Bands within Keltner Channels plus momentum histogram. `intraday_consolidation_breakout` uses rectangular price action boundaries.
- **Distinct from `high_of_day_momentum` / `previous_day_high_breakout`**: Breaks the local consolidation ceiling rather than running session or prior-day high.

---

## Candidate 4: High Volume Reversal (SELL) — Unresolved

### Determination: `REQUIRES_SOURCE_DEFINITION`

#### Analysis & Ambiguity
- **Concept**: Exit a long position when a high-volume exhaustion or reversal candle appears.
- **Literature Review**:
  - Richard Wyckoff (Buying Climax): Climax volume descriptions are qualitative, contextual, and dependent on broad market background analysis.
  - Anna Coulling (*Volume Price Analysis*, 2013): High volume bars indicate stopping/climax action but require subjective discretionary interpretation.
  - Thomas Bulkowski (*Encyclopedia of Candlestick Charts*, 2008): Reversal candlestick patterns with volume (e.g. Shooting Star, Bearish Engulfing) are catalogued individually, but no canonical universal "high volume reversal" rule exists.
- **Overlap with Existing Strategies**: Mike_Trade already implements:
  - `volume_spike`
  - `climactic_volume_exhaustion`
  - `rvol_exhaustion`
- **Rationale for Non-Implementation**: Selecting arbitrary combinations of relative-volume multipliers, candlestick patterns (e.g. pin bar vs shooting star vs engulfing), lookback windows, and confirmation criteria would violate the governing mandate not to synthesize arbitrary trading rules. Retained as unresolved until an authoritative deterministic definition is established.

---

## Attribution Corrections (Batch A Update)

During Batch B, book attribution in Batch A documentation was reviewed and corrected:
- **Andrew Aziz**, *How to Day Trade for a Living* (2016), Chapter 7: "VWAP Trading" was correctly attributed (previously misattributed to Brian Pezim; Brian Pezim is the author of *How to Swing Trade* (2018)).
- Files updated:
  - [`mike_trade/strategies/buy/vwap_pullback.py`](file:///home/mike/dev/mike/stocks/python/work/mike_trade-gemini-buy-sell-backlog-b/mike_trade/strategies/buy/vwap_pullback.py)
  - [`docs/BUY_SELL_BACKLOG_BATCH_A.md`](file:///home/mike/dev/mike/stocks/python/work/mike_trade-gemini-buy-sell-backlog-b/docs/BUY_SELL_BACKLOG_BATCH_A.md)
  - [`docs/TECHNICAL_STRATEGY_INVENTORY.md`](file:///home/mike/dev/mike/stocks/python/work/mike_trade-gemini-buy-sell-backlog-b/docs/TECHNICAL_STRATEGY_INVENTORY.md)
- Conceptual Foundation vs Deterministic Engineering Choices: The source literature establishes the general concept of buying a pullback that tests and holds VWAP support in an uptrend; parameterizing prior trend duration (`trend_bars`) and touch proximity (`tolerance_pct`) are deterministic engineering choices.
