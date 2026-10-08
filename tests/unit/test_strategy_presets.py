"""Tests for the executable vwap_reference BUY/SELL pair and its shadow config.

Covers ``mike_trade/strategies/presets/pairs.py`` (the ``vwap_reference``
pair preset pairing the independently registered ``vwap_bounce`` BUY and
``vwap_breakdown`` SELL by ``(strategy_id, version)`` through configuration
only) and ``config/reference_vwap_shadow.yaml`` loaded through the normal
application configuration loader.

These tests never read real credentials (every loader call is passed an
explicit empty environment mapping, never ``os.environ``) and never contact
any broker (loader and preset validation are offline and side-effect free).
"""

from __future__ import annotations

import argparse
import dataclasses
import io
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from mike_trade.controls.config_commands import (
    ConfigValidationError,
    SecretInConfigError,
    _load_effective,
    register_config_commands,
    run_config_command,
)
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.strategies.presets.pairs import (
    VWAP_REFERENCE,
    PairPreset,
    PairPresetError,
    resolve_pair_preset,
    validate_pair_preset,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_CONFIG = REPO_ROOT / "config" / "reference_vwap_shadow.yaml"
EXAMPLE_PROVIDERS = REPO_ROOT / "config" / "providers.example.yaml"
BUY_MODULE = REPO_ROOT / "mike_trade" / "strategies" / "buy" / "vwap_bounce.py"
SELL_MODULE = REPO_ROOT / "mike_trade" / "strategies" / "sell" / "vwap_breakdown.py"

#: Credential/endpoint fragments that must never appear as config keys/values.
FORBIDDEN_KEY_TOKENS = ("api_key", "secret", "password", "token", "key_id", "private_key")
FORBIDDEN_VALUE_PREFIXES = ("http://", "https://", "wss://")

#: An explicitly empty environment: the loader resolves mode-relevant
#: credential names from this mapping only, so no real credentials are read.
EMPTY_ENVIRON: dict[str, str] = {}


def load_reference() -> dict:
    """Load the reference config through the normal loader with no environ."""
    settings, _secrets, _sources = _load_effective(
        str(REFERENCE_CONFIG), None, dict(EMPTY_ENVIRON), []
    )
    return settings


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mike-trade")
    subparsers = parser.add_subparsers(dest="subcommand")
    register_config_commands(subparsers)
    return parser


def run_cmd(argv: list[str], environ: dict) -> tuple[int, str, str]:
    args = make_parser().parse_args(argv)
    stdout, stderr = io.StringIO(), io.StringIO()
    code = run_config_command(args, environ=environ, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


# ---------------------------------------------------------------------------
# Pair preset resolution
# ---------------------------------------------------------------------------


def test_vwap_reference_resolves_registered_buy_and_sell_legs() -> None:
    resolved = resolve_pair_preset("vwap_reference")
    buy_cls = resolved["buy_cls"]
    sell_cls = resolved["sell_cls"]
    assert buy_cls.spec.strategy_id == "vwap_bounce"
    assert buy_cls.spec.version == "1.0.0"
    assert buy_cls.spec.side is StrategyRole.BUY
    assert sell_cls.spec.strategy_id == "vwap_breakdown"
    assert sell_cls.spec.version == "1.0.0"
    assert sell_cls.spec.side is StrategyRole.SELL
    assert resolved["preset"] is VWAP_REFERENCE
    assert dict(resolved["buy_params"]) == {}
    assert dict(resolved["sell_params"]) == {}
    assert resolved["exit_policy"] == "first_valid_exit"


def test_vwap_reference_carries_shadow_only_1m_trailing_contract() -> None:
    assert VWAP_REFERENCE.preset_id == "vwap_reference"
    assert VWAP_REFERENCE.buy_strategy_id == "vwap_bounce"
    assert VWAP_REFERENCE.sell_strategy_id == "vwap_breakdown"
    assert dict(VWAP_REFERENCE.buy_params) == {}
    assert dict(VWAP_REFERENCE.sell_params) == {}
    assert VWAP_REFERENCE.trigger_timeframe is CandleTimeframe.M1
    assert VWAP_REFERENCE.completed_bars_only is True
    assert set(VWAP_REFERENCE.allowed_modes) == {"shadow"}
    assert VWAP_REFERENCE.requires_trailing_protection is True
    assert VWAP_REFERENCE.exit_policy == "first_valid_exit"


def test_strategy_modules_do_not_import_each_other() -> None:
    buy_source = BUY_MODULE.read_text(encoding="utf-8")
    sell_source = SELL_MODULE.read_text(encoding="utf-8")
    assert "vwap_breakdown" not in buy_source
    assert "vwap_bounce" not in sell_source


# ---------------------------------------------------------------------------
# Fail-closed validation
# ---------------------------------------------------------------------------


def test_rejects_unknown_strategy_id() -> None:
    preset = dataclasses.replace(VWAP_REFERENCE, buy_strategy_id="no_such_strategy")
    with pytest.raises(PairPresetError):
        validate_pair_preset(preset)
    with pytest.raises(PairPresetError):
        resolve_pair_preset(preset)


def test_rejects_unknown_strategy_version() -> None:
    preset = dataclasses.replace(VWAP_REFERENCE, sell_version="9.9.9")
    with pytest.raises(PairPresetError):
        validate_pair_preset(preset)
    with pytest.raises(PairPresetError):
        resolve_pair_preset(preset)


def test_rejects_unknown_preset_id() -> None:
    with pytest.raises(PairPresetError):
        resolve_pair_preset("no_such_pair")


def test_rejects_side_swap() -> None:
    sell_in_buy_slot = dataclasses.replace(
        VWAP_REFERENCE,
        buy_strategy_id=VWAP_REFERENCE.sell_strategy_id,
        buy_version=VWAP_REFERENCE.sell_version,
    )
    with pytest.raises(PairPresetError):
        validate_pair_preset(sell_in_buy_slot)
    buy_in_sell_slot = dataclasses.replace(
        VWAP_REFERENCE,
        sell_strategy_id=VWAP_REFERENCE.buy_strategy_id,
        sell_version=VWAP_REFERENCE.buy_version,
    )
    with pytest.raises(PairPresetError):
        validate_pair_preset(buy_in_sell_slot)


def test_rejects_invalid_params() -> None:
    preset = dataclasses.replace(VWAP_REFERENCE, buy_params={"bogus_param": 1})
    with pytest.raises(PairPresetError):
        validate_pair_preset(preset)
    preset = dataclasses.replace(VWAP_REFERENCE, sell_params={"bogus_param": 1})
    with pytest.raises(PairPresetError):
        validate_pair_preset(preset)


@pytest.mark.parametrize("mode", ["paper", "live", "PAPER", "LIVE", " paper ", ""])
def test_rejects_paper_and_live_use(mode: str) -> None:
    with pytest.raises(PairPresetError):
        validate_pair_preset(VWAP_REFERENCE, mode=mode)
    with pytest.raises(PairPresetError):
        resolve_pair_preset(VWAP_REFERENCE, mode=mode)


def test_rejects_preset_whose_modes_admit_paper_or_live() -> None:
    widened = dataclasses.replace(
        VWAP_REFERENCE, allowed_modes=frozenset({"shadow", "paper"})
    )
    with pytest.raises(PairPresetError):
        validate_pair_preset(widened, mode="shadow")
    assert isinstance(VWAP_REFERENCE, PairPreset)


# ---------------------------------------------------------------------------
# Reference config through the normal loader
# ---------------------------------------------------------------------------


def test_reference_config_loads_shadow_through_normal_loader() -> None:
    settings = load_reference()
    execution = settings["execution"]
    assert execution["account_mode"] == "shadow"
    assert execution["live_trading_enabled"] is False

    scenarios = settings["shadow_scenarios"]
    assert len(scenarios) >= 1
    assert all(s["name"] for s in scenarios)
    assert any(s["enabled"] for s in scenarios)

    market_data = settings["market_data"]
    assert market_data["provider"] == "alpaca"
    assert market_data["feed"] == "iex"
    assert "1Min" in market_data["timeframes"]

    # Mandatory trailing protection: stops required, unprotected entries
    # blocked, and a trailing stop policy on the symbol.
    assert execution["stop_required"] is True
    assert execution["block_new_entries_when_unprotected"] is True
    assert len(settings["symbols"]) == 1
    symbol = next(iter(settings["symbols"].values()))
    assert symbol["stop_policy"]["type"] in ("native_trailing", "stepped_trailing")

    # Budgets are Decimal (quoted decimal strings in YAML).
    assert isinstance(settings["risk"]["total_application_budget"], Decimal)
    assert isinstance(settings["risk"]["daily_loss_limit"], Decimal)
    assert isinstance(symbol["budget"], Decimal)
    assert all(
        isinstance(s["starting_cash"], Decimal) for s in scenarios
    )

    # 1m completed-candle triggers for both legs.
    resolved = resolve_pair_preset(VWAP_REFERENCE, mode="shadow")
    assert resolved["buy_cls"].spec.trigger_timeframe is CandleTimeframe.M1
    assert resolved["sell_cls"].spec.trigger_timeframe is CandleTimeframe.M1
    assert resolved["buy_cls"].spec.completed_bars_only is True
    assert resolved["sell_cls"].spec.completed_bars_only is True

    # Existing config keys: minimum hold, max entry cycles, cooldown, cutoff.
    assert symbol["min_hold_seconds"] >= 0
    assert symbol["max_entry_cycles_per_session"] >= 1
    assert symbol["post_sale_cooldown_seconds"] >= 0
    assert execution["entry_cutoff"]

    # End-of-day close disabled by default.
    assert execution["end_of_day_close"]["enabled"] is False

    # The vwap_reference pair wiring.
    strategies = settings["strategies"]
    buys = [s for s in strategies.values() if s["side"] == "buy"]
    sells = [s for s in strategies.values() if s["side"] == "sell"]
    assert any(
        s["plugin"] == "vwap_bounce" and s["pair"] == "vwap_reference" for s in buys
    )
    assert any(
        s["plugin"] == "vwap_breakdown" and s["pair"] == "vwap_reference"
        for s in sells
    )
    assert symbol["buy_strategy"] in strategies
    assert symbol["sell_strategy"] in strategies


def test_reference_config_validates_against_provider_catalog() -> None:
    code, out, err = run_cmd(
        [
            "config",
            "validate",
            "--config",
            str(REFERENCE_CONFIG),
            "--providers",
            str(EXAMPLE_PROVIDERS),
        ],
        dict(EMPTY_ENVIRON),
    )
    assert code == 0, err
    assert "valid" in out


def test_reference_config_show_reports_shadow_mode() -> None:
    code, out, err = run_cmd(
        ["config", "show", "--config", str(REFERENCE_CONFIG), "--section", "execution"],
        dict(EMPTY_ENVIRON),
    )
    assert code == 0, err
    assert "account_mode=shadow" in out.splitlines()[0]


def test_mutated_live_mode_is_rejected_by_loader(tmp_path: Path) -> None:
    text = REFERENCE_CONFIG.read_text(encoding="utf-8")
    assert "account_mode: shadow" in text
    path = tmp_path / "mike_trade.yaml"
    path.write_text(
        text.replace("account_mode: shadow", "account_mode: live"), encoding="utf-8"
    )
    with pytest.raises(ConfigValidationError):
        _load_effective(str(path), None, dict(EMPTY_ENVIRON), [])
    # The preset independently refuses live use.
    with pytest.raises(PairPresetError):
        validate_pair_preset(VWAP_REFERENCE, mode="live")


def test_mutated_live_credentials_are_rejected_by_loader(tmp_path: Path) -> None:
    text = REFERENCE_CONFIG.read_text(encoding="utf-8")
    injected = text.replace(
        "  broker: alpaca", "  broker: alpaca\n  api_key: FAKE-LIVE-KEY-DO-NOT-USE"
    )
    assert injected != text
    path = tmp_path / "mike_trade.yaml"
    path.write_text(injected, encoding="utf-8")
    with pytest.raises((SecretInConfigError, ConfigValidationError)):
        _load_effective(str(path), None, dict(EMPTY_ENVIRON), [])


def test_live_override_via_cli_is_refused() -> None:
    code, _out, err = run_cmd(
        [
            "config",
            "validate",
            "--config",
            str(REFERENCE_CONFIG),
            "--providers",
            str(EXAMPLE_PROVIDERS),
            "--set",
            "execution.account_mode=live",
        ],
        dict(EMPTY_ENVIRON),
    )
    assert code == 2
    assert err != ""


def test_reference_config_contains_no_credentials_or_endpoints() -> None:
    text = REFERENCE_CONFIG.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    assert isinstance(raw, dict)

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}" if path else str(key)
                assert isinstance(key, str), child
                lowered = key.lower()
                assert not any(token in lowered for token in FORBIDDEN_KEY_TOKENS), child
                walk(value, child)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, str):
            assert not any(
                node.strip().lower().startswith(prefix)
                for prefix in FORBIDDEN_VALUE_PREFIXES
            ), path

    walk(raw, "")


def _config_key_names(node: object) -> list[str]:
    """Collect every mapping key name in a parsed YAML document."""
    names: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            names.append(str(key))
            names.extend(_config_key_names(value))
    elif isinstance(node, list):
        for value in node:
            names.extend(_config_key_names(value))
    return names


def test_reference_config_declares_no_live_authorization_key() -> None:
    raw = yaml.safe_load(REFERENCE_CONFIG.read_text(encoding="utf-8"))
    assert "live_trading_enabled" not in _config_key_names(raw)
    assert load_reference()["execution"]["live_trading_enabled"] is False
    assert "account_mode: shadow" in REFERENCE_CONFIG.read_text(encoding="utf-8")


def test_shipped_files_carry_no_confidence_values_or_profitability_claims() -> None:
    pairs_source = (
        REPO_ROOT / "mike_trade" / "strategies" / "presets" / "pairs.py"
    ).read_text(encoding="utf-8")
    config_text = REFERENCE_CONFIG.read_text(encoding="utf-8")
    # No confidence field, score, or threshold is assigned anywhere (negating
    # language such as "no confidence score" is allowed and matches the
    # existing preset modules).
    for snippet in ("confidence =", "confidence:", "confidence_score", "confident"):
        assert snippet not in pairs_source.lower(), snippet
        assert snippet not in config_text.lower(), snippet
    for snippet in ("profitable", "expected return", "sharpe", "win rate"):
        assert snippet not in pairs_source.lower(), snippet
        assert snippet not in config_text.lower(), snippet
