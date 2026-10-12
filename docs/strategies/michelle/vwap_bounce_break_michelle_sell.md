# `vwap_bounce_break_michelle_sell`

## Identity and status

- Original document: `VWAP_Bounce_Break_Formatted.docx`
- Original name: **VWAP BOUNCE AND BREAK ORIGINAL STRATEGY**
- Role: independent SELL/protective-stop policy
- Source: Michelle, with approved October 2026 clarifications taking precedence
- Status: **implemented for SHADOW and historical replay**

## Contract

Purpose: protect and fully close a VWAP-bounce long; there is no VWAP-break or EMA technical exit.

Required state/data: actual fill and position identity, session VWAP captured at fill, ordered trade
prices, engine high-water mark, stop mode/price, and the existing EOD lifecycle.

Rules:

1. Freeze fill-time VWAP `V`; immediately install `InitialStop = V - $0.25`.
2. Keep it fixed until `Candidate = 0.995 * H > V + $0.25`, where `H` is highest traded price
   since entry.
3. After activation, `Stop = max(previous_stop, 0.995 * H)` and never decreases.
4. A protective trigger or mandatory EOD liquidation closes the entire remaining position. A
   VWAP cross or EMA condition alone never exits.

No configurable parameters. The strategy class mirrors engine-owned state for deterministic
evaluation; mandatory protective execution remains in the lifecycle.

Superseded source statements: “.25 cents” is explicitly $0.25; activation uses `V + $0.25`
rather than fill-relative 99%; highest traded price is authoritative; VWAP is frozen at fill.

## Traceability

| Rule | Python | Verification |
|---|---|---|
| Fill-time VWAP minus $0.25, frozen | lifecycle fill facts and `domain/protection.py` | focused VWAP SELL state test |
| Strict activation using highest trade | `brokers/shadow/stops.py` | `test_michelle_fixed_stop_activates_monotonic_trail_from_trade_high` |
| Monotonic 0.5% trail/full close | same plus lifecycle stop precedence | focused SELL/shadow tests |
| Independent registration | runtime/research registries | `test_all_four_strategies_register_independently` |

Historical replay freezes the causally available VWAP from the signal/preceding completed bar at
the next-bar fill; it does not use the fill bar's completed OHLCV. PAPER/LIVE remain fail-closed on
adapters that cannot safely preserve broker-native coverage through stop replacement.
