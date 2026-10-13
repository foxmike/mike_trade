# Michelle stop clarification — October 2026

Nine development backtests that isolate Michelle's confirmed protective-stop clarification.
**Not executed yet**; the operator launches it (see *Run*).

| | OLD (`michelle_zero_slippage_oct2026`) | NEW (this campaign) |
|---|---|---|
| Initial stop | ORB high (or fill-time VWAP) − $0.25 | **actual BUY fill − $0.25**, rounded **up** to the tick |
| Trail | 0.5% of the highest traded price | **0.2%**, rounded **down** to the tick |
| Activation | `0.995·H > reference + $0.25` | tick-adjusted `0.998·H` **strictly above the initial stop** |
| Atomic entry rejection | fill not above `reference − $0.25` | fill admits no valid stop (positive, below the fill) |

Both scenarios assume **zero slippage and zero fees** (a research scenario, not a live-price guarantee).
Stops are levels, not guaranteed prices: a gap fills at the gapped open and can lose more than $0.25/share.

Unchanged: BUY signals, SIP dataset IDs, development window, seed, $10,000 capital, 10 entry cycles per
session, EOD liquidation at 15:55 ET, `NEXT_BAR_OPEN` fills, `STOP_FIRST` ambiguity, and the sizing framework
($3,000 per-symbol budget, $30 planned-loss budget, generic 1.5% sizing reference — deliberately not touched).
`experiments.json` is byte-identical to the OLD campaign's. Exits now differ, so later trades and trade
counts differ; that is expected. The reserved holdout is not included.

Note for interpretation: below about $125 a 0.2% trail is under $0.25, so the first new high already gives a
candidate above the initial stop and the trail activates at once. IBM, NVDA and TSLA trade above that, where
the initial stop stays fixed until price has risen enough.

## Run

```
nohup research/campaigns/michelle_stop_clarification_oct2026/run_michelle_stop_clarification.sh > /dev/null 2>&1 &
```

`WORKERS` (default 4) sets the pool size. The launcher refuses to start without the OLD baseline
(`research/results/michelle_zero_slippage_oct2026/run-20261013T170714Z-3627337`, read-only), creates a fresh
`research/results/michelle_stop_clarification_oct2026/run-<UTC>-<pid>/`, rebuilds the research image, and then
validates in the container — before any experiment — that the jobs are exact, the zero-cost defaults and the
fill-based rule are present, and that every stop-related source file is byte-identical to the working tree
(`code_hashes.json`). It then runs the nine jobs and writes `comparison.md|json|csv`, `run.log`, `run_info.json`.

## Report

`comparison.md` shows the effective execution assumptions (checked against the required zeros), trades, wins,
losses, win rate, net P&L, return, profit factor, max drawdown, largest winner/loser, stop and EOD exit counts,
atomic rejections, and OLD vs NEW. Every Michelle cycle is audited from recorded fills and protection events:
initial stop = ceil_tick(first BUY fill − 0.25); each move strictly above the initial stop and above the previous
stop, on the tick grid, and exactly floor_tick(0.998 × the recorded high that triggered it); the final stop
matches the last move; exits close the whole position. Rejected entries have no cycle and no `cycle_id`; they
are counted and validated separately and never treated as protective-stop events. Any other event without a
`cycle_id` is reported as an audit failure.

## Future `mike_*` comparison

`campaign_manifest.json` → `comparison_contract` keeps the shared frame; the Mike runs may differ only in the
strategy-rule fields (`strategy`, `buy_strategies`, `sell_strategies`, `short_strategies`, `cover_strategies`,
`parameters`, `protective_stop`). `build_campaign.py` regenerates the jobs and fails if they ever drift from the
previous campaign.
