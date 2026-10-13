# Annual SIP campaign investigation

Source batch: `research/results/sip_annual_oct2026/development-20261012T230002Z-981668/`

The source batch completed 140 of 144 jobs. This investigation did not run an
annual job. It reads the completed ledgers and provides a separate four-job
corrective rerun.

## ORB failure

Jobs 0 and 2 share the IBM event below. Jobs 96 and 98 share the TSLA event.
The combined jobs failed on the ORB entry before their VWAP leg mattered.

| Original jobs | Symbol and session | ORB high | Required initial stop | Signal bar (ET) and OHLC | Next bar open | Fill after 5 bps slippage | Invalid by |
|---|---|---:|---:|---|---:|---:|---:|
| 0, 2 | IBM, 2025-11-10 | 307.60 | 307.35 | 09:40, 306.9392 / 307.71 / 306.9392 / 307.355 | 307.15 | 307.3036 | 0.0464 |
| 96, 98 | TSLA, 2026-07-15 | 404.92 | 404.67 | 09:35, 404.67 / 405.39 / 403.8406 / 404.78 | 404.44 | 404.6423 | 0.0277 |

The completed signal bars validly crossed the opening-range high. Under the
configured `NEXT_BAR_OPEN` policy, the market orders reached the next bar after
the price had gapped down. Adverse BUY slippage then put each prospective fill
below the exact Michelle stop, `ORB high - $0.25`. A stop at or above a long
entry is invalid. The stop formula and next-bar execution are behaving as
specified; the defect was that replay committed the fill before checking
whether its mandatory protection could be established. The later protection
arming exception then terminated the job with an open simulated position.

## Correction and verification

The historical simulator now accepts a required initial protection price for a
pending long entry. It evaluates the final slippage-adjusted fill immediately
before fill commitment. When the fill is not strictly above the required stop,
it rejects the entry atomically, creates no fill or position, and records:

`ENTRY_REJECTED_PROTECTIVE_STOP_NOT_BELOW_FILL`

The runner registers this constraint only for the matching Michelle BUY/SELL
policy and records an `ENTRY_REFUSED` decision plus an
`ENTRY_REJECTED_PROTECTION_INVALID` protection event. The exact ORB or frozen
VWAP reference and `$0.25` distance remain unchanged. No synthetic stop and no
generic trailing policy are substituted.

Focused unit tests cover both observed prices, the strict equality boundary,
and a valid fill: `4 passed, 66 deselected`. One-session replays against the annual SIP snapshots
also completed without exceptions. They recorded the expected IBM and TSLA
rejections. TSLA created no position; IBM continued after the rejection and
completed two later protected trades, showing that rejection does not end the
backtest.

## Technical SELL analysis

The controlled analysis covers 48 completed configurations: four matched BUY
families, each with 0, 1, 2, or 3 technical SELL rules, for all three symbols.
Each layer keeps the BUY collection and 1.5% native trailing stop fixed. The
development window contains 237 trading sessions.

| SELL rules | Configs | Trades | Trades/session/config | Avg hold | Avg P&L/trade | Avg winner | Avg loser | Gross profit | Gross loss | Net P&L | Estimated slippage |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 12 | 5,436 | 1.91 | 178.3 min | -$1.52 | $20.23 | -$16.24 | $44,391 | $52,646 | -$8,255 | $12,480 |
| 1 | 12 | 19,191 | 6.75 | 35.0 min | -$1.66 | $9.89 | -$5.86 | $50,574 | $82,523 | -$31,949 | $36,547 |
| 2 | 12 | 21,836 | 7.68 | 23.8 min | -$1.75 | $8.07 | -$5.20 | $45,809 | $84,065 | -$38,256 | $41,171 |
| 3 | 12 | 24,566 | 8.64 | 12.8 min | -$1.77 | $6.39 | -$4.23 | $36,346 | $79,917 | -$43,572 | $45,922 |

The direction is consistent by symbol. Aggregating the four BUY families for
each symbol, the zero-rule to three-rule change was 1,676 to 8,352 trades and
-$726 to -$14,223 for IBM; 1,720 to 8,098 and -$4,606 to -$15,329 for NVDA;
and 2,040 to 8,116 and -$2,923 to -$14,020 for TSLA.

### Re-entry and slippage

| SELL rules | Same-day re-entry rate | Median same-day gap | Re-entry within 5 min | Added trades vs matched zero-SELL | Net P&L change | Added estimated slippage |
|---:|---:|---:|---:|---:|---:|---:|
| 0 | 47.8% | 13 min | 626 | baseline | baseline | baseline |
| 1 | 85.2% | 7 min | 6,762 | 13,755 | -$23,694 | $24,067 |
| 2 | 87.0% | 8 min | 7,337 | 16,400 | -$30,001 | $28,691 |
| 3 | 88.5% | 8 min | 8,124 | 19,130 | -$35,317 | $33,442 |

Technical exits shortened positions and reopened eligibility for independent
BUY signals. That changed the replay path and generated many more round trips.
The added estimated slippage is 101.6%, 95.6%, and 94.7% of the respective net
P&L deterioration for layers 1, 2, and 3. Estimated slippage covers entries and
exits and is already reflected through simulated fill prices, so it must not be
subtracted from net P&L a second time.

The controlled pairs demonstrate that adding these SELL policies caused the
different exit and re-entry paths in this simulator. They also show a strong
association among short holds, frequent same-day re-entry, slippage, and worse
P&L. They do not prove that re-entry alone caused the loss. The exits also
changed which price moves were captured, and this campaign has no counterfactual
that permits the same exits while suppressing re-entry.

### Exit arbitration

Winner attribution describes the trade P&L assigned to the first technical
SELL that closed the position. It is not a standalone strategy estimate because
rules occur in nested collections and compete with stops and EOD liquidation.

| Winning SELL rule | Exits won | Realized P&L | Avg P&L | Win rate | Same-day re-entry |
|---|---:|---:|---:|---:|---:|
| `previous_swing_low_break` | 18,286 | -$40,229 | -$2.20 | 22.9% | 89.7% |
| `bearish_engulfing_exit` | 9,356 | -$3,366 | -$0.36 | 34.0% | 90.4% |
| `failed_breakout_exit` | 9,235 | $3,622 | $0.39 | 36.6% | 88.6% |
| `vwap_breakdown` | 9,148 | -$24,977 | -$2.73 | 14.0% | 93.4% |
| `price_below_ema` | 5,129 | -$7,928 | -$1.55 | 17.8% | 87.9% |
| `dmi_bearish_crossover` | 4,696 | -$10,494 | -$2.23 | 17.0% | 88.9% |
| `ema_bearish_crossover` | 4,311 | -$8,442 | -$1.96 | 19.9% | 87.0% |
| `high_volume_reversal` | 945 | -$1,348 | -$1.43 | 34.7% | 80.5% |
| `shooting_star_reversal` | 376 | $82 | $0.22 | 38.0% | 87.8% |
| `rsi_reversal` | 317 | $5,599 | $17.66 | 87.1% | 73.8% |

`previous_swing_low_break` won most often, partly because it appears in both
trend and price-action collections. `vwap_breakdown` had the highest same-day
re-entry rate and the worst average outcome in this winner attribution.
`momentum_reversal` never won arbitration in the matched configurations;
`shooting_star_reversal` and `rsi_reversal` won rarely. This is evidence that
some later rules were dominated in these collections, rather than evidence that
their standalone signals are ineffective.

The reproducible detailed inputs for these tables are in
`technical_sell_analysis.json`; `analyze_sell_underperformance.py` regenerates
that file directly from the immutable batch and trade ledgers.

## Next research questions

1. Add a controlled re-entry cooldown experiment while holding each technical
   exit fixed, to separate the exit-price effect from repeated-entry costs.
2. Run each frequently winning SELL rule alone against its matched BUY family;
   nested winner attribution cannot estimate standalone value.
3. Compare the same exit layers at zero and 5 bps slippage to measure execution
   sensitivity without changing signals.
4. Break results down by symbol, time of day, and first versus later daily trade
   to locate the repeated-entry loss concentration.
5. Select any follow-up configuration using development results before running
   the reserved holdout.

## Four-job corrective rerun

The rerun file contains only original jobs 0, 2, 96, and 98 and reuses the IBM
and TSLA annual SIP dataset IDs. It writes to a new timestamped result directory
and leaves the source batch intact. From the repository root, run:

```bash
WORKERS=4 research/campaigns/sip_annual_oct2026/run_failed_orb_reruns.sh
```
