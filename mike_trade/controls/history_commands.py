"""Bounded, filterable, read-only history and health commands (phase S).

Entry points (called lazily by ``mike_trade.cli``; never import this module
from anywhere else at import time):

* :func:`run_history_command` -- dispatch ``signals`` / ``evaluations`` /
  ``events`` / ``logs`` / ``health``.
* :func:`add_history_arguments` -- attach filter options to an argparse
  subparser (skips options the caller already registered).

Read-only contract: the SQLite database is opened with ``mode=ro`` URI plus
``PRAGMA query_only`` (via :class:`ReadStore`) and an authorizer that denies
writes.  These commands perform no writes and import no broker or
data-provider modules.  Output is redacted (no secrets, no full account
identifiers, no raw provider payloads) and Decimal values render as strings
in JSON output.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mike_trade.observability.metrics import evaluate_alerts
from mike_trade.persistence.readstore import ReadStore, ReadStoreUnavailableError
from mike_trade.persistence.schema import from_db_timestamp, to_db_timestamp

__all__ = [
    "run_history_command",
    "add_history_arguments",
    "HISTORY_DEFAULT_LIMIT",
    "HISTORY_MAX_LIMIT",
]

HISTORY_DEFAULT_LIMIT = 50
HISTORY_MAX_LIMIT = 1000

LOG_MAX_BYTES = 256 * 1024
LOG_FILE_PATTERNS = ("*.jsonl", "*.log")

NEW_YORK = ZoneInfo("America/New_York")

# Injectable clock for tests: any object with ``now() -> aware datetime``.
_CLOCK: Any | None = None

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNHEALTHY = 5

_SECRET_PARTS = ("secret", "password", "token", "api_key", "key_id", "private_key")
_ACCOUNT_KEYS = frozenset(
    {"account", "account_id", "account_scope_id", "broker_account_ref"}
)
# Provider payload containers are never printed raw; only allowlisted keys pass.
_PROVIDER_CONTAINER_KEYS = frozenset(
    {
        "provenance_json", "payload_json", "raw_json", "details_json",
        "provider_payload", "raw_payload", "provider_response", "error_detail",
    }
)
_PROVIDER_ALLOWLIST = frozenset(
    {
        "symbol", "side", "quantity", "price", "status", "order_id",
        "client_order_id", "order_type", "time_in_force", "filled_quantity",
        "event_type",
    }
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _clock_now(args: Any) -> datetime:
    clock = getattr(args, "clock", None)
    if clock is None:
        clock = _CLOCK
    if clock is None:
        return datetime.now(UTC)
    now = clock.now() if hasattr(clock, "now") else clock()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return now.astimezone(UTC)


def _clamp_limit(raw: Any) -> tuple[int, bool]:
    try:
        limit = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return HISTORY_DEFAULT_LIMIT, True
    if limit < 1:
        return HISTORY_DEFAULT_LIMIT, True
    if limit > HISTORY_MAX_LIMIT:
        return HISTORY_MAX_LIMIT, True
    return limit, False


def _get(args: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        value = getattr(args, name, None)
        if value is not None:
            return value
    return default


def _resolve_db_path(args: Any) -> Path:
    for attr in ("db_path", "database", "database_path"):
        value = getattr(args, attr, None)
        if value:
            return Path(str(value)).expanduser()
    env_path = os.environ.get("MIKE_TRADE_DB_PATH") or os.environ.get("MIKE_TRADE_DB")
    if env_path:
        return Path(env_path).expanduser()
    data_dir = (
        getattr(args, "data_dir", None)
        or os.environ.get("MIKE_TRADE_DATA_DIR")
        or "/var/lib/mike_trade"
    )
    return Path(str(data_dir)).expanduser() / "mike_trade.db"


def _resolve_log_dir(args: Any) -> Path:
    for attr in ("log_dir", "logdir", "logs_dir"):
        value = getattr(args, attr, None)
        if value:
            return Path(str(value)).expanduser()
    env_dir = os.environ.get("MIKE_TRADE_LOG_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    data_dir = (
        getattr(args, "data_dir", None)
        or os.environ.get("MIKE_TRADE_DATA_DIR")
        or "/var/lib/mike_trade"
    )
    return Path(str(data_dir)).expanduser() / "logs"


def _threshold(args: Any, name: str, default: float) -> float:
    value = getattr(args, name, None)
    if value is None:
        env_map = {
            "heartbeat_offline_seconds": "MIKE_TRADE_HEARTBEAT_OFFLINE_SECONDS",
            "heartbeat_stale_seconds": "MIKE_TRADE_HEARTBEAT_STALE_SECONDS",
            "feed_max_age_seconds": "MIKE_TRADE_FEED_MAX_AGE_SECONDS",
        }
        raw = os.environ.get(env_map.get(name, ""), None)
        if raw is None:
            return float(default)
        try:
            return float(raw)
        except ValueError:
            return float(default)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float(default)


def _parse_time(value: str | None, *, is_end: bool) -> datetime | None:
    """Parse ISO-8601 or an America/New_York session date (YYYY-MM-DD)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    text = str(value).strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        from datetime import date as _date
        from datetime import time as _time

        day = _date.fromisoformat(text)
        if is_end:
            local = datetime.combine(day, _time(23, 59, 59, 999999), NEW_YORK)
        else:
            local = datetime.combine(day, _time(0, 0, 0), NEW_YORK)
        return local.astimezone(UTC)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        # Naive ISO timestamps are interpreted as New York wall time.
        parsed = parsed.replace(tzinfo=NEW_YORK)
    return parsed.astimezone(UTC)


def _to_db(value: datetime) -> str:
    return to_db_timestamp(value)


def _secret_key(key: str) -> bool:
    low = key.lower()
    return any(part in low for part in _SECRET_PARTS)


def _mask_account(value: Any) -> str:
    text = str(value)
    return "****" + text[-4:] if len(text) > 4 else "****"


def _redact_mapping(mapping: Mapping[str, Any] | dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in dict(mapping).items():
        name = str(key)
        low = name.lower()
        if _secret_key(name):
            out[name] = "***REDACTED***"
        elif low in _ACCOUNT_KEYS:
            out[name] = _mask_account(value)
        elif isinstance(value, dict):
            out[name] = _redact_mapping(value)
        elif isinstance(value, (list, tuple)):
            out[name] = [
                _redact_mapping(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            out[name] = value
    return out


def _safe_row(row: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            cleaned[key] = str(value)
        elif isinstance(value, datetime):
            cleaned[key] = value.astimezone(UTC).isoformat()
        elif isinstance(value, Mapping):
            cleaned[key] = _redact_mapping(dict(value))
        elif isinstance(value, str) and key in _PROVIDER_CONTAINER_KEYS:
            try:
                parsed = json.loads(value) if value.strip() else {}
            except (ValueError, AttributeError):
                parsed = {}
            if isinstance(parsed, Mapping):
                cleaned[key] = {
                    str(k): v for k, v in parsed.items()
                    if str(k) in _PROVIDER_ALLOWLIST
                }
            else:
                cleaned[key] = {}
        else:
            cleaned[key] = value
    return _redact_mapping(cleaned)


def _emit(rows: list[dict[str, Any]], output_format: str, *, clamped_note: bool) -> None:
    safe = [_safe_row(row) for row in rows]
    if output_format == "json":
        payload: dict[str, Any] = {"rows": safe}
        if clamped_note:
            payload["limit_clamped_to"] = HISTORY_MAX_LIMIT
        print(json.dumps(payload, sort_keys=True, default=str))
        return
    if clamped_note:
        print(f"(limit clamped to {HISTORY_MAX_LIMIT})")
    if not safe:
        print("(no data)")
        return
    columns = list(safe[0].keys())
    widths = {col: max(len(col), *(len(str(r.get(col, ""))) for r in safe)) for col in columns}
    print("  ".join(col.ljust(widths[col]) for col in columns))
    for row in safe:
        print("  ".join(str(row.get(col, "")).ljust(widths[col]) for col in columns))


# ---------------------------------------------------------------------------
# Scope resolution (strict: exactly one mode/account/scenario)
# ---------------------------------------------------------------------------

def _resolve_scope(store: ReadStore, args: Any) -> str:
    raw_account = str(_get(args, "account", default="") or "").strip()
    scenario = _get(args, "scenario", default=None)
    if scenario is not None:
        scenario = str(scenario).strip() or None
    if not raw_account:
        raise ValueError("account is required (e.g. --account SHADOW:default)")
    scopes = store.list_account_scopes()
    if not scopes:
        raise ValueError("no account scopes in database")

    candidates: list[dict[str, Any]] = []
    if ":" in raw_account:
        parts = raw_account.split(":")
        env = parts[0].upper()
        if len(parts) == 2:
            account_id, embedded_scenario = parts[1], scenario
        elif len(parts) == 3:
            account_id, embedded_scenario = parts[1], parts[2]
            if scenario is not None and scenario != embedded_scenario:
                raise ValueError("scenario conflicts with the embedded account scenario")
        else:
            raise ValueError(f"unparseable account {raw_account!r}")
        for scope in scopes:
            scope_id = str(scope["account_scope_id"])
            sparts = scope_id.split(":")
            if len(sparts) == 3:
                s_env, s_acct, s_scen = sparts[0].upper(), sparts[1], sparts[2]
            elif len(sparts) == 2:
                s_env, s_acct, s_scen = sparts[0].upper(), sparts[1], None
            else:  # pragma: no cover - defensive
                continue
            if s_env != env:
                continue
            if account_id not in ("", "*", s_acct):
                continue
            if embedded_scenario is not None and s_scen != embedded_scenario:
                continue
            candidates.append(scope)
    else:
        needle = raw_account.lower()
        for scope in scopes:
            scope_id = str(scope["account_scope_id"]).lower()
            starts = scope_id.startswith
            if scope_id == needle or starts(needle + ":") or starts(needle):
                if scenario is not None and str(scope.get("scenario_name") or "") != scenario:
                    continue
                candidates.append(scope)

    unique = sorted({str(s["account_scope_id"]) for s in candidates})
    if len(unique) != 1:
        if not unique:
            raise ValueError(f"account {raw_account!r} matches no known scope")
        raise ValueError(
            f"account {raw_account!r} is ambiguous ({len(unique)} scopes); "
            "specify account and scenario strictly"
        )
    scope_id = unique[0]
    run_filter = _get(args, "run_id", "runId", default=None)
    if run_filter is not None:
        for scope in scopes:
            if str(scope["account_scope_id"]) == scope_id:
                scope_run = scope.get("run_id")
                if scope_run is not None and str(scope_run) != str(run_filter):
                    raise ValueError(f"run_id {run_filter!r} does not match scope {scope_id}")
    return scope_id


def _open_store(args: Any) -> ReadStore:
    return ReadStore.open(_resolve_db_path(args))


# ---------------------------------------------------------------------------
# Query builders
# ---------------------------------------------------------------------------

def _strategy_key(conn: Any, scope: str, instance_id: str | None) -> str | None:
    if not instance_id:
        return None
    row = conn.execute(
        "SELECT strategy_key FROM strategy_instances "
        "WHERE account_scope_id = ? AND instance_id = ?",
        (scope, instance_id),
    ).fetchone()
    return str(row[0]) if row else None


def _query_signals(conn: Any, scope: str, args: Any, limit: int) -> list[dict[str, Any]]:
    clauses = ["s.account_scope_id = ?"]
    params: list[Any] = [scope]
    symbol = _get(args, "symbol", default=None)
    if symbol:
        clauses.append("s.symbol = ?")
        params.append(str(symbol).upper())
    signal_id = _get(args, "signal_id", "signalId", default=None)
    if signal_id:
        clauses.append("s.signal_id = ?")
        params.append(str(signal_id))
    action = _get(args, "event_type", "type", "action", default=None)
    if action:
        clauses.append("s.action = ?")
        params.append(str(action))
    for bound, column in (
        (_parse_time(_get(args, "since", default=None), is_end=False), "s.generated_at >= ?"),
        (_parse_time(_get(args, "until", default=None), is_end=True), "s.generated_at <= ?"),
    ):
        if bound is not None:
            clauses.append(column)
            params.append(_to_db(bound))
    strategy = _get(args, "strategy", default=None)
    if strategy:
        clauses.append(
            "EXISTS (SELECT 1 FROM strategy_instances si "
            "WHERE si.account_scope_id = s.account_scope_id "
            "AND si.instance_id = s.instance_id AND si.strategy_key = ?)"
        )
        params.append(str(strategy))
    for extra, sub in (
        (_get(args, "cycle_id", "cycleId", default=None),
         "EXISTS (SELECT 1 FROM order_intents oi WHERE oi.account_scope_id = s.account_scope_id "
         "AND oi.signal_id = s.signal_id AND oi.cycle_id = ?)"),
        (_get(args, "intent_id", "intentId", default=None),
         "EXISTS (SELECT 1 FROM order_intents oi WHERE oi.account_scope_id = s.account_scope_id "
         "AND oi.signal_id = s.signal_id AND oi.intent_id = ?)"),
        (_get(args, "client_order_id", "clientOrderId", default=None),
         "EXISTS (SELECT 1 FROM order_intents oi WHERE oi.account_scope_id = s.account_scope_id "
         "AND oi.signal_id = s.signal_id AND oi.client_order_id = ?)"),
    ):
        if extra:
            clauses.append(sub)
            params.append(str(extra))
    rows = conn.execute(
        "SELECT s.signal_id, s.instance_id, s.symbol, s.action, s.reference_price, "
        "s.reason, s.bar_time, s.generated_at FROM signals s WHERE "
        + " AND ".join(clauses)
        + " ORDER BY s.generated_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    out = []
    for signal_id_v, instance_id, symbol_v, action_v, ref, reason, bar_time, generated_at in rows:
        out.append({
            "signal_id": signal_id_v,
            "symbol": symbol_v,
            "strategy": _strategy_key(conn, scope, instance_id),
            "action": action_v,
            "reference_price": ref,
            "reason": reason,
            "bar_time": bar_time,
            "generated_at": generated_at,
        })
    return out


def _query_evaluations(conn: Any, scope: str, args: Any, limit: int) -> list[dict[str, Any]]:
    clauses = ["e.account_scope_id = ?"]
    params: list[Any] = [scope]
    symbol = _get(args, "symbol", default=None)
    if symbol:
        clauses.append("e.symbol = ?")
        params.append(str(symbol).upper())
    outcome = _get(args, "outcome", "status", "event_type", "type", default=None)
    if outcome:
        clauses.append("e.status = ?")
        params.append(str(outcome))
    reason = _get(args, "rejection_reason", "rejectionReason", "reason", default=None)
    if reason:
        clauses.append("e.reason LIKE ?")
        params.append(f"%{reason}%")
    for bound, column in (
        (_parse_time(_get(args, "since", default=None), is_end=False), "e.evaluated_at >= ?"),
        (_parse_time(_get(args, "until", default=None), is_end=True), "e.evaluated_at <= ?"),
    ):
        if bound is not None:
            clauses.append(column)
            params.append(_to_db(bound))
    strategy = _get(args, "strategy", default=None)
    if strategy:
        clauses.append(
            "EXISTS (SELECT 1 FROM strategy_instances si "
            "WHERE si.account_scope_id = e.account_scope_id "
            "AND si.instance_id = e.instance_id AND si.strategy_key = ?)"
        )
        params.append(str(strategy))
    for extra, sub in (
        (_get(args, "signal_id", "signalId", default=None),
         "EXISTS (SELECT 1 FROM signals s WHERE s.account_scope_id = e.account_scope_id "
         "AND s.instance_id = e.instance_id AND s.symbol = e.symbol AND s.signal_id = ?)"),
        (_get(args, "cycle_id", "cycleId", default=None),
         "EXISTS (SELECT 1 FROM order_intents oi WHERE oi.account_scope_id = e.account_scope_id "
         "AND oi.cycle_id = ?)"),
        (_get(args, "intent_id", "intentId", default=None),
         "EXISTS (SELECT 1 FROM order_intents oi WHERE oi.account_scope_id = e.account_scope_id "
         "AND oi.intent_id = ?)"),
        (_get(args, "client_order_id", "clientOrderId", default=None),
         "EXISTS (SELECT 1 FROM order_intents oi WHERE oi.account_scope_id = e.account_scope_id "
         "AND oi.client_order_id = ?)"),
    ):
        if extra:
            clauses.append(sub)
            params.append(str(extra))
    rows = conn.execute(
        "SELECT e.evaluation_id, e.instance_id, e.symbol, e.status, e.reason, "
        "e.bar_time, e.evaluated_at FROM strategy_evaluations e WHERE "
        + " AND ".join(clauses)
        + " ORDER BY e.evaluated_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    out = []
    for eval_id, instance_id, symbol_v, status, reason_v, bar_time, evaluated_at in rows:
        out.append({
            "evaluation_id": eval_id,
            "symbol": symbol_v,
            "strategy": _strategy_key(conn, scope, instance_id),
            "outcome": status,
            "rejection_reason": reason_v,
            "bar_time": bar_time,
            "evaluated_at": evaluated_at,
        })
    return out


def _query_events(conn: Any, scope: str, args: Any, limit: int) -> list[dict[str, Any]]:
    clauses = ["account_scope_id = ?"]
    params: list[Any] = [scope]
    event_type = _get(args, "event_type", "type", default=None)
    if event_type:
        clauses.append("event_type = ?")
        params.append(str(event_type))
    for bound, column in (
        (_parse_time(_get(args, "since", default=None), is_end=False), "occurred_at >= ?"),
        (_parse_time(_get(args, "until", default=None), is_end=True), "occurred_at <= ?"),
    ):
        if bound is not None:
            clauses.append(column)
            params.append(_to_db(bound))
    for value in (
        _get(args, "run_id", "runId", default=None),
        _get(args, "cycle_id", "cycleId", default=None),
        _get(args, "signal_id", "signalId", default=None),
        _get(args, "intent_id", "intentId", default=None),
        _get(args, "client_order_id", "clientOrderId", default=None),
        _get(args, "symbol", default=None),
        _get(args, "strategy", default=None),
        _get(args, "outcome", default=None),
        _get(args, "rejection_reason", "rejectionReason", "reason", default=None),
    ):
        if value is not None:
            needle = str(value)
            clauses.append(
                "(correlation_id = ? OR causation_id = ? OR stream_id = ? "
                "OR event_id = ? OR payload_json LIKE ?)"
            )
            params.extend([needle, needle, needle, needle, f"%{needle}%"])
    rows = conn.execute(
        "SELECT event_seq, event_id, stream_type, stream_id, event_type, "
        "correlation_id, causation_id, occurred_at, recorded_at "
        "FROM domain_events WHERE " + " AND ".join(clauses)
        + " ORDER BY event_seq DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [
        {
            "event_seq": seq,
            "event_id": event_id,
            "stream": f"{stream_type}/{stream_id}",
            "event_type": event_type,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "occurred_at": occurred_at,
            "recorded_at": recorded_at,
        }
        for seq, event_id, stream_type, stream_id, event_type,
        correlation_id, causation_id, occurred_at, recorded_at in rows
    ]


# ---------------------------------------------------------------------------
# Logs (bounded reverse read of structured JSON log files)
# ---------------------------------------------------------------------------

def _iter_log_files(log_dir: Path) -> list[Path]:
    if not log_dir.is_dir():
        return []
    found: list[Path] = []
    for pattern in LOG_FILE_PATTERNS:
        found.extend(sorted(log_dir.glob(pattern)))
    # Newest files first; fall back to name order.
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(found, key=lambda p: (_mtime(p), p.name), reverse=True)


def _read_logs(log_dir: Path, args: Any, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    max_bytes = _get(args, "max_bytes", "maxBytes", default=LOG_MAX_BYTES)
    try:
        max_bytes = int(max_bytes)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        max_bytes = LOG_MAX_BYTES
    max_bytes = max(1024, min(max_bytes, 4 * 1024 * 1024))
    level = str(_get(args, "level", "log_level", default="") or "").upper() or None
    wanted = {
        key: str(value)
        for key, value in {
            "run_id": _get(args, "run_id", "runId", default=None),
            "cycle_id": _get(args, "cycle_id", "cycleId", default=None),
            "signal_id": _get(args, "signal_id", "signalId", default=None),
            "intent_id": _get(args, "intent_id", "intentId", default=None),
            "client_order_id": _get(args, "client_order_id", "clientOrderId", default=None),
        }.items()
        if value is not None
    }
    collected: list[dict[str, Any]] = []
    bytes_used = 0
    truncated = False
    for path in _iter_log_files(log_dir):
        if len(collected) >= limit or bytes_used >= max_bytes:
            truncated = True
            break
        try:
            size = path.stat().st_size
        except OSError:
            continue
        allowance = max_bytes - bytes_used
        start = max(0, size - allowance)
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                chunk = handle.read(allowance)
        except OSError:
            continue
        if start > 0:
            # Drop a possible partial first line so only whole records parse.
            newline = chunk.find(b"\n")
            chunk = chunk[newline + 1:] if newline != -1 else b""
        bytes_used += len(chunk)
        try:
            text = chunk.decode("utf-8", errors="replace")
        except Exception:
            continue
        lines = [line for line in text.splitlines() if line.strip()]
        for line in reversed(lines):
            if len(collected) >= limit:
                break
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if level and str(record.get("level", "")).upper() != level:
                continue
            if any(str(record.get(key, "")) != value for key, value in wanted.items()):
                continue
            collected.append({
                "timestamp": record.get("timestamp"),
                "level": record.get("level"),
                "logger": record.get("logger"),
                "message": record.get("message"),
                "run_id": record.get("run_id"),
                "cycle_id": record.get("cycle_id"),
                "signal_id": record.get("signal_id"),
                "intent_id": record.get("intent_id"),
                "client_order_id": record.get("client_order_id"),
            })
        if start > 0 and len(collected) < limit:
            truncated = True
    meta = {"truncated": truncated or len(collected) >= limit, "bytes_used": bytes_used}
    return collected, meta


# ---------------------------------------------------------------------------
# Health (independent heartbeat-age computation + alert evaluation)
# ---------------------------------------------------------------------------

def _health_snapshot(conn: Any, scope: str, now: datetime) -> dict[str, Any]:
    data: dict[str, Any] = {}

    def _single(sql: str, params: tuple[Any, ...] = ()) -> Any:
        try:
            row = conn.execute(sql, params).fetchone()
        except sqlite3.Error:
            return None
        return row[0] if row else None

    heartbeat_raw = _single(
        "SELECT heartbeat_at FROM service_health "
        "WHERE account_scope_id = ? AND component = 'engine'",
        (scope,),
    )
    heartbeat_at = None
    if heartbeat_raw is not None:
        try:
            heartbeat_at = from_db_timestamp(str(heartbeat_raw))
        except ValueError:
            heartbeat_at = None
    data["heartbeat_at"] = heartbeat_at.astimezone(UTC).isoformat() if heartbeat_at else None
    data["heartbeat_age_seconds"] = (
        (now - heartbeat_at).total_seconds() if heartbeat_at else -1.0
    )

    latest_bar = _single(
        "SELECT MAX(bar_start) FROM market_bars WHERE account_scope_id = ?", (scope,)
    )
    if latest_bar is not None:
        try:
            bar_at = from_db_timestamp(str(latest_bar))
            data["feed_age_seconds"] = max(0.0, (now - bar_at).total_seconds())
        except ValueError:
            data["feed_age_seconds"] = -1.0
    else:
        data["feed_age_seconds"] = -1.0
    bar_count = _single(
        "SELECT COUNT(*) FROM market_bars WHERE account_scope_id = ?", (scope,)
    )
    data["feed_message_rate_per_sec"] = float(bar_count or 0) / 3600.0

    data["scheduler_lag_seconds"] = 0.0
    sched_raw = _single(
        "SELECT heartbeat_at FROM service_health "
        "WHERE account_scope_id = ? AND component = 'scheduler'",
        (scope,),
    )
    if sched_raw is not None:
        try:
            data["scheduler_lag_seconds"] = max(
                0.0, (now - from_db_timestamp(str(sched_raw))).total_seconds()
            )
        except ValueError:
            pass

    eval_row = None
    try:
        eval_row = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) "
            "FROM strategy_evaluations WHERE account_scope_id = ?",
            (scope,),
        ).fetchone()
    except sqlite3.Error:
        eval_row = None
    data["evaluation_duration_seconds"] = {"count": 0, "sum": 0.0, "avg": 0.0, "max": 0.0}
    data["evaluation_errors_total"] = float((eval_row[1] or 0) if eval_row else 0)

    data["signals_total"] = float(
        _single("SELECT COUNT(*) FROM signals WHERE account_scope_id = ?", (scope,)) or 0
    )
    data["rejections_total"] = float(
        _single(
            "SELECT COUNT(*) FROM decisions WHERE account_scope_id = ? AND outcome = 'REJECTED'",
            (scope,),
        )
        or 0
    )

    data["broker_rest_errors_total"] = 0.0
    data["broker_rest_error_rate"] = 0.0
    data["broker_ws_errors_total"] = 0.0
    data["broker_ws_error_rate"] = 0.0
    try:
        rest_errors = conn.execute(
            "SELECT COUNT(*) FROM broker_order_events WHERE account_scope_id = ? "
            "AND (event_type LIKE '%ERROR%' OR event_type LIKE '%REJECT%')",
            (scope,),
        ).fetchone()[0]
        total_events = conn.execute(
            "SELECT COUNT(*) FROM broker_order_events WHERE account_scope_id = ?",
            (scope,),
        ).fetchone()[0]
        data["broker_rest_errors_total"] = float(rest_errors or 0)
        data["broker_rest_error_rate"] = float(rest_errors or 0) / float(total_events or 1)
    except sqlite3.Error:
        pass

    data["unknown_order_states"] = float(
        _single(
            "SELECT COUNT(*) FROM broker_orders WHERE account_scope_id = ? AND status = 'UNKNOWN'",
            (scope,),
        )
        or 0
    )

    data["position_drift_quantity"] = 0.0
    data["reconciliation_drifts_unresolved"] = 0.0
    data["reconciliation_status_error"] = 0.0
    try:
        unresolved = conn.execute(
            "SELECT COUNT(*) FROM reconciliation_drifts d LEFT JOIN reconciliation_resolutions r "
            "ON r.account_scope_id = d.account_scope_id AND r.drift_id = d.drift_id "
            "WHERE d.account_scope_id = ? AND r.resolution_id IS NULL",
            (scope,),
        ).fetchone()[0]
        data["reconciliation_drifts_unresolved"] = float(unresolved or 0)
        data["position_drift_quantity"] = float(unresolved or 0)
        error_runs = conn.execute(
            "SELECT COUNT(*) FROM reconciliation_runs "
            "WHERE account_scope_id = ? AND status = 'ERROR'",
            (scope,),
        ).fetchone()[0]
        data["reconciliation_status_error"] = float(error_runs or 0)
        last_status = conn.execute(
            "SELECT status FROM reconciliation_runs WHERE account_scope_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (scope,),
        ).fetchone()
        data["reconciliation_last_status"] = last_status[0] if last_status else None
    except sqlite3.Error:
        data["reconciliation_last_status"] = None

    for key, sql in (
        ("stop_placement_failures_total",
         "SELECT COUNT(*) FROM protection_orders "
         "WHERE account_scope_id = ? AND status = 'UNSUPPORTED'"),
        ("orphan_stops",
         "SELECT COUNT(*) FROM protection_orders p LEFT JOIN positions pos "
         "ON pos.account_scope_id = p.account_scope_id AND pos.cycle_id = p.cycle_id "
         "WHERE p.account_scope_id = ? AND pos.symbol IS NULL "
         "AND p.status IN ('PENDING','ACTIVE','REPLACING','UNKNOWN')"),
        ("expired_stops",
         "SELECT COUNT(*) FROM protection_orders "
         "WHERE account_scope_id = ? AND status = 'CANCELED'"),
        ("unconfirmed_stepped_trailing_replacement",
         "SELECT COUNT(*) FROM trailing_stop_states "
         "WHERE account_scope_id = ? AND status = 'REPLACING'"),
        ("uncovered_fills_total",
         "SELECT COUNT(*) FROM fills f LEFT JOIN protection_orders p "
         "ON p.account_scope_id = f.account_scope_id AND p.cycle_id = f.cycle_id "
         "WHERE f.account_scope_id = ? AND p.protection_order_id IS NULL"),
        ("shadow_fills_total", "SELECT COUNT(*) FROM fills WHERE account_scope_id = ?"),
        ("unexpected_shorts",
         "SELECT COUNT(*) FROM positions WHERE account_scope_id = ? AND direction = 'SHORT'"),
        ("live_position_engine_offline",
         "SELECT COUNT(*) FROM positions WHERE account_scope_id = ?"),
    ):
        try:
            data[key] = float(conn.execute(sql, (scope,)).fetchone()[0] or 0)
        except sqlite3.Error:
            data[key] = 0.0

    try:
        stale_marks = conn.execute(
            "SELECT COUNT(*) FROM trailing_stop_states WHERE account_scope_id = ? "
            "AND (last_confirmed_at IS NULL OR last_confirmed_at < ?)",
            (scope, _to_db(now)),
        ).fetchone()[0]
        data["stale_trailing_watermark_age_seconds"] = (
            3600.0 if (stale_marks or 0) > 0 else 0.0
        )
    except sqlite3.Error:
        data["stale_trailing_watermark_age_seconds"] = 0.0

    data["stale_feed"] = 1.0 if float(data.get("feed_age_seconds", 0.0)) > 90.0 else 0.0
    data["risk_breaches_total"] = 0.0
    data["unhandled_exceptions_total"] = 0.0
    try:
        data["risk_breaches_total"] = float(
            conn.execute(
                "SELECT COUNT(*) FROM entry_lockouts "
                "WHERE account_scope_id = ? AND cleared_at IS NULL",
                (scope,),
            ).fetchone()[0] or 0
        )
    except sqlite3.Error:
        pass
    return data


def _run_health(args: Any) -> int:
    output_format = str(_get(args, "format", default="table") or "table")
    try:
        store = _open_store(args)
    except (ReadStoreUnavailableError, OSError, sqlite3.Error) as exc:
        print(f"health: cannot open database: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        scope = _resolve_scope(store, args)
        now = _clock_now(args)
        offline_after = _threshold(args, "heartbeat_offline_seconds", 60.0)
        feed_max_age = _threshold(args, "feed_max_age_seconds", 90.0)
        with store.snapshot(scope) as conn:
            snapshot = _health_snapshot(conn, scope, now)
        heartbeat_age = float(snapshot.get("heartbeat_age_seconds", -1.0))
        if heartbeat_age < 0 or heartbeat_age > offline_after:
            engine_state = "OFFLINE"
        else:
            engine_state = "ONLINE"
        # Live positions while offline are themselves a critical condition.
        if engine_state == "OFFLINE":
            snapshot["live_position_engine_offline"] = float(
                snapshot.get("live_position_engine_offline", 0.0)
            )
        else:
            snapshot["live_position_engine_offline"] = 0.0
        issues = evaluate_alerts(
            snapshot,
            {
                "heartbeat_offline_seconds": offline_after,
                "feed_max_age_seconds": feed_max_age,
            },
        )
        critical = [i for i in issues if i.severity == "critical"]
        report = {
            "account_scope": _mask_account(scope),
            "engine_state": engine_state,
            "heartbeat_age_seconds": heartbeat_age,
            "feed_age_seconds": snapshot.get("feed_age_seconds"),
            "feed_message_rate_per_sec": snapshot.get("feed_message_rate_per_sec"),
            "scheduler_lag_seconds": snapshot.get("scheduler_lag_seconds"),
            "reconciliation_last_status": snapshot.get("reconciliation_last_status"),
            "unresolved_drifts": snapshot.get("reconciliation_drifts_unresolved"),
            "unknown_order_states": snapshot.get("unknown_order_states"),
            "position_drift_quantity": snapshot.get("position_drift_quantity"),
            "issues": [
                {
                    "severity": i.severity,
                    "code": i.code,
                    "message": i.message,
                    "remediation_command": i.remediation_command,
                }
                for i in issues
            ],
        }
        if output_format == "json":
            print(json.dumps(_safe_row(report), sort_keys=True, default=str))
        else:
            print(f"engine_state  {engine_state}")
            print(f"heartbeat_age_seconds  {heartbeat_age:.1f}")
            print(f"feed_age_seconds  {snapshot.get('feed_age_seconds')}")
            print(f"reconciliation  {snapshot.get('reconciliation_last_status')}")
            if not issues:
                print("issues  none")
            for issue in issues:
                print(f"[{issue.severity}] {issue.code}: {issue.message}")
                print(f"  resolve: {issue.remediation_command}")
        return EXIT_UNHEALTHY if (engine_state == "OFFLINE" or critical) else EXIT_OK
    except ValueError as exc:
        print(f"health: {exc}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Argument wiring
# ---------------------------------------------------------------------------

def add_history_arguments(parser: argparse.ArgumentParser, name: str) -> None:
    """Attach bounded-history filter options for command *name*.

    Options already present on *parser* (e.g. ``--account``/``--format`` when
    the global CLI registered them) are left untouched.
    """
    existing = {action.dest for action in parser._actions}

    def _add(*flags: str, **kwargs: Any) -> None:
        dest = kwargs.get("dest") or flags[0].lstrip("-").replace("-", "_")
        if dest in existing:
            return
        parser.add_argument(*flags, **kwargs)
        existing.add(dest)

    _add("--account", default=None, help="Account scope (e.g. SHADOW:default)")
    _add("--scenario", default=None, help="Shadow scenario")
    _add("--symbol", default=None, help="Symbol filter")
    _add("--strategy", default=None, help="Strategy key filter")
    _add("--since", default=None, help="Earliest timestamp (ISO-8601 or NY session date)")
    _add("--until", default=None, help="Latest timestamp (ISO-8601 or NY session date)")
    _add("--event-type", "--type", dest="event_type", default=None,
         help="Event/action/outcome type filter")
    _add("--outcome", default=None, help="Outcome filter (evaluations/decisions)")
    _add("--rejection-reason", "--reason", dest="rejection_reason", default=None,
         help="Rejection reason substring filter")
    _add("--run-id", dest="run_id", default=None, help="Run id filter")
    _add("--cycle-id", dest="cycle_id", default=None, help="Cycle id filter")
    _add("--signal-id", dest="signal_id", default=None, help="Signal id filter")
    _add("--intent-id", dest="intent_id", default=None, help="Intent id filter")
    _add("--client-order-id", dest="client_order_id", default=None,
         help="Client order id filter")
    _add("--limit", type=int, default=HISTORY_DEFAULT_LIMIT,
         help=f"Max rows (default {HISTORY_DEFAULT_LIMIT}, max {HISTORY_MAX_LIMIT})")
    _add("--format", choices=("table", "json"), default="table")
    if name == "logs":
        _add("--level", default=None, help="Log level filter (e.g. ERROR)")
        _add("--max-bytes", dest="max_bytes", type=int, default=LOG_MAX_BYTES,
             help="Byte cap for log reads")
    if name in {"signals", "evaluations", "events", "logs", "health"}:
        _add("--db-path", dest="db_path", default=None, help="SQLite database path")
        _add("--log-dir", dest="log_dir", default=None, help="Structured log directory")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _run_bounded_query(args: Any, kind: str) -> int:
    output_format = str(_get(args, "format", default="table") or "table")
    limit, clamped = _clamp_limit(_get(args, "limit", default=HISTORY_DEFAULT_LIMIT))
    try:
        store = _open_store(args)
    except (ReadStoreUnavailableError, OSError, sqlite3.Error) as exc:
        print(f"{kind}: cannot open database: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        scope = _resolve_scope(store, args)
        with store.snapshot(scope) as conn:
            if kind == "signals":
                rows = _query_signals(conn, scope, args, limit)
            elif kind == "evaluations":
                rows = _query_evaluations(conn, scope, args, limit)
            else:
                rows = _query_events(conn, scope, args, limit)
        _emit(rows, output_format, clamped_note=clamped)
        return EXIT_OK
    except ValueError as exc:
        print(f"{kind}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        store.close()


def _run_logs(args: Any) -> int:
    output_format = str(_get(args, "format", default="table") or "table")
    limit, clamped = _clamp_limit(_get(args, "limit", default=HISTORY_DEFAULT_LIMIT))
    log_dir = _resolve_log_dir(args)
    rows, meta = _read_logs(log_dir, args, limit)
    safe = [_safe_row(row) for row in rows]
    if output_format == "json":
        payload: dict[str, Any] = {"rows": safe, "truncated": meta["truncated"]}
        if clamped:
            payload["limit_clamped_to"] = HISTORY_MAX_LIMIT
        print(json.dumps(payload, sort_keys=True, default=str))
        return EXIT_OK
    if clamped:
        print(f"(limit clamped to {HISTORY_MAX_LIMIT})")
    if not safe:
        print("(no data)")
        return EXIT_OK
    for row in safe:
        print(
            f"{row.get('timestamp')} {row.get('level')} "
            f"{row.get('logger')}: {row.get('message')}"
        )
    if meta["truncated"]:
        print("(truncated: bounded log window)")
    return EXIT_OK


def run_history_command(name: str, args: argparse.Namespace) -> int:
    """Run a bounded read-only history/health command; returns an exit code."""
    if name in {"signals", "evaluations", "events"}:
        return _run_bounded_query(args, name)
    if name == "logs":
        return _run_logs(args)
    if name == "health":
        return _run_health(args)
    print(f"Unknown history command: {name}", file=sys.stderr)
    return EXIT_USAGE
