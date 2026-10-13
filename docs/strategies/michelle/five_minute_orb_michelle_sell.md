# `five_minute_orb_michelle_sell`

## Identity and status

- Original document: `Five_Minute_ORB_Formatted.docx` (superseded for protective-stop rules; see below)
- Original name: **5-MINUTE OPENING RANGE BREAKOUT (ORB) STRATEGY (LONG ONLY)**
- Role: independent SELL/protective-stop policy
- Source: Michelle, with approved October 2026 clarifications taking precedence. The protective-stop
  rules below are Michelle's own confirmed clarification: *"Only a .5% trailing stop after that
  stop has exceeded the initial stop. That way you never lose more than 25 cents per share on a
  losing stock."* She then changed the trail: *"switch the trailing stop to .2%."* The operator
  confirmed with Michelle that the initial stop is always $0.25 below the **actual executed BUY
  fill price**.
- Status: **implemented for historical replay, SHADOW and PAPER/LIVE (broker-held stop)**
- Strategy version: **1.1.0** (fill-based semantics; 1.0.0 used reference-based formulas)

## Contract

Purpose: protect and fully close a long; there is no technical-indicator exit. It protects a position opened by `five_minute_orb_michelle_buy`.

Required state/data: the confirmed BUY fill price, position identity, ordered traded prices since
the fill, the engine high-water mark, active stop mode/price, and the session/EOD lifecycle.

Rules (identical for the ORB and VWAP strategies):

1. Immediately after a positive fill, install `InitialStop = ActualBuyFillPrice - $0.25`.
2. `HighestPrice` is the highest observed traded price **since the confirmed fill**; it starts at
   the fill price and never includes market highs before entry.
3. `Candidate = HighestPrice * 0.998` (a **0.2%** trail).
4. Keep the initial stop fixed until the exchange-valid candidate **strictly exceeds** it:
   `floor_to_tick(Candidate) > InitialStop` activates; equality does not.
5. After activation, `ActiveStop = max(PreviousActiveStop, floor_to_tick(Candidate))`. The stop
   never moves down and never returns to fixed mode.
6. A protective trigger closes the entire remaining position. Existing EOD liquidation also closes
   it. No VWAP/EMA/discretionary SELL exists.

The VWAP at the fill, the ORB high, the signal price, the previous candle close and any estimated
entry price play **no** part in the stop.

### Price precision and rounding (Decimal throughout)

- The initial stop rounds **up** to the tick (`ceil`), so rounding can only make the initial
  distance smaller than $0.25, never larger. Example: fill `428.9944`, raw `428.7444`, stop
  `428.75`.
- The trailing candidate rounds **down** (`floor`), the engine's convention for every long
  trailing stop (never tighter than designed). Example: high `401.00` gives `400.198`, i.e. `400.19`.
- Ticks follow the Alpaca equity grid (0.01 at or above $1, 0.0001 below); the live broker adapter
  supplies its own tick size.
- Worked example: fill `400.00` -> initial `399.75`; at entry the candidate is `399.20` (below, so
  the stop stays `399.75`); once a high of `401.00` is observed the stop becomes `400.19`, stays
  there on a decline, and rises again only with a higher valid candidate.
- Below roughly $125 a 0.2% trail is smaller than $0.25, so the first new high already yields a
  candidate above the initial stop and the trail activates at once; this is the rule as written.

The initial stop is a **level**, not a guaranteed exit price: gaps, market conditions and execution
slippage can realize losses larger than $0.25 per share. Historical replay fills a gap through the
stop at the gapped open, and keeps the conservative stop-first handling of ambiguous bars (a stop
ratcheted from a completed bar's high takes effect after that bar).

No configurable parameters. Position state resets on a new cycle. The strategy class is an
advisory mirror; the engine-owned broker-held (or simulated) stop has precedence and performs the
exit.

Entry safety: an entry fill that admits no valid stop (positive and strictly below the fill) is
rejected atomically in replay, creating no position. Because the stop derives from the fill, a
next-bar open that falls under the old reference-based stop level is no longer a reason to reject.

Superseded: the reference-based initial stops (`ORB_High - $0.25` and fill-time `VWAP - $0.25`),
the activation test `0.995 * H > reference + $0.25`, and the 0.5% trail.

## Traceability

| Rule | Python | Verification |
|---|---|---|
| Fill-based initial stop, 0.2% trail, strict activation, rounding | `risk/michelle_stop.py` | `test_michelle_stop_rule.py` |
| Historical engine (initial stop, entry bar, gap, full exit) | `backtest/runner.py` | `test_michelle_strategies.py` (`test_backtest_*`) |
| SHADOW simulated stop | `brokers/shadow/stops.py`, `app.py` | `test_shadow_stops.py` |
| PAPER/LIVE broker-held stop and ratchet | `live/executor.py`, `live/lifecycle.py` | `test_live_protection.py` |
| Advisory mirror | `strategies/sell/_michelle_protective.py` | `test_michelle_sell_is_fill_based_monotonic_and_full_exit_advisory` |
| Atomic entry rejection | `backtest/execution.py` | `test_backtest_execution.py` |

PAPER/LIVE limitation: adapters without a verified broker-held safe replacement capability must
fail closed rather than expose a live position during the fixed-to-trailing transition.
