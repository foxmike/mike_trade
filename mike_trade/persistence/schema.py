"""SQLite connection, migration, and scalar-persistence helpers.

Design choices (4, the task's decision budget):
1. ``discover_migrations`` returns immutable private records containing the
   version, unprefixed name, path, text, and text checksum.
2. Migration names stored in ``schema_migrations`` omit the numeric prefix and
   ``.sql`` suffix (for example, ``initial``).
3. Database environments may be supplied as either ``AccountEnvironment``
   values or their uppercase strings, keeping this infrastructure boundary
   convenient without duplicating the domain enum.
4. Decimal reads reject non-finite values just as decimal writes do, so corrupt
   persisted numeric text cannot silently enter the domain.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final
from uuid import uuid4

from mike_trade.domain.accounts import AccountEnvironment
from mike_trade.domain.errors import PersistenceError

__all__ = [
    "open_connection",
    "discover_migrations",
    "apply_migrations",
    "current_schema_version",
    "initialize_database",
    "to_db_timestamp",
    "from_db_timestamp",
    "decimal_to_db",
    "decimal_from_db",
    "EXPECTED_TABLES",
    "MigrationChecksumError",
    "SchemaTooNewError",
    "DatabaseIdentityMismatchError",
]


class MigrationChecksumError(PersistenceError):
    """An applied migration no longer has its recorded contents."""


class SchemaTooNewError(PersistenceError):
    """The database was migrated by a newer version of the application."""


class DatabaseIdentityMismatchError(PersistenceError):
    """The database identity does not match the requested environment."""


EXPECTED_TABLES: Final[frozenset[str]] = frozenset(
    {
        "schema_migrations",
        "db_identity",
        "account_scopes",
        "run_configs",
        "operator_commands",
        "service_health",
        "instruments",
        "instrument_capabilities",
        "market_bars",
        "strategy_instances",
        "strategy_evaluations",
        "signals",
        "decisions",
        "position_cycles",
        "budget_reservations",
        "order_intents",
        "broker_orders",
        "broker_order_events",
        "fills",
        "broker_activities",
        "protection_orders",
        "trailing_stop_states",
        "trailing_stop_events",
        "positions",
        "cash_events",
        "equity_snapshots",
        "reconciliation_runs",
        "reconciliation_drifts",
        "reconciliation_resolutions",
        "strategy_state",
        "entry_eligibility_counters",
        "entry_lockouts",
        "entry_cooldowns",
        "entry_controls",
        "backtest_manifests",
        "domain_events",
    }
)

_MIGRATION_RE = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[A-Za-z0-9][A-Za-z0-9_]*)\.sql$")
_ENVIRONMENTS = frozenset(item.value for item in AccountEnvironment)


@dataclass(frozen=True, slots=True)
class _Migration:
    version: int
    name: str
    path: Path
    text: str
    sha256: str


def open_connection(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a configured SQLite connection for an operational database."""
    database_path = Path(path).expanduser().resolve()
    if read_only:
        uri = f"{database_path.as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA query_only = ON")
        except BaseException:
            conn.close()
            raise
        return conn

    conn = sqlite3.connect(database_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        if mode != "wal":
            raise PersistenceError(f"SQLite refused WAL mode for {database_path}: {mode!r}")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA busy_timeout = 5000")
    except BaseException:
        conn.close()
        raise
    return conn


def discover_migrations(migrations_dir: str | Path | None = None) -> tuple[_Migration, ...]:
    """Read a complete, consecutively numbered set of forward migrations."""
    directory = (
        Path(migrations_dir)
        if migrations_dir is not None
        else Path(__file__).parent / "migrations"
    )
    discovered: list[_Migration] = []
    versions: set[int] = set()
    paths = directory.iterdir() if directory.is_dir() else ()
    for path in sorted(paths, key=lambda item: item.name):
        match = _MIGRATION_RE.fullmatch(path.name)
        if match is None or not path.is_file():
            continue
        version = int(match.group("version"))
        if version in versions:
            raise PersistenceError(f"duplicate migration version {version:04d} in {directory}")
        versions.add(version)
        text = path.read_text(encoding="utf-8")
        discovered.append(
            _Migration(
                version=version,
                name=match.group("name"),
                path=path,
                text=text,
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        )
    discovered.sort(key=lambda migration: migration.version)
    actual = [migration.version for migration in discovered]
    expected = list(range(1, len(discovered) + 1))
    if actual != expected:
        raise PersistenceError(
            f"migration versions must be consecutive starting at 0001; found {actual}"
        )
    return tuple(discovered)


def _schema_migrations_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    return row is not None


def current_schema_version(conn: sqlite3.Connection) -> int:
    """Return the greatest applied migration version, or zero for a fresh DB."""
    if not _schema_migrations_exists(conn):
        return 0
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    return int(row[0])


def _migration_statements(text: str) -> tuple[str, ...]:
    """Split SQL without losing trigger bodies; unlike executescript, do not commit."""
    statements: list[str] = []
    buffer = ""
    for character in text:
        buffer += character
        if character == ";" and sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer)
            buffer = ""
    remaining_lines = (line for line in buffer.splitlines() if line.strip())
    if buffer.strip() and not all(line.lstrip().startswith("--") for line in remaining_lines):
        if not sqlite3.complete_statement(buffer):
            raise sqlite3.OperationalError("incomplete SQL at end of migration")
        statements.append(buffer)
    return tuple(statements)


def apply_migrations(
    conn: sqlite3.Connection,
    migrations_dir: str | Path | None = None,
    *,
    now: datetime,
) -> None:
    """Verify applied migration contents and atomically apply each pending file."""
    migrations = discover_migrations(migrations_dir)
    current = current_schema_version(conn)
    highest_known = migrations[-1].version if migrations else 0
    if current > highest_known:
        raise SchemaTooNewError(
            f"database schema version {current} exceeds highest known version {highest_known}"
        )

    applied: dict[int, tuple[str, str]] = {}
    if _schema_migrations_exists(conn):
        rows = conn.execute("SELECT version, name, sha256 FROM schema_migrations").fetchall()
        applied = {int(version): (str(name), str(checksum)) for version, name, checksum in rows}
    known_by_version = {migration.version: migration for migration in migrations}
    unknown_applied = sorted(set(applied) - set(known_by_version))
    if unknown_applied:
        raise SchemaTooNewError(f"database contains unknown migration versions: {unknown_applied}")
    for version, (_, recorded_checksum) in applied.items():
        migration = known_by_version[version]
        if recorded_checksum != migration.sha256:
            raise MigrationChecksumError(
                f"migration {version:04d}_{migration.name}.sql checksum does not match database"
            )

    applied_at = to_db_timestamp(now)
    for migration in migrations:
        if migration.version in applied:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _migration_statements(migration.text):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, sha256, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.sha256, applied_at),
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def _environment_value(environment: AccountEnvironment | str) -> str:
    value = environment.value if isinstance(environment, AccountEnvironment) else environment
    if not isinstance(value, str) or value not in _ENVIRONMENTS:
        raise ValueError(f"environment must be one of {sorted(_ENVIRONMENTS)}")
    return value


def initialize_database(
    path: str | Path,
    environment: AccountEnvironment | str,
    *,
    now: datetime,
) -> sqlite3.Connection:
    """Open, migrate, and bind a database permanently to one environment."""
    expected_environment = _environment_value(environment)
    conn = open_connection(path)
    try:
        apply_migrations(conn, now=now)
        created_at = to_db_timestamp(now)
        conn.execute("BEGIN IMMEDIATE")
        try:
            identity = conn.execute(
                "SELECT environment FROM db_identity WHERE id = 1"
            ).fetchone()
            if identity is None:
                conn.execute(
                    "INSERT INTO db_identity(id, environment, database_id, created_at) "
                    "VALUES (1, ?, ?, ?)",
                    (expected_environment, str(uuid4()), created_at),
                )
            elif identity[0] != expected_environment:
                raise DatabaseIdentityMismatchError(
                    f"database environment is {identity[0]!r}, not {expected_environment!r}"
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    except BaseException:
        conn.close()
        raise
    return conn


def to_db_timestamp(value: datetime) -> str:
    """Serialize an aware datetime as fixed-width microsecond UTC text."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("database timestamps require a timezone-aware datetime")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def from_db_timestamp(text: str) -> datetime:
    """Parse the database's fixed-width UTC timestamp representation."""
    if not isinstance(text, str):
        raise TypeError("database timestamp must be str")
    try:
        value = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise ValueError(f"invalid database timestamp: {text!r}") from error
    if to_db_timestamp(value) != text:
        raise ValueError(f"invalid database timestamp: {text!r}")
    return value


def decimal_to_db(value: Decimal) -> str:
    """Serialize a finite Decimal without accepting lossy float input."""
    if not isinstance(value, Decimal):
        raise TypeError("database decimal value must be Decimal")
    if not value.is_finite():
        raise ValueError("database decimal value must be finite")
    return str(value)


def decimal_from_db(text: str) -> Decimal:
    """Parse finite decimal text from SQLite."""
    if not isinstance(text, str):
        raise TypeError("database decimal value must be str")
    try:
        value = Decimal(text)
    except InvalidOperation as error:
        raise ValueError(f"invalid database decimal: {text!r}") from error
    if not value.is_finite():
        raise ValueError("database decimal value must be finite")
    return value
