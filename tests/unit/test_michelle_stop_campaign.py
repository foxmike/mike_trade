"""Stop-clarification campaign: job identity, container-code checks, and the fill-based audit."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from decimal import Decimal as D
from pathlib import Path

import pytest
from test_michelle_strategies import _run_vwap_replay, bar

CAMPAIGNS = Path(__file__).resolve().parents[2] / "research" / "campaigns"
CAMPAIGN = CAMPAIGNS / "michelle_stop_clarification_oct2026"
PREVIOUS = CAMPAIGNS / "michelle_zero_slippage_oct2026"
BASELINE = (
    Path(__file__).resolve().parents[2]
    / "research/results/michelle_zero_slippage_oct2026/run-20261013T170714Z-3627337"
)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"stopcampaign_{name}", CAMPAIGN / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _jobs(path: Path) -> list[dict]:
    return json.loads(path.read_text())["experiments"]


def test_jobs_are_identical_to_the_previous_zero_slippage_campaign() -> None:
    assert _jobs(CAMPAIGN / "experiments.json") == _jobs(PREVIOUS / "experiments.json")
    manifest = json.loads((CAMPAIGN / "campaign_manifest.json").read_text())
    validator = _load("validate_campaign")
    assert validator.check_jobs(_jobs(CAMPAIGN / "experiments.json"), manifest) == []
    assert manifest["holdout_jobs"] == 0 and len(_jobs(CAMPAIGN / "experiments.json")) == 9
    for job in _jobs(CAMPAIGN / "experiments.json"):
        assert job["execution"] == manifest["expected_execution"]
        assert job["starting_capital"] == "10000" and job["max_entry_cycles_per_session"] == 10


def test_installed_code_check_passes_and_detects_a_stale_file(tmp_path) -> None:
    validator = _load("validate_campaign")
    manifest = json.loads((CAMPAIGN / "campaign_manifest.json").read_text())
    root = Path(__file__).resolve().parents[2]
    hashes = {rel: hashlib.sha256((root / rel).read_bytes()).hexdigest()
              for rel in manifest["stop_code_files"]}
    assert validator.check_code(hashes) == []
    stale = {**hashes, "mike_trade/backtest/runner.py": "0" * 64}
    assert any("runner.py" in e and "stale" in e for e in validator.check_code(stale))


def _artifact(tmp_path):
    result, _ = _run_vwap_replay(
        tmp_path,
        (
            bar(27, "100.20", "100.30", "100.10", "100.25"),
            bar(28, "100.25", "101.20", "100.20", "101.10"),
            bar(29, "101.10", "101.15", "100.90", "101.00"),
            bar(30, "101.00", "101.00", "99.00", "99.50"),
        ),
    )
    return {"cycles": result.cycles, "protection_events": result.protection_events,
            "fills": result.fills}


def test_audit_passes_a_real_replay_and_counts_cycleless_rejections(tmp_path) -> None:
    report = _load("report_results")
    artifact = _artifact(tmp_path)
    artifact["protection_events"] = [
        *artifact["protection_events"],
        {"event_type": "ENTRY_REJECTED_PROTECTION_INVALID", "candidate_entry_price": "0.25",
         "initial_stop_price": None, "order_id": "o1",
         "reason": "ENTRY_REJECTED_PROTECTIVE_STOP_NOT_BELOW_FILL"},
    ]
    rejected, audit = report.audit_artifact(artifact)
    assert rejected == 1 and audit["passed"], audit["errors"]
    assert audit["cycles"] == audit["initial_ok"] == 1
    assert audit["moves"] == 2 and audit["activation_ok"] == audit["monotonic_ok"] == 1


@pytest.mark.parametrize("tamper", ["old_initial", "half_percent", "downward", "stray_event"])
def test_audit_flags_genuine_stop_discrepancies(tmp_path, tamper) -> None:
    report = _load("report_results")
    artifact = copy.deepcopy(_artifact(tmp_path))
    events = artifact["protection_events"] = list(artifact["protection_events"])
    initial = next(e for e in events if e["event_type"] == "INITIAL_STOP_ESTABLISHED")
    moves = [e for e in events if e["event_type"] == "STOP_MOVED"]
    if tamper == "old_initial":
        initial["stop_price"] = D("99.70")  # a VWAP - 0.25 style level
    elif tamper == "half_percent":
        moves[-1]["new_stop_price"] = D("100.69")  # a 0.5% trail from 101.20 (0.2% is 100.99)
        artifact["cycles"][0]["final_stop_price"] = D("100.69")
    elif tamper == "downward":
        moves[-1]["new_stop_price"] = D("100.00")
    else:
        events.append({"event_type": "STOP_MOVED", "new_stop_price": D("1")})
    rejected, audit = report.audit_artifact(artifact)
    assert not audit["passed"] and audit["errors"]


@pytest.mark.skipif(not (BASELINE / "batch.json").is_file(), reason="baseline results absent")
def test_baseline_loads_all_nine_old_results_without_modifying_them() -> None:
    report = _load("report_results")
    before = (BASELINE / "batch.json").stat().st_mtime_ns
    baseline = report.load_baseline(BASELINE, _jobs(CAMPAIGN / "experiments.json"))
    assert len(baseline) == 9 and all(v["status"] == "completed" for v in baseline.values())
    assert baseline[("TSLA", "michelle_orb")]["trades"] == 172
    assert (BASELINE / "batch.json").stat().st_mtime_ns == before
