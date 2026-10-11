"""Configuration and composition of SHORT/COVER strategies.

Shorting is opt-in and fails closed: short strategies need ``execution.short_selling_enabled``,
LIVE accounts can never enable it, a strategy must be configured in its own role, and the six
registered SHORT/COVER strategies actually compose into a runnable SHADOW runtime.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from mike_trade.app import (
    AppSettings,
    ShadowScenarioSettings,
    StrategyAssignment,
    SymbolStrategySettings,
    _fingerprint,
    build_runtime,
    load_app_settings,
)
from mike_trade.controls.config_commands import ConfigValidationError, load_effective_config
from mike_trade.domain.errors import ConfigurationError
from mike_trade.runtime import ExecutionMode

REFERENCE = Path(__file__).resolve().parents[2] / "config" / "reference_vwap_shadow.yaml"
SHORT_PLUGINS = ("short_ema_bearish_crossover", "short_supertrend_bearish_flip",
                 "short_donchian_breakdown")
COVER_PLUGINS = ("cover_ema_bullish_crossover", "cover_supertrend_bullish_flip",
                 "cover_donchian_breakout")


def _document(short: bool = True) -> dict:
    doc = yaml.safe_load(REFERENCE.read_text())
    doc["strategies"].update({
        "short_ema": {"plugin": "short_ema_bearish_crossover", "side": "short", "parameters": {}},
        "cover_ema": {"plugin": "cover_ema_bullish_crossover", "side": "cover", "parameters": {}},
    })
    entry = next(iter(doc["symbols"].values()))
    entry["short_strategies"] = ["short_ema"]
    entry["cover_strategies"] = ["cover_ema"]
    doc["execution"]["short_selling_enabled"] = short
    return doc


def _load(tmp_path: Path, doc: dict) -> dict:
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(doc))
    return load_effective_config(path, environ={})


def test_short_and_cover_collections_normalize_when_shorting_is_enabled(tmp_path):
    effective = _load(tmp_path, _document())
    symbol = next(iter(effective["symbols"].values()))
    assert symbol["short_strategies"] == ["short_ema"]
    assert symbol["cover_strategies"] == ["cover_ema"]
    assert symbol["buy_strategies"]  # BUY/SELL are independent of SHORT/COVER
    assert effective["execution"]["short_selling_enabled"] is True


def test_short_strategies_require_the_explicit_short_selling_switch(tmp_path):
    with pytest.raises(ConfigValidationError, match="short_selling_enabled"):
        _load(tmp_path, _document(short=False))


def test_a_short_only_symbol_needs_no_buy_strategy_but_a_symbol_needs_some_entry(tmp_path):
    doc = _document()
    entry = next(iter(doc["symbols"].values()))
    del entry["buy_strategy"]
    assert next(iter(_load(tmp_path, doc)["symbols"].values()))["buy_strategies"] == []
    del entry["short_strategies"]
    with pytest.raises(ConfigValidationError, match="at least one buy"):
        _load(tmp_path, doc)


def test_a_strategy_must_be_configured_in_its_own_role(tmp_path):
    doc = _document()
    entry = next(iter(doc["symbols"].values()))
    entry["short_strategies"] = ["cover_ema"]       # a COVER strategy listed as SHORT
    with pytest.raises(ConfigValidationError, match="does not reference a short strategy"):
        _load(tmp_path, doc)
    doc = _document()
    doc["strategies"]["short_ema"]["side"] = "hold"
    with pytest.raises(ConfigValidationError, match="expected buy, sell, short, or cover"):
        _load(tmp_path, doc)


def test_live_accounts_can_never_enable_short_selling(tmp_path):
    doc = _document()
    doc["execution"].update(account_mode="live", live_trading_enabled=True)
    with pytest.raises(ConfigValidationError, match="short selling is not available for live"):
        _load(tmp_path, doc)


def test_settings_carry_short_cover_collections_and_the_scenario_switch(tmp_path):
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(_document()))
    settings = load_app_settings(path, environ={})
    assert settings.short_selling_enabled is True
    item = settings.symbol_strategies[0]
    assert [a.plugin for a in item.shorts] == ["short_ema_bearish_crossover"]
    assert [a.plugin for a in item.covers] == ["cover_ema_bullish_crossover"]
    assert all(s.short_selling_enabled for s in settings.shadow_scenarios)
    off = tmp_path / "off.yaml"
    document = copy.deepcopy(_document())
    document["execution"]["short_selling_enabled"] = True
    for entry in document["symbols"].values():
        entry.pop("short_strategies"), entry.pop("cover_strategies")
    off.write_text(yaml.safe_dump(document))
    assert load_app_settings(off, environ={}).symbol_strategies[0].shorts == ()


def test_the_config_fingerprint_changes_only_when_shorting_is_configured():
    base = AppSettings(
        mode=ExecutionMode.SHADOW, symbols=("SPY",),
        shadow_scenarios=(ShadowScenarioSettings(),),
        symbol_strategies=(SymbolStrategySettings(
            "SPY", (StrategyAssignment("b", "momentum_breakout"),)),))
    with_short = AppSettings(
        mode=ExecutionMode.SHADOW, symbols=("SPY",),
        shadow_scenarios=(ShadowScenarioSettings(short_selling_enabled=True),),
        symbol_strategies=(SymbolStrategySettings(
            "SPY", (StrategyAssignment("b", "momentum_breakout"),),
            shorts=(StrategyAssignment("s", "short_ema_bearish_crossover"),)),))
    assert _fingerprint(base) == _fingerprint(base)
    assert _fingerprint(base) != _fingerprint(with_short)  # drift is fingerprinted and blocks


def test_all_six_strategies_are_registered_and_compose_into_a_shadow_runtime(tmp_path):
    settings = AppSettings(
        mode=ExecutionMode.SHADOW, db_path=tmp_path / "t.sqlite3", symbols=("SPY",),
        shadow_scenarios=(ShadowScenarioSettings(short_selling_enabled=True),),
        symbol_strategies=(SymbolStrategySettings(
            "SPY", (StrategyAssignment("buy", "momentum_breakout"),),
            shorts=tuple(StrategyAssignment(p, p) for p in SHORT_PLUGINS),
            covers=tuple(StrategyAssignment(p, p) for p in COVER_PLUGINS)),))
    runtime = build_runtime(settings)
    collections = runtime.lifecycle.collections("SPY")
    assert [b.strategy_id for b in collections.shorts] == sorted(SHORT_PLUGINS)
    assert [b.strategy_id for b in collections.covers] == sorted(COVER_PLUGINS)
    assert {b.role for b in collections.shorts} == {"short"}
    assert {b.role for b in collections.covers} == {"cover"}


def test_a_strategy_bound_in_the_wrong_role_fails_closed_at_build_time(tmp_path):
    settings = AppSettings(
        mode=ExecutionMode.SHADOW, db_path=tmp_path / "t.sqlite3", symbols=("SPY",),
        shadow_scenarios=(ShadowScenarioSettings(short_selling_enabled=True),),
        symbol_strategies=(SymbolStrategySettings(
            "SPY", (StrategyAssignment("buy", "momentum_breakout"),),
            shorts=(StrategyAssignment("oops", "cover_ema_bullish_crossover"),)),))
    with pytest.raises(ConfigurationError) as raised:
        build_runtime(settings)
    assert raised.value.code == "STRATEGY_SIDE_MISMATCH"
