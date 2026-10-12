# `vwap_bounce_break_michelle_sell`

## Identity and status

- Original document: `VWAP_Bounce_Break_Formatted.docx`
- Original strategy name: **VWAP BOUNCE AND BREAK ORIGINAL STRATEGY**
- Proposed Mike_Trade identifier: `vwap_bounce_break_michelle_sell`
- Role: SELL (independent advisory exit for a managed long)
- Source attribution: Michelle; the original document above is authoritative
- Purpose: exit at an immutable fill-time VWAP stop or an activated 0.5% trailing stop
- Status: **BLOCKED — no Python class or registry entry exists**

This strategy must be optional independently of `vwap_bounce_break_michelle_buy` and must operate
only on a managed long. It cannot suppress or bypass mandatory engine protection.

## Formal contract

### Required market data and indicators

- Actual entry fill price and timestamp for the current managed long.
- VWAP captured at the moment of actual fill, not a later drifting VWAP.
- Ordered real-time trade prices and bid/ask quotes after fill.
- Position-cycle high-water price and protective-stop status.
- VWAP is the only named indicator, but its exact price basis/formula is unstated.

### Timeframes and lookback

- Exit monitoring is intrabar/tick-driven.
- The initial stop requires no rolling candle lookback once fill-time VWAP is captured.
- The trailing stop requires causal price history or an engine-maintained high-water mark from
  activation onward.

### Explicit exit rules (A — stated in the document)

1. Capture VWAP at the moment of actual entry fill.
2. Set the initial stop to captured VWAP minus `$0.25`; do not move it when VWAP drifts.
3. Put a 0.5% trailing stop in place “when 99.0% of price is greater than entry fill price.”
4. Exit according to the initial stop or the activated trailing stop.

The quick summary says “Set immediate stop .25 cents below VWAP,” which literally means $0.0025;
the detailed stop rule says `$0.25`, i.e. 25 cents. The detailed rule is more explicit but the
conflict still requires approval.

### Parameters and constraints

| Value | Source value | Constraint |
|---|---:|---|
| Initial stop offset | `$0.25` in detailed rule; “.25 cents” in summary | Conflict requires approval |
| Trailing distance | 0.5% | Positive; never loosen once active |
| Activation | `0.99 * price > entry_fill` if literal | Exact intent/boundary requires approval |

### State, rearming, and execution timing

- Per-position-cycle state: cycle id, actual fill, fill-time VWAP, immutable initial stop,
  activation status/time, high-water price, and current trailing stop.
- Reset only on a new managed long cycle. State must be independent of whichever BUY strategy
  opened the position.
- Exact behavior requires tick-level stop monitoring. A SELL class would emit only an advisory
  exit signal; the engine would arbitrate and execute it after higher-priority protection.

## B — rules inferred from explanations

- The detailed `$0.25` wording probably controls over the summary's “.25 cents,” but it is not
  silently selected.
- The activation phrase read literally is `0.99 * current_price > fill`, or current price more
  than about 1.010101% above fill. It may have intended “price is 1% above fill.”
- A conventional percentage trail follows the highest post-activation price; the document does
  not identify the mark or update cadence.

## C — missing or conflicting information

- Whether the stop offset is $0.25 or $0.0025.
- Exact activation formula and equality handling.
- VWAP calculation and whether the triggering fill itself participates in captured VWAP.
- Trailing reference (trade, bid, midpoint, candle high, or broker-native mark) and update cadence.
- Whether trail activation replaces the initial stop or uses the tighter of both.
- Stop trigger mark, equality semantics, gaps, partial-fill handling, and average fill handling.

## D — implementation assumptions requiring operator approval

None have been applied. Section C and the position/fill-state interface require approval.

## E — rules unsupported by the existing system

- SELL strategies are not supplied fill-time VWAP, actual fills/partial fills, position-cycle
  high-water marks, or protective-stop state.
- Closed-candle strategy scheduling cannot reproduce immediate stop/trailing behavior.
- Strategy code cannot and must not submit or replace protective orders.
- Applying this SELL independently to a position opened by any BUY requires engine-owned capture
  of fill-time VWAP; a same-stem BUY strategy cannot be hardwired as a data source.

## Known implementation limitations

A completed-bar exit would expose the position beyond the documented stop and is not faithful.
Implementing only a protection policy would not satisfy the requested independent SELL-class
deliverable without explicit approval. This strategy is not ready for backtesting.

## Traceability matrix

| Original rule or condition | Python implementation location | Verification test |
|---|---|---|
| Capture VWAP at actual fill | Blocked; no fill callback/state contract | Pending partial/complete fill and frozen-VWAP tests |
| Initial stop below captured VWAP | Blocked by offset conflict | Pending exact offset and no-drift tests |
| Activation at stated fill-relative condition | Blocked by ambiguous phrase | Pending exact boundary tests |
| 0.5% trailing stop | Blocked by trail reference definition | Pending ratchet, no-loosening, gap, and replay tests |
| Exit at initial or trailing stop | Blocked by tick/protection integration | Pending first-breach and precedence tests |
| Independent optional SELL behavior | Blocked by missing engine-owned fill facts | Pending configuration/position-cycle isolation tests |
| Never bypass mandatory protection | Existing engine invariant; no new code | Pending exit-priority integration test |

