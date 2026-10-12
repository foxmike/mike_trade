# `vwap_bounce_break_michelle_buy`

## Identity and status

- Original document: `VWAP_Bounce_Break_Formatted.docx`
- Original strategy name: **VWAP BOUNCE AND BREAK ORIGINAL STRATEGY**
- Proposed Mike_Trade identifier: `vwap_bounce_break_michelle_buy`
- Role: BUY (independent long-entry signal)
- Source attribution: Michelle; the original document above is authoritative
- Purpose: buy an immediate VWAP recross after a qualified below-VWAP reversal cycle
- Status: **BLOCKED — no Python class or registry entry exists**

## Formal contract

### Required market data

- Completed RAW 1-minute OHLCV for the current session, sufficient to calculate EMA(9), EMA(20),
  each bottoming pattern, and three-minute comparisons.
- Ordered real-time trade ticks and a contemporaneous bid/ask snapshot for the immediate VWAP cross.
- A causal per-bar/session VWAP series anchored at 09:30 ET, plus VWAP at the cycle low and fill.
- Previous Day Close, Yesterday's High, Yesterday's Low, Premarket High, and Premarket Low. The
  document marks these required but does not use them in any predicate.
- Exchange session identity and America/New_York time.

### Required indicators

- VWAP anchored at 09:30 ET.
- EMA(9) and EMA(20) of completed 1-minute closes.
- Dip depth: `VWAP_at_cycle_low - lowest_low_since_price_first_lost_VWAP`.
- Hammer/wick rejection, three-candle base consolidation, and bullish engulfing detectors.
- EMA recovery comparisons: `EMA9[k] > EMA9[k-3]` and the EMA9/EMA20 gap at `k` smaller than at
  `k-3`.

### Timeframes and lookback

- Setup/recovery: completed 1-minute candles.
- Trigger: intrabar trade tick.
- EMA20 requires at least 20 completed closes under a conventional SMA seed, but the source does
  not state the EMA seed or initialization convention.
- Recovery comparisons require valid EMA readings at `k` and `k-3`.
- Bottoming alternatives require one candle, two candles, or three consecutive candles.
- Cycle history begins when price first loses VWAP and continues until entry, rejection, or an
  undefined reset event.

### Explicit setup and entry rules (A — stated in the document)

1. Trade window is 09:30–11:30 ET; 09:30–10:30 ET is the “highest priority” window.
2. Symbol is one of `AAPL, PLTR, MSFT, GOOGL, NVDA, TSLA, AMD, SPCX, ORCL, INTC, DELL`.
3. Initially price drops below VWAP, EMA20, and EMA9; EMA9 is below EMA20; EMA20 is below VWAP.
4. Track the lowest low since price first lost VWAP. Dip must never exceed fixed `MAX_DIP = $2.00`.
5. While below VWAP, form at least one:
   - Hammer/wick rejection: lower wick length `>= 2 * body length` and close in the top 40% of
     candle range.
   - Base consolidation: three consecutive candles with combined high-to-low range `<= 0.15% of price`.
   - Bullish engulfing: a green candle body completely engulfs the prior red candle body.
6. Price moves back up toward VWAP.
7. On latest completed minute `k`, `EMA9[k] > EMA9[k-3]`, and the EMA9/EMA20 spread is smaller
   than it was at `k-3`.
8. Buy at market the instant trade price crosses above VWAP (`intrabar trade price > VWAP`).
9. Reject if the dip exceeded MAX_DIP at any point in the cycle.
10. Reject if actual entry/fill price is more than 0.10% above VWAP at fill.

Signal confirmation is the selected immediate-cross mode; the document lists no alternative mode.
The qualifying bottom and both recovery tests must precede the tick crossing.

### Parameters and constraints

The document fixes these values:

| Value | Source value | Constraint |
|---|---:|---|
| Allowed window | 09:30–11:30 ET | Endpoint inclusion missing |
| Priority window | 09:30–10:30 ET | Priority behavior missing |
| MAX_DIP | $2.00 | Dip must never exceed; equality allowed |
| Hammer wick/body | 2 × | Inclusive |
| Hammer close zone | Top 40% of range | Zero-range treatment missing |
| Base length | 3 candles | Consecutive completed M1 |
| Base combined range | 0.15% of price | Denominator missing; inclusive |
| EMA periods | 9 and 20 | Seed convention missing |
| EMA comparison lag | 3 completed candles | `k` versus `k-3` |
| Maximum fill extension | 0.10% above VWAP | Strict `>` rejection |

### State, rearming, and timing

- Required per-session state: cycle start, first loss of VWAP, cycle-low time/price/VWAP, maximum
  dip, initial hierarchy qualification, bottom-pattern qualification, recovery qualification,
  previous trade price, and consumed/rejected state.
- The source does not say when an invalid or completed cycle rearms, whether multiple entries are
  allowed per day, or whether a recovery above VWAP without entry resets the cycle.
- Expected signal time is the first qualifying intrabar trade-price cross. Engine-owned arbitration,
  sizing, protection, submission, and fills follow.

## B — rules inferred from examples or explanations

- “Narrowing the spread” most naturally means `(EMA20 - EMA9)[k] < (EMA20 - EMA9)[k-3]` while
  EMA9 remains below EMA20, but the document defines only “gap” and does not state absolute/signed
  handling or whether a crossover is permitted.
- “Completely engulfing” commonly means current open at/below prior close and current close at/above
  prior open, but strict versus inclusive boundaries are unstated.
- An immediate cross requires a preceding trade at/below contemporaneous VWAP, not merely a current
  trade above VWAP.

These inferences are not approved implementation rules.

## C — missing or conflicting information

- Whether “price drops below” is determined by trade, candle low, or completed close.
- Exact EMA seed/initialization and whether premarket closes participate.
- What “below VWAP” means for a bottoming candle (high, body, close, or pattern point).
- Numerical definition of “price moves back up toward VWAP.”
- Signed versus absolute EMA spread and behavior after EMA9 crosses EMA20.
- Base-range “price” denominator.
- Strict/inclusive engulfing boundaries and zero-body/zero-range candle handling.
- Why the five required reference levels are needed and what conditions use them.
- Entry-window endpoint inclusion and what “highest priority” changes in a single-strategy decision.
- Cycle reset/rearm and daily attempt limits.
- VWAP price basis and whether the intrabar VWAP changes with the triggering tick.
- Whether `SPCX` is intentional.
- A fill-dependent extension rejection cannot determine whether to emit the pre-fill signal.

## D — implementation assumptions requiring operator approval

None have been applied. All choices in section C, plus the required tick/fill lifecycle contract,
need operator approval.

## E — rules unsupported by the existing system

- Immediate intrabar first-cross evaluation under the closed-bar-only `StrategySpec`/scheduler.
- Historical per-bar VWAP at the cycle low is not supplied as a series by the scalar indicator map;
  exact strategy-local calculation needs an approved price/volume formula.
- Actual fill/VWAP-at-fill extension validation has no post-fill strategy callback.
- “Priority window” has no strategy arbitration meaning defined by the source or platform.
- Required-but-unused reference levels cannot be invented as filters.

## Known implementation limitations

Replacing the trade-price crossing with a completed close above VWAP would be a different strategy
and is prohibited. The unresolved subjective recovery rule also prevents a faithful deterministic
test oracle. This strategy is not ready for backtesting.

## Traceability matrix

| Original rule or condition | Python implementation location | Verification test |
|---|---|---|
| Time and universe restrictions | Blocked; none | Pending endpoint, priority, symbol tests |
| Initial price/EMA/VWAP hierarchy | Blocked by event-definition ambiguity | Pending strict hierarchy tests |
| Cycle dip and $2 maximum | Blocked by cycle/VWAP definition | Pending equality, exceed-once, persistent rejection tests |
| Hammer/wick pattern | Blocked by below-VWAP/zero-range details | Pending shape and boundary tests |
| Three-candle base | Blocked by denominator definition | Pending adjacency/range boundary tests |
| Bullish engulfing | Blocked by boundary definition | Pending color and engulfing equality tests |
| Price recovers toward VWAP | Blocked as subjective/unnumbered | Pending approved deterministic rule tests |
| EMA9 slope and narrowing spread | Blocked by spread/EMA details | Pending `k`/`k-3`, equality, crossover tests |
| Immediate VWAP cross | Blocked by tick scheduling | Pending first-cross/replay/no-look-forward tests |
| Fill no more than 0.10% above VWAP | Blocked by post-fill lifecycle | Pending equality and overextension tests |
| Reference levels | Blocked because no predicate is supplied | Pending only after rule is supplied |
| Cycle rearming | Blocked because reset rules are absent | Pending same-cycle/session reset tests |

