"""Tests for mike_trade.config.mutations."""

from __future__ import annotations

import hashlib
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from mike_trade.config.mutations import (
    REMOVE,
    ConcurrentConfigModificationError,
    ConfigAuditEntry,
    ConfigAuditFailedError,
    ConfigAuditSink,
    ConfigMutation,
    ConfigMutationValidationError,
    ExposureSnapshot,
    LiveEnableRefusedError,
    MutationPathError,
    MutationRequest,
    MutationResult,
    OpenExposure,
    SecretMutationRefusedError,
    UnsafeConfigMutationError,
    apply_config_mutation,
)

FIXED_NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def base_doc() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "execution": {
            "account_mode": "paper",
            "live_trading_enabled": False,
            "broker": "alpaca",
            "stop_required": True,
            "block_new_entries_when_unprotected": True,
        },
        "symbols": {
            "AAPL": {
                "enabled": True,
                "budget": 1000,
                "buy_strategy": "strat_a",
                "sell_strategy": "strat_a",
                "stop_policy": {"type": "trailing", "trail_pct": 5.0},
            }
        },
        "strategies": {"strat_a": {"kind": "momentum"}},
        "shadow_scenarios": [{"name": "small_account", "starting_cash": 10000}],
        "reporting": {"level": "summary"},
    }


def write_config(path: Path, doc: dict[str, Any]) -> bytes:
    raw = yaml.safe_dump(doc, sort_keys=False).encode("utf-8")
    path.write_bytes(raw)
    return raw


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class ListAudit(ConfigAuditSink):
    def __init__(self) -> None:
        self.entries: list[ConfigAuditEntry] = []

    def record(self, entry: ConfigAuditEntry) -> None:
        self.entries.append(entry)


def accept(mapping: Any) -> None:
    return None


def empty_exposure() -> ExposureSnapshot:
    return ExposureSnapshot(positions=(), known=True)


def aapl_exposure(**kwargs: Any) -> ExposureSnapshot:
    defaults: dict[str, Any] = {
        "symbol": "AAPL",
        "account_mode": "paper",
        "scenario": None,
        "sell_strategy": "strat_a",
        "has_active_protection": False,
        "has_unresolved_orders": False,
    }
    defaults.update(kwargs)
    return ExposureSnapshot(positions=(OpenExposure(**defaults),), known=True)


def make_request(*mutations: ConfigMutation, **kwargs: Any) -> MutationRequest:
    params: dict[str, Any] = {"mutations": tuple(mutations), "actor": "op", "reason": "test change"}
    params.update(kwargs)
    return MutationRequest(**params)


def test_successful_budget_change_is_atomic_and_audited(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    raw_before = write_config(cfg, base_doc())
    mode_before = stat.S_IMODE(os.stat(cfg).st_mode)
    audit = ListAudit()
    request = make_request(ConfigMutation("symbols.AAPL.budget", 2000))
    result = apply_config_mutation(
        cfg, request, validate=accept, exposure=empty_exposure(), audit=audit,
        clock=lambda: FIXED_NOW,
    )
    raw_after = cfg.read_bytes()
    assert raw_after != raw_before
    assert result.file_sha256_before == sha(raw_before)
    assert result.file_sha256_after == sha(raw_after)
    assert result.changed_paths == ("symbols.AAPL.budget",)
    assert yaml.safe_load(raw_after)["symbols"]["AAPL"]["budget"] == 2000
    assert stat.S_IMODE(os.stat(cfg).st_mode) == mode_before
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry.changed_paths == ("symbols.AAPL.budget",)
    assert entry.file_sha256_before == sha(raw_before)
    assert entry.file_sha256_after == sha(raw_after)
    assert entry.actor == "op"
    assert entry.occurred_at == FIXED_NOW
    assert entry.redacted_old_values["symbols.AAPL.budget"] == 1000
    assert entry.redacted_new_values["symbols.AAPL.budget"] == 2000
    assert result.audit_entry == entry


def test_stale_expected_sha_refused(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    raw_before = write_config(cfg, base_doc())
    audit = ListAudit()
    with pytest.raises(ConcurrentConfigModificationError):
        bad = make_request(
            ConfigMutation("symbols.AAPL.budget", 5), expected_file_sha256="0" * 64
        )
        apply_config_mutation(
            cfg, bad,
            validate=accept, exposure=empty_exposure(), audit=audit, clock=lambda: FIXED_NOW,
        )
    assert cfg.read_bytes() == raw_before
    assert audit.entries == []


def test_schema_version_change_refused(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    raw_before = write_config(cfg, base_doc())
    with pytest.raises(MutationPathError):
        apply_config_mutation(
            cfg, make_request(ConfigMutation("schema_version", 2)),
            validate=accept, exposure=empty_exposure(), audit=ListAudit(), clock=lambda: FIXED_NOW,
        )
    assert cfg.read_bytes() == raw_before


def test_secret_like_key_refused(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    raw_before = write_config(cfg, base_doc())
    with pytest.raises(SecretMutationRefusedError):
        apply_config_mutation(
            cfg, make_request(ConfigMutation("execution.api_key", "abc")),
            validate=accept, exposure=empty_exposure(), audit=ListAudit(), clock=lambda: FIXED_NOW,
        )
    assert cfg.read_bytes() == raw_before


def test_live_enablement_requires_confirm(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    write_config(cfg, base_doc())
    with pytest.raises(LiveEnableRefusedError) as exc_info:
        apply_config_mutation(
            cfg, make_request(ConfigMutation("execution.account_mode", "live")),
            validate=accept, exposure=empty_exposure(), audit=ListAudit(), clock=lambda: FIXED_NOW,
        )
    assert isinstance(exc_info.value, UnsafeConfigMutationError)
    assert exc_info.value.reasons
    with pytest.raises(LiveEnableRefusedError):
        apply_config_mutation(
            cfg, make_request(ConfigMutation("execution.live_trading_enabled", True)),
            validate=accept, exposure=empty_exposure(), audit=ListAudit(), clock=lambda: FIXED_NOW,
        )
    audit = ListAudit()
    result = apply_config_mutation(
        cfg, make_request(ConfigMutation("execution.account_mode", "live"), confirm_live=True),
        validate=accept, exposure=empty_exposure(), audit=audit, clock=lambda: FIXED_NOW,
    )
    assert result.changed_paths == ("execution.account_mode",)
    assert audit.entries[0].live_confirmed is True


def test_symbol_removal_or_disable_with_position_refused(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    raw_before = write_config(cfg, base_doc())
    exposure = aapl_exposure()
    with pytest.raises(UnsafeConfigMutationError):
        apply_config_mutation(
            cfg, make_request(ConfigMutation("symbols.AAPL", REMOVE)),
            validate=accept, exposure=exposure, audit=ListAudit(), clock=lambda: FIXED_NOW,
        )
    with pytest.raises(UnsafeConfigMutationError):
        apply_config_mutation(
            cfg, make_request(ConfigMutation("symbols.AAPL.enabled", False)),
            validate=accept, exposure=exposure, audit=ListAudit(), clock=lambda: FIXED_NOW,
        )
    assert cfg.read_bytes() == raw_before


def test_stop_type_change_refused_but_numeric_allowed(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    write_config(cfg, base_doc())
    exposure = aapl_exposure(has_active_protection=True)
    with pytest.raises(UnsafeConfigMutationError) as exc_info:
        apply_config_mutation(
            cfg, make_request(ConfigMutation("symbols.AAPL.stop_policy.type", "fixed")),
            validate=accept, exposure=exposure, audit=ListAudit(), clock=lambda: FIXED_NOW,
        )
    assert any("stop_policy" in reason for reason in exc_info.value.reasons)
    audit = ListAudit()
    result = apply_config_mutation(
        cfg, make_request(ConfigMutation("symbols.AAPL.stop_policy.trail_pct", 7.5)),
        validate=accept, exposure=exposure, audit=audit, clock=lambda: FIXED_NOW,
    )
    assert result.changed_paths == ("symbols.AAPL.stop_policy.trail_pct",)
    assert "forward" in audit.entries[0].reason.lower()


def test_sell_strategy_to_undefined_refused(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    write_config(cfg, base_doc())
    with pytest.raises(UnsafeConfigMutationError):
        apply_config_mutation(
            cfg, make_request(ConfigMutation("symbols.AAPL.sell_strategy", "nope")),
            validate=accept, exposure=aapl_exposure(), audit=ListAudit(), clock=lambda: FIXED_NOW,
        )


def test_removing_referenced_strategy_refused(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    raw_before = write_config(cfg, base_doc())
    with pytest.raises(UnsafeConfigMutationError):
        apply_config_mutation(
            cfg, make_request(ConfigMutation("strategies.strat_a", REMOVE)),
            validate=accept, exposure=aapl_exposure(), audit=ListAudit(), clock=lambda: FIXED_NOW,
        )
    assert cfg.read_bytes() == raw_before


def test_account_mode_change_with_position_refused(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    raw_before = write_config(cfg, base_doc())
    with pytest.raises(UnsafeConfigMutationError):
        apply_config_mutation(
            cfg, make_request(ConfigMutation("execution.account_mode", "shadow")),
            validate=accept, exposure=aapl_exposure(), audit=ListAudit(), clock=lambda: FIXED_NOW,
        )
    with pytest.raises(UnsafeConfigMutationError):
        apply_config_mutation(
            cfg, make_request(ConfigMutation("execution.broker", "other")),
            validate=accept, exposure=aapl_exposure(), audit=ListAudit(), clock=lambda: FIXED_NOW,
        )
    assert cfg.read_bytes() == raw_before


def test_removing_scenario_with_positions_refused(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    raw_before = write_config(cfg, base_doc())
    exposure = ExposureSnapshot(
        positions=(
            OpenExposure(symbol="AAPL", account_mode="shadow", scenario="small_account",
                         sell_strategy="strat_a"),
        ),
        known=True,
    )
    with pytest.raises(UnsafeConfigMutationError):
        apply_config_mutation(
            cfg, make_request(ConfigMutation("shadow_scenarios[small_account]", REMOVE)),
            validate=accept, exposure=exposure, audit=ListAudit(), clock=lambda: FIXED_NOW,
        )
    assert cfg.read_bytes() == raw_before


def test_stop_required_false_with_position_refused(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    raw_before = write_config(cfg, base_doc())
    with pytest.raises(UnsafeConfigMutationError):
        apply_config_mutation(
            cfg, make_request(ConfigMutation("execution.stop_required", False)),
            validate=accept, exposure=aapl_exposure(), audit=ListAudit(), clock=lambda: FIXED_NOW,
        )
    assert cfg.read_bytes() == raw_before


def test_unknown_exposure_blocks_trades_but_allows_reporting(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    write_config(cfg, base_doc())
    unknown = ExposureSnapshot(positions=(), known=False)
    with pytest.raises(UnsafeConfigMutationError):
        apply_config_mutation(
            cfg, make_request(ConfigMutation("symbols.AAPL.budget", 999)),
            validate=accept, exposure=unknown, audit=ListAudit(), clock=lambda: FIXED_NOW,
        )
    audit = ListAudit()
    result = apply_config_mutation(
        cfg, make_request(ConfigMutation("reporting.level", "detailed")),
        validate=accept, exposure=unknown, audit=audit, clock=lambda: FIXED_NOW,
    )
    assert result.changed_paths == ("reporting.level",)
    assert len(audit.entries) == 1


def test_validate_failure_leaves_file_unchanged(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    raw_before = write_config(cfg, base_doc())

    def boom(mapping: Any) -> None:
        raise ValueError("bad config")

    with pytest.raises(ConfigMutationValidationError) as exc_info:
        apply_config_mutation(
            cfg, make_request(ConfigMutation("symbols.AAPL.budget", 42)),
            validate=boom, exposure=empty_exposure(), audit=ListAudit(), clock=lambda: FIXED_NOW,
        )
    assert isinstance(exc_info.value.original, ValueError)
    assert cfg.read_bytes() == raw_before


def test_replace_failure_leaves_bytes_and_no_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.yaml"
    raw_before = write_config(cfg, base_doc())
    before_files = set(os.listdir(tmp_path))

    def failing_replace(src: Any, dst: Any) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError):
        apply_config_mutation(
            cfg, make_request(ConfigMutation("symbols.AAPL.budget", 42)),
            validate=accept, exposure=empty_exposure(), audit=ListAudit(), clock=lambda: FIXED_NOW,
        )
    assert cfg.read_bytes() == raw_before
    assert set(os.listdir(tmp_path)) == before_files


def test_audit_failure_restores_bytes(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    raw_before = write_config(cfg, base_doc())

    class FailingAudit(ConfigAuditSink):
        def record(self, entry: ConfigAuditEntry) -> None:
            raise RuntimeError("sink down")

    with pytest.raises(ConfigAuditFailedError):
        apply_config_mutation(
            cfg, make_request(ConfigMutation("symbols.AAPL.budget", 42)),
            validate=accept, exposure=empty_exposure(), audit=FailingAudit(),
            clock=lambda: FIXED_NOW,
        )
    assert cfg.read_bytes() == raw_before


def test_noop_request_writes_nothing(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    raw_before = write_config(cfg, base_doc())
    mtime_before = os.stat(cfg).st_mtime_ns
    audit = ListAudit()
    result: MutationResult = apply_config_mutation(
        cfg, make_request(ConfigMutation("symbols.AAPL.budget", 1000)),
        validate=accept, exposure=empty_exposure(), audit=audit, clock=lambda: FIXED_NOW,
    )
    assert result.changed_paths == ()
    assert cfg.read_bytes() == raw_before
    assert os.stat(cfg).st_mtime_ns == mtime_before
    assert audit.entries == []


def test_no_import_of_models_or_loader() -> None:
    source = Path("mike_trade/config/mutations.py").read_text(encoding="utf-8")
    assert "mike_trade.config.models" not in source
    assert "mike_trade.config.loader" not in source
