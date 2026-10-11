from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from mike_trade.dashboard.api import DashboardSettings
from mike_trade.dashboard.server import create_app
from mike_trade.domain.accounts import AccountEnvironment
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.persistence.schema import initialize_database
from mike_trade.research.attribution import (
    build_signal_indexes,
    reconcile_trade_attribution,
    recover_run_strategies,
    recover_stop_configuration,
)
from mike_trade.research.run_catalog import (
    ResearchRunCatalog,
    ResearchRunQuery,
)
from mike_trade.research.strategy_roles import (
    classify_strategies,
    get_strategy_role,
    get_strategy_roles,
)
from mike_trade.research.universe_service import UniverseService
from mike_trade.runtime import SystemClock

# ---------------------------------------------------------------------------
# 1. Strategy Role Discovery & Classification
# ---------------------------------------------------------------------------


def test_strategy_role_discovery_and_classification() -> None:
    roles = get_strategy_roles()
    assert len(roles) == 192, f"Expected 192 strategies, found {len(roles)}"

    # Check key representative strategies across all 4 roles
    assert get_strategy_role("vwap_bounce") is StrategyRole.BUY
    assert get_strategy_role("momentum_breakout") is StrategyRole.BUY
    assert get_strategy_role("vwap_breakdown") is StrategyRole.SELL
    assert get_strategy_role("rsi_reversal") is StrategyRole.SELL
    assert get_strategy_role("short_bearish_engulfing") is StrategyRole.SHORT
    assert get_strategy_role("cover_vwap_reclaim") is StrategyRole.COVER

    # Classify a heterogeneous list of names and dicts
    buys, sells, shorts, covers = classify_strategies([
        {"name": "vwap_bounce", "version": "1.0.0"},
        {"name": "vwap_breakdown", "version": "1.0.0"},
        "short_bearish_engulfing",
        "cover_vwap_reclaim",
    ])
    assert buys == ["vwap_bounce"]
    assert sells == ["vwap_breakdown"]
    assert shorts == ["short_bearish_engulfing"]
    assert covers == ["cover_vwap_reclaim"]


# ---------------------------------------------------------------------------
# 2. Legacy Attribution Recovery (Deterministic, Zero Hallucination)
# ---------------------------------------------------------------------------


def test_recover_run_strategies_from_legacy_manifest() -> None:
    # Legacy manifest with configuration = None
    manifest = {
        "run_id": "legacy-run-1",
        "configuration": None,
        "strategies": [
            {"name": "vwap_bounce", "version": "1.0.0"},
            {"name": "vwap_breakdown", "version": "1.0.0"},
        ],
    }
    buys, sells, shorts, covers = recover_run_strategies(manifest)
    assert buys == ["vwap_bounce"]
    assert sells == ["vwap_breakdown"]
    assert shorts == []
    assert covers == []


def test_reconcile_trade_attribution_matches_unambiguous_signals() -> None:
    result = {
        "signals": [
            {
                "signal_id": "sig-entry-123",
                "strategy": "vwap_bounce",
                "symbol": "NVDA",
                "time": "2026-07-09T13:35:00Z",
                "version": "1.0.0",
            },
            {
                "signal_id": "sig-exit-456",
                "strategy": "vwap_breakdown",
                "symbol": "NVDA",
                "time": "2026-07-09T13:36:00Z",
                "version": "1.0.0",
            },
        ],
        "intents": [
            {
                "intent_id": "intent-123",
                "strategy": "vwap_bounce",
                "symbol": "NVDA",
                "time": "2026-07-09T13:35:00Z",
            },
        ],
    }
    cycle = {
        "cycle_id": "cycle-nvda-1",
        "symbol": "NVDA",
        "opened_at": "2026-07-09T13:35:00Z",
        "closed_at": "2026-07-09T13:36:00Z",
    }
    buys = [{"side": "BUY", "quantity": "10", "price": "100.0", "time": "2026-07-09T13:35:00Z"}]
    sells = [{"side": "SELL", "quantity": "10", "price": "105.0", "time": "2026-07-09T13:36:00Z"}]

    sig_idx, int_idx = build_signal_indexes(result)
    versions = {"vwap_bounce": "1.0.0", "vwap_breakdown": "1.0.0"}

    attr = reconcile_trade_attribution(cycle, buys, sells, sig_idx, int_idx, versions)

    assert attr.entry_strategy_id == "vwap_bounce"
    assert attr.entry_strategy_version == "1.0.0"
    assert attr.entry_signal_id == "sig-entry-123"
    assert attr.exit_strategy_id == "vwap_breakdown"
    assert attr.exit_signal_id == "sig-exit-456"
    assert attr.exit_reason == "STRATEGY_EXIT"
    assert attr.attribution_complete is True
    assert attr.attribution_source == "RECOVERED_SIGNALS"


def test_reconcile_trade_attribution_preserves_missing_without_guessing() -> None:
    # When signals and intents are absent, attribution remains unresolved
    result = {"signals": [], "intents": []}
    cycle = {
        "cycle_id": "cycle-unknown-1",
        "symbol": "NVDA",
        "opened_at": "2026-07-09T13:35:00Z",
        "closed_at": "2026-07-09T13:36:00Z",
    }
    buys = [{"side": "BUY", "quantity": "10", "price": "100.0", "time": "2026-07-09T13:35:00Z"}]
    sells = [{"side": "SELL", "quantity": "10", "price": "105.0", "time": "2026-07-09T13:36:00Z"}]

    sig_idx, int_idx = build_signal_indexes(result)
    attr = reconcile_trade_attribution(cycle, buys, sells, sig_idx, int_idx, {})

    assert attr.entry_strategy_id is None
    assert attr.entry_signal_id is None
    assert attr.exit_strategy_id is None
    assert attr.exit_reason is None
    assert attr.attribution_complete is False
    assert attr.attribution_source == "UNRESOLVED"


def test_stop_configuration_recovery() -> None:
    manifest = {
        "configuration": {
            "protective_stop": {"type": "fixed", "percent": "2.0"},
            "trailing_stop": {"type": "stepped", "step_percent": "0.5"},
            "other_param": 123,
        },
        "stop_loss_mode": "native",
    }
    stops = recover_stop_configuration(manifest)
    assert "protective_stop" in stops
    assert "trailing_stop" in stops
    assert "stop_loss_mode" in stops
    assert "other_param" not in stops


# ---------------------------------------------------------------------------
# 3. DuckDB Persistent Research Catalog
# ---------------------------------------------------------------------------


def test_research_run_catalog_incremental_indexing(tmp_path: Path) -> None:
    research_dir = tmp_path / "research"
    research_dir.mkdir()
    run_dir = research_dir / "run1"
    run_dir.mkdir()

    manifest = {
        "run_id": "run-001",
        "created_at": "2026-10-08T14:00:00Z",
        "started_at": "2026-10-08T14:00:00Z",
        "completed_at": "2026-10-08T14:05:00Z",
        "completion_status": "COMPLETED",
        "symbols": ["NVDA"],
        "period_role": "RESEARCH",
        "window_start": "2026-07-01T00:00:00Z",
        "window_end": "2026-07-02T00:00:00Z",
        "starting_capital": "100000",
        "strategies": [{"name": "vwap_bounce", "version": "1.0.0"}],
    }
    result = {
        "schema_version": "2",
        "run_id": "run-001",
        "cycles": [
            {
                "cycle_id": "c-1",
                "symbol": "NVDA",
                "opened_at": "2026-07-01T14:00:00Z",
                "closed_at": "2026-07-01T14:10:00Z",
                "entry_quantity": "10",
                "exit_quantity": "10",
                "realized_pnl": "50.0",
            }
        ],
        "fills": [
            {
                "side": "BUY",
                "symbol": "NVDA",
                "quantity": "10",
                "price": "100.0",
                "time": "2026-07-01T14:00:00Z",
            },
            {
                "side": "SELL",
                "symbol": "NVDA",
                "quantity": "10",
                "price": "105.0",
                "time": "2026-07-01T14:10:00Z",
            },
        ],
        "signals": [
            {
                "signal_id": "sig-1",
                "strategy": "vwap_bounce",
                "symbol": "NVDA",
                "time": "2026-07-01T14:00:00Z",
            },
        ],
        "equity_curve": [{"equity": "100000"}, {"equity": "100050"}],
    }

    (run_dir / "run-001.manifest.json").write_text(json.dumps(manifest))
    (run_dir / "run-001.results.json").write_text(json.dumps(result))

    catalog = ResearchRunCatalog(research_dir, catalog_path=tmp_path / "catalog.duckdb")
    count = catalog.sync()
    assert count == 1

    # Second sync is incremental (reads from metadata cache)
    count2 = catalog.sync()
    assert count2 == 1

    # Query
    q_result = catalog.query_runs(ResearchRunQuery(symbol="NVDA", page=1, page_size=10))
    assert q_result.total == 1
    assert q_result.items[0]["run_id"] == "run-001"
    assert q_result.items[0]["completed_trade_count"] == 1
    assert Decimal(q_result.items[0]["net_pnl"]) == Decimal("50.0")

    # Verify trades
    trades = catalog.get_trades_for_run("run-001")
    assert len(trades) == 1
    assert trades[0]["entry_strategy_id"] == "vwap_bounce"
    assert trades[0]["entry_signal_id"] == "sig-1"
    assert trades[0]["attribution_complete"] is True


# ---------------------------------------------------------------------------
# 4. Universe Service & Dataset Distinction
# ---------------------------------------------------------------------------


def test_universe_service_accurately_flags_missing_vs_available(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    snapshots_dir = data_dir / "snapshots"
    manifests_dir = data_dir / "manifests"
    snapshots_dir.mkdir(parents=True)
    manifests_dir.mkdir(parents=True)

    # Fake dataset manifest for NVDA only
    nvda_manifest = {
        "dataset_id": "alpaca-iex-NVDA-1m-raw-123456",
        "provider": "alpaca",
        "feed": "iex",
        "symbol": "NVDA",
        "asset_class": "us_equity",
        "timeframe": "1m",
        "adjustment": "raw",
        "acquired_at": "2026-10-09T14:00:00Z",
        "start": "2025-10-09T00:00:00Z",
        "end": "2026-10-09T00:00:00Z",
        "first_bar": "2025-10-09T13:30:00Z",
        "last_bar": "2026-10-08T19:59:00Z",
        "bar_count": 50000,
        "data_file": "snapshots/alpaca-iex-NVDA-1m-raw-123456.parquet",
        "sha256": "fakehash",
    }
    (manifests_dir / "alpaca-iex-NVDA-1m-raw-123456.json").write_text(json.dumps(nvda_manifest))

    # Universe config with 2 symbols: NVDA and AAPL
    config_file = tmp_path / "universe.json"
    config_file.write_text(json.dumps({
        "universe_id": "test-universe",
        "symbols": [
            {"symbol": "NVDA", "asset_class": "us_equity", "group": "tech"},
            {"symbol": "AAPL", "asset_class": "us_equity", "group": "tech"},
        ],
    }))

    srv = UniverseService(data_dir, universe_config_path=config_file)
    inv = srv.get_inventory(backtested_counts={"NVDA": 2})

    assert inv.total_universe_symbols == 2
    assert inv.available_dataset_symbols == 1
    assert inv.missing_dataset_symbols == 1
    assert inv.backtested_symbols == 1

    nvda = next(s for s in inv.symbols if s.symbol == "NVDA")
    assert nvda.status == "DATASET_AND_BACKTEST"
    assert nvda.dataset_available is True
    assert nvda.has_backtest_results is True
    assert nvda.backtest_run_count == 2

    aapl = next(s for s in inv.symbols if s.symbol == "AAPL")
    assert aapl.status == "MISSING_DATASET"
    assert aapl.dataset_available is False
    assert aapl.has_backtest_results is False
    assert aapl.dataset_id is None


# ---------------------------------------------------------------------------
# 5. REST API v2 Research Endpoints
# ---------------------------------------------------------------------------


def test_research_api_v2_endpoints(tmp_path: Path) -> None:
    db_path = tmp_path / "ops.sqlite"
    initialize_database(
        db_path,
        AccountEnvironment.SHADOW,
        now=datetime(2026, 10, 8, 15, 0, tzinfo=UTC),
    )

    research_dir = tmp_path / "research"
    research_dir.mkdir()
    data_dir = research_dir / "data"
    data_dir.mkdir()
    run_dir = research_dir / "bench"
    run_dir.mkdir()

    manifest = {
        "run_id": "test-run-100",
        "created_at": "2026-10-08T14:00:00Z",
        "started_at": "2026-10-08T14:00:00Z",
        "completed_at": "2026-10-08T14:05:00Z",
        "completion_status": "COMPLETED",
        "symbols": ["NVDA"],
        "period_role": "RESEARCH",
        "window_start": "2026-07-01T00:00:00Z",
        "window_end": "2026-07-02T00:00:00Z",
        "starting_capital": "100000",
        "strategies": [{"name": "vwap_bounce", "version": "1.0.0"}],
        "configuration": {"protective_stop": {"type": "fixed"}},
    }
    result = {
        "schema_version": "2",
        "run_id": "test-run-100",
        "cycles": [
            {
                "cycle_id": "cycle-1",
                "symbol": "NVDA",
                "opened_at": "2026-07-01T14:00:00Z",
                "closed_at": "2026-07-01T14:10:00Z",
                "entry_quantity": "10",
                "exit_quantity": "10",
                "realized_pnl": "100.0",
            }
        ],
        "fills": [
            {
                "side": "BUY",
                "symbol": "NVDA",
                "quantity": "10",
                "price": "100.0",
                "time": "2026-07-01T14:00:00Z",
            },
            {
                "side": "SELL",
                "symbol": "NVDA",
                "quantity": "10",
                "price": "110.0",
                "time": "2026-07-01T14:10:00Z",
            },
        ],
        "signals": [
            {
                "signal_id": "sig-buy",
                "strategy": "vwap_bounce",
                "symbol": "NVDA",
                "time": "2026-07-01T14:00:00Z",
            },
        ],
        "equity_curve": [{"equity": "100000"}, {"equity": "100100"}],
    }
    (run_dir / "test-run-100.manifest.json").write_text(json.dumps(manifest))
    (run_dir / "test-run-100.results.json").write_text(json.dumps(result))

    settings = DashboardSettings(
        bind_host="127.0.0.1",
        port=8000,
        research_root=research_dir,
        research_dataset_root=data_dir,
    )
    app = create_app(settings, SystemClock(), db_path)
    client = TestClient(app)

    # 1. GET /api/v2/research/datasets
    resp = client.get("/api/v2/research/datasets")
    assert resp.status_code == 200
    data = resp.json()
    assert "universe_id" in data
    assert "symbols" in data
    assert "snapshots" in data

    # 2. GET /api/v2/research/runs
    resp = client.get("/api/v2/research/runs?page=1&page_size=20")
    assert resp.status_code == 200
    runs_data = resp.json()
    assert runs_data["total"] == 1
    assert runs_data["items"][0]["run_id"] == "test-run-100"
    assert runs_data["items"][0]["buy_strategies"] == ["vwap_bounce"]
    assert Decimal(runs_data["items"][0]["net_pnl"]) == Decimal("100.0")

    # Filter with non-matching symbol returns 0
    resp_empty = client.get("/api/v2/research/runs?symbol=MSFT")
    assert resp_empty.json()["total"] == 0

    # 3. GET /api/v2/research/runs/{run_id}
    resp_detail = client.get("/api/v2/research/runs/test-run-100")
    assert resp_detail.status_code == 200
    detail = resp_detail.json()
    assert detail["run"]["run_id"] == "test-run-100"
    assert detail["configuration"]["protective_stop"]["type"] == "fixed"
    assert detail["diagnostics"]["winning_cycles"] == 1

    # 4. GET /api/v2/research/runs/{run_id}/trades
    resp_trades = client.get("/api/v2/research/runs/test-run-100/trades")
    assert resp_trades.status_code == 200
    trades_data = resp_trades.json()
    assert trades_data["total"] == 1
    t = trades_data["items"][0]
    assert t["symbol"] == "NVDA"
    assert t["entry_strategy_id"] == "vwap_bounce"
    assert t["entry_signal_id"] == "sig-buy"
    assert t["attribution_complete"] is True

    # 5. GET /api/v2/research/runs/compare
    resp_compare = client.get(
        "/api/v2/research/runs/compare?run_id=test-run-100&run_id=test-run-100"
    )
    assert resp_compare.status_code == 200
    comp_data = resp_compare.json()
    assert len(comp_data["runs"]) == 2
    assert "symbols_union" in comp_data["comparative_metrics"]

    # Nonexistent run 404
    assert client.get("/api/v2/research/runs/nonexistent").status_code == 404
