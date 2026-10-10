"""Deterministic unified trade journal, inspection, and research compatibility tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mike_trade.dashboard.api import DashboardSettings
from mike_trade.dashboard.server import create_app
from mike_trade.domain.accounts import AccountEnvironment
from mike_trade.persistence.schema import initialize_database, to_db_timestamp

NOW = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)


class Clock:
    def now(self) -> datetime:
        return NOW


def ts(minutes: int, seconds: int = 0) -> str:
    return to_db_timestamp(NOW - timedelta(minutes=minutes, seconds=seconds))


def seed(
    path: Path, mode: AccountEnvironment = AccountEnvironment.SHADOW
) -> tuple[TestClient, str]:
    conn = initialize_database(path, mode, now=NOW)
    scope = f"{mode.value.lower()}:journal"
    conn.execute(
        "INSERT INTO account_scopes(account_scope_id,environment,broker,broker_account_ref,scenario_name,created_at) VALUES (?,?,?,?,?,?)",
        (
            scope,
            mode.value,
            "alpaca" if mode in {AccountEnvironment.PAPER, AccountEnvironment.LIVE} else None,
            "fixture-account"
            if mode in {AccountEnvironment.PAPER, AccountEnvironment.LIVE}
            else None,
            "baseline" if mode is AccountEnvironment.SHADOW else None,
            ts(60),
        ),
    )
    conn.execute(
        "INSERT INTO instruments(account_scope_id,symbol,observed_at) VALUES (?,'NVDA',?)",
        (scope, ts(1)),
    )
    for instance, key, version, role in (
        ("buy-a", "vwap_bounce", "1.2.0", "BUY"),
        ("buy-b", "momentum_breakout", "2.0.0", "BUY"),
        ("sell-a", "vwap_breakdown", "1.1.0", "SELL"),
    ):
        conn.execute(
            "INSERT INTO strategy_instances(instance_id,account_scope_id,strategy_key,strategy_version,role,symbol,parameters_json,enabled,created_at) VALUES (?,?,?,?,?,'NVDA','{}',1,?)",
            (instance, scope, key, version, role, ts(40)),
        )
    for sid, instance, action, minute in (
        ("sig-win", "buy-a", "ENTER_LONG", 30),
        ("sig-lost", "buy-b", "ENTER_LONG", 30),
        ("sig-exit", "sell-a", "EXIT_LONG", 25),
    ):
        conn.execute(
            "INSERT INTO signals(signal_id,account_scope_id,instance_id,symbol,action,reference_price,reason,provenance_json,bar_time,generated_at) VALUES (?,?,?,'NVDA',?,'100','fixture','{}',?,?)",
            (sid, scope, instance, action, ts(minute + 1), ts(minute)),
        )
    conn.execute(
        "INSERT INTO decisions(decision_id,account_scope_id,signal_id,outcome,reason,decided_at) VALUES ('d-win',?,'sig-win','SELECTED','priority',?),('d-lost',?,'sig-lost','REJECTED','superseded',?),('d-exit',?,'sig-exit','SELECTED','exit accepted',?)",
        (scope, ts(30), scope, ts(30), scope, ts(25)),
    )
    conn.execute(
        "INSERT INTO position_cycles(cycle_id,account_scope_id,symbol,direction,state,entry_strategy_key,opened_at,closed_at,created_at,updated_at) VALUES ('cycle-1',?,'NVDA','LONG','CLOSED','vwap_bounce',?,?,?,?)",
        (scope, ts(29, 55), ts(24, 55), ts(31), ts(24)),
    )
    intents = (
        ("entry-intent", "entry-client", "sig-win", "BUY", "ENTRY", ts(30)),
        ("exit-intent", "exit-client", "sig-exit", "SELL", "STRATEGY_EXIT", ts(25)),
    )
    for iid, client, signal, side, purpose, created in intents:
        conn.execute(
            "INSERT INTO order_intents(intent_id,account_scope_id,cycle_id,signal_id,client_order_id,symbol,side,purpose,order_type,time_in_force,quantity,status,created_at,updated_at) VALUES (?,?,'cycle-1',?,?,'NVDA',?,?,'MARKET','DAY','10','SUBMITTED',?,?)",
            (iid, scope, signal, client, side, purpose, created, created),
        )
    for oid, iid, client, side, submitted in (
        ("buy-order", "entry-intent", "entry-client", "BUY", ts(30)),
        ("sell-order", "exit-intent", "exit-client", "SELL", ts(25)),
    ):
        conn.execute(
            "INSERT INTO broker_orders(account_scope_id,broker_order_id,intent_id,client_order_id,symbol,side,order_type,status,quantity,filled_quantity,filled_avg_price,submitted_at,updated_at) VALUES (?,?,?,?, 'NVDA',?,'MARKET','FILLED','10','10','105',?,?)",
            (scope, oid, iid, client, side, submitted, submitted),
        )
    fills = (
        ("f1", "buy-order", "BUY", "4", "100", 30, 0),
        ("f2", "buy-order", "BUY", "6", "102", 29, 55),
        ("f3", "sell-order", "SELL", "5", "110", 25, 0),
        ("f4", "sell-order", "SELL", "5", "108", 24, 55),
    )
    for fid, order, side, qty, price, minute, seconds in fills:
        conn.execute(
            "INSERT INTO fills(fill_id,account_scope_id,external_activity_id,broker_order_id,cycle_id,symbol,side,quantity,price,fee,filled_at,recorded_at) VALUES (?,?,?,?,'cycle-1','NVDA',?,?,?,'0.5',?,?)",
            (fid, scope, fid, order, side, qty, price, ts(minute, seconds), ts(minute, seconds)),
        )
    conn.execute(
        "INSERT INTO protection_orders(protection_order_id,account_scope_id,cycle_id,kind,status,covered_quantity,stop_price,created_at,updated_at) VALUES ('stop-1',?,'cycle-1','STEPPED_TRAILING_STOP','TRIGGERED','10','98',?,?)",
        (scope, ts(29), ts(24)),
    )
    conn.execute(
        "INSERT INTO trailing_stop_events(trailing_stop_event_id,account_scope_id,cycle_id,event_type,previous_stop_price,new_stop_price,payload_json,occurred_at) VALUES ('move-1',?,'cycle-1','STOP_REPLACEMENT_CONFIRMED','98','103','{}',?)",
        (scope, ts(26)),
    )
    for minute, op, hi, lo, close, vwap in (
        (30, "99", "104", "98", "103", "101"),
        (29, "103", "112", "102", "110", "106"),
        (28, "110", "111", "107", "108", "108"),
        (25, "109", "110", "107", "108", "108"),
    ):
        conn.execute(
            "INSERT INTO market_bars(account_scope_id,symbol,timeframe,bar_start,open_price,high_price,low_price,close_price,vwap,volume,feed,provenance,recorded_at) VALUES (?,'NVDA','1m',?,?,?,?,?,?,'1000','iex','fixture',?)",
            (scope, ts(minute), op, hi, lo, close, vwap, ts(minute - 1)),
        )
    conn.execute(
        "INSERT INTO position_cycles(cycle_id,account_scope_id,symbol,direction,state,entry_strategy_key,opened_at,created_at,updated_at) VALUES ('cycle-open',?,'NVDA','LONG','OPEN',NULL,?,?,?)",
        (scope, ts(10), ts(11), ts(1)),
    )
    conn.execute(
        "INSERT INTO order_intents(intent_id,account_scope_id,cycle_id,client_order_id,symbol,side,purpose,order_type,time_in_force,quantity,status,created_at,updated_at) VALUES ('open-intent',?,'cycle-open','open-client','NVDA','BUY','ENTRY','MARKET','DAY','3','SUBMITTED',?,?)",
        (scope, ts(10), ts(10)),
    )
    conn.execute(
        "INSERT INTO broker_orders(account_scope_id,broker_order_id,intent_id,client_order_id,symbol,side,order_type,status,quantity,filled_quantity,filled_avg_price,submitted_at,updated_at) VALUES (?,'open-order','open-intent','open-client','NVDA','BUY','MARKET','FILLED','3','3','105',?,?)",
        (scope, ts(10), ts(10)),
    )
    conn.execute(
        "INSERT INTO fills(fill_id,account_scope_id,external_activity_id,broker_order_id,cycle_id,symbol,side,quantity,price,fee,filled_at,recorded_at) VALUES ('open-fill',?,'open-fill','open-order','cycle-open','NVDA','BUY','3','105','0',?,?)",
        (scope, ts(10), ts(10)),
    )
    conn.execute(
        "INSERT INTO positions(account_scope_id,symbol,cycle_id,management,direction,quantity,average_entry_price,market_price,market_value,unrealized_pl,as_of) VALUES (?,'NVDA','cycle-open','MANAGED','LONG','3','105','107','321','6',?)",
        (scope, ts(1)),
    )
    conn.execute(
        "INSERT INTO position_cycles(cycle_id,account_scope_id,symbol,direction,state,entry_strategy_key,opened_at,closed_at,created_at,updated_at) VALUES ('cycle-stop',?,'NVDA','LONG','CLOSED','vwap_bounce',?,?,?,?)",
        (scope, ts(20), ts(18), ts(21), ts(18)),
    )
    for iid, client, side, purpose in (
        ("stop-entry", "stop-entry-client", "BUY", "ENTRY"),
        ("stop-exit", "stop-exit-client", "SELL", "PROTECTIVE_STOP"),
    ):
        conn.execute(
            "INSERT INTO order_intents(intent_id,account_scope_id,cycle_id,signal_id,client_order_id,symbol,side,purpose,order_type,time_in_force,quantity,status,created_at,updated_at) VALUES (?,?,'cycle-stop',NULL,?,'NVDA',?,?,'STOP','GTC','2','SUBMITTED',?,?)",
            (iid, scope, client, side, purpose, ts(20), ts(20)),
        )
        oid = f"{iid}-order"
        conn.execute(
            "INSERT INTO broker_orders(account_scope_id,broker_order_id,intent_id,client_order_id,symbol,side,order_type,status,quantity,filled_quantity,filled_avg_price,stop_price,submitted_at,updated_at) VALUES (?,?,?,?, 'NVDA',?,'STOP','FILLED','2','2','99','99',?,?)",
            (scope, oid, iid, client, side, ts(20), ts(18)),
        )
    for fid, order, side, price, minute in (
        ("sf1", "stop-entry-order", "BUY", "100", 20),
        ("sf2", "stop-exit-order", "SELL", "99", 18),
    ):
        conn.execute(
            "INSERT INTO fills(fill_id,account_scope_id,external_activity_id,broker_order_id,cycle_id,symbol,side,quantity,price,fee,filled_at,recorded_at) VALUES (?,?,?,?,'cycle-stop','NVDA',?,'2',?,'0',?,?)",
            (fid, scope, fid, order, side, price, ts(minute), ts(minute)),
        )
    conn.execute(
        "INSERT INTO protection_orders(protection_order_id,account_scope_id,cycle_id,broker_order_id,kind,status,covered_quantity,stop_price,created_at,updated_at) VALUES ('fixed-stop',?,'cycle-stop','stop-exit-order','FIXED_STOP','TRIGGERED','2','99',?,?)",
        (scope, ts(19), ts(18)),
    )
    conn.execute(
        "INSERT INTO position_cycles(cycle_id,account_scope_id,symbol,direction,state,opened_at,created_at,updated_at) VALUES ('cycle-no-bars',?,'AAPL','LONG','OPEN',?,?,?)",
        (scope, ts(5), ts(6), ts(1)),
    )
    conn.close()
    settings = DashboardSettings(
        research_root=path.parent / "research", research_dataset_root=path.parent / "data"
    )
    return TestClient(create_app(settings, Clock(), path, None)), scope


@pytest.mark.parametrize(
    "mode", [AccountEnvironment.SHADOW, AccountEnvironment.PAPER, AccountEnvironment.LIVE]
)
def test_operational_modes_use_normalized_projection(
    tmp_path: Path, mode: AccountEnvironment
) -> None:
    client, scope = seed(tmp_path / f"{mode.value}.sqlite", mode)
    response = client.get(f"/api/accounts/{scope}/trades")
    assert response.status_code == 200
    trade = next(row for row in response.json()["items"] if row["trade_id"] == "cycle-1")
    assert trade["execution_mode"] == mode.value
    assert trade["entry_strategy_id"] == "vwap_bounce"
    assert trade["entry_strategy_version"] == "1.2.0"
    assert trade["entry_signal_id"] == "sig-win"
    assert trade["exit_strategy_id"] == "vwap_breakdown"
    assert trade["exit_reason"] == "STRATEGY_EXIT"
    assert trade["filled_quantity"] == "10" and trade["realized_pnl"] == "76.0"
    open_trade = next(row for row in response.json()["items"] if row["trade_id"] == "cycle-open")
    assert open_trade["entry_strategy_id"] is None
    assert open_trade["filled_quantity"] == "3" and open_trade["open_quantity"] == "3"
    assert open_trade["unrealized_pnl"] == "6" and open_trade["position_status"] == "OPEN"


def test_inspection_preserves_partial_fills_stops_markers_and_diagnostics(tmp_path: Path) -> None:
    client, scope = seed(tmp_path / "ops.sqlite")
    body = client.get(f"/api/accounts/{scope}/trades/cycle-1").json()
    assert len(body["fills"]) == 4
    assert [(row["side"], row["quantity"], row["price"]) for row in body["fills"]] == [
        ("BUY", "4", "100"),
        ("BUY", "6", "102"),
        ("SELL", "5", "110"),
        ("SELL", "5", "108"),
    ]
    assert body["trade"]["initial_stop_price"] == "98"
    assert body["trade"]["final_stop_price"] == "103"
    assert any(
        event["event_type"] == "STOP_MOVED" and event["price"] == "103" for event in body["events"]
    )
    assert body["diagnostics"]["best_price"] == "112"
    assert body["diagnostics"]["worst_price"] == "98"
    assert body["diagnostics"]["maximum_favorable_excursion"] == "108.0"
    # Only the first partial fill was known at the start of the 14:30 bar;
    # the later 14:30:05 fill is not back-applied to that bar's low.
    assert body["diagnostics"]["maximum_adverse_excursion"] == "-8"
    assert body["bars"][0]["timestamp_et"].endswith("-04:00")
    assert client.post(f"/api/accounts/{scope}/trades").status_code == 405
    fixed = next(
        row
        for row in client.get(f"/api/accounts/{scope}/trades").json()["items"]
        if row["trade_id"] == "cycle-stop"
    )
    assert fixed["exit_reason"] == "PROTECTIVE_STOP"
    assert fixed["exit_strategy_id"] is None and fixed["protective_stop_mode"] == "FIXED_STOP"
    missing = client.get(f"/api/accounts/{scope}/trades/cycle-no-bars").json()
    assert missing["bars_available"] is False
    assert missing["diagnostics"]["observations_available"] is False


def test_backtest_older_artifact_is_listed_without_invented_attribution_or_bars(
    tmp_path: Path,
) -> None:
    client, _ = seed(tmp_path / "ops.sqlite")
    root = tmp_path / "research" / "r730-reproduction-20261009T213852Z"
    root.mkdir(parents=True)
    run = "old-run"
    (root / f"{run}.manifest.json").write_text(
        json.dumps(
            {
                "run_id": run,
                "created_at": "2026-10-09T21:38:52Z",
                "symbols": ["NVDA"],
                "period_role": "RESEARCH",
                "data_revision": "missing-dataset",
                "config_hash": "abc",
                "strategies": [],
            }
        )
    )
    (root / f"{run}.results.json").write_text(
        json.dumps(
            {
                "cycles": [
                    {
                        "cycle_id": "old-cycle",
                        "symbol": "NVDA",
                        "opened_at": "2026-10-08T14:30:00Z",
                        "closed_at": "2026-10-08T14:31:00Z",
                        "entry_quantity": "1",
                        "exit_quantity": "1",
                    }
                ],
                "fills": [
                    {
                        "order_id": "b",
                        "side": "BUY",
                        "symbol": "NVDA",
                        "quantity": "1",
                        "price": "100",
                        "fee": "0",
                        "time": "2026-10-08T14:30:00Z",
                    },
                    {
                        "order_id": "s",
                        "side": "SELL",
                        "symbol": "NVDA",
                        "quantity": "1",
                        "price": "101",
                        "fee": "0",
                        "time": "2026-10-08T14:31:00Z",
                        "exit_reason": "STRATEGY_SELL",
                    },
                ],
            }
        )
    )
    runs = client.get("/api/research/runs").json()["runs"]
    listed = next(row for row in runs if row["run_id"] == run)
    assert listed["execution_at"] is None
    assert listed["execution_identity_uncertain"] is True
    run_detail = client.get(f"/api/research/runs/{listed['selection_id']}").json()
    assert run_detail["run"]["run_id"] == run
    assert run_detail["run"]["execution_at"] is None
    trade = client.get(f"/api/research/runs/{run}/trades").json()["items"][0]
    assert trade["execution_mode"] == "BACKTEST" and trade["entry_strategy_id"] is None
    assert trade["entry_signal_id"] is None and trade["attribution_complete"] is False
    assert trade["exit_reason"] == "STRATEGY_EXIT"
    detail = client.get(f"/api/research/runs/{run}/trades/old-cycle").json()
    assert detail["bars_available"] is False and "unavailable" in detail["bars_limitation"]


def test_execution_scoped_artifacts_appear_in_research_api(tmp_path: Path) -> None:
    client, _ = seed(tmp_path / "ops.sqlite")
    execution_id = "execution-123"
    run_id = "experiment-abc"
    artifact_dir = tmp_path / "research" / "executions" / execution_id
    artifact_dir.mkdir(parents=True)
    manifest = {
        "run_id": run_id,
        "execution_id": execution_id,
        "created_at": "2026-10-08T14:29:00Z",
        "started_at": "2026-10-08T14:29:00Z",
        "completed_at": "2026-10-08T14:32:00Z",
        "completion_status": "COMPLETED",
        "config_hash": "config-hash",
        "symbols": ["NVDA"],
        "window_start": "2026-10-08T14:30:00Z",
        "window_end": "2026-10-08T14:32:00Z",
        "period_role": "RESEARCH",
        "data_revision": "local-fixture",
        "dataset_hash": "dataset-hash",
        "starting_capital": "100000",
        "data_provenance": {"source": "local-parquet", "point_in_time": True},
        "strategies": [{"name": "vwap_bounce", "version": "1.0.0"},
                        {"name": "vwap_breakdown", "version": "1.0.0"}],
        "configuration": {
            "strategy_params": {"vwap_bounce": {"example": "value"}},
            "strategy_collections": {"NVDA": {"buy": ["vwap_bounce"],
                                                 "sell": ["vwap_breakdown"]}},
            "protective_stop": {"type": "fixed", "stop_percent": "2"},
        },
    }
    result = {
        "schema_version": "2",
        "run_id": run_id,
        "execution_id": execution_id,
        "cycles": [{
            "cycle_id": "cycle-1", "symbol": "NVDA",
            "opened_at": "2026-10-08T14:30:00Z", "closed_at": "2026-10-08T14:31:00Z",
            "entry_strategy": "vwap_bounce", "exit_strategy": "vwap_breakdown",
            "exit_reason": "STRATEGY_SELL",
        }],
        "fills": [
            {"fill_id": "buy-1", "cycle_id": "cycle-1", "side": "BUY", "symbol": "NVDA",
             "quantity": "2", "price": "100", "fee": "0", "time": "2026-10-08T14:30:00Z",
             "strategy": "vwap_bounce"},
            {"fill_id": "sell-1", "cycle_id": "cycle-1", "side": "SELL", "symbol": "NVDA",
             "quantity": "2", "price": "105", "fee": "0", "time": "2026-10-08T14:31:00Z",
             "strategy": "vwap_breakdown", "exit_reason": "STRATEGY_SELL"},
        ],
        "equity_curve": [{"equity": "100000"}, {"equity": "100010"}],
    }
    (artifact_dir / f"{run_id}.manifest.json").write_text(json.dumps(manifest))
    (artifact_dir / f"{run_id}.results.json").write_text(json.dumps(result))

    listed = client.get("/api/research/runs").json()["runs"]
    row = next(item for item in listed if item["execution_id"] == execution_id)
    assert row["selection_id"] == execution_id
    assert row["execution_at"] == "2026-10-08T14:29:00Z"
    assert row["buy_strategies"] == ["vwap_bounce"]
    assert row["sell_strategies"] == ["vwap_breakdown"]
    assert row["completed_trade_count"] == 1
    assert row["net_pnl"] == "10"

    run_detail = client.get(f"/api/research/runs/{execution_id}").json()
    assert run_detail["run"]["run_id"] == run_id
    assert run_detail["configuration"]["protective_stop"]["type"] == "fixed"
    trades = client.get(f"/api/research/runs/{execution_id}/trades").json()
    assert trades["total"] == 1
    trade = trades["items"][0]
    assert trade["entry_strategy_id"] == "vwap_bounce"
    assert trade["exit_strategy_id"] == "vwap_breakdown"
    assert trade["realized_pnl"] == "10"
    inspection = client.get(
        f"/api/research/runs/{execution_id}/trades/cycle-1"
    ).json()
    assert inspection["trade"]["exit_price"] == "105"


def test_account_scope_isolation_and_filters(tmp_path: Path) -> None:
    client, scope = seed(tmp_path / "ops.sqlite")
    assert client.get(f"/api/accounts/{scope}/trades?symbol=MSFT").json()["total"] == 0
    assert (
        client.get(f"/api/accounts/{scope}/trades?entry_strategy=momentum_breakout").json()["total"]
        == 0
    )
    assert client.get("/api/accounts/shadow:other/trades").status_code == 404
