"""Read-only access to the operational SQLite database."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from mike_trade.persistence import schema

__all__ = ["ReadStore", "ReadStoreUnavailableError", "ReadOnlyViolationError"]


class ReadStoreUnavailableError(RuntimeError):
    """The requested database does not exist or cannot be opened read-only."""


class ReadOnlyViolationError(RuntimeError):
    """A caller attempted to modify data through the read store."""


_WRITE_ACTIONS = frozenset(
    action
    for action in (
        getattr(sqlite3, name, None)
        for name in (
            "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "SQLITE_CREATE_INDEX",
            "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX", "SQLITE_CREATE_TEMP_TABLE",
            "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER",
            "SQLITE_CREATE_VIEW", "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE",
            "SQLITE_DROP_TEMP_INDEX", "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_TRIGGER",
            "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER", "SQLITE_DROP_VIEW",
            "SQLITE_ALTER_TABLE", "SQLITE_REINDEX", "SQLITE_ANALYZE", "SQLITE_ATTACH",
            "SQLITE_DETACH",
        )
    )
    if action is not None
)


class _ReadConnection:
    """Small connection surface that gives denied writes a stable exception."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, parameters=()):
        try:
            return self._conn.execute(sql, parameters)
        except sqlite3.DatabaseError as error:
            if "not authorized" in str(error).lower() or "readonly" in str(error).lower():
                raise ReadOnlyViolationError("read-only database connection") from error
            raise

    def executemany(self, sql: str, parameters):
        try:
            return self._conn.executemany(sql, parameters)
        except sqlite3.DatabaseError as error:
            if "not authorized" in str(error).lower() or "readonly" in str(error).lower():
                raise ReadOnlyViolationError("read-only database connection") from error
            raise

    def executescript(self, sql: str):
        try:
            return self._conn.executescript(sql)
        except sqlite3.DatabaseError as error:
            if "not authorized" in str(error).lower() or "readonly" in str(error).lower():
                raise ReadOnlyViolationError("read-only database connection") from error
            raise

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


class ReadStore:
    """Read-only SQLite facade; opening it never initializes or locks a database."""

    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self._conn = conn
        self.path = path
        self._view = _ReadConnection(conn)
        conn.set_authorizer(
            lambda action, _arg1, _arg2, _db, _source: (
                sqlite3.SQLITE_DENY if action in _WRITE_ACTIONS else sqlite3.SQLITE_OK
            )
        )

    @classmethod
    def open(cls, db_path: str | Path) -> "ReadStore":
        path = Path(db_path).expanduser().resolve()
        if not path.is_file():
            raise ReadStoreUnavailableError(f"database does not exist: {path}")
        try:
            conn = schema.open_connection(path, read_only=True)
        except (OSError, sqlite3.Error) as error:
            raise ReadStoreUnavailableError(f"cannot open database read-only: {path}") from error
        return cls(conn, path)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ReadStore":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def database_identity(self) -> dict[str, object] | None:
        row = self._view.execute(
            "SELECT environment, database_id, created_at FROM db_identity WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "environment": row[0],
            "database_id": row[1],
            "created_at": schema.from_db_timestamp(row[2]),
        }

    def list_account_scopes(self) -> list[dict[str, str | None]]:
        rows = self._view.execute(
            "SELECT account_scope_id, environment, scenario_name, run_id "
            "FROM account_scopes ORDER BY account_scope_id"
        ).fetchall()
        return [
            {
                "account_scope_id": row[0],
                "environment": row[1],
                "scenario_name": row[2],
                "run_id": row[3],
            }
            for row in rows
        ]

    @contextmanager
    def snapshot(self, account_scope_id: str) -> Iterator[_ReadConnection]:
        """Yield a transactionally consistent, scope-validated WAL snapshot."""
        if not isinstance(account_scope_id, str) or not account_scope_id.strip():
            raise ValueError("account_scope_id must be a non-empty string")
        self._view.execute("BEGIN DEFERRED")
        try:
            self._view.execute(
                "SELECT account_scope_id FROM account_scopes WHERE account_scope_id = ?",
                (account_scope_id,),
            )
            yield self._view
            self._view.execute("COMMIT")
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
