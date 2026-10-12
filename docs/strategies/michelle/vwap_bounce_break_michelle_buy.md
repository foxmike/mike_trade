# `vwap_bounce_break_michelle_buy`

## Identity and status

- Original document: `VWAP_Bounce_Break_Formatted.docx`
- Original name: **VWAP BOUNCE AND BREAK ORIGINAL STRATEGY**
- Role: independent BUY
- Source: Michelle, with approved October 2026 clarifications taking precedence
- Status: **implemented; ready for targeted historical backtesting and SHADOW evaluation**

## Contract

Purpose: enter the second qualified green opportunity after a below-VWAP bottom and confirmed
VWAP recovery, rather than buying the first recross.

Required data: completed regular-session M1 OHLCV, ordered live trades/developing M1, VWAP from
09:30, and SMA-seeded EMA9/EMA20 from completed M1 closes. Previous-day and premarket reference
levels are metadata only when already available.

Rules:

1. Start a cycle on a completed regular-session bar with price below VWAP, EMA9 and EMA20 and
   strict `EMA9 < EMA20 < VWAP`.
2. Retain the cycle low through minor recoveries. Invalidate if VWAP at the cycle low minus the
   lowest price exceeds $2.00, or if VWAP recovery does not occur within 30 minutes.
3. Below VWAP, require at least one deterministic pattern: (a) lower wick at least twice body and
   close in top 40% of range; (b) three candles with combined range no more than 0.15% of the third
   close; or (c) green body inclusively engulfing the preceding red body.
4. Require `EMA9[k] > EMA9[k-3]` and absolute EMA9/EMA20 spread smaller than at `k-3`.
5. First confirmation must be green, cross VWAP during the minute, and close above it. Record its
   close/time/cycle; do not buy.
6. A later (not necessarily consecutive) green candle qualifies when current price is above VWAP,
   reaches the first confirmation close, and is no more than `$0.50` over VWAP.
7. Before 11:30 ET, emit a MARKET BUY during that live developing candle. Replay uses the completed
   second green and the existing next-M1-open fill model.
8. Consume the cycle only when the entry is submitted. A later entry requires a genuinely new
   breakdown, bottom, recovery, and second green, plus the shared attempt gate.

No configurable parameters and no exclusive symbol list. State contains session/cycle identity,
breakdown and low facts, bottom type, first confirmation, signal identity, consumed timestamp,
entry VWAP/reference, attempts, and position identity.

Superseded source statements: entry is on the second green rather than immediate first recross;
maximum extension is fixed $0.50 rather than 0.10%; recovery limit is 30 minutes; absolute EMA
spread is authoritative; reference levels are optional context; VWAP/pattern setup is regular
session; the fixed universe is not exclusive.

## Traceability

| Rule | Python | Verification |
|---|---|---|
| Bearish hierarchy and EMA recovery | `strategies/buy/vwap_bounce_break_michelle.py` | VWAP cycle positive/negative tests |
| Hammer/base/engulfing | `_hammer`, `_base`, `_engulfing` | `test_vwap_patterns_and_historical_second_green_with_red_between` |
| $2 dip and 30-minute expiry | `_scan_cycle` | `test_vwap_cycle_invalidates_deep_dip_expiry_extension_and_consumption` |
| First then second green; intervening red | `_scan_cycle` and live evaluator | second-green focused test |
| $0.50 extension and cycle consumption | evaluator plus lifecycle submission state | invalidation/consumption focused test |
| Stable signal and shared attempts | strategy state plus lifecycle | identity and attempt tests |

Known limitation: historical M1 replay cannot reconstruct the second candle's intrabar ordering;
it uses only the completed bar and submits for the next bar open without favorable look-ahead.
