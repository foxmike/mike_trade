# Michelle zero-slippage campaign — October 2026

Nine development backtests of Michelle's current specification, re-run with **no assumed slippage or fees**.
Nothing here has been executed; the operator starts it (see *Run*).

Authoritative rules: `docs/strategies/michelle/{five_minute_orb_michelle_buy,five_minute_orb_michelle_sell,vwap_bounce_break_michelle_buy,vwap_bounce_break_michelle_sell}.md`. No rule or parameter was changed or tuned.

## What differs from `sip_annual_oct2026`

| Change | Why |
|---|---|
| `execution`: `slippage_bps` 5 → 0; `slippage_ticks`, `fee_per_order`, `fee_per_share`, `fee_bps` explicitly 0 | The specifications call for no slippage. The research runner's implicit default was 5 bps (`research_default_execution_assumptions()` is now all zero). |
| Atomic rejection of a Michelle entry whose required initial stop is not strictly below the fill (commit `ff9634a`) | The four original ORB jobs aborted on this condition. |

Everything else is copied verbatim from the original job definitions and checked by `build_campaign.py` (only `execution` may differ): dataset IDs, window `2025-10-09T00:00:00Z` → `2026-09-21T04:00:00Z`, seed 271828, $10,000 capital, 10 entry cycles per session, EOD liquidation at 15:55 ET, strategy parameters (registry defaults), `protective_stop: null` (Michelle's own fixed-then-trailing policy). The reserved holdout (`2026-09-21T04:00:00Z` onward) is not included.

**Zero assumed slippage is a research scenario, not a guarantee of live execution prices.** Market orders still fill at the next eligible bar's open; gap-through-stop exits fill at the gapped open, and OHLC ambiguity stays `STOP_FIRST`.

Jobs: IBM, NVDA, TSLA × `michelle_orb`, `michelle_vwap`, `michelle_combined` = 9.

## Michelle stops (unchanged, verified)

- VWAP: initial stop = frozen fill VWAP − 0.25; ORB: initial stop = ORB high − 0.25.
- Trail activates only when `0.995 × highest price since entry > reference + 0.25` (strict); then `stop = max(previous stop, 0.995 × highest)`; stops never fall.
- No technical SELL; mandatory EOD liquidation remains on.
- Stops are floored to the cent by the runner's tick rule. `report_results.py` re-audits every cycle (initial stop, monotonic movement, policy) from the immutable artifacts.

## BUY specification audit

Distinguishing the four paths in each BUY:

| Concern | Live (trade events) | Historical (completed M1) |
|---|---|---|
| ORB cross | from-below trade cross of ORB high with a green developing candle | completed green M1 whose high crossed ORB high from a prior close ≤ high |
| ORB VWAP/ATR/risk tests | price vs VWAP including developing bar; reference = trade price | VWAP and reference = completed bar; D1 history is point-in-time (`available_at`) |
| VWAP second green | trade during a green developing candle after the first confirmation | completed second green meeting the same price rules |
| Submission | MARKET at the event | MARKET at the completed bar's end |
| Fill | broker | next eligible M1 open (no look-ahead) |
| Stop | installed immediately after a positive fill | installed in the fill event; entry-bar low tested before any trail advance |

Shared lifecycle gates: ≤ 2 attempts per symbol per rolling 10 minutes, shared by both BUYs, never on consecutive minutes; the attempt is recorded immediately before submission (so rejected/unfilled orders count); a refused signal does not consume the VWAP cycle; the cycle is consumed only on submission; no BUY at or after 11:30 ET.

**Result: no material mismatch with the four Markdown specifications; no BUY rule changed.** Points left as-is because the specification is silent or the replay is deliberately conservative:

1. Historical ORB treats a crossing bar that also trades below ORB low as breached (intra-bar order unknown).
2. A bar completing at 11:30:00 ET (the 11:29 minute) cannot generate a historical entry; its market order would be submitted at/after 11:30.
3. The VWAP cycle's $2.00 dip test keeps applying after the first confirmation, and the second green has no time limit other than the session/cutoff; the specification states neither.
4. Replay freezes the SELL VWAP from the signal bar (documented replay limitation), not the fill bar's completed OHLCV.
5. ATR is the simple mean of 14 true ranges from 15 completed daily bars (the specified Wilder seed).

## Comparing with future `mike_*` strategies

`campaign_manifest.json` → `comparison_contract` fixes the shared frame (dataset IDs, window, seed, capital, execution, EOD policy, entry-cycle limit, short settings). A `mike_*` job file may differ from the Michelle jobs **only** in `strategy`, `buy_strategies`, `sell_strategies`, `short_strategies`, `cover_strategies`, `parameters`, `protective_stop`. Check a candidate file and list its rule fields with:

```
python validate_campaign.py --experiments experiments.json --manifest campaign_manifest.json \
  --candidate <mike_experiments.json>
```

Sizing, risk limits, next-bar-open fills and the 11:30 cutoff are runner behavior, not job fields: run Mike jobs at the same code revision (each run records `git_commit` in `run_info.json`) or declare the difference. Use `WORKERS=4` for both.

## Run

```
nohup research/campaigns/michelle_zero_slippage_oct2026/run_michelle_zero_slippage.sh > /dev/null 2>&1 &
```

The script writes a new `research/results/michelle_zero_slippage_oct2026/run-<UTC>-<pid>/` directory (never touching existing results), rebuilds the research image, validates the jobs and the installed container code (fail-closed), runs the nine jobs with `WORKERS` (default 4), and writes `comparison.md|json|csv`, `run.log`, `run_info.json`, `batch.json`, `executions/`, and `trade_ledgers/`. It does not start or restart trading services and never reads the holdout. `comparison.md` lists effective execution assumptions (checked against the required zeros), completed/failed jobs, trades, win rate, P&L, profit factor, drawdown, exit-reason counts, atomic entry rejections, and the original 5 bps result side by side (the latter also reflects the ORB safety fix).
