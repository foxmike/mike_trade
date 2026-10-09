"""Container-local health checks with no trading or mutation side effects."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def engine() -> int:
    path = Path(os.environ.get("MIKE_TRADE_DB_PATH", "/app/state/shadow.sqlite3"))
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            row = connection.execute(
                "SELECT status, heartbeat_at, details_json FROM service_health "
                "WHERE component = 'engine' ORDER BY heartbeat_at DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        if row is None or row[0] != "HEALTHY" or not row[1]:
            return 1
        details = json.loads(str(row[2]))
        if details.get("state") != "RUNNING" or details.get("reconciliation_status") != "CLEAN":
            return 1
        age = (datetime.now(UTC) - _timestamp(str(row[1]))).total_seconds()
        return 0 if 0 <= age <= 30 else 1
    except (OSError, sqlite3.Error, ValueError):
        return 1


def api() -> int:
    try:
        with urlopen("http://127.0.0.1:8765/api/live", timeout=3) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["engine"]:
        return engine()
    if args == ["api"]:
        return api()
    print("usage: python -m mike_trade.healthcheck engine|api", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
