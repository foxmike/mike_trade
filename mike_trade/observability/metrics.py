"""In-process metrics registry, log correlation context, redaction and alerts.

Public API (preserved additions only -- this module is new in phase S):

* :class:`MetricsRegistry` -- counters, gauges and histograms for feed,
  scheduler, evaluation, signal, broker, order, position, stop, risk and
  heartbeat health signals.
* :class:`HealthIssue` -- typed alert record (severity, code, message,
  remediation command).
* :func:`evaluate_alerts` -- pure snapshot -> issues evaluator.
* :func:`bind_log_context` -- binds run/cycle/signal/intent/order ids into
  subsequent structured log records.
* :class:`RedactionFilter` -- logging filter masking secrets, masking account
  identifiers to their last 4 characters, allowlisting provider payloads and
  injecting bound correlation ids.
"""

from __future__ import annotations

import contextvars
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MetricsRegistry",
    "HealthIssue",
    "evaluate_alerts",
    "bind_log_context",
    "RedactionFilter",
]

# ---------------------------------------------------------------------------
# Metric name catalogue
# ---------------------------------------------------------------------------

COUNTER_NAMES = (
    "signals_total",
    "rejections_total",
    "broker_rest_errors_total",
    "broker_ws_errors_total",
    "shadow_fills_total",
    "uncovered_fills_total",
    "stop_placement_failures_total",
    "risk_breaches_total",
    "unhandled_exceptions_total",
)

GAUGE_NAMES = (
    "feed_age_seconds",
    "feed_message_rate_per_sec",
    "scheduler_lag_seconds",
    "broker_rest_error_rate",
    "broker_ws_error_rate",
    "unknown_order_states",
    "position_drift_quantity",
    "realized_pnl",
    "unrealized_pnl",
    "heartbeat_age_seconds",
    "orphan_stops",
    "expired_stops",
    "unconfirmed_stepped_trailing_replacement",
    "stale_trailing_watermark_age_seconds",
    "live_position_engine_offline",
    "unexpected_shorts",
    "stale_feed",
)

HISTOGRAM_NAMES = (
    "evaluation_duration_seconds",
)

# Every name the registry always reports (zero-initialised).
ALL_METRIC_NAMES = COUNTER_NAMES + GAUGE_NAMES + HISTOGRAM_NAMES


class MetricsRegistry:
    """Trivial in-process metrics registry (counters, gauges, histograms)."""

    def __init__(self) -> None:
        self._counters: dict[str, float] = {name: 0.0 for name in COUNTER_NAMES}
        self._gauges: dict[str, float] = {name: 0.0 for name in GAUGE_NAMES}
        self._histograms: dict[str, list[float]] = {
            name: [] for name in HISTOGRAM_NAMES
        }

    # -- counters ---------------------------------------------------------
    def inc(self, name: str, amount: float = 1.0) -> float:
        """Increment a counter (auto-registers unknown names)."""
        if not isinstance(amount, (int, float)) or isinstance(amount, bool):
            raise TypeError("amount must be numeric")
        current = float(self._counters.get(name, 0.0)) + float(amount)
        self._counters[name] = current
        return current

    def counter(self, name: str) -> float:
        return float(self._counters.get(name, 0.0))

    # -- gauges -----------------------------------------------------------
    def set_gauge(self, name: str, value: float) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError("gauge value must be numeric")
        self._gauges[name] = float(value)

    def gauge(self, name: str) -> float:
        return float(self._gauges.get(name, 0.0))

    # -- histograms -------------------------------------------------------
    def observe(self, name: str, value: float) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError("observation must be numeric")
        self._histograms.setdefault(name, []).append(float(value))

    def histogram(self, name: str) -> tuple[float, ...]:
        return tuple(self._histograms.get(name, ()))

    def histogram_stats(self, name: str) -> dict[str, Any]:
        values = self._histograms.get(name, [])
        if not values:
            return {"count": 0, "sum": 0.0, "avg": 0.0, "max": 0.0}
        ordered = sorted(values)
        return {
            "count": len(ordered),
            "sum": float(sum(ordered)),
            "avg": float(sum(ordered) / len(ordered)),
            "max": float(ordered[-1]),
        }

    # -- snapshots --------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Return a plain snapshot covering every catalogued metric."""
        data: dict[str, Any] = {}
        for name in COUNTER_NAMES:
            data[name] = float(self._counters.get(name, 0.0))
        for name in GAUGE_NAMES:
            data[name] = float(self._gauges.get(name, 0.0))
        for name in HISTOGRAM_NAMES:
            data[name] = self.histogram_stats(name)
        for name, value in self._counters.items():
            if name not in data:
                data[name] = float(value)
        for name, value in self._gauges.items():
            if name not in data:
                data[name] = float(value)
        return data

    def reset(self) -> None:
        for name in self._counters:
            self._counters[name] = 0.0
        for name in self._gauges:
            self._gauges[name] = 0.0
        for name in self._histograms:
            self._histograms[name] = []


# ---------------------------------------------------------------------------
# Correlation context
# ---------------------------------------------------------------------------

_CORRELATION_KEYS = (
    "run_id",
    "cycle_id",
    "signal_id",
    "intent_id",
    "client_order_id",
)

_log_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "mike_trade_log_context", default=None
)


def bind_log_context(
    run_id: str | None = None,
    cycle_id: str | None = None,
    signal_id: str | None = None,
    intent_id: str | None = None,
    client_order_id: str | None = None,
) -> dict[str, Any]:
    """Bind correlation ids for subsequent structured log records.

    Passing ``None`` leaves the current value for that key unchanged; pass an
    empty string to clear a key.  Returns the effective context mapping.
    """
    current = dict(_log_context.get() or {})
    updates = {
        "run_id": run_id,
        "cycle_id": cycle_id,
        "signal_id": signal_id,
        "intent_id": intent_id,
        "client_order_id": client_order_id,
    }
    for key, value in updates.items():
        if value is None:
            continue
        if value == "":
            current.pop(key, None)
        else:
            current[key] = str(value)
    _log_context.set(current)
    return dict(current)


def _current_log_context() -> dict[str, Any]:
    try:
        return dict(_log_context.get() or {})
    except LookupError:
        return {}


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_SECRET_PATTERN = re.compile(
    r"(api_key|apikey|api-secret|secret|passwd|password|token|authorization"
    r"|bearer|private_key|key_id|access_key|session_key)",
    re.IGNORECASE,
)

_ACCOUNT_KEYS = frozenset(
    {
        "account",
        "account_id",
        "account_scope_id",
        "broker_account_ref",
        "broker_account_id",
        "account_number",
    }
)

# Raw provider payload containers: only allowlisted sub-keys survive.
_PROVIDER_PAYLOAD_KEYS = frozenset(
    {
        "provider_payload",
        "raw_payload",
        "raw_json",
        "provider_response",
        "provider_request",
        "raw_provider_payload",
        "payload",
    }
)

PROVIDER_ALLOWLIST = frozenset(
    {
        "symbol",
        "side",
        "quantity",
        "price",
        "status",
        "order_id",
        "client_order_id",
        "order_type",
        "time_in_force",
        "filled_quantity",
        "event_type",
    }
)


def _mask_account(value: Any) -> str:
    text = str(value)
    if len(text) <= 4:
        return "****"
    return "****" + text[-4:]


def _redact_key_value(key: str, value: Any) -> Any:
    low = key.lower()
    if _SECRET_PATTERN.search(key):
        return "***REDACTED***"
    if low in _ACCOUNT_KEYS:
        return _mask_account(value)
    if low in _PROVIDER_PAYLOAD_KEYS:
        if isinstance(value, Mapping):
            return {
                str(k): v for k, v in value.items() if str(k) in PROVIDER_ALLOWLIST
            }
        return {}
    if isinstance(value, Mapping):
        return {
            str(k): _redact_key_value(str(k), v) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_key_value(key, item) for item in value]
    return value


class RedactionFilter(logging.Filter):
    """Mask secrets/tokens, mask account ids to last 4, allowlist payloads.

    Also injects bound correlation ids (see :func:`bind_log_context`) into
    each record so structured formatters emit them.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        context = _current_log_context()
        for key in _CORRELATION_KEYS:
            if key in context and not hasattr(record, key):
                setattr(record, key, context[key])
        for key in list(record.__dict__.keys()):
            if key.startswith("_"):
                continue
            if key in {
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "message", "module",
                "msecs", "msg", "name", "pathname", "process", "processName",
                "relativeCreated", "stack_info", "thread", "threadName", "taskName",
            }:
                continue
            try:
                record.__dict__[key] = _redact_key_value(key, record.__dict__[key])
            except Exception:
                record.__dict__[key] = "***REDACTED***"
        # Scrub a rendered message that accidentally embeds a secret value.
        try:
            message = str(record.getMessage())
            if _SECRET_PATTERN.search(message):
                record.msg = "***REDACTED***"
                record.args = ()
        except Exception:
            pass
        return True


# ---------------------------------------------------------------------------
# Health issues + pure alert evaluators
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class HealthIssue:
    """One typed alert derived from a metrics/projection snapshot."""

    severity: str  # "critical" | "warning"
    code: str
    message: str
    remediation_command: str

    def __post_init__(self) -> None:
        if self.severity not in ("critical", "warning"):
            raise ValueError("severity must be 'critical' or 'warning'")
        if not self.code or not self.code.strip():
            raise ValueError("code must be a non-empty string")
        if not self.message or not self.message.strip():
            raise ValueError("message must be a non-empty string")
        if not self.remediation_command or not self.remediation_command.strip():
            raise ValueError("remediation_command must be a non-empty string")

    @property
    def remediation(self) -> str:
        """Alias for ``remediation_command``."""
        return self.remediation_command


_DEFAULT_THRESHOLDS: dict[str, float] = {
    "heartbeat_offline_seconds": 60.0,
    "feed_max_age_seconds": 90.0,
    "feed_min_rate_per_sec": 0.0001,
    "scheduler_lag_seconds": 30.0,
    "evaluation_max_seconds": 10.0,
    "broker_error_rate": 0.05,
    "position_drift_quantity": 0.0,
}


def _number(snapshot: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    try:
        value = snapshot.get(key, default)
    except AttributeError:
        return float(default)
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Mapping):
        for sub in ("max", "avg", "value"):
            candidate = value.get(sub)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                return float(candidate)
        return float(default)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float(default)


def _thresholds(overrides: Mapping[str, Any] | None) -> dict[str, float]:
    merged = dict(_DEFAULT_THRESHOLDS)
    if overrides:
        for key, value in overrides.items():
            try:
                merged[str(key)] = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
    return merged


def evaluate_alerts(
    snapshot: Mapping[str, Any] | None,
    thresholds: Mapping[str, Any] | None = None,
) -> list[HealthIssue]:
    """Turn a metrics/projection snapshot into typed :class:`HealthIssue`s.

    Pure function of its inputs: no I/O, no clock reads, no mutation.  Every
    condition named by the phase-S contract yields a distinct issue code with
    severity and a CLI/Make remediation command.
    """
    data: Mapping[str, Any] = snapshot or {}
    limits = _thresholds(thresholds)
    issues: list[HealthIssue] = []

    def add(severity: str, code: str, message: str, remediation: str) -> None:
        issues.append(
            HealthIssue(
                severity=severity,
                code=code,
                message=message,
                remediation_command=remediation,
            )
        )

    heartbeat_age = _number(data, "heartbeat_age_seconds", -1.0)
    if heartbeat_age < 0 or heartbeat_age > limits["heartbeat_offline_seconds"]:
        add(
            "critical", "heartbeat_offline",
            f"Engine heartbeat is absent or stale (age {heartbeat_age:.1f}s).",
            "mike-trade health --account SHADOW:default",
        )

    feed_age = _number(data, "feed_age_seconds", 0.0)
    if feed_age > limits["feed_max_age_seconds"]:
        add(
            "critical", "stale_feed",
            f"Market feed is stale (age {feed_age:.1f}s).",
            "mike-trade data-check --account SHADOW:default",
        )
    feed_rate = _number(data, "feed_message_rate_per_sec", -1.0)
    if 0 <= feed_rate < limits["feed_min_rate_per_sec"]:
        add(
            "warning", "feed_rate_low",
            f"Market feed message rate is low ({feed_rate:.4f}/s).",
            "mike-trade data-check --account SHADOW:default",
        )

    scheduler_lag = _number(data, "scheduler_lag_seconds", 0.0)
    if scheduler_lag > limits["scheduler_lag_seconds"]:
        add(
            "warning", "scheduler_lag",
            f"Scheduler lag is high ({scheduler_lag:.1f}s).",
            "mike-trade status --account SHADOW:default",
        )

    eval_max = _number(data, "evaluation_duration_seconds", 0.0)
    if eval_max > limits["evaluation_max_seconds"]:
        add(
            "warning", "evaluation_slow",
            f"Strategy evaluation is slow (max {eval_max:.2f}s).",
            "mike-trade strategy-check --account SHADOW:default",
        )
    if _number(data, "evaluation_errors_total", 0.0) > 0:
        add(
            "warning", "evaluation_errors",
            "Strategy evaluations have errored.",
            "mike-trade evaluations --account SHADOW:default --outcome ERROR",
        )

    if _number(data, "broker_rest_errors_total", 0.0) > 0 or _number(
        data, "broker_rest_error_rate", 0.0
    ) > limits["broker_error_rate"]:
        add(
            "critical", "broker_rest_errors",
            "Broker REST errors exceed the error-rate budget.",
            "mike-trade broker-check --account SHADOW:default",
        )
    if _number(data, "broker_ws_errors_total", 0.0) > 0 or _number(
        data, "broker_ws_error_rate", 0.0
    ) > limits["broker_error_rate"]:
        add(
            "critical", "broker_ws_errors",
            "Broker websocket errors exceed the error-rate budget.",
            "mike-trade broker-check --account SHADOW:default",
        )

    if _number(data, "unknown_order_states", 0.0) > 0:
        add(
            "critical", "unknown_order_states",
            "Orders are in UNKNOWN state.",
            "mike-trade orders --account SHADOW:default",
        )

    if _number(data, "position_drift_quantity", 0.0) > limits["position_drift_quantity"]:
        add(
            "critical", "position_drift",
            "Broker/engine position drift is unresolved.",
            "mike-trade reconcile --account SHADOW:default",
        )

    if _number(data, "reconciliation_drifts_unresolved", 0.0) > 0 or _number(
        data, "reconciliation_status_error", 0.0
    ) > 0:
        add(
            "critical", "reconciliation_drift",
            "Reconciliation reports unresolved drift or error.",
            "mike-trade reconcile --account SHADOW:default",
        )

    if _number(data, "shadow_fills_total", 0.0) < 0:  # pragma: no cover - guard
        add(
            "warning", "shadow_fills",
            "Shadow fill ledger is inconsistent.",
            "mike-trade report-shadow --account SHADOW:default",
        )
    if _number(data, "shadow_fill_gap", 0.0) > 0:
        add(
            "warning", "shadow_fills",
            "Shadow fill ledger has gaps.",
            "mike-trade report-shadow --account SHADOW:default",
        )

    if _number(data, "stop_placement_failures_total", 0.0) > 0:
        add(
            "critical", "stop_placement_failures",
            "Protective stop placements have failed.",
            "mike-trade stop-status --account SHADOW:default",
        )
    if _number(data, "orphan_stops", 0.0) > 0:
        add(
            "warning", "orphan_stops",
            "Orphan protective stops exist without an open position.",
            "mike-trade stops-audit --account SHADOW:default",
        )
    if _number(data, "expired_stops", 0.0) > 0:
        add(
            "warning", "expired_stops",
            "Expired protective stops need re-issue or release.",
            "mike-trade stops-audit --account SHADOW:default",
        )
    if _number(data, "unconfirmed_stepped_trailing_replacement", 0.0) > 0:
        add(
            "warning", "unconfirmed_stepped_trailing_replacement",
            "Stepped trailing replacement is unconfirmed.",
            "mike-trade trail-status --account SHADOW:default",
        )
    if _number(data, "stale_trailing_watermark_age_seconds", 0.0) > limits[
        "feed_max_age_seconds"
    ]:
        add(
            "warning", "stale_trailing_watermark",
            "Trailing high-water mark is stale.",
            "mike-trade trail-reconcile --account SHADOW:default",
        )
    if _number(data, "uncovered_fills_total", 0.0) > 0:
        add(
            "critical", "uncovered_fills",
            "Fills exist without protective coverage.",
            "mike-trade stops-audit --account SHADOW:default",
        )

    if _number(data, "live_position_engine_offline", 0.0) > 0:
        add(
            "critical", "live_position_engine_offline",
            "Live position exists while the engine is offline.",
            "mike-trade status --account SHADOW:default",
        )
    if _number(data, "unexpected_shorts", 0.0) > 0:
        add(
            "critical", "unexpected_short",
            "Unexpected short position in a long-only book.",
            "mike-trade positions --account SHADOW:default",
        )
    if _number(data, "risk_breaches_total", 0.0) > 0:
        add(
            "critical", "risk_breach",
            "Risk limits have been breached.",
            "mike-trade budget --account SHADOW:default",
        )
    if _number(data, "unhandled_exceptions_total", 0.0) > 0:
        add(
            "critical", "unhandled_exception",
            "Unhandled exceptions were recorded.",
            "mike-trade logs --account SHADOW:default --level ERROR",
        )

    if _number(data, "signals_total", 0.0) < 0:  # pragma: no cover - guard
        add(
            "warning", "signal_counts",
            "Signal counters are inconsistent.",
            "mike-trade signals --account SHADOW:default",
        )
    if _number(data, "rejections_total", 0.0) > 0 and _number(
        data, "alert_on_rejections", 0.0
    ) > 0:
        add(
            "warning", "signal_rejections",
            "Signals are being rejected.",
            "mike-trade evaluations --account SHADOW:default",
        )

    return issues
