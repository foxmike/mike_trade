# BUY/SELL Backlog Batch D — Investigation and Implementation

> **Review note:** this batch record was written before the independent review. Counts here are branch-local (pre-integration); the reviewed behaviour, corrections and final 182-strategy counts are in `docs/BUY_SELL_BACKLOG_CD_REVIEW.md`, which takes precedence.

* **Branch:** `feature/gemini-buy-sell-backlog-d`
* **Base commit:** `0f2a54e` (`origin/feature/buy-sell-backlog-reviewed`)
* **Worktree:** `/home/mike/dev/mike/stocks/python/work/mike_trade-gemini-buy-sell-backlog-d`
* **Scope:** 4 candidates from `docs/TECHNICAL_STRATEGY_INVENTORY.md`
  1. `relative_volume_momentum` (BUY) — **IMPLEMENTED**
  2. `moving_average_compression` (BUY) — **IMPLEMENTED**
  3. `moving_average_mean_reversion` (BUY) — **EXISTING_EQUIVALENT**
  4. `high_volume_reversal` (SELL) — **IMPLEMENTED**
* **Resulting Strategy Counts:**
  * BUY: **82** (was 80)
  * SELL: **41** (was 40)
  * SHORT: **3** (unchanged)
  * COVER: **3** (unchanged)
  * Total Registered: **129** (was 126)

---

## 1. Candidate Methodology and Source Classification

Sources and definitions follow the classification established in `docs/BUY_SELL_BACKLOG_REVIEW.md`:
* **Type A:** Published indicator definition.
* **Type B:** Published trading methodology with mechanical entry/exit specifications.
* **Type C:** Engineering choice made to obtain a deterministic signal from qualitative or vendor-specific descriptions.

---

### Candidate 1: Relative Volume Momentum (`relative_volume_momentum`)

* **Role:** `StrategyRole.BUY`
* **Status:** `IMPLEMENTED`
* **Implementation File:** `mike_trade/strategies/buy/relative_volume_momentum.py`
* **Classification:** **Type C** built on Type A volume SMA indicators.
* **Literature / Concepts:** Day-trading relative volume (RVOL) scanners (e.g., Warrior Trading, Trade Ideas) identify stocks trading at multiples of their baseline volume while expanding upward. Published vendor scans vary in intraday baselines and momentum thresholds.

#### Distinctness Analysis
* `momentum_breakout`: Requires a price breakout above an $n$-bar high accompanied by volume $\ge k \times \text{SMA}(V, n)$.
* `high_of_day_momentum`: Requires breaking the absolute high of the current regular session.
* `rate_of_change_momentum`: Pure price momentum based on ROC zero-line crossover without volume conditioning.
* `relative_volume_momentum`: Focuses specifically on a sharp volume expansion edge trigger ($RVOL \ge 2.0$ crossing from below) combined with short-term price momentum and positive bar progression, without requiring an $n$-bar price high breakout.

#### Mathematical Rule
1. **Prior Volume Baseline:**
   $$\mu_{vol, t-1} = \frac{1}{N_{vol}} \sum_{i=1}^{N_{vol}} volume_{t-i} \quad \text{where } N_{vol} = 20$$
   Requires all volume values in the window to be strictly positive ($> 0$).
2. **Relative Volume (RVOL):**
   $$RVOL_t = \frac{volume_t}{\mu_{vol, t-1}}, \quad RVOL_{t-1} = \frac{volume_{t-1}}{\mu_{vol, t-2}}$$
3. **Edge Crossing:**
   $$RVOL_{t-1} < 2.0 \quad \text{and} \quad RVOL_t \ge 2.0$$
4. **Bullish Price & Momentum Confirmation:**
   $$close_t > open_t \quad \text{and} \quad close_t > close_{t-1}$$
   $$\text{ROC}_t = 100 \times \frac{close_t - close_{t-N_{mom}}}{close_{t-N_{mom}}} \ge 0.1\% \quad \text{where } N_{mom} = 3$$
5. **Warmup & Point-in-Time Integrity:**
   * Required bars: $\max(N_{vol}, N_{mom}) + 2 = 22$ bars.
   * Point-in-time calculation strictly avoids look-ahead: $\mu_{vol, t-1}$ uses completed bars prior to $t$.

---

### Candidate 2: Moving Average Compression (`moving_average_compression`)

* **Role:** `StrategyRole.BUY`
* **Status:** `IMPLEMENTED`
* **Implementation File:** `mike_trade/strategies/buy/moving_average_compression.py`
* **Classification:** **Type C** built on Type A SMA indicators.
* **Literature / Concepts:** Moving average ribbon convergence before expansion (John J. Murphy, *Technical Analysis of the Financial Markets*, Daryl Guppy). MAs converge into a tight bandwidth, indicating volatility contraction, followed by price breaking out above all moving averages.

#### Distinctness Analysis
* `moving_average_ribbon`: Daryl Guppy GMMA crossover (short group vs. long group crossing).
* `triple_moving_average`: Fast SMA crossing above both medium and slow SMAs.
* `volatility_squeeze_breakout` / `bollinger_squeeze`: Bandwidth compression defined by Bollinger Bands inside Keltner Channels or BandWidth historical percentiles.
* `moving_average_compression`: Specifically quantifies the convergence bandwidth of multiple moving averages across a sustained consolidation window ($5$ bars $\le 0.75\%$), triggering on the first bar where price breaks above the compressed cluster.

#### Mathematical Rule
1. **Moving Average Cluster:**
   * 3 SMAs: Fast ($P_1 = 10$), Medium ($P_2 = 20$), Slow ($P_3 = 50$).
2. **Compression Measurement:**
   For any bar $i$:
   $$BW_i = \frac{\max(SMA_1(i), SMA_2(i), SMA_3(i)) - \min(SMA_1(i), SMA_2(i), SMA_3(i))}{SMA_3(i)} \times 100$$
   Compression requires:
   $$BW_i \le 0.75\% \quad \text{for all } i \in [t - 5, t - 1]$$
3. **Bullish Breakout Confirmation:**
   $$close_{t-1} \le \max(SMA(t-1)) \quad \text{and} \quad close_t > \max(SMA(t)) \quad \text{and} \quad close_t > open_t$$
4. **Warmup & Parameters:**
   * Required bars: $P_{slow} + N_{comp} = 50 + 5 = 55$ bars.
   * No future bars utilized; compression strictly measured over preceding bars $[t-5..t-1]$.

---

### Candidate 3: Moving Average Mean Reversion (`moving_average_mean_reversion`)

* **Role:** `StrategyRole.BUY`
* **Status:** `EXISTING_EQUIVALENT`
* **Canonical Mapping:** `bollinger_band_mean_reversion` / `z_score_mean_reversion` / `keltner_channel_reversion` / `moving_average_envelope` / `moving_average_reclaim`
* **Analysis & Findings:**
  1. In financial econometrics and technical analysis literature, "Moving Average Mean Reversion" is an umbrella conceptual framework rather than a singular standalone indicator formula.
  2. To create an operational trading signal from price displacement away from an MA, that displacement must be measured and normalized:
     * **Standard Deviation Normalization:** Ernest Chan (*Algorithmic Trading*), Bollinger (*Bollinger on Bollinger Bands*). Implemented as `z_score_mean_reversion` (z-score $< -2.0$) and `bollinger_band_mean_reversion` (close below lower band reclaimed).
     * **Volatility (ATR) Normalization:** Chester Keltner / Linda Raschke. Implemented as `keltner_channel_reversion` (close below lower ATR channel reclaimed).
     * **Percentage Envelope Normalization:** Implemented as `moving_average_envelope` (fixed percentage band).
     * **Centerline Reclaim:** Implemented as `moving_average_reclaim` (crossing back above the SMA).
  3. Implementing another class with arbitrary fixed percentages would invent ad-hoc criteria and duplicate existing registered implementations. Therefore, Candidate 3 is reconciled as `EXISTING_EQUIVALENT`.

---

### Candidate 4: High-Volume Reversal (`high_volume_reversal`)

* **Role:** `StrategyRole.SELL`
* **Status:** `IMPLEMENTED`
* **Implementation File:** `mike_trade/strategies/sell/high_volume_reversal.py`
* **Classification:** **Type C** built on Type A volume SMA and price action rules.
* **Literature / Concepts:** Richard Wyckoff volume climax/churn; John J. Murphy (*Technical Analysis of the Financial Markets*, Ch. 10). After an established advance, abnormal volume with poor price progress (bearish bar closing in the lower half of its range) indicates distribution and exhaustion.

#### Distinctness Analysis
* `momentum_reversal`: Price-only ROC reversal without volume conditions.
* `shooting_star_reversal` / `bearish_engulfing_exit`: Strict Japanese candlestick body/shadow formations without relative volume surge requirements.
* `failed_breakout_exit`: Donchian channel new high followed by immediate breakdown.
* `high_volume_reversal`: Specifically combines a prior 5-bar uptrend, volume $\ge 2.0\times$ prior 20-bar volume average, bearish candle ($close < open$ and $close < close_{t-1}$), and lower-half close location ($(close - low) \le 0.5 \times (high - low)$).

#### Mathematical Rule
1. **Prior Uptrend:**
   $$close_{t-1} > close_{t-1-N_{trend}} \quad \text{where } N_{trend} = 5$$
2. **Volume Surge:**
   $$\mu_{vol, t-1} = \frac{1}{N_{vol}} \sum_{i=1}^{N_{vol}} volume_{t-i} \quad \text{where } N_{vol} = 20$$
   $$volume_t \ge 2.0 \times \mu_{vol, t-1}$$
3. **Bearish Reversal Candle:**
   $$close_t < open_t \quad \text{and} \quad close_t < close_{t-1}$$
   $$\frac{close_t - low_t}{high_t - low_t} \le 0.5 \quad (\text{close in the lower } 50\% \text{ of the bar's range})$$
4. **Edge Trigger:**
   The prior bar $t-1$ was not already an active high-volume reversal, preventing consecutive multi-bar exit signals.
5. **Warmup & Parameters:**
   * Required bars: $\max(N_{vol}, N_{trend}) + 2 = 22$ bars.
   * Inherits cleanly from `SellRule`.

---

## 2. Integration and Registry Verification

Both registries were updated with the 3 newly implemented strategy classes:
* `mike_trade/app.py`: `build_runtime`
* `mike_trade/research/commands.py`: `_research_registry`

### Verified Strategy Counts
| Role | Previous Count | Batch D Additions | New Total |
|---|---:|---:|---:|
| **BUY** | 80 | +2 (`relative_volume_momentum`, `moving_average_compression`) | **82** |
| **SELL** | 40 | +1 (`high_volume_reversal`) | **41** |
| **SHORT** | 3 | 0 | **3** |
| **COVER** | 3 | 0 | **3** |
| **TOTAL** | **126** | **+3** | **129** |

---

## 3. Automated Test Coverage

Comprehensive unit tests were added in `tests/unit/test_buy_sell_backlog_batch_d.py` (22 tests):
1. **`relative_volume_momentum`:**
   * Valid signal detection under qualifying RVOL and momentum.
   * Ineligibility gating (`eligibility != "WATCHING_BUY"` returns `NOT_ELIGIBLE`).
   * Edge trigger verification (no signal if prior bar already exceeded threshold).
   * Warmup sufficiency (insufficient data handling).
   * Zero or negative volume rejection (fail-closed).
   * Bearish / flat bar rejection.
   * Insufficient price momentum rejection ($< 0.1\%$).
2. **`moving_average_compression`:**
   * Valid compression and breakout detection.
   * Ineligibility gating.
   * Squeeze broken during the 5-bar window fails closed.
   * Inside-range bar without breakout rejected.
   * Bearish breakout candle ($close \le open$) rejected.
   * Warmup boundary verification.
3. **`high_volume_reversal`:**
   * Valid reversal detection after uptrend with volume surge and bearish lower-half close.
   * Uptrend absence rejection ($close_{t-1} \le close_{t-6}$).
   * Insufficient volume rejection ($< 2.0\times$).
   * Bullish candle rejection ($close \ge open$).
   * Upper-half close rejection ($(close - low) > 0.5 \times (high - low)$).
   * Edge trigger verification (no duplicate trigger if $t-1$ was already a reversal).
   * Warmup boundary verification.
4. **Registry Integrity:**
   * All 3 strategies registered in `app.build_runtime` with matching IDs and roles.
   * All 3 strategies registered in `commands._research_registry`.
   * Verified total count: 82 BUY, 41 SELL, 3 SHORT, 3 COVER = 129.

All 22 unit tests pass:
```bash
pytest tests/unit/test_buy_sell_backlog_batch_d.py
============================== 22 passed in 0.21s ==============================
```

---

## 4. Operational Boundaries and Constraints
* Work conducted strictly within exclusive worktree `/home/mike/dev/mike/stocks/python/work/mike_trade-gemini-buy-sell-backlog-d`.
* No changes to `main`, SHORT, or COVER branches.
* No orders placed; no deployment to R730; no SHADOW restarts.
