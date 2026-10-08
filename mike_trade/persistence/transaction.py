"""Single-writer transaction layer for operational SQLite databases.

One process owns the authoritative write path for a database: ``WriterLock``
holds an exclusive, non-blocking advisory ``flock`` on ``<db_path>.writer.lock``
and ``WriterSession`` pairs that lock with the only write connection.  All
mutation happens inside ``unit_of_work()`` (``BEGIN IMMEDIATE`` ... commit or
rollback), and repositories reach the connection through a ``ScopedWriter``
bound to one account scope so an entity from another scope can never be written.

Scope environments are read from the environment-qualified scope id
(``paper:ACCT``) so a scope can be bound before it is registered.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from types import TracebackType

from mike_trade.domain.accounts import AccountEnvironment
from mike_trade.domain.errors import ModeIsolationError, PersistenceError
from mike_trade.persistence.schema import open_connection

__all__ = [
    "WriterLock",
    "WriterSession",
    "ScopedWriter",
    "WriterLockHeldError",
    "WriterBusyError",
    "CrossScopeWriteError",
]

_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6


class WriterLockHeldError(PersistenceError):
    """Another writer already holds the lock for this database."""


class WriterBusyError(PersistenceError):
    """SQLite stayed busy for longer than the connection's busy_timeout."""


class CrossScopeWriteError(PersistenceError):
    """An entity belonging to a different account scope was presented for writing."""


def _is_busy(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in (_SQLITE_BUSY, _SQLITE_LOCKED)
    text = str(error).lower()
    return "database is locked" in text or "database table is locked" in text


class WriterLock:
    """Exclusive advisory lock proving this process is the database's writer."""

    def __init__(self, db_path: str | Path) -> None:
        database_path = Path(db_path).expanduser().resolve()
        self._lock_path = Path(f"{database_path}.writer.lock")
        self._fd: int | None = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._fd)
            self._fd = None
            raise WriterLockHeldError(
                f"another writer holds {self._lock_path}",
                code="writer_lock_held",
                context={"lock_path": str(self._lock_path)},
            ) from exc
        except BaseException:
            os.close(self._fd)
            self._fd = None
            raise
        os.ftruncate(self._fd, 0)
        os.write(self._fd, f"{os.getpid()}\n".encode("ascii"))

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def held(self) -> bool:
        return self._fd is not None

    def close(self) -> None:
        """Release the lock; safe to call more than once."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> WriterLock:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class WriterSession:
    """The writer lock plus the single write connection for one database."""

    def __init__(self, db_path: str | Path) -> None:
        database_path = Path(db_path).expanduser().resolve()
        if not database_path.is_file():
            raise PersistenceError(f"database {database_path} is not initialized")
        self._lock: WriterLock | None = WriterLock(database_path)
        self._conn: sqlite3.Connection | None = None
        self._in_unit_of_work = False
        try:
            self._conn = open_connection(database_path)
            try:
                row = self._conn.execute(
                    "SELECT environment FROM db_identity WHERE id = 1"
                ).fetchone()
            except sqlite3.Error as exc:
                raise PersistenceError(
                    f"database {database_path} is not initialized"
                ) from exc
            if row is None:
                raise PersistenceError(f"database {database_path} has no identity")
            self._environment = AccountEnvironment(row[0])
        except BaseException:
            self.close()
            raise

    @property
    def environment(self) -> AccountEnvironment:
        return self._environment

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise PersistenceError("writer session is closed")
        return self._conn

    @contextmanager
    def unit_of_work(self) -> Iterator[sqlite3.Connection]:
        """Run one ``BEGIN IMMEDIATE`` transaction: commit on success, else roll back."""
        conn = self.connection
        if self._in_unit_of_work:
            raise PersistenceError("nested unit_of_work is not permitted")
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if _is_busy(exc):
                raise WriterBusyError("database stayed busy past busy_timeout") from exc
            raise
        self._in_unit_of_work = True
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException as exc:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(exc, sqlite3.OperationalError) and _is_busy(exc):
                raise WriterBusyError("database stayed busy past busy_timeout") from exc
            raise
        finally:
            self._in_unit_of_work = False

    def bind_scope(self, account_scope_id: str) -> ScopedWriter:
        """Bind to one account scope whose environment matches the database identity."""
        if not isinstance(account_scope_id, str) or not account_scope_id.strip():
            raise ValueError("account_scope_id must be a non-empty str")
        scope_id = account_scope_id.strip()
        environment = scope_id.split(":", 1)[0].upper()
        if environment != self._environment.value:
            raise ModeIsolationError(
                f"scope {scope_id!r} environment {environment!r} differs from database "
                f"environment {self._environment.value!r}",
                code="scope_environment_mismatch",
            )
        row = self.connection.execute(
            "SELECT environment FROM account_scopes WHERE account_scope_id = ?", (scope_id,)
        ).fetchone()
        if row is not None and row[0] != self._environment.value:
            raise ModeIsolationError(
                f"registered scope {scope_id!r} has environment {row[0]!r}",
                code="scope_environment_mismatch",
            )
        return ScopedWriter(self, scope_id)

    def close(self) -> None:
        """Close the connection and release the writer lock."""
        conn, self._conn = self._conn, None
        lock, self._lock = self._lock, None
        try:
            if conn is not None:
                conn.close()
        finally:
            if lock is not None:
                lock.close()

    def __enter__(self) -> WriterSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class ScopedWriter:
    """A ``WriterSession`` view that only accepts entities of one account scope."""

    def __init__(self, session: WriterSession, account_scope_id: str) -> None:
        self._session = session
        self._account_scope_id = account_scope_id

    @property
    def account_scope_id(self) -> str:
        return self._account_scope_id

    @property
    def environment(self) -> AccountEnvironment:
        return self._session.environment

    @property
    def connection(self) -> sqlite3.Connection:
        return self._session.connection

    def unit_of_work(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._session.unit_of_work()

    def require_scope(self, entity_account_scope_id: str) -> None:
        """Raise ``CrossScopeWriteError`` unless the entity belongs to this scope."""
        if entity_account_scope_id != self._account_scope_id:
            raise CrossScopeWriteError(
                f"entity scope {entity_account_scope_id!r} differs from writer scope "
                f"{self._account_scope_id!r}",
                code="cross_scope_write",
                context={
                    "writer_scope": self._account_scope_id,
                    "entity_scope": entity_account_scope_id,
                },
            )
