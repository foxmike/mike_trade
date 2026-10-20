# ruff: noqa: F811  (pytest fixtures are imported from test_model_workflow)
"""Operator CLI and Makefile surface for the model workflow (offline, no engine involved)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from test_model_workflow import (  # noqa: F401  (fixtures)
    MODELS,
    approved,
    campaign_yaml,
    workflow,
)

from mike_trade.cli import CANONICAL_COMMANDS, OFFLINE_COMMANDS, main

MODEL_COMMANDS = (
    "model-prepare",
    "model-train",
    "model-walk-forward",
    "model-evaluate",
    "model-inspect",
    "model-list",
    "model-compare",
    "model-report",
    "model-backtest",
    "model-backtest-compare",
    "model-approve",
    "model-pin",
    "model-check",
    "model-health",
)


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_every_model_command_is_canonical_offline_and_has_a_make_target():
    makefile = Path("Makefile").read_text()
    for name in MODEL_COMMANDS:
        assert name in CANONICAL_COMMANDS and name in OFFLINE_COMMANDS
        assert f"\n{name}:" in makefile
        dry = subprocess.run(
            [
                "make",
                "-n",
                name,
                "MODEL_ID=a",
                "MODEL_VERSION=1.0.0",
                "STAGE=shadow",
                "REASON=x",
                "MODELS=a@1.0.0",
                "REPORTS=r.json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert dry.returncode == 0, dry.stderr
        assert "-m mike_trade.cli " + name in dry.stdout
    # recipes only delegate: no business logic and no CONFIRM_LIVE default
    assert "CONFIRM_LIVE=YES" not in makefile and "--confirm-live YES" not in makefile


def test_listing_inspecting_reporting_and_pinning(approved, capsys):
    models = str(approved["models"])
    code, out, _ = run(capsys, "model-list", "--root", models)
    listed = json.loads(out)
    assert code == 0 and {m["model_id"] for m in listed} == set(MODELS.values())
    assert {m["approval"] for m in listed} == {"shadow"}
    code, out, _ = run(
        capsys, "model-inspect", "--root", models, "--model-id", "xgb_buy_t", "--version", "1.0.0"
    )
    assert code == 0 and json.loads(out)["approval"] == "shadow"
    code, out, _ = run(
        capsys, "model-report", "--root", models, "--model-id", "lgbm_sell_t", "--version", "1.0.0"
    )
    assert code == 0 and Path(json.loads(out)["markdown"]).read_text().startswith("# Model report")
    code, out, _ = run(
        capsys, "model-compare", "--root", models, "--models", "xgb_buy_t@1.0.0,lgbm_buy_t@1.0.0"
    )
    assert code == 0 and json.loads(out)["all_comparable"] is True
    code, out, _ = run(
        capsys,
        "model-pin",
        "--root",
        models,
        "--model-id",
        "xgb_buy_t",
        "--version",
        "1.0.0",
        "--name",
        "aapl_xgb_buy",
    )
    assert code == 0 and "plugin: xgboost_probability_buy" in out and "artifact_sha256:" in out
    code, _, err = run(
        capsys,
        "model-pin",
        "--root",
        models,
        "--model-id",
        "xgb_buy_t",
        "--version",
        "1.0.0",
        "--stage",
        "paper",
    )
    assert code == 3 and "not approved for 'paper'" in err


def test_approval_guards_on_the_command_line(approved, capsys):
    models = str(approved["models"])
    common = ("--root", models, "--model-id", "xgb_buy_t", "--version", "1.0.0", "--reason", "x")
    code, _, err = run(capsys, "model-approve", *common, "--stage", "live")
    assert code == 3 and "--confirm-live YES" in err
    code, _, err = run(capsys, "model-approve", *common, "--stage", "paper")
    assert code == 3 and "requires --evidence" in err
    code, _, err = run(capsys, "model-approve", *common, "--stage", "live", "--confirm-live", "YES")
    assert code == 3 and "'paper' approval must precede" in err


def test_backtest_dry_run_prints_the_plan_and_refuses_unsafe_windows(approved, capsys, tmp_path):
    campaign = campaign_yaml(approved, directory=tmp_path / "c")
    code, out, _ = run(capsys, "model-backtest", "--campaign", str(campaign), "--dry-run")
    assert "campaign preflight (effective settings)" in out
    plan = json.loads(out[out.index("{"):])
    assert code == 0 and plan["dry_run"] and plan["jobs"] == 8 and plan["feed"] == "iex"
    assert {m["sample_role"] for m in plan["models"].values()} == {"out_of_sample_development_test"}
    holdout = campaign_yaml(
        approved, directory=tmp_path / "h", window=("2026-09-21T00:00:00Z", "2026-09-30T00:00:00Z")
    )
    code, _, err = run(capsys, "model-backtest", "--campaign", str(holdout), "--dry-run")
    assert code == 3 and "holdout" in err
    assert not (tmp_path / "h" / "acceptance").exists()  # nothing was run or written


def test_prepare_walk_forward_and_retraining_through_the_cli(approved, capsys):
    config = str(approved["configs"][("xgboost", "buy")])
    code, out, _ = run(capsys, "model-prepare", "--config", config)
    prepared = json.loads(out)
    assert code == 0 and prepared["counts"]["fit"] > 0 and prepared["symbols"] == 3
    code, out, _ = run(capsys, "model-walk-forward", "--config", config, "--lock", prepared["lock"])
    assert code == 0 and json.loads(out)["folds"] == 2
    # the lock belongs to a different configuration -> refused, nothing stored
    other = str(approved["configs"][("lightgbm", "buy")])
    code, _, err = run(capsys, "model-train", "--config", other, "--lock", prepared["lock"])
    assert code == 5 and "different configuration" in err
    # an already-stored id@version is immutable
    code, _, err = run(capsys, "model-train", "--config", config, "--lock", prepared["lock"])
    assert code == 5 and "immutable" in err


def test_preflight_and_health_exit_codes(approved, capsys, tmp_path):
    code, out, _ = run(capsys, "model-health", "--db", str(tmp_path / "missing.sqlite3"))
    assert code == 5
    code, out, err = run(capsys, "model-check", "--config", str(tmp_path / "nope.yaml"))
    assert code == 5 and "failed" in err.lower()
