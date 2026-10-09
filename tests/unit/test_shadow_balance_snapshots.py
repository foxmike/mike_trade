"""SHADOW balances are isolated simulated values persisted for the dashboard."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from mike_trade.app import ShadowBrokerGroup, ShadowScenarioSettings, SQLitePersistence
from mike_trade.domain.accounts import AccountEnvironment, AccountScope


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 9, 20, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


def test_shadow_balance_snapshots_use_simulated_scope_and_refresh(tmp_path) -> None:
    clock = Clock()
    scope = AccountScope(AccountEnvironment.SHADOW, "default", "r730_shadow")
    other = AccountScope(AccountEnvironment.SHADOW, "default", "other")
    store = SQLitePersistence(tmp_path / "shadow.sqlite3", AccountEnvironment.SHADOW,
                              (scope, other), clock)
    store.open_migrate_integrity()
    try:
        group = ShadowBrokerGroup(store, clock, "default", (
            ShadowScenarioSettings(name="r730_shadow", starting_cash=Decimal("25000")),
            ShadowScenarioSettings(name="other", starting_cash=Decimal("5000")),
        ))
        group.validate_startup()
        asyncio.run(group.refresh_balance_snapshots())
        rows = store.session.connection.execute(
            "SELECT account_scope_id, cash, equity, buying_power, portfolio_value, source "
            "FROM equity_snapshots ORDER BY account_scope_id"
        ).fetchall()
        assert rows == [
            (other.scope_id, "5000", "5000", "5000", "5000",
             "shadow:simulated;estimated=True;broker_confirmed=False"),
            (scope.scope_id, "25000", "25000", "25000", "25000",
             "shadow:simulated;estimated=True;broker_confirmed=False"),
        ]
        asyncio.run(group.refresh_balance_snapshots())
        assert store.session.connection.execute(
            "SELECT count(*) FROM equity_snapshots"
        ).fetchone()[0] == 2
        clock.value += timedelta(minutes=1)
        asyncio.run(group.refresh_balance_snapshots())
        assert store.session.connection.execute(
            "SELECT count(*) FROM equity_snapshots"
        ).fetchone()[0] == 4
    finally:
        store.close()
