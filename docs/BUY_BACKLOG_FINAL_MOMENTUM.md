# BUY Strategy Backlog — Final Momentum Strategies Report

**Agent:** Gemini 3.8 Flash  
**Worktree:** `/home/mike/dev/mike/stocks/python/work/mike_trade-gemini-final-momentum`  
**Branch:** `feature/gemini-buy-backlog-final-momentum`  
**Base Commit:** `569da60` (`origin/feature/strategy-library-176-integrated`)  

---

## 1. Executive Summary & Determinations

This technical investigation evaluated the four remaining BUY strategy candidates from the original technical strategy inventory:

1. **Momentum Ignition**
2. **Momentum Continuation**
3. **RSI Momentum**
4. **Volatility Expansion Continuation**

Each candidate underwent rigorous methodology analysis against published technical literature, quantitative definability, look-ahead safety, overlap with the existing 176-strategy library, and execution feasibility.

| Candidate | Proposed ID | Determination | Final Strategy ID / Mapping | Notes |
|---|---|---|---|---|
| **Momentum Ignition** | `momentum_ignition` | `REQUIRES_SOURCE_DEFINITION` | — (Not Registered) | In microstructure literature (SEC/CFTC studies), momentum ignition denotes predatory spoofing/ramping behavior intended to bait algorithmic stops; lacks canonical mechanical OHLCV entry rules. Legitimate momentum thrusts are handled by `momentum_breakout` and `atr_expansion_breakout`. |
| **Momentum Continuation** | `momentum_continuation` | `IMPLEMENTED` | `momentum_continuation` | Implemented production-quality Jeff Cooper / Martin Pring momentum continuation pattern: 3-bar rising impulse, 2–5 bar tight consolidation holding ≤ 50% retrace above origin, edge trigger on bullish close breaking consolidation high. |
| **RSI Momentum** | `rsi_momentum` | `IMPLEMENTED` | `rsi_momentum` | Implemented Andrew Cardwell / Constance Brown RSI positive reversal: price forms a higher confirmed fractal swing low while Wilder RSI forms a lower low within the bull support zone (30–55), zero look-ahead. |
| **Volatility Expansion Continuation** | `volatility_expansion_continuation` | `EXISTING_EQUIVALENT` | `atr_expansion_breakout` | Volatility expansion continuation after a price thrust is canonically formalized in technical literature (Wilder, Kaufman) as ATR expansion breakout (`close[t] - close[t-1] > multiplier * ATR[t-1]`); cleanly maps to existing registered `AtrExpansionBreakoutBuy`. |

---

## 2. Detailed Candidate Evaluations

### Candidate 1: Momentum Ignition
- **Role:** BUY
- **Determination:** `REQUIRES_SOURCE_DEFINITION`
- **Methodology & Literature Analysis:**
  In financial economics and market microstructure literature (e.g., SEC and CFTC reports, academic high-frequency trading research), "momentum ignition" refers to a predatory order-ramping strategy where an aggressive participant submits rapid series of spoofed orders or aggressive trades to ignite artificial price movement, enticing momentum algorithms and triggering stop orders before reversing their position.
  Retail charting references colloquially describe momentum ignition as sudden spikes in volume and volatility. However, there exists no canonical, published, mechanically deterministic OHLCV rule defining an entry that is not already covered by legitimate breakout indicators. Attempting to invent an ad-hoc volume-multiplier or range-expansion rule under this name would create an unjustified duplicate of `momentum_breakout`, `atr_expansion_breakout`, or `relative_volume_momentum`.
- **Conclusion:** Preserved with `REQUIRES_SOURCE_DEFINITION` in the inventory without registering an invented or duplicative strategy.

---

### Candidate 2: Momentum Continuation
- **Role:** BUY
- **Determination:** `IMPLEMENTED`
- **Implementation ID:** `momentum_continuation`
- **Module:** `mike_trade/strategies/buy/momentum_continuation.py`
- **Source & Foundation:**
  - Jeff Cooper, *Hit & Run Trading: The Short-Term Stock Trader's Bible* (1996), continuation and pause setups.
  - Martin J. Pring, *Technical Analysis Explained* (McGraw-Hill), momentum thrust and consolidation continuation patterns.
- **Methodology & Classification:**
  - **Type B (Published Trading Concept):** A directional impulse thrust followed by a low-volatility pause/flag, resolving in a causal edge breakout resuming the primary trend.
  - **Type A (Mathematical Formulas):**
    - Impulse window: $N$ adjacent bars with strictly rising closes: $C_t > C_{t-1} > \dots > C_{t-N+1}$.
    - Impulse range: $\text{Range} = \max(H) - \min(L)$, requiring advance percentage $\ge \text{min\_impulse\_pct}$ (default 0.20%).
    - Consolidation pause: $K \in [\text{min\_pause\_bars}, \text{max\_pause\_bars}]$ (default 2 to 5 bars).
    - Containment: Pause high does not exceed impulse peak ($\max(H_{\text{pause}}) \le \max(H_{\text{impulse}})$) and pause low holds strictly above impulse origin ($\min(L_{\text{pause}}) > \min(L_{\text{impulse}})$).
    - Retracement bound: Retracement percentage $\frac{\text{peak} - \min(L_{\text{pause}})}{\text{impulse\_range}} \le \text{max\_retrace}$ (default 0.50, or 50% Fibonacci/Dow retracement limit).
    - Edge Breakout: Latest bar $C > \text{pause\_high}$, prior bar $C \le \text{pause\_high}$, and latest bar is bullish ($C > O$).
  - **Type C (Engineering Choices):**
    - Defaults: $N = 3$ impulse bars, $K \in [2, 5]$ pause bars, advance $\ge 0.20\%$, retrace $\le 50\%$.
    - Warmup bars: $N + \text{max\_pause\_bars} + 2 = 10$ completed M1 bars.
    - Replay protection via persistent state key `last_signal_bar`.

---

### Candidate 3: RSI Momentum
- **Role:** BUY
- **Determination:** `IMPLEMENTED`
- **Implementation ID:** `rsi_momentum`
- **Module:** `mike_trade/strategies/buy/rsi_momentum.py`
- **Source & Foundation:**
  - Andrew Cardwell, *Cardwell RSI EDGE*; RSI range rules and positive reversals.
  - Constance Brown, *Technical Analysis for the Trading Professional* (McGraw-Hill, 1999), Chapters 2–3 (RSI range rules and positive reversals).
  - Bill Williams, *Trading Chaos*, fractal pivot construction.
- **Methodology & Classification:**
  - **Type B (Published Trading Concept):** Andrew Cardwell discovered that in an established bullish trend, momentum and price diverge positively through "positive reversals": price forms a higher swing low (support) while the RSI oscillator dips to a lower swing low within the bull support range ($30 \le \text{RSI} \le 55$). This reveals aggressive accumulation where price refuses to decline despite temporary oscillator exhaustion.
  - **Type A (Mathematical Formulas):**
    - J. Welles Wilder 14-period smoothed RSI.
    - Bill Williams confirmed fractal swing lows parameterized by `strength` (default 2 bars on each side). A swing low at index $i$ is confirmed strictly on bar $i + \text{strength}$, ensuring zero look-ahead bias.
    - Two swing lows $(L_1, L_2)$ within a rolling lookback window:
      - Price higher low: $\text{Price}(L_2) > \text{Price}(L_1)$.
      - RSI lower low: $\text{RSI}(L_2) < \text{RSI}(L_1)$.
      - Bull support zone: $\text{min\_rsi\_low} \le \text{RSI}(L_2) \le \text{max\_rsi\_low}$ (defaults 30 and 55).
  - **Type C (Engineering Choices):**
    - Defaults: `rsi_period=14`, `strength=2`, `lookback=50`, `min_rsi_low=30.0`, `max_rsi_low=55.0`.
    - Warmup bars: $\text{rsi\_period} + \text{lookback} + 2 = 66$ completed M1 bars.
    - Replay protection via persistent state key `last_signal_bar`.

---

### Candidate 4: Volatility Expansion Continuation
- **Role:** BUY
- **Determination:** `EXISTING_EQUIVALENT`
- **Mapping:** Mapped to existing registered `atr_expansion_breakout` (`AtrExpansionBreakoutBuy`)
- **Methodology & Analysis:**
  - Volatility expansion continuation is the classic methodology pioneered by J. Welles Wilder (*New Concepts in Technical Trading Systems*, 1978) and formalized by Perry Kaufman (*Smarter Trading*, *Trading Systems and Methods*).
  - The canonical mechanical entry rule is an ATR expansion thrust: a completed price bar whose range or price move exceeds a multiple of the trailing Average True Range:
    $$\text{Close}_t - \text{Close}_{t-1} > \text{multiplier} \times \text{ATR}_{t-1}$$
  - This exact methodology is already fully implemented, tested, and registered in production as `AtrExpansionBreakoutBuy` (`atr_expansion_breakout`). Registering a redundant class would artificially inflate library counts without adding distinct predictive content.
- **Conclusion:** Reconciled in `docs/TECHNICAL_STRATEGY_INVENTORY.md` as `EXISTING_EQUIVALENT`.

---

## 3. Library Registration & Counts Reconciliation

Both new strategies were registered in:
1. **Production:** `mike_trade/app.py` (`build_runtime`)
2. **Research:** `mike_trade/research/commands.py` (`_research_registry`)

### Verification of Registration Counts:
- **BUY:** 82 (Starting: 80 + `momentum_continuation` + `rsi_momentum`)
- **SELL:** 40 (Unchanged)
- **SHORT:** 28 (Unchanged)
- **COVER:** 28 (Unchanged)
- **Total Strategies:** **178**

Production and Research registries are strictly identical with 0 duplicate strategy IDs and 0 symbol collisions.

---

## 4. Verification & Testing

Targeted unit testing in `tests/unit/test_buy_backlog_final_momentum.py` verified:
- Valid trigger conditions and signal emission for both `MomentumContinuationBuy` and `RsiMomentumBuy`.
- Gating and blocked status handling (`NOT_ELIGIBLE`).
- Edge trigger duplicate suppression and replay protection via `last_signal_bar`.
- Resistance to excessive retracement (> 50%) and bearish breakout bars.
- Confirmation of non-lookahead fractal swing pivots.
- Parameter schema validation and out-of-bounds cross-parameter checking.
- Exact strategy role, warmup bar calculations, and library count invariance (178 total).
- Backtest runner execution integration with historical feed and execution simulator.

All 18 targeted unit tests passed with 100% compliance. Code style and formatting adhere strictly to project standards (`ruff check` clean).
