"""Stable versioned CSV/JSON export of strategy performance report rows.

Every report type has a fixed documented column order (``REPORT_SCHEMAS``)
and deterministic row sorting.  Decimal values are serialized as strings
with no float conversion, timestamps as UTC ISO-8601, and account
identifiers are masked.  Only schema columns are emitted, so secrets or raw
provider payloads can never leak through.  File writes are atomic (temp
file plus rename) and refuse to overwrite an existing path unless
``overwrite=True``.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from mike_trade.reporting.metrics import PERFORMANCE_DISCLAIMER

__all__ = ["REPORT_SCHEMAS", "SCHEMA_VERSION", "export_csv", "export_json"]

SCHEMA_VERSION = "1"

_REPORT_TYPES = (
    "cycles",
    "strategy summary",
    "pair summary",
    "symbol summary",
    "signals/evaluations",
    "rejections",
    "trailing updates",
    "eligibility decisions",
    "reconciliation drift",
    "comparison",
)


def _columns(*names: str) -> tuple[str, ...]:
    return tuple(names)


_SUMMARY_STATS = _columns(
    "trade_count", "wins", "losses", "win_rate", "avg_win", "avg_loss",
    "profit_factor", "expectancy", "total_gross", "total_net",
    "avg_holding_duration_seconds", "avg_signal_to_submit_seconds",
    "avg_submit_to_first_fill_seconds", "avg_first_to_final_fill_seconds",
    "avg_slippage_dollars", "avg_slippage_bps",
)

REPORT_SCHEMAS: dict[str, dict[str, Any]] = {
    "cycles": {
        "schema_version": SCHEMA_VERSION,
        "columns": _columns(
            "cycle_id", "account_scope_id", "symbol", "buy_strategy",
            "sell_strategy", "exit_cause", "pair", "entry_avg_price",
            "exit_avg_price", "filled_quantity", "gross_pnl", "fee_confirmed",
            "fee_estimated", "fee_status", "fees_complete", "net_pnl",
            "opened_at", "closed_at", "holding_duration_seconds",
            "signal_time", "signal_bar_time", "submit_time", "first_fill_time",
            "final_fill_time", "latency_signal_to_submit_seconds",
            "latency_submit_to_first_fill_seconds",
            "latency_first_to_final_fill_seconds", "signal_reference_price",
            "slippage_dollars", "slippage_bps", "trailing_update_count",
            "last_confirmed_stop", "entry_fill_count", "exit_fill_count",
        ),
        "sort_keys": ("symbol", "cycle_id"),
        "masked": ("account_scope_id",),
    },
    "strategy summary": {
        "schema_version": SCHEMA_VERSION,
        "columns": ("strategy",) + _SUMMARY_STATS,
        "sort_keys": ("strategy",),
        "masked": (),
    },
    "pair summary": {
        "schema_version": SCHEMA_VERSION,
        "columns": ("pair",) + _SUMMARY_STATS,
        "sort_keys": ("pair",),
        "masked": (),
    },
    "symbol summary": {
        "schema_version": SCHEMA_VERSION,
        "columns": ("symbol",) + _SUMMARY_STATS,
        "sort_keys": ("symbol",),
        "masked": (),
    },
    "signals/evaluations": {
        "schema_version": SCHEMA_VERSION,
        "columns": _columns("symbol", "status", "count"),
        "sort_keys": ("symbol", "status"),
        "masked": (),
    },
    "rejections": {
        "schema_version": SCHEMA_VERSION,
        "columns": _columns("reason", "count"),
        "sort_keys": ("reason",),
        "masked": (),
    },
    "trailing updates": {
        "schema_version": SCHEMA_VERSION,
        "columns": _columns("cycle_id", "symbol", "update_count", "last_confirmed_stop"),
        "sort_keys": ("cycle_id",),
        "masked": (),
    },
    "eligibility decisions": {
        "schema_version": SCHEMA_VERSION,
        "columns": _columns("decision", "symbol", "count"),
        "sort_keys": ("decision", "symbol"),
        "masked": (),
    },
    "reconciliation drift": {
        "schema_version": SCHEMA_VERSION,
        "columns": _columns("drift_id", "drift_type", "entity_ref", "detected_at"),
        "sort_keys": ("detected_at", "drift_id"),
        "masked": (),
    },
    "comparison": {
        "schema_version": SCHEMA_VERSION,
        "columns": _columns(
            "match", "symbol", "pair", "signal_bar_time", "cycle_a", "cycle_b",
            "entry_price_a", "entry_price_b", "entry_price_diff",
            "exit_price_a", "exit_price_b", "exit_price_diff",
            "net_a", "net_b", "net_diff",
        ),
        "sort_keys": ("symbol", "pair", "signal_bar_time", "cycle_a", "cycle_b"),
        "masked": (),
    },
}

_MASKED_KEYS = frozenset({"account_scope_id", "scope_a", "scope_b", "account"})


def mask_account(value: Any) -> Any:
    """Mask an account identifier, keeping only the last four characters."""
    if value is None:
        return None
    text = str(value)
    if len(text) <= 4:
        return "****"
    return "*" * (len(text) - 4) + text[-4:]


def _decimal_seconds(value: timedelta) -> str:
    total = (
        Decimal(value.days * 86400 + value.seconds)
        + Decimal(value.microseconds) / Decimal(1_000_000)
    )
    return str(total)


def _to_text(value: Any, *, masked: bool) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "true" if value else "false"
        return text
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal values cannot be exported")
        return str(value)
    if isinstance(value, float):
        raise ValueError("float values cannot be exported; use Decimal")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetimes cannot be exported; use UTC-aware values")
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, timedelta):
        return _decimal_seconds(value)
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if masked:
        masked_value = mask_account(text)
        return "" if masked_value is None else str(masked_value)
    return text


def _normalize_row(
    row: Mapping[str, Any] | Any, columns: tuple[str, ...], masked: frozenset[str]
) -> dict[str, str]:
    if hasattr(row, "to_row") and callable(getattr(row, "to_row")):
        mapping: Mapping[str, Any] = row.to_row()
    elif isinstance(row, Mapping):
        mapping = row
    else:
        raise ValueError(f"report rows must be mappings, got {type(row).__name__}")
    normalized: dict[str, str] = {}
    for column in columns:
        normalized[column] = _to_text(
            mapping.get(column), masked=(column in masked or column in _MASKED_KEYS)
        )
    return normalized


def _sorted_rows(
    rows: list[dict[str, str]], sort_keys: tuple[str, ...]
) -> list[dict[str, str]]:
    return sorted(rows, key=lambda r: tuple(r.get(key, "") for key in sort_keys))


def _prepare(
    rows: Iterable[Mapping[str, Any] | Any], report_type: str
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if report_type not in REPORT_SCHEMAS:
        raise ValueError(
            f"unknown report_type {report_type!r}; expected one of {sorted(REPORT_SCHEMAS)}"
        )
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Iterable):
        raise ValueError("rows must be an iterable of mappings")
    materialized = list(rows)
    schema = REPORT_SCHEMAS[report_type]
    columns = schema["columns"]
    masked = frozenset(schema.get("masked", ()))
    normalized = [_normalize_row(row, columns, masked) for row in materialized]
    return schema, _sorted_rows(normalized, schema["sort_keys"])


def _write_atomic(path: str | Path, content: str, *, overwrite: bool) -> Path:
    target = Path(path).expanduser()
    if not overwrite and target.exists():
        raise FileExistsError(f"refusing to overwrite existing file: {target}")
    if target.parent != Path(".") and str(target.parent):
        target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.parent / f".{target.name}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, target)
    return target


def export_csv(
    rows: Iterable[Mapping[str, Any] | Any],
    report_type: str,
    path: str | Path | None = None,
    *,
    overwrite: bool = False,
    to_stdout: bool = False,
) -> str:
    """Export report rows as CSV with schema_version first, then fixed columns.

    Returns the CSV text in all cases.  Writes atomically to ``path`` unless
    ``to_stdout`` is set (content goes to stdout and no file is written) or
    ``path`` is None (no file is written).
    """
    schema, normalized = _prepare(rows, report_type)
    columns = schema["columns"]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["schema_version", *columns])
    for row in normalized:
        writer.writerow([schema["schema_version"], *(row[c] for c in columns)])
    content = buffer.getvalue()
    if to_stdout:
        sys.stdout.write(content)
        return content
    if path is not None:
        _write_atomic(path, content, overwrite=overwrite)
    return content


def export_json(
    rows: Iterable[Mapping[str, Any] | Any],
    report_type: str,
    path: str | Path | None = None,
    *,
    overwrite: bool = False,
    to_stdout: bool = False,
    context: Mapping[str, Any] | None = None,
) -> str:
    """Export report rows as versioned JSON with sample size and disclaimer.

    ``context`` carries envelope-only fields (for example masked ``scope_a``
    /``scope_b`` for comparisons); only allowlisted scalar keys survive and
    account identifiers in it are masked.  Returns the JSON text in all cases.
    """
    schema, normalized = _prepare(rows, report_type)
    envelope: dict[str, Any] = {
        "schema_version": schema["schema_version"],
        "report_type": report_type,
        "sample_size": len(normalized),
        "performance_disclaimer": PERFORMANCE_DISCLAIMER,
    }
    if context is not None:
        if not isinstance(context, Mapping):
            raise ValueError("context must be a mapping")
        for key, value in context.items():
            if not isinstance(key, str) or not key:
                raise ValueError("context keys must be non-empty strings")
            if isinstance(value, (Decimal, float)):
                raise ValueError("context values must be plain scalars, not numerics")
            if isinstance(value, datetime):
                text: Any = _to_text(value, masked=False)
            elif key in _MASKED_KEYS:
                text = mask_account("" if value is None else str(value))
            elif value is None or isinstance(value, (str, int, bool)):
                text = value
            else:
                raise ValueError(f"context value for {key!r} must be a plain scalar")
            envelope[key] = text
    envelope["rows"] = normalized
    content = json.dumps(envelope, indent=2, sort_keys=True) + "\n"
    if to_stdout:
        sys.stdout.write(content)
        return content
    if path is not None:
        _write_atomic(path, content, overwrite=overwrite)
    return content
