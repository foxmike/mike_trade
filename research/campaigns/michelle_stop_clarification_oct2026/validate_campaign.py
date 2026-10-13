"""Fail-closed validation of the stop-clarification jobs and of the code that will run them.

    validate_campaign.py --experiments E --manifest M --code-check [--code-hashes H.json]

``--code-check`` imports the *installed* ``mike_trade`` and verifies the fill-based stop rule;
``--code-hashes`` additionally requires each listed source file to be byte-identical to the working
tree the launcher hashed, so a stale image cannot run the experiments.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from decimal import Decimal as D
from pathlib import Path

from mike_trade.research.commands import ExperimentSpec

SYMBOLS = ("IBM", "NVDA", "TSLA")
CONFIGURATIONS = ("michelle_orb", "michelle_vwap", "michelle_combined")
MICHELLE_IDS = {
    "five_minute_orb_michelle_buy", "five_minute_orb_michelle_sell",
    "vwap_bounce_break_michelle_buy", "vwap_bounce_break_michelle_sell",
}


def check_jobs(jobs: list[dict], manifest: dict) -> list[str]:
    errors = []
    if len(jobs) != 9 or {(j["symbol"], j["strategy"]) for j in jobs} != {
        (s, c) for s in SYMBOLS for c in CONFIGURATIONS
    }:
        errors.append("jobs are not exactly {IBM,NVDA,TSLA} x {orb,vwap,combined}")
    frame = manifest["comparison_contract"]
    for job in jobs:
        label = f"{job['symbol']}/{job['strategy']}"
        if job["execution"] != manifest["expected_execution"]:
            errors.append(f"{label}: execution {job['execution']} is not all zero")
        if job["dataset_id"] != frame["datasets"][job["symbol"]]:
            errors.append(f"{label}: unexpected dataset {job['dataset_id']}")
        if job["start"] != manifest["observation_start"] or job["end"] != manifest["development_end_exclusive"]:
            errors.append(f"{label}: window is not the development window")
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
        except Exception as exc:
            errors.append(f"{label}: invalid ExperimentSpec: {exc}")
    return errors


def check_code(hashes: dict[str, str] | None) -> list[str]:
    errors = []
    try:
        from mike_trade.backtest.execution import HistoricalExecutionSimulator
        from mike_trade.research.commands import research_default_execution_assumptions
        from mike_trade.risk import michelle_stop
        from mike_trade.strategies.sell.five_minute_orb_michelle import FiveMinuteOrbMichelleSell
        from mike_trade.strategies.sell.vwap_bounce_break_michelle import VwapBounceBreakMichelleSell
    except ImportError as exc:
        return [f"the installed mike_trade lacks the fill-based stop code (stale image?): {exc}"]

    for key, value in research_default_execution_assumptions().items():
        if value != 0:
            errors.append(f"research default {key} is {value!r}, expected 0")
    if not hasattr(HistoricalExecutionSimulator, "require_entry_protection"):
        errors.append("fill-derived atomic entry protection is missing")
    if michelle_stop.MICHELLE_TRAIL_PERCENT != D("0.2"):
        errors.append(f"trail is {michelle_stop.MICHELLE_TRAIL_PERCENT}%, expected 0.2%")
    if michelle_stop.initial_stop(D("428.9944")) != D("428.75"):
        errors.append("initial stop is not fill - 0.25 rounded up to the tick")
    initial = michelle_stop.initial_stop(D("400"))
    if initial != D("399.75") or michelle_stop.trailing_candidate(D("401")) != D("400.19"):
        errors.append("spec example (fill 400 -> 399.75; high 401 -> 400.19) is not reproduced")
    if michelle_stop.next_stop(D("499.75"), D("499.75"), D("500.76")) != D("499.75"):
        errors.append("an equal tick-adjusted candidate activated the trail")
    if michelle_stop.next_stop(initial, D("401"), D("400")) != D("401"):
        errors.append("the stop moved downward")
    for cls in (FiveMinuteOrbMichelleSell, VwapBounceBreakMichelleSell):
        if cls.spec.version != "1.1.0":
            errors.append(f"{cls.spec.strategy_id} is version {cls.spec.version}, expected 1.1.0")
    if hashes:
        root = Path(importlib.util.find_spec("mike_trade").submodule_search_locations[0]).parent
        for rel, expected in sorted(hashes.items()):
            path = root / rel
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            if actual != expected:
                errors.append(f"installed {rel} differs from the working tree (stale image?)")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiments", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--code-check", action="store_true")
    parser.add_argument("--code-hashes", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    errors = check_jobs(json.loads(args.experiments.read_text())["experiments"], manifest)
    if args.code_check:
        hashes = json.loads(args.code_hashes.read_text()) if args.code_hashes else None
        errors += check_code(hashes)
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    if not errors:
        print("campaign validation passed" + (" (code check included)" if args.code_check else ""))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
