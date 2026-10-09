"""Makefile target contract tests.

Verifies that every operator operation is exposed as a thin Make target.
The tests parse the Makefile text and inspect `make -n <target>` dry-run
output with dummy variables. Recipes are never executed, with one deliberate
exception: the read-only `make help` listing (grep/echo only, no side
effects) is executed to assert that every target appears in the help output.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from mike_trade.cli import CANONICAL_COMMANDS

REPO = Path(__file__).resolve().parents[2]
MAKEFILE = REPO / "Makefile"
OPERATIONS = REPO / "docs" / "OPERATIONS.md"

REQUIRED_TARGETS = [
    "install", "update", "doctor", "config-show",
    "providers", "provider-check", "broker-check", "data-check",
    "strategies", "strategy-info", "strategy-check", "strategy-enable",
    "strategy-disable", "strategy-set",
    "symbols", "symbols-add", "symbols-remove", "symbols-enable",
    "symbols-disable",
    "start", "stop", "restart", "status",
    "dashboard", "dashboard-start", "dashboard-stop", "dashboard-health",
    "health",
    "shadow-start", "shadow-stop", "shadow-list", "shadow-create",
    "shadow-reset",
    "paper-start", "paper-stop", "paper-status",
    "live-preflight", "live-enable", "live-start", "live-disable",
    "pause", "resume", "pause-symbol", "resume-symbol", "entries-disable",
    "entries-enable",
    "close", "close-all", "flatten",
    "trail-status", "trail-history", "trail-policy-show", "trail-policy-set",
    "trail-reconcile",
    "entry-limits", "entry-limits-reset", "cooldown-status",
    "stop-status", "stops-audit",
    "positions", "orders", "balance", "budget", "reconcile",
    "signals", "evaluations", "events", "logs",
    "report", "report-daily", "report-symbol", "report-strategy",
    "report-pairs", "report-shadow", "report-comparison", "report-export",
    "backtest", "backtest-status", "backtest-report", "backtest-list",
    "cache-status", "cache-refresh",
    "db-check", "db-backup", "db-restore-dry-run",
    "test", "test-unit", "test-integration", "test-paper", "lint",
    "typecheck",
    "service-install", "service-uninstall", "service-status",
    "help",
]

# Targets that intentionally do not map to a canonical CLI subcommand.
TOOLING_TARGETS = frozenset({
    "install", "update", "test", "test-unit", "test-integration",
    "test-paper", "lint", "typecheck", "help",
})

NON_TOOLING = [t for t in REQUIRED_TARGETS if t not in TOOLING_TARGETS]

# Markers that would indicate business logic inside a Make recipe.
BUSINESS_LOGIC_MARKERS = (
    "python -c",
    "sqlite3",
    "curl",
    "alpaca",
    "select ",
    "insert ",
    "delete from",
    "$(( ",
)

DUMMY_VARS = {
    "ACCOUNT": "DUMMYACCT",
    "SCENARIO": "DUMMYSCEN",
    "SYMBOL": "DUMMYSYM",
    "STRATEGY": "DUMMYSTRAT",
    "KEY": "DUMMYKEY",
    "VALUE": "DUMMYVAL",
    "SINCE": "2024-01-01",
    "UNTIL": "2024-01-02",
    "LIMIT": "5",
    "FORMAT": "json",
    "OUT": "/tmp/dummy-out.json",
    "RUN_ID": "DUMMYRUN",
    "CONFIRM_LIVE": "NO",
}

MAKE = shutil.which("make")
needs_make = pytest.mark.skipif(MAKE is None, reason="make is not available")


def _read_makefile() -> str:
    return MAKEFILE.read_text(encoding="utf-8")


def _parse_targets(text: str) -> dict[str, str]:
    """Map target name -> description from `name: ... ## description` lines."""
    found: dict[str, str] = {}
    pattern = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*:([^=#]*?)##\s+(.+)$")
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            found[match.group(1)] = match.group(3).strip()
    return found


def _parse_phony(text: str) -> set[str]:
    names: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(".PHONY:"):
            names.update(stripped.split(":", 1)[1].split())
    return names


def _recipe_block(text: str, target: str) -> str:
    """Raw recipe lines (tab-indented) belonging to *target*."""
    lines = text.splitlines()
    block: list[str] = []
    in_target = False
    header = re.compile(r"^" + re.escape(target) + r"\s*:.*##")
    for line in lines:
        if not in_target:
            if header.match(line):
                in_target = True
            continue
        if line.startswith("\t"):
            block.append(line)
            continue
        if not line.strip():
            continue
        break
    return "\n".join(block)


def _dry_run(target: str) -> str:
    """`make -n <target>` expanded output (dry run; never executes recipes)."""
    assert MAKE is not None
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "MAKEFLAGS": ""}
    command = [MAKE, "-n", target]
    for key, value in DUMMY_VARS.items():
        command.append(f"{key}={value}")
    completed = subprocess.run(
        command, cwd=REPO, capture_output=True, text=True, env=env,
        timeout=60, check=False,
    )
    assert completed.returncode == 0, (
        f"make -n {target} failed: {completed.stderr}"
    )
    return completed.stdout


def test_makefile_defines_all_required_targets_with_descriptions():
    text = _read_makefile()
    targets = _parse_targets(text)
    phony = _parse_phony(text)
    for name in REQUIRED_TARGETS:
        assert name in targets, f"target missing: {name}"
        assert targets[name], f"target has empty description: {name}"
        assert name in phony, f"target is not .PHONY: {name}"


def test_help_is_default_goal():
    text = _read_makefile()
    assert re.search(r"^\.DEFAULT_GOAL\s*:=\s*help\s*$", text, re.M) is not None


@needs_make
def test_help_lists_every_target():
    assert MAKE is not None
    completed = subprocess.run(
        [MAKE, "help"], cwd=REPO, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin"}, timeout=60, check=False,
    )
    assert completed.returncode == 0, f"make help failed: {completed.stderr}"
    plain = re.sub(r"\x1b\[[0-9;]*m", "", completed.stdout)
    listed = set(re.findall(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)\s", plain, re.M))
    for name in REQUIRED_TARGETS:
        assert name in listed, f"target missing from help output: {name}"


@needs_make
def test_non_tooling_targets_invoke_canonical_cli_subcommands():
    canonical = set(CANONICAL_COMMANDS)
    assert set(NON_TOOLING) == canonical, (
        f"non-tooling targets must match CANONICAL_COMMANDS exactly: "
        f"extra={set(NON_TOOLING) - canonical} missing={canonical - set(NON_TOOLING)}"
    )
    for name in NON_TOOLING:
        output = _dry_run(name)
        assert "-m mike_trade.cli" in output, f"{name}: recipe does not invoke the CLI"
        assert re.search(r"(?<!\S)" + re.escape(name) + r"(?!\S)", output) is not None, (
            f"{name}: dry-run output does not reference the same-named subcommand"
        )


@needs_make
def test_recipes_contain_no_business_logic():
    text = _read_makefile().lower()
    for marker in BUSINESS_LOGIC_MARKERS:
        assert marker not in text, f"Makefile contains business-logic marker: {marker!r}"
    for name in REQUIRED_TARGETS:
        if name == "help":
            continue
        output = _dry_run(name).lower()
        for marker in BUSINESS_LOGIC_MARKERS:
            assert marker not in output, (
                f"make -n {name} contains business-logic marker: {marker!r}"
            )


def test_no_recipe_confirms_live_by_default():
    text = _read_makefile()
    assert "CONFIRM_LIVE=YES" not in text
    assert "--confirm-live YES" not in text


@needs_make
def test_stop_and_flatten_are_distinct_subcommands():
    stop_output = _dry_run("stop")
    flatten_output = _dry_run("flatten")
    assert " stop" in stop_output.replace("\n", " ")
    assert " flatten" in flatten_output.replace("\n", " ")
    assert stop_output != flatten_output
    stop_block = _recipe_block(_read_makefile(), "stop")
    assert "flatten" not in stop_block
    assert "close-all" not in stop_block


@needs_make
def test_install_update_use_lock_tooling_only():
    for name in ("install", "update"):
        output = _dry_run(name)
        assert "uv " in output or output.strip().startswith("uv"), (
            f"{name} must use the dependency lock tool"
        )
        lowered = output.lower()
        for marker in BUSINESS_LOGIC_MARKERS:
            assert marker not in lowered


@needs_make
def test_test_targets_are_bounded_and_paper_is_credential_gated():
    unit = _dry_run("test-unit")
    assert "tests/unit" in unit
    integration = _dry_run("test-integration")
    assert "tests/integration" in integration
    default = _dry_run("test")
    assert "tests/unit" in default and "tests/integration" in default
    paper = _dry_run("test-paper")
    assert "paper" in paper.lower()
    assert "live" not in paper.lower(), "test-paper must never reference live credentials"
    assert "apca" not in paper.lower(), "test-paper must never reference live credentials"


@needs_make
def test_lint_and_typecheck_invoke_configured_linters():
    lint = _dry_run("lint")
    assert "ruff" in lint.lower()
    typecheck = _dry_run("typecheck")
    assert "mypy" in typecheck.lower()


def test_operations_doc_covers_every_target():
    docs = OPERATIONS.read_text(encoding="utf-8")
    for name in REQUIRED_TARGETS:
        assert f"make {name}" in docs, f"docs/OPERATIONS.md missing target: {name}"


def test_operations_doc_covers_required_concepts():
    docs = OPERATIONS.read_text(encoding="utf-8")
    assert "left in place" in docs, "stop semantics must state stops/positions are left in place"
    assert "entries-disable" in docs and "pause" in docs
    assert "continue while paused" in docs
    assert "CONFIRM_LIVE" in docs
    assert "nothing is queued" in docs
    assert "UNMANAGED" in docs
    assert "ESTIMATED" in docs
    assert "hand-loaded" in docs
    assert "client_order_id" in docs
    assert "OFFLINE" in docs
    assert "systemd" in docs
    assert "dry-run" in docs.lower() or "dry run" in docs.lower()
    assert "Ordinary tests never use live credentials." in docs
