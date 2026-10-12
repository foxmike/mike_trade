# `five_minute_orb_michelle_sell`

## Identity and status

- Original document: `Five_Minute_ORB_Formatted.docx`
- Original strategy name: **5-MINUTE OPENING RANGE BREAKOUT (ORB) STRATEGY (LONG ONLY)**
- Proposed Mike_Trade identifier: `five_minute_orb_michelle_sell`
- Role: SELL (independent advisory exit for a managed long)
- Source attribution: Michelle; the original document above is authoritative
- Purpose: exit at the entry-time initial stop or at an activated 0.5% trailing stop
- Status: **BLOCKED — no Python class or registry entry exists**

This strategy must remain selectable independently of `five_minute_orb_michelle_buy`. It must never
disable, replace, delay, or outrank the engine's mandatory protective stop.

## Formal contract

### Required market data and indicators

- Actual position fill price and fill timestamp for the currently managed long.
- The exact previous-candle low selected at the entry decision/fill.
- Ordered real-time trade prices and bid/ask quotes after the fill.
- Position-cycle maximum favorable price after trailing activation.
- No independent technical indicator is named for the exit; its levels are fill/price state.

### Timeframes and lookback

- Exit monitoring is intrabar/tick-driven in the document.
- “Previous candle” is not assigned a timeframe, so required candle timeframe/lookback is unresolved.
- A trailing stop needs the full causal stream since activation, or equivalent engine-maintained
  high-water state.

### Explicit exit rules (A — stated in the document)

1. Initial stop equals the low of the previous candle.
2. The trade exits according to that initial stop unless/reuntil the trailing rule governs.
3. A 0.5% trailing stop is put in place “when 99% of price is greater than entry fill price.”
4. The trade exits in accordance with the activated trailing stop.

### Parameters and constraints

| Value | Source value | Constraint |
|---|---:|---|
| Initial stop | Previous candle low | Candle timeframe and capture time missing |
| Trailing distance | 0.5% | Positive; ratchet must never loosen |
| Activation | `0.99 * price > entry_fill` if read literally | Wording requires approval |

### State, rearming, and execution timing

- Per-position-cycle state: cycle id, actual fill, immutable initial stop, activation status,
  activation time, high-water mark, and current trailing stop.
- State resets only when a new managed long cycle begins; it must not leak across positions or
  symbols.
- Exit should be detected on the first causally observed tick that breaches the governing stop.
- Strategy output, if a SELL class is approved, is advisory only; exit arbitration and order
  submission remain engine-owned. Mandatory protection continues to run first.

## B — rules inferred from explanations

- The phrase can be evaluated literally as `0.99 * current_price > fill`, equivalent to a gain
  strictly greater than approximately 1.010101%. It may instead be an imprecise attempt to say that
  price is 1% above the fill. Neither interpretation is adopted.
- A conventional 0.5% trailing stop would follow the highest post-activation trade price and never
  move downward, but the document does not define its reference price or update granularity.

## C — missing or conflicting information

- The timeframe and identity of the “previous candle,” and whether its low is captured at signal or fill.
- Exact meaning and boundary of “99% of price is greater than entry fill price.”
- Whether the trail is percent below the highest trade, bid, mid, candle high, or broker-native mark.
- Whether activation replaces/cancels the initial stop or ratchets from the safer of the two.
- Stop trigger basis (trade, bid, ask, or quote midpoint), gap behavior, and equality semantics.

## D — implementation assumptions requiring operator approval

None have been applied. Operator approval is required for the items in section C and for the
fill/cycle-state interface described below.

## E — rules unsupported by the existing system

- `SellStrategy.evaluate` is not given the held position's fill, previous-candle stop captured at
  entry, position cycle, high-water mark, or protective-stop state.
- Strategy evaluation is closed-bar-driven; exact tick stop monitoring belongs to the engine's
  protection subsystem.
- A strategy cannot create/replace broker protective orders, and must not do so.

## Known implementation limitations

Implementing a close-below-stop SELL signal would materially differ from an intrabar protective
stop. Implementing the rule as protection policy rather than a SELL class would violate the requested
four independent strategy deliverables unless explicitly approved. This strategy is not ready for
backtesting.

## Traceability matrix

| Original rule or condition | Python implementation location | Verification test |
|---|---|---|
| Initial stop = previous candle low | Blocked; none | Pending timeframe/capture and equality tests |
| Activate trail at stated fill-relative condition | Blocked by ambiguous phrase/fill state | Pending exact activation boundary tests |
| 0.5% trailing stop | Blocked by reference/update definition | Pending ratchet, no-loosening, gap, and deterministic replay tests |
| Exit at initial or trailing stop | Blocked by tick/protection integration | Pending precedence and first-breach tests |
| Independent optional SELL behavior | Blocked by missing position-state contract | Pending registry/config independence tests |
| Never bypass mandatory protection | Existing engine invariant; no new code | Pending exit-priority integration test |

