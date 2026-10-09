"""Phase R: dashboard read-only and isolation acceptance tests.

Proves the dashboard exposes no write surface, never mutates the database,
imports no broker/market-data adapters, isolates PAPER vs SHADOW scenarios,
serves the compiled frontend bundle, and binds to loopback by default.

Method: temporary migrated SQLite DBs seeded through the persistence layer
(repositories + ``WriterSession``; never through the dashboard) and an injected
clock. The persistence layer enforces one environment per database (a
``account_scopes_environment_guard`` trigger plus ``WriterSession.bind_scope``),
so PAPER and SHADOW scenarios live in separate databases served by separate app
instances -- which is itself the isolation boundary under test. No network, no
broker credentials, no running engine.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mike_trade.dashboard.api import DashboardSettings
from mike_trade.dashboard.server import create_app, is_loopback_host, validate_bind
from mike_trade.domain.accounts import (
    AccountEnvironment,
    AccountScope,
    AccountSnapshot,
    HealthStatus,
)
from mike_trade.domain.errors import ConfigurationError
from mike_trade.persistence.repositories import (
    AccountScopeRepository,
    CashEquityRepository,
    RunConfigRepository,
    ServiceHealthRepository,
)
from mike_trade.persistence.schema import initialize_database, to_db_timestamp
from mike_trade.persistence.transaction import WriterSession

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)  # Thursday 10:30 ET, regular session.
D = Decimal
ROOT = Path(__file__).resolve().parents[2]
REAL_DIST = ROOT / "dashboard" / "frontend" / "dist"
WRITE_METHODS = ("post", "put", "patch", "delete")
FORBIDDEN_IMPORTS = (
    "alpaca",
    "mike_trade.brokers",
    "mike_trade.market_data",
    "mike_trade.execution",
    "mike_trade.research",
)


class Clock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current


def _seed_paper(db_path: Path, clock: Clock) -> tuple[WriterSession, AccountScope]:
    """PAPER database with one scope, cash 9000.00 and an AAPL managed position."""
    initialize_database(db_path, AccountEnvironment.PAPER, now=NOW).close()
    session = WriterSession(db_path)
    scope = AccountScope(AccountEnvironment.PAPER, "P1")
    writer = session.bind_scope(scope.scope_id)
    AccountScopeRepository(writer, clock).register_scope(scope, broker="alpaca")
    RunConfigRepository(writer, clock).record_run_config(
        {"risk": {"total_application_budget": "10000.00"},
         "symbols": {"AAPL": {"budget": "3000.00"}}}
    )
    CashEquityRepository(writer, clock).append_equity_snapshot(
        AccountSnapshot(
            scope=scope, as_of=NOW, cash=D("9000.00"), equity=D("10005.50"),
            buying_power=D("18000.00"), portfolio_value=D("10005.50"),
            source="alpaca_paper",
        )
    )
    health = ServiceHealthRepository(writer, clock)
    health.heartbeat("engine", HealthStatus.HEALTHY, {"state": "RUNNING"})
    health.heartbeat("broker", HealthStatus.HEALTHY, {})
    health.heartbeat("feed", HealthStatus.HEALTHY, {})
    with writer.unit_of_work() as conn:
        conn.execute(
            "INSERT INTO instruments(account_scope_id, symbol, observed_at) "
            "VALUES (?, 'AAPL', ?)",
            (scope.scope_id, to_db_timestamp(NOW)),
        )
        conn.execute(
            "INSERT INTO positions(account_scope_id, symbol, cycle_id, management, "
            "direction, quantity, average_entry_price, as_of) VALUES (?, 'AAPL', NULL, "
            "'MANAGED', 'LONG', '10', '100.25', ?)",
            (scope.scope_id, to_db_timestamp(NOW)),
        )
    return session, scope


def _seed_shadow(db_path: Path, clock: Clock) -> tuple[WriterSession, list[AccountScope]]:
    """SHADOW database with two scenarios holding distinct cash and positions."""
    initialize_database(db_path, AccountEnvironment.SHADOW, now=NOW).close()
    session = WriterSession(db_path)
    scopes = [
        AccountScope(AccountEnvironment.SHADOW, "S1", scenario="alpha"),
        AccountScope(AccountEnvironment.SHADOW, "S1", scenario="beta"),
    ]
    specs = [("111.11", "TSLA", "3"), ("222.22", "NVDA", "7")]
    for scope, (cash, symbol, qty) in zip(scopes, specs):
        writer = session.bind_scope(scope.scope_id)
        AccountScopeRepository(writer, clock).register_scope(scope)
        CashEquityRepository(writer, clock).append_equity_snapshot(
            AccountSnapshot(
                scope=scope, as_of=NOW, cash=D(cash), equity=D(cash),
                buying_power=D(cash), source="shadow_ledger",
            )
        )
        health = ServiceHealthRepository(writer, clock)
        health.heartbeat("engine", HealthStatus.HEALTHY, {"state": "RUNNING"})
        with writer.unit_of_work() as conn:
            conn.execute(
                "INSERT INTO positions(account_scope_id, symbol, cycle_id, management, "
                "direction, quantity, average_entry_price, as_of) VALUES (?, ?, NULL, "
                "'MANAGED', 'LONG', ?, '50.00', ?)",
                (scope.scope_id, symbol, qty, to_db_timestamp(NOW)),
            )
    return session, scopes


@pytest.fixture
def paper(tmp_path: Path) -> Any:
    clock = Clock()
    session, scope = _seed_paper(tmp_path / "paper.sqlite3", clock)
    client = TestClient(create_app(DashboardSettings(), clock, tmp_path / "paper.sqlite3", None))
    yield types(client, session, clock, scope, tmp_path / "paper.sqlite3")
    session.close()


@pytest.fixture
def shadow(tmp_path: Path) -> Any:
    clock = Clock()
    session, scopes = _seed_shadow(tmp_path / "shadow.sqlite3", clock)
    client = TestClient(
        create_app(DashboardSettings(), clock, tmp_path / "shadow.sqlite3", None)
    )
    yield types(client, session, clock, scopes, tmp_path / "shadow.sqlite3")
    session.close()


class types:  # noqa: N801 - tiny fixture bundle.
    def __init__(self, client: TestClient, session: WriterSession, clock: Clock,
                 scope: Any, db_path: Path) -> None:
        self.client = client
        self.session = session
        self.clock = clock
        self.scope = scope
        self.db_path = db_path


def _get(client: TestClient, path: str) -> Any:
    response = client.get(path)
    assert response.status_code == 200, f"{path}: {response.text}"
    return response.json()


def _api_paths(account_id: str) -> list[str]:
    return [
        "/api/live",
        "/api/accounts",
        f"/api/accounts/{account_id}/status",
        f"/api/accounts/{account_id}/kpis",
        f"/api/accounts/{account_id}/watchlist",
        f"/api/accounts/{account_id}/symbols/AAPL",
        f"/api/accounts/{account_id}/detail",
        f"/api/accounts/{account_id}/health",
        "/api/no-such-route",
    ]


# ------------------------------------------------- (1) GET/HEAD-only route table


def _nested(routes: Any) -> Any:
    """Yield routes, descending into Starlette included-router wrappers."""
    for route in routes:
        yield route
        yield from _nested(getattr(route, "routes", None) or [])
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _nested(getattr(original, "routes", None) or [])


def _all_methods(app: Any) -> set[str]:
    """Collect HTTP methods from routes, descending into included routers."""
    return {
        method for route in _nested(app.routes)
        for method in getattr(route, "methods", None) or ()
    }


def _all_paths(app: Any) -> list[str]:
    return [
        path for route in _nested(app.routes)
        if (path := getattr(route, "path", None))
    ]


def test_only_get_and_head_routes_are_registered(paper: types) -> None:
    methods = _all_methods(paper.client.app)
    assert methods, "expected at least one registered route"
    assert methods <= {"GET", "HEAD"}, f"write methods registered: {methods}"
    api_paths = sorted(p for p in _all_paths(paper.client.app) if p.startswith("/api/"))
    assert "/api/accounts" in api_paths
    assert any(p.endswith("/status") for p in api_paths)


# ------------------------------------------------- (2) write methods rejected


def test_write_methods_return_405_on_every_api_route(paper: types) -> None:
    account_id: str = paper.scope.scope_id
    for path in _api_paths(account_id):
        for method in WRITE_METHODS:
            response = getattr(paper.client, method)(path)
            assert response.status_code == 405, f"{method.upper()} {path}"
            assert response.headers["allow"] == "GET, HEAD", f"{method.upper()} {path}"


# ------------------------------------------------- (3) database untouched


def _table_counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        return {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables
        }


def _file_hashes(db_path: Path) -> dict[str, str]:
    hashes = {}
    for suffix in ("", "-wal", "-shm", ".writer.lock"):
        candidate = Path(str(db_path) + suffix)
        if candidate.is_file():
            hashes[suffix] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    return hashes


def test_exercising_every_endpoint_leaves_the_database_unchanged(paper: types) -> None:
    before_counts = _table_counts(paper.db_path)
    assert before_counts, "expected a migrated database with tables"
    before_hashes = _file_hashes(paper.db_path)
    for path in _api_paths(paper.scope.scope_id):
        response = paper.client.get(path)
        assert response.status_code in (200, 404), f"{path}: {response.status_code}"
    assert _table_counts(paper.db_path) == before_counts
    assert _file_hashes(paper.db_path) == before_hashes


# ------------------------------------------------- (4) no broker/market-data imports


def _dashboard_sources() -> list[Path]:
    package = ROOT / "mike_trade" / "dashboard"
    return [package / "api.py", package / "server.py"]


def test_dashboard_sources_import_no_broker_or_market_data_modules() -> None:
    for source in _dashboard_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        bad = [name for name in imported
               if name.split(".")[0] in {"alpaca"} or name in FORBIDDEN_IMPORTS
               or any(name.startswith(prefix + ".") for prefix in FORBIDDEN_IMPORTS[1:])]
        assert not bad, f"{source.name} imports adapter modules: {bad}"


def test_dashboard_import_pulls_in_no_adapter_modules() -> None:
    code = (
        "import sys, mike_trade.dashboard.server, mike_trade.dashboard.api; "
        "bad = [m for m in sys.modules if m == 'alpaca' or m.startswith("
        "('alpaca.', 'mike_trade.brokers', 'mike_trade.market_data', "
        "'mike_trade.execution', 'mike_trade.research'))]; "
        "print('\\n'.join(sorted(bad))); sys.exit(1 if bad else 0)"
    )
    completed = subprocess.run([sys.executable, "-c", code], check=False,
                               capture_output=True, text=True)
    assert completed.returncode == 0, f"adapter modules imported: {completed.stdout}"


# ------------------------------------------------- (5) PAPER vs SHADOW isolation


def test_paper_and_shadow_scenarios_are_isolated(paper: types, shadow: types) -> None:
    paper_id: str = paper.scope.scope_id
    alpha, beta = (s.scope_id for s in shadow.scope)
    assert len({paper_id, alpha, beta}) == 3

    paper_body = json.dumps(_get(paper.client, f"/api/accounts/{paper_id}/detail"))
    assert "9000.00" in paper_body and "AAPL" in paper_body
    for foreign in (alpha, beta, "111.11", "222.22", "TSLA", "NVDA", "shadow_ledger"):
        assert foreign not in paper_body, f"paper response leaks {foreign}"

    alpha_body = json.dumps(_get(shadow.client, f"/api/accounts/{alpha}/detail"))
    assert "111.11" in alpha_body and "TSLA" in alpha_body
    for foreign in (beta, paper_id, "222.22", "9000.00", "NVDA", "AAPL", "alpaca_paper"):
        assert foreign not in alpha_body, f"alpha response leaks {foreign}"

    beta_body = json.dumps(_get(shadow.client, f"/api/accounts/{beta}/detail"))
    assert "222.22" in beta_body and "NVDA" in beta_body
    for foreign in (alpha, paper_id, "111.11", "9000.00", "TSLA", "AAPL"):
        assert foreign not in beta_body, f"beta response leaks {foreign}"

    # Account listings stay within their own database boundary.
    paper_ids = {a["account_id"] for a in _get(paper.client, "/api/accounts")["accounts"]}
    assert paper_ids == {paper_id}
    shadow_ids = {a["account_id"] for a in _get(shadow.client, "/api/accounts")["accounts"]}
    assert shadow_ids == {alpha, beta}
    modes = {a["account_id"]: (a["mode"], a["scenario"])
             for a in _get(shadow.client, "/api/accounts")["accounts"]}
    assert modes[alpha] == ("SHADOW", "alpha")
    assert modes[beta] == ("SHADOW", "beta")

    # Cross-check balances/kpis endpoints too, not just detail.
    assert "111.11" not in json.dumps(_get(shadow.client, f"/api/accounts/{beta}/kpis"))
    assert "222.22" not in json.dumps(_get(shadow.client, f"/api/accounts/{alpha}/kpis"))


@pytest.mark.parametrize("suffix", ["status", "kpis", "watchlist", "symbols/AAPL",
                                    "detail", "health"])
def test_unknown_account_returns_404(paper: types, shadow: types, suffix: str) -> None:
    for client in (paper.client, shadow.client):
        response = client.get(f"/api/accounts/paper:NOPE/{suffix}")
        assert response.status_code == 404, suffix
        assert "unknown account_id" in response.json()["detail"], suffix


# ------------------------------------------------- (6) static bundle serving


@pytest.fixture
def tmp_dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<html><body>dashboard shell</body></html>")
    (root / "assets" / "app.abc123def.js").write_text("console.log('bundle')")
    return root


def test_static_index_hashed_assets_and_spa_fallback(tmp_dist: Path) -> None:
    app = create_app(DashboardSettings(), Clock(), tmp_dist / "missing.sqlite3", tmp_dist)
    client = TestClient(app)
    index = client.get("/")
    assert index.status_code == 200 and "dashboard shell" in index.text
    asset = client.get("/assets/app.abc123def.js")
    assert asset.status_code == 200 and "bundle" in asset.text
    assert "immutable" in asset.headers["cache-control"]
    fallback = client.get("/accounts/paper:P1/watchlist")
    assert fallback.status_code == 200 and "dashboard shell" in fallback.text
    assert client.get("/assets/missing.js").status_code == 404
    # API 404s are JSON, never the SPA shell.
    missing_api = client.get("/api/does-not-exist")
    assert missing_api.status_code == 404
    assert "dashboard shell" not in missing_api.text


def test_production_build_uses_hashed_assets_without_vite_dev_client() -> None:
    index = REAL_DIST / "index.html"
    if not index.is_file():
        pytest.skip("dashboard/frontend/dist is not built")
    html = index.read_text(encoding="utf-8")
    assert "/@vite/client" not in html
    assert "@vite/client" not in html
    assert re.search(r"/assets/[^\"']+-[A-Za-z0-9]{6,}\.js", html), \
        "production index.html must reference a hashed /assets/ bundle"
    assets = sorted((REAL_DIST / "assets").iterdir()) if (REAL_DIST / "assets").is_dir() else []
    assert assets, "expected compiled assets in dashboard/frontend/dist/assets"


# ------------------------------------------------- (7) loopback default bind


def test_default_bind_host_is_loopback() -> None:
    assert DashboardSettings().bind_host == "127.0.0.1"
    assert DashboardSettings.from_env({}).bind_host == "127.0.0.1"
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert is_loopback_host("::1")
    assert not is_loopback_host("0.0.0.0")
    validate_bind(DashboardSettings())
    with pytest.raises(ConfigurationError):
        validate_bind(DashboardSettings(bind_host="0.0.0.0"))
    validate_bind(DashboardSettings(bind_host="0.0.0.0", allow_non_loopback=True))
