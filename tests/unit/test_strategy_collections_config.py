"""Configuration of per-symbol BUY/SELL strategy collections and stop policies."""

from __future__ import annotations

import copy
from decimal import Decimal as D
from pathlib import Path

import pytest
import yaml

from mike_trade.app import load_app_settings
from mike_trade.controls.config_commands import ConfigValidationError, load_effective_config
from mike_trade.domain.errors import ConfigurationError
from mike_trade.runtime import ExecutionMode

REFERENCE = Path(__file__).resolve().parents[2] / "config" / "reference_vwap_shadow.yaml"


def _document() -> dict:
    return yaml.safe_load(REFERENCE.read_text())


def _strategies(doc: dict) -> None:
    doc["strategies"].update(
        {
            "momentum_default": {"plugin": "momentum_breakout", "side": "buy", "parameters": {}},
            "orb_default": {"plugin": "opening_range_breakout", "side": "buy", "parameters": {}},
            "momentum_exit": {"plugin": "momentum_reversal", "side": "sell", "parameters": {}},
        }
    )


def _write(tmp_path: Path, doc: dict) -> Path:
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


def _symbol(doc: dict) -> dict:
    return next(iter(doc["symbols"].values()))


def _load(tmp_path: Path, doc: dict) -> dict:
    return load_effective_config(_write(tmp_path, doc), environ={})


def test_singular_spelling_normalizes_to_one_element_collections(tmp_path):
    symbol = _symbol(_load(tmp_path, _document()))
    assert symbol["buy_strategies"] == [symbol["buy_strategy"]]
    assert symbol["sell_strategies"] == [symbol["sell_strategy"]]


def test_a_symbol_may_list_any_number_of_buy_and_sell_strategies(tmp_path):
    doc = _document()
    _strategies(doc)
    entry = _symbol(doc)
    del entry["buy_strategy"], entry["sell_strategy"]
    entry["buy_strategies"] = ["vwap_bounce_default", "momentum_default", "orb_default"]
    entry["sell_strategies"] = ["vwap_breakdown_default", "momentum_exit"]
    symbol = _symbol(_load(tmp_path, doc))
    assert symbol["buy_strategies"] == ["vwap_bounce_default", "momentum_default", "orb_default"]
    assert symbol["sell_strategies"] == ["vwap_breakdown_default", "momentum_exit"]
    assert symbol["buy_strategy"] == "vwap_bounce_default"  # first entry, for legacy readers


def test_sell_collection_may_be_empty_or_omitted_while_the_stop_policy_stays_required(tmp_path):
    doc = _document()
    entry = _symbol(doc)
    del entry["sell_strategy"]
    assert _symbol(_load(tmp_path, doc))["sell_strategies"] == []
    entry["sell_strategies"] = []
    assert _symbol(_load(tmp_path, doc))["sell_strategy"] is None
    del entry["stop_policy"]
    with pytest.raises(ConfigValidationError, match="stop_policy"):
        _load(tmp_path, doc)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda e: e.update(buy_strategies=["vwap_bounce_default"]), "alternatives"),
        (lambda e: (e.pop("buy_strategy"), e.update(buy_strategies=[])), "at least one buy"),
        (lambda e: (e.pop("buy_strategy"), e.pop("buy_strategies", None)), "at least one buy"),
        (lambda e: (e.pop("buy_strategy"),
                    e.update(buy_strategies=["vwap_bounce_default", "vwap_bounce_default"])),
         "duplicate buy strategies"),
        (lambda e: (e.pop("sell_strategy"), e.update(sell_strategies="vwap_breakdown_default")),
         "list of strategy names"),
        (lambda e: (e.pop("sell_strategy"), e.update(sell_strategies=["vwap_bounce_default"])),
         "does not reference a sell strategy"),
        (lambda e: (e.pop("buy_strategy"), e.update(buy_strategies=["missing_one"])),
         "does not reference a buy strategy"),
        (lambda e: (e.pop("buy_strategy"),
                    e.update(buy_strategies=["vwap_bounce_default", "vwap_bounce_other"])),
         "configures plugin 'vwap_bounce' twice"),
    ],
)
def test_strategy_collections_are_strictly_validated(tmp_path, mutate, message):
    doc = _document()
    doc["strategies"]["vwap_bounce_other"] = copy.deepcopy(doc["strategies"]["vwap_bounce_default"])
    mutate(_symbol(doc))
    with pytest.raises(ConfigValidationError, match=message):
        _load(tmp_path, doc)


def _multi_doc(tmp_path: Path) -> Path:
    doc = _document()
    _strategies(doc)
    base = _symbol(doc)
    base["buy_strategies"] = ["vwap_bounce_default", "momentum_default"]
    base["sell_strategies"] = ["vwap_breakdown_default", "momentum_exit"]
    del base["buy_strategy"], base["sell_strategy"]
    base["stop_policy"] = {"type": "native_trailing", "trail_percent": "1.5"}
    other = copy.deepcopy(base)
    other.update(buy_strategies=["orb_default"], sell_strategies=[])
    other["stop_policy"] = {"type": "fixed", "stop_amount": "1.25"}
    third = copy.deepcopy(base)
    third.update(buy_strategies=["momentum_default"], sell_strategies=["momentum_exit"])
    third["stop_policy"] = {"type": "stepped_trailing", "initial_stop_percent": "1.5",
                            "step_trigger_percent": "1.0", "step_percent": "0.5"}
    doc["symbols"] = {"NVDA": base, "IBM": other, "TSLA": third}
    for scenario in doc["shadow_scenarios"]:
        scenario["symbols"] = ["NVDA", "IBM", "TSLA"]
    doc["execution"]["account_mode"] = "shadow"
    return _write(tmp_path, doc)


def test_runtime_settings_carry_per_symbol_collections_and_stop_policies(tmp_path):
    settings = load_app_settings(_multi_doc(tmp_path), environ={})
    by_symbol = {item.symbol: item for item in settings.symbol_strategies}
    assert [a.plugin for a in by_symbol["NVDA"].buys] == ["vwap_bounce", "momentum_breakout"]
    assert [a.plugin for a in by_symbol["NVDA"].sells] == ["vwap_breakdown", "momentum_reversal"]
    assert [a.plugin for a in by_symbol["IBM"].buys] == ["opening_range_breakout"]
    assert by_symbol["IBM"].sells == ()
    assert settings.symbol_stop_policies["IBM"] == {"type": "fixed", "stop_amount": D("1.25")}
    assert settings.symbol_stop_policies["TSLA"]["type"] == "stepped_trailing"
    assert settings.symbol_stop_policies["NVDA"] == {
        "type": "native_trailing", "trail_percent": D("1.5")}
    assert settings.mode is ExecutionMode.SHADOW


def test_non_shadow_runtime_refuses_stop_policies_it_cannot_wire(tmp_path):
    path = _multi_doc(tmp_path)
    doc = yaml.safe_load(path.read_text())
    doc["execution"]["account_mode"] = "paper"
    with pytest.raises(ConfigurationError) as raised:
        load_app_settings(_write(tmp_path, doc), environ={})
    assert raised.value.code == "UNSUPPORTED_STOP_POLICY"


def test_legacy_reference_configuration_keeps_its_runtime_fingerprint(tmp_path):
    from mike_trade.app import AppSettings, _fingerprint

    settings = load_app_settings(REFERENCE, environ={})
    legacy = AppSettings(
        mode=settings.mode, db_path=settings.db_path, data_dir=settings.data_dir,
        symbols=settings.symbols, shadow_scenarios=settings.shadow_scenarios,
        total_budget=settings.total_budget, per_symbol_budget=settings.per_symbol_budget,
        entry_cutoff_minutes=settings.entry_cutoff_minutes,
        stop_trail_percent=settings.stop_trail_percent,
    )
    # The pre-collections fingerprint inputs: an unchanged config must not trip the
    # CONFIG_FINGERPRINT_CHANGED entry block after upgrade.
    assert _fingerprint(settings) == _fingerprint(legacy)
    changed = load_app_settings(_multi_doc(tmp_path), environ={})
    assert _fingerprint(changed) != _fingerprint(legacy)
