"""Focused symbol-boundary checks for shared-portfolio backtest manifests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mike_trade.backtest.manifest import PeriodRole, RunManifest


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _manifest(symbols: tuple[str, ...]) -> RunManifest:
    return RunManifest(
        run_id="portfolio-limit-test",
        created_at=NOW,
        config_hash="a" * 64,
        symbols=symbols,
        window_start=NOW,
        window_end=NOW + timedelta(days=1),
        period_role=PeriodRole.RESEARCH,
        code_revision={"commit": "test", "dirty": True},
        data_provenance={"source": "fixture", "feed": "IEX"},
        data_revision="fixture",
        data_vintage="fixture",
        dataset_hash="b" * 64,
        starting_capital="25000",
        fee_assumptions={},
        slippage_assumptions={"slippage_bps": "5"},
        fill_assumptions={"bar_fill_policy": "NEXT_BAR_OPEN"},
        ambiguity_assumptions={"ambiguity_policy": "STOP_FIRST"},
        seed=271828,
        strategies=(),
    )


def test_run_manifest_accepts_100_symbols() -> None:
    manifest = _manifest(tuple(f"S{index:03d}" for index in range(100)))
    assert len(manifest.symbols) == 100


def test_run_manifest_rejects_101_symbols() -> None:
    with pytest.raises(ValueError, match="1-100 symbols"):
        _manifest(tuple(f"S{index:03d}" for index in range(101)))


def test_michelle_portfolio_campaign_constructs_manifest() -> None:
    root = Path(__file__).resolve().parents[2]
    campaign = json.loads(
        (root / "config/research_campaigns/michelle_corrected_vwap_iex_2024_2025_v1.json")
        .read_text(encoding="utf-8")
    )
    universe = json.loads((root / campaign["universe_file"]).read_text(encoding="utf-8"))
    symbols = tuple(item["symbol"] for item in universe["symbols"])

    manifest = _manifest(symbols)

    assert len(manifest.symbols) == campaign["symbols_expected"] == 100
    assert set(manifest.symbols) == set(campaign["data"]["dataset_ids"])
