"""Tests for mike_trade.controls.config_commands (read-only config CLI)."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

from mike_trade.controls.config_commands import (
    register_config_commands,
    run_config_command,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "mike_trade.example.yaml"
EXAMPLE_PROVIDERS = REPO_ROOT / "config" / "providers.example.yaml"

SENTINELS = {
    "MIKE_TRADE_ALPACA_DATA_KEY_ID": "FAKE-TEST-SECRET-DO-NOT-USE-DATA-KEY",
    "MIKE_TRADE_ALPACA_DATA_SECRET_KEY": "FAKE-TEST-SECRET-DO-NOT-USE-DATA-SECRET",
    "MIKE_TRADE_ALPACA_PAPER_KEY_ID": "FAKE-TEST-SECRET-DO-NOT-USE-PAPER-KEY",
    "MIKE_TRADE_ALPACA_PAPER_SECRET_KEY": "FAKE-TEST-SECRET-DO-NOT-USE-PAPER-SECRET",
    "MIKE_TRADE_ALPACA_LIVE_KEY_ID": "FAKE-TEST-SECRET-DO-NOT-USE-LIVE-KEY",
    "MIKE_TRADE_ALPACA_LIVE_SECRET_KEY": "FAKE-TEST-SECRET-DO-NOT-USE-LIVE-SECRET",
}

FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class RecordingEnviron(dict):
    """A dict environ that records every key read via [] or .get()."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads: list[str] = []

    def __getitem__(self, key):
        self.reads.append(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mike-trade")
    subparsers = parser.add_subparsers(dest="subcommand")
    register_config_commands(subparsers)
    return parser


def write_copies(tmp_path: Path) -> tuple[Path, Path]:
    config = tmp_path / "mike_trade.yaml"
    providers = tmp_path / "providers.yaml"
    config.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    providers.write_text(EXAMPLE_PROVIDERS.read_text(encoding="utf-8"), encoding="utf-8")
    return config, providers


def run_cmd(argv: list[str], environ: dict) -> tuple[int, str, str]:
    args = make_parser().parse_args(argv)
    stdout, stderr = io.StringIO(), io.StringIO()
    code = run_config_command(args, environ=environ, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


def base_argv(config: Path, *extra: str) -> list[str]:
    return ["config", *extra, "--config", str(config)]


# ---------------------------------------------------------------------------
# Registration surface
# ---------------------------------------------------------------------------


def test_only_read_only_subcommands_exist() -> None:
    for name in ("show", "validate", "fingerprint", "sources", "providers"):
        args = make_parser().parse_args(["config", name])
        assert args.config_subcommand == name
    for name in ("set", "edit", "apply", "diff", "history", "preview"):
        with pytest.raises(SystemExit):
            make_parser().parse_args(["config", name])


# ---------------------------------------------------------------------------
# validate on the example config + example catalog (d.1 + d.2 integration)
# ---------------------------------------------------------------------------


def test_validate_example_config_exits_0_with_fingerprint(tmp_path: Path) -> None:
    config, providers = write_copies(tmp_path)
    code, out, err = run_cmd(
        ["config", "validate", "--config", str(config), "--providers", str(providers)],
        dict(SENTINELS),
    )
    assert code == 0
    assert "valid" in out
    match = re.search(r"[0-9a-f]{64}", out)
    assert match is not None
    assert FINGERPRINT_RE.match(match.group(0))
    assert err == ""


# ---------------------------------------------------------------------------
# Sentinel secrets never leak in any subcommand or format
# ---------------------------------------------------------------------------


def _all_argv(config: Path, providers: Path) -> dict[str, list[str]]:
    return {
        "show-yaml": base_argv(config, "show"),
        "show-json": base_argv(config, "show", "--format", "json"),
        "show-trade-relevant": base_argv(config, "show", "--trade-relevant"),
        "show-trade-relevant-json": base_argv(
            config, "show", "--trade-relevant", "--format", "json"
        ),
        "show-section": base_argv(config, "show", "--section", "market_data"),
        "sources": base_argv(config, "sources"),
        "providers": ["config", "providers", "--config", str(config)],
        "fingerprint": base_argv(config, "fingerprint"),
        "validate": [
            "config", "validate", "--config", str(config),
            "--providers", str(providers),
        ],
    }


@pytest.mark.parametrize("case", sorted(_all_argv(Path("x"), Path("y")).keys()))
def test_sentinel_never_appears(tmp_path: Path, case: str, monkeypatch) -> None:
    config, providers = write_copies(tmp_path)
    # providers resolves its catalog via the CWD fallback for this case.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "providers.example.yaml").write_text(
        EXAMPLE_PROVIDERS.read_text(encoding="utf-8"), encoding="utf-8"
    )
    argv = _all_argv(config, providers)[case]
    code, out, err = run_cmd(argv, dict(SENTINELS))
    assert code == 0, f"{case}: {err}"
    for sentinel in SENTINELS.values():
        assert sentinel not in out, case
        assert sentinel not in err, case


def test_sentinel_never_appears_on_errors(tmp_path: Path) -> None:
    config, _providers = write_copies(tmp_path)
    bad = tmp_path / "bad.yaml"
    doc = yaml.safe_load(config.read_text(encoding="utf-8"))
    doc["bogus_top_level_key"] = True
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    code, out, err = run_cmd(["config", "show", "--config", str(bad)], dict(SENTINELS))
    assert code == 2
    for sentinel in SENTINELS.values():
        assert sentinel not in out
        assert sentinel not in err
    code, out, err = run_cmd(
        ["config", "show", "--config", str(config), "--set", "execution.account_mode=live"],
        dict(SENTINELS),
    )
    assert code == 2
    for sentinel in SENTINELS.values():
        assert sentinel not in out
        assert sentinel not in err


# ---------------------------------------------------------------------------
# show behavior
# ---------------------------------------------------------------------------


def test_show_header_has_mode_live_flag_and_fingerprint(tmp_path: Path) -> None:
    config, _providers = write_copies(tmp_path)
    code, out, _err = run_cmd(base_argv(config, "show"), dict(SENTINELS))
    assert code == 0
    header = out.splitlines()[0]
    assert "account_mode=shadow" in header
    assert "live_trading_enabled=false" in header
    assert re.search(r"[0-9a-f]{64}", header)


def test_show_annotates_iex_feed(tmp_path: Path) -> None:
    config, _providers = write_copies(tmp_path)
    code, out, _err = run_cmd(base_argv(config, "show"), dict(SENTINELS))
    assert code == 0
    assert "non-consolidated" in out
    assert "IEX" in out


def test_show_decimals_print_as_strings(tmp_path: Path) -> None:
    config, _providers = write_copies(tmp_path)
    code, out, _err = run_cmd(base_argv(config, "show", "--format", "json"), dict(SENTINELS))
    assert code == 0
    body_lines = [line for line in out.splitlines() if not line.startswith("#")]
    body = json.loads("\n".join(body_lines))
    assert body["risk"]["total_application_budget"] == "10000.00"
    assert body["symbols"]["SPY"]["budget"] == "3000.00"


def test_show_section_execution_only(tmp_path: Path) -> None:
    config, _providers = write_copies(tmp_path)
    code, out, _err = run_cmd(
        base_argv(config, "show", "--section", "execution"), dict(SENTINELS)
    )
    assert code == 0
    assert "account_mode" in out
    assert "warmup_bars" not in out
    assert "total_application_budget" not in out
    assert "market_data" not in out


def test_show_unknown_section_exits_2(tmp_path: Path) -> None:
    config, _providers = write_copies(tmp_path)
    code, out, err = run_cmd(
        base_argv(config, "show", "--section", "nope"), dict(SENTINELS)
    )
    assert code == 2
    assert "section" in err
    assert "Traceback" not in out + err


# ---------------------------------------------------------------------------
# fingerprint consistency
# ---------------------------------------------------------------------------


def test_fingerprint_matches_show_header_and_is_stable(tmp_path: Path) -> None:
    config, _providers = write_copies(tmp_path)
    code, out, _err = run_cmd(base_argv(config, "fingerprint"), dict(SENTINELS))
    assert code == 0
    digest = out.strip()
    assert FINGERPRINT_RE.match(digest)
    assert out.strip().splitlines() == [digest]
    code, show_out, _err = run_cmd(base_argv(config, "show"), dict(SENTINELS))
    assert code == 0
    assert digest in show_out.splitlines()[0]


def test_fingerprint_stable_under_key_reorder_but_moves_with_budget(
    tmp_path: Path,
) -> None:
    config, _providers = write_copies(tmp_path)
    _code, out, _err = run_cmd(base_argv(config, "fingerprint"), dict(SENTINELS))
    before = out.strip()
    doc = yaml.safe_load(config.read_text(encoding="utf-8"))
    reordered = {key: doc[key] for key in reversed(list(doc.keys()))}
    config.write_text(yaml.safe_dump(reordered, sort_keys=False), encoding="utf-8")
    _code, out, _err = run_cmd(base_argv(config, "fingerprint"), dict(SENTINELS))
    assert out.strip() == before
    code, out, _err = run_cmd(
        base_argv(config, "show", "--set", 'symbols.SPY.budget="2000.00"'),
        dict(SENTINELS),
    )
    assert code == 0
    _code, out, _err = run_cmd(
        base_argv(config, "fingerprint", "--set", 'symbols.SPY.budget="2000.00"'),
        dict(SENTINELS),
    )
    assert out.strip() != before


# ---------------------------------------------------------------------------
# --set overrides are output-only
# ---------------------------------------------------------------------------


def test_set_override_reflected_without_touching_files(tmp_path: Path) -> None:
    config, _providers = write_copies(tmp_path)
    before_files = set(p.name for p in tmp_path.iterdir())
    before_sha = hashlib.sha256(config.read_bytes()).hexdigest()
    before_mtime = config.stat().st_mtime_ns
    code, out, err = run_cmd(
        base_argv(config, "show", "--set", 'symbols.SPY.budget="2000.00"'),
        dict(SENTINELS),
    )
    assert code == 0, err
    assert "2000.00" in out
    assert hashlib.sha256(config.read_bytes()).hexdigest() == before_sha
    assert config.stat().st_mtime_ns == before_mtime
    assert set(p.name for p in tmp_path.iterdir()) == before_files


def test_set_live_account_mode_refused(tmp_path: Path) -> None:
    config, _providers = write_copies(tmp_path)
    code, out, err = run_cmd(
        base_argv(config, "show", "--set", "execution.account_mode=live"),
        dict(SENTINELS),
    )
    assert code == 2
    lowered = (out + err).lower()
    assert "live" in lowered
    assert "override" in lowered
    assert "Traceback" not in out + err


def test_set_live_trading_enabled_refused(tmp_path: Path) -> None:
    config, _providers = write_copies(tmp_path)
    code, _out, err = run_cmd(
        base_argv(config, "validate", "--set", "execution.live_trading_enabled=true"),
        dict(SENTINELS),
    )
    assert code == 2
    assert "override" in err.lower()


def test_env_account_mode_live_refused(tmp_path: Path) -> None:
    config, _providers = write_copies(tmp_path)
    environ = dict(SENTINELS)
    environ["MIKE_TRADE_ACCOUNT_MODE"] = "live"
    code, out, err = run_cmd(base_argv(config, "show"), environ)
    assert code == 2
    assert "override" in err.lower()


# ---------------------------------------------------------------------------
# Invalid and missing configs
# ---------------------------------------------------------------------------


def test_invalid_config_unknown_key_exits_2(tmp_path: Path) -> None:
    config, _providers = write_copies(tmp_path)
    doc = yaml.safe_load(config.read_text(encoding="utf-8"))
    doc["bogus_top_level_key"] = True
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(doc), encoding="utf-8")
    code, out, err = run_cmd(
        ["config", "validate", "--config", str(bad)], dict(SENTINELS)
    )
    assert code == 2
    assert "bogus_top_level_key" in err
    assert re.search(r"^bogus_top_level_key: .+", err, re.MULTILINE)
    assert "Traceback" not in out + err


def test_invalid_config_unquoted_float_rejected(tmp_path: Path) -> None:
    config, _providers = write_copies(tmp_path)
    text = config.read_text(encoding="utf-8").replace('budget: "3000.00"', "budget: 3000.00")
    bad = tmp_path / "bad_float.yaml"
    bad.write_text(text, encoding="utf-8")
    code, out, err = run_cmd(["config", "show", "--config", str(bad)], dict(SENTINELS))
    assert code == 2
    assert "quote" in err.lower()
    assert "Traceback" not in out + err


def test_missing_config_file_exits_2(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    code, out, err = run_cmd(["config", "show", "--config", str(missing)], dict(SENTINELS))
    assert code == 2
    assert "does-not-exist.yaml" in err
    assert "Traceback" not in out + err


def test_missing_providers_file_exits_2(tmp_path: Path, monkeypatch) -> None:
    config, _providers = write_copies(tmp_path)
    monkeypatch.chdir(tmp_path)
    code, out, err = run_cmd(["config", "validate", "--config", str(config)], {})
    assert code == 2
    assert "providers" in err.lower()
    assert "Traceback" not in out + err


# ---------------------------------------------------------------------------
# Offline behavior, defaults, mode isolation, import hygiene
# ---------------------------------------------------------------------------


def test_show_succeeds_with_nonexistent_data_dir(tmp_path: Path) -> None:
    config, _providers = write_copies(tmp_path)
    ghost = tmp_path / "no-such-data-dir"
    assert not ghost.exists()
    code, out, err = run_cmd(
        base_argv(config, "show", "--set", f"service.data_dir={ghost}"), dict(SENTINELS)
    )
    assert code == 0, err
    assert str(ghost) in out


def test_config_defaults_to_env_config_path(tmp_path: Path) -> None:
    config, _providers = write_copies(tmp_path)
    args = make_parser().parse_args(["config", "fingerprint"])
    stdout, stderr = io.StringIO(), io.StringIO()
    code = run_config_command(
        args, environ={"MIKE_TRADE_CONFIG": str(config), **SENTINELS},
        stdout=stdout, stderr=stderr,
    )
    assert code == 0
    assert FINGERPRINT_RE.match(stdout.getvalue().strip())


def test_providers_catalog_fallback_to_example(tmp_path: Path, monkeypatch) -> None:
    config, _providers = write_copies(tmp_path)
    work = tmp_path / "work"
    (work / "config").mkdir(parents=True)
    (work / "config" / "mike_trade.yaml").write_text(
        config.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (work / "config" / "providers.example.yaml").write_text(
        EXAMPLE_PROVIDERS.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.chdir(work)
    args = make_parser().parse_args(["config", "validate"])
    stdout, stderr = io.StringIO(), io.StringIO()
    code = run_config_command(args, environ=dict(SENTINELS), stdout=stdout, stderr=stderr)
    assert code == 0, stderr.getvalue()
    assert "valid" in stdout.getvalue()


def test_providers_reports_verdict_and_redacted_statuses(
    tmp_path: Path, monkeypatch
) -> None:
    config, _providers = write_copies(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "providers.example.yaml").write_text(
        EXAMPLE_PROVIDERS.read_text(encoding="utf-8"), encoding="utf-8"
    )
    code, out, _err = run_cmd(["config", "providers", "--config", str(config)],
                              dict(SENTINELS))
    assert code == 0
    assert "verdict" in out
    assert "ok: true" in out
    assert "https://paper-api.alpaca.markets" in out
    assert "MIKE_TRADE_ALPACA_DATA_KEY_ID" in out


def test_shadow_mode_never_reads_live_or_paper_env(tmp_path: Path) -> None:
    config, providers = write_copies(tmp_path)
    argv_map = _all_argv(config, providers)
    # providers/validate read the catalog from explicit paths here.
    argv_map["providers"] = [
        "config", "providers", "--config", str(config),
    ]
    for case, argv in sorted(argv_map.items()):
        environ = RecordingEnviron(SENTINELS)
        code, _out, err = run_cmd(argv, environ)
        assert code == 0, f"{case}: {err}"
        for key in environ.reads:
            assert "LIVE_" not in key, f"{case} read {key}"
            assert "PAPER_" not in key, f"{case} read {key}"


def test_sources_names_layers_without_values(tmp_path: Path) -> None:
    config, _providers = write_copies(tmp_path)
    code, out, _err = run_cmd(
        base_argv(config, "sources", "--set", 'symbols.SPY.budget="2000.00"'),
        dict(SENTINELS),
    )
    assert code == 0
    assert "built-in defaults" in out
    assert "main config file" in out
    assert str(config) in out
    assert "cli --set symbols.SPY.budget" in out
    for sentinel in SENTINELS.values():
        assert sentinel not in out


def test_no_broker_or_persistence_modules_imported(tmp_path: Path) -> None:
    before = set(sys.modules)
    config, providers = write_copies(tmp_path)
    for case, argv in sorted(_all_argv(config, providers).items()):
        code, _out, _err = run_cmd(argv, dict(SENTINELS))
        assert code == 0, case
    new_modules = set(sys.modules) - before
    offenders = [
        name for name in new_modules
        if "alpaca" in name or "brokers." in name or "persistence.transaction" in name
    ]
    assert offenders == []
