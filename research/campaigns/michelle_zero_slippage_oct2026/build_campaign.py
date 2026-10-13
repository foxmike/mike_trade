"""Derive the nine zero-slippage Michelle development jobs from the original annual campaign.

Every field of each job is copied from ``sip_annual_oct2026/experiments.json``; the only
permitted difference is the explicit ``execution`` mapping.  The reserved holdout is never
read for job content and is only used to prove it is excluded.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from mike_trade.research.commands import ExperimentSpec

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "sip_annual_oct2026"
SYMBOLS = ("IBM", "NVDA", "TSLA")
CONFIGURATIONS = ("michelle_orb", "michelle_vwap", "michelle_combined")
EXPECTED_EXECUTION = {
    "slippage_bps": "0",
    "slippage_ticks": "0",
    "fee_per_order": "0",
    "fee_per_share": "0",
    "fee_bps": "0",
}
#: Job fields a future ``mike_*`` experiment may change; everything else is the shared frame.
STRATEGY_RULE_FIELDS = (
    "strategy",
    "buy_strategies",
    "sell_strategies",
    "short_strategies",
    "cover_strategies",
    "parameters",
    "protective_stop",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build() -> None:
    source_jobs = json.loads((SOURCE / "experiments.json").read_text())["experiments"]
    holdout_jobs = json.loads((SOURCE / "holdout_experiments.json").read_text())["experiments"]
    source_manifest = json.loads((SOURCE / "campaign_manifest.json").read_text())
    audit = json.loads((SOURCE / "dataset_audit.json").read_text())

    jobs = []
    for symbol in SYMBOLS:
        for name in CONFIGURATIONS:
            matches = [j for j in source_jobs if j["symbol"] == symbol and j["strategy"] == name]
            if len(matches) != 1:
                raise ValueError(f"{symbol}/{name}: expected one source job, found {len(matches)}")
            job = copy.deepcopy(matches[0])
            if job["execution"].get("slippage_bps") != "5":
                raise ValueError(f"{symbol}/{name}: unexpected source execution {job['execution']}")
            job["execution"] = dict(EXPECTED_EXECUTION)
            changed = {k for k in job if job[k] != matches[0][k]}
            if changed != {"execution"}:
                raise ValueError(f"{symbol}/{name}: unexpected changed fields {sorted(changed)}")
            if job["dataset_id"] != audit["symbols"][symbol]["minute_dataset_id"]:
                raise ValueError(f"{symbol}: dataset id does not match the pinned audit")
            if job["end"] != source_manifest["development_end_exclusive"]:
                raise ValueError(f"{symbol}/{name}: not a development window")
            ExperimentSpec(**job).validate()
            jobs.append(job)

    holdout_starts = {j["start"] for j in holdout_jobs}
    assert len(jobs) == 9
    assert all(j["start"] not in holdout_starts for j in jobs), "holdout job leaked"
    assert all(j["end"] <= source_manifest["holdout_start"] for j in jobs), "holdout window leaked"

    (HERE / "experiments.json").write_text(json.dumps({"experiments": jobs}, indent=2) + "\n")

    frame_fields = sorted(set(jobs[0]) - set(STRATEGY_RULE_FIELDS))
    configurations = [c for c in source_manifest["configurations"] if c["name"] in CONFIGURATIONS]
    manifest = {
        "campaign": "michelle_zero_slippage_oct2026",
        "purpose": (
            "Specification-faithful Michelle ORB/VWAP backtests with no assumed slippage or fees. "
            "Zero assumed slippage is a research scenario, not a guarantee of live execution prices."
        ),
        "source_campaign": "sip_annual_oct2026",
        "source_experiments_sha256": sha256(SOURCE / "experiments.json"),
        "source_holdout_sha256": sha256(SOURCE / "holdout_experiments.json"),
        "observation_start": source_manifest["observation_start"],
        "development_end_exclusive": source_manifest["development_end_exclusive"],
        "holdout_start": source_manifest["holdout_start"],
        "holdout_jobs": 0,
        "experiments": len(jobs),
        "configurations": configurations,
        "datasets": {k: v for k, v in source_manifest["datasets"].items() if k in SYMBOLS},
        "expected_execution": EXPECTED_EXECUTION,
        "intended_changes_from_source": [
            "execution: slippage_bps 5 -> 0 (all cost assumptions explicit and zero)",
            "atomic rejection of Michelle entries whose required initial stop is not below the fill "
            "(commit ff9634a); the source campaign's four ORB jobs failed on this condition",
        ],
        "comparison_contract": {
            "shared_frame_fields": frame_fields,
            "strategy_rule_fields": list(STRATEGY_RULE_FIELDS),
            "frame_values": {
                field: jobs[0][field]
                for field in frame_fields
                if field not in ("symbol", "dataset_id")
            },
            "datasets": {s: audit["symbols"][s]["minute_dataset_id"] for s in SYMBOLS},
            "worker_default": 4,
            "notes": [
                "Position sizing, risk limits, next-bar-open fills, STOP_FIRST OHLC ambiguity and "
                "the Michelle 11:30 ET entry cutoff are engine/runner behavior at the recorded code "
                "revision, not job fields; compare Mike runs at the same revision or declare the delta.",
                "A mike_* job may differ from a Michelle job only in strategy_rule_fields.",
            ],
        },
    }
    (HERE / "campaign_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"validated {len(jobs)} jobs; wrote experiments.json and campaign_manifest.json")


if __name__ == "__main__":
    build()
