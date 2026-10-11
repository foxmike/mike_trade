# Mike_Trade — Trading Strategy and Risk Management Guide

*Prepared for Michelle* · Reference manual for the automated stock-trading research platform **Mike_Trade** (strategy library 192, branch `feature/strategy-library-192-final`).

This guide describes what the software actually implements, as of the 192-strategy integration. It documents 192 registered strategies — 93 buying, 43 selling, 28 shorting and 28 covering — plus the protective-stop system, the market-wide crash/recovery monitor, the position lifecycle, the four trading modes, and the dashboard.

Nothing in this document is a claim that any strategy is profitable. The software evaluates strategies on correctness, determinism and risk control; profitability has not been established for any of them.

---

## 1. How Mike_Trade Works

Mike_Trade is an automated stock-trading research platform for US equities. It evaluates **technical trading strategies** on completed price bars (principally 1-minute bars during the regular New York session), decides when a trade is permitted, and manages the resulting positions with protective stops. It runs as two processes: a trading engine that owns all market data, strategy evaluation, risk controls and order execution, and a read-only React dashboard that shows account and trading state.

The core idea is a four-role system of independent rules:

| Role | Meaning | Effect |
| :--- | :--- | :--- |
| **BUY** | Open a long position (buy shares) | Entry |
| **SELL** | Close a long position (sell the shares held) | Exit |
| **SHORT** | Open a short position (sell borrowed shares, hoping to buy back cheaper) | Entry |
| **COVER** | Close a short position (buy the shares back) | Exit |

BUY and SELL rules are fully separate; SHORT and COVER rules are fully separate. A SELL signal never opens a short, and a COVER signal never opens a long. There is **no automatic reversal** — the system never flips a position in one order.

Several strategies can be configured for the same symbol, on any side. For example, a symbol may run three BUY strategies, two SELL strategies, two SHORT strategies and one COVER strategy, each with its own parameters. On every closed bar, all configured strategies are evaluated against the same market data.

**Signals are advisory.** A strategy produces a *signal* — "the market did what this rule looks for." It contains no quantity and no order. The execution and risk system then decides whether the trade is actually permitted: whether the position is flat, whether the market state allows new entries, whether budget and buying power suffice, whether protection can be established, and so on. The path is always:

> closed bar → strategy evaluation (advisory signal) → **arbitration** (which signal wins, at most one entry or one exit) → risk and market-state gates → order intent → broker order → confirmed fill → protective stop armed

When several eligible strategies signal on the same bar, **arbitration** picks one. For entries, at most one direction (long *or* short) and one strategy wins per symbol; tie-breaking is deterministic (earliest bar, then long before short, then strategy id). For exits, safety exits outrank strategy exits: protective stop, manual close, hard-risk exit, forced liquidation, then the ordinary SELL/COVER strategy signals. A strategy that would exit is only considered while the matching position is held.

**The protective stop system** is a third, independent mechanism: it is armed after a confirmed entry fill, evaluated before any SELL strategy, and protects a symbol even if it has no SELL strategies configured at all.

**The Market Status Monitor** watches broad-market reference ETFs (SPY, QQQ, IWM) and classifies the whole market as NORMAL, WARNING, CRASH, STABILIZING or RECOVERY. It does not evaluate individual strategies; it gates *new entries* per market state, and it **never** blocks stops, SELL exits, COVER exits or emergency liquidation — under any state.

**Research modes:** strategies can run forward in **SHADOW** (a persistent simulated account on live data), historically in **BACKTEST** (deterministic replay of stored bars), and — with significant limitations documented later — in **PAPER** and **LIVE** broker modes.

The dashboard (read-only) shows cash, equity, buying power, the watchlist, positions, orders, protective-stop state, P&L, market/engine health and a trade journal. The current market-status state is **not** shown in the dashboard UI (see Limitations).

---

## 2. BUY Strategies (93)

---

This section documents all 93 registered BUY strategies, grouped by the platform's own technical sub-categories (moving average, VWAP, momentum, breakout, oscillator, trend following, volatility, price action, volume, gap trading, intraday session, mean reversion). Each entry gives the name, the exact entry signal, the key indicators with defaults, the market behavior the rule is designed to identify, and its timeframe.

Unless a block says otherwise: strategies operate on **completed 1-minute bars during the regular New York session only**, produce **advisory entry signals** (the risk system decides whether a trade is actually permitted), require the engine's `WATCHING_BUY` armed state, and their default warm-up is the lookback shown below (a symbol returns `INSUFFICIENT_DATA` until that many bars have accrued). None of these descriptions is a profitability claim.




#### Moving-average strategies (10 strategies)

### adaptive_moving_average
**Name:** Adaptive Moving Average
**Category:** moving_average
**What it does:** Buys when the close crosses above Kaufman's Adaptive Moving Average. KAMA speeds up in trending markets and flattens in chop, so the cross means price is beating an efficiency-adjusted trend line.
**Entry signal:** The close crosses from at-or-below KAMA to strictly above it. KAMA blends an efficiency ratio over 10 bars with fast (2) and slow (30) smoothing, seeded from a simple average and resumed from persisted state.
**Indicators/defaults:** Efficiency-ratio lookback (er_period, default 10); fast end (fast_period, default 2); slow end (slow_period, default 30, must exceed fast).
**Market behavior identified:** Adaptive trend as price overtakes a noise-adjusted average.
**Timeframe:** Completed 1-minute bars; warmup_bars 11; no daily-history bars.

### ema_crossover
**Name:** EMA Crossover
**Category:** moving_average
**What it does:** Buys when a fast exponential average overtakes a slow one. It uses the crossing of the two smoothed price lines as evidence that the short-term trend has turned up relative to the longer trend.
**Entry signal:** On a completed bar, the fast EMA of closes crosses above the slow EMA (prior fast at or below prior slow, latest fast strictly above latest slow).
**Indicators/defaults:** Fast EMA (fast_period, default 10); slow EMA (slow_period, default 50, must exceed fast).
**Market behavior identified:** Trend, a shorter average turning up through a longer one.
**Timeframe:** Completed 1-minute bars; warmup_bars 51; no daily-history bars.

### ema_pullback
**Name:** EMA Pullback
**Category:** moving_average
**What it does:** Buys the first pullback touch of the fast EMA inside an intact uptrend. The band between the fast and slow EMA is treated as a value zone, and the entry is the first dip into it that holds.
**Entry signal:** With the slow EMA rising and the fast EMA above it, the latest bar's low touches the fast EMA (low at or below it) while its close stays above the slow EMA, and the prior bar's low had not yet reached the fast EMA.
**Indicators/defaults:** Fast EMA, upper value-zone edge (fast_period, default 13); slow EMA, trend and lower edge (slow_period, default 26, must exceed fast).
**Market behavior identified:** Trend pullback, buying the first dip inside a rising trend rather than chasing.
**Timeframe:** Completed 1-minute bars; warmup_bars 27; no daily-history bars.

### hull_moving_average
**Name:** Hull Moving Average
**Category:** moving_average
**What it does:** Buys when the fast-reacting Hull Moving Average ticks up. It uses the HMA's smoothed-but-responsive line and signals at the first upturn after a flat or falling stretch.
**Entry signal:** Across three adjacent completed bars, the HMA was flat or falling into the prior bar (older value at or above prior) and the latest HMA value is strictly above the prior one.
**Indicators/defaults:** Hull Moving Average (period, default 20).
**Market behavior identified:** Trend, catching the early upturn of a responsive trend average.
**Timeframe:** Completed 1-minute bars; warmup_bars 25; no daily-history bars.

### moving_average_compression
**Name:** Moving Average Compression
**Category:** moving_average
**What it does:** Buys the breakout after three moving averages coil tightly together, signaling trend energy building before a directional move. The averages must pinch within a small percentage for several bars.
**Entry signal:** After the 10-, 20- and 50-bar SMAs stay compressed within 0.75% (relative to the slow average) for 5 consecutive bars, the first bar closing above the compressed cluster triggers the buy.
**Indicators/defaults:** Fast SMA 10; medium SMA 20; slow SMA 50; compression_bars 5; compression_threshold_pct 0.75%.
**Market behavior identified:** Pre-expansion coil resolving to the upside.
**Timeframe:** 1m trigger (1m required); warmup_bars 55; no daily history.

### moving_average_envelope
**Name:** Moving Average Envelope
**Category:** moving_average
**What it does:** Buys when the close breaks above an upper envelope wrapped around a moving average. The envelope sits a fixed percentage above the average, so the break means price is stretching hard above its mean.
**Entry signal:** On a completed bar, the prior close was at or below the prior upper envelope (SMA scaled up by the percentage) while the latest close is strictly above the latest upper envelope.
**Indicators/defaults:** Envelope average length (period, default 20); envelope offset (percent, default 2.5%).
**Market behavior identified:** Breakout and momentum as price thrusts above its percentage envelope.
**Timeframe:** Completed 1-minute bars; warmup_bars 21; no daily-history bars.

### moving_average_reclaim
**Name:** Moving Average Reclaim
**Category:** moving_average
**What it does:** Buys when price reclaims its moving average from below. It treats the average as a dividing line between weak and firm price action and signals on the fresh recross above it.
**Entry signal:** On a completed bar, the prior close was at or below the prior SMA while the latest close is strictly above the latest SMA.
**Indicators/defaults:** Simple Moving Average (period, default 20 closes).
**Market behavior identified:** Trend reclaim as price retakes its average after trading underneath it.
**Timeframe:** Completed 1-minute bars; warmup_bars 21; no daily-history bars.

### moving_average_ribbon
**Name:** Moving Average Ribbon
**Category:** moving_average
**What it does:** Buys when Guppy's full short-term ribbon lifts above the long-term ribbon. It requires every one of the six fast EMAs to clear every one of the six slow EMAs, a strict whole-ribbon agreement.
**Entry signal:** The lowest of the short-group EMAs (3/5/8/10/12/15) crosses above the highest of the long-group EMAs (30/35/40/45/50/60), prior bar at-or-below turning to latest strictly above. EMAs are SMA-seeded and resumed from state.
**Indicators/defaults:** Fixed Guppy ribbon lengths; no tunable parameters.
**Market behavior identified:** Broad trend agreement as the entire short ribbon separates above the long ribbon.
**Timeframe:** Completed 1-minute bars; warmup_bars 61; no daily-history bars.

### sma_crossover
**Name:** SMA Crossover
**Category:** moving_average
**What it does:** Buys a golden-cross style turn in simple moving averages. It compares a medium-term average against a long-term average of closes and signals when the shorter one takes the lead.
**Entry signal:** On a completed bar, the fast SMA of closes crosses above the slow SMA (prior fast at or below prior slow, latest fast strictly above latest slow).
**Indicators/defaults:** Fast SMA (fast_period, default 50); slow SMA (slow_period, default 200, must exceed fast).
**Market behavior identified:** Long-term trend turning up, the classic medium-over-long average alignment.
**Timeframe:** Completed 1-minute bars; warmup_bars 201; no daily-history bars.

### triple_moving_average
**Name:** Triple Moving Average
**Category:** moving_average
**What it does:** Buys when three stacked simple averages snap into bullish alignment. It requires the fast average to sit above both the medium and slow ones, with the alignment being newly formed.
**Entry signal:** On a completed bar, the latest fast SMA is above both the medium and slow SMAs while on the prior bar it was at or below at least one of them (a fresh bullish stack).
**Indicators/defaults:** Fast SMA (fast_period, default 5); medium SMA (medium_period, default 10); slow SMA (slow_period, default 20).
**Market behavior identified:** Trend alignment as all three average lengths line up bullish for the first time.
**Timeframe:** Completed 1-minute bars; warmup_bars 21; no daily-history bars.

#### VWAP strategies (5 strategies)

### anchored_vwap_breakout
**Name:** Anchored Vwap Breakout
**Category:** vwap
**What it does:** Buys the first break above volume-weighted average price anchored to the session's first confirmed swing low. The anchor is fixed once and never moves to a later low.
**Entry signal:** After the earliest confirmed session swing low fixes the anchor, a later bar's close breaks above the anchored VWAP computed from that pivot (never on the pivot or confirmation bar itself).
**Indicators/defaults:** Swing strength 2 bars each side confirming the anchor low.
**Market behavior identified:** Breakout above value anchored to the session's capitulation low.
**Timeframe:** 1m trigger (1m required); warmup_bars 6; no daily history.

### vwap_band_reversion
**Name:** Vwap Band Reversion
**Category:** vwap
**What it does:** Buys the recovery back above the lower VWAP deviation band after price stretched abnormally far below the session's volume-weighted average. It is the VWAP analogue of a Bollinger snap-back.
**Entry signal:** The close crosses from below back above the lower session-VWAP band (VWAP minus 2.0 standard deviations), evaluated only after at least 5 session bars.
**Indicators/defaults:** Band multiplier 2.0; min_session_bars 5; session VWAP and band required.
**Market behavior identified:** Intraday mean reversion to session value after downside stretch.
**Timeframe:** 1m trigger (1m required); warmup_bars 5; no daily history.

### vwap_bounce
**Name:** VWAP Bounce
**Category:** vwap
**What it does:** Buys the moment price reclaims the session volume-weighted average price intraday. It treats VWAP as the day's fair-value benchmark and signals only when the close pushes back above it.
**Entry signal:** On a completed 1-minute bar, the prior bar's close was at or below its VWAP while the latest bar's close is strictly above its VWAP (an edge-triggered upward cross; one signal per crossing).
**Indicators/defaults:** Session VWAP computed per bar (resets each session); no tunable parameters.
**Market behavior identified:** Intraday trend reclaim and short-term momentum as price retakes the day's average traded price.
**Timeframe:** Completed 1-minute bars; warmup_bars 2; no daily-history bars; regular session only, requires positive volume.

### vwap_pullback
**Name:** Vwap Pullback
**Category:** vwap
**What it does:** Buys a confirmed touch of session VWAP inside an established intraday uptrend. The stock must first prove strength by holding above VWAP for several bars before the dip is buyable.
**Entry signal:** After at least 3 prior session bars establish an uptrend above VWAP, a setup bar pulls back to within 0.1% of session VWAP and the pullback confirms upward.
**Indicators/defaults:** trend_bars 3; tolerance_pct 0.1% of VWAP; per-bar session VWAP required.
**Market behavior identified:** Trend-continuation pullback to the session's volume-weighted fair value.
**Timeframe:** 1m trigger (1m required); warmup_bars 5; no daily history.

### vwap_rejection
**Name:** Vwap Rejection
**Category:** vwap
**What it does:** Buys a single-bar rejection of VWAP from above: the stock held VWAP, pierced it shallowly intrabar, but bulls forced a bullish close back above it.
**Entry signal:** The prior bar's low held above VWAP while the latest bar dipped below VWAP by no more than 0.5% and closed back above VWAP as a bullish (up-close) candle.
**Indicators/defaults:** max_penetration_pct 0.5%; session VWAP required.
**Market behavior identified:** Intraday VWAP defense / shallow dip bought immediately.
**Timeframe:** 1m trigger (1m required); warmup_bars 3; no daily history.

#### Momentum strategies (10 strategies)

### elder_impulse_system
**Name:** Elder Impulse System
**Category:** momentum
**What it does:** Buys the bar where Elder's Impulse System turns green after a non-green bar. Green demands both trend and momentum agree: a rising 13-bar EMA plus a rising MACD histogram.
**Entry signal:** The prior bar was not green while the latest bar is green, meaning its EMA(13) exceeds the prior bar's and its MACD(12,26,9) histogram exceeds the prior bar's. All EMAs are SMA-seeded and resumed from state.
**Indicators/defaults:** Trend EMA (ema_period, default 13); MACD fast (fast_period, default 12); slow (slow_period, default 26); signal (signal_period, default 9).
**Market behavior identified:** Joint trend-momentum agreement as both components turn up together.
**Timeframe:** Completed 1-minute bars; warmup_bars 36; no daily-history bars.

### high_of_day_momentum
**Name:** High Of Day Momentum
**Category:** momentum
**What it does:** Buys when price takes out everything seen so far today on a closing basis. It compares each bar's close against all earlier highs of the same session inside a daytime entry window.
**Entry signal:** A bar inside the entry window whose close exceeds the highest high of all prior bars of the current regular session.
**Indicators/defaults:** No price indicator; entry_start 10:00 ET, entry_end 15:30 ET (signal bar must start inside this window).
**Market behavior identified:** Intraday momentum / session-high breakout.
**Timeframe:** 1m trigger (1m required); warmup_bars 31; no daily history.

### macd_signal_crossover
**Name:** MACD Signal Crossover
**Category:** momentum
**What it does:** Buys the classic bullish MACD signal-line cross. It tracks the gap between two exponential averages (the MACD line) against its own smoothed signal line and fires when the faster line takes the lead.
**Entry signal:** On a completed bar, the MACD line crosses above its signal line (prior MACD at or below prior signal, latest MACD strictly above latest signal).
**Indicators/defaults:** MACD fast EMA (fast_period, default 12); slow EMA (slow_period, default 26); signal-line EMA (signal_period, default 9).
**Market behavior identified:** Momentum turning up as the MACD line overtakes its signal.
**Timeframe:** Completed 1-minute bars; warmup_bars 35; no daily-history bars.

### macd_zero_line_crossover
**Name:** MACD Zero Line Crossover
**Category:** momentum
**What it does:** Buys when MACD itself turns positive. It reads the MACD line (fast EMA minus slow EMA) crossing above zero as the point where the shorter average has pulled ahead of the longer one.
**Entry signal:** On a completed bar, the MACD line crosses above zero (prior value at or below zero, latest strictly above).
**Indicators/defaults:** MACD fast EMA (fast_period, default 12); slow EMA (slow_period, default 26).
**Market behavior identified:** Momentum shift as the fast average overtakes the slow average in absolute terms.
**Timeframe:** Completed 1-minute bars; warmup_bars 27; no daily-history bars.

### momentum_breakout
**Name:** Momentum Breakout
**Category:** momentum
**What it does:** Buys a fresh N-bar high breakout confirmed by a volume surge. It looks for price pushing into new short-term highs with strong participation behind the move.
**Entry signal:** The latest completed 1-minute close crosses strictly above the highest high of the prior 20 completed bars, and the breakout bar's volume is at least 1.5 times the average volume of those lookback bars.
**Indicators/defaults:** Highest-high lookback (lookback_bars, default 20); volume confirmation multiple (volume_multiple, default 1.5).
**Market behavior identified:** Breakout and momentum as price escapes a recent range on expanding volume.
**Timeframe:** Completed 1-minute bars; warmup_bars 22; no daily-history bars; regular session only, requires volume.

### momentum_continuation
**Name:** Momentum Continuation
**Category:** momentum
**What it does:** Buys breakout resumption after a quick momentum burst pauses in a shallow, tight consolidation. The pause must hold above where the impulse started and must not retrace too deeply.
**Entry signal:** After an impulse of 3 rising closes gaining at least 0.2% and a 2–5 bar pause retracing at most half the impulse height, the latest bar breaks above the pause high with a bullish close (first-break edge only).
**Indicators/defaults:** impulse_bars 3; min_impulse_pct 0.2%; pause 2–5 bars; max_retrace 0.5.
**Market behavior identified:** Short-burst momentum resuming after an orderly pause.
**Timeframe:** 1m trigger (1m required); warmup_bars 10; no daily history.

### rate_of_change_momentum
**Name:** Rate of Change Momentum
**Category:** momentum
**What it does:** Buys when the rate of price change turns positive. It compares the latest close against the close a fixed number of bars back and signals as that percentage change flips above zero.
**Entry signal:** On a completed bar, the rate of change crosses above zero (prior ROC at or below zero, latest strictly above).
**Indicators/defaults:** Rate of Change lookback (period, default 12 bars).
**Market behavior identified:** Momentum as trailing price change turns from negative to positive.
**Timeframe:** Completed 1-minute bars; warmup_bars 14; no daily-history bars.

### relative_volume_momentum
**Name:** Relative Volume Momentum
**Category:** momentum
**What it does:** Buys the bar where unusual participation meets confirmed price progress: volume surges to a multiple of its baseline while price shows a real gain over the last few bars.
**Entry signal:** The latest bar's volume reaches at least 2.0× its 20-bar average (edge-triggered, with the prior bar not already surging) together with a gain of at least 0.1% measured over 3 bars.
**Indicators/defaults:** volume_period 20; rvol_threshold 2.0; momentum_bars 3; min_return_pct 0.1%.
**Market behavior identified:** Participation-backed momentum burst.
**Timeframe:** 1m trigger (1m required); warmup_bars 22; no daily history.

### rsi_momentum
**Name:** RSI Momentum
**Category:** momentum
**What it does:** Buys Cardwell's bullish positive reversal: price holds a higher swing low while the RSI at those same two lows dips lower inside the normal bull-support zone, signaling underlying accumulation.
**Entry signal:** Once two confirmed fractal swing lows show price higher at the second low while RSI(14) is lower there, with the second RSI low inside the 30–55 bull zone, the completed pattern triggers the buy.
**Indicators/defaults:** Wilder RSI 14; swing strength 2; lookback 50; RSI-low zone 30–55.
**Market behavior identified:** Hidden-bullish (positive-reversal) momentum continuation in an uptrend.
**Timeframe:** 1m trigger (1m required); warmup_bars 66; no daily history.

### tsi_momentum
**Name:** TSI Momentum
**Category:** momentum
**What it does:** Buys the moment double-smoothed momentum flips from negative to positive. The True Strength Index applies two rounds of exponential smoothing to price changes so choppy wiggles are damped.
**Entry signal:** TSI(25,13) at or below zero on the prior bar crossing to above zero on the latest bar (zero-line cross, not a signal-line cross).
**Indicators/defaults:** TSI long_period 25, short_period 13; SMA-seeded EMAs.
**Market behavior identified:** Momentum regime flip from negative to positive.
**Timeframe:** 1m trigger (1m required); warmup_bars 39; no daily history.

#### Breakout strategies (10 strategies)

### ascending_triangle_breakout
**Name:** Ascending Triangle Breakout
**Category:** breakout
**What it does:** Buys when flat overhead resistance finally gives way while buyers keep paying higher lows. Three touches of the same ceiling plus rising troughs form the triangle.
**Entry signal:** The last three swing highs sit within 0.2% of each other (resistance = highest) with at least two strictly rising swing lows since the first touch; the first close above that resistance triggers the buy.
**Indicators/defaults:** Fractal strength 2; lookback 100; top flatness tolerance 0.2%.
**Market behavior identified:** Accumulation breakout under flat supply with rising demand.
**Timeframe:** 1m trigger (1m required); warmup_bars 101; no daily history.

### bollinger_band_breakout
**Name:** Bollinger Band Breakout
**Category:** breakout
**What it does:** Buys when the close pushes outside the upper Bollinger Band. It uses the band (a moving average plus a multiple of standard deviation) as a volatility-adjusted ceiling and signals on a close beyond it.
**Entry signal:** On a completed bar, the prior close was at or below the prior upper band while the latest close is strictly above the latest upper band, computed over a finite trailing window.
**Indicators/defaults:** Band length (period, default 20); band width in standard deviations (deviations, default 2).
**Market behavior identified:** Breakout and volatility expansion as price stretches beyond its recent statistical range.
**Timeframe:** Completed 1-minute bars; warmup_bars 21; no daily-history bars.

### donchian_channel_breakout
**Name:** Donchian Channel Breakout
**Category:** breakout
**What it does:** Buys when the close breaks above the highest high of the recent window. It treats that high as the channel top and requires the breakout to be fresh, not a continuation.
**Entry signal:** On a completed bar, the latest close crosses above the highest high of the preceding 20 bars while the prior close was at or below its own channel top.
**Indicators/defaults:** Donchian channel (period, default 20 bars).
**Market behavior identified:** Breakout as price pushes into new high ground above the recent range.
**Timeframe:** Completed 1-minute bars; warmup_bars 22; no daily-history bars.

### intraday_consolidation_breakout
**Name:** Intraday Consolidation Breakout
**Category:** breakout
**What it does:** Buys the first upside close out of a tight same-session base. The base must be genuinely compressed in both highs and lows, and steadily drifting (not flat) prices still qualify.
**Entry signal:** After 15 consecutive same-session bars whose total range spans no more than 0.75%, the next bar closes above the base high (edge-triggered so a steady climber does not re-signal every bar).
**Indicators/defaults:** consolidation_bars 15; max_range_pct 0.75%.
**Market behavior identified:** Micro-consolidation breakout / pause-then-go continuation.
**Timeframe:** 1m trigger (1m required); warmup_bars 17; no daily history.

### keltner_channel_breakout
**Name:** Keltner Channel Breakout
**Category:** breakout
**What it does:** Buys when the close breaks above the upper Keltner Channel. It uses the original Keltner construction as a volatility envelope and signals on a close beyond its top edge.
**Entry signal:** On a completed bar, the prior close was at or below the prior upper channel line while the latest close is strictly above the latest upper line.
**Indicators/defaults:** Keltner Channel (period, default 10).
**Market behavior identified:** Breakout as price pushes through the top of its volatility envelope.
**Timeframe:** Completed 1-minute bars; warmup_bars 11; no daily-history bars.

### opening_range_breakout
**Name:** Opening Range Breakout
**Category:** breakout
**What it does:** Buys a breakout above the high of the first minutes of the trading day. It uses the opening range as the day's first balance area and signals when price resolves above it.
**Entry signal:** After the opening window (default first 30 minutes from the 09:30 New York open) is complete, the latest completed 1-minute close crosses strictly above the highest high of the range bars, edge-triggered (prior close at or below the range high).
**Indicators/defaults:** Opening range length (opening_range_minutes, default 30); optional breakout-bar minimum volume (min_breakout_volume, default 0, disabled).
**Market behavior identified:** Breakout as the market picks a direction out of the opening balance.
**Timeframe:** Completed 1-minute bars; warmup_bars 31; no daily-history bars; regular session only.

### opening_range_retest
**Name:** Opening Range Retest
**Category:** breakout
**What it does:** Plays the classic breakout-then-retest of the opening range. It waits for price to break above the early range high, then buys the first successful retest that holds.
**Entry signal:** After a bar closes above the opening-range high, the first later bar whose low touches or dips to that level but closes back above it (with no earlier retest in between) triggers the buy.
**Indicators/defaults:** opening_range_minutes 15 (bars from 09:30 ET defining the range high).
**Market behavior identified:** Opening-range breakout with retest confirmation.
**Timeframe:** 1m trigger (1m required); warmup_bars 17; no daily history.

### previous_day_high_breakout
**Name:** Previous Day High Breakout
**Category:** breakout
**What it does:** Buys the first close above yesterday's high. It uses a completed-bar close as conservative confirmation rather than an intrabar stop, and needs the full prior session observed.
**Entry signal:** The prior bar's close at or below the previous regular session's high with the latest bar's close above it.
**Indicators/defaults:** No parameters; reference level is the prior session high.
**Market behavior identified:** Multi-session breakout above remembered supply.
**Timeframe:** 1m trigger (1m required); warmup_bars 2; no daily history.

### triangle_breakout
**Name:** Triangle Breakout
**Category:** breakout
**What it does:** Buys the upside break of a converging triangle: a falling upper line through two lower swing highs plus strictly rising swing lows squeezing price toward the apex.
**Entry signal:** The last two swing highs fall while the last two swing lows since the first high rise; the first close above the extended upper line, with no earlier close through it, triggers the buy.
**Indicators/defaults:** Fractal strength 2 bars each side; lookback 100 bars.
**Market behavior identified:** Volatility-compression breakout from a symmetrical-style coil.
**Timeframe:** 1m trigger (1m required); warmup_bars 101; no daily history.

### volatility_squeeze_breakout
**Name:** Volatility Squeeze Breakout
**Category:** breakout
**What it does:** Catches the moment a coiled, low-volatility market releases. It watches Bollinger Bands squeeze inside the Keltner Channel, then buys the release bar when momentum points up.
**Entry signal:** On the bar where the squeeze was on at the prior bar and off at the latest bar, with squeeze momentum (linear regression of close minus the mean of mid-range and SMA over the period) above zero.
**Indicators/defaults:** Bollinger/Keltner/momentum period 20; Bollinger 2 population standard deviations; Keltner 1.5× average true range.
**Market behavior identified:** Volatility expansion breakout after contraction.
**Timeframe:** 1m trigger (1m required); warmup_bars 39; no daily history.

#### Oscillator strategies (9 strategies)

### awesome_oscillator
**Name:** Awesome Oscillator
**Category:** oscillator
**What it does:** Buys when the Awesome Oscillator turns positive. It compares a fast and a slow simple average of bar midpoints and treats the zero crossing as momentum flipping up.
**Entry signal:** On a completed bar, the Awesome Oscillator crosses above zero (prior value at or below zero, latest strictly above).
**Indicators/defaults:** Awesome Oscillator over a fixed 35-bar window; no tunable parameters.
**Market behavior identified:** Momentum shift as medium-term midpoint momentum turns positive.
**Timeframe:** Completed 1-minute bars; warmup_bars 35; no daily-history bars.

### cci_zero_line_crossover
**Name:** CCI Zero Line Crossover
**Category:** oscillator
**What it does:** Buys when the Commodity Channel Index pushes up through zero. It reads zero as the line between below-average and above-average price pressure and treats the crossing as momentum turning up.
**Entry signal:** On a completed bar, CCI crosses above zero (prior value at or below zero, latest value strictly above).
**Indicators/defaults:** Commodity Channel Index (period, default 20 bars).
**Market behavior identified:** Momentum shift from weak to strong price pressure.
**Timeframe:** Completed 1-minute bars; warmup_bars 21; no daily-history bars.

### rsi_centerline_crossover
**Name:** RSI Centerline Crossover
**Category:** oscillator
**What it does:** Buys when the Relative Strength Index pushes up through its midpoint. It treats 50 as the line between bearish and bullish momentum and signals as strength crosses into the upper half.
**Entry signal:** On a completed bar, Wilder RSI crosses above 50 (prior RSI at or below 50, latest RSI strictly above).
**Indicators/defaults:** Wilder RSI (rsi_period, default 14 bars).
**Market behavior identified:** Momentum as buying strength moves into the bullish half of its range.
**Timeframe:** Completed 1-minute bars; warmup_bars 16; no daily-history bars.

### rsi_oversold_reversal
**Name:** RSI Oversold Reversal
**Category:** oscillator
**What it does:** Buys the turn back up out of oversold territory. It waits for RSI to wash out to a washed-out reading, then signals once strength starts recovering above that floor.
**Entry signal:** On a completed bar, RSI was at or below the oversold level on the prior bar and closes back above it on the latest bar (an upward cross of the level).
**Indicators/defaults:** Wilder RSI (rsi_period, default 14); oversold level (oversold, default 30).
**Market behavior identified:** Mean reversion and short-term reversal as selling exhaustion gives way to recovery.
**Timeframe:** Completed 1-minute bars; warmup_bars 16; no daily-history bars.

### stochastic_crossover
**Name:** Stochastic Crossover
**Category:** oscillator
**What it does:** Buys when the faster Stochastic line overtakes its slower partner. It tracks where closes sit inside the recent high-low range and signals as short-term positioning turns up.
**Entry signal:** On a completed bar, Full Stochastic %K crosses above %D (prior %K at or below prior %D, latest %K strictly above latest %D).
**Indicators/defaults:** Raw %K lookback (k_period, default 14); %K smoothing (k_smooth, default 3); %D average length (d_period, default 3).
**Market behavior identified:** Momentum as the fast Stochastic line turns up through the slow line.
**Timeframe:** Completed 1-minute bars; warmup_bars 19; no daily-history bars.

### stochastic_oversold_reversal
**Name:** Stochastic Oversold Reversal
**Category:** oscillator
**What it does:** Buys the turn up out of Stochastic oversold territory. It uses the raw %K reading and signals once it climbs back above the oversold floor.
**Entry signal:** On a completed bar, raw %K (unsmoothed, smoothing 1) crosses above 20 (prior %K at or below 20, latest strictly above).
**Indicators/defaults:** Stochastic %K lookback (period, default 14); oversold threshold fixed at 20 in code.
**Market behavior identified:** Mean reversion as oversold Stochastic pressure starts to recover.
**Timeframe:** Completed 1-minute bars; warmup_bars 15; no daily-history bars.

### stochastic_rsi
**Name:** Stochastic RSI
**Category:** oscillator
**What it does:** Buys when the RSI of recent bars snaps back out of deeply oversold territory. StochRSI measures where the current RSI sits inside its own recent range on a 0–1 scale.
**Entry signal:** Raw (unsmoothed) StochRSI crossing from at or below 0.20 to above 0.20 on the latest bar.
**Indicators/defaults:** Wilder RSI period 14; StochRSI look-back 14; oversold level 0.20.
**Market behavior identified:** Oversold bounce / short-term mean reversion within the RSI cycle.
**Timeframe:** 1m trigger (1m required); warmup_bars 29; no daily history.

### ultimate_oscillator
**Name:** Ultimate Oscillator
**Category:** oscillator
**What it does:** Buys a confirmed bullish divergence: price undercuts to a lower swing low while the multi-timeframe Ultimate Oscillator refuses and prints a higher low from below oversold levels.
**Entry signal:** After price makes a lower fractal swing low with UO making a higher low at the same two bars (divergence low UO below 30), the first bar where UO rises above the highest UO between those two lows.
**Indicators/defaults:** Ultimate Oscillator (7,14,28); swing strength 2 bars each side; lookback 50; oversold 30.
**Market behavior identified:** Bullish divergence reversal off an oversold extreme.
**Timeframe:** 1m trigger (1m required); warmup_bars 78; no daily history.

### williams_r_reversion
**Name:** Williams %R Reversion
**Category:** oscillator
**What it does:** Buys the recovery out of Williams %R oversold territory. It watches how close the latest close sits to the recent lows and signals once that pressure starts lifting.
**Entry signal:** On a completed bar, Williams %R was at or below the oversold level on the prior bar and rises back above it on the latest bar.
**Indicators/defaults:** Williams %R lookback (period, default 14); oversold level (oversold, default -80).
**Market behavior identified:** Mean reversion as deeply oversold pressure starts to ease.
**Timeframe:** Completed 1-minute bars; warmup_bars 15; no daily-history bars.

#### Trend-following strategies (8 strategies)

### adx_trend_following
**Name:** ADX Trend Following
**Category:** trend_following
**What it does:** Buys when trend strength becomes strong while direction is already up. Unlike a plain +DI/-DI cross, it demands that Wilder's ADX rises through a strength threshold with +DI above -DI.
**Entry signal:** ADX crosses above the threshold (default 25) from at-or-above handling while +DI is above -DI. ADX and DIs are Wilder 14-period values resumed from persisted state.
**Indicators/defaults:** Wilder smoothing length (period, default 14); strong-trend level (threshold, default 25).
**Market behavior identified:** Confirmed strong uptrend, strength plus bullish direction together.
**Timeframe:** Completed 1-minute bars; warmup_bars 29; no daily-history bars.

### aroon_trend
**Name:** Aroon Trend
**Category:** trend_following
**What it does:** Buys when the Aroon indicator shows a new uptrend emerging. It compares how recently the highest high versus the lowest low occurred and signals when the balance flips bullish.
**Entry signal:** On a completed bar, Aroon-Up crosses above Aroon-Down (prior Aroon-Up at or below Aroon-Down, latest Aroon-Up strictly above).
**Indicators/defaults:** Aroon (period, default 25 bars).
**Market behavior identified:** Trend, specifically the early stage of an emerging uptrend.
**Timeframe:** Completed 1-minute bars; warmup_bars 27; no daily-history bars.

### dmi_crossover
**Name:** DMI Crossover
**Category:** trend_following
**What it does:** Buys when directional movement flips in favor of buyers. It compares smoothed buying pressure (+DI) against selling pressure (-DI) and signals when buyers take the lead.
**Entry signal:** On a completed bar, +DI crosses above -DI (prior +DI at or below prior -DI, latest +DI strictly above latest -DI).
**Indicators/defaults:** Directional Movement Index with Wilder smoothing (period, default 14 bars).
**Market behavior identified:** Trend, the point where upward directional movement overtakes downward movement.
**Timeframe:** Completed 1-minute bars; warmup_bars 16; no daily-history bars.

### holy_grail_pullback
**Name:** Holy Grail Pullback
**Category:** trend_following
**What it does:** Buys a shallow pullback inside an already-strong uptrend, Linda Raschke style. The trend must be powerful (high rising ADX with bulls dominant), then price kisses the moving average before resuming.
**Entry signal:** Setup bar touched the 20-bar EMA from above with ADX(14) above 30 and rising and +DI above −DI; the next bar closes above the setup bar's high.
**Indicators/defaults:** ADX/DI period 14, ADX threshold 30; pullback EMA 20.
**Market behavior identified:** Pullback continuation inside a strong established trend.
**Timeframe:** 1m trigger (1m required); warmup_bars 30; no daily history.

### ichimoku_cloud_breakout
**Name:** Ichimoku Cloud Breakout
**Category:** trend_following
**What it does:** Buys when the close breaks above the top of the Ichimoku cloud. The cloud's forward-plotted spans act as layered resistance, so clearing the top means price has overcome the whole projected zone.
**Entry signal:** The close crosses above the cloud top, defined as the higher of Span A and Span B as plotted at each bar (prior close at or below the prior top, latest close strictly above the latest top). Uses canonical 9/26/52 settings with 26-bar displacement, point-in-time only.
**Indicators/defaults:** Fixed canonical settings (9, 26, 52, displacement 26); no tunable parameters.
**Market behavior identified:** Trend breakout above projected Ichimoku resistance.
**Timeframe:** Completed 1-minute bars; warmup_bars 79; no daily-history bars.

### linear_regression_trend
**Name:** Linear Regression Trend
**Category:** trend_following
**What it does:** Buys when the slope of price's best-fit line turns positive. It fits a straight line through recent closes and treats the slope flipping above zero as a trend turning up.
**Entry signal:** On a completed bar, the linear regression slope of closes crosses above zero (prior slope at or below zero, latest strictly above).
**Indicators/defaults:** Regression window (period, default 20 closes).
**Market behavior identified:** Trend, the moment the statistical drift of price turns upward.
**Timeframe:** Completed 1-minute bars; warmup_bars 21; no daily-history bars.

### parabolic_sar
**Name:** Parabolic SAR
**Category:** trend_following
**What it does:** Buys when the Parabolic SAR trend indicator flips from bearish to bullish. It follows the SAR dots that trail price and signals when price penetrates the dot from below.
**Entry signal:** On a completed bar, the SAR state changes from downtrend on the prior bar to uptrend on the latest bar (price crossing through the SAR level).
**Indicators/defaults:** Parabolic SAR acceleration step (step, default 0.02); acceleration cap (maximum, default 0.20, must be at least step).
**Market behavior identified:** Trend reversal from downtrend to uptrend.
**Timeframe:** Completed 1-minute bars; warmup_bars 3; no daily-history bars.

### supertrend
**Name:** Supertrend
**Category:** trend_following
**What it does:** Buys when the Supertrend indicator flips from down to up. Its ratcheting ATR bands trail price, and the flip happens when the close finishes above the final upper band.
**Entry signal:** Supertrend turns from downtrend to uptrend as the latest close ends above the final upper band, where bands are bar midpoint plus/minus 3x Wilder ATR(10) with the usual only-fall/only-rise ratcheting. ATR and band state resume from persisted state.
**Indicators/defaults:** Wilder ATR length (atr_period, default 10); band offset (multiplier, default 3).
**Market behavior identified:** Trend reversal into an uptrend as price clears the trailing stop band.
**Timeframe:** Completed 1-minute bars; warmup_bars 11; no daily-history bars.

#### Volatility strategies (4 strategies)

### atr_expansion_breakout
**Name:** ATR Expansion Breakout
**Category:** volatility
**What it does:** Buys an abnormally large upward thrust relative to recent volatility. It measures the close-to-close jump against the prior bar's Average True Range so the expansion bar cannot inflate its own hurdle.
**Entry signal:** The latest close minus the prior close exceeds the multiplier (default 2) times the prior bar's Wilder ATR, i.e. close[t] - close[t-1] > multiplier x ATR[t-1].
**Indicators/defaults:** Wilder ATR length (period, default 14); thrust size (multiplier, default 2 ATRs).
**Market behavior identified:** Volatility expansion breakout, an outsized up-move versus recent range.
**Timeframe:** Completed 1-minute bars; warmup_bars 15; no daily-history bars.

### bollinger_squeeze
**Name:** Bollinger Squeeze
**Category:** volatility
**What it does:** Buys an upper-band breakout that follows a volatility squeeze. It first requires BandWidth to have hit its lowest point over a long lookback, then signals on the close crossing above the upper band.
**Entry signal:** The latest close crosses above the upper Bollinger Band (prior close at or below its band) with a squeeze, the lowest BandWidth of the last 125 values, occurring within the 5 bars before the breakout. Bands use population standard deviation.
**Indicators/defaults:** Band length (period, default 20); deviations (default 2); squeeze lookback (lookback, default 125); release window (release_window, default 5).
**Market behavior identified:** Volatility compression resolving into an upside breakout.
**Timeframe:** Completed 1-minute bars; warmup_bars 149; no daily-history bars.

### nr7_breakout
**Name:** NR7 Breakout
**Category:** volatility
**What it does:** Buys the breakout after the narrowest-range bar in a week-long window. It treats that contraction (the NR7 bar) as coiled volatility and signals when price breaks above its high.
**Entry signal:** The second-to-last bar has a smaller high-minus-low range than every other bar in the 7-bar window, and the latest completed bar closes above that narrow bar's high.
**Indicators/defaults:** Range window (period, default 7, range 4-7).
**Market behavior identified:** Volatility contraction followed by breakout as coiled range resolves upward.
**Timeframe:** Completed 1-minute bars; warmup_bars 8; no daily-history bars.

### volatility_contraction_pattern
**Name:** Volatility Contraction Pattern
**Category:** volatility
**What it does:** Buys the intraday breakout of a Minervini-style base: a stage-2 uptrend stock that tightens through successively smaller pullbacks on drying volume, then closes through its pivot.
**Entry signal:** With the daily trend template met, 2–6 contractions each at most 0.85× the prior one (first ≤35% deep, last ≤10%), volume dried up, and the daily close within 5% below the pivot, the first same-session M1 close above the pivot (optionally 1.4× pro-rated volume) triggers the buy.
**Indicators/defaults:** Daily SMAs 50/150/200 with 200-day rising 21 sessions; 252-session 52-week range; contraction_ratio 0.85; breakout_volume_ratio 1.4.
**Market behavior identified:** Multi-week tightening base breaking out within a major uptrend.
**Timeframe:** 1m trigger (1m plus 1d required); warmup_bars 1; daily history 252 bars of 1d.

#### Chart-pattern and price-action strategies (14 strategies)

### bull_flag_breakout
**Name:** Bull Flag Breakout
**Category:** price_action
**What it does:** Buys the resumption after a sharp rally (the pole) digests its gains in a short, shallow flag. The flag must be brief and must not retrace too much of the pole.
**Entry signal:** After a pole of at least 1% within 15 bars and a 3–20 bar flag retracing no more than half the pole's height, the first close above the flag's highest high triggers the buy.
**Indicators/defaults:** Lookback 60; pole_bars 15; pole_pct 1%; flag 3–20 bars; max_retrace 0.5.
**Market behavior identified:** Momentum continuation after a controlled digestion of a spike.
**Timeframe:** 1m trigger (1m required); warmup_bars 61; no daily history.

### bull_pennant_breakout
**Name:** Bull Pennant Breakout
**Category:** price_action
**What it does:** Buys the upside break of a small symmetrical pennant that formed after a sharp flagpole advance. Both pennant edges must converge (falling top, rising bottom) with the breakout arriving before the apex.
**Entry signal:** After a pole of at least 1.5% within 10 bars and a 6–20 bar pennant retracing at most half the pole, the first close above the extended falling upper line (with no earlier close through either line) triggers the buy.
**Indicators/defaults:** Fractal strength 1; lookback 60; pole_bars 10; pole_pct 1.5%; pennant 6–20 bars; max_retrace 0.5.
**Market behavior identified:** Momentum continuation through a converging digestion of a spike.
**Timeframe:** 1m trigger (1m required); warmup_bars 61; no daily history.

### cup_and_handle
**Name:** Cup And Handle
**Category:** price_action
**What it does:** Buys the intraday breakout of an O'Neil-style cup with handle built on daily candles: a rounded 7–26 week bowl with smooth sides, a short handle in the upper half on lighter volume, after a strong prior advance.
**Entry signal:** With the cup (35–130 sessions, 12–35% deep, U-shaped with centered bottom), qualifying handle (5–25 sessions, 2–15% deep, low in the upper half), and a ≥30% prior advance all confirmed, the first same-session M1 close above the pivot (optionally 1.4× pro-rated volume) triggers the buy.
**Indicators/defaults:** Swing strength 3; cup 35–130 sessions; handle 5–25 sessions; handle volume ratio 0.9; breakout_volume_ratio 1.4.
**Market behavior identified:** Long-base accumulation breakout after a rounded correction and final shakeout.
**Timeframe:** 1m trigger (1m plus 1d required); warmup_bars 1; daily history 241 bars of 1d.

46 blocks written.

### double_bottom_reversal
**Name:** Double Bottom Reversal
**Category:** price_action
**What it does:** Buys the confirmation of a W-shaped bottom: two similarly-priced swing lows separated by a visible bounce, resolved when price closes through the middle peak.
**Entry signal:** With the two fractal lows within 0.5% of each other, the interim peak at least 1% above the higher low, and no breakdown since, the first close above that peak triggers the buy.
**Indicators/defaults:** Fractal strength 2 bars each side; lookback 100; low tolerance 0.5%; minimum middle rise 1%.
**Market behavior identified:** Bottom reversal after a failed second test of the lows.
**Timeframe:** 1m trigger (1m required); warmup_bars 101; no daily history.

### engulfing_candle_reversal
**Name:** Engulfing Candle Reversal
**Category:** price_action
**What it does:** Buys a classic two-candle bullish reversal. It looks for a green candle whose real body fully swallows the prior candle's body, showing buyers seizing control.
**Entry signal:** On two adjacent completed bars, the latest bar forms a bullish engulfing pattern over the prior bar (per the engulfing pattern check in code).
**Indicators/defaults:** Pure candlestick price action; no indicators or parameters.
**Market behavior identified:** Reversal as buyers overwhelm the prior bar's selling in a single candle.
**Timeframe:** Completed 1-minute bars; warmup_bars 2; no daily-history bars.

### hammer_reversal
**Name:** Hammer Reversal
**Category:** price_action
**What it does:** Buys bullish confirmation after a hammer candle forms during a decline. The hammer's long lower shadow shows sellers were rejected, and the next bar closing above its high confirms buyers took over.
**Entry signal:** A hammer at the prior bar (lower shadow at least 2x the real body, negligible upper shadow, non-zero range) forms after the close fell over the prior 5 bars, and the latest bar closes above the hammer's high.
**Indicators/defaults:** Pre-hammer decline window (trend_bars, default 5); minimum lower-shadow multiple (shadow_ratio, default 2); maximum upper shadow share of range (max_upper_shadow, default 0.1).
**Market behavior identified:** Reversal as rejection of lows plus confirmation turns a decline around.
**Timeframe:** Completed 1-minute bars; warmup_bars 8; no daily-history bars.

### higher_high_higher_low
**Name:** Higher High Higher Low
**Category:** price_action
**What it does:** Buys the bar that completes a rising Dow-theory structure. It tracks fractal swing highs and lows and signals at the first bar that proves the newest swing low sits above the previous one.
**Entry signal:** Exactly strength bars (default 2) after a fractal swing low forms, the signal bar confirms a higher low while the last two swing highs inside the 50-bar lookback are also rising (strict extremes each side).
**Indicators/defaults:** Swing confirmation strength (strength, default 2 bars each side); pivot search window (lookback, default 50 bars).
**Market behavior identified:** Uptrend structure, the classic higher-high plus higher-low staircase.
**Timeframe:** Completed 1-minute bars; warmup_bars 51; no daily-history bars.

### inside_bar_breakout
**Name:** Inside Bar Breakout
**Category:** price_action
**What it does:** Buys the resolution of a brief pause in price action. It spots a bar that fits entirely inside the prior bar's range, then signals when price breaks out above the bigger bar's high.
**Entry signal:** A completed bar (the inside bar) has a lower high and a higher low than the mother bar before it, and the next completed bar closes above the mother bar's high.
**Indicators/defaults:** Pure price action, no indicators or parameters; needs 3 completed bars (mother, inside, breakout).
**Market behavior identified:** Consolidation breakout after a one-bar contraction in range.
**Timeframe:** Completed 1-minute bars; warmup_bars 3; no daily-history bars.

### intraday_exhaustion_reversal
**Name:** Intraday Exhaustion Reversal
**Category:** price_action
**What it does:** Buys the snap-back after downside exhaustion with absorption volume. It demands a prior decline, a fresh low on an abnormally wide and heavy bar, then a bullish confirmation bar.
**Entry signal:** After a decline over 4 bars and a new 10-bar low on an exhaustion bar with at least 1.5× baseline range and 1.5× baseline volume, the next bullish confirmation bar triggers the buy.
**Indicators/defaults:** trend_bars 4; lookback 10; range_multiple 1.5× ATR baseline; volume_multiple 1.5× volume SMA.
**Market behavior identified:** Intraday capitulation-and-absorption reversal.
**Timeframe:** 1m trigger (1m required); warmup_bars 22; no daily history.

### inverse_head_and_shoulders
**Name:** Inverse Head And Shoulders
**Category:** price_action
**What it does:** Buys the resolution of an inverted head-and-shoulders base: a deeper middle trough flanked by two shallower, similarly-priced shoulders, broken through a sloped neckline.
**Entry signal:** With the head at least 0.3% below the lower shoulder and shoulders within 1% of each other, the first close above the neckline joining the two interim swing highs (with no earlier close through it since the right shoulder).
**Indicators/defaults:** Fractal strength 2; lookback 150; shoulder tolerance 1%; head depth 0.3%.
**Market behavior identified:** Bottom reversal completing a three-trough base.
**Timeframe:** 1m trigger (1m required); warmup_bars 151; no daily history.

### previous_day_low_reversal
**Name:** Previous Day Low Reversal
**Category:** price_action
**What it does:** Buys the sweep-and-reclaim of yesterday's low within a single bar: price pokes under the level but buyers force the same bar to close back above it.
**Entry signal:** The latest completed bar's low at or below the previous session's low with its close back above that level (each fresh qualifying bar can signal).
**Indicators/defaults:** No parameters; reference level is the prior session low.
**Market behavior identified:** Failed breakdown / support reclaim reversal.
**Timeframe:** 1m trigger (1m required); warmup_bars 1; no daily history.

### resistance_breakout
**Name:** Resistance Breakout
**Category:** price_action
**What it does:** Buys when the close clears the most recent confirmed swing high. That fractal pivot acts as structural resistance fixed by already-closed bars, so the cross means a real ceiling gave way.
**Entry signal:** The close crosses above resistance R, the latest fractal swing high (strict extreme, 2 bars each side) found in the 50 completed bars before the signal bar (prior close at or below R, latest strictly above).
**Indicators/defaults:** Swing confirmation strength (strength, default 2 bars each side); pivot search window (lookback, default 50 bars).
**Market behavior identified:** Structural breakout through a confirmed prior pivot rather than a rolling highest high.
**Timeframe:** Completed 1-minute bars; warmup_bars 51; no daily-history bars.

### support_bounce
**Name:** Support Bounce
**Category:** price_action
**What it does:** Buys a confirmed rebound off a previously proven swing-low support. The support pivot must be fully confirmed before the test, and any breakdown since invalidates the level.
**Entry signal:** A setup bar dips into the support zone (within 0.2% of the confirmed swing low) without closing below it, and the next bar confirms the rebound higher.
**Indicators/defaults:** Fractal strength 2; lookback 50; tolerance_pct 0.2%.
**Market behavior identified:** Support-hold reversal / buying at remembered demand.
**Timeframe:** 1m trigger (1m required); warmup_bars 52; no daily history.

### trendline_breakout
**Name:** Trendline Breakout
**Category:** price_action
**What it does:** Buys the first close through a falling downtrend line drawn across the last two lower swing highs. It requires the line to have held unbroken from the first high until now.
**Entry signal:** The last two fractal swing highs are strictly falling; the latest close prints above the extended line through them with no close through that line between the first high and the prior bar.
**Indicators/defaults:** Fractal strength 2 bars each side; lookback 100 bars.
**Market behavior identified:** Downtrend-line break / early trend reversal.
**Timeframe:** 1m trigger (1m required); warmup_bars 101; no daily history.

#### Volume strategies (7 strategies)

### accumulation_distribution_breakout
**Name:** Accumulation Distribution Breakout
**Category:** volume
**What it does:** Buys when the Accumulation/Distribution money-flow line prints a fresh high. Each bar contributes volume weighted by where price closed inside its high–low range.
**Entry signal:** The A/D line at the latest bar exceeds its maximum over the prior 20 values while the previous bar had not (first breakout only, indicator-only with no extra price confirmation).
**Indicators/defaults:** Accumulation/Distribution line, period 20 bars of line history.
**Market behavior identified:** Volume-weighted accumulation breakout.
**Timeframe:** 1m trigger (1m required); warmup_bars 22; no daily history.

### chaikin_money_flow
**Name:** Chaikin Money Flow
**Category:** volume
**What it does:** Buys when Chaikin Money Flow turns positive. It measures where closes fall inside each bar's range weighted by volume, and the zero crossing marks buying pressure overtaking selling pressure.
**Entry signal:** On a completed bar, Chaikin Money Flow crosses above zero (prior value at or below zero, latest strictly above).
**Indicators/defaults:** Chaikin Money Flow (period, default 20); zero-line cross; requires volume.
**Market behavior identified:** Accumulation and buying pressure as volume-weighted flow turns positive.
**Timeframe:** Completed 1-minute bars; warmup_bars 21; no daily-history bars; requires volume.

### ease_of_movement
**Name:** Ease of Movement
**Category:** volume
**What it does:** Buys when Arms' Ease of Movement turns positive on average. It relates each bar's midpoint migration to its volume-per-range cost, so a positive average means price is floating up on light volume.
**Entry signal:** The simple average of one-bar EMV over 14 bars crosses above zero. Zero-range bars contribute 0; a zero-volume bar yields insufficient data; the scale constant never affects the sign.
**Indicators/defaults:** EMV averaging length (period, default 14); requires volume.
**Market behavior identified:** Effortless upward drift, price rising with little volume resistance relative to range.
**Timeframe:** Completed 1-minute bars; warmup_bars 16; no daily-history bars; requires volume.

### money_flow_index
**Name:** Money Flow Index
**Category:** volume
**What it does:** Buys when volume-weighted buying pressure recovers from an oversold reading. It combines price and volume into the Money Flow Index and signals once money flow turns back up.
**Entry signal:** On a completed bar, the Money Flow Index crosses above 20 (prior value at or below 20, latest strictly above).
**Indicators/defaults:** Money Flow Index (period, default 14); oversold threshold fixed at 20 in code; requires volume.
**Market behavior identified:** Reversal with volume confirmation, as money flow recovers from a washed-out reading.
**Timeframe:** Completed 1-minute bars; warmup_bars 16; no daily-history bars; requires volume.

### obv_breakout
**Name:** OBV Breakout
**Category:** volume
**What it does:** Buys when cumulative buying pressure, measured by On-Balance Volume, breaks to a fresh high. It ignores price and reads only participation Novack: up closes add volume, down closes subtract it.
**Entry signal:** The OBV line at the latest bar exceeds its highest value over the prior 20 bars while the previous bar had not exceeded its own prior high (first breakout only).
**Indicators/defaults:** On-Balance Volume, period 20 bars of line history.
**Market behavior identified:** Volume-led breakout / accumulation showing up before price.
**Timeframe:** 1m trigger (1m required); warmup_bars 22; no daily history.

### volume_dry_up_pullback
**Name:** Volume Dry Up Pullback
**Category:** volume
**What it does:** Buys an uptrend pullback where sellers show no conviction (volume dries up), then price resumes upward on expanding volume. The correction must be shallow enough to leave the trend structure intact.
**Entry signal:** After a 5-bar uptrend and a 3-bar pullback whose average volume runs at most 0.70× the 20-bar baseline (with the pullback low holding above the trend-start low), the resumption bar upward on expanding volume triggers the buy.
**Indicators/defaults:** trend_bars 5; pullback_bars 3; volume_period 20; dry_up_factor 0.70.
**Market behavior identified:** Low-conviction pullback resolving back into the uptrend.
**Timeframe:** 1m trigger (1m required); warmup_bars 24; no daily history.

### volume_price_trend_breakout
**Name:** Volume Price Trend Breakout
**Category:** volume
**What it does:** Buys when the Volume Price Trend line, which scales each bar's volume by its percentage price change, breaks above its recent range. Larger moves on heavier volume push the line hardest.
**Entry signal:** The VPT line at the latest bar exceeds its maximum over the prior 20 values while the previous bar had not (first breakout only, indicator-only with no price confirmation).
**Indicators/defaults:** Volume Price Trend line, period 20 bars of line history.
**Market behavior identified:** Volume-scaled momentum breakout.
**Timeframe:** 1m trigger (1m required); warmup_bars 22; no daily history.

#### Gap-trading strategies (4 strategies)

### gap_and_go
**Name:** Gap And Go
**Category:** gap_trading
**What it does:** Buys continuation when a stock gaps up big and then pushes through its opening range. Premarket prices are ignored; only the regular-session gap and opening bars count, and signals stop after 10:00 ET.
**Entry signal:** With a regular-session gap up of at least 4% over the prior close, the prior close at or below the opening-range high and the latest close above it (first breakout, no earlier close through).
**Indicators/defaults:** opening_range_minutes 1; min_gap_percent 4%.
**Market behavior identified:** Gap-continuation momentum in the first half hour.
**Timeframe:** 1m trigger (1m required); warmup_bars 2; no daily history.

### gap_exhaustion
**Name:** Gap Exhaustion
**Category:** gap_trading
**What it does:** Buys the washout bottom after a multi-day slide ends in a gap below the prior low on heavy volume. It needs the decline, the gap, the participation, and the first close back above the gap edge together.
**Entry signal:** After 4 straight lower closes totaling at least a 3% drop, the session opens at least 1% below the prior low on heavy early volume, and the first bar closes back above that prior low.
**Indicators/defaults:** trend_sessions 4; min_decline_pct 3%; min_gap_pct 1%; min_relative_volume 1.5.
**Market behavior identified:** Capitulation / exhaustion-gap reversal after a persistent decline.
**Timeframe:** 1m trigger (1m required); warmup_bars 1; no daily history beyond the session-reference history.

### gap_fill
**Name:** Gap Fill
**Category:** gap_trading
**What it does:** Buys the recovery toward the prior close after a gap down. It treats the previous session's close as the reference and fires when price first climbs back above the session's own open.
**Entry signal:** After a strict gap down of at least the minimum size, the prior close at or below the current session's open with the latest close back above that open.
**Indicators/defaults:** min_gap_percent 0 (any strict gap down qualifies by default).
**Market behavior identified:** Gap-fill mean reversion after overnight weakness.
**Timeframe:** 1m trigger (1m required); warmup_bars 2; no daily history.

### gap_reversal
**Name:** Gap Reversal
**Category:** gap_trading
**What it does:** Buys only the strong version of a gap-down recovery: price must reclaim the opening range and hold above the session's volume-weighted average price, confirming real Spons/Ryan buying.
**Entry signal:** After a gap down, the prior close at or below the 15-minute opening-range high with the latest close above it, plus the latest close above session VWAP.
**Indicators/defaults:** opening_range_minutes 15; min_gap_percent 0; session VWAP required.
**Market behavior identified:** Gap-down reversal confirmed by reclaiming value (VWAP).
**Timeframe:** 1m trigger (1m required); warmup_bars 16; no daily history.

#### Intraday session-timing strategies (6 strategies)

### afternoon_breakout
**Name:** Afternoon Breakout
**Category:** intraday_session
**What it does:** Buys when the afternoon session resolves above the morning's ceiling. The morning range is frozen early, so later breakouts are measured against a fixed reference.
**Entry signal:** The prior close at or below the morning-range high (highest high of the first 60 minutes from the open) and the latest close above it, with the signal bar inside the 13:00–15:00 ET window.
**Indicators/defaults:** morning_range_minutes 60; entry_start 13:00 ET, entry_end 15:00 ET.
**Market behavior identified:** Intraday breakout of the morning range in the afternoon session.
**Timeframe:** 1m trigger (1m required); warmup_bars 211; no daily history.

### first_pullback_buy
**Name:** First Pullback Buy
**Category:** intraday_session
**What it does:** Buys the very first pullback after the session's opening impulse, then stands aside for the rest of the day. Only one impulse-and-pullback sequence is consumed per session, and weak or overextended setups are discarded.
**Entry signal:** After a first impulse of 3 rising bars gaining at least 1% on strong volume, a short pullback (up to 5 bars, retracing at most half the impulse) turns back up, within 90 minutes of the open.
**Indicators/defaults:** impulse_bars 3; impulse_pct 1%; max_pullback_bars 5; max_retrace 0.5; volume_multiple 1.2; entry within 90 minutes of 09:30 ET.
**Market behavior identified:** Opening-drive continuation via its first orderly pullback.
**Timeframe:** 1m trigger (1m required); warmup_bars 5; no daily history.

### midday_range_breakout
**Name:** Midday Range Breakout
**Category:** intraday_session
**What it does:** Trades the resolution of the lunchtime lull. It marks the high of a midday range, then buys when price closes through it during the afternoon entry window.
**Entry signal:** The prior bar's close at or below the midday-range high and the latest bar's close above it (a close crossing), with the signal bar starting between the range end and the entry end.
**Indicators/defaults:** Range 12:00–13:00 ET; entries allowed until 15:00 ET.
**Market behavior identified:** Intraday range breakout after midday consolidation.
**Timeframe:** 1m trigger (1m required); warmup_bars 211; no daily history.

### opening_drive
**Name:** Opening Drive
**Category:** intraday_session
**What it does:** Buys a strong open where buyers dominate from the opening bell. It checks the first few minutes of the regular session for one-sided bullish control that never gives back the opening price.
**Entry signal:** Exactly when the opening window completes (default 5 bars from 09:30 ET): every bar closed above its open, closes rose bar to bar, and no subsequent bar's low broke below the first bar's open.
**Indicators/defaults:** drive_minutes 5 (number of M1 bars from the open).
**Market behavior identified:** Intraday session momentum / opening trend.
**Timeframe:** 1m trigger (1m required); warmup_bars 5; no daily history.

### power_hour_momentum
**Name:** Power Hour Momentum
**Category:** intraday_session
**What it does:** Buys late-day breakouts only when the day itself was already bullish. It requires the session into the final hour to be up, then buys the first close above the whole day's high.
**Entry signal:** The pre-15:00 session closed higher than it opened, and a bar in the final window crosses from at/below the pre-window high to a close above it.
**Indicators/defaults:** power_hour_start 15:00 ET (pre-window runs from the 09:30 ET open to this time).
**Market behavior identified:** Late-session momentum continuation on an already-up day.
**Timeframe:** 1m trigger (1m required); warmup_bars 331; no daily history.

### previous_close_reclaim
**Name:** Previous Close Reclaim
**Category:** intraday_session
**What it does:** Buys when a gapped-down stock works all the way back through yesterday's close on rising volume and holds above VWAP. It is the full round-trip version of a gap recovery.
**Entry signal:** After a gap down of at least 0.5%, between 10:00 and the entry end: the prior close at or below the previous session's close with the latest close above it, the latest close above VWAP, and latest volume at least 1.5× the first-30-minute average.
**Indicators/defaults:** min_gap_percent 0.5%; volume_multiplier 1.5; session VWAP required.
**Market behavior identified:** Full gap recovery / prior-close reclaim with volume confirmation.
**Timeframe:** 1m trigger (1m required); warmup_bars 31; no daily history.

#### Mean-reversion strategies (6 strategies)

### bollinger_band_mean_reversion
**Name:** Bollinger Band Mean Reversion
**Category:** mean_reversion
**What it does:** Buys the snap-back after price stretches below its Bollinger Band. It waits for an actual close outside the band, then buys the return trip back inside it.
**Entry signal:** The prior bar's close below its own lower Bollinger Band with the latest bar's close back above its own lower band.
**Indicators/defaults:** Bollinger SMA period 20; standard-deviation multiplier 2 (population basis).
**Market behavior identified:** Short-term mean reversion after a downside stretch.
**Timeframe:** 1m trigger (1m required); warmup_bars 21; no daily history.

### cci_reversion
**Name:** CCI Reversion
**Category:** mean_reversion
**What it does:** Buys the turn back up out of deeply negative CCI territory. It waits for the Commodity Channel Index to wash out to an extreme low, then signals once it starts recovering above that floor.
**Entry signal:** On a completed bar, CCI crosses above the -100 oversold level (prior value at or below -100, latest value strictly above).
**Indicators/defaults:** Commodity Channel Index (period, default 20); oversold threshold fixed at -100 in code.
**Market behavior identified:** Mean reversion as an extreme negative deviation starts to snap back.
**Timeframe:** Completed 1-minute bars; warmup_bars 21; no daily-history bars.

### connors_rsi_mean_reversion
**Name:** Connors RSI Mean Reversion
**Category:** mean_reversion
**What it does:** Buys when the composite ConnorsRSI sinks into deeply oversold territory. It averages a 3-period price RSI, a 2-period streak RSI, and a 100-bar percent-rank of one-day change, and entering the bottom zone is treated as buy-the-weakness.
**Entry signal:** ConnorsRSI crosses from at-or-above to strictly below the threshold (default 10). Recursive RSIs and the ROC window resume from persisted state.
**Indicators/defaults:** Price RSI length (rsi_period, default 3); streak RSI length (streak_period, default 2); percent-rank lookback (rank_period, default 100); oversold entry (threshold, default 10).
**Market behavior identified:** Short-term mean reversion, buying an extreme washed-out composite reading.
**Timeframe:** Completed 1-minute bars; warmup_bars 103; no daily-history bars.

### keltner_channel_reversion
**Name:** Keltner Channel Reversion
**Category:** mean_reversion
**What it does:** Buys the snap back inside the lower Keltner band after an overextended dip. Unlike the upper-band breakout variant, this fades a downside excursion once price re-enters the channel.
**Entry signal:** The prior close sat below the prior lower band while the latest close is back above the latest lower band, where bands are EMA(20) minus/plus 2x Wilder ATR(10).
**Indicators/defaults:** Centre-line EMA (ema_period, default 20); band ATR length (atr_period, default 10); band offset (multiplier, default 2).
**Market behavior identified:** Mean reversion as an overstretched close re-enters its volatility channel.
**Timeframe:** Completed 1-minute bars; warmup_bars 21; no daily-history bars.

### rsi2_trend_pullback
**Name:** RSI2 Trend Pullback
**Category:** mean_reversion
**What it does:** Buys brief oversold dips inside a long-term uptrend, Connors style. The stock must be above its 200-bar average; only then does a plunge in the 2-period RSI become a buy.
**Entry signal:** With the close above its 200-bar SMA, the 2-period Wilder RSI crosses from at or above 10 to below 10 on the latest bar (edge-triggered on the first crossing, not every oversold bar).
**Indicators/defaults:** Wilder RSI period 2; entry threshold 10; trend SMA 200.
**Market behavior identified:** Short-term mean reversion (oversold pullback) inside a major uptrend.
**Timeframe:** 1m trigger (1m required); warmup_bars 200; no daily history.

### z_score_mean_reversion
**Name:** Z Score Mean Reversion
**Category:** mean_reversion
**What it does:** Buys when price stretches unusually far below its own recent average in statistical terms. The z-score counts how many standard deviations the close sits from its rolling mean.
**Entry signal:** The rolling z-score crosses from at or above −2 to below −2 on the latest bar (first entry into the stretched-low zone, not the recovery back).
**Indicators/defaults:** Rolling period 20; entry_z 2.0; population standard deviation.
**Market behavior identified:** Statistical mean reversion after an abnormal downside stretch.
**Timeframe:** 1m trigger (1m required); warmup_bars 21; no daily history.

---

## 3. SELL Strategies (43)

---

This section documents all 43 registered SELL strategies. **SELL strategies close existing long positions; they never open a short** (that is SHORT's job). While a long is held, all configured SELL strategies are evaluated on every completed bar and the exit arbitration picks a single winner — safety exits (protective stop, manual close, hard risk, forced liquidation) outrank every strategy exit, and among strategies the first valid signal in the deterministic tie-break order wins; the exit closes the full remaining position. **A protective stop can close a position even when no SELL strategy has signaled** — stops are evaluated independently on every cycle. All blocks below are advisory exit rules; none submit orders by itself.


### accumulation_distribution_breakdown
**Name:** Accumulation Distribution Breakdown
**Category:** volume
**What it does:** Exits a long when the cumulative money-flow line breaks its own rolling support, meaning flow has turned against holders.
**Exit trigger:** The A/D Line crosses below its rolling support (prior line at or above prior support, latest line below latest support) on completed bars.
**Indicators/defaults:** Accumulation/Distribution line; support_period=20.
**Market behavior identified:** Volume-flow breakdown; accumulation giving way to distribution.
**Timeframe:** 1m trigger and required; warmup_bars 22; no daily-history bars.

### adx_trend_weakening
**Name:** ADX Trend Weakening
**Category:** trend_following
**What it does:** Exits a long when trend strength itself evaporates, since a directionless market no longer supports holding for trend continuation.
**Exit trigger:** Wilder's ADX crosses below the no-trend threshold on a completed bar (prior ADX at or above it, latest below it).
**Indicators/defaults:** ADX; period=14, threshold=20.
**Market behavior identified:** Weakening trend strength (not direction); trend dissipating.
**Timeframe:** 1m trigger and required; warmup_bars 29; no daily-history bars.

### aroon_bearish_reversal
**Name:** Aroon Bearish Reversal
**Category:** trend_following
**What it does:** Exits a long when trend-measurement flips against the position, with new lows dominating new highs.
**Exit trigger:** Aroon-Down crosses above Aroon-Up on a completed bar (prior Down at or below Up, latest Down above Up).
**Indicators/defaults:** Aroon Up/Down; period=25.
**Market behavior identified:** Trend reversal; an emerging downtrend.
**Timeframe:** 1m trigger and required; warmup_bars 27; no daily-history bars.

### awesome_oscillator_reversal
**Name:** Awesome Oscillator Reversal
**Category:** oscillator
**What it does:** Exits a long when Bill Williams's medium-vs-long median-price momentum turns negative.
**Exit trigger:** The Awesome Oscillator crosses below zero on a completed bar (prior at or above zero, latest below zero).
**Indicators/defaults:** Awesome Oscillator with fixed 5/34 median-price periods; no parameters.
**Market behavior identified:** Momentum reversal through the neutral line.
**Timeframe:** 1m trigger and required; warmup_bars 35; no daily-history bars.

### bearish_engulfing_exit
**Name:** Bearish Engulfing Exit
**Category:** price_action
**What it does:** Exits a long on a two-bar bearish engulfing pattern, where sellers fully overwhelm the prior bar's range.
**Exit trigger:** On two consecutive completed bars, the latest bearish bar's real body engulfs the prior bar's real body (bearish engulfing pattern).
**Indicators/defaults:** Candle real bodies only; no parameters.
**Market behavior identified:** Sharp short-term rejection and reversal candle.
**Timeframe:** 1m trigger and required; warmup_bars 2; no daily-history bars.

### bearish_momentum_divergence
**Name:** Bearish Momentum Divergence
**Category:** oscillator
**What it does:** Exits a long when price makes a higher confirmed high that weaker momentum cannot confirm, the classic topping divergence.
**Exit trigger:** Two confirmed price pivot highs exist with the newer high above the older one, but same-bar n-period close-difference momentum at the newer high is lower than at the older high.
**Indicators/defaults:** Close-difference momentum; momentum_period=10, strength=2, lookback=50.
**Market behavior identified:** Bearish divergence; rally on weakening thrust.
**Timeframe:** 1m trigger and required; warmup_bars 60; no daily-history bars.

### bollinger_upper_band_reversal
**Name:** Bollinger Upper Band Reversal
**Category:** volatility
**What it does:** Exits a long when an upside stretch outside the bands snaps back inside, treating the extension as failed.
**Exit trigger:** The prior close is above the prior upper band while the latest close is back below the latest upper band, on completed adjacent bars.
**Indicators/defaults:** Bollinger Bands (population standard deviation); period=20, deviations=2.
**Market behavior identified:** Exhaustion reversal after a volatility stretch.
**Timeframe:** 1m trigger and required; warmup_bars 21; no daily-history bars.

### cci_bearish_reversal
**Name:** CCI Bearish Reversal
**Category:** oscillator
**What it does:** Exits a long when CCI drops back out of its overbought zone, treating the turn as buying pressure spent.
**Exit trigger:** CCI crosses below +100 on a completed bar (prior value at or above +100, latest value below +100, per the strategy source).
**Indicators/defaults:** Commodity Channel Index; period=20.
**Market behavior identified:** Overbought reversal in price momentum.
**Timeframe:** 1m trigger and required; warmup_bars 21; no daily-history bars.

### cci_zero_line_breakdown
**Name:** CCI Zero Line Breakdown
**Category:** oscillator
**What it does:** Exits a long when the Commodity Channel Index loses its zero line, meaning price action has shifted below its average trend level.
**Exit trigger:** CCI crosses below zero on a completed bar (prior value at or above zero, latest value below zero).
**Indicators/defaults:** Commodity Channel Index; period=20.
**Market behavior identified:** Momentum breakdown through the neutral line.
**Timeframe:** 1m trigger and required; warmup_bars 21; no daily-history bars.

### chaikin_money_flow_reversal
**Name:** Chaikin Money Flow Reversal
**Category:** volume
**What it does:** Exits a long when cumulative money flow turns negative, meaning distribution outweighs accumulation.
**Exit trigger:** Rolling Chaikin Money Flow crosses below its zero line on a completed bar (prior at or above zero, latest below zero).
**Indicators/defaults:** Chaikin Money Flow; period=20.
**Market behavior identified:** Money-flow breakdown; sellers dominating flow.
**Timeframe:** 1m trigger and required; warmup_bars 21; no daily-history bars.

### dmi_bearish_crossover
**Name:** DMI Bearish Crossover
**Category:** trend_following
**What it does:** Exits a long when sellers seize directional control from buyers on Wilder's directional movement gauges.
**Exit trigger:** Minus-DI (−DI) crosses above Plus-DI (+DI) on a completed bar (prior −DI at or below +DI, latest −DI above +DI).
**Indicators/defaults:** Wilder DMI (+DI/−DI); period=14.
**Market behavior identified:** Bearish trend reversal; negative direction taking over.
**Timeframe:** 1m trigger and required; warmup_bars 16; no daily-history bars.

### donchian_lower_channel_exit
**Name:** Donchian Lower Channel Exit
**Category:** breakout
**What it does:** Exits a long while price trades below its recent floor, treating a new closing low as the up-move being over.
**Exit trigger:** Level condition (not a one-time cross): the latest completed close is below the lowest low of the preceding period bars.
**Indicators/defaults:** Donchian lower channel; period=20.
**Market behavior identified:** Breakdown; fresh closing lows and support failure.
**Timeframe:** 1m trigger and required; warmup_bars 21; no daily-history bars.

### double_top_reversal
**Name:** Double Top Reversal
**Category:** price_action
**What it does:** Exits a long when a two-peak topping pattern completes with a break of the neckline between the peaks.
**Exit trigger:** Two similar confirmed peaks (within the peak tolerance) separated by a deep-enough trough complete when the latest bar crosses below neckline support.
**Indicators/defaults:** Confirmed fractal peaks; strength=2, lookback=60, peak_tolerance=0.03, min_decline=0.10.
**Market behavior identified:** Topping-pattern reversal (double top).
**Timeframe:** 1m trigger and required; warmup_bars 61; no daily-history bars.

### ema_bearish_crossover
**Name:** EMA Bearish Crossover
**Category:** moving_average
**What it does:** Exits a long when the faster average rolls over beneath the slower one, a classic short-term trend turn.
**Exit trigger:** Fast EMA of closes crosses below slow EMA on a completed bar (prior fast at or above slow, latest fast below slow).
**Indicators/defaults:** EMAs of closes; fast_period=10, slow_period=50.
**Market behavior identified:** Trend reversal; short-term average breaking down against the longer trend.
**Timeframe:** 1m trigger and required; warmup_bars 51; no daily-history bars.

### failed_breakout_exit
**Name:** Failed Breakout Exit
**Category:** breakout
**What it does:** Exits a long when a fresh upside breakout immediately fails, the classic bull-trap shape.
**Exit trigger:** A bar closes above the highest high of the lookback window (breakout), and the immediately following bar closes back below that same level.
**Indicators/defaults:** Highest-high breakout level; period=20.
**Market behavior identified:** Failed breakout; upside push rejected at the highs.
**Timeframe:** 1m trigger and required; warmup_bars 23; no daily-history bars.

### head_and_shoulders_breakdown
**Name:** Head And Shoulders Breakdown
**Category:** price_action
**What it does:** Exits a long when a three-peak top completes downward through the line joining its reaction lows.
**Exit trigger:** A left-shoulder/head/right-shoulder shape with the head highest completes when a close crosses below the neckline through the two intervening lows.
**Indicators/defaults:** Confirmed fractal peaks; strength=2, lookback=60.
**Market behavior identified:** Major top reversal (head and shoulders).
**Timeframe:** 1m trigger and required; warmup_bars 61; no daily-history bars.

### high_volume_reversal
**Name:** High Volume Reversal
**Category:** volume
**What it does:** Exits a long when an uptrend ends in a heavy-volume bearish rejection bar, treating the volume surge as supply overwhelming demand.
**Exit trigger:** After a qualifying prior uptrend, the latest bar carries volume at least the multiple of its baseline average and forms a bearish rejection candle closing below the prior close in weak territory.
**Indicators/defaults:** Volume SMA baseline plus candle shape; trend_bars=5, volume_period=20, volume_multiple=2.0.
**Market behavior identified:** Supply-driven reversal; high-volume rejection of higher prices.
**Timeframe:** 1m trigger and required; warmup_bars 22; no daily-history bars.

### ichimoku_bearish_exit
**Name:** Ichimoku Bearish Exit
**Category:** trend_following
**What it does:** Exits a long when price falls out of Ichimoku cloud support on a point-in-time basis, with no forward-looking data.
**Exit trigger:** The close crosses below the lower edge of the point-in-time cloud (prior close at or above the prior cloud bottom, latest close below the latest cloud bottom).
**Indicators/defaults:** Ichimoku cloud with fixed 9/26/52/26 settings (Chikou excluded); no parameters.
**Market behavior identified:** Trend reversal; loss of cloud support.
**Timeframe:** 1m trigger and required; warmup_bars 79; no daily-history bars.

### keltner_channel_breakdown
**Name:** Keltner Channel Breakdown
**Category:** volatility
**What it does:** Exits a long when price closes beneath Keltner's original lower channel, a volatility-adjusted support break.
**Exit trigger:** Prior close at or above the prior lower channel with the latest close below the latest lower channel, on completed adjacent bars.
**Indicators/defaults:** Original Keltner channel (SMA plus range, not EMA/ATR); period=10.
**Market behavior identified:** Breakdown below volatility-based support.
**Timeframe:** 1m trigger and required; warmup_bars 11; no daily-history bars.

### lower_low_confirmation
**Name:** Lower Low Confirmation
**Category:** price_action
**What it does:** Exits a long once the chart confirms a descending structure, waiting until the new low is knowable rather than guessing early.
**Exit trigger:** Two confirmed swing lows exist and the newly confirmed low (knowable only after two right-hand bars close) sits below the preceding swing low.
**Indicators/defaults:** Swing pivot lows; lookback=30.
**Market behavior identified:** Confirmed downtrend structure (lower lows).
**Timeframe:** 1m trigger and required; warmup_bars 31; no daily-history bars.

### macd_bearish_crossover
**Name:** MACD Bearish Crossover
**Category:** momentum
**What it does:** Exits a long when MACD momentum turns against the position relative to its own signal line.
**Exit trigger:** The MACD line crosses below its signal line on a completed bar (prior MACD at or above signal, latest MACD below signal).
**Indicators/defaults:** MACD of closes; fast_period=12, slow_period=26, signal_period=9.
**Market behavior identified:** Momentum reversal; upside thrust rolling over.
**Timeframe:** 1m trigger and required; warmup_bars 35; no daily-history bars.

### macd_zero_line_breakdown
**Name:** MACD Zero Line Breakdown
**Category:** momentum
**What it does:** Exits a long when MACD itself turns negative, meaning the fast average has fallen behind the slow one outright.
**Exit trigger:** The MACD line (fast EMA minus slow EMA) crosses below zero on a completed bar (prior at or above zero, latest below zero).
**Indicators/defaults:** MACD line only; fast_period=12, slow_period=26.
**Market behavior identified:** Momentum breakdown from positive to negative regime.
**Timeframe:** 1m trigger and required; warmup_bars 27; no daily-history bars.

### momentum_reversal
**Name:** Momentum Reversal
**Category:** momentum
**What it does:** Exits a long when short-horizon price momentum flips from positive to clearly negative, catching a stall that turns into a slide.
**Exit trigger:** The prior n-bar rate of change is above zero while the latest overlapping n-bar rate of change is at or below the negative threshold (default −0.02), on closed 1-minute bars within the same session.
**Indicators/defaults:** Rate of change over closes; lookback_bars=5, reversal_threshold=-0.02.
**Market behavior identified:** Momentum reversal; weakening then negative short-term thrust.
**Timeframe:** 1m trigger and required; warmup_bars 4; no daily-history bars.

### money_flow_index_reversal
**Name:** Money Flow Index Reversal
**Category:** volume
**What it does:** Exits a long when volume-weighted buying pressure reverses out of overbought territory.
**Exit trigger:** Money Flow Index crosses below 80 on a completed bar (prior at or above 80, latest below 80, per the strategy source).
**Indicators/defaults:** Money Flow Index (volume-weighted); period=14.
**Market behavior identified:** Overbought reversal with volume confirming the fade.
**Timeframe:** 1m trigger and required; warmup_bars 16; no daily-history bars.

### obv_breakdown
**Name:** OBV Breakdown
**Category:** volume
**What it does:** Exits a long when cumulative on-balance volume breaks its own recent floor, meaning volume flow has turned against the rally.
**Exit trigger:** Cumulative OBV crosses below its lowest value in the prior window (prior OBV at or above prior support, latest OBV below latest support).
**Indicators/defaults:** On-Balance Volume; support_period=20.
**Market behavior identified:** Volume-flow breakdown; distribution under the surface.
**Timeframe:** 1m trigger and required; warmup_bars 22; no daily-history bars.

### opening_range_breakdown
**Name:** Opening Range Breakdown
**Category:** intraday_session
**What it does:** Exits a long when price breaks down out of the morning's established trading range to the downside.
**Exit trigger:** After the first N minutes complete, a close crosses below the range low: prior close at or above the low of the opening-range minutes, latest close below it.
**Indicators/defaults:** Opening range high/low; opening_range_minutes=30.
**Market behavior identified:** Intraday breakdown below the opening balance.
**Timeframe:** 1m trigger and required; warmup_bars 31; no daily-history bars.

### parabolic_sar_reversal
**Name:** Parabolic SAR Reversal
**Category:** trend_following
**What it does:** Exits a long when the trailing stop-and-reverse dots flip above price, ending the defined uptrend.
**Exit trigger:** The Parabolic SAR state flips from uptrend to downtrend on a completed bar, i.e. price penetrates the SAR from above.
**Indicators/defaults:** Parabolic SAR; step=0.02, maximum=0.20.
**Market behavior identified:** Trend reversal; trailing support giving way.
**Timeframe:** 1m trigger and required; warmup_bars 3; no daily-history bars.

### previous_swing_low_break
**Name:** Previous Swing Low Break
**Category:** price_action
**What it does:** Exits a long when price closes through the most recent confirmed swing low, breaking the last defended floor.
**Exit trigger:** The latest close falls below the newest confirmed five-bar pivot low (two bars each side) found within the lookback, with the prior close at or above it.
**Indicators/defaults:** Swing pivot lows; lookback=20.
**Market behavior identified:** Structure breakdown; swing support failure.
**Timeframe:** 1m trigger and required; warmup_bars 21; no daily-history bars.

### price_below_ema
**Name:** Price Below EMA
**Category:** moving_average
**What it does:** Exits a long while price sits under its own smoothed average, treating the position as working against the near-term drift.
**Exit trigger:** Level condition: the latest completed close is below the EMA of closes.
**Indicators/defaults:** EMA of closes; period=20.
**Market behavior identified:** Weakness; price losing its short-term average.
**Timeframe:** 1m trigger and required; warmup_bars 20; no daily-history bars.

### price_below_sma
**Name:** Price Below SMA
**Category:** moving_average
**What it does:** Exits a long while price holds beneath its longer simple average, a plain trend-health check.
**Exit trigger:** Level condition: the latest completed close is below the SMA of closes.
**Indicators/defaults:** SMA of closes; period=50.
**Market behavior identified:** Trend deterioration; up-move no longer intact versus the longer average.
**Timeframe:** 1m trigger and required; warmup_bars 50; no daily-history bars.

### rsi_centerline_breakdown
**Name:** RSI Centerline Breakdown
**Category:** oscillator
**What it does:** Exits a long when RSI slips into its lower half, meaning average gains no longer dominate average losses.
**Exit trigger:** Wilder RSI crosses below the 50 centerline on a completed bar (prior RSI at or above 50, latest below 50).
**Indicators/defaults:** Wilder RSI; rsi_period=14.
**Market behavior identified:** Momentum regime shift from bullish half to bearish half.
**Timeframe:** 1m trigger and required; warmup_bars 16; no daily-history bars.

### rsi_reversal
**Name:** RSI Reversal
**Category:** oscillator
**What it does:** Exits a long when Wilder RSI reverses down out of overbought territory, treating the turn as exhaustion of the buying push.
**Exit trigger:** Prior-bar RSI at or above the overbought level with latest-bar RSI back below it, on closed bars; disarms after each signal until the documented reset condition is met.
**Indicators/defaults:** Wilder RSI; rsi_period=14, overbought=70, reset_threshold=50, timeframe=5m.
**Market behavior identified:** Overbought reversal; fading upside momentum.
**Timeframe:** 5m trigger and required; warmup_bars 16; no daily-history bars.

### shooting_star_reversal
**Name:** Shooting Star Reversal
**Category:** price_action
**What it does:** Exits a long when an uptrend produces a shooting-star rejection wick and the next bar confirms weakness below the star's low.
**Exit trigger:** After a rising close over the trend window, a shooting star forms (upper shadow at least the ratio multiple of the body, tiny lower shadow), and the following bar closes below the star's low.
**Indicators/defaults:** Candle shape; trend_bars=5, shadow_ratio=2, max_lower_shadow=0.1.
**Market behavior identified:** Rejection-wick top with bearish confirmation.
**Timeframe:** 1m trigger and required; warmup_bars 8; no daily-history bars.

### sma_bearish_crossover
**Name:** SMA Bearish Crossover
**Category:** moving_average
**What it does:** Exits a long on the slow long-horizon death-cross style turn, when the medium average falls beneath the very long one.
**Exit trigger:** Fast SMA of closes crosses below slow SMA on a completed bar (prior fast at or above slow, latest fast below slow).
**Indicators/defaults:** SMAs of closes; fast_period=50, slow_period=200.
**Market behavior identified:** Long-horizon trend reversal.
**Timeframe:** 1m trigger and required; warmup_bars 201; no daily-history bars.

### stochastic_bearish_crossover
**Name:** Stochastic Bearish Crossover
**Category:** oscillator
**What it does:** Exits a long when the faster stochastic line turns down through its slower companion, a short-term momentum turn.
**Exit trigger:** Full Stochastic %K crosses below %D on a completed bar (prior %K at or above %D, latest %K below %D).
**Indicators/defaults:** Full Stochastic; k_period=14, k_smooth=3, d_period=3.
**Market behavior identified:** Weakening momentum; fast line losing to the smoothed line.
**Timeframe:** 1m trigger and required; warmup_bars 19; no daily-history bars.

### stochastic_rsi_reversal
**Name:** Stochastic RSI Reversal
**Category:** oscillator
**What it does:** Exits a long when the RSI-of-RSI gauge leaves its overbought region, catching momentum-of-momentum turning first.
**Exit trigger:** Unsmoothed Stochastic RSI crosses below the overbought boundary on a completed bar (prior at or above it, latest below it).
**Indicators/defaults:** Stochastic RSI over Wilder RSI; rsi_period=14, stochastic_period=14, overbought=80.
**Market behavior identified:** Overbought reversal in RSI momentum.
**Timeframe:** 1m trigger and required; warmup_bars 29; no daily-history bars.

### supertrend_bearish_flip
**Name:** Supertrend Bearish Flip
**Category:** trend_following
**What it does:** Exits a long when the recursive volatility-trailing Supertrend flips from supporting price to pressuring it from above.
**Exit trigger:** The Supertrend direction changes from up to down between the prior and latest completed bars.
**Indicators/defaults:** Supertrend (Wilder ATR bands); atr_period=10, multiplier=3.
**Market behavior identified:** Trailing-trend flip from bullish to bearish.
**Timeframe:** 1m trigger and required; warmup_bars 11; no daily-history bars.

### trendline_breakdown
**Name:** Trendline Breakdown
**Category:** price_action
**What it does:** Exits a long when price breaks a mechanically drawn rising support line, removing judgment from the trendline.
**Exit trigger:** The latest close falls below an upward-sloping line through two successively higher confirmed pivot lows, with the prior close at or above that line.
**Indicators/defaults:** Pivot-low trendline; lookback=30.
**Market behavior identified:** Rising-support break; uptrend structure failing.
**Timeframe:** 1m trigger and required; warmup_bars 31; no daily-history bars.

### ultimate_oscillator_reversal
**Name:** Ultimate Oscillator Reversal
**Category:** oscillator
**What it does:** Exits a long when Williams's multi-length buying-pressure gauge drops out of overbought.
**Exit trigger:** The Ultimate Oscillator crosses below the overbought level on a completed bar (prior at or above it, latest below it).
**Indicators/defaults:** Ultimate Oscillator (4:2:1 weighted buying pressure over true range); short_period=7, medium_period=14, long_period=28, overbought=70.
**Market behavior identified:** Overbought reversal in blended buying pressure.
**Timeframe:** 1m trigger and required; warmup_bars 30; no daily-history bars.

### volatility_expansion_reversal
**Name:** Volatility Expansion Reversal
**Category:** volatility
**What it does:** Exits a long when an uptrend ends in an outsized bearish outside reversal bar on expanding range.
**Exit trigger:** After a qualifying prior uptrend, the latest bar's True Range reaches at least the expansion multiple of baseline ATR and the bar forms a bearish outside key reversal.
**Indicators/defaults:** True Range versus finite ATR baseline; trend_bars=5, atr_period=14, expansion_multiple=1.5.
**Market behavior identified:** Volatility-expansion top; sharp rejection after an advance.
**Timeframe:** 1m trigger and required; warmup_bars 17; no daily-history bars.

### volume_climax_exit
**Name:** Volume Climax Exit
**Category:** volume
**What it does:** Exits a long on a Wyckoff-style buying climax: an advance ends in a new high on extreme volume and wide spread, then the next bar's reaction confirms demand spent.
**Exit trigger:** After a prior advance, the climax bar sets a new swing high on volume and spread at or above their multiples, and the following Automatic Reaction bar confirms weakness.
**Indicators/defaults:** Volume SMA and ATR spread baselines; trend_bars=5, lookback=15, climax_multiple=2.5, spread_multiple=1.25.
**Market behavior identified:** Buying-climax exhaustion; demand spent at the top.
**Timeframe:** 1m trigger and required; warmup_bars 22; no daily-history bars.

43 blocks written.

### vwap_breakdown
**Name:** VWAP Breakdown
**Category:** vwap
**What it does:** Exits a long when price loses the session's volume-weighted average price, treating VWAP as intraday fair value and support.
**Exit trigger:** On a closed 1-minute bar, the prior close was at or above its session VWAP while the latest close is strictly below its session VWAP (edge-triggered downward cross; VWAP resets each session).
**Indicators/defaults:** Session VWAP read from the shared indicator service (`vwap`, `vwap_prev`); no tunable parameters.
**Market behavior identified:** Intraday breakdown below the benchmark average; loss of session-level support.
**Timeframe:** 1m trigger and required; warmup_bars 2; no daily-history bars.

### williams_r_reversal
**Name:** Williams %R Reversal
**Category:** oscillator
**What it does:** Exits a long when a rally pressed against its recent highs starts slipping back, catching overbought fade early.
**Exit trigger:** Williams %R falls back below the overbought level on a completed bar after sitting at or above it on the prior bar.
**Indicators/defaults:** Williams %R; period=14, overbought=-20.
**Market behavior identified:** Overbought reversal; rally losing thrust near the highs.
**Timeframe:** 1m trigger and required; warmup_bars 15; no daily-history bars.

---

## 4. SHORT Strategies (28)

---

This section documents all 28 registered SHORT strategies — the entry side for short positions. Shorting is **opt-in and fail-closed**: a short strategy is only eligible when `execution.short_selling_enabled: true` is set, and that switch cannot be enabled for a LIVE account. **Autonomous short execution is available in SHADOW and BACKTEST only**; PAPER/LIVE short order flow has never been verified, and LIVE short selling is permanently disabled by configuration. A SHORT signal opens a new short position and is distinct from SELL (which closes a long). The market-status monitor may restrict new short entries by market state (WARNING/CRASH allow shorts under the default policy; STABILIZING and RECOVERY restrict them). Opening a short requires the `WATCHING_SHORT` armed state, passes the same entry gates as a BUY, and triggers collateral/margin checks — short proceeds are restricted collateral, never purchasing power.


### short_adx_bearish_trend_confirmation
**Name:** ADX Bearish Trend Confirmation
**Category:** trend_following
**What it does:** Shorts only when a downtrend is both correctly directed and measurably strong on a completed bar.
**Entry trigger:** ADX crosses above threshold (25) while minus-DI is above plus-DI, using Wilder 14-period DI/ADX resumed from persisted state.
**Indicators/defaults:** Wilder ADX and DIs, period=14, threshold=25.
**Bearish condition detected:** Confirmed strong downtrend (direction plus strength, unlike a bare DMI cross).
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 29; no daily-history bars.

### short_aroon_bearish_reversal
**Name:** Aroon Bearish Reversal
**Category:** trend_following
**What it does:** Shorts the emergence of a new downtrend as downside recency dominates upside recency on a completed bar.
**Entry trigger:** Aroon-Down crosses above Aroon-Up on adjacent completed bars (prior Down at/below Up, latest Down above Up).
**Indicators/defaults:** Aroon Up/Down, period=25.
**Bearish condition detected:** Emerging downtrend after highs grow stale and lows grow fresh.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 27; no daily-history bars.

### short_bear_flag_breakdown
**Name:** Bear Flag Breakdown
**Category:** price_action
**What it does:** Shorts the downside continuation after a sharp drop pauses in a shallow upward flag and then breaks lower.
**Entry trigger:** A ≥1% falling pole within ≤15 bars, then a 3–20 bar flag retracing ≤50% while holding above the pole low, then a close below the flag lows.
**Indicators/defaults:** Pole/flag structure, lookback=60, pole_bars=15, pole_pct=1, min_flag_bars=3, max_flag_bars=20, max_retrace=0.5.
**Bearish condition detected:** Bearish continuation after a corrective pause.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 61; no daily-history bars.

### short_bearish_engulfing
**Name:** Bearish Engulfing
**Category:** price_action
**What it does:** Shorts a sharp upside-to-downside reversal after a short advance, marked by a large red bar swallowing the prior green body.
**Entry trigger:** After a 3-bar close-to-close advance, the latest bearish candle's real body engulfs the prior bullish candle's body, on fully adjacent bars.
**Indicators/defaults:** Candle pattern only, trend_bars=3.
**Bearish condition detected:** Overbought reversal with sellers overwhelming buyers.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 5; no daily-history bars.

### short_bearish_momentum_divergence
**Name:** Bearish Momentum Divergence
**Category:** oscillator
**What it does:** Shorts a rally whose new price high is not matched by momentum, signaling a weakening push.
**Entry trigger:** The latest confirmed price pivot high exceeds the prior one while its same-bar n-period close-difference momentum is lower (10-period momentum, strength 2, 50-bar lookback).
**Indicators/defaults:** Price pivots plus close-difference momentum, momentum_period=10, strength=2, lookback=50.
**Bearish condition detected:** Bearish divergence with rally strength fading under the surface.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 60; no daily-history bars.

### short_bollinger_lower_band_breakdown
**Name:** Bollinger Lower Band Breakdown
**Category:** breakout
**What it does:** Shorts when the close breaks beneath the lower volatility envelope on a completed bar.
**Entry trigger:** Prior close at/above the lower band and latest close below the recomputed lower band, on adjacent completed bars.
**Indicators/defaults:** Bollinger bands on closes, period=20, deviations=2 (population standard deviation).
**Bearish condition detected:** Volatility-envelope breakdown and downside expansion.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 21; no daily-history bars.

### short_cci_zero_line_breakdown
**Name:** CCI Zero Line Breakdown
**Category:** oscillator
**What it does:** Shorts when the Commodity Channel Index flips from above to below its zero mean line on a completed bar.
**Entry trigger:** CCI crosses below zero: prior value at/above zero and latest value below zero, on adjacent completed bars.
**Indicators/defaults:** CCI, period=20.
**Bearish condition detected:** Momentum-regime flip from firm to weak relative to its average.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 21; no daily-history bars.

### short_dmi_bearish_crossover
**Name:** DMI Bearish Crossover
**Category:** trend_following
**What it does:** Shorts when selling pressure overtakes buying pressure in the directional-movement system on a completed bar.
**Entry trigger:** Minus-DI crosses above plus-DI: prior -DI at/below +DI and latest -DI above +DI, on adjacent bars.
**Indicators/defaults:** Wilder +DI/-DI, period=14.
**Bearish condition detected:** Sellers seizing directional control (no strength filter).
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 16; no daily-history bars.

### short_donchian_breakdown
**Name:** Donchian Breakdown
**Category:** breakout
**What it does:** Shorts a breakdown where the close undercuts the lowest low of the recent range on a completed bar.
**Entry trigger:** Latest close below the lowest low of the preceding period bars (period=20), with the signal bar excluded from the reference window.
**Indicators/defaults:** Donchian lower channel, period=20.
**Bearish condition detected:** Range breakdown and emerging downside expansion.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 21; no daily-history bars.

### short_double_top_breakdown
**Name:** Double Top Breakdown
**Category:** price_action
**What it does:** Shorts the completion of a two-peak topping formation when support finally gives way on a completed bar.
**Entry trigger:** Two similar confirmed fractal peaks (within 3%, ≥10% peak-to-trough decline, strength 2, 60-bar lookback) followed by a close below the intervening-trough neckline.
**Indicators/defaults:** Swing fractals, strength=2, lookback=60, peak_tolerance=0.03, min_decline=0.10.
**Bearish condition detected:** Topping-pattern breakdown after buyers fail twice near the same high.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 61; no daily-history bars.

### short_ema_bearish_crossover
**Name:** EMA Bearish Crossover
**Category:** moving_average
**What it does:** Opens a short when short-term trend momentum rolls over beneath longer-term momentum on completed 1-minute bars.
**Entry trigger:** Fast EMA (12) crosses below slow EMA (26): prior bar fast at/above slow and latest bar fast below slow.
**Indicators/defaults:** EMAs of closes, fast_period=12, slow_period=26 (recursive from persisted state, SMA-seeded).
**Bearish condition detected:** Trend reversal into a new downtrend.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 27; no daily-history bars.

### short_failed_breakout_reversal
**Name:** Failed Breakout Reversal
**Category:** breakout
**What it does:** Shorts the bull trap where an upside breakout immediately fails and price falls back into the range.
**Entry trigger:** A bar closes above the highest high of the prior 20-bar reference window, then the immediately following bar closes back below that same level.
**Indicators/defaults:** Donchian-style reference high, period=20.
**Bearish condition detected:** Failed breakout reversal with trapped longs.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 22; no daily-history bars.

### short_head_and_shoulders_breakdown
**Name:** Head And Shoulders Breakdown
**Category:** price_action
**What it does:** Shorts the completion of a three-peak top when price closes through the reaction-low neckline.
**Entry trigger:** Confirmed left-shoulder/head/right-shoulder with the head highest, then the latest close crosses below the extrapolated line joining the two intervening reaction lows.
**Indicators/defaults:** Swing fractals, strength=2, lookback=60; no symmetry or volume filter imposed.
**Bearish condition detected:** Classic topping-pattern breakdown as rally structure fails.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 61; no daily-history bars.

### short_ichimoku_bearish_breakdown
**Name:** Ichimoku Bearish Breakdown
**Category:** trend_following
**What it does:** Shorts when price breaks down through the bottom of the point-in-time Ichimoku cloud on completed bars.
**Entry trigger:** Prior close at/above the cloud bottom and latest close below the cloud bottom, using only observations ending displacement bars earlier (no Chikou look-ahead).
**Indicators/defaults:** Ichimoku cloud, tenkan_period=9, kijun_period=26, senkou_b_period=52, displacement=26.
**Bearish condition detected:** Cloud breakdown and bearish equilibrium shift.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 79; no daily-history bars.

### short_keltner_channel_breakdown
**Name:** Keltner Channel Breakdown
**Category:** volatility
**What it does:** Shorts a breakdown below Keltner's original SMA-and-range channel on a completed bar.
**Entry trigger:** Prior close at/above the prior lower channel line and latest close below the recomputed lower line, on adjacent bars.
**Indicators/defaults:** Original Keltner channel (SMA plus range, not EMA/ATR), period=10.
**Bearish condition detected:** Volatility-channel breakdown to the downside.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 11; no daily-history bars.

### short_lower_low_confirmation
**Name:** Lower Low Confirmation
**Category:** price_action
**What it does:** Shorts the moment a newly confirmed pivot low prints beneath the prior pivot low, locking in a descending structure.
**Entry trigger:** The newest confirmed five-bar pivot low (knowable only after two right-hand bars close) is strictly lower than the previous one, within 30 bars.
**Indicators/defaults:** Confirmed pivot lows, lookback=30.
**Bearish condition detected:** Confirmed downtrend structure via a fresh lower low.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 31; no daily-history bars.

### short_macd_bearish_crossover
**Name:** MACD Bearish Crossover
**Category:** momentum
**What it does:** Shorts the moment MACD momentum crosses beneath its own signal line on a completed bar.
**Entry trigger:** MACD line crosses below the signal line: prior MACD at/above signal, latest MACD below signal, on adjacent bars.
**Indicators/defaults:** MACD of closes, fast_period=12, slow_period=26, signal_period=9.
**Bearish condition detected:** Momentum rollover from bullish to bearish.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 35; no daily-history bars.

### short_macd_zero_line_breakdown
**Name:** MACD Zero Line Breakdown
**Category:** momentum
**What it does:** Shorts when raw MACD momentum turns outright negative by crossing beneath zero on a completed bar.
**Entry trigger:** MACD line (fast EMA minus slow EMA) crosses below zero: prior MACD at/above zero, latest below zero.
**Indicators/defaults:** MACD line only, fast_period=12, slow_period=26.
**Bearish condition detected:** Momentum regime flip into negative territory.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 27; no daily-history bars.

### short_obv_breakdown
**Name:** OBV Breakdown
**Category:** volume
**What it does:** Shorts when cumulative volume flow breaks beneath its recent support level on a completed bar.
**Entry trigger:** Cumulative OBV crosses below its lowest value over the prior support window (support_period=20), evaluated on adjacent bars with snapshot-restored OBV.
**Indicators/defaults:** On-balance volume, support_period=20.
**Bearish condition detected:** Distribution with volume flow confirming downside.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 22; no daily-history bars.

### short_opening_range_breakdown
**Name:** Opening Range Breakdown
**Category:** intraday_session
**What it does:** Shorts an intraday failure when price breaks below the low of the early-session range on a completed bar.
**Entry trigger:** After the first 30 minutes from the 09:30 New York open, prior close at/above the range low and latest close below it, with every range minute present and adjacent trigger bars.
**Indicators/defaults:** Opening-range low, opening_range_minutes=30.
**Bearish condition detected:** Intraday breakdown and rejection of the opening range.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 31; no daily-history bars.

### short_parabolic_sar_bearish_reversal
**Name:** Parabolic SAR Bearish Reversal
**Category:** trend_following
**What it does:** Shorts the bar where the trailing stop-and-reverse system flips from supporting price to capping it.
**Entry trigger:** SAR trend flips uptrend to downtrend on the completed bar (price penetrates the SAR dot from above).
**Indicators/defaults:** Parabolic SAR, step=0.02, maximum=0.20.
**Bearish condition detected:** Trailing-trend reversal into a short-side regime.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 3; no daily-history bars.

### short_previous_swing_low_break
**Name:** Previous Swing Low Break
**Category:** price_action
**What it does:** Shorts the loss of the most recent structural swing-low support level on a completed bar.
**Entry trigger:** Prior close at/above the latest confirmed five-bar (strength-2) swing low and latest close below it, within a 20-bar lookback of adjacent bars.
**Indicators/defaults:** Confirmed swing-low pivot, lookback=20.
**Bearish condition detected:** Short-term support break in price structure.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 21; no daily-history bars.

### short_rsi_centerline_breakdown
**Name:** RSI Centerline Breakdown
**Category:** oscillator
**What it does:** Shorts when buying pressure fades and RSI slips through the 50 midline on a completed bar.
**Entry trigger:** Wilder RSI crosses below 50: prior RSI at/above 50 and latest RSI below 50, on adjacent bars.
**Indicators/defaults:** Wilder RSI of closes, rsi_period=14.
**Bearish condition detected:** Loss of upside momentum and bearish momentum-regime shift.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 16; no daily-history bars.

### short_shooting_star_reversal
**Name:** Shooting Star Reversal
**Category:** price_action
**What it does:** Shorts the confirmation after an uptrend shooting star shows strong intraday rejection of higher prices.
**Entry trigger:** After a 5-bar close advance, a star with a long upper shadow (≥2× body, tiny lower shadow ≤10% of range) prints, then the next bar closes below the star's low.
**Indicators/defaults:** Star shape, trend_bars=5, shadow_ratio=2, max_lower_shadow=0.1; no gap required.
**Bearish condition detected:** Overbought rejection and failed rally follow-through.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 8; no daily-history bars.

### short_sma_bearish_crossover
**Name:** SMA Bearish Crossover
**Category:** moving_average
**What it does:** Shorts when the faster average decisively crosses beneath the slower average, signaling a bearish regime shift.
**Entry trigger:** Fast SMA crosses below slow SMA on adjacent completed bars: prior fast at/above slow, latest fast below slow.
**Indicators/defaults:** SMAs of closes, fast_period=50, slow_period=200.
**Bearish condition detected:** Long-horizon downtrend confirmation (death-cross style on M1).
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 201; no daily-history bars.

### short_supertrend_bearish_flip
**Name:** Supertrend Bearish Flip
**Category:** trend_following
**What it does:** Opens a short the moment the Supertrend regime flips from uptrend to downtrend on a completed bar.
**Entry trigger:** Prior-bar Supertrend direction up and latest-bar direction down, using (high+low)/2 ± multiplier × Wilder ATR bands with ratcheting.
**Indicators/defaults:** Supertrend, atr_period=10, multiplier=3.
**Bearish condition detected:** Fresh trend flip into a volatility-confirmed downtrend.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 11; no daily-history bars.

### short_trendline_breakdown
**Name:** Trendline Breakdown
**Category:** price_action
**What it does:** Shorts the break of a rising support line drawn through two higher confirmed swing lows.
**Entry trigger:** Prior close at/above the extrapolated trendline and latest close below it, using the two most recent confirmed strength-2 pivot lows within 30 bars.
**Indicators/defaults:** Pivot trendline, lookback=30.
**Bearish condition detected:** Uptrend support failure as higher-low structure breaks.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 31; no daily-history bars.

### short_vwap_breakdown
**Name:** VWAP Breakdown
**Category:** vwap
**What it does:** Shorts an edge-triggered loss of the session average-price anchor on closed 1-minute bars.
**Entry trigger:** Price crosses from at/above session VWAP to below it on same-session adjacent bars (one signal per bar; positive-volume, warmed per-bar session VWAP; VWAP resets each session).
**Indicators/defaults:** Session VWAP (no tunable parameters).
**Bearish condition detected:** Intraday distribution with sellers dominating the session average.
**Timeframe:** Trigger 1m, requires 1m; warmup_bars 2; no daily-history bars.

---

## 5. COVER Strategies (28)

---

This section documents all 28 registered COVER strategies — the exit side for short positions. **COVER closes an existing short position; it never opens a long.** While a short is held, COVER strategies are evaluated on every completed bar and arbitrated like SELL exits; protective stops and safety exits outrank them. **Protective stops override ordinary COVER signals** — a short's stop sits above the market and is evaluated before any COVER strategy. A COVER exit never needs the short-selling switch (closing a short is always permitted), and a stop or cover fill can never create a long position.


### cover_aroon_bullish_reversal
**Name:** Aroon Bullish Reversal
**Category:** trend_following
**What it does:** Closes an existing short when trend-recency flips from bearish to bullish. Exit signal only; never opens a position.
**Cover trigger:** Aroon-Down strictly above Aroon-Up on prior bar, then Aroon-Up strictly above Aroon-Down on the latest completed bar.
**Indicators/defaults:** Aroon period 25 (Up/Down from bars since highest high/lowest low).
**Bullish condition detected:** New highs becoming more recent than new lows, indicating an emerging uptrend after bearish conditions.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 27; daily-history bars 0.

### cover_bollinger_lower_band_reversion
**Name:** Bollinger Lower Band Reversion
**Category:** mean_reversion
**What it does:** Closes an existing short when a stretched-low close snaps back inside the volatility band. Exit signal only; never opens a position.
**Cover trigger:** Prior close strictly below its lower band, then latest close strictly above its own lower band (middle = SMA, population SD).
**Indicators/defaults:** Bollinger period 20, deviations 2.
**Bullish condition detected:** Price re-entering the band from below, indicating the washout low is reversing toward the mean.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 21; daily-history bars 0.

### cover_bull_flag_breakout
**Name:** Bull Flag Breakout
**Category:** price_action
**What it does:** Closes an existing short when a bull-flag consolidation breaks upward. Exit signal only; never opens a position.
**Cover trigger:** Sharp pole advance, short shallow flag (limited retrace), then the close first exceeding the flag's highest high.
**Indicators/defaults:** lookback 60, pole_bars 15, pole_pct 1, min_flag_bars 3, max_flag_bars 20, max_retrace 0.5.
**Bullish condition detected:** Upside continuation after a brief pause, indicating renewed buying after consolidation.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 61; daily-history bars 0.

### cover_bullish_engulfing
**Name:** Bullish Engulfing
**Category:** price_action
**What it does:** Closes an existing short on a two-candle bullish engulfing pattern. Exit signal only; never opens a position.
**Cover trigger:** Prior bar bearish (close below open) followed by a bullish bar whose real body fully engulfs the prior real body; shadows ignored.
**Indicators/defaults:** Pure candle pattern; no tunable params.
**Bullish condition detected:** Buyers overwhelming the prior bar's sellers in one candle, indicating an abrupt upside shift.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 2; daily-history bars 0.

### cover_bullish_momentum_divergence
**Name:** Bullish Momentum Divergence
**Category:** oscillator
**What it does:** Closes an existing short when price makes a lower low but momentum improves. Exit signal only; never opens a position.
**Cover trigger:** Newest confirmed swing low below the prior swing low while close-difference momentum at the new pivot exceeds momentum at the old pivot.
**Indicators/defaults:** momentum_period 10, strength 2, lookback 50.
**Bullish condition detected:** Downside move losing force beneath the surface, indicating weakening sellers and a possible bounce.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 60; daily-history bars 0.

### cover_cci_oversold_reversal
**Name:** CCI Oversold Reversal
**Category:** mean_reversion
**What it does:** Closes an existing short when the commodity-channel reading recovers from oversold. Exit signal only; never opens a position.
**Cover trigger:** CCI at or below the oversold level on prior bar, then strictly above it on the latest completed bar.
**Indicators/defaults:** CCI on typical price, period 20, oversold −100.
**Bullish condition detected:** Deviation-from-average swinging back up from deeply negative, indicating selling exhaustion and a bounce.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 21; daily-history bars 0.

### cover_chaikin_money_flow_recovery
**Name:** Chaikin Money Flow Recovery
**Category:** volume
**What it does:** Closes an existing short when volume-weighted money flow turns positive. Exit signal only; never opens a position.
**Cover trigger:** Rolling n-bar CMF at or below zero ending on prior bar, then strictly above zero ending on the latest completed bar.
**Indicators/defaults:** Chaikin Money Flow period 20 (money-flow multiplier × volume).
**Bullish condition detected:** Accumulation (buy-side volume pressure) taking over after neutral-or-selling pressure.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 21; daily-history bars 0.

### cover_dmi_bullish_crossover
**Name:** DMI Bullish Crossover
**Category:** trend_following
**What it does:** Closes an existing short when upward directional movement overtakes downward movement. Exit signal only; never opens a position.
**Cover trigger:** +DI at or below −DI on prior bar, then +DI strictly above −DI on the latest completed bar; no ADX filter.
**Indicators/defaults:** Wilder DMI, period 14 (+DI, −DI).
**Bullish condition detected:** Buying direction overpowering selling direction, indicating trend control is shifting upward.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 16; daily-history bars 0.

### cover_donchian_breakout
**Name:** Donchian Breakout
**Category:** breakout
**What it does:** Closes an existing short when price breaks above recent resistance. Exit signal only; never opens a position.
**Cover trigger:** Latest completed close strictly above the highest high of the preceding period bars, signal bar excluded from the window.
**Indicators/defaults:** Donchian upper channel, period 20.
**Bullish condition detected:** Close breaking the prior range high, indicating upside breakout strength against the short.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 21; daily-history bars 0.

### cover_double_bottom_reversal
**Name:** Double Bottom Reversal
**Category:** price_action
**What it does:** Closes an existing short when a double-bottom neckline breaks upward. Exit signal only; never opens a position.
**Cover trigger:** Two similar confirmed swing lows (within tolerance, with a minimum interim peak) and the close first crossing above that interim peak.
**Indicators/defaults:** strength 2, lookback 100, tolerance_pct 0.5, min_rise_pct 1.
**Bullish condition detected:** Repeated support holding plus a break of the intervening high, indicating sellers failed twice and buyers broke out.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 101; daily-history bars 0.

### cover_ema_bullish_crossover
**Name:** EMA Bullish Crossover
**Category:** moving_average
**What it does:** Closes an existing short when short-term trend turns up relative to the longer trend. Exit signal only; never opens a position.
**Cover trigger:** Fast EMA at or below slow EMA on prior completed bar, then fast EMA strictly above slow EMA on the latest completed bar.
**Indicators/defaults:** EMA of closes, fast_period 12, slow_period 26.
**Bullish condition detected:** Short-term average reclaiming the long-term average, indicating upside momentum is overtaking the downtrend.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 27; daily-history bars 0.

### cover_hammer_reversal
**Name:** Hammer Reversal
**Category:** price_action
**What it does:** Closes an existing short on a confirmed hammer after a short decline. Exit signal only; never opens a position.
**Cover trigger:** Declining closes over the window, then a hammer-shape bar (long lower shadow, tiny upper shadow), then the next bar closing above the hammer's high.
**Indicators/defaults:** trend_bars 5, shadow_ratio 2, max_upper_shadow 0.1.
**Bullish condition detected:** Rejection of lows plus upside confirmation, indicating sellers failed and buyers followed through.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 8; daily-history bars 0.

### cover_higher_high_higher_low
**Name:** Higher High Higher Low
**Category:** price_action
**What it does:** Closes an existing short when confirmed swing structure turns into an uptrend. Exit signal only; never opens a position.
**Cover trigger:** On the bar confirming the newest swing low, the last two swing lows rise and the last two swing highs rise.
**Indicators/defaults:** strength 2, lookback 50.
**Bullish condition detected:** Dow-style uptrend structure (rising peaks and troughs), indicating trend control has flipped to buyers.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 51; daily-history bars 0.

### cover_inverse_head_and_shoulders
**Name:** Inverse Head and Shoulders
**Category:** price_action
**What it does:** Closes an existing short when an inverse head-and-shoulders neckline breaks upward. Exit signal only; never opens a position.
**Cover trigger:** Last three swing lows forming left shoulder, deeper head, right shoulder, with the close first rising above the neckline through the intervening highs.
**Indicators/defaults:** strength 2, lookback 150, shoulder_tolerance_pct 1, min_head_depth_pct 0.3.
**Bullish condition detected:** Bottoming structure completing with a neckline break, indicating selling is exhausted and demand is leading.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 151; daily-history bars 0.

### cover_keltner_channel_reversion
**Name:** Keltner Channel Reversion
**Category:** mean_reversion
**What it does:** Closes an existing short when price recovers back above the lower volatility channel. Exit signal only; never opens a position.
**Cover trigger:** Prior close strictly below its lower Keltner band, then latest close strictly above its own lower band.
**Indicators/defaults:** EMA centre ema_period 20, Wilder ATR atr_period 10, multiplier 2.
**Bullish condition detected:** Price re-entering the ATR-based channel from below, indicating the downside excursion is fading.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 21; daily-history bars 0.

### cover_macd_bullish_crossover
**Name:** MACD Bullish Crossover
**Category:** momentum
**What it does:** Closes an existing short when MACD momentum turns up through its signal line. Exit signal only; never opens a position.
**Cover trigger:** MACD line at or below the signal line on prior bar, then MACD strictly above the signal line on the latest completed bar.
**Indicators/defaults:** MACD fast_period 12, slow_period 26, signal_period 9.
**Bullish condition detected:** Momentum line reclaiming its smoothed average, indicating accelerating upside momentum.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 35; daily-history bars 0.

### cover_macd_zero_line_crossover
**Name:** MACD Zero Line Crossover
**Category:** momentum
**What it does:** Closes an existing short when MACD momentum turns net positive. Exit signal only; never opens a position.
**Cover trigger:** MACD line (fast EMA minus slow EMA) at or below zero on prior bar, then strictly above zero on the latest completed bar.
**Indicators/defaults:** MACD EMAs fast_period 12, slow_period 26; no signal line.
**Bullish condition detected:** Fast average reclaiming the slow average, indicating momentum has shifted from negative to positive.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 27; daily-history bars 0.

### cover_obv_breakout
**Name:** OBV Breakout
**Category:** volume
**What it does:** Closes an existing short when volume-weighted conviction breaks to new highs. Exit signal only; never opens a position.
**Cover trigger:** On-Balance Volume strictly above its highest value of the previous period bars, with edge logic so only a fresh break signals.
**Indicators/defaults:** OBV period 20; requires bars with volume.
**Bullish condition detected:** Cumulative volume flow breaking its recent ceiling, indicating sustained buying conviction behind the move.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 22; daily-history bars 0.

### cover_parabolic_sar_bullish_reversal
**Name:** Parabolic SAR Bullish Reversal
**Category:** trend_following
**What it does:** Closes an existing short when the trailing stop-and-reverse indicator flips to bullish. Exit signal only; never opens a position.
**Cover trigger:** SAR trend down on prior bar and up on the latest completed bar, i.e. a bar high penetrates the SAR.
**Indicators/defaults:** Parabolic SAR step 0.02, maximum 0.20.
**Bullish condition detected:** Price rising through the trailing SAR level, indicating the downtrend has reversed upward.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 3; daily-history bars 0.

### cover_previous_swing_high_breakout
**Name:** Previous Swing High Breakout
**Category:** price_action
**What it does:** Closes an existing short when price takes out the latest confirmed swing high. Exit signal only; never opens a position.
**Cover trigger:** Close at or below the latest confirmed fractal swing high on prior bar, then strictly above it on the latest bar.
**Indicators/defaults:** strength 2, lookback 50.
**Bullish condition detected:** Structural resistance breaking, indicating buyers absorbed the last pivot supply level.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 51; daily-history bars 0.

### cover_rsi_centerline_crossover
**Name:** RSI Centerline Crossover
**Category:** oscillator
**What it does:** Closes an existing short when RSI momentum shifts to the bullish half. Exit signal only; never opens a position.
**Cover trigger:** Wilder RSI at or below 50 on prior bar, then strictly above 50 on the latest completed bar.
**Indicators/defaults:** Wilder RSI rsi_period 14; fixed 50 centerline.
**Bullish condition detected:** Momentum crossing into above-average territory, indicating buyers have taken control from sellers.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 16; daily-history bars 0.

### cover_rsi_oversold_reversal
**Name:** RSI Oversold Reversal
**Category:** oscillator
**What it does:** Closes an existing short when selling pressure exhausts and RSI recovers from oversold. Exit signal only; never opens a position.
**Cover trigger:** Wilder RSI at or below the oversold level on prior bar, then strictly above it on the latest completed bar.
**Indicators/defaults:** Wilder RSI rsi_period 14, oversold 30.
**Bullish condition detected:** RSI climbing back out of oversold, indicating sellers are spent and buyers are stepping back in.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 16; daily-history bars 0.

### cover_sma_bullish_crossover
**Name:** SMA Bullish Crossover
**Category:** moving_average
**What it does:** Closes an existing short when the faster average crosses above the slower average. Exit signal only; never opens a position.
**Cover trigger:** Fast SMA at or below slow SMA on prior bar, then fast SMA strictly above slow SMA on the latest completed bar.
**Indicators/defaults:** SMA of closes, fast_period 50, slow_period 200.
**Bullish condition detected:** Medium-term average overtaking the long-term average, indicating a structural turn toward buying pressure.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 201; daily-history bars 0.

### cover_stochastic_bullish_crossover
**Name:** Stochastic Bullish Crossover
**Category:** oscillator
**What it does:** Closes an existing short when the stochastic momentum lines cross bullishly. Exit signal only; never opens a position.
**Cover trigger:** Full Stochastic %K at or below %D on prior bar, then %K strictly above %D on the latest completed bar, at any level.
**Indicators/defaults:** Full Stochastic k_period 14, k_smooth 3, d_period 3.
**Bullish condition detected:** Faster %K reclaiming slower %D, indicating near-term closing strength within the range.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 19; daily-history bars 0.

### cover_supertrend_bullish_flip
**Name:** Supertrend Bullish Flip
**Category:** trend_following
**What it does:** Closes an existing short when the Supertrend direction flips from down to up. Exit signal only; never opens a position.
**Cover trigger:** Prior completed bar direction down and latest completed bar direction up, using (high+low)/2 ± multiplier × Wilder ATR bands.
**Indicators/defaults:** Supertrend with atr_period 10, multiplier 3.
**Bullish condition detected:** Price action flipping above the trailing volatility band, indicating the downtrend has reversed to an uptrend.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 11; daily-history bars 0.

### cover_trendline_breakout
**Name:** Trendline Breakout
**Category:** price_action
**What it does:** Closes an existing short when price breaks above a falling resistance line. Exit signal only; never opens a position.
**Cover trigger:** Close first rising above the line through the last two falling confirmed swing highs, with no earlier close through that line.
**Indicators/defaults:** strength 2, lookback 100.
**Bullish condition detected:** Downtrend resistance giving way, indicating buyers pushing through the descending supply line.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 101; daily-history bars 0.

### cover_vwap_reclaim
**Name:** VWAP Reclaim
**Category:** vwap
**What it does:** Closes an existing short when price reclaims the session average price from below. Exit signal only; never opens a position.
**Cover trigger:** Prior-bar close strictly below session VWAP and latest completed-bar close strictly above it, same session and consecutive minutes.
**Indicators/defaults:** Session VWAP from shared market context; no tunable params; requires positive-volume bars.
**Bullish condition detected:** Price trading back above the day's average cost level, indicating intraday buyers have regained control.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 2; daily-history bars 0.

### cover_williams_r_bullish_reversal
**Name:** Williams %R Bullish Reversal
**Category:** oscillator
**What it does:** Closes an existing short when Williams %R climbs out of oversold. Exit signal only; never opens a position.
**Cover trigger:** %R at or below the oversold level on prior bar, then strictly above it on the latest completed bar.
**Indicators/defaults:** Williams %R period 14, oversold −80.
**Bullish condition detected:** Closes moving off the bottom of the recent high-low range, indicating sellers losing grip.
**Timeframe:** 1m trigger (requires 1m); warmup_bars 15; daily-history bars 0.

---

## 6. Protective Stops and Risk Management

Protective stops are managed by the **protection coordinator** (`mike_trade/risk/protection.py`) under a per-symbol policy (`stop_policy`) translated by `mike_trade/risk/stop_config.py`. A stop is armed **only after a confirmed entry fill** and is anchored to the confirmed average fill price. It covers the remaining confirmed quantity, and a stop fill can only close the position — it can never reverse it.

### 7.1 Supported stop types

Three configured stop policies are implemented, covering the five forms of stop you asked about:

| Configured policy | What it does | Fixed distance? | Trailing? |
| :--- | :--- | :--- | :--- |
| `fixed` | One stop at a fixed distance from the entry | Yes — `stop_percent` **or** `stop_amount` | No |
| `native_trailing` | A trailing stop managed ratchet-style; the trailing gap is fixed, the stop level moves with the price | No | Yes — `trail_percent` **or** `trail_amount` |
| `stepped_trailing` | A trailing stop that moves up (long) or down (short) in discrete steps, engine-managed | Initial fixed distance | Yes — stepped |

Each is configured per symbol in the `stop_policy` block. A symbol with no explicit policy uses the default native trailing stop of 1.5%. One mode applies for the life of a position — a fixed stop that later converts to a trailing stop is not supported (use `stepped_trailing`, which starts fixed and only ratchets after the activation gain).

### 7.2 Fixed stops (percentage or dollar)

- **What it does:** places one protective order at a fixed distance below the entry for a long, above the entry for a short.
- **Initial level:** anchored to the confirmed average fill price. `stop_percent` → a percentage distance; `stop_amount` → a fixed dollar distance. A long bought at $100.00 with `stop_percent: 5` gets a stop at **$95.00**. A short sold at $100.00 with the same policy gets a protective buy-stop at **$105.00** (above the market, because a rising price is what hurts a short).
- **When it moves:** it never moves. The stop level stays fixed for the life of the position.
- **Can it move backward?** No — by design the fixed stop simply doesn't move.
- **When it triggers:** when the market trades at or through the stop level. If prices gap through the stop, the position is closed at the gapped price — the stop trigger price is **not** a guaranteed execution price.
- **Long vs short:** long = sell-stop below the market; short = buy-stop above the market.
- **Broker-native or local?** In PAPER/LIVE the fixed stop is submitted to the broker as a stop order (default stop-market, GTC), so it survives engine restarts. In SHADOW and BACKTEST it is simulated locally at the same levels.

### 7.3 Trailing stops (native, percentage or dollar)

- **What it does:** maintains a protective stop that follows price in the favorable direction, keeping a constant distance.
- **Initial level:** the same distance from the entry as a fixed stop — e.g. `trail_percent: 5` on a $100 long starts the stop at $95.00.
- **When it moves:** whenever the price improves by more than the trail distance. A long bought at $100.00 with a 5% trail that rises to $110.00 has its stop raised to **$104.50** (5% below the best price — the code tracks a high-water mark and never lowers a long's stop below its previous level). Keep trailing $10 of the gain on every new high.
- **Can it move backward?** **No.** The stop ratchets in the favorable direction only. A long's stop only rises; a short's stop only falls — a short's protective buy-stop sits above the market at entry ($105 on a $100 short with 5% trail, for example) and ratchets **down** as the price falls, so gains are banked as the short works. On a rebound, the short's stop is never loosened upward.
- **When it triggers:** when price trades back to the current stop level.
- **Long vs short:** mirror images — long trails below, short trails above-then-down.
- **Broker-native or local?** `native_trailing` is broker-native in PAPER/LIVE (submitted as a broker trailing-stop order), so it survives engine stops and restarts. In SHADOW and BACKTEST it is simulated locally.
- **Limitation:** with `DAY` time-in-force, protection expires at the end of the day and leaves an overnight residual unless renewed; `GTC` persists per broker rules. A native trailing stop is only as continuous as the broker's order handling.

### 7.4 Stepped trailing stops (engine-managed)

- **What it does:** a trailing stop that moves in discrete whole steps rather than continuously. The engine computes candidate stops and re-anchors the protective order through cancel/replace operations — it is **not** a broker-native trailing order.
- **Configuration:** `initial_stop_percent`, `step_trigger_percent`, `step_percent`. All percentages are taken on the **entry price**, so they resolve to constant dollar amounts for the life of the position.
- **Example:** buy at $100.00 with `initial_stop_percent: 1.5`, `step_trigger_percent: 1.0`, `step_percent: 0.5`. The initial stop is at **$98.50** (1.5% below entry; steps are $0.50 each). The ratchet stays dormant until price gains the full activation amount ($1.00, or 1.0% of entry); until price reaches $101.00 the stop remains $98.50. At $101.00 the ratchet engages: the stop moves to **$99.50** (two whole $0.50 steps anchored at $101.00, still $1.50 behind). At $101.50 it moves to $100.00; at $102.00, to **$100.50**. The stop always stays $1.50 behind the highest whole-step anchor. If price then falls to $100.90, the stop never moves back down — it stays at $100.50.
- **Can it move backward?** No — same monotonic rule as all trailing stops. Every replacement is recorded in a trailing-stop event history.
- **Short mirror:** a short's stepped stop starts above the market and steps **down** as price falls.
- **Broker-native or local?** Engine-managed everywhere. The broker-facing capability check treats stepped trailing as locally managed (not a provider-native trailing order), and the PAPER/LIVE composition currently wires a single shared native-trailing percentage and **refuses every other policy at start-up** (`UNSUPPORTED_STOP_POLICY`). Stepped trailing is fully supported in SHADOW and BACKTEST; in PAPER/LIVE it requires wiring a policy the composition does not currently provide. Because it depends on the engine to re-anchor orders, a stepped stop is **not** continuous protection while the engine is offline — treat it as locally managed, not broker-native.

### 7.5 How stops are enforced (all types)

- **Post-fill start:** protection is placed only after a confirmed entry fill. There is no protective order before there is a confirmed position.
- **Remaining-quantity coverage:** the stop covers the confirmed remaining quantity; a sell that would oversell the position is rejected at every layer, so the stop can never promise more shares than are held.
- **Never away from the market:** trailing coverage ratchets only in the favorable direction (a long's stop never moves lower, a short's stop never moves higher). A stop fill closes the position; it can never open or add to a reverse position.
- **Stop-loss precedence:** protective stops are evaluated **before** any SELL/COVER strategy on every cycle. When both a stop and a strategy exit are valid, the protective stop wins (exit priority 0 vs strategy exits at priority 4). A stop is cancelled only to make way for the single arbitrated discretionary exit; an exit order that overlaps an effective stop is refused.
- **Emergency exits:** protective stops, SELL exits, COVER exits and emergency liquidation are never blocked by any market state, by paused entries, or by missing/stale market data.
- **Minimum holding periods:** apply to **ordinary strategy SELL/COVER exits only**. Protective stops, trailing stops, risk/safety exits, daily-loss exits, manual close, close-all, flatten, reconciliation and end-of-day exits bypass the minimum hold.
- **End-of-day liquidation:** optional and **disabled by default** (`end_of_day_close_enabled: false`). When enabled, positions are closed near the configured end-of-day time.
- **Position cooldowns:** after a sale (`SELL` or `COVER`) on a symbol, re-entry on that symbol is refused until the `post_sale_cooldown_seconds` deadline passes (`0` disables this).
- **Maximum daily entries:** `max_entry_cycles_per_session` caps filled entry cycles per symbol per session, and `max_filled_entry_cycles_per_symbol_per_session` is a second cap; refusals report `MAX_ENTRY_CYCLES_REACHED`. Entry counts reset at the next NY session open, not at midnight.
- **Consecutive-loss suspension:** a losing-streak tracker arms a lockout after `consecutive_loss_limit` consecutive losing cycles per symbol (or per symbol+buy-strategy, depending on `consecutive_loss_scope`). Lockouts reset at the next session (`consecutive_loss_reset: session`) or by operator reset. Refusals report `LOSING_STREAK_LOCKOUT`. A daily loss limit (`daily_loss_limit`, optional) also refuses new entries once the session's realized loss reaches it (`DAILY_LOSS_LIMIT_REACHED`) — it gates entries only, never exits.
- **Budget restrictions:** per-symbol budget and total application budget cap the managed open-position entry notional plus pending reservations. Entries must also fit the broker's reported buying power (checked by default). Optional `planned_loss_per_trade` sizing derives quantity from the planned loss against the protective distance — a wider stop means a smaller position. `max_open_positions` caps the number of managed open positions.
- **Per-stock investment limits and account-level restrictions:** per-symbol budget, `max_open_positions`, total budget and daily/entry-cycle caps are all enforced per account scope and are durable across restarts.
- **Short collateral and margin controls:** short-sale proceeds are **restricted collateral** (never purchasing power). A short requires `(short_margin_multiplier − 1) × notional` of own cash (default multiplier 1.5, so 0.5× notional); available cash for any other entry excludes the locked collateral. Borrow fees are an *estimated* assumption (`short_borrow_bps_per_day`, default 0) charged on cover; real borrow availability/fees are never presented as confirmed. No unrestricted margin or leverage is enabled.
- **Fail-closed behavior:** unknown stop-policy combinations refuse at start-up; invalid strategy parameters never produce a signal; new entries are blocked while any filled position is unprotected (`block_new_entries_when_unprotected`, with `stop_required`), or when broker state is stale/unreconciled.

> **Note for trader use:** a stop trigger price is not a guaranteed execution price or maximum loss; a stop-limit order may not fill; `DAY`-TIF protection expires overnight. Locally simulated stops (SHADOW/BACKTEST) fill at the stop level (or at the open when gaps through) — they cannot capture slippage beyond the configured model.

---

## 7. Market Status Monitor

The Market Status Monitor is a separate, deterministic risk subsystem. It watches three broad-market reference ETFs — **SPY** (S&P 500), **QQQ** (Nasdaq-100), **IWM** (Russell 2000) — on completed 1-minute bars, computes point-in-time risk metrics, and classifies the overall market into one of five states. It does not evaluate stocks or strategies; it produces an **authorization recommendation** that the portfolio layer applies to *new entries only*.

The monitor is enabled with `execution.market_status_enabled: true` (default **false** in SHADOW; supported in BACKTEST configuration). When disabled, it has no effect on trading. Reference symbols are fetched independently of the trading symbols and are never evaluated by trading strategies.

### 8.1 What it measures

All metrics are computed per reference symbol and combined:

| Metric | Definition | Defaults |
| :--- | :--- | :--- |
| **Broad-market drawdown** | How far below its recent peak each ETF trades: `(close − highest high of lookback) / highest high`, as a negative percentage. Composite = average across symbols; worst = the most negative. | Peak lookback **390 bars** (one standard NY session) |
| **Realized volatility** | Standard deviation of per-bar close-to-close returns over a rolling window, in basis points (1 bp = 0.01%). | Window **20 bars** |
| **Momentum** | Distance of the latest close from its 20-bar SMA, in basis points. | Window **20 bars** |
| **Breadth weakness** | Fraction of the three reference symbols under stress (drawdown ≤ −1.5% or momentum negative). | Stress levels as defined |
| **Trough/rebound** | During stress episodes: the worst composite drawdown since the episode began, how many bars ago it printed, and the rebound off it. | Tracked per episode |

It does **not** try to forecast crashes. It measures deterioration as it happens, with multi-bar confirmation to avoid whipsawing on single bad minutes.

### 8.2 The five market states

| State | Meaning | New BUY entries | New SHORT entries | Exits / stops |
| :--- | :--- | :--- | :--- | :--- |
| **NORMAL** | Healthy market; metrics in normal ranges, broad participation | **Allowed** | **Allowed** | Always allowed |
| **WARNING** | Elevated risk: material drawdown or elevated volatility with negative momentum; breadth deteriorating | **Restricted** (blocked by default policy) | Allowed | Always allowed |
| **CRASH** | Acute downside shock: severe drawdown or acute decline across indices | **Blocked** | Allowed (default) | Always allowed |
| **STABILIZING** | Post-crash base formation: no new lows, volatility contracting | **Blocked** | **Restricted** (blocked by default) | Always allowed |
| **RECOVERY** | Meaningful rebound off the trough with positive momentum and broadening participation | **Allowed** (qualified) | **Restricted** (blocked by default) | Always allowed |

**The core invariant:** in every state — including CRASH, missing data and stale feeds — `SELL` exits, `COVER` exits, protective stops and emergency liquidation are **never** blocked. This is enforced by runtime assertions and automated tests. Market-state restrictions apply only to opening new positions.

### 8.3 Transition rules and confirmation

States move progressively NORMAL → WARNING → CRASH and back CRASH → STABILIZING → RECOVERY → NORMAL, with shortcuts for fast deterioration or fast resolution. Every transition requires confirmation to prevent oscillation:

| Transition | Requirement |
| :--- | :--- |
| NORMAL → WARNING | 2 consecutive bars meeting warning conditions (composite drawdown below **−1.5%**, or volatility above **25 bps** with negative momentum and ≥ 2 of 3 symbols deteriorating) |
| NORMAL → CRASH | **Flash crash directly**: benchmark drop ≤ **−5.0%**, or composite ≤ −3.5% with broad agreement — single bar, no confirmation wait |
| WARNING → CRASH | 2 consecutive bars (or 1 bar on an acute flash move) with drawdown ≤ **−3.5%** across ≥ 2 of 3 symbols, or volatility ≥ **50 bps** |
| WARNING → NORMAL | 3 consecutive bars with drawdown recovered above **−0.8%** (a hysteresis gap above the −1.5% warning level, so a borderline market does not ping-pong) |
| CRASH → STABILIZING | Minimum **5 bars** in crash, **≥ 5 bars without new lows**, and a rebound of **≥ +0.5%** off the trough held for **3 bars** |
| STABILIZING → RECOVERY | Minimum **5 bars** stabilizing, composite drawdown back above **−1.2%**, positive momentum, held for **3 bars** |
| RECOVERY → NORMAL | Drawdown above **−0.5%**, realized volatility **below 25 bps**, positive momentum, held for **3 bars** |
| Any → CRASH (relapse) | Immediate (1 bar) if a stabilization/rebound breaks: new low, rebound broken, or drawdown collapsing back below the crash threshold. RECOVERY → WARNING also exists for a stalled recovery |

A state diagram:

```
NORMAL ──(2 bars warning)───────────▶ WARNING ──(2 bars, or 1 flash)──▶ CRASH
   ▲                                     │  ▲                              │
   │  (3 bars cleared,                 (3 bars,                    (≥5 bars in crash,
   │   dd > −0.8%)                       dd > −0.8%)                 ≥5 bars no new lows,
   │                                     │                            rebound ≥ +0.5% ×3 bars)
RECOVERY ▲ ──────────────────────────────┘                              ▼
   ▲     │ (stalled recovery → WARNING)                         STABILIZING
   │     └─(relapse → CRASH, 1 bar)                                    │
   │  (≥5 bars stabilizing, dd ≥ −1.2%,                               (new low / rebound
   │   positive momentum ×3 bars)                                      broken → CRASH)
   └───────────────────────────────────────────────────────────────────┘
```

Direct shortcuts exist for: `WARNING → NORMAL` (cleared), `NORMAL → CRASH` (flash), `STABILIZING → CRASH` (secondary breakdown), `RECOVERY → CRASH` (relapse), `STABILIZING → NORMAL` (direct normalization) and `RECOVERY → WARNING` (stalled recovery).

### 8.4 Configurable parameters

All thresholds are configurable: drawdown levels (`warning_percent` −1.5%, `crash_percent` −3.5%, `flash_crash_percent` −5.0%, cleared −0.8%, recovery −1.2%, normal −0.5%), volatility (`warning_bps` 25, `crash_bps` 50, stabilizing decay ≤ 80% of crash peak), breadth ratios (0.66), hysteresis bar counts (2/3/5/3/3), lookback windows (390/20/20), staleness tolerance (300 s), and the reference symbol set. The defaults are conservative engineering choices, not validated market-prediction thresholds.

### 8.5 Data failure behavior

- If fewer than **2 of 3** reference symbols have data, or data is stale (older than 300 s), the monitor fails closed: **new BUY and SHORT entries are both blocked** (`MARKET_DATA_UNAVAILABLE` / `MARKET_DATA_STALE`). Exits and stops are never affected.
- The monitor's state survives restarts (persisted with the engine health row), so a restart during CRASH resumes in CRASH. A corrupted/unreadable saved state also fails closed until a full lookback of reference minutes has been observed.
- The state machine advances at most once per completed reference minute, and repeated evaluation calls do not change it — the state sequence depends only on the data fed in, which is what makes SHADOW and BACKTEST behave identically.

### 8.6 Market detection vs. trade authorization

The monitor only *recommends*. The `MarketStatusEntryGate` applies the recommendation **per direction and additively**: it is one more layer on top of the existing risk, budget, buying-power, margin and eligibility gates. A BUY or SHORT candidate in a restricted direction is dropped **before** arbitration, so a restricted BUY can never suppress an authorized SHORT (or vice versa). A SHORT is never authorized by CRASH automatically — a strategy must still produce a valid SHORT signal and pass every other restriction. SELL, COVER, protective stops and liquidation bypass the gate entirely.

---

## 8. Crash Protection and Emergency Behavior

When the broad market begins deteriorating, the monitor walks the states from the previous section and the entry gates respond immediately. This section summarizes what actually happens to trading during a deterioration, and what a trader can count on.

### 9.1 Warning detection

WARNING requires 2 consecutive completed reference minutes with a material drawdown (composite **≤ −1.5%** of the rolling peak), or realized volatility above 25 bps combined with negative momentum and deteriorating breadth (≥ 2 of the 3 ETFs). The default policy then:

- **restricts new BUY entries** (blocked),
- **leaves SHORT entries permitted**,
- leaves SELL/COVER exits and protective stops fully enabled.

### 9.2 Crash declaration

CRASH is declared after 2 consecutive minutes at composite drawdown **≤ −3.5%** with broad agreement (≥ 2 of 3 symbols), or volatility ≥ 50 bps — or immediately on an acute flash move (a benchmark ≤ **−5.0%**, or ≤ −3.5% with consensus). A relapse from STABILIZING or RECOVERY returns to CRASH on a single bar (new low or rebound broken).

### 9.3 What CRASH does to trading

| Action | Under CRASH |
| :--- | :--- |
| New long (BUY) entries | **Blocked** — candidates are dropped before arbitration |
| New short (SHORT) entries | **Permitted by the default policy** — but only with a genuine SHORT signal that still passes every other gate (budget, margin, borrow, eligibility); CRASH alone never authorizes a short |
| Existing long positions | Remain open; ordinary SELL strategy exits and protective stops continue to work normally |
| Existing short positions | Remain open; ordinary COVER exits and protective stops continue to work normally |
| Emergency liquidation | **Supported and always permitted** (never blocked by market state) |
| Ordinary strategy exits | Enabled — SELL/COVER strategies still evaluate and exit normally; the market monitor never blocks them |
| Protective stops | Enabled — stops are independent of market state and never blocked |

The default policy can be adjusted: `warning_restricts_buy`, `crash_allows_short`, `stabilizing_blocks_buy`, `stabilizing_restricts_short`, `recovery_allows_buy`, `recovery_restricts_short` are all configurable booleans. The one thing that is **not** configurable is the invariant that exits, stops and liquidation are never blocked.

### 9.4 If the monitor has no data

Missing or stale reference data (fewer than 2 of 3 symbols, or bars older than 300 s) fails closed: both new BUY and new SHORT entries are blocked, while exits and stops continue. The operator can also block entries manually at any time — `pause` (all symbols), `pause-symbol`, or the sticky `entries-disable` kill switch — all of which leave protection and exits running, and all of which are risk-reducing (no confirmation required even on LIVE accounts).

### 9.5 Emergency exit tools

Beyond the automatic gate, the operator has explicit controls: `close` (one symbol), `close-all` (all managed positions) and `flatten` (flatten the book *including* protective orders — the strongest). These go through the engine control channel and require the engine to be online; `flatten` is the tool for "get the whole book flat now." Stopping or pausing the engine never closes positions and never cancels broker-held protective stops.

---

## 9. Market Stabilization and Recovery

Getting out of CRASH is deliberately slower than getting in. The sequence CRASH → STABILIZING → RECOVERY → NORMAL requires the market to hold for several minutes at each step, and each improvement must survive confirmation bars.

### 10.1 Stabilization (CRASH → STABILIZING)

Stabilization means the free fall has stopped and a base is forming:

- the market must have been in CRASH for at least **5 bars**,
- **5 or more bars** must pass without a new composite drawdown low,
- and the composite must rebound **≥ 0.5%** off its trough, held for **3 bars**.
- Volatility must also be decaying (to ≤ 80% of its crash peak).

During STABILIZING the default policy **blocks new BUY and new SHORT entries** — both directions are deliberately kept out during the observation window. Exits and stops remain fully enabled.

### 10.2 Recovery (STABILIZING → RECOVERY)

Recovery is confirmed when the market has spent at least **5 bars** stabilizing and simultaneously:

- composite drawdown has recovered to **≥ −1.2%** (i.e., within 1.2% of the recent peak),
- momentum (close vs. 20-bar average) is **positive**,
- and those conditions hold for **3 consecutive bars**.
- Breadth participation (≥ 2 of 3 ETFs agreeing) is also required by the default breadth ratio.

Recovery re-opens the door to **qualified BUY entries** (new longs allowed again) while new SHORT entries remain restricted by the default policy — the design assumes the rebound may fail, so it does not let a trader pile onto the short side into a rising market.

### 10.3 Normal (RECOVERY → NORMAL)

Full normalization requires drawdown **≥ −0.5%**, realized volatility **below 25 bps**, positive momentum, all held for **3 bars**. Only then do both BUY and SHORT entries become fully allowed.

### 10.4 Hysteresis and failed recoveries

The confirmation-bar requirements *are* the hysteresis mechanism — the system refuses to flip between states on single bars. A recovery can fail at any point:

- **STABILIZING → CRASH**: a new low or a broken rebound returns to CRASH on **1 bar**.
- **RECOVERY → CRASH**: an immediate relapse — drawdown collapsing back below the crash threshold — is also a **1-bar** transition.
- **RECOVERY → WARNING**: a stalled recovery that starts deteriorating again drops back to WARNING rather than CRASH.

### 10.5 Permitted actions across states (default policy)

| State | New BUY | New SHORT | SELL / COVER exits | Protective stops | Emergency liquidation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| NORMAL | ✅ | ✅ | ✅ | ✅ | ✅ |
| WARNING | ❌ | ✅ | ✅ | ✅ | ✅ |
| CRASH | ❌ | ✅ | ✅ | ✅ | ✅ |
| STABILIZING | ❌ | ❌ | ✅ | ✅ | ✅ |
| RECOVERY | ✅ | ❌ | ✅ | ✅ | ✅ |
| Data missing/stale | ❌ | ❌ | ✅ | ✅ | ✅ |

These are the defaults; each entry restriction is a configurable policy flag. Detection is separate from authorization: the monitor classifies, the gate applies the classification only to entries, and all risk-reducing activity is unconditional.

---

## 10. Position Lifecycle

Each symbol in the engine follows one position cycle at a time: a single intentional entry followed by one complete exit. The exact state machine (`CycleState` in `mike_trade/portfolio/cycles.py`) is:

```
DISABLED → WATCHING_BUY → ENTRY_RESERVED → BUY_SUBMITTING → BUY_PENDING → PARTIALLY_OWNED
                                                                                         │
  → PROTECTIVE_PENDING → WATCHING_SELL → SELL_SUBMITTING → SELL_PENDING → CLOSING → CLOSED
```

with supporting states `RECONCILING`, `PAUSED`, `ERROR`, `UNMANAGED` and `CLOSED`. The conceptual flow for both directions:

```
FLAT → BUY_PENDING   → LONG  → SELL_PENDING  → FLAT     (BUY, SELL)
FLAT → SHORT_PENDING → SHORT → COVER_PENDING → FLAT     (SHORT, COVER)
```

**One position per symbol.** While a symbol is flat, its BUY strategies and (when authorized) SHORT strategies compete for the single entry slot. While a position is held, only the exit role of that position's direction acts: SELL closes a long, COVER closes a short. Second entries, scaling, and reversal are structurally refused.

**Eligibility arming.** BUY strategies only fire when the engine has armed the symbol with the `WATCHING_BUY` eligibility state; SHORT strategies only fire when armed with `WATCHING_SHORT`. SELL and COVER strategies carry no arm state — they are simply not evaluated while no matching position is held. This is how the engine guarantees a BUY signal cannot fire on top of an existing long.

**Entry arbitration.** When several BUY/SHORT strategies signal on one tick, `arbitrate_entry_decisions` selects at most one entry, of one direction. The tie-break is deterministic: earliest completed-bar timestamp, then LONG before SHORT, then strategy id, then signal id — independent of configuration or arrival order. A held position or a still-working entry order rejects every candidate.

**Exit arbitration.** When an exit is warranted, candidates are ranked by `EXIT_PRIORITY`: protective stop (0), manual close (1), hard-risk exit (2), forced liquidation (3), then ordinary strategy SELL/COVER (4). The first valid candidate wins — there is no voting and no confidence scoring. The selected exit closes **100% of the confirmed remaining quantity**; partial exits do not exist. The minimum holding period applies only to ordinary strategy exits (priority 4).

**Signal vs. order vs. fill.** These are three different things:

1. **Signal** — a strategy reports the pattern it watches for occurred on a completed bar. Advisory only; no quantity.
2. **Order submission** — after arbitration and all gates (market state, entry cutoff, budget, buying power, margin, protection eligibility), the engine creates an order intent with a stable client id and submits it to the broker (or simulator).
3. **Fill** — the broker confirms execution. Fills are ingested exactly once (idempotent); the confirmed average fill price becomes the anchor for the protective stop, which is armed only after this confirmation.

**Pending orders and fills.** While an entry order is pending, no second entry is allowed. Partial fills update the cycle (PARTIALLY_OWNED) and protective coverage is reconciled to the remaining quantity; the system never automatically buys the missing remainder.

**Protective stop activation.** After a positive confirmed fill, protection is placed (per the symbol's stop policy) and the cycle moves to PROTECTIVE_PENDING → WATCHING_SELL. The stop is evaluated before any SELL strategy on every cycle and is cancelled only to make way for the single arbitrated discretionary exit.

**Position closure and cooldown.** After a full exit fill (strategy, stop, manual or safety), the cycle reaches CLOSED, budget is released, and the symbol becomes eligible for re-entry — subject to the post-sale cooldown, entry-cycle caps, losing-streak lockouts, daily loss limits, and the market-state gate. A symbol can re-enter as soon as all gates pass.

---

## 11. Shadow, Backtest, Paper and Live

Four execution modes exist with strictly separated credentials, ledgers and configuration.

| Mode | Purpose | Real money? | Data source | Order execution | Stop support | Important limitations |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **SHADOW** | Forward simulation of the full strategy lifecycle on live data, in a simulated account | No | Live market feed (same feed as PAPER/LIVE) | Simulated broker — fills, fees and slippage are modeled, labeled `ESTIMATED` | All three policies simulated locally (fixed, native-trailing-equivalent ratchet, stepped) | Simulated fills are assumptions, not broker fills; needs a persistent scenario; sized positions not real |
| **BACKTEST** | Deterministic historical replay of stored bars | No | Immutable historical bar snapshots (Parquet) | Replay: a signal on a completed bar executes at the next bar's open with configured adverse slippage; stops fill intrabar at the stop or at the open if gapped | All three policies, same translation as SHADOW | Bar-only (no bid/ask, no partial fills beyond the participation cap); results are descriptive baselines, never profitability evidence |
| **PAPER** | Broker paper trading | No | Live feed | Real order flow to the Alpaca paper endpoint | Broker-native stops are supported as the shared configuration; other policies refused at composition | **No automatic strategy evaluation** — the PAPER runtime does not run strategies today; orders would be manual/operator-driven. Short flows never verified |
| **LIVE** | Real broker trading | Yes | Live feed | Real order flow to Alpaca live, gated by extensive preflight and confirmation | Broker-native stops supported under the same restriction | **No automatic strategy evaluation**; heavily gated (preflight, explicit enablement, live confirmation); short selling is permanently disabled for LIVE by configuration |

**Cross-mode isolation is enforced**: paper credentials never touch live code paths and vice versa; each mode/account uses its own database and ledger; a restore or migration across modes is refused.

### 12.1 SHADOW in detail

- A **simulated account** with its own starting cash, per-named-scenario isolation (multiple scenarios can run concurrently on one shared feed), and a persistent ledger.
- **Simulated positions, fills and P&L**: fills are produced by a fill simulator with documented assumptions (including adverse slippage); fees are estimated; P&L is computed and reported per cycle and per strategy. The account is event-sourced — a restart replays the same events and reproduces the same balance, so state survives restarts.
- **Independent of the Alpaca paper account** — shadow never touches broker paper credentials or state.

### 12.2 BACKTEST in detail

- **Historical candles** from immutable, content-addressed Parquet snapshots (IEX feed); one full year × 100-symbol universe available.
- **Deterministic replay**: the same inputs reproduce the same ledger fill-for-fill (verified in practice); runs carry versioned lifecycle semantics in their configuration hash.
- **Strategy combinations**: any registered BUY, SELL, SHORT and COVER strategies per symbol, each with independent parameters.
- **Protective stops**: fixed, native-trailing and stepped, translated by the same code as SHADOW; a stop never moves down; entry cutoff, one-entry-cycle-per-session and minimum-hold rules mirror the live engine.
- **Transaction costs**: configurable slippage (default 5 bps adverse), fees per order/share, borrow-cost assumption for shorts.
- **Historical market-state evaluation**: the backtest runner can feed SPY/QQQ/IWM reference candles to the same Market Status Monitor and records refused entries (`ENTRY_REFUSED`, gate MARKET_STATUS).
- **Reproducibility limitations**: the monitor starts empty at the window start (reference candles before the window are not replayed), so early-window entries are blocked until two reference symbols have data; the research batch interface does not yet expose the market-status switch; sizing differs from SHADOW (research uses `min($3,000 symbol budget, $30 planned loss ÷ initial stop distance)`, SHADOW sizes by symbol budget alone).

### 12.3 PAPER and LIVE — read carefully

The **four-role lifecycle (BUY/SELL/SHORT/COVER) is implemented in SHADOW and BACKTEST**. As of this release, the PAPER/LIVE runtime composition is built **without automatic strategy evaluation** (`lifecycle=None`): strategies are not run, so no autonomous signals become paper or live orders. The order, position, reconciliation and protection layers are direction-aware and complete, but operator-driven order flow is the only path today. PAPER short order shapes have never been verified end-to-end, and LIVE short selling is refused by configuration (`short_selling_enabled` cannot be set for `account_mode: live`).

**Bottom line:** the environment for testing Michelle's strategies is SHADOW (live feed, forward) and BACKTEST (history, replay). PAPER and LIVE are not yet autonomous strategy environments.

---

## 12. How to Combine Strategies

Michelle can configure per symbol any combination of sides, and the engine arbitrates for her. The practical combinations:

| Combination | What the system does |
| :--- | :--- |
| **BUY + SELL** | BUY strategies open longs; SELL strategies close them. Classic pair. |
| **BUY + protective stop only** | BUY strategies open longs; the stop is the *only* exit (a SELL collection may be empty). The stop is evaluated independently and protects the position. |
| **SHORT + COVER** | SHORT strategies open shorts; COVER strategies close them. Requires `short_selling_enabled: true`; SHADOW/BACKTEST only. |
| **SHORT + protective stop only** | SHORT strategies open shorts; the protective stop (above the market) is the only exit. |
| **Multiple BUY strategies** | All evaluate every bar; arbitration selects at most one entry per bar per symbol. They compete, they don't stack — one signal wins. |
| **Multiple SELL strategies** | All evaluate every bar while a long is held; arbitration selects the single exit that wins the priority/tie-break ordering. First-valid wins; no voting. A protective stop outranks all of them. |
| **Multiple SHORT / multiple COVER** | Same arbitration semantics as their long counterparts; only one direction enters on a bar, and only one exit closes. |

**Arbitration in one sentence:** for entries, at most one strategy of one direction acts per symbol per bar (earliest signal wins, LONG before SHORT); for exits, the highest-priority valid exit acts (protective stop > manual > hard-risk > forced > strategy), and it closes the full remaining position.

**A few practical notes:**

- **SELL and COVER are optional.** A symbol with only BUY strategies is still fully risk-managed by its stop policy — this is the "BUY + stop only" pattern, and it is a designed, supported configuration.
- **Mixed-direction symbols are allowed but constrained.** BUY and SHORT strategies may coexist on one symbol; at most one direction's signal is acted on per bar, and there is never automatic reversal.
- **More strategies ≠ more profit.** Adding indicators or signals to a symbol does not improve the quality of the set; it only changes which signal wins more often. Strategy collections should be designed with a clear idea of what *kind* of behavior each member adds (trend vs. mean-reversion vs. breakout), because arbitration will otherwise spend its tie-breaks arbitrarily.
- **Choosing a combination to test historically.** Use BACKTEST with explicit `buy_strategies` / `sell_strategies` (and optionally `short_strategies` / `cover_strategies` and a `protective_stop`) per symbol; pin the data snapshot, parameters, capital and cost assumptions in the job manifest; then compare runs across the same window. Because runs carry their configuration in the artifact hash, an identical rerun is verifiable.
- **Stops are not a strategy substitute.** A tight stop and a SELL strategy can both exist; the stop simply enforces the worst case while strategies attempt the best-case exit.

---

## 13. Testing Michelle's Custom Trading Methods

Mike_Trade can add custom strategies as Python classes that follow the same strategy contract as the 192 registered ones: a pure function of (completed bars + parameters + persisted state) that returns a signal or no-signal, with no order submission, no broker access, and no wall-clock dependence. Before that step, the design work happens in trading terms.

### 14.1 What to provide for each method

For a method to be implemented faithfully, it helps to write down, exactly:

1. **Exact entry conditions** — the precise market behavior that must occur (which indicator, which crossing, which bar; e.g., "close crosses above the 20-bar high on a completed minute bar").
2. **Exact exit conditions** — same precision for closing the position.
3. **Indicators and parameters** — every indicator, its window, and every tunable parameter with a default and a sane range.
4. **Preferred stocks** — universe (liquid US equities/ETFs), any filters (price, volume, ADX, sector).
5. **Timeframe** — which bar interval triggers the rule (minute, daily, weekly) and how much history it needs (warm-up bars; daily-history strategies need enough completed daily bars).
6. **Market conditions** — whether the method should only trade in certain market states (e.g., long-only in NORMAL/RECOVERY), to be enforced via the market-status gate or strategy-level filters.
7. **Stop rules** — fixed, native-trailing or stepped, with parameters.
8. **Examples of successful trades** — 5–10 concrete charts/date ranges where the method would have worked.
9. **Examples of unsuccessful trades** — equally important: where it would have lost or whipsawed. This is what makes the difference between a rule and a narrative.

### 14.2 The testing process

1. **Preserve the original trading rules.** Write the method down in your own words first; the implementation must match that description, not a refined version of it.
2. **Translate them into deterministic conditions.** Every condition must become a mechanical rule on completed bars — no discretion, no "if it looks strong." Ambiguity here (e.g., "a significant retracement") blocks implementation.
3. **Implement the strategies** as new strategy classes registered alongside the existing library.
4. **Backtest using historical data** on the frozen 100-symbol universe or a chosen subset, with explicit parameters and cost assumptions.
5. **Compare against existing strategies** on identical windows and symbols, so the comparison is apples-to-apples.
6. **Test different market environments** — separate the bull, bear, and choppy periods in the window; a method that only wins in one regime is a regime-dependent method.
7. **Evaluate realistic trading costs** — slippage (the default model assumes 5 bps adverse), fees, and — for shorts — the estimated borrow assumption.
8. **Use data not involved in developing the method.** If the method was tuned on a period, test it on a later, untouched period. The software's snapshots are immutable and dated, which makes honest out-of-sample splits possible; note that a universe selected today still carries survivorship/selection bias for earlier dates.
9. **Measure win rate, profit factor and drawdown** — the reporting and trade-journal tooling reports per-strategy P&L, exit reasons, MFE/MAE and excursions; use those to compute win rate, profit factor and max drawdown, not just total return.
10. **Decide whether results justify further testing.** Small-sample wins (a handful of trades) prove nothing; look at trade counts as carefully as returns.

**Historical performance does not guarantee future profitability.** Markets change, and results from any backtest—including this software's—are descriptive baselines, not evidence of an edge. That caution is also the reason the platform makes no profitability claims about its own strategies.

A note on judgment: none of this implies your discretionary trading judgment is wrong. The purpose of the exercise is to make a discretionary method *testable*: if the rules can be stated, they can be measured; if they can be measured, they can be improved — or candidly retired.

---

## 14. Dashboard

The dashboard is a separate, **read-only** process (FastAPI serving a compiled React app, default port 8765). It reads the same SQLite database through read-only projections and a heartbeat row, and — critically — it **cannot** place orders, cancel orders, mutate configuration, or drive the engine. All order and configuration actions go through the engine's control channel (`make close`, `make flatten`, `make pause`, etc.). It stays up when the engine is down and independently reports engine `OFFLINE` from heartbeat age (stale at 15 s, offline at 60 s by default).

Screens and fields (verified against the API and frontend, `dashboard/frontend/src`):

| Panel | Shows |
| :--- | :--- |
| **Account KPIs** | Cash, equity, buying power, portfolio value, per selected account/scenario scope |
| **Watchlist** | The 1–10 configured symbols with per-symbol status |
| **Symbol details** | Per-symbol signals, strategy evaluations, positions (external broker holdings labeled `UNMANAGED`), orders, protective/trailing stop state |
| **Account detail** | Entry limits, lockouts, cooldowns, P&L and balance/buying power, health and reconciliation (drifts, `UNMANAGED_HOLDING` items) |
| **Trades (journal)** | Open, partially closed and completed cycles with pagination/filters (symbol, date, entry strategy, exit, outcome, direction); selecting a trade shows an SVG chart of completed 1-minute bars, fill markers, session VWAP, the holding interval, and recorded stop changes |
| **Research runs** | Backtest run selection and per-run trade inspection from immutable artifacts |
| **Health** | Engine/broker/feed/scheduler/persistence status via `make health` and `make dashboard-health` |

**Trade attribution:** the trade journal attributes each entry to the exact accepted `signal_id` → strategy instance, and labels exits by cause — strategy SELL/COVER, protective stop, manual, safety, or end-of-session. For older research artifacts where signal/version data is absent, attribution fields show `UNKNOWN`.

**What the dashboard does not currently show:**

- **Market-status state.** The NORMAL/WARNING/CRASH/STABILIZING/RECOVERY classification from the Market Status Monitor is **not exposed in the dashboard UI**; it lives in engine state and logs, and must be inspected via the CLI/state tooling. (Planned gap, not a bug.)
- **Per-trade strategy/exit-reason on the main KPI screens.** Attribution is present in the trade journal detail, but the account KPI and watchlist screens do not surface "which strategy opened this trade" or "why it exited" inline.

Values that need a live authority (engine, broker link, feed) are explicitly marked stale with a reason when that authority is offline; fields with no persisted source are null rather than guessed.

---

## 15. Known Limitations

Checked against the current source on `feature/strategy-library-192-final`:

1. **PAPER/LIVE autonomous strategy execution is not implemented.** Only SHADOW and BACKTEST evaluate strategies: the PAPER/LIVE runtime composition builds no strategy lifecycle (`lifecycle=None`). Until that changes, PAPER/LIVE do not run Michelle's (or any) strategies automatically. LIVE short selling is additionally refused by configuration.
2. **PAPER/LIVE short flows are unverified.** No paper or live short order flow has been tested end-to-end; the order service requires a recorded capability verification before a paper short could even be attempted.
3. **Historical backtests using the market monitor need monitor warm-up.** The backtest monitor starts empty at the window start (reference candles before the window are not replayed), so entries are blocked until two of the three reference symbols have data; the `research-batch` interface does not yet expose the market-status switch at all.
4. **SHADOW and BACKTEST are not guaranteed identical.** SHADOW consumes the live stream with a bounded start-up warm-up (default 60 bars) and live-session references; BACKTEST replays stored bars deterministically. Strategies whose lookback exceeds the SHADOW warm-up return `INSUFFICIENT_DATA` until enough live bars accrue, and recursive-indicator state can therefore differ between the two for the same period. Research sizing also differs (planned-loss-based vs. symbol-budget-based).
5. **M1 signal processing is scheduled, not instantaneous.** Evaluations run on closed-bar triggers through a bounded worker pool (5-second scheduler cadence by default); a signal is processed shortly after the bar closes, not at the exact close instant.
6. **Daily-history availability depends on provider support.** D1/W1 candle support is provider-dependent; daily history fails closed on missing or late bars, uses unverified provider timestamp assumptions, and is built on IEX volume.
7. **Daily-data strategies need deep warm-up.** Daily-history strategies (e.g., the volatility-contraction pattern needing 252 daily bars, cup-and-handle needing 241) return no signal until the required daily history is present.
8. **Some implementations use engineering defaults.** Parameters are engineering defaults chosen from published definitions — the market-status thresholds explicitly state they are "not optimized or validated" — and at least one candidate (Momentum Ignition) was deferred for lack of a legitimate mechanical rule. Defaults are a starting point for testing, not a claim of efficacy.
9. **Profitability has not been established.** No strategy, combination, or backtest result in this repository is presented as profitable or as an edge. All research outputs are explicitly descriptive baselines.
10. **Dashboard trade attribution is limited to the journal.** The trade journal attributes entries and exit causes; the main KPI/watchlist screens do not, and the market-status state is not displayed in the dashboard UI.
11. **Stop semantics caveats.** A stop trigger price is not a guaranteed execution price; stop-limit orders may not fill; `DAY`-TIF protection expires overnight unless renewed; stepped trailing is locally managed and does not provide broker-native continuous protection while the engine is offline.
