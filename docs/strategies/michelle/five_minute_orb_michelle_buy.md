# `five_minute_orb_michelle_buy`

## Identity and status

- Original document: `Five_Minute_ORB_Formatted.docx`
- Original name: **5-MINUTE OPENING RANGE BREAKOUT (ORB) STRATEGY (LONG ONLY)**
- Role: independent BUY
- Source: Michelle, with approved October 2026 clarifications taking precedence
- Status: **implemented; ready for targeted historical backtesting and SHADOW evaluation**

## Contract

Purpose: enter a qualified upward break of the first five regular-session minutes.

Required data and indicators:

- RAW M1 OHLCV from 09:30 onward and ordered live trades for intrabar entry.
- Fifteen completed regular-session D1 bars for 14 true ranges and Wilder ATR(14).
- Session VWAP from 09:30 and its latest two completed-M1 observations.
- Optional latest two completed SPY M1 closes.
- Opening price, ORB high, low, close, and midpoint from 09:30:00–09:34:59 ET.

Exact entry rules:

1. Finalize the range at 09:35. Require `close > open`.
2. Require inclusive height `0.30% <= (high-low)/open <= 1.80%`.
3. Require opening-range close above VWAP; invalidate a range that fails either setup rule.
4. Invalidate the setup if any later price breaches ORB low before entry.
5. Between 09:35:00 and 11:29:59 ET, require a from-below trade cross over ORB high and a green
   developing M1 candle. Historical replay uses a green completed M1 whose high crossed the level.
6. Require current price above VWAP and completed-M1 VWAP now greater than its preceding value.
7. When enabled, require latest completed SPY M1 close greater than its preceding close.
8. Require entry reference `<= VWAP + 1.5 * daily ATR(14)`.
9. With `InitialStop = ORB_High - $0.25`, require risk/share `<= 1%` of entry reference.
10. Emit one stable MARKET BUY signal for the event. Shared submission-attempt restrictions then
    apply in the lifecycle.

Parameter: `spy_confirmation_enabled: bool = false`. No symbol allow-list is imposed.

State: session id, ORB-low breach, last trade, stable signal/cycle identity, entry VWAP, ORB high,
entry reference, submission attempt timestamps, and eventual position cycle id. A new entry after
exit needs a fresh crossing and must pass the shared attempt gate.

Superseded source statements: the $0.01 offset is removed; entry is MARKET; the window ends at
11:30 rather than 10:15; VWAP excludes premarket; ATR is daily rather than M1; the prior-candle
stop and one-entry-per-day experiment are removed; the fixed symbol list is not exclusive.

## Traceability

| Rule | Python | Verification |
|---|---|---|
| First five minutes, green, 0.30%–1.80% | `strategies/buy/five_minute_orb_michelle.py` | `test_orb_buy_historical_signal_and_live_tick_identity`, `test_orb_range_boundaries` |
| VWAP, rising VWAP, SPY option | same evaluator | `test_orb_optional_spy_filter_and_parameter_validation` |
| Trade cross, identity, cutoff, low breach | same evaluator | live identity and rejection tests in `test_michelle_strategies.py` |
| Daily ATR extension and 1% risk | same evaluator plus `michelle_common.daily_wilder_atr` | positive/negative ORB rule tests |
| Shared attempts | lifecycle and `michelle_common.entry_attempt_allowed` | `test_shared_attempt_limit_and_nonconsecutive_rule` |

Known limitation: historical M1 replay cannot know intrabar trade order and therefore signals only
after the completed crossing bar; its market order fills under the existing next-bar-open model.
