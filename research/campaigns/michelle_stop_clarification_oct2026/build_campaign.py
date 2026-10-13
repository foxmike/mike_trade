"""Derive the nine Michelle stop-clarification jobs from the zero-slippage campaign.

The jobs are byte-for-byte the same experiment definitions as ``michelle_zero_slippage_oct2026``
(datasets, window, capital, sizing framework, entry-cycle limit, EOD, zero costs, seed). Only the
code under test differs: this campaign runs the fill-based $0.25 initial stop with a 0.2% trail.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mike_trade.research.commands import ExperimentSpec

HERE = Path(__file__).resolve().parent
PREVIOUS = HERE.parent / "michelle_zero_slippage_oct2026"
BASELINE_RUN = "research/results/michelle_zero_slippage_oct2026/run-20261013T170714Z-3627337"
SYMBOLS = ("IBM", "NVDA", "TSLA")
CONFIGURATIONS = ("michelle_orb", "michelle_vwap", "michelle_combined")
EXPECTED_EXECUTION = {
    "slippage_bps": "0",
    "slippage_ticks": "0",
    "fee_per_order": "0",
    "fee_per_share": "0",
    "fee_bps": "0",
}
#: Source files whose installed (container) copy must be byte-identical to the working tree.
STOP_CODE_FILES = (
    "mike_trade/risk/michelle_stop.py",
    "mike_trade/backtest/runner.py",
    "mike_trade/backtest/execution.py",
    "mike_trade/brokers/shadow/stops.py",
    "mike_trade/live/executor.py",
    "mike_trade/live/lifecycle.py",
    "mike_trade/app.py",
    "mike_trade/strategies/sell/_michelle_protective.py",
    "mike_trade/strategies/sell/five_minute_orb_michelle.py",
    "mike_trade/strategies/sell/vwap_bounce_break_michelle.py",
    "mike_trade/research/commands.py",
)
STRATEGY_RULE_FIELDS = (
    "strategy", "buy_strategies", "sell_strategies", "short_strategies", "cover_strategies",
    "parameters", "protective_stop",
)


def build() -> None:
    previous = json.loads((PREVIOUS / "experiments.json").read_text())["experiments"]
    previous_manifest = json.loads((PREVIOUS / "campaign_manifest.json").read_text())
    jobs = [dict(job) for job in previous]
    expected = {(s, c) for s in SYMBOLS for c in CONFIGURATIONS}
    if {(j["symbol"], j["strategy"]) for j in jobs} != expected or len(jobs) != 9:
        raise ValueError("previous campaign is not the expected nine Michelle jobs")
    for job in jobs:
        if job["execution"] != EXPECTED_EXECUTION:
            raise ValueError(f"{job['symbol']}/{job['strategy']}: execution is not all zero")
        ExperimentSpec(**job).validate()
    if jobs != previous:
        raise ValueError("jobs must equal the previous campaign's definitions")
    (HERE / "experiments.json").write_text(json.dumps({"experiments": jobs}, indent=2) + "\n")

    manifest = {
        "campaign": "michelle_stop_clarification_oct2026",
        "purpose": (
            "Isolate Michelle's confirmed stop clarification: fill-based $0.25 initial stop, "
            "0.2% trail, activation only above the initial stop. Zero assumed slippage and fees "
            "in both scenarios; zero slippage is a research scenario, not a live-price guarantee."
        ),
        "scenarios": {
            "OLD": "reference-based $0.25 initial stop (ORB high / fill VWAP), 0.5% trail, "
                   "activation 0.995*H > reference + $0.25",
            "NEW": "fill-based $0.25 initial stop (rounded up to tick), 0.2% trail, activation "
                   "when floor_tick(0.998*H) > initial stop",
        },
        "baseline_run": BASELINE_RUN,
        "baseline_experiments_sha256": hashlib.sha256(
            (PREVIOUS / "experiments.json").read_bytes()
        ).hexdigest(),
        "observation_start": previous_manifest["observation_start"],
        "development_end_exclusive": previous_manifest["development_end_exclusive"],
        "holdout_start": previous_manifest["holdout_start"],
        "holdout_jobs": 0,
        "experiments": len(jobs),
        "configurations": [
            {
                **configuration,
                "rationale": {
                    "michelle_orb": (
                        "Michelle ORB entry with fill-price-based $0.25 initial protection and "
                        "the shared 0.2% trailing policy."
                    ),
                    "michelle_vwap": (
                        "Michelle VWAP entry with fill-price-based $0.25 initial protection and "
                        "the shared 0.2% trailing policy."
                    ),
                    "michelle_combined": (
                        "Independent ORB and VWAP entries; the winning entry receives the same "
                        "fill-price-based $0.25 initial protection and 0.2% trailing policy."
                    ),
                }[configuration["name"]],
            }
            for configuration in previous_manifest["configurations"]
        ],
        "datasets": previous_manifest["datasets"],
        "expected_execution": EXPECTED_EXECUTION,
        "intended_differences_from_baseline": [
            "initial stop = actual BUY fill - $0.25 (was ORB high / fill VWAP - $0.25)",
            "trailing percent 0.2% (was 0.5%)",
            "activation when the tick-adjusted candidate strictly exceeds the initial stop "
            "(was 0.995*H > reference + $0.25)",
            "atomic entry rejection now tests the fill-derived stop (no reference-based rejection)",
        ],
        "unchanged": [
            "BUY signals, datasets, window, seed, $10,000 capital, 10 entry cycles per session",
            "sizing framework: $3,000 per-symbol budget, $30 planned-loss budget, generic 1.5% "
            "sizing reference (deliberately untouched)",
            "EOD liquidation 15:55 ET, NEXT_BAR_OPEN fills, STOP_FIRST ambiguity, gap handling",
        ],
        "stop_code_files": list(STOP_CODE_FILES),
        "comparison_contract": previous_manifest["comparison_contract"],
    }
    (HERE / "campaign_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"validated {len(jobs)} jobs identical to the previous campaign")


if __name__ == "__main__":
    build()
