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

__all__ = [
    "MarketStatusRepository",
    "InMemoryMarketStatusRepository",
    "JsonFileMarketStatusRepository",
]


class MarketStatusRepository(Protocol):
    """Persistence port for market status monitor state documents."""

    def load(self, scope_id: str) -> Mapping[str, Any] | None:
        """Load persisted market status state for scope, or None if absent."""
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
        try:
            content = path.read_text(encoding="utf-8")
            data = json.loads(content)
            if isinstance(data, Mapping):
                return data
            return None
        except (OSError, json.JSONDecodeError):
            return None

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
