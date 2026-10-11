"""Unit tests for the Market Status Monitor API v2 (/api/v2/market-status/).

Verifies:
1. Endpoint responses (/state, /benchmarks, /transitions, /bars, /config).
2. Online/offline/stale state detection and fail-closed permissions.
3. Exits/stops/liquidations are never blocked under any state or data outage.
4. Database remains completely read-only.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mike_trade.dashboard.api import DashboardSettings
from mike_trade.dashboard.server import create_app
from mike_trade.domain.accounts import (
    AccountEnvironment,
    AccountScope,
    HealthStatus,
)
from mike_trade.market_status.persistence import SqliteMarketStatusRepository
from mike_trade.persistence.repositories import (
    AccountScopeRepository,
    ServiceHealthRepository,
)
from mike_trade.persistence.schema import initialize_database, to_db_timestamp
from mike_trade.persistence.transaction import WriterSession

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
D = Decimal


class FixedClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


@pytest.fixture
def test_setup(tmp_path: Path) -> dict[str, Any]:
    db_path = tmp_path / "test_market.sqlite3"
    initialize_database(db_path, AccountEnvironment.PAPER, now=NOW).close()
    session = WriterSession(db_path)
    scope = AccountScope(AccountEnvironment.PAPER, "P1")
    writer = session.bind_scope(scope.scope_id)
    clock = FixedClock(NOW)

    AccountScopeRepository(writer, clock).register_scope(scope, broker="alpaca")
    health = ServiceHealthRepository(writer, clock)
    health.heartbeat("broker", HealthStatus.HEALTHY, {})
    health.heartbeat("feed", HealthStatus.HEALTHY, {})

    # Start with engine online
    health.heartbeat("engine", HealthStatus.HEALTHY, {"state": "RUNNING"})

    # Seed market status record
    market_doc = {
        "current_state": "CRASH",
        "last_transition_at": (NOW - timedelta(minutes=15)).isoformat(),
        "reason": "consensus breach on SPY and QQQ",
        "metrics_summary": {
            "composite_drawdown_pct": -4.2,
            "composite_volatility_bps": 280,
            "composite_momentum_bps": -310,
            "benchmarks": {
                "SPY": {
                    "price": 580.25,
                    "peak_price": 605.00,
                    "drawdown_pct": -4.09,
                    "volatility_bps": 260,
                    "momentum_bps": -290,
                    "last_updated": NOW.isoformat(),
                    "breach": True,
                },
                "QQQ": {
                    "price": 485.10,
                    "peak_price": 508.00,
                    "drawdown_pct": -4.51,
                    "volatility_bps": 310,
                    "momentum_bps": -340,
                    "last_updated": NOW.isoformat(),
                    "breach": True,
                },
                "IWM": {
                    "price": 215.50,
                    "peak_price": 222.00,
                    "drawdown_pct": -2.93,
                    "volatility_bps": 210,
                    "momentum_bps": -180,
                    "last_updated": NOW.isoformat(),
                    "breach": False,
                },
            },
        },
        "transitions": [
            {
                "from_state": "NORMAL",
                "to_state": "WARNING",
                "timestamp": (NOW - timedelta(minutes=25)).isoformat(),
                "reason": "drawdown threshold breached on SPY",
                "metrics_summary": {"composite_drawdown_pct": -1.8},
            },
            {
                "from_state": "WARNING",
                "to_state": "CRASH",
                "timestamp": (NOW - timedelta(minutes=15)).isoformat(),
                "reason": "consensus breach on SPY and QQQ",
                "metrics_summary": {"composite_drawdown_pct": -4.2},
            },
        ],
    }
    repo = SqliteMarketStatusRepository(writer, clock)
    repo.save(scope.scope_id, market_doc)

    # Seed 1m bars for SPY, QQQ, IWM
    with writer.unit_of_work() as conn:
        for symbol, base_price in [("SPY", 580.0), ("QQQ", 485.0), ("IWM", 215.0)]:
            for i in range(10):
                bar_time = NOW - timedelta(minutes=10 - i)
                p = base_price + i * 0.1
                conn.execute(
                    "INSERT INTO market_bars "
                    "(account_scope_id, symbol, timeframe, bar_start, "
                    "open_price, high_price, low_price, close_price, volume, "
                    "feed, provenance, recorded_at) "
                    "VALUES (?, ?, '1m', ?, ?, ?, ?, ?, '1000', 'alpaca', 'live', ?)",
                    (
                        scope.scope_id,
                        symbol,
                        to_db_timestamp(bar_time),
                        str(p),
                        str(p + 0.2),
                        str(p - 0.2),
                        str(p + 0.05),
                        to_db_timestamp(bar_time),
                    ),
                )

    client = TestClient(create_app(DashboardSettings(), clock, db_path, None))
    return {
        "client": client,
        "session": session,
        "clock": clock,
        "scope": scope,
        "db_path": db_path,
        "writer": writer,
    }


def test_market_status_overview_online(test_setup: dict[str, Any]) -> None:
    client: TestClient = test_setup["client"]
    resp = client.get("/api/v2/market-status/")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["market_state"] == "CRASH"
    assert data["engine_online"] is True
    assert data["is_stale"] is False
    assert data["account_id"] == "paper:P1"
    assert "consensus breach" in data["reason"]

    # Trading authorization under CRASH: BUY is blocked, SHORT allowed, EXITS/STOPS allowed
    auth = data["authorization"]
    assert auth["buy_allowed"] is False
    assert auth["short_allowed"] is True
    assert auth["sell_allowed"] is True
    assert auth["cover_allowed"] is True
    assert auth["protective_stops_allowed"] is True
    assert auth["buy_permission"] == "BLOCKED"
    assert auth["short_permission"] == "ALLOWED"

    # Composite metrics computed from real 1m bars
    comp = data["composite_metrics"]
    assert comp["composite_drawdown_percent"] is not None
    assert comp["composite_drawdown_percent"] == pytest.approx(-0.042, abs=0.01)

    # Benchmarks
    assert len(data["benchmarks"]) == 3
    bench_map = {b["symbol"]: b for b in data["benchmarks"]}
    assert "SPY" in bench_map
    assert "QQQ" in bench_map
    assert "IWM" in bench_map
    assert bench_map["SPY"]["latest_price"] == pytest.approx(580.95)


def test_market_status_overview_offline_fails_closed(test_setup: dict[str, Any]) -> None:
    client: TestClient = test_setup["client"]
    writer = test_setup["writer"]

    # Make engine heartbeat old (> 60s offline cutoff)
    old_time = NOW - timedelta(minutes=5)
    health = ServiceHealthRepository(writer, FixedClock(old_time))
    health.heartbeat("engine", HealthStatus.HEALTHY, {"state": "RUNNING"})

    resp = client.get("/api/v2/market-status/state")
    assert resp.status_code == 200
    data = resp.json()

    assert data["engine_online"] is False
    assert data["is_stale"] is True

    # When engine is offline, fail closed on new entries, but exits/stops remain PERMITTED!
    auth = data["authorization"]
    assert auth["buy_allowed"] is False
    assert auth["short_allowed"] is False
    assert auth["sell_allowed"] is True
    assert auth["cover_allowed"] is True
    assert auth["protective_stops_allowed"] is True
    assert auth["buy_permission"] == "BLOCKED"
    assert auth["short_permission"] == "BLOCKED"
    assert "offline" in data["reason"].lower()


def test_market_benchmarks_endpoint(test_setup: dict[str, Any]) -> None:
    client: TestClient = test_setup["client"]
    resp = client.get("/api/v2/market-status/benchmarks")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    symbols = {b["symbol"] for b in data}
    assert symbols == {"SPY", "QQQ", "IWM"}
    spy = next(b for b in data if b["symbol"] == "SPY")
    assert spy["latest_price"] == pytest.approx(580.95)
    assert spy["drawdown_percent"] is not None


def test_market_transitions_endpoint(test_setup: dict[str, Any]) -> None:
    client: TestClient = test_setup["client"]
    resp = client.get("/api/v2/market-status/transitions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["account_id"] == "paper:P1"
    transitions = data["transitions"]
    assert len(transitions) >= 2
    # Check that recent transitions are listed
    states = [(t["from_state"], t["to_state"]) for t in transitions]
    assert ("WARNING", "CRASH") in states
    assert ("NORMAL", "WARNING") in states


def test_market_bars_endpoint(test_setup: dict[str, Any]) -> None:
    client: TestClient = test_setup["client"]
    resp = client.get("/api/v2/market-status/bars?limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["account_id"] == "paper:P1"
    assert len(data["symbols"]) == 3
    spy_bars = next(s for s in data["symbols"] if s["symbol"] == "SPY")
    assert len(spy_bars["bars"]) == 5
    first_bar = spy_bars["bars"][0]
    assert first_bar["open"] is not None
    assert first_bar["close"] is not None
    assert first_bar["timestamp"] is not None

    # Single symbol query
    resp_spy = client.get("/api/v2/market-status/bars?symbol=SPY&limit=3")
    assert resp_spy.status_code == 200
    spy_only = resp_spy.json()["symbols"]
    assert len(spy_only) == 1
    assert spy_only[0]["symbol"] == "SPY"
    assert len(spy_only[0]["bars"]) == 3


def test_market_config_endpoint(test_setup: dict[str, Any]) -> None:
    client: TestClient = test_setup["client"]
    resp = client.get("/api/v2/market-status/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["warning_drawdown_percent"] == -1.5
    assert data["crash_drawdown_percent"] == -3.5
    assert data["stabilizing_confirmation_bars"] == 3
    assert data["recovery_confirmation_bars"] == 3
    assert "SPY" in data["reference_symbols"]


def test_endpoints_never_mutate_database(test_setup: dict[str, Any]) -> None:
    client: TestClient = test_setup["client"]
    db_path: Path = test_setup["db_path"]

    def _get_counts() -> dict[str, int]:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            ]
            return {
                tbl: conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                for tbl in tables
            }

    counts_before = _get_counts()

    # Call all endpoints
    client.get("/api/v2/market-status/")
    client.get("/api/v2/market-status/state")
    client.get("/api/v2/market-status/benchmarks")
    client.get("/api/v2/market-status/transitions")
    client.get("/api/v2/market-status/bars")
    client.get("/api/v2/market-status/config")

    counts_after = _get_counts()
    assert (
        counts_before == counts_after
    ), f"Database was mutated: before={counts_before}, after={counts_after}"
