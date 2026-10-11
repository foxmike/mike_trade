"""Restart persistence ports and adapters for Market Status Monitor.

Provides atomic serialization and restoration of market status monitor state,
ensuring uninterrupted tracking and state continuity across engine restarts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Protocol

from datetime import UTC, datetime

from mike_trade.persistence.schema import to_db_timestamp

__all__ = [
    "MarketStatusRepository",
    "InMemoryMarketStatusRepository",
    "JsonFileMarketStatusRepository",
    "SqliteMarketStatusRepository",
]


class MarketStatusRepository(Protocol):
    """Persistence port for market status monitor state documents."""

    def load(self, scope_id: str) -> Mapping[str, Any] | None:
        """Load persisted state for scope; ``None`` if absent, ``ValueError`` if unreadable."""
        ...

    def save(self, scope_id: str, document: Mapping[str, Any]) -> None:
        """Atomically persist market status state document for scope."""
        ...


class InMemoryMarketStatusRepository:
    """In-memory market status persistence repository for tests and replay."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def load(self, scope_id: str) -> Mapping[str, Any] | None:
        return self._store.get(scope_id)

    def save(self, scope_id: str, document: Mapping[str, Any]) -> None:
        # Deepcopy-equivalent via dict copy
        self._store[scope_id] = json.loads(json.dumps(document))

    def clear(self) -> None:
        self._store.clear()


class JsonFileMarketStatusRepository:
    """Atomic file-based JSON repository for persistent service restarts."""

    def __init__(self, base_directory: Path | str) -> None:
        self.directory = Path(base_directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    def _file_path(self, scope_id: str) -> Path:
        sanitized = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in scope_id)
        return self.directory / f"market_status_{sanitized}.json"

    def load(self, scope_id: str) -> Mapping[str, Any] | None:
        path = self._file_path(scope_id)
        if not path.exists():
            return None
        # An existing but unreadable document is an error, never "no state": the caller must
        # fail closed instead of silently starting from NORMAL.
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"unreadable market status document: {path}") from exc
        if not isinstance(data, Mapping):
            raise ValueError(f"malformed market status document: {path}")
        return data

    def save(self, scope_id: str, document: Mapping[str, Any]) -> None:
        path = self._file_path(scope_id)
        text = json.dumps(document, indent=2, sort_keys=True)
        # Atomic write via temporary file replacement
        with NamedTemporaryFile(
            mode="w",
            dir=self.directory,
            encoding="utf-8",
            delete=False,
            prefix="tmp_mkt_status_",
            suffix=".json",
        ) as tmp:
            tmp.write(text)
            tmp_path = Path(tmp.name)
        tmp_path.replace(path)


class SqliteMarketStatusRepository:
    """SQLite-backed repository using the existing service_health table."""

    def __init__(self, session: Any, clock: Any = None) -> None:
        self._session = session
        self._clock = clock

    def _connection(self) -> Any:
        return getattr(self._session, "connection", self._session)

    def load(self, scope_id: str) -> Mapping[str, Any] | None:
        conn = self._connection()
        row = conn.execute(
            "SELECT details_json FROM service_health WHERE account_scope_id = ? "
            "AND component = ?",
            (scope_id, "market_status_monitor"),
        ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row[0])
        except Exception as exc:
            raise ValueError("unreadable market status document") from exc
        if not isinstance(data, Mapping):
            raise ValueError("malformed market status document")
        return data

    def save(self, scope_id: str, document: Mapping[str, Any]) -> None:
        now = (
            self._clock.now() if self._clock and hasattr(self._clock, "now") else datetime.now(UTC)
        ).astimezone(UTC)
        text = json.dumps(document, sort_keys=True, default=str)
        curr_state = str(document.get("current_state", "")).upper()
        status = "DEGRADED" if curr_state in {"CRASH", "WARNING"} else "HEALTHY"
        ts = to_db_timestamp(now)
        env = scope_id.split(":")[0].upper()

        def _execute(conn: Any) -> None:
            conn.execute(
                "INSERT OR IGNORE INTO account_scopes(account_scope_id, environment, broker, "
                "broker_account_ref, created_at) VALUES (?, ?, 'none', 'none', ?)",
                (scope_id, env, ts),
            )
            conn.execute(
                "INSERT INTO service_health(account_scope_id, component, status, heartbeat_at, "
                "observed_at, details_json) VALUES (?, 'market_status_monitor', ?, ?, ?, ?) "
                "ON CONFLICT(account_scope_id, component) DO UPDATE SET "
                "status = excluded.status, heartbeat_at = excluded.heartbeat_at, "
                "observed_at = excluded.observed_at, details_json = excluded.details_json",
                (scope_id, status, ts, ts, text),
            )

        if hasattr(self._session, "unit_of_work"):
            with self._session.unit_of_work() as conn:
                _execute(conn)
        else:
            conn = self._connection()
            _execute(conn)
