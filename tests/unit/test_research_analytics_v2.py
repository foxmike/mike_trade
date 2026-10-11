"""Research Lab analytics endpoints: facets, aggregates, run analysis, journal, compare."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mike_trade.dashboard.v2.research import create_research_v2_router
from mike_trade.research.run_catalog import (
    ResearchRunCatalog,
    ResearchRunQuery,
    compute_trade_stats,
    stop_policy_label,
)

WINDOW = ("2026-07-01T00:00:00Z", "2026-07-03T00:00:00Z")


def _write_run(
    root: Path,
    name: str,
    *,
    symbol: str,
    strategies: list[str],
    trades: list[tuple[str, str, str, str, str, str]],
    equity: list[tuple[str, str]],
    stop: dict[str, Any] | None = None,
    window: tuple[str, str] = WINDOW,
) -> str:
    """Write a manifest/results pair. ``trades`` = (side_in, open, close, in_px, out_px, qty)."""
    folder = root / "bench"
    folder.mkdir(parents=True, exist_ok=True)
    run_id = f"run-{name}"
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "created_at": "2026-10-08T14:00:00Z",
        "symbols": [symbol],
        "period_role": "RESEARCH",
        "window_start": window[0],
        "window_end": window[1],
        "starting_capital": "10000",
        "strategies": [{"name": n, "version": "1.0.0"} for n in strategies],
        "data_revision": f"ds-{symbol}",
    }
    if stop is not None:
        manifest["configuration"] = stop
    cycles, fills = [], []
    for index, (side, opened, closed, price_in, price_out, qty) in enumerate(trades, start=1):
        entry_side, exit_side = ("BUY", "SELL") if side == "LONG" else ("SELL_SHORT", "BUY_TO_COVER")
        cycles.append({
            "cycle_id": f"{run_id}:{symbol}:{index}",
            "symbol": symbol,
            "opened_at": opened,
            "closed_at": closed or None,
            "entry_quantity": qty,
        })
        fills.append({"side": entry_side, "symbol": symbol, "quantity": qty,
                      "price": price_in, "time": opened, "fee": "0"})
        if closed:
            fills.append({"side": exit_side, "symbol": symbol, "quantity": qty,
                          "price": price_out, "time": closed, "fee": "0"})
    result = {
        "schema_version": "2",
        "cycles": cycles,
        "fills": fills,
        "signals": [],
        "intents": [],
        "equity_curve": [{"time": t, "equity": e, "cash": e} for t, e in equity],
    }
    (folder / f"{run_id}.manifest.json").write_text(json.dumps(manifest))
    (folder / f"{run_id}.results.json").write_text(json.dumps(result))
    return run_id


@pytest.fixture
def lab(tmp_path: Path) -> tuple[TestClient, ResearchRunCatalog, dict[str, str]]:
    research = tmp_path / "research"
    data = research / "data"
    data.mkdir(parents=True)
    ids = {
        # +100, -50, short +20 (entry 50 exit 48, qty 10), one open trade
        "a": _write_run(
            research, "a", symbol="NVDA", strategies=["vwap_bounce", "vwap_breakdown"],
            stop={"protective_stop": {"type": "fixed", "stop_percent": "2"}},
            trades=[
                ("LONG", "2026-07-01T14:00:00Z", "2026-07-01T14:10:00Z", "100", "110", "10"),
                ("LONG", "2026-07-01T15:00:00Z", "2026-07-01T15:10:00Z", "100", "95", "10"),
                ("SHORT", "2026-07-02T14:00:00Z", "2026-07-02T14:30:00Z", "50", "48", "10"),
                ("LONG", "2026-07-02T19:00:00Z", "", "20", "0", "5"),
            ],
            equity=[
                ("2026-07-01T14:00:00Z", "10000"),
                ("2026-07-01T14:10:00Z", "10100"),
                ("2026-07-01T15:10:00Z", "10050"),
                ("2026-07-02T14:30:00Z", "10070"),
                ("2026-07-02T20:00:00Z", "10060"),
            ],
        ),
        "b": _write_run(
            research, "b", symbol="IBM", strategies=["vwap_bounce", "vwap_breakdown"],
            stop={"protective_trailing_percent": "1.5"},
            trades=[("LONG", "2026-07-01T14:00:00Z", "2026-07-01T14:10:00Z", "10", "9", "100")],
            equity=[("2026-07-01T14:00:00Z", "10000"), ("2026-07-01T14:10:00Z", "9900")],
        ),
        "c": _write_run(
            research, "c", symbol="TSLA", strategies=["vwap_bounce", "vwap_breakdown"],
            window=("2026-08-01T00:00:00Z", "2026-08-05T00:00:00Z"),
            trades=[], equity=[("2026-08-01T14:00:00Z", "10000")],
        ),
    }
    service = SimpleNamespace(settings=SimpleNamespace(
        research_root=research, research_dataset_root=data
    ))
    app = FastAPI()
    router = create_research_v2_router(service)
    app.include_router(router)
    catalog = ResearchRunCatalog(research, data / "research_runs.duckdb")
    return TestClient(app), catalog, ids


def test_trade_statistics_come_from_recorded_fills_including_shorts(lab: Any) -> None:
    client, _, ids = lab
    run = client.get(f"/api/v2/research/runs/{ids['a']}").json()
    stats = run["run"]["trade_stats"]
    assert stats["closed_trades"] == 3
    assert stats["winning_trades"] == 2 and stats["losing_trades"] == 1
    assert float(stats["realized_pnl"]) == pytest.approx(70.0)  # +100 -50 +20 (short)
    assert float(stats["average_win"]) == pytest.approx(60.0)
    assert float(stats["expectancy"]) == pytest.approx(70 / 3, rel=1e-5)
    assert float(stats["top3_profit_share"]) == pytest.approx(100.0)
    assert run["run"]["win_rate"].startswith("66.66")
    assert float(run["run"]["profit_factor"]) == pytest.approx(120 / 50)

    trades = client.get(f"/api/v2/research/runs/{ids['a']}/trades").json()
    short = next(t for t in trades["items"] if t["direction"] == "SHORT")
    assert float(short["realized_pnl"]) == pytest.approx(20.0)
    assert trades["available_directions"] == ["LONG", "SHORT"]


def test_run_analysis_separates_equity_and_closed_trade_series(lab: Any) -> None:
    client, _, ids = lab
    body = client.get(f"/api/v2/research/runs/{ids['a']}").json()

    assert body["equity_curve_total_points"] == 5
    assert [p["equity"] for p in body["equity_curve"]] == [10000, 10100, 10050, 10070, 10060]
    # Equity-basis net P&L (+60 incl. open trade mark) differs from closed-trade realized (+70).
    assert float(body["run"]["net_pnl"]) == pytest.approx(60.0)
    assert float(body["diagnostics"]["realized_pnl"]) == pytest.approx(70.0)
    assert float(body["diagnostics"]["unrealized_or_unexplained_pnl"]) == pytest.approx(-10.0)
    assert body["diagnostics"]["open_cycles"] == 1

    cumulative = [p["cumulative_pnl"] for p in body["trade_series"]]
    assert cumulative == pytest.approx([100.0, 50.0, 70.0])

    detail = body["drawdown_detail"]
    assert detail["available"] is True
    assert float(detail["max_drawdown_percent"]) == pytest.approx(50 / 10100 * 100, rel=1e-3)
    assert max(p["drawdown_percent"] for p in body["drawdown_curve"]) == pytest.approx(
        50 / 10100 * 100, rel=1e-3
    )

    dist = body["pnl_distribution"]
    assert dist["count"] == 3 and dist["winners"] == 2 and dist["losers"] == 1
    assert sum(b["count"] for b in dist["bins"]) == 3
    assert not any(b["lower"] < 0 < b["upper"] for b in dist["bins"])

    assert body["period_performance"]["granularity"] == "day"
    assert sum(r["closed_trades"] for r in body["period_performance"]["periods"]) == 3
    assert "cumulative_trade_pnl" in body["metric_definitions"]


def test_equity_series_downsample_preserves_extremes(lab: Any) -> None:
    client, catalog, _ = lab
    root = catalog.research_root
    curve = [(f"2026-07-01T{14 + i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z",
              str(10000 + (i % 97) - (500 if i == 777 else 0))) for i in range(2000)]
    run_id = _write_run(root, "dense", symbol="AMD", strategies=["vwap_bounce"],
                        trades=[], equity=curve)
    catalog.sync(force=True)
    body = TestClient(_app_for(catalog)).get(
        f"/api/v2/research/runs/{run_id}", params={"max_points": 100}
    ).json()
    assert body["equity_curve_total_points"] == 2000
    assert len(body["equity_curve"]) <= 100
    assert min(p["equity"] for p in body["equity_curve"]) == 9500 + (777 % 97)
    assert float(body["drawdown_detail"]["max_drawdown_percent"]) > 4.0


def _app_for(catalog: ResearchRunCatalog) -> FastAPI:
    service = SimpleNamespace(settings=SimpleNamespace(
        research_root=catalog.research_root,
        research_dataset_root=catalog.catalog_path.parent,
    ))
    app = FastAPI()
    app.include_router(create_research_v2_router(service, catalog))
    return app


def test_journal_server_side_sort_filter_and_summary(lab: Any) -> None:
    client, _, ids = lab
    base = f"/api/v2/research/runs/{ids['a']}/trades"
    page = client.get(base, params={"page_size": 2, "sort_by": "realized_pnl", "sort_order": "desc"}).json()
    assert page["total"] == 4 and page["total_pages"] == 2 and len(page["items"]) == 2
    assert float(page["items"][0]["realized_pnl"]) == pytest.approx(100.0)

    # Trades without realized P&L (the open one) sort last in both directions.
    asc = client.get(base, params={"sort_by": "realized_pnl", "sort_order": "asc"}).json()["items"]
    assert asc[-1]["position_status"] == "OPEN"
    desc = client.get(base, params={"sort_by": "realized_pnl", "sort_order": "desc"}).json()["items"]
    assert desc[-1]["position_status"] == "OPEN"

    losers = client.get(base, params={"outcome": "LOSS"}).json()
    assert losers["total"] == 1
    assert float(losers["filtered_summary"]["realized_pnl"]) == pytest.approx(-50.0)

    shorts = client.get(base, params={"direction": "SHORT"}).json()
    assert shorts["total"] == 1

    by_return = client.get(base, params={"sort_by": "realized_return_percent"}).json()["items"]
    assert float(by_return[0]["realized_return_percent"]) >= float(by_return[1]["realized_return_percent"])


def test_facets_cover_the_whole_catalog_not_one_page(lab: Any) -> None:
    client, _, _ = lab
    facets = client.get("/api/v2/research/runs/facets").json()
    assert facets["total_runs"] == 3
    assert {o["value"] for o in facets["symbols"]} == {"NVDA", "IBM", "TSLA"}
    assert {o["value"] for o in facets["stop_policies"]} == {
        "fixed stop 2%", "native_trailing 1.5%"
    }
    assert facets["runs_without_stop_policy"] == 1
    assert {o["value"] for o in facets["windows"]} == {
        "2026-07-01..2026-07-03", "2026-08-01..2026-08-05"
    }
    assert facets["short_strategies"] == []

    # The facet value round-trips as a filter.
    filtered = client.get("/api/v2/research/runs", params={"stop_policy": "fixed stop 2%"}).json()
    assert filtered["total"] == 1


def test_aggregate_uses_medians_and_never_sums_capital(lab: Any) -> None:
    client, _, ids = lab
    body = client.get("/api/v2/research/runs/aggregate", params={"group_by": "stop_policy"}).json()
    groups = {g["key"]: g for g in body["groups"]}
    assert set(groups) == {"fixed stop 2%", "native_trailing 1.5%", "UNRECORDED"}
    assert groups["fixed stop 2%"]["median_return"] == pytest.approx(0.6)
    assert groups["native_trailing 1.5%"]["profitable_run_rate"] == 0.0
    assert groups["fixed stop 2%"]["best_run_id"] == ids["a"]
    assert "total_return" not in groups["fixed stop 2%"] and "net_pnl_sum" not in groups["fixed stop 2%"]

    strategies = client.get("/api/v2/research/runs/aggregate", params={"group_by": "buy_strategy"}).json()
    assert strategies["groups"][0]["key"] == "vwap_bounce"
    assert strategies["groups"][0]["run_count"] == 3
    assert strategies["groups"][0]["trade_count"] == 4  # closed trades: 3 + 1 + 0

    windows = client.get("/api/v2/research/runs/aggregate", params={"group_by": "window"}).json()
    assert {g["key"]: g["run_count"] for g in windows["groups"]} == {
        "2026-07-01..2026-07-03": 2, "2026-08-01..2026-08-05": 1,
    }
    same = client.get("/api/v2/research/runs", params={"window_key": "2026-07-01..2026-07-03"}).json()
    assert same["total"] == 2

    filtered = client.get(
        "/api/v2/research/runs/aggregate", params={"group_by": "symbol", "min_trades": 1}
    ).json()
    assert {g["key"] for g in filtered["groups"]} == {"NVDA", "IBM"}
    assert client.get("/api/v2/research/runs/aggregate", params={"group_by": "nope"}).status_code == 400


def test_compare_returns_leaders_curves_and_differences(lab: Any) -> None:
    client, _, ids = lab
    body = client.get(
        "/api/v2/research/runs/compare", params=[("run_id", ids["a"]), ("run_id", ids["b"])]
    ).json()
    metrics = body["comparative_metrics"]
    assert metrics["leaders"]["net_pnl"] == ids["a"]
    assert metrics["leaders"]["max_drawdown"] == ids["a"]  # lower drawdown wins
    assert metrics["period"]["identical_windows"] is True
    assert {d["field"] for d in body["differences"]} >= {"symbols", "stop_policy"}
    assert set(body["curves"]) == {ids["a"], ids["b"]}
    assert body["curves"][ids["a"]][0]["index"] == pytest.approx(100.0)

    mixed = client.get(
        "/api/v2/research/runs/compare", params=[("run_id", ids["a"]), ("run_id", ids["c"])]
    ).json()
    assert mixed["comparative_metrics"]["period"]["identical_windows"] is False

    assert client.get("/api/v2/research/runs/compare", params=[("run_id", ids["a"])]).status_code == 400


def test_stale_persisted_index_is_rebuilt_on_schema_change(lab: Any) -> None:
    _, catalog, _ = lab
    assert catalog.query_runs(ResearchRunQuery()).total == 3
    import duckdb

    con = duckdb.connect(str(catalog.catalog_path))
    con.execute("UPDATE catalog_meta SET value = '1' WHERE key = 'index_schema_version'")
    con.close()
    reopened = ResearchRunCatalog(catalog.research_root, catalog.catalog_path)
    assert reopened.query_runs(ResearchRunQuery()).total == 3


def test_date_only_window_filters_are_utc(lab: Any) -> None:
    client, _, _ = lab
    body = client.get(
        "/api/v2/research/runs",
        params={"window_start": "2026-07-01", "window_end": "2026-07-03"},
    ).json()
    assert body["total"] == 2


def test_helpers() -> None:
    assert stop_policy_label({}) is None
    assert stop_policy_label({"protective_trailing_percent": "1.5"}) == "native_trailing 1.5%"
    assert compute_trade_stats([])["expectancy"] is None


def test_ten_thousand_indexed_runs_stay_server_paginated(tmp_path: Path) -> None:
    research = tmp_path / "research"
    data = research / "data"
    data.mkdir(parents=True)
    catalog = ResearchRunCatalog(research, data / "research_runs.duckdb")
    catalog.query_runs(ResearchRunQuery())  # initialize empty index
    import duckdb

    from mike_trade.research.run_catalog import _INDEX_COLUMNS

    con = duckdb.connect(str(catalog.catalog_path))
    expressions: dict[str, str] = {
        "selection_id": "printf('sel-%05d', i)",
        "run_id": "printf('run-%05d', i)",
        "symbols": "[printf('SYM%03d', i % 100)]",
        "buy_strategies": "[printf('buy_strategy_%03d', i % 40)]",
        "sell_strategies": "[]::VARCHAR[]",
        "short_strategies": "[]::VARCHAR[]",
        "cover_strategies": "[]::VARCHAR[]",
        "stop_policy": "CASE i % 4 WHEN 0 THEN 'fixed 1%' WHEN 1 THEN 'fixed 2%' "
                       "WHEN 2 THEN 'native_trailing 1.5%' ELSE NULL END",
        "stop_configuration_json": "'{}'",
        "completed_trade_count": "CAST(i % 300 AS INTEGER)",
        "net_pnl_num": "CAST(i % 500 - 250 AS DOUBLE)",
        "return_percent_num": "CAST(i % 500 - 250 AS DOUBLE) / 100",
        "max_drawdown_num": "CAST(i % 37 AS DOUBLE)",
        "profit_factor_num": "0.5 + (i % 40) / 10.0",
        "win_rate_num": "CAST(i % 100 AS DOUBLE)",
        "window_key": "'2026-07-01..2026-10-01'",
        "status": "'COMPLETED'",
        "manifest_path": "'m'",
        "results_path": "'r'",
        "result_hash": "CAST(i AS VARCHAR)",
        "manifest_mtime": "0.0",
        "results_mtime": "0.0",
        "manifest_size": "0",
        "results_size": "0",
        "indexed_at": "now()",
        "payload_json": "json_object('selection_id', printf('sel-%05d', i), "
                        "'experiment_id', printf('run-%05d', i), 'run_id', printf('run-%05d', i), "
                        "'symbols', [printf('SYM%03d', i % 100)], "
                        "'completed_trade_count', i % 300)",
    }
    select_list = ", ".join(expressions.get(name, "NULL") for name, _ in _INDEX_COLUMNS)
    con.execute(
        f"INSERT INTO research_runs_index SELECT {select_list} FROM range(12000) AS t(i)"
    )
    con.close()

    client = TestClient(_app_for(catalog))
    started = time.perf_counter()
    page = client.get(
        "/api/v2/research/runs",
        params={"page": 3, "page_size": 25, "sort_by": "return_percent", "buy_strategy": "buy_strategy_007"},
    ).json()
    assert page["total"] == 300 and len(page["items"]) == 25 and page["page"] == 3
    unfiltered = client.get("/api/v2/research/runs", params={"page_size": 25}).json()
    assert unfiltered["total"] == 12000 and len(unfiltered["items"]) == 25

    facets = client.get("/api/v2/research/runs/facets").json()
    assert facets["total_runs"] == 12000 and len(facets["buy_strategies"]) == 40
    assert len(facets["symbols"]) == 100  # options span the whole catalog, not the 25-row page

    groups = client.get(
        "/api/v2/research/runs/aggregate",
        params={"group_by": "buy_strategy", "sort_by": "median_return", "limit": 10},
    ).json()
    assert len(groups["groups"]) == 10 and groups["runs_considered"] == 12000
    assert time.perf_counter() - started < 5.0
