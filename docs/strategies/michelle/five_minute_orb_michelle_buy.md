# `five_minute_orb_michelle_buy`

## Identity and status

- Original document: `Five_Minute_ORB_Formatted.docx`
- Original strategy name: **5-MINUTE OPENING RANGE BREAKOUT (ORB) STRATEGY (LONG ONLY)**
- Proposed Mike_Trade identifier: `five_minute_orb_michelle_buy`
- Role: BUY (independent long-entry signal)
- Source attribution: Michelle; the original document above is authoritative
- Purpose: enter the first qualified upside break of the first regular-session five-minute range
- Status: **BLOCKED — no Python class or registry entry exists**

## Formal contract

### Required market data

- Completed RAW 5-minute OHLCV for exactly 09:30:00–09:34:59 America/New_York.
- Completed RAW 1-minute OHLCV sufficient for ATR(14), the previous-candle stop reference, and
  causal validation through the decision time.
- Every regular-session trade tick from 09:35:00 through 10:15:00 ET, ordered causally, so the
  first strict upward crossing can be identified.
- A contemporaneous bid/ask snapshot at the decision time.
- VWAP observations required by the source's anchoring/inclusion rule.
- Exchange calendar/session identity and per-symbol session state.

### Required indicators and derived values

- `ORB_High`: exact high of the 09:30–09:35 five-minute candle.
- `ORB_Low`: exact low of that candle.
- `ORB_Midpoint = (ORB_High + ORB_Low) / 2` (required by the document, although no entry
  predicate uses it).
- Opening-range height: `ORB_High - ORB_Low`.
- VWAP described as “anchored at 9:30 AM ET, including premarket.”
- ATR(14) calculated from completed 1-minute candles at entry time.

### Timeframes and lookback

- Trigger cadence: trade tick/intrabar, not a completed candle.
- Required candle timeframes: 1 minute and 5 minutes.
- The opening range requires the completed 09:30 five-minute candle.
- ATR lookback cannot be finalized until Michelle selects its smoothing/seed convention and
  specifies whether the previous close may come from premarket or the previous session.
- VWAP lookback cannot be finalized because “anchored at 9:30” and “including premarket” describe
  incompatible start sets without an additional convention.

### Explicit entry and rejection rules (A — stated in the document)

1. Symbol is one of `AAPL, PLTR, MSFT, GOOGL, NVDA, TSLA, AMD, SPCX, ORCL, INTC, DELL`.
2. The 09:30 five-minute candle is strictly green: `close > open`.
3. The opening-range height satisfies the inclusive boundary
   `0.0030 * open <= ORB_High - ORB_Low <= 0.0180 * open`.
4. The document first says the opening candle close must be strictly above VWAP **or within .50
   of VWAP**. Its rejection list later rejects an opening candle that “closes below VWAP.” No
   implementation choice is made between those statements.
5. From 09:35:00 through 10:15:00 ET, identify the first regular-session trade price strictly
   crossing above `ORB_High + $0.01`.
6. Reject the cycle if price breached below `ORB_Low` before that trigger.
7. At the entry time, reject when entry/fill price is greater than
   `VWAP + 1.5 * ATR(14)`.
8. Initial stop reference is the low of the “previous candle.” Reject when
   `(entry - stop) > 0.010 * entry`.
9. Reject when the ticker has already had one ORB attempt that day.
10. The document calls for a market order and also says it is “placed at ORB_High + $0.01,” with
    an offset capped at `entry + max($0.10, 0.03% of price)`.

Signal confirmation is the first strict trade-price crossing after all applicable setup and
rejection conditions have remained valid. No completed-bar confirmation is stated.

### Parameters and constraints

The source presents fixed values rather than operator-tunable defaults. Faithful v1 parameters
would therefore be fixed unless Michelle approves configurability:

| Value | Source value | Constraint |
|---|---:|---|
| Entry window | 09:35:00–10:15:00 ET | Endpoint inclusion needs confirmation |
| Break offset | $0.01 | Positive tick increment |
| Minimum range | 0.30% of opening price | Inclusive |
| Maximum range | 1.80% of opening price | Inclusive |
| VWAP/ATR overextension | 1.5 × ATR(14) | Strict `>` rejection |
| Maximum risk/share | 1.0% of entry | Strict `>` rejection |
| Maximum attempts | 1 per ticker/day | “Attempt” event undefined |
| Order offset cap | max($0.10, 0.03% of price) | Meaning/order type undefined |

### Session, state, rearming, and timing

- Regular-session setup and trigger only; America/New_York session clock.
- Persistent session state must contain the session id, ORB values, whether `ORB_Low` was breached,
  the prior observed trade price for crossing detection, and whether the daily attempt was consumed.
- A valid cycle can fire at most once per symbol per trading day. Rearming is at the next trading
  session, but the event that consumes the one attempt is missing.
- Expected signal time is the first qualifying trade tick, followed by engine-owned arbitration,
  sizing, risk checks, submission, fill handling, and mandatory protection.

## B — rules inferred from examples or explanations

- “Within .50 of VWAP” appears to mean within $0.50, probably including a close below VWAP, but the
  later rejection directly conflicts with that reading.
- “First regular-session trade price strictly crossing” implies retaining the preceding trade
  price and requiring `prior <= threshold < current`, not merely observing a price above threshold.
- The high/low percentages use the opening candle's open, as confirmed by the displayed formula.

These inferences are not approved implementation rules.

## C — missing or conflicting information

- Which opening-candle/VWAP statement controls, and what “within .50” means.
- Whether VWAP starts at 09:30 or includes premarket, and the exact price basis/formula.
- ATR smoothing and seed convention; treatment of overnight/premarket previous close.
- Whether entry-window endpoints are inclusive.
- Whether “previous candle” means the latest completed 1-minute or 5-minute candle.
- What event is an ORB “attempt”: signal, authorization, order submission, partial fill, or fill.
- How a market order can be placed at a price and what the “offset capped at” clause instructs.
- Whether `SPCX` is intentional (rather than another symbol such as `SPXC`).

## D — implementation assumptions requiring operator approval

None have been applied. Approval is required for every choice listed in section C and for the
infrastructure design listed below.

## E — rules unsupported by the existing system

- Tick-driven first-cross strategy evaluation (`StrategySpec` is closed-bar-only).
- A fill-dependent rejection before/after an advisory signal without a post-fill strategy callback.
- An order price/slippage cap inside strategy code; execution belongs to the engine.
- Reliable attempt counting based on an engine lifecycle event; strategy state currently receives
  only evaluation updates.
- A VWAP series that is simultaneously 09:30-anchored and premarket-inclusive as currently worded.

## Known implementation limitations

No reduced completed-bar implementation is permitted. Sampling `latest_trade` only on a candle-close
event would miss the first crossing, its ordering relative to an `ORB_Low` breach, and the exact
VWAP/ATR/fill-time checks. The current source rule is therefore not ready for backtesting.

## Traceability matrix

| Original rule or condition | Python implementation location | Verification test |
|---|---|---|
| 09:30–09:35 range and exact high/low/midpoint | Blocked; none | Pending exact M5 boundary/aggregation test |
| Green opening candle | Blocked; none | Pending equality and strict-green boundary tests |
| 0.30%–1.80% inclusive range | Blocked; none | Pending lower/upper/in/out boundary tests |
| Opening close vs VWAP | Blocked by conflicting source text | Pending operator-approved rule tests |
| First tick cross above high + $0.01 | Blocked by tick scheduling | Pending first-cross, replay, and no-look-forward tests |
| Entry window | Blocked; none | Pending both endpoint and outside-window tests |
| Prior ORB-low breach invalidates cycle | Blocked by ordered tick history | Pending breach-before/after-cross tests |
| VWAP + 1.5 × ATR(14) rejection | Blocked by indicator/fill definitions | Pending equality/strict-rejection tests |
| Previous-candle stop and 1% risk rejection | Blocked by timeframe/fill ambiguity | Pending timeframe and risk boundary tests |
| One attempt per day | Blocked by attempt-event ambiguity | Pending same-session suppression/next-session rearm tests |
| Order/offset clause | Engine behavior; blocked/undefined | Pending execution-contract tests after approval |

