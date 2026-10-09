"""Dashboard API integration tests: TestClient over a seeded, migrated SQLite DB."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mike_trade.dashboard.api import (
    DashboardSettings,
    compute_engine_state,
)
from mike_trade.dashboard.server import create_app, validate_bind
from mike_trade.domain.accounts import (
    AccountEnvironment,
    AccountScope,
    AccountSnapshot,
    HealthStatus,
)
from mike_trade.domain.errors import ConfigurationError
from mike_trade.domain.protection import ProtectionKind, ProtectionStatus, TrailingStopState
from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    BudgetPolicy,
    Fill,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    PositionCycle,
    TimeInForce,
)
from mike_trade.persistence.readstore import ReadStore
from mike_trade.persistence.repositories import (
    AccountScopeRepository,
    BrokerOrderRepository,
    CashEquityRepository,
    EntryControlRepository,
    FillRepository,
    OrderIntentRepository,
    PositionCycleRepository,
    ProtectionRepository,
    ReconciliationRepository,
    RunConfigRepository,
    ServiceHealthRepository,
)
from mike_trade.persistence.schema import initialize_database, to_db_timestamp
from mike_trade.persistence.transaction import WriterSession

# Thursday 2026-10-08 10:30 ET: regular session, before the 15:30 entry cutoff.
NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
SCOPE = AccountScope(AccountEnvironment.PAPER, "ACCT1")
OTHER = AccountScope(AccountEnvironment.PAPER, "ACCT2")
ACCOUNT = SCOPE.scope_id
D = Decimal


class MutableClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


class World:
    """A seeded database plus the writers used to change it between requests."""

    def __init__(self, db_path: Path, session: WriterSession, clock: MutableClock) -> None:
        self.db_path = db_path
        self.session = session
        self.clock = clock  # shared by writers and the dashboard
        self.writer = session.bind_scope(ACCOUNT)
        self.health = ServiceHealthRepository(self.writer, clock)

    def beat(self, component: str, status: HealthStatus = HealthStatus.HEALTHY,
             details: dict[str, str] | None = None, at: datetime | None = None) -> None:
        """Write a component heartbeat as of ``at`` and restore the shared clock."""
        saved = self.clock.current
        self.clock.current = at or saved
        try:
            self.health.heartbeat(component, status, details)
        finally:
            self.clock.current = saved

    def sql(self, statement: str, args: tuple[Any, ...] = ()) -> None:
        with self.writer.unit_of_work() as conn:
            conn.execute(statement, args)


def _seed(world: World) -> None:
    w, clock = world.writer, world.clock
    scopes = AccountScopeRepository(w, clock)
    scopes.register_scope(SCOPE, broker="alpaca")
    RunConfigRepository(w, clock).record_run_config({
        "risk": {"total_application_budget": "10000.00"},
        "symbols": {"AAPL": {"budget": "3000.00", "planned_loss_cap": "30.00",
                              "min_hold_seconds": 120, "max_entry_cycles_per_session": 1,
                              "consecutive_loss_limit": 2, "consecutive_loss_reset": "session"}},
    })
    CashEquityRepository(w, clock).append_equity_snapshot(AccountSnapshot(
        scope=SCOPE, as_of=NOW, cash=D("9000.00"), equity=D("10005.50"),
        buying_power=D("18000.00"), portfolio_value=D("10005.50"), source="alpaca_paper"))

    cycles = PositionCycleRepository(w, clock)
    cycles.open_cycle(PositionCycle(cycle_id="c1", account_scope_id=ACCOUNT, symbol="AAPL"))
    policy = BudgetPolicy(per_symbol_budget=D("3000"), total_application_budget=D("10000"),
                          max_open_positions=3)
    intents = OrderIntentRepository(w, clock, policy)
    intents.record_intent(OrderIntent(
        intent_id="i1", client_order_id="client-i1", account_scope_id=ACCOUNT, cycle_id="c1",
        symbol="AAPL", side=OrderSide.BUY, purpose=OrderPurpose.ENTRY,
        order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY, created_at=NOW,
        quantity=D("10")), D("1000"))
    BrokerOrderRepository(w, clock).upsert_broker_order(BrokerOrder(
        broker_order_id="b1", client_order_id="client-i1", account_scope_id=ACCOUNT,
        symbol="AAPL", side=OrderSide.BUY, order_type=OrderType.MARKET,
        status=BrokerOrderStatus.ACCEPTED, quantity=D("10"), filled_quantity=D("0"),
        filled_avg_price=None, submitted_at=NOW, updated_at=NOW + timedelta(seconds=1)))
    FillRepository(w, clock).apply_fill(Fill(
        fill_id="a1", broker_order_id="b1", account_scope_id=ACCOUNT, symbol="AAPL",
        side=OrderSide.BUY, quantity=D("10"), price=D("100.25"), fee=D("0.10"),
        filled_at=NOW + timedelta(seconds=5)))
    protection = ProtectionRepository(w, clock)
    protection.record_protection_order("p1", "c1", ProtectionKind.STEPPED_TRAILING_STOP,
                                       ProtectionStatus.ACTIVE, D("10"), stop_price=D("98.50"))
    protection.save_trailing_state(TrailingStopState(
        cycle_id="c1", account_scope_id=ACCOUNT, symbol="AAPL",
        kind=ProtectionKind.STEPPED_TRAILING_STOP, status=ProtectionStatus.ACTIVE,
        covered_quantity=D("10"), stop_price=D("98.50"), high_water_mark=D("101.00"),
        last_confirmed_at=NOW))
    protection.append_trailing_event("c1", "STOP_RAISE_PROPOSED", previous_stop_price=D("98.00"),
                                     new_stop_price=D("98.50"), payload={"step": "1"})
    EntryControlRepository(w, clock).increment_filled_entry_cycles("AAPL", date(2026, 10, 8))
    recon = ReconciliationRepository(w, clock)
    run = recon.start_run()
    recon.record_drift(run, "QUANTITY_MISMATCH", entity_ref="AAPL",
                       expected={"quantity": "10"}, actual={"quantity": "9"})
    recon.complete_run(run, status="DRIFTED")  # type: ignore[call-arg]

    now_text = to_db_timestamp(NOW)
    bar_text = to_db_timestamp(NOW - timedelta(minutes=1))
    with w.unit_of_work() as conn:
        conn.execute("INSERT INTO instruments(account_scope_id, symbol, observed_at) "
                     "VALUES (?, 'AAPL', ?)", (ACCOUNT, now_text))
        conn.execute(
            "INSERT INTO market_bars(account_scope_id, symbol, timeframe, bar_start, open_price, "
            "high_price, low_price, close_price, vwap, volume, feed, provenance, recorded_at) "
            "VALUES (?, 'AAPL', '1m', ?, '100', '101', '99', '100.75', '100.5', '1000', 'iex', "
            "'IEX', ?)", (ACCOUNT, bar_text, now_text))
        conn.execute(
            "INSERT INTO strategy_instances(instance_id, account_scope_id, strategy_key, "
            "strategy_version, role, symbol, parameters_json, enabled, created_at) VALUES "
            "('s-buy', ?, 'vwap_bounce_default', '1', 'BUY', 'AAPL', '{}', 1, ?), "
            "('s-sell', ?, 'vwap_breakdown_default', '1', 'SELL', 'AAPL', '{}', 1, ?)",
            (ACCOUNT, now_text, ACCOUNT, now_text))
        conn.execute(
            "INSERT INTO strategy_evaluations(evaluation_id, account_scope_id, instance_id, "
            "symbol, bar_time, status, reason, evaluated_at) VALUES "
            "('e1', ?, 's-sell', 'AAPL', ?, 'NO_SIGNAL', 'above_vwap', ?)",
            (ACCOUNT, bar_text, now_text))
        conn.execute(
            "INSERT INTO signals(signal_id, account_scope_id, instance_id, symbol, action, "
            "reference_price, reason, provenance_json, bar_time, generated_at) VALUES "
            "('sig1', ?, 's-buy', 'AAPL', 'ENTER_LONG', '100.00', 'vwap_bounce', "
            "'{\"feed\":\"iex\"}', ?, ?)", (ACCOUNT, bar_text, now_text))
        conn.execute(
            "INSERT INTO decisions(decision_id, account_scope_id, signal_id, outcome, reason, "
            "decided_at) VALUES ('d1', ?, 'sig1', 'SELECTED', 'budget_ok', ?)",
            (ACCOUNT, now_text))
        conn.execute("UPDATE order_intents SET signal_id = 'sig1' WHERE intent_id = 'i1'")
        conn.execute(
            "INSERT INTO positions(account_scope_id, symbol, cycle_id, management, direction, "
            "quantity, average_entry_price, market_price, market_value, unrealized_pl, as_of) "
            "VALUES (?, 'TSLA', NULL, 'UNMANAGED', 'LONG', '2', '200', '210', '420', '20', ?)",
            (ACCOUNT, now_text))

    world.beat("engine", details={"state": "RUNNING"})
    world.beat("broker", details={"rate_limit_errors": "0"})
    world.beat("feed", details={"reconnects": "2", "subscriptions": "1"})
    world.beat("scheduler", details={"lag_seconds": "1.5"})

    # A second account in the same database must never leak into ACCT1 values.
    other = session_scope(world, OTHER)
    AccountScopeRepository(other, clock).register_scope(OTHER, broker="alpaca")
    CashEquityRepository(other, clock).append_equity_snapshot(AccountSnapshot(
        scope=OTHER, as_of=NOW, cash=D("1.00"), equity=D("2.00"), buying_power=D("3.00"),
        source="alpaca_paper"))


def session_scope(world: World, scope: AccountScope):  # noqa: ANN201
    return world.session.bind_scope(scope.scope_id)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    db_path = tmp_path / "ops.sqlite3"
    initialize_database(db_path, AccountEnvironment.PAPER, now=NOW).close()
    session = WriterSession(db_path)
    built = World(db_path, session, MutableClock())
    _seed(built)
    yield built
    session.close()


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<html><body>mike dashboard</body></html>")
    (root / "assets" / "app.abc123.js").write_text("console.log('ok')")
    return root


def make_client(world: World, dist: Path | None = None,
                settings: DashboardSettings | None = None) -> TestClient:
    app = create_app(settings or DashboardSettings(), world.clock, world.db_path, dist)
    return TestClient(app)


@pytest.fixture
def client(world: World, dist: Path) -> TestClient:
    return make_client(world, dist)


def get(client: TestClient, path: str) -> dict[str, Any]:
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------- liveness & accounts


def test_live_does_not_touch_the_database(tmp_path: Path) -> None:
    app = create_app(DashboardSettings(), MutableClock(), tmp_path / "missing.sqlite3", None)
    response = TestClient(app).get("/api/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["server_time"] == "2026-10-08T14:30:00Z"
    assert not (tmp_path / "missing.sqlite3").exists()


def test_db_routes_return_503_when_database_missing_or_unmigrated(tmp_path: Path) -> None:
    missing = TestClient(create_app(DashboardSettings(), MutableClock(), tmp_path / "x", None))
    assert missing.get("/api/accounts").status_code == 503
    empty = tmp_path / "empty.sqlite3"
    sqlite3.connect(empty).close()
    unmigrated = TestClient(create_app(DashboardSettings(), MutableClock(), empty, None))
    assert unmigrated.get("/api/accounts").status_code == 503
    assert unmigrated.get(f"/api/accounts/{ACCOUNT}/status").status_code == 503
    # The dashboard never migrates: the file still has no tables.
    with sqlite3.connect(empty) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0


def test_accounts_lists_mode_scenario_and_label(world: World, dist: Path) -> None:
    settings = DashboardSettings(labels={ACCOUNT: "Paper main"})
    body = get(make_client(world, dist, settings), "/api/accounts")
    by_id = {row["account_id"]: row for row in body["accounts"]}
    assert set(by_id) == {ACCOUNT, OTHER.scope_id}
    assert by_id[ACCOUNT] == {"account_id": ACCOUNT, "mode": "PAPER", "scenario": None,
                              "label": "Paper main"}
    assert by_id[OTHER.scope_id]["label"] == OTHER.scope_id


# ---------------------------------------------------------------------------- status


def test_status_shape_when_running(client: TestClient) -> None:
    body = get(client, f"/api/accounts/{ACCOUNT}/status")
    assert body["engine_state"] == "RUNNING"
    assert body["mode"] == "PAPER"
    assert body["last_heartbeat_at"] == "2026-10-08T14:30:00Z"
    assert body["heartbeat_age_seconds"] == 0
    assert body["is_stale"] is False and body["fresh"] is True
    assert body["broker"]["connected"] is True and body["broker"]["fresh"] is True
    assert body["data"]["connected"] is True and body["data"]["fresh"] is True
    assert body["market"] == "OPEN"
    assert body["exchange_clock"]["now_et"].startswith("2026-10-08T10:30:00-04:00")
    assert body["exchange_clock"]["session_status"] == "REGULAR"
    cutoff = body["next_entry_cutoff"]
    assert cutoff["at"] == "2026-10-08T19:30:00Z"
    assert cutoff["at_et"].startswith("2026-10-08T15:30:00-04:00")
    assert body["server_time"] == "2026-10-08T14:30:00Z"
    # The seeded unresolved reconciliation drift blocks entries, with a reason.
    assert body["entries_allowed"] is False
    assert "unresolved_reconciliation_drift" in body["entries_blocked_reasons"]


def test_entries_allowed_once_drift_is_absent(world: World, dist: Path) -> None:
    world.sql("INSERT INTO reconciliation_resolutions(resolution_id, account_scope_id, drift_id, "
              "resolution_kind, resolved_by, resolved_at) "
              "SELECT 'r1', account_scope_id, drift_id, 'ACK', 'op', ? FROM reconciliation_drifts",
              (to_db_timestamp(NOW),))
    body = get(make_client(world, dist), f"/api/accounts/{ACCOUNT}/status")
    assert body["entries_allowed"] is True and body["entries_blocked_reasons"] == []


def test_offline_is_computed_from_heartbeat_age_with_injected_clock(
    world: World, client: TestClient
) -> None:
    status = f"/api/accounts/{ACCOUNT}/status"
    world.clock.current = NOW + timedelta(seconds=20)  # stale (>15s) but not offline
    body = get(client, status)
    assert body["engine_state"] == "RUNNING" and body["is_stale"] is True
    world.clock.current = NOW + timedelta(seconds=60)  # exactly at the threshold
    assert get(client, status)["engine_state"] == "RUNNING"
    world.clock.current = NOW + timedelta(seconds=61)
    body = get(client, status)
    assert body["engine_state"] == "OFFLINE"
    assert body["heartbeat_age_seconds"] == 61
    assert body["entries_allowed"] is False
    assert body["entries_blocked_reasons"][0] == "engine_offline"
    assert body["fresh"] is False and body["stale_reason"] == "engine_offline"


def test_engine_written_offline_status_is_ignored(world: World, client: TestClient) -> None:
    world.beat("engine", HealthStatus.OFFLINE, {"state": "RUNNING"})
    assert get(client, f"/api/accounts/{ACCOUNT}/status")["engine_state"] == "RUNNING"


def test_offline_marks_every_value_stale(world: World, client: TestClient) -> None:
    world.clock.current = NOW + timedelta(minutes=5)
    kpis = get(client, f"/api/accounts/{ACCOUNT}/kpis")
    for key in ("cash", "equity", "buying_power", "deployed_capital", "open_position_count",
                "daily_realized_pnl", "protection_error_count"):
        assert kpis[key]["fresh"] is False, key
        assert kpis[key]["stale_reason"] == "engine_offline", key
    row = get(client, f"/api/accounts/{ACCOUNT}/watchlist")["symbols"][0]
    for key in ("confirmed_quantity", "protection_status", "native_broker_hwm",
                "desired_stop", "phase", "price"):
        assert row[key]["fresh"] is False and row[key]["stale_reason"] == "engine_offline", key


def test_feed_stale_with_fresh_heartbeat_is_degraded_and_blocks_entries(
    world: World, client: TestClient
) -> None:
    world.beat("feed", at=NOW - timedelta(minutes=5), details={"reconnects": "2"})
    body = get(client, f"/api/accounts/{ACCOUNT}/status")
    assert body["engine_state"] == "DEGRADED"
    assert body["entries_allowed"] is False
    assert "feed_stale" in body["entries_blocked_reasons"]
    assert body["data"]["fresh"] is False and body["data"]["stale_reason"] == "feed_stale"
    row = get(client, f"/api/accounts/{ACCOUNT}/watchlist")["symbols"][0]
    assert row["price"]["fresh"] is False and row["price"]["stale_reason"] == "feed_stale"
    assert row["data_stale"] is True


def test_broker_disconnect_never_reports_broker_truth_fresh(
    world: World, client: TestClient
) -> None:
    world.beat("broker", HealthStatus.OFFLINE)
    status = get(client, f"/api/accounts/{ACCOUNT}/status")
    assert status["engine_state"] == "RUNNING"
    assert status["broker"]["connected"] is False
    assert status["broker"]["stale_reason"] == "broker_disconnected"
    kpis = get(client, f"/api/accounts/{ACCOUNT}/kpis")
    for key in ("cash", "equity", "buying_power", "open_position_count"):
        assert kpis[key]["fresh"] is False and kpis[key]["stale_reason"] == "broker_disconnected"
    row = get(client, f"/api/accounts/{ACCOUNT}/watchlist")["symbols"][0]
    for key in ("confirmed_quantity", "native_broker_hwm", "stepped_anchor",
                "confirmed_stop", "protection_status"):
        assert row[key]["fresh"] is False, key
        assert row[key]["stale_reason"] == "broker_disconnected", key
    detail = get(client, f"/api/accounts/{ACCOUNT}/detail")
    assert detail["balances"]["fresh"] is False
    assert all(p["fresh"] is False for p in detail["positions"])
    errors = get(client, f"/api/accounts/{ACCOUNT}/health")["errors"]
    assert any(e["code"] == "BROKER_DISCONNECTED" for e in errors)


def test_compute_engine_state_unit() -> None:
    offline_after = timedelta(seconds=60)
    kwargs = {"now": NOW, "offline_after": offline_after}
    assert compute_engine_state(heartbeat_at=None, **kwargs) == "OFFLINE"
    assert compute_engine_state(heartbeat_at=NOW - timedelta(seconds=61), **kwargs) == "OFFLINE"
    assert compute_engine_state(heartbeat_at=NOW - timedelta(seconds=60), **kwargs) == "RUNNING"
    assert compute_engine_state(heartbeat_at=NOW, paused=True, **kwargs) == "PAUSED"
    assert compute_engine_state(heartbeat_at=NOW, feed_stale=True, **kwargs) == "DEGRADED"
    assert compute_engine_state(heartbeat_at=NOW, reported_state="STARTING",
                                **kwargs) == "RECONCILING"
    assert compute_engine_state(heartbeat_at=NOW + timedelta(seconds=5), **kwargs) == "RUNNING"


# ------------------------------------------------------------------------------ KPIs


def test_kpis_shape_and_decimal_strings(client: TestClient) -> None:
    body = get(client, f"/api/accounts/{ACCOUNT}/kpis")
    assert body["cash"]["value"] == "9000.00"
    assert body["equity"]["value"] == "10005.50"
    assert body["buying_power"]["value"] == "18000.00"
    assert body["cash"]["provenance"] == "equity_snapshots:alpaca_paper"
    assert body["cash"]["as_of"] == "2026-10-08T14:30:00Z"
    assert body["cash"]["fresh"] is True and body["cash"]["stale_reason"] is None
    assert body["configured_total_budget"]["value"] == "10000.00"
    assert body["reserved_pending_buys"]["value"] == "0"
    assert body["deployed_capital"]["value"] == "1002.50"
    assert body["open_position_count"]["value"] == 1  # the UNMANAGED holding is excluded
    assert body["pending_order_count"]["value"] == 1  # b1 stays ACCEPTED until FILLED
    assert body["daily_realized_pnl"]["value"] == "0"
    assert body["daily_unrealized_pnl"]["value"] is None  # no mark on the managed position
    assert body["daily_unrealized_pnl"]["stale_reason"] == "unavailable"
    assert body["reconciliation_error_count"]["value"] == 1
    for key in ("cash", "equity", "buying_power", "configured_total_budget",
                "reserved_pending_buys", "deployed_capital", "daily_realized_pnl",
                "lifetime_realized_pnl"):
        assert isinstance(body[key]["value"], str), key
    # No cross-account aggregation: ACCT2's cash of 1.00 is not mixed in.
    other = get(client, f"/api/accounts/{OTHER.scope_id}/kpis")
    assert other["cash"]["value"] == "1.00" and other["open_position_count"]["value"] == 0


# -------------------------------------------------------------------------- watchlist


def test_watchlist_row_has_every_required_field(client: TestClient) -> None:
    body = get(client, f"/api/accounts/{ACCOUNT}/watchlist")
    assert body["engine_state"] == "RUNNING"
    assert [row["symbol"] for row in body["symbols"]] == ["AAPL"]
    row = body["symbols"][0]
    expected = {
        "symbol", "configured", "price", "bid", "ask", "data_age_seconds", "data_stale", "feed",
        "phase", "active_buy_strategies", "active_sell_strategies", "next_due_trigger",
        "last_evaluation", "last_signal", "confirmed_quantity", "cost_basis", "purchase_time",
        "symbol_budget", "commitment", "planned_loss", "daily_entry_count", "daily_entry_cap",
        "loss_streak", "lockout_active", "lockout_until", "loss_reset_mode", "cooldown_until",
        "next_eligible_at", "min_hold_remaining_seconds", "entry_cutoff", "protection_mode",
        "protection_status", "protection_price", "protection_quantity", "native_broker_hwm",
        "stepped_anchor", "desired_stop", "confirmed_stop", "last_protection_update",
        "realized_pnl_today", "unrealized_pnl", "lifetime_realized_pnl", "lifetime_pnl",
    }
    assert set(row) == expected
    assert row["price"]["value"] == "100.75" and row["price"]["provenance"] == "market_bars:iex:IEX"
    assert row["feed"]["value"] == "iex (IEX)"
    assert row["bid"]["value"] is None and row["bid"]["stale_reason"] == "unavailable"
    assert row["ask"]["stale_reason"] == "unavailable"
    assert row["data_age_seconds"]["value"] == 60
    assert row["phase"]["value"] == "WATCHING_SELL"
    assert row["active_buy_strategies"]["value"] == ["vwap_bounce_default"]
    assert row["active_sell_strategies"]["value"] == ["vwap_breakdown_default"]
    assert row["last_evaluation"]["value"]["status"] == "NO_SIGNAL"
    signal = row["last_signal"]["value"]
    assert signal["disposition"] == "ACCEPTED" and signal["disposition_reason"] == "budget_ok"
    assert signal["reference_price"] == "100.00"
    assert row["confirmed_quantity"]["value"] == "10"
    assert row["cost_basis"]["value"] == "1002.50"
    assert row["purchase_time"]["value"] == "2026-10-08T14:30:05Z"
    assert row["symbol_budget"]["value"] == "3000.00" and row["planned_loss"]["value"] == "30.00"
    assert row["commitment"]["value"] == "1002.50"
    assert row["daily_entry_count"]["value"] == 1 and row["daily_entry_cap"]["value"] == 1
    assert row["loss_reset_mode"]["value"] == "session"
    assert row["min_hold_remaining_seconds"]["value"] == 125
    assert row["entry_cutoff"]["value"] == "2026-10-08T19:30:00Z"
    assert row["protection_mode"]["value"] == "STEPPED_TRAILING_STOP"
    assert row["protection_status"]["value"] == "ACTIVE"
    assert row["protection_price"]["value"] == "98.50"
    assert row["protection_quantity"]["value"] == "10"
    assert row["stepped_anchor"]["value"] == "101.00"
    assert row["native_broker_hwm"]["value"] is None
    assert row["desired_stop"]["value"] == "98.50"
    assert row["last_protection_update"]["value"] is not None
    assert row["confirmed_quantity"]["fresh"] is True
    assert row["lifetime_realized_pnl"]["value"] == "0"


def test_watchlist_is_capped_at_ten_symbols(world: World, dist: Path) -> None:
    now_text = to_db_timestamp(NOW)
    with world.writer.unit_of_work() as conn:
        for index in range(15):
            conn.execute("INSERT INTO instruments(account_scope_id, symbol, observed_at) "
                         "VALUES (?, ?, ?)", (ACCOUNT, f"SYM{index:02d}", now_text))
    body = get(make_client(world, dist), f"/api/accounts/{ACCOUNT}/watchlist")
    assert len(body["symbols"]) == 10


# ---------------------------------------------------------------------- symbol detail


def test_symbol_detail_timeline_evidence_tree_history_and_cycles(client: TestClient) -> None:
    body = get(client, f"/api/accounts/{ACCOUNT}/symbols/aapl")
    assert body["symbol"] == "AAPL" and body["fresh"] is True
    kinds = {(item["kind"], item["status"]) for item in body["timeline"]}
    assert ("EVALUATION", "NO_SIGNAL") in kinds
    assert ("SIGNAL", "ACCEPTED") in kinds
    assert body["evidence"][0]["provenance"] == {"feed": "iex"}
    assert body["feed_provenance"] == {
        "feed": "iex", "provenance": "IEX", "consolidated": False,
        "last_bar_at": "2026-10-08T14:29:00Z", "recorded_at": "2026-10-08T14:30:00Z"}
    tree = body["order_tree"]
    assert tree[0]["cycle_id"] == "c1"
    assert tree[0]["orders"][0]["broker_order_id"] == "b1"
    assert tree[0]["orders"][0]["purpose"] == "ENTRY"
    assert tree[0]["orders"][0]["quantity"] == "10"
    event = next(e for e in body["protection_history"] if e["event_type"] == "STOP_RAISE_PROPOSED")
    assert event["previous_stop_price"] == "98.00" and event["proposed_stop_price"] == "98.50"
    assert event["confirmed_stop_price"] is None
    cycle = body["managed_cycles"][0]
    assert cycle["cycle_id"] == "c1" and cycle["state"] == "OPEN"
    assert cycle["entry_reference_price"] == "100.00"
    assert cycle["entry_average_price"] == "100.25"
    assert cycle["entry_slippage"] == "0.25"
    assert cycle["decision_reasons"] == ["budget_ok"]
    assert len(body["managed_cycles"]) <= 20


def test_symbol_without_data_returns_empty_collections(client: TestClient) -> None:
    body = get(client, f"/api/accounts/{ACCOUNT}/symbols/NVDA")
    assert body["timeline"] == [] and body["order_tree"] == [] and body["managed_cycles"] == []
    assert body["feed_provenance"]["feed"] is None


# ----------------------------------------------------------------------------- detail


def test_detail_shape(client: TestClient) -> None:
    body = get(client, f"/api/accounts/{ACCOUNT}/detail")
    balances = body["balances"]
    assert balances["cash"] == "9000.00" and balances["portfolio_value"] == "10005.50"
    assert balances["source"] == "alpaca_paper" and balances["fresh"] is True
    assert [p["symbol"] for p in body["positions"]] == ["AAPL"]
    assert body["positions"][0]["quantity"] == "10"
    assert body["positions"][0]["average_entry_price"] == "100.25"
    assert [p["symbol"] for p in body["external_holdings"]] == ["TSLA"]
    assert body["external_holdings"][0]["management"] == "UNMANAGED"
    assert body["orders"][0]["broker_order_id"] == "b1"
    assert body["total_fees"] == "0.10"
    drift = body["reconciliation"]
    assert drift["unresolved_drift_count"] == 1
    assert drift["drifts"][0]["drift_type"] == "QUANTITY_MISMATCH"
    assert drift["drifts"][0]["expected"] == {"quantity": "10"}
    assert drift["last_result"] == "DRIFTED"


# ----------------------------------------------------------------------------- health


def test_health_shape_and_actionable_errors(world: World, dist: Path, tmp_path: Path) -> None:
    client = make_client(world, dist, DashboardSettings(backup_dir=tmp_path / "no-backups"))
    body = get(client, f"/api/accounts/{ACCOUNT}/health")
    assert body["feed"]["reconnects"]["value"] == 2
    assert body["feed"]["subscription_count"]["value"] == 1
    assert body["feed"]["link"]["connected"] is True
    assert body["broker"]["rate_limit_errors"]["value"] == 0
    assert body["scheduler_lag_seconds"]["value"] == 1.5
    persistence = body["persistence"]
    assert persistence["schema_version"] == 1 and persistence["size_bytes"] > 0
    assert persistence["last_backup_at"]["stale_reason"] == "unavailable"
    assert body["last_reconciliation_at"]["value"] == "2026-10-08T14:30:00Z"
    codes = {e["code"]: e for e in body["errors"]}
    assert {"RECONCILIATION_DRIFT", "BACKUP_UNAVAILABLE"} <= set(codes)
    assert codes["RECONCILIATION_DRIFT"]["severity"] == "critical"
    assert all(e["command"] == "make doctor" for e in body["errors"])


def test_health_reports_engine_offline_error(world: World, client: TestClient) -> None:
    world.clock.current = NOW + timedelta(minutes=10)
    codes = {e["code"] for e in get(client, f"/api/accounts/{ACCOUNT}/health")["errors"]}
    assert "ENGINE_OFFLINE" in codes


def test_health_reads_latest_backup_manifest(world: World, dist: Path, tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "paper-1.sqlite3.manifest.json").write_text(
        '{"created_at": "2026-10-08T12:00:00Z", "integrity_ok": true}')
    client = make_client(world, dist, DashboardSettings(backup_dir=backups))
    persistence = get(client, f"/api/accounts/{ACCOUNT}/health")["persistence"]
    assert persistence["last_backup_at"]["value"] == "2026-10-08T12:00:00Z"
    assert persistence["last_backup_integrity_ok"] is True


# ------------------------------------------------------------------- scoping & 404


@pytest.mark.parametrize("suffix", ["status", "kpis", "watchlist", "symbols/AAPL", "detail",
                                    "health"])
def test_unknown_account_returns_404(client: TestClient, suffix: str) -> None:
    response = client.get(f"/api/accounts/paper:NOPE/{suffix}")
    assert response.status_code == 404
    assert "unknown account_id" in response.json()["detail"]


def test_account_data_is_not_visible_through_other_account(client: TestClient) -> None:
    other = f"/api/accounts/{OTHER.scope_id}"
    assert get(client, f"{other}/watchlist")["symbols"] == []
    assert get(client, f"{other}/detail")["positions"] == []
    assert get(client, f"{other}/detail")["external_holdings"] == []
    assert get(client, f"{other}/detail")["balances"]["cash"] == "1.00"


# ------------------------------------------------------------- read-only guarantees


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete", "options"])
@pytest.mark.parametrize("path", ["/api/accounts", f"/api/accounts/{ACCOUNT}/status",
                                  "/api/nope", "/", "/some/ui/route"])
def test_non_get_methods_are_rejected_with_405(client: TestClient, method: str, path: str) -> None:
    response = getattr(client, method)(path)
    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"


def test_no_write_routes_are_registered(world: World, dist: Path) -> None:
    app = create_app(DashboardSettings(), world.clock, world.db_path, dist)
    methods = {m for route in app.routes for m in getattr(route, "methods", None) or ()}
    assert methods <= {"GET", "HEAD"}


def test_requests_do_not_modify_the_database(world: World, client: TestClient) -> None:
    def fingerprint() -> tuple[int, int, int]:
        with sqlite3.connect(f"file:{world.db_path}?mode=ro", uri=True) as conn:
            return tuple(conn.execute(  # type: ignore[return-value]
                "SELECT (SELECT COUNT(*) FROM domain_events), (SELECT COUNT(*) FROM fills), "
                "(SELECT COUNT(*) FROM service_health)").fetchone())

    before = fingerprint()
    for suffix in ("status", "kpis", "watchlist", "symbols/AAPL", "detail", "health"):
        get(client, f"/api/accounts/{ACCOUNT}/{suffix}")
    assert fingerprint() == before


def test_service_connection_is_read_only(world: World) -> None:
    with ReadStore.open(world.db_path) as store, store.snapshot(ACCOUNT) as conn:
        with pytest.raises(Exception, match="read-only|readonly|not authorized"):
            conn.execute("DELETE FROM positions")


def test_dashboard_import_pulls_in_no_broker_or_market_data_modules() -> None:
    code = (
        "import sys, mike_trade.dashboard.server; "
        "bad = [m for m in sys.modules if m.startswith(('alpaca', 'mike_trade.brokers', "
        "'mike_trade.market_data', 'mike_trade.execution', 'mike_trade.research'))]; "
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


# --------------------------------------------------------------------------- static UI


def test_static_index_assets_and_spa_fallback(client: TestClient) -> None:
    index = client.get("/")
    assert index.status_code == 200 and "mike dashboard" in index.text
    asset = client.get("/assets/app.abc123.js")
    assert asset.status_code == 200 and "console.log" in asset.text
    assert "immutable" in asset.headers["cache-control"]
    fallback = client.get("/accounts/paper:ACCT1/watchlist")
    assert fallback.status_code == 200 and "mike dashboard" in fallback.text
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/api/does-not-exist").status_code == 404
    assert "root:" not in client.get("/..%2f..%2fetc/passwd").text
    assert "root:" not in client.get("/%2e%2e/%2e%2e/etc/passwd").text


def test_missing_dist_returns_503_for_ui_but_api_keeps_working(
    world: World, tmp_path: Path
) -> None:
    client = make_client(world, tmp_path / "no-dist")
    ui = client.get("/")
    assert ui.status_code == 503 and "frontend build not found" in ui.json()["detail"]
    assert client.get("/some/route").status_code == 503
    assert client.get("/api/live").status_code == 200
    assert client.get(f"/api/accounts/{ACCOUNT}/status").status_code == 200


# ------------------------------------------------------------------------- bind policy


def test_non_loopback_bind_requires_explicit_opt_in(caplog: pytest.LogCaptureFixture) -> None:
    validate_bind(DashboardSettings())
    validate_bind(DashboardSettings(bind_host="::1"))
    with pytest.raises(ConfigurationError):
        validate_bind(DashboardSettings(bind_host="0.0.0.0"))
    with caplog.at_level("WARNING"):
        validate_bind(DashboardSettings(bind_host="0.0.0.0", allow_non_loopback=True))
    assert any("private auth/TLS/VPN/reverse proxy" in r.getMessage() for r in caplog.records)


def test_settings_from_env_defaults_to_loopback() -> None:
    settings = DashboardSettings.from_env({})
    assert settings.bind_host == "127.0.0.1" and settings.port == 8765
    assert settings.allow_non_loopback is False
    custom = DashboardSettings.from_env({"MIKE_TRADE_DASHBOARD_BIND": "0.0.0.0:9000"})
    assert (custom.bind_host, custom.port) == ("0.0.0.0", 9000)
