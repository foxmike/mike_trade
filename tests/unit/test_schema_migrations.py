from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from mike_trade.persistence.schema import (
    EXPECTED_TABLES,
    DatabaseIdentityMismatchError,
    MigrationChecksumError,
    SchemaTooNewError,
    apply_migrations,
    current_schema_version,
    decimal_from_db,
    decimal_to_db,
    discover_migrations,
    from_db_timestamp,
    initialize_database,
    open_connection,
    to_db_timestamp,
)

NOW = datetime(2026, 10, 8, 14, 30, 1, 123456, tzinfo=UTC)
STAMP = "2026-10-08T14:30:01.123456Z"


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = initialize_database(tmp_path / "schema.db", "SHADOW", now=NOW)
    yield conn
    conn.close()


def _scope(conn: sqlite3.Connection, scope_id: str) -> None:
    conn.execute(
        "INSERT INTO account_scopes("
        "account_scope_id, environment, broker, scenario_name, created_at"
        ") VALUES (?, 'SHADOW', 'simulated', ?, ?)",
        (scope_id, scope_id, STAMP),
    )


def _broker_order(conn: sqlite3.Connection, scope_id: str, order_id: str) -> None:
    conn.execute(
        "INSERT INTO broker_orders("
        "account_scope_id, broker_order_id, client_order_id, symbol, side, order_type, "
        "status, quantity, filled_quantity, submitted_at, updated_at"
        ") VALUES (?, ?, ?, 'AAPL', 'BUY', 'MARKET', 'FILLED', '1', '1', ?, ?)",
        (scope_id, order_id, f"client-{order_id}", STAMP, STAMP),
    )


def _fill(
    conn: sqlite3.Connection,
    scope_id: str,
    fill_id: str,
    activity_id: str,
    order_id: str,
    *,
    price: object = "185.25",
) -> None:
    conn.execute(
        "INSERT INTO fills("
        "fill_id, account_scope_id, external_activity_id, broker_order_id, symbol, side, "
        "quantity, price, fee, filled_at, recorded_at"
        ") VALUES (?, ?, ?, ?, 'AAPL', 'BUY', '1', ?, '0', ?, ?)",
        (fill_id, scope_id, activity_id, order_id, price, STAMP, STAMP),
    )


def _event(conn: sqlite3.Connection, scope_id: str, event_id: str = "event-1") -> None:
    conn.execute(
        "INSERT INTO domain_events("
        "event_id, account_scope_id, stream_type, stream_id, stream_version, event_type, "
        "payload_json, payload_sha256, occurred_at, recorded_at"
        ") VALUES (?, ?, 'cycle', 'cycle-1', 1, 'CycleOpened', '{}', ?, ?, ?)",
        (event_id, scope_id, "0" * 64, STAMP, STAMP),
    )


def test_fresh_apply_configures_connection_and_creates_expected_tables(tmp_path: Path) -> None:
    conn = initialize_database(tmp_path / "fresh.db", "PAPER", now=NOW)
    try:
        assert current_schema_version(conn) == discover_migrations()[-1].version == 3
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert EXPECTED_TABLES <= tables
    finally:
        conn.close()


def test_read_only_connection_rejects_writes(tmp_path: Path) -> None:
    path = tmp_path / "readonly.db"
    initialize_database(path, "PAPER", now=NOW).close()
    conn = open_connection(path, read_only=True)
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO account_scopes("
                "account_scope_id, environment, broker_account_ref, created_at"
                ") VALUES ('paper:x', 'PAPER', 'x', ?)",
                (STAMP,),
            )
    finally:
        conn.close()


def test_reapplying_migrations_is_a_no_op(connection: sqlite3.Connection) -> None:
    before = connection.execute("SELECT * FROM schema_migrations").fetchall()
    apply_migrations(connection, now=NOW + timedelta(days=1))
    assert connection.execute("SELECT * FROM schema_migrations").fetchall() == before


def test_changed_applied_migration_checksum_raises(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    source = discover_migrations()[0].path
    target = migrations_dir / source.name
    shutil.copyfile(source, target)
    conn = open_connection(tmp_path / "checksum.db")
    try:
        apply_migrations(conn, migrations_dir, now=NOW)
        target.write_text(target.read_text(encoding="utf-8") + "\n-- tampered\n", encoding="utf-8")
        with pytest.raises(MigrationChecksumError):
            apply_migrations(conn, migrations_dir, now=NOW)
    finally:
        conn.close()


def test_database_newer_than_known_migrations_raises(connection: sqlite3.Connection) -> None:
    connection.execute("UPDATE schema_migrations SET version = version + 100")
    with pytest.raises(SchemaTooNewError):
        apply_migrations(connection, now=NOW)


def test_broken_migration_rolls_back_every_object(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "broken_migrations"
    migrations_dir.mkdir()
    (migrations_dir / "0001_broken.sql").write_text(
        "CREATE TABLE schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT, sha256 TEXT, applied_at TEXT);\n"
        "CREATE TABLE partial_object (id INTEGER PRIMARY KEY);\n"
        "THIS IS NOT VALID SQL;\n",
        encoding="utf-8",
    )
    conn = open_connection(tmp_path / "broken.db")
    try:
        with pytest.raises(sqlite3.Error):
            apply_migrations(conn, migrations_dir, now=NOW)
        objects = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'index', 'trigger')"
            )
        }
        assert "partial_object" not in objects
        assert "schema_migrations" not in objects
        assert current_schema_version(conn) == 0
    finally:
        conn.close()


def test_account_scope_must_match_database_environment(connection: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="environment differs"):
        connection.execute(
            "INSERT INTO account_scopes("
            "account_scope_id, environment, broker, run_id, created_at"
            ") VALUES ('backtest:wrong', 'BACKTEST', 'simulated', 'run-1', ?)",
            (STAMP,),
        )


def test_initialize_rejects_existing_database_for_other_environment(tmp_path: Path) -> None:
    path = tmp_path / "identity.db"
    initialize_database(path, "PAPER", now=NOW).close()
    with pytest.raises(DatabaseIdentityMismatchError):
        initialize_database(path, "LIVE", now=NOW)


@pytest.mark.parametrize("verb", ["UPDATE", "DELETE"])
def test_db_identity_is_immutable(connection: sqlite3.Connection, verb: str) -> None:
    statement = (
        "UPDATE db_identity SET database_id = 'changed' WHERE id = 1"
        if verb == "UPDATE"
        else "DELETE FROM db_identity WHERE id = 1"
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable: db_identity"):
        connection.execute(statement)


@pytest.mark.parametrize("table", ["fills", "domain_events"])
@pytest.mark.parametrize("verb", ["UPDATE", "DELETE"])
def test_fill_and_domain_event_rows_are_append_only(
    connection: sqlite3.Connection, table: str, verb: str
) -> None:
    _scope(connection, "shadow:one")
    if table == "fills":
        _broker_order(connection, "shadow:one", "order-1")
        _fill(connection, "shadow:one", "fill-1", "activity-1", "order-1")
        key_column, key = "fill_id", "fill-1"
    else:
        _event(connection, "shadow:one")
        key_column, key = "event_id", "event-1"
    statement = (
        f"UPDATE {table} SET recorded_at = recorded_at WHERE {key_column} = ?"
        if verb == "UPDATE"
        else f"DELETE FROM {table} WHERE {key_column} = ?"
    )
    with pytest.raises(sqlite3.IntegrityError, match=f"append-only: {table}"):
        connection.execute(statement, (key,))


def test_duplicate_fill_external_activity_is_rejected(connection: sqlite3.Connection) -> None:
    _scope(connection, "shadow:one")
    _broker_order(connection, "shadow:one", "order-1")
    _fill(connection, "shadow:one", "fill-1", "activity-1", "order-1")
    with pytest.raises(sqlite3.IntegrityError):
        _fill(connection, "shadow:one", "fill-2", "activity-1", "order-1")


def test_fill_cannot_reference_broker_order_in_another_scope(
    connection: sqlite3.Connection,
) -> None:
    _scope(connection, "shadow:one")
    _scope(connection, "shadow:two")
    _broker_order(connection, "shadow:one", "order-1")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _fill(connection, "shadow:two", "fill-1", "activity-1", "order-1")


def test_non_text_money_value_fails_type_check(connection: sqlite3.Connection) -> None:
    _scope(connection, "shadow:one")
    _broker_order(connection, "shadow:one", "order-1")
    with pytest.raises(sqlite3.IntegrityError, match=r"typeof\(price\)"):
        _fill(
            connection,
            "shadow:one",
            "fill-1",
            "activity-1",
            "order-1",
            price=sqlite3.Binary(b"185.25"),
        )


def test_schema_has_no_tick_quote_or_raw_trade_archive(connection: sqlite3.Connection) -> None:
    table_names = {
        row[0].lower()
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    forbidden = {"ticks", "raw_ticks", "quotes", "trades_raw", "raw_trades"}
    assert table_names.isdisjoint(forbidden)


def test_domain_events_columns_match_shared_contract(connection: sqlite3.Connection) -> None:
    columns = [row[1] for row in connection.execute("PRAGMA table_info(domain_events)")]
    assert columns == [
        "event_seq",
        "event_id",
        "account_scope_id",
        "stream_type",
        "stream_id",
        "stream_version",
        "event_type",
        "payload_json",
        "payload_sha256",
        "idempotency_key",
        "causation_id",
        "correlation_id",
        "occurred_at",
        "recorded_at",
    ]


def test_timestamp_helpers_are_utc_and_fixed_width() -> None:
    offset_value = datetime(
        2026, 10, 8, 10, 30, 1, 123456, tzinfo=timezone(timedelta(hours=-4))
    )
    assert to_db_timestamp(offset_value) == STAMP
    assert from_db_timestamp(STAMP) == NOW
    with pytest.raises(ValueError, match="timezone-aware"):
        to_db_timestamp(datetime(2026, 10, 8))


def test_decimal_helpers_require_finite_decimals() -> None:
    assert decimal_to_db(Decimal("1.2300")) == "1.2300"
    assert decimal_from_db("1.2300") == Decimal("1.2300")
    with pytest.raises(TypeError):
        decimal_to_db(1.23)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite"):
        decimal_to_db(Decimal("NaN"))
    with pytest.raises(ValueError, match="finite"):
        decimal_from_db("Infinity")


# ----------------------------------------------------------- 0002: bidirectional sides


def _order(conn: sqlite3.Connection, scope_id: str, order_id: str, side: str) -> None:
    conn.execute(
        "INSERT INTO broker_orders("
        "account_scope_id, broker_order_id, client_order_id, symbol, side, order_type, "
        "status, quantity, filled_quantity, submitted_at, updated_at"
        ") VALUES (?, ?, ?, 'AAPL', ?, 'MARKET', 'FILLED', '1', '1', ?, ?)",
        (scope_id, order_id, f"client-{order_id}", side, STAMP, STAMP),
    )


def _fill_side(conn: sqlite3.Connection, scope_id: str, fill_id: str, order_id: str, side: str) -> None:
    conn.execute(
        "INSERT INTO fills("
        "fill_id, account_scope_id, external_activity_id, broker_order_id, symbol, side, "
        "quantity, price, fee, filled_at, recorded_at"
        ") VALUES (?, ?, ?, ?, 'AAPL', ?, '1', '150', '0', ?, ?)",
        (fill_id, scope_id, f"act-{fill_id}", order_id, side, STAMP, STAMP),
    )


def _strategy_instance(conn: sqlite3.Connection, scope_id: str, instance_id: str, role: str) -> None:
    conn.execute(
        "INSERT INTO strategy_instances(instance_id, account_scope_id, strategy_key, "
        "strategy_version, role, symbol, parameters_json, enabled, created_at) "
        "VALUES (?, ?, 'k', '1.0.0', ?, 'AAPL', '{}', 1, ?)",
        (instance_id, scope_id, role, STAMP),
    )


def test_fresh_database_accepts_short_roles_and_order_sides(connection: sqlite3.Connection) -> None:
    _scope(connection, "shadow:a")
    for number, role in enumerate(("BUY", "SELL", "SHORT", "COVER")):
        _strategy_instance(connection, "shadow:a", f"i{number}", role)
    for number, side in enumerate(("BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER")):
        _order(connection, "shadow:a", f"o{number}", side)
        _fill_side(connection, "shadow:a", f"f{number}", f"o{number}", side)
    sides = [r[0] for r in connection.execute("SELECT side FROM fills ORDER BY fill_id")]
    assert sides == ["BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER"]


@pytest.mark.parametrize("table", ["strategy_instances", "broker_orders", "fills"])
def test_unknown_roles_and_sides_are_still_rejected(connection: sqlite3.Connection, table: str) -> None:
    _scope(connection, "shadow:a")
    with pytest.raises(sqlite3.IntegrityError):
        if table == "strategy_instances":
            _strategy_instance(connection, "shadow:a", "bad", "HOLD")
        elif table == "broker_orders":
            _order(connection, "shadow:a", "bad", "SELL_LONG")
        else:
            _order(connection, "shadow:a", "ok", "BUY")
            _fill_side(connection, "shadow:a", "bad", "ok", "COVER")


def test_upgrade_of_a_populated_version_1_database_keeps_rows_triggers_and_keys(tmp_path: Path) -> None:
    v1 = tmp_path / "v1_only"
    v1.mkdir()
    first = discover_migrations()[0]
    shutil.copyfile(first.path, v1 / first.path.name)
    conn = open_connection(tmp_path / "upgrade.db")
    try:
        apply_migrations(conn, v1, now=NOW)  # an existing deployment at version 1
        assert current_schema_version(conn) == 1
        conn.execute(
            "INSERT INTO db_identity(id, environment, database_id, created_at) "
            "VALUES (1, 'SHADOW', 'db-upgrade', ?)", (STAMP,))
        _scope(conn, "shadow:a")
        _strategy_instance(conn, "shadow:a", "legacy", "BUY")
        _order(conn, "shadow:a", "o1", "BUY")
        _fill_side(conn, "shadow:a", "f1", "o1", "BUY")
        with pytest.raises(sqlite3.IntegrityError):  # version 1 really refuses a short side
            _order(conn, "shadow:a", "o-short", "SELL_SHORT")
        apply_migrations(conn, now=NOW + timedelta(days=1))  # upgrade in place
        assert current_schema_version(conn) == 3
        # existing rows survive, and the SAME connection already accepts the new vocabulary
        assert conn.execute("SELECT count(*) FROM fills").fetchone()[0] == 1
        _order(conn, "shadow:a", "o2", "SELL_SHORT")
        _fill_side(conn, "shadow:a", "f2", "o2", "SELL_SHORT")
        _strategy_instance(conn, "shadow:a", "cover", "COVER")
        # append-only triggers and foreign keys are untouched
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE fills SET fee = '1' WHERE fill_id = 'f2'")
        with pytest.raises(sqlite3.IntegrityError):
            _fill_side(conn, "shadow:a", "f3", "missing-order", "BUY")
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
    # a new connection (a restart) sees the widened schema as well
    reopened = open_connection(tmp_path / "upgrade.db")
    try:
        _order(reopened, "shadow:a", "o3", "BUY_TO_COVER")
    finally:
        reopened.close()
