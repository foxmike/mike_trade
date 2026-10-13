"""Zero-slippage Michelle research campaign: defaults, recorded assumptions, and job integrity."""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mike_trade.market_data.alpaca_history import Adjustment, HistoricalBar
from mike_trade.research.commands import (
    ExperimentSpec,
    _run_one,
    research_default_execution_assumptions,
)
from mike_trade.research.datasets import DatasetSnapshot, HistoricalDatasetRepository

CAMPAIGNS = Path(__file__).resolve().parents[2] / "research" / "campaigns"
CAMPAIGN = CAMPAIGNS / "michelle_zero_slippage_oct2026"
SOURCE = CAMPAIGNS / "sip_annual_oct2026"
ZERO_EXECUTION = {
    "slippage_bps": "0",
    "slippage_ticks": "0",
    "fee_per_order": "0",
    "fee_per_share": "0",
    "fee_bps": "0",
}


def _load_validator():
    spec = importlib.util.spec_from_file_location(
        "michelle_zero_validate", CAMPAIGN / "validate_campaign.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _jobs(path: Path) -> list[dict]:
    return json.loads(path.read_text())["experiments"]


def test_research_default_costs_are_zero() -> None:
    assert research_default_execution_assumptions() == {
        key: Decimal(0) for key in ZERO_EXECUTION
    }


@pytest.fixture
def recorded_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    snapshot = DatasetSnapshot(
        dataset_id="snap-sip-ibm-1",
        provider="alpaca",
        feed="sip",
        symbol="IBM",
        asset_class="us_equity",
        timeframe="1m",
        adjustment="raw",
        acquired_at="2026-10-12T12:00:00+00:00",
        start="2026-07-09T00:00:00+00:00",
        end="2026-10-09T00:00:00+00:00",
        first_bar="2026-07-09T13:30:00+00:00",
        last_bar="2026-10-08T19:59:00+00:00",
        bar_count=1,
        data_file="snapshots/snap-sip-ibm-1.parquet",
        sha256="abc12345" * 8,
    )
    repo = MagicMock(spec=HistoricalDatasetRepository)
    repo.snapshots.return_value = [snapshot]
    monkeypatch.setattr("mike_trade.research.commands.HistoricalDatasetRepository", lambda root: repo)
    bar = HistoricalBar(
        symbol="IBM",
        timestamp=datetime(2026, 7, 9, 14, 0, tzinfo=UTC),
        open=Decimal("140"),
        high=Decimal("141"),
        low=Decimal("139.5"),
        close=Decimal("140.5"),
        volume=1000,
        vwap=Decimal("140.25"),
        trade_count=100,
        feed_id="sip",
        adjustment=Adjustment.RAW,
    )
    monkeypatch.setattr(
        "mike_trade.research.commands._cached_snapshot_bars", lambda *a, **k: (bar,)
    )
    requests = []

    async def fake_run(self, request):  # noqa: ANN001, ANN202
        requests.append(request)
        result = MagicMock()
        result.run_id, result.results_hash = "run", "hash"
        result.evaluations = result.signals = result.fills = result.equity_curve = ()
        return result

    monkeypatch.setattr("mike_trade.backtest.runner.BacktestRunner.run", fake_run)
    metrics = MagicMock()
    metrics.trades = ()
    metrics.win_rate = metrics.total_return = metrics.net_return = Decimal(0)
    metrics.expectancy_net = metrics.max_drawdown = Decimal(0)
    metrics.profit_factor = None
    monkeypatch.setattr("mike_trade.backtest.runner.compute_metrics", lambda *a, **k: metrics)

    def run(execution=None):  # noqa: ANN001, ANN202
        result = _run_one(
            "IBM",
            datetime(2026, 7, 9, tzinfo=UTC),
            datetime(2026, 10, 9, tzinfo=UTC),
            str(tmp_path),
            str(tmp_path / "results"),
            271828,
            "michelle_orb",
            buy_strategies=("five_minute_orb_michelle_buy",),
            sell_strategies=("five_minute_orb_michelle_sell",),
            feed="sip",
            execution=execution,
        )
        return result, requests[-1]

    return run


def test_implicit_research_run_records_zero_slippage_and_fees(recorded_run) -> None:
    result, request = recorded_run()
    recorded = result["execution_assumptions"]
    assert {key: recorded[key] for key in ZERO_EXECUTION} == ZERO_EXECUTION
    assert recorded["bar_fill_policy"] == "NEXT_BAR_OPEN"
    assert recorded["ambiguity_policy"] == "STOP_FIRST"
    assert request.config["assumptions"] == recorded


def test_explicit_zero_execution_is_recorded_exactly(recorded_run) -> None:
    result, _ = recorded_run(ZERO_EXECUTION)
    assert {key: result["execution_assumptions"][key] for key in ZERO_EXECUTION} == ZERO_EXECUTION


def test_explicit_costs_still_override_the_zero_default(recorded_run) -> None:
    result, _ = recorded_run({"slippage_bps": "5"})
    assert result["execution_assumptions"]["slippage_bps"] == "5"
    assert result["execution_assumptions"]["fee_per_share"] == "0"


def test_campaign_has_nine_validated_michelle_jobs() -> None:
    validator = _load_validator()
    manifest = json.loads((CAMPAIGN / "campaign_manifest.json").read_text())
    jobs = _jobs(CAMPAIGN / "experiments.json")
    assert validator.check_michelle(jobs, manifest) == []
    assert len(jobs) == 9
    for job in jobs:
        assert job["execution"] == ZERO_EXECUTION
        ExperimentSpec(**job).validate()


def test_campaign_differs_from_source_only_by_execution_and_excludes_holdout() -> None:
    source = _jobs(SOURCE / "experiments.json")
    holdout = _jobs(SOURCE / "holdout_experiments.json")
    for job in _jobs(CAMPAIGN / "experiments.json"):
        (original,) = [
            j for j in source if (j["symbol"], j["strategy"]) == (job["symbol"], job["strategy"])
        ]
        assert original["execution"]["slippage_bps"] == "5"
        assert {key for key in job if job[key] != original[key]} == {"execution"}
        assert job["end"] == "2026-09-21T04:00:00Z"
        assert all(job["start"] != h["start"] for h in holdout)


def test_future_candidate_must_share_the_frame_and_reports_rule_differences() -> None:
    validator = _load_validator()
    manifest = json.loads((CAMPAIGN / "campaign_manifest.json").read_text())
    candidate = json.loads(json.dumps(_jobs(CAMPAIGN / "experiments.json")[:1]))
    candidate[0]["strategy"] = "mike_orb"
    errors, lines = validator.compare_candidate(candidate, manifest)
    assert errors == [] and "mike_orb" in lines[0]
    candidate[0]["execution"] = {**ZERO_EXECUTION, "slippage_bps": "5"}
    candidate[0]["starting_capital"] = "25000"
    errors, _ = validator.compare_candidate(candidate, manifest)
    assert any("execution" in e for e in errors) and any("starting_capital" in e for e in errors)
