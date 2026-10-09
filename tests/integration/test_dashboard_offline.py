"""Phase R: dashboard offline/stale isolation acceptance tests.

Proves the read-only dashboard keeps serving last-known values (marked stale)
when the engine is killed, the feed dies, or the broker disconnects, plus
partial-fill protection coverage, native-vs-stepped trailing display, and
visible budget/cutoff/lockout/cooldown reasons.

Method: a temporary migrated SQLite DB seeded through the persistence layer
(repositories + ``WriterSession``; never through the dashboard) and an injected
controllable clock shared by the writers and the dashboard app. No network, no
broker credentials, no running engine. A short-lived heartbeat thread stands in
for the engine process and is stopped ("killed") before the clock advances.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from mike_trade.dashboard.api import DashboardSettings
from mike_trade.dashboard.server import create_app
from mike_trade.domain.accounts import (
    AccountEnvironment,
    AccountScope,
    AccountSnapshot,
    HealthStatus,
)
from mike_trade.domain.protection import (
    ProtectionKind,
    ProtectionStatus,
    TrailingStopState,
)
from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    BudgetPolicy,
    EntryEligibilityPolicy,
    Fill,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    PositionCycle,
    TimeInForce,
)
from mike_trade.persistence.repositories import (
    AccountScopeRepository,
    BrokerOrderRepository,
    CashEquityRepository,
    EntryControlRepository,
    FillRepository,
    OrderIntentRepository,
    PositionCycleRepository,
    ProtectionRepository,
    RunConfigRepository,
    ServiceHealthRepository,
)
from mike_trade.persistence.schema import initialize_database, to_db_timestamp
from mike_trade.persistence.transaction import WriterSession

# Thursday 2026-10-08 10:30 ET: regular session, before the 15:30 entry cutoff.
NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
D = Decimal


class Clock:
    """Injected controllable clock shared by writers and the dashboard."""

    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


def _session(db_path: Path, clock: Clock) -> WriterSession:
    initialize_database(db_path, AccountEnvironment.PAPER, now=NOW).close()
    return WriterSession(db_path)


def _seed_base(
    session: WriterSession,
    clock: Clock,
    scope: AccountScope,
    *,
    symbols: dict[str, dict[str, Any]] | None = None,
) -> Any:
    """Register a PAPER scope with budget config, cash snapshot and fresh links."""
    writer = session.bind_scope(scope.scope_id)
    AccountScopeRepository(writer, clock).register_scope(scope, broker="alpaca")
    RunConfigRepository(writer, clock).record_run_config(
        {
            "risk": {"total_application_budget": "10000.00"},
            "symbols": symbols
            if symbols is not None
            else {
                "AAPL": {
                    "budget": "3000.00",
                    "planned_loss_cap": "30.00",
                    "min_hold_seconds": 120,
                    "max_entry_cycles_per_session": 1,
                    "consecutive_loss_limit": 2,
                    "consecutive_loss_reset": "session",
                }
            },
        }
    )
    CashEquityRepository(writer, clock).append_equity_snapshot(
        AccountSnapshot(
            scope=scope,
            as_of=NOW,
            cash=D("9000.00"),
            equity=D("10005.50"),
            buying_power=D("18000.00"),
            portfolio_value=D("10005.50"),
            source="alpaca_paper",
        )
    )
    health = ServiceHealthRepository(writer, clock)
    health.heartbeat("engine", HealthStatus.HEALTHY, {"state": "RUNNING"})
    health.heartbeat("broker", HealthStatus.HEALTHY, {"rate_limit_errors": "0"})
    health.heartbeat("feed", HealthStatus.HEALTHY, {"reconnects": "1", "subscriptions": "1"})
    health.heartbeat("scheduler", HealthStatus.HEALTHY, {"lag_seconds": "1.0"})
    return writer


def _add_bar(
    writer: Any,
    scope_id: str,
    symbol: str,
    *,
    price: str = "100.75",
    at: datetime = NOW - timedelta(minutes=1),
) -> None:
    with writer.unit_of_work() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO instruments(account_scope_id, symbol, observed_at) "
            "VALUES (?, ?, ?)",
            (scope_id, symbol, to_db_timestamp(NOW)),
        )
        conn.execute(
            "INSERT INTO market_bars(account_scope_id, symbol, timeframe, bar_start, "
            "open_price, high_price, low_price, close_price, vwap, volume, feed, "
            "provenance, recorded_at) VALUES (?, ?, '1m', ?, '100', '101', '99', ?, "
            "'100.5', '1000', 'iex', 'IEX', ?)",
            (scope_id, symbol, to_db_timestamp(at), price, to_db_timestamp(NOW)),
        )


def _entry_fill(
    session: WriterSession,
    clock: Clock,
    scope_id: str,
    *,
    cycle_id: str,
    symbol: str,
    order_qty: Decimal,
    fill_qty: Decimal,
    price: Decimal,
    tag: str,
    at: datetime = NOW + timedelta(seconds=5),
) -> None:
    """Open a cycle, record a BUY intent/order and apply one (possibly partial) fill."""
    writer = session.bind_scope(scope_id)
    PositionCycleRepository(writer, clock).open_cycle(
        PositionCycle(cycle_id=cycle_id, account_scope_id=scope_id, symbol=symbol)
    )
    policy = BudgetPolicy(
        per_symbol_budget=D("3000"),
        total_application_budget=D("10000"),
        max_open_positions=3,
    )
    OrderIntentRepository(writer, clock, policy).record_intent(
        OrderIntent(
            intent_id=f"i-{tag}",
            client_order_id=f"client-{tag}",
            account_scope_id=scope_id,
            cycle_id=cycle_id,
            symbol=symbol,
            side=OrderSide.BUY,
            purpose=OrderPurpose.ENTRY,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            created_at=at,
            quantity=order_qty,
        ),
        D("1000"),
    )
    BrokerOrderRepository(writer, clock).upsert_broker_order(
        BrokerOrder(
            broker_order_id=f"b-{tag}",
            client_order_id=f"client-{tag}",
            account_scope_id=scope_id,
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            status=BrokerOrderStatus.ACCEPTED,
            quantity=order_qty,
            filled_quantity=D("0"),
            filled_avg_price=None,
            submitted_at=at,
            updated_at=at,
        )
    )
    FillRepository(writer, clock).apply_fill(
        Fill(
            fill_id=f"f-{tag}",
            broker_order_id=f"b-{tag}",
            account_scope_id=scope_id,
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=fill_qty,
            price=price,
            fee=D("0.01"),
            filled_at=at,
        )
    )


def _protect(
    session: WriterSession,
    clock: Clock,
    scope_id: str,
    *,
    protection_id: str,
    cycle_id: str,
    kind: ProtectionKind,
    covered: Decimal,
    stop: Decimal,
    stop_broker_id: str | None = None,
    stop_confirmed: Decimal | None = None,
    symbol: str = "AAPL",
) -> None:
    """Record a protective stop order plus its (optionally distinct) broker stop."""
    writer = session.bind_scope(scope_id)
    if stop_broker_id is not None:
        BrokerOrderRepository(writer, clock).upsert_broker_order(
            BrokerOrder(
                broker_order_id=stop_broker_id,
                client_order_id=f"client-{stop_broker_id}",
                account_scope_id=scope_id,
                symbol=symbol,
                side=OrderSide.SELL,
                order_type=OrderType.STOP,
                status=BrokerOrderStatus.ACCEPTED,
                quantity=covered,
                filled_quantity=D("0"),
                filled_avg_price=None,
                submitted_at=NOW,
                updated_at=NOW,
                stop_price=stop_confirmed if stop_confirmed is not None else stop,
            )
        )
    ProtectionRepository(writer, clock).record_protection_order(
        protection_id,
        cycle_id,
        kind,
        ProtectionStatus.ACTIVE,
        covered,
        stop_price=stop,
        broker_order_id=stop_broker_id,
    )


def _trail(
    session: WriterSession,
    clock: Clock,
    scope_id: str,
    *,
    cycle_id: str,
    symbol: str,
    kind: ProtectionKind,
    covered: Decimal,
    stop: Decimal,
    hwm: Decimal,
    stop_broker_id: str | None = None,
) -> None:
    ProtectionRepository(session.bind_scope(scope_id), clock).save_trailing_state(
        TrailingStopState(
            cycle_id=cycle_id,
            account_scope_id=scope_id,
            symbol=symbol,
            kind=kind,
            status=ProtectionStatus.ACTIVE,
            covered_quantity=covered,
            stop_price=stop,
            high_water_mark=hwm,
            broker_order_id=stop_broker_id,
            last_confirmed_at=NOW,
        )
    )


def _client(
    db_path: Path, clock: Clock, settings: DashboardSettings | None = None
) -> TestClient:
    return TestClient(create_app(settings or DashboardSettings(), clock, db_path, None))


def _get(client: TestClient, path: str) -> dict[str, Any]:
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------ (1) engine killed


def test_engine_killed_offline_serves_stale_last_known(tmp_path: Path) -> None:
    """A killed heartbeat writer + clock past the threshold => OFFLINE, /api/live OK."""
    db_path = tmp_path / "ops.sqlite3"
    clock = Clock()
    session = _session(db_path, clock)
    scope = AccountScope(AccountEnvironment.PAPER, "OFF1")
    writer = _seed_base(session, clock, scope)
    _add_bar(writer, scope.scope_id, "AAPL")
    _entry_fill(
        session, clock, scope.scope_id, cycle_id="c1", symbol="AAPL",
        order_qty=D("10"), fill_qty=D("10"), price=D("100.25"), tag="e1",
    )
    # The writer lock is exclusive: close the seeding session so the stand-in
    # engine thread can open its own session on the same file. Nothing after
    # the kill needs a writer (the dashboard only reads).
    session.close()
    # Short-lived heartbeat thread: the stand-in engine process. Setting the
    # stop event and joining is the "kill": no more heartbeats are written.
    # It must use its own WriterSession: SQLite connections are thread-bound.
    stop = threading.Event()
    beats: list[datetime] = []

    def _engine_loop() -> None:
        engine_session = WriterSession(db_path)
        try:
            health = ServiceHealthRepository(
                engine_session.bind_scope(scope.scope_id), clock)
            while not stop.is_set():
                health.heartbeat("engine", HealthStatus.HEALTHY,
                                  {"state": "RUNNING"})
                beats.append(clock.current)
                time.sleep(0.01)
        finally:
            engine_session.close()

    thread = threading.Thread(target=_engine_loop, name="fake-engine")
    thread.start()
    time.sleep(0.05)
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert beats, "the fake engine must have written at least one heartbeat"

    client = _client(db_path, clock)
    live = client.get("/api/live")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"

    # Advance the injected clock past the configured 60s offline threshold.
    clock.current = NOW + timedelta(seconds=61)
    status = _get(client, f"/api/accounts/{scope.scope_id}/status")
    assert status["engine_state"] == "OFFLINE"
    assert status["heartbeat_age_seconds"] == 61
    assert status["last_heartbeat_at"] == "2026-10-08T14:30:00Z"
    assert status["fresh"] is False
    assert status["stale_reason"] == "engine_offline"
    assert status["entries_allowed"] is False
    assert status["entries_blocked_reasons"][0] == "engine_offline"

    # Last-known values are still returned, marked stale with a reason.
    kpis = _get(client, f"/api/accounts/{scope.scope_id}/kpis")
    assert kpis["cash"]["value"] == "9000.00"
    assert kpis["cash"]["fresh"] is False
    assert kpis["cash"]["stale_reason"] == "engine_offline"
    row = _get(client, f"/api/accounts/{scope.scope_id}/watchlist")["symbols"][0]
    assert row["confirmed_quantity"]["value"] == "10"
    assert row["confirmed_quantity"]["fresh"] is False
    assert row["confirmed_quantity"]["stale_reason"] == "engine_offline"
    assert row["price"]["fresh"] is False


# ------------------------------------------------------- (2) feed dead, engine alive


def test_feed_dead_while_engine_alive_is_degraded_and_blocks_entries(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops.sqlite3"
    clock = Clock()
    session = _session(db_path, clock)
    try:
        scope = AccountScope(AccountEnvironment.PAPER, "OFF2")
        writer = _seed_base(session, clock, scope)
        _add_bar(writer, scope.scope_id, "AAPL")
        # Feed heartbeat is 5 minutes old (threshold 90s); engine just beat.
        clock.current = NOW - timedelta(minutes=5)
        ServiceHealthRepository(writer, clock).heartbeat(
            "feed", HealthStatus.HEALTHY, {"reconnects": "1"}
        )
        clock.current = NOW

        client = _client(db_path, clock)
        status = _get(client, f"/api/accounts/{scope.scope_id}/status")
        assert status["engine_state"] == "DEGRADED"
        assert status["entries_allowed"] is False
        assert "feed_stale" in status["entries_blocked_reasons"]
        assert status["data"]["fresh"] is False
        assert status["data"]["stale_reason"] == "feed_stale"
        # Engine link itself is still fresh: only the feed is dead.
        assert status["fresh"] is True
        row = _get(client, f"/api/accounts/{scope.scope_id}/watchlist")["symbols"][0]
        assert row["price"]["fresh"] is False
        assert row["price"]["stale_reason"] == "feed_stale"
        assert row["data_stale"] is True
    finally:
        session.close()


# ------------------------------------------------------- (3) broker disconnected


def test_broker_disconnected_broker_truth_never_fresh(tmp_path: Path) -> None:
    db_path = tmp_path / "ops.sqlite3"
    clock = Clock()
    session = _session(db_path, clock)
    try:
        scope = AccountScope(AccountEnvironment.PAPER, "OFF3")
        _seed_base(session, clock, scope)
        writer = session.bind_scope(scope.scope_id)
        _add_bar(writer, scope.scope_id, "AAPL")
        _entry_fill(
            session, clock, scope.scope_id, cycle_id="c1", symbol="AAPL",
            order_qty=D("10"), fill_qty=D("10"), price=D("100.25"), tag="e1",
        )
        _protect(
            session, clock, scope.scope_id, protection_id="p1", cycle_id="c1",
            kind=ProtectionKind.STEPPED_TRAILING_STOP, covered=D("10"),
            stop=D("98.50"), stop_broker_id="stop-1",
        )
        _trail(
            session, clock, scope.scope_id, cycle_id="c1", symbol="AAPL",
            kind=ProtectionKind.STEPPED_TRAILING_STOP, covered=D("10"),
            stop=D("98.50"), hwm=D("101.00"), stop_broker_id="stop-1",
        )
        ServiceHealthRepository(writer, clock).heartbeat("broker", HealthStatus.OFFLINE)

        client = _client(db_path, clock)
        status = _get(client, f"/api/accounts/{scope.scope_id}/status")
        assert status["engine_state"] == "RUNNING"
        assert status["broker"]["connected"] is False
        assert status["broker"]["fresh"] is False
        assert status["broker"]["stale_reason"] == "broker_disconnected"

        kpis = _get(client, f"/api/accounts/{scope.scope_id}/kpis")
        for key in ("cash", "equity", "buying_power", "open_position_count",
                    "deployed_capital"):
            assert kpis[key]["fresh"] is False, key
            assert kpis[key]["stale_reason"] == "broker_disconnected", key

        row = _get(client, f"/api/accounts/{scope.scope_id}/watchlist")["symbols"][0]
        for key in ("confirmed_quantity", "protection_status", "protection_price",
                    "protection_quantity", "native_broker_hwm", "stepped_anchor",
                    "desired_stop", "confirmed_stop"):
            assert row[key]["fresh"] is False, key
            assert row[key]["stale_reason"] == "broker_disconnected", key

        detail = _get(client, f"/api/accounts/{scope.scope_id}/detail")
        assert detail["balances"]["fresh"] is False
        assert detail["balances"]["stale_reason"] == "broker_disconnected"
        assert detail["positions"], "expected the seeded managed position"
        for position in detail["positions"]:
            assert position["fresh"] is False
            assert position["stale_reason"] == "broker_disconnected"

        errors = _get(client, f"/api/accounts/{scope.scope_id}/health")["errors"]
        assert any(e["code"] == "BROKER_DISCONNECTED" for e in errors)
    finally:
        session.close()


# -------------------------------------------- (4) partial fill protection coverage


def test_partial_fill_full_protection_coverage(tmp_path: Path) -> None:
    """10-share entry, 6 filled, stop confirmed for 6: exact '6'/'6', full coverage."""
    db_path = tmp_path / "ops.sqlite3"
    clock = Clock()
    session = _session(db_path, clock)
    try:
        scope = AccountScope(AccountEnvironment.PAPER, "OFF4")
        writer = _seed_base(session, clock, scope)
        _add_bar(writer, scope.scope_id, "AAPL")
        _entry_fill(
            session, clock, scope.scope_id, cycle_id="c1", symbol="AAPL",
            order_qty=D("10"), fill_qty=D("6"), price=D("100.25"), tag="pf",
        )
        _protect(
            session, clock, scope.scope_id, protection_id="p1", cycle_id="c1",
            kind=ProtectionKind.STEPPED_TRAILING_STOP, covered=D("6"),
            stop=D("98.50"), stop_broker_id="stop-1",
        )

        client = _client(db_path, clock)
        row = _get(client, f"/api/accounts/{scope.scope_id}/watchlist")["symbols"][0]
        assert row["confirmed_quantity"]["value"] == "6"
        assert isinstance(row["confirmed_quantity"]["value"], str)
        assert row["protection_quantity"]["value"] == "6"
        assert isinstance(row["protection_quantity"]["value"], str)
        assert row["protection_status"]["value"] == "ACTIVE"

        detail = _get(client, f"/api/accounts/{scope.scope_id}/symbols/AAPL")
        entry_orders = [
            o for tree in detail["order_tree"] for o in tree["orders"]
            if o["purpose"] == "ENTRY"
        ]
        assert entry_orders and entry_orders[0]["quantity"] == "10"
        assert entry_orders[0]["filled_quantity"] == "6"

        codes = {e["code"] for e in
                 _get(client, f"/api/accounts/{scope.scope_id}/health")["errors"]}
        assert "PROTECTION_GAP" not in codes
    finally:
        session.close()


def test_partial_fill_under_covered_protection(tmp_path: Path) -> None:
    """Same 6-share fill with the stop covering only 4: under-coverage is reported."""
    db_path = tmp_path / "ops.sqlite3"
    clock = Clock()
    session = _session(db_path, clock)
    try:
        scope = AccountScope(AccountEnvironment.PAPER, "OFF4B")
        writer = _seed_base(session, clock, scope)
        _add_bar(writer, scope.scope_id, "AAPL")
        _entry_fill(
            session, clock, scope.scope_id, cycle_id="c1", symbol="AAPL",
            order_qty=D("10"), fill_qty=D("6"), price=D("100.25"), tag="pu",
        )
        _protect(
            session, clock, scope.scope_id, protection_id="p1", cycle_id="c1",
            kind=ProtectionKind.STEPPED_TRAILING_STOP, covered=D("4"),
            stop=D("98.50"), stop_broker_id="stop-1",
        )

        client = _client(db_path, clock)
        row = _get(client, f"/api/accounts/{scope.scope_id}/watchlist")["symbols"][0]
        assert row["confirmed_quantity"]["value"] == "6"
        assert row["protection_quantity"]["value"] == "4"
        errors = _get(client, f"/api/accounts/{scope.scope_id}/health")["errors"]
        gap = [e for e in errors if e["code"] == "PROTECTION_GAP"]
        assert gap, "under-covered stop must raise PROTECTION_GAP"
        assert "COVERAGE_GAP" in gap[0]["message"]
    finally:
        session.close()


# --------------------------------------- (5) native HWM vs stepped anchor trailing


def test_native_broker_trailing_vs_stepped_anchor(tmp_path: Path) -> None:
    """Native (broker HWM) and stepped (local anchor) rows differ; pending update
    shows distinct desired vs confirmed stop values."""
    db_path = tmp_path / "ops.sqlite3"
    clock = Clock()
    session = _session(db_path, clock)
    try:
        scope = AccountScope(AccountEnvironment.PAPER, "OFF5")
        _seed_base(
            session, clock, scope,
            symbols={
                "AAPL": {"budget": "3000.00", "planned_loss_cap": "30.00"},
                "NVDA": {"budget": "3000.00", "planned_loss_cap": "30.00"},
            },
        )
        writer = session.bind_scope(scope.scope_id)
        _add_bar(writer, scope.scope_id, "AAPL")
        _add_bar(writer, scope.scope_id, "NVDA", price="150.10")
        # Stepped (local anchor) position.
        _entry_fill(
            session, clock, scope.scope_id, cycle_id="c-step", symbol="AAPL",
            order_qty=D("10"), fill_qty=D("10"), price=D("100.25"), tag="st",
        )
        _protect(
            session, clock, scope.scope_id, protection_id="p-step", cycle_id="c-step",
            kind=ProtectionKind.STEPPED_TRAILING_STOP, covered=D("10"),
            stop=D("98.50"), stop_broker_id="stop-step", symbol="AAPL",
        )
        _trail(
            session, clock, scope.scope_id, cycle_id="c-step", symbol="AAPL",
            kind=ProtectionKind.STEPPED_TRAILING_STOP, covered=D("10"),
            stop=D("98.50"), hwm=D("101.00"), stop_broker_id="stop-step",
        )
        # Native broker trailing position with a pending update: the desired stop
        # (140.00) differs from the last confirmed broker stop (139.50).
        _entry_fill(
            session, clock, scope.scope_id, cycle_id="c-nat", symbol="NVDA",
            order_qty=D("5"), fill_qty=D("5"), price=D("150.00"), tag="nt",
        )
        _protect(
            session, clock, scope.scope_id, protection_id="p-nat", cycle_id="c-nat",
            kind=ProtectionKind.BROKER_TRAILING_STOP, covered=D("5"),
            stop=D("140.00"), stop_broker_id="stop-nat",
            stop_confirmed=D("139.50"), symbol="NVDA",
        )
        _trail(
            session, clock, scope.scope_id, cycle_id="c-nat", symbol="NVDA",
            kind=ProtectionKind.BROKER_TRAILING_STOP, covered=D("5"),
            stop=D("140.00"), hwm=D("150.00"), stop_broker_id="stop-nat",
        )

        client = _client(db_path, clock)
        rows = {r["symbol"]: r for r in
                _get(client, f"/api/accounts/{scope.scope_id}/watchlist")["symbols"]}
        assert {"AAPL", "NVDA"} <= set(rows)
        stepped, native = rows["AAPL"], rows["NVDA"]
        assert stepped["protection_mode"]["value"] == "STEPPED_TRAILING_STOP"
        assert native["protection_mode"]["value"] == "BROKER_TRAILING_STOP"
        # Stepped rows carry the local anchor and no broker HWM; native is reversed.
        assert stepped["stepped_anchor"]["value"] == "101.00"
        assert stepped["native_broker_hwm"]["value"] is None
        assert native["native_broker_hwm"]["value"] == "150.00"
        assert native["stepped_anchor"]["value"] is None
        # Pending native update: desired and confirmed stops are distinct.
        assert native["desired_stop"]["value"] == "140.00"
        assert native["confirmed_stop"]["value"] == "139.50"
    finally:
        session.close()


# --------------------------- (6) budget / cutoff / lockout / cooldown reasons


def test_watchlist_budget_cutoff_lockout_cooldown_reasons(tmp_path: Path) -> None:
    db_path = tmp_path / "ops.sqlite3"
    clock = Clock()
    session = _session(db_path, clock)
    try:
        scope = AccountScope(AccountEnvironment.PAPER, "OFF6")
        writer = _seed_base(session, clock, scope)
        _add_bar(writer, scope.scope_id, "AAPL")
        controls = EntryControlRepository(writer, clock)
        controls.increment_filled_entry_cycles("AAPL", date(2026, 10, 8))
        controls.record_trade_result(
            "AAPL",
            date(2026, 10, 8),
            D("-5.00"),
            EntryEligibilityPolicy(
                max_filled_entry_cycles_per_symbol_per_session=1,
                consecutive_loss_lockout_threshold=1,
            ),
        )
        controls.set_cooldown("AAPL", NOW + timedelta(hours=1), reason="manual_review")

        client = _client(db_path, clock)
        row = _get(client, f"/api/accounts/{scope.scope_id}/watchlist")["symbols"][0]
        # Symbol budget from config.
        assert row["symbol_budget"]["value"] == "3000.00"
        assert row["planned_loss"]["value"] == "30.00"
        assert row["daily_entry_cap"]["value"] == 1
        # Entry cutoff for today's session.
        assert row["entry_cutoff"]["value"] == "2026-10-08T19:30:00Z"
        # Loss-streak lockout state.
        assert row["daily_entry_count"]["value"] == 1
        assert row["loss_streak"]["value"] == 1
        assert row["lockout_active"]["value"] is True
        # Cooldown state with a concrete resume time.
        assert row["cooldown_until"]["value"] == "2026-10-08T15:30:00Z"
        assert row["next_eligible_at"]["value"] == "2026-10-08T15:30:00Z"
    finally:
        session.close()
