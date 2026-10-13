"""Fail-closed validation of the zero-slippage Michelle jobs, the research code, and future comparisons.

Usage:
    validate_campaign.py --experiments experiments.json --manifest campaign_manifest.json [--code-check]
    validate_campaign.py --experiments experiments.json --manifest campaign_manifest.json \
        --candidate mike_experiments.json      # future mike_* jobs vs the shared frame
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mike_trade.research.commands import ExperimentSpec

SYMBOLS = ("IBM", "NVDA", "TSLA")
CONFIGURATIONS = ("michelle_orb", "michelle_vwap", "michelle_combined")
MICHELLE_IDS = {
    "five_minute_orb_michelle_buy",
    "five_minute_orb_michelle_sell",
    "vwap_bounce_break_michelle_buy",
    "vwap_bounce_break_michelle_sell",
}


def load(path: Path) -> list[dict]:
    return json.loads(path.read_text())["experiments"]


def check_code() -> list[str]:
    """The installed (container) research code must carry the corrections."""
    from decimal import Decimal

    from mike_trade.backtest.execution import HistoricalExecutionSimulator
    from mike_trade.research.commands import research_default_execution_assumptions

    errors = []
    defaults = research_default_execution_assumptions()
    for key in ("slippage_bps", "slippage_ticks", "fee_per_order", "fee_per_share", "fee_bps"):
        if defaults.get(key) != Decimal(0):
            errors.append(f"research default {key} is {defaults.get(key)!r}, expected 0")
    if not hasattr(HistoricalExecutionSimulator, "require_entry_protection_below"):
        errors.append("atomic entry-protection rejection is missing from the installed code")
    return errors


def check_michelle(jobs: list[dict], manifest: dict) -> list[str]:
    errors = []
    if len(jobs) != 9:
        errors.append(f"expected 9 experiments, found {len(jobs)}")
    expected_pairs = {(s, c) for s in SYMBOLS for c in CONFIGURATIONS}
    if {(j["symbol"], j["strategy"]) for j in jobs} != expected_pairs or len(jobs) != len(expected_pairs):
        errors.append("jobs are not exactly {IBM,NVDA,TSLA} x {orb,vwap,combined}")
    frame = manifest["comparison_contract"]
    for job in jobs:
        label = f"{job['symbol']}/{job['strategy']}"
        if job["execution"] != manifest["expected_execution"]:
            errors.append(f"{label}: execution {job['execution']} != {manifest['expected_execution']}")
        if job["dataset_id"] != frame["datasets"][job["symbol"]]:
            errors.append(f"{label}: unexpected dataset {job['dataset_id']}")
        if job["start"] != manifest["observation_start"] or job["end"] != manifest["development_end_exclusive"]:
            errors.append(f"{label}: window {job['start']}..{job['end']} is not the development window")
        if job["end"] > manifest["holdout_start"]:
            errors.append(f"{label}: overlaps the reserved holdout")
        ids = set(job["buy_strategies"]) | set(job["sell_strategies"] or [])
        if not ids <= MICHELLE_IDS:
            errors.append(f"{label}: non-Michelle strategies {sorted(ids - MICHELLE_IDS)}")
        for key, value in frame["frame_values"].items():
            if job.get(key) != value:
                errors.append(f"{label}: {key}={job.get(key)!r} differs from the shared frame")
        try:
            ExperimentSpec(**job).validate()
        except Exception as exc:  # report every failure, not just the first
            errors.append(f"{label}: invalid ExperimentSpec: {exc}")
    return errors


def compare_candidate(candidate: list[dict], manifest: dict) -> tuple[list[str], list[str]]:
    contract = manifest["comparison_contract"]
    errors, lines = [], []
    for job in candidate:
        label = f"{job['symbol']}/{job['strategy']}"
        if job["dataset_id"] != contract["datasets"].get(job["symbol"]):
            errors.append(f"{label}: dataset {job['dataset_id']} is not the pinned dataset")
        for key, value in contract["frame_values"].items():
            if job.get(key) != value:
                errors.append(f"{label}: frame field {key}={job.get(key)!r} != {value!r}")
        rules = {k: job.get(k) for k in contract["strategy_rule_fields"] if job.get(k) is not None}
        lines.append(f"{label}: strategy-rule fields = {json.dumps(rules, sort_keys=True)}")
    return errors, lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiments", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--code-check", action="store_true")
    parser.add_argument("--candidate", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    errors = check_michelle(load(args.experiments), manifest)
    if args.code_check:
        errors += check_code()
    if args.candidate:
        extra, lines = compare_candidate(load(args.candidate), manifest)
        errors += extra
        print("\n".join(lines))
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    if not errors:
        print("campaign validation passed"
              + (" (code check included)" if args.code_check else ""))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
