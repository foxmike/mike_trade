# `five_minute_orb_michelle_sell`

## Identity and status

- Original document: `Five_Minute_ORB_Formatted.docx`
- Original name: **5-MINUTE OPENING RANGE BREAKOUT (ORB) STRATEGY (LONG ONLY)**
- Role: independent SELL/protective-stop policy
- Source: Michelle, with approved October 2026 clarifications taking precedence
- Status: **implemented for SHADOW and historical replay**

## Contract

Purpose: protect and fully close an ORB long; there is no technical-indicator exit.

Required state/data: actual fill price/time, position identity, captured ORB high, ordered traded
prices, engine high-water mark, active stop mode/price, and session/EOD lifecycle.

Rules:

1. Immediately after fill, install `InitialStop = ORB_High - $0.25`.
2. Keep that stop fixed until `Candidate = 0.995 * H > ORB_High + $0.25`, where `H` is the
   highest observed traded price since entry.
3. After activation, use `Stop = max(previous_stop, 0.995 * H)`; it never decreases.
4. A protective trigger closes the entire remaining position. Existing EOD liquidation also
   closes it. No VWAP/EMA/discretionary SELL exists.

No configurable parameters. Position state resets on a new cycle. The strategy class is an
advisory mirror; the engine-owned broker-native stop has precedence and performs the exit.

Superseded source statements: the previous-candle-low initial stop and fill-relative 99% activation
wording are replaced by the exact ORB-reference formula above.

## Traceability

| Rule | Python | Verification |
|---|---|---|
| ORB high minus $0.25 | `app.VwapReferenceLifecycle._michelle_policy` and `domain/protection.py` | `test_michelle_sell_fixed_to_trailing_is_monotonic_and_full_exit_advisory` |
| Strict activation and 0.5% traded-price trail | `brokers/shadow/stops.py` | `test_michelle_fixed_stop_activates_monotonic_trail_from_trade_high` |
| Monotonic stop and full exit | same plus lifecycle stop precedence | focused SELL and shadow-stop tests |
| Independent registration | runtime/research registries | `test_all_four_strategies_register_independently` |

PAPER/LIVE limitation: adapters without a verified broker-held safe replacement capability must
fail closed rather than expose a live position during the fixed-to-trailing transition.
