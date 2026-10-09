"""Phase-S history/health command tests: bounded, read-only, filterable."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from mike_trade.controls import history_commands
from mike_trade.controls.history_commands import (
    HISTORY_DEFAULT_LIMIT,
    HISTORY_MAX_LIMIT,
    add_history_arguments,
    run_history_command,
)
from mike_trade.observability.metrics import (
    HealthIssue,
    MetricsRegistry,
    RedactionFilter,
    bind_log_context,
    evaluate_alerts,
)
from mike_trade.persistence.readstore import ReadOnlyViolationError, ReadStore
from mike_trade.persistence.schema import initialize_database, to_db_timestamp

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
SCOPE_A = "shadow:acct-a:baseline"
SCOPE_B = "shadow:acct-b:baseline"


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _stamp(when: datetime) -> str:
    return to_db_timestamp(when)


def _seed_db(path: Path) -> None:
    conn = initialize_database(path, "SHADOW", now=NOW)
    try:
        for scope in (SCOPE_A, SCOPE_B):
            conn.execute(
                "INSERT INTO account_scopes(account_scope_id, environment, broker, "
                "scenario_name, created_at) VALUES (?, 'SHADOW', 'sim', 'baseline', ?)",
                (scope, _stamp(NOW)),
            )
        # Strategy instances (one per scope/symbol/strategy).
        conn.execute(
            "INSERT INTO strategy_instances(instance_id, account_scope_id, strategy_key, "
            "strategy_version, role, symbol, parameters_json, enabled, created_at) "
            "VALUES ('inst-a-spy', ?, 'vwap_bounce_default', '1', 'BUY', 'SPY', '{}', 1, ?)",
            (SCOPE_A, _stamp(NOW)),
        )
        conn.execute(
            "INSERT INTO strategy_instances(instance_id, account_scope_id, strategy_key, "
            "strategy_version, role, symbol, parameters_json, enabled, created_at) "
            "VALUES ('inst-a-aapl', ?, 'vwap_breakdown_default', '1', 'BUY', 'AAPL', '{}', 1, ?)",
            (SCOPE_A, _stamp(NOW)),
        )
        conn.execute(
            "INSERT INTO strategy_instances(instance_id, account_scope_id, strategy_key, "
            "strategy_version, role, symbol, parameters_json, enabled, created_at) "
            "VALUES ('inst-b-spy', ?, 'vwap_bounce_default', '1', 'BUY', 'SPY', '{}', 1, ?)",
            (SCOPE_B, _stamp(NOW)),
        )
        # Cycles for intent linkage.
        for scope, cycle in ((SCOPE_A, "cyc-a1"), (SCOPE_B, "cyc-b1")):
            conn.execute(
                "INSERT INTO position_cycles(cycle_id, account_scope_id, symbol, direction, "
                "state, created_at, updated_at) VALUES (?, ?, 'SPY', 'LONG', 'OPEN', ?, ?)",
                (cycle, scope, _stamp(NOW), _stamp(NOW)),
            )
        # Signals in scope A.
        signals = [
            ("sig-a1", "inst-a-spy", "SPY", "ENTER_LONG", "100.10", "setup-a",
             NOW - timedelta(hours=3)),
            ("sig-a2", "inst-a-aapl", "AAPL", "EXIT_LONG", "200.20", "setup-b",
             NOW - timedelta(hours=1)),
        ]
        for signal_id, inst, symbol, action, ref, reason, when in signals:
            conn.execute(
                "INSERT INTO signals(signal_id, account_scope_id, instance_id, symbol, action, "
                "reference_price, reason, provenance_json, bar_time, generated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)",
                (signal_id, SCOPE_A, inst, symbol, action, ref, reason,
                 _stamp(when), _stamp(when)),
            )
        conn.execute(
            "INSERT INTO signals(signal_id, account_scope_id, instance_id, symbol, action, "
            "reference_price, reason, provenance_json, bar_time, generated_at) "
            "VALUES ('sig-b1', ?, 'inst-b-spy', 'SPY', 'ENTER_LONG', '300.30', 'setup-c', "
            "'{}', ?, ?)",
            (SCOPE_B, _stamp(NOW - timedelta(hours=2)), _stamp(NOW - timedelta(hours=2))),
        )
        # Evaluations in scope A (one NO_SIGNAL, one SIGNAL).
        conn.execute(
            "INSERT INTO strategy_evaluations(evaluation_id, account_scope_id, instance_id, "
            "symbol, status, reason, evaluated_at) VALUES ('eval-a1', ?, 'inst-a-spy', 'SPY', "
            "'NO_SIGNAL', 'no-cross', ?)",
            (SCOPE_A, _stamp(NOW - timedelta(hours=3))),
        )
        conn.execute(
            "INSERT INTO strategy_evaluations(evaluation_id, account_scope_id, instance_id, "
            "symbol, status, reason, evaluated_at) VALUES ('eval-a2', ?, 'inst-a-aapl', 'AAPL', "
            "'SIGNAL', 'cross-up', ?)",
            (SCOPE_A, _stamp(NOW - timedelta(hours=1))),
        )
        # Intent linking sig-a1 -> cycle/intent/client order.
        conn.execute(
            "INSERT INTO order_intents(intent_id, account_scope_id, cycle_id, signal_id, "
            "client_order_id, symbol, side, purpose, order_type, time_in_force, quantity, "
            "status, created_at, updated_at) VALUES ('intent-a1', ?, 'cyc-a1', 'sig-a1', "
            "'client-a1', 'SPY', 'BUY', 'ENTRY', 'MARKET', 'DAY', '10', 'RECORDED', ?, ?)",
            (SCOPE_A, _stamp(NOW), _stamp(NOW)),
        )
        # Domain events.
        conn.execute(
            "INSERT INTO domain_events(event_id, account_scope_id, stream_type, stream_id, "
            "stream_version, event_type, payload_json, payload_sha256, correlation_id, "
            "occurred_at, recorded_at) VALUES ('evt-a1', ?, 'cycle', 'cyc-a1', 1, "
            "'CycleOpened', '{\"cycle_id\":\"cyc-a1\"}', ?, 'run-a1', ?, ?)",
            (SCOPE_A, "0" * 64, _stamp(NOW - timedelta(hours=2)), _stamp(NOW - timedelta(hours=2))),
        )
        conn.execute(
            "INSERT INTO domain_events(event_id, account_scope_id, stream_type, stream_id, "
            "stream_version, event_type, payload_json, payload_sha256, correlation_id, "
            "occurred_at, recorded_at) VALUES ('evt-a2', ?, 'order', 'intent-a1', 1, "
            "'OrderRecorded', '{\"intent_id\":\"intent-a1\",\"cycle_id\":\"cyc-a1\"}', "
            "?, 'run-a1', ?, ?)",
            (SCOPE_A, "1" * 64, _stamp(NOW - timedelta(hours=1)), _stamp(NOW - timedelta(hours=1))),
        )
        # Healthy heartbeat + fresh bar for scope A.
        conn.execute(
            "INSERT INTO service_health(account_scope_id, component, status, heartbeat_at, "
            "observed_at, details_json) VALUES (?, 'engine', 'HEALTHY', ?, ?, '{}')",
            (SCOPE_A, _stamp(NOW - timedelta(seconds=5)), _stamp(NOW)),
        )
        conn.execute(
            "INSERT INTO market_bars(account_scope_id, symbol, timeframe, bar_start, "
            "open_price, high_price, low_price, close_price, volume, feed, provenance, "
            "recorded_at) VALUES (?, 'SPY', '1m', ?, '1', '2', '1', '2', '10', 'iex', "
            "'IEX', ?)",
            (SCOPE_A, _stamp(NOW - timedelta(seconds=10)), _stamp(NOW)),
        )
        conn.execute(
            "INSERT INTO reconciliation_runs(reconciliation_run_id, account_scope_id, status, "
            "started_at, completed_at) VALUES ('recon-a1', ?, 'CLEAN', ?, ?)",
            (SCOPE_A, _stamp(NOW - timedelta(minutes=5)), _stamp(NOW - timedelta(minutes=4))),
        )
    finally:
        conn.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "history.db"
    _seed_db(path)
    return path


def _args(
    db_path: Path,
    command_account: str = "SHADOW:acct-a:baseline",
    **over: object,
) -> SimpleNamespace:
    base: dict[str, object] = {
        "account": command_account, "scenario": None, "symbol": None, "strategy": None,
        "since": None, "until": None, "event_type": None, "type": None, "outcome": None,
        "status": None, "rejection_reason": None, "reason": None, "run_id": None,
        "cycle_id": None, "signal_id": None, "intent_id": None, "client_order_id": None,
        "limit": HISTORY_DEFAULT_LIMIT, "format": "json", "db_path": str(db_path),
        "log_dir": None, "clock": FakeClock(), "level": None, "max_bytes": 256 * 1024,
        "heartbeat_offline_seconds": 60.0, "feed_max_age_seconds": 90.0,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _run(name: str, args: SimpleNamespace, capsys) -> tuple[int, dict]:
    code = run_history_command(name, args)  # type: ignore[arg-type]
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else {})


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def test_signals_symbol_filter(db_path: Path, capsys) -> None:
    code, payload = _run("signals", _args(db_path, symbol="SPY"), capsys)
    assert code == 0
    assert {row["signal_id"] for row in payload["rows"]} == {"sig-a1"}


def test_signals_strategy_filter(db_path: Path, capsys) -> None:
    code, payload = _run(
        "signals", _args(db_path, strategy="vwap_breakdown_default"), capsys)
    assert code == 0
    assert [row["signal_id"] for row in payload["rows"]] == ["sig-a2"]


def test_signals_since_until_iso(db_path: Path, capsys) -> None:
    since = (NOW - timedelta(hours=2)).isoformat()
    code, payload = _run("signals", _args(db_path, since=since), capsys)
    assert code == 0
    assert [row["signal_id"] for row in payload["rows"]] == ["sig-a2"]
    until = (NOW - timedelta(hours=2)).isoformat()
    code, payload = _run("signals", _args(db_path, until=until), capsys)
    assert code == 0
    assert [row["signal_id"] for row in payload["rows"]] == ["sig-a1"]


def test_signals_session_date_filter(db_path: Path, capsys) -> None:
    session_day = NOW.astimezone(history_commands.NEW_YORK).date().isoformat()
    code, payload = _run("signals", _args(db_path, since=session_day), capsys)
    assert code == 0
    assert len(payload["rows"]) == 2


def test_signals_correlation_filters(db_path: Path, capsys) -> None:
    for key, value, expected in (
        ("signal_id", "sig-a2", {"sig-a2"}),
        ("cycle_id", "cyc-a1", {"sig-a1"}),
        ("intent_id", "intent-a1", {"sig-a1"}),
        ("client_order_id", "client-a1", {"sig-a1"}),
        ("event_type", "EXIT_LONG", {"sig-a2"}),
    ):
        code, payload = _run("signals", _args(db_path, **{key: value}), capsys)
        assert code == 0, key
        assert {row["signal_id"] for row in payload["rows"]} == expected, key


def test_evaluations_outcome_and_reason_filters(db_path: Path, capsys) -> None:
    code, payload = _run("evaluations", _args(db_path, outcome="NO_SIGNAL"), capsys)
    assert code == 0
    assert [row["evaluation_id"] for row in payload["rows"]] == ["eval-a1"]
    code, payload = _run(
        "evaluations", _args(db_path, rejection_reason="cross-up"), capsys)
    assert code == 0
    assert [row["evaluation_id"] for row in payload["rows"]] == ["eval-a2"]
    code, payload = _run(
        "evaluations", _args(db_path, symbol="SPY", strategy="vwap_bounce_default"), capsys)
    assert code == 0
    assert [row["evaluation_id"] for row in payload["rows"]] == ["eval-a1"]


def test_events_type_and_correlation_filters(db_path: Path, capsys) -> None:
    code, payload = _run("events", _args(db_path, event_type="OrderRecorded"), capsys)
    assert code == 0
    assert [row["event_id"] for row in payload["rows"]] == ["evt-a2"]
    for key, value, expected in (
        ("run_id", "run-a1", {"evt-a1", "evt-a2"}),
        ("cycle_id", "cyc-a1", {"evt-a1", "evt-a2"}),
        ("intent_id", "intent-a1", {"evt-a2"}),
        ("signal_id", "no-such-signal", set()),
    ):
        code, payload = _run("events", _args(db_path, **{key: value}), capsys)
        assert code == 0, key
        assert {row["event_id"] for row in payload["rows"]} == expected, key


def test_decimal_values_render_as_strings(db_path: Path, capsys) -> None:
    code, payload = _run("signals", _args(db_path, symbol="SPY"), capsys)
    assert code == 0
    assert payload["rows"][0]["reference_price"] == "100.10"


# ---------------------------------------------------------------------------
# Limits, isolation, read-only
# ---------------------------------------------------------------------------

def test_default_limit_and_clamping(db_path: Path, capsys) -> None:
    assert HISTORY_DEFAULT_LIMIT == 50
    assert HISTORY_MAX_LIMIT == 1000
    code, payload = _run("signals", _args(db_path, limit=5000), capsys)
    assert code == 0
    assert payload["limit_clamped_to"] == 1000
    assert len(payload["rows"]) == 2


def test_default_limit_applies(db_path: Path, tmp_path: Path, capsys) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for index in range(60):
            when = NOW - timedelta(minutes=index)
            conn.execute(
                "INSERT INTO signals(signal_id, account_scope_id, instance_id, symbol, "
                "action, reference_price, reason, provenance_json, bar_time, generated_at) "
                "VALUES (?, ?, 'inst-a-spy', 'SPY', 'ENTER_LONG', '1', 'bulk', '{}', ?, ?)",
                (f"sig-bulk-{index:03d}", SCOPE_A, _stamp(when), _stamp(when)),
            )
        conn.commit()
    finally:
        conn.close()
    code, payload = _run("signals", _args(db_path), capsys)
    assert code == 0
    assert len(payload["rows"]) == HISTORY_DEFAULT_LIMIT


def test_account_isolation(db_path: Path, capsys) -> None:
    code, payload = _run(
        "signals", _args(db_path, command_account="SHADOW:acct-b:baseline"), capsys)
    assert code == 0
    assert [row["signal_id"] for row in payload["rows"]] == ["sig-b1"]


def test_unknown_account_is_usage_error(db_path: Path, capsys) -> None:
    code = run_history_command("signals", _args(db_path, command_account="SHADOW:nobody:baseline"))  # type: ignore[arg-type]
    assert code == 2


def test_ambiguous_account_is_usage_error(db_path: Path, capsys) -> None:
    code = run_history_command("signals", _args(db_path, command_account="shadow"))  # type: ignore[arg-type]
    assert code == 2


def test_read_only_connection_rejects_writes(db_path: Path) -> None:
    store = ReadStore.open(db_path)
    try:
        with store.snapshot(SCOPE_A) as conn:
            with pytest.raises((ReadOnlyViolationError, sqlite3.DatabaseError)):
                conn.execute("INSERT INTO signals(signal_id) VALUES ('x')")
    finally:
        store.close()
    # No broker or data-provider modules may be pulled in by history commands.
    assert "mike_trade.brokers" not in sys.modules
    assert "mike_trade.market_data" not in sys.modules
    source = Path(history_commands.__file__).read_text()
    assert "mike_trade.brokers" not in source
    assert "mike_trade.market_data" not in source


def test_add_history_arguments_registers_filters() -> None:
    parser = argparse.ArgumentParser()
    add_history_arguments(parser, "signals")
    parsed = parser.parse_args(["--symbol", "SPY", "--limit", "10", "--run-id", "r1"])
    assert parsed.symbol == "SPY"
    assert parsed.limit == 10
    assert parsed.run_id == "r1"


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

def _write_log(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def test_logs_level_and_correlation_filtering(tmp_path: Path, capsys) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _write_log(log_dir / "app.jsonl", [
        {"timestamp": NOW.isoformat(), "level": "INFO", "logger": "a",
         "message": "kept", "run_id": "run-1", "cycle_id": "c1"},
        {"timestamp": NOW.isoformat(), "level": "ERROR", "logger": "a",
         "message": "boom", "run_id": "run-2", "cycle_id": "c2"},
        {"timestamp": NOW.isoformat(), "level": "INFO", "logger": "a",
         "message": "other", "run_id": "run-2", "cycle_id": "c2"},
    ])
    args = _args(tmp_path / "unused.db", log_dir=str(log_dir), level="ERROR", run_id="run-2")
    code = run_history_command("logs", args)  # type: ignore[arg-type]
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["message"] == "boom"


def test_logs_line_and_byte_caps(tmp_path: Path, capsys) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _write_log(log_dir / "app.jsonl", [
        {"timestamp": NOW.isoformat(), "level": "INFO", "logger": "a",
         "message": f"line-{i}", "run_id": "r"} for i in range(30)
    ])
    args = _args(tmp_path / "unused.db", log_dir=str(log_dir), limit=5)
    assert run_history_command("logs", args) == 0  # type: ignore[arg-type]
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["rows"]) == 5
    assert payload["truncated"] is True
    # Byte cap truncates even when the line limit would allow more.
    args = _args(tmp_path / "unused.db", log_dir=str(log_dir), limit=50, max_bytes=1024)
    assert run_history_command("logs", args) == 0  # type: ignore[arg-type]
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["rows"]) < 30
    assert payload["truncated"] is True


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health_healthy_exit_zero(db_path: Path, capsys) -> None:
    code, payload = _run("health", _args(db_path), capsys)
    assert code == 0
    assert payload["engine_state"] == "ONLINE"
    assert payload["issues"] == []


def test_health_offline_exit_nonzero(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE service_health SET heartbeat_at = ? WHERE account_scope_id = ? "
            "AND component = 'engine'",
            (_stamp(NOW - timedelta(minutes=10)), SCOPE_A),
        )
        conn.commit()
    finally:
        conn.close()
    args = _args(db_path)
    code = run_history_command("health", args)  # type: ignore[arg-type]
    assert code == 5


def test_health_reports_unknown_orders_and_drift(db_path: Path, capsys) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO broker_orders(account_scope_id, broker_order_id, client_order_id, "
            "symbol, side, order_type, status, quantity, filled_quantity, submitted_at, "
            "updated_at) VALUES (?, 'bo-1', 'client-x', 'SPY', 'BUY', 'MARKET', 'UNKNOWN', "
            "'10', '0', ?, ?)",
            (SCOPE_A, _stamp(NOW), _stamp(NOW)),
        )
        conn.execute(
            "INSERT INTO reconciliation_runs(reconciliation_run_id, account_scope_id, status, "
            "started_at) VALUES ('recon-a2', ?, 'DRIFTED', ?)",
            (SCOPE_A, _stamp(NOW)),
        )
        conn.execute(
            "INSERT INTO reconciliation_drifts(drift_id, account_scope_id, "
            "reconciliation_run_id, drift_type, detected_at) VALUES ('drift-1', ?, "
            "'recon-a2', 'POSITION', ?)",
            (SCOPE_A, _stamp(NOW)),
        )
        conn.commit()
    finally:
        conn.close()
    code, payload = _run("health", _args(db_path), capsys)
    assert code == 5
    codes = {issue["code"] for issue in payload["issues"]}
    assert "unknown_order_states" in codes
    assert "reconciliation_drift" in codes or "position_drift" in codes
    for issue in payload["issues"]:
        assert issue["remediation_command"].startswith("mike-trade ")


# ---------------------------------------------------------------------------
# Alert evaluators: every listed condition
# ---------------------------------------------------------------------------

def _snapshot(**over: object) -> dict:
    base: dict[str, object] = {
        "heartbeat_age_seconds": 5.0, "feed_age_seconds": 5.0,
        "feed_message_rate_per_sec": 1.0, "scheduler_lag_seconds": 0.0,
        "evaluation_duration_seconds": {"count": 1, "max": 1.0},
        "evaluation_errors_total": 0.0, "broker_rest_errors_total": 0.0,
        "broker_rest_error_rate": 0.0, "broker_ws_errors_total": 0.0,
        "broker_ws_error_rate": 0.0, "unknown_order_states": 0.0,
        "position_drift_quantity": 0.0, "reconciliation_drifts_unresolved": 0.0,
        "reconciliation_status_error": 0.0, "shadow_fill_gap": 0.0,
        "stop_placement_failures_total": 0.0, "orphan_stops": 0.0, "expired_stops": 0.0,
        "unconfirmed_stepped_trailing_replacement": 0.0,
        "stale_trailing_watermark_age_seconds": 0.0, "uncovered_fills_total": 0.0,
        "live_position_engine_offline": 0.0, "unexpected_shorts": 0.0,
        "risk_breaches_total": 0.0, "unhandled_exceptions_total": 0.0,
        "signals_total": 0.0, "rejections_total": 0.0,
    }
    base.update(over)
    return base


@pytest.mark.parametrize("over,code", [
    ({"heartbeat_age_seconds": 500.0}, "heartbeat_offline"),
    ({"feed_age_seconds": 500.0}, "stale_feed"),
    ({"feed_message_rate_per_sec": 0.0}, "feed_rate_low"),
    ({"scheduler_lag_seconds": 120.0}, "scheduler_lag"),
    ({"evaluation_duration_seconds": {"count": 2, "max": 99.0}}, "evaluation_slow"),
    ({"evaluation_errors_total": 1.0}, "evaluation_errors"),
    ({"broker_rest_errors_total": 2.0}, "broker_rest_errors"),
    ({"broker_ws_errors_total": 2.0}, "broker_ws_errors"),
    ({"unknown_order_states": 1.0}, "unknown_order_states"),
    ({"position_drift_quantity": 3.0}, "position_drift"),
    ({"reconciliation_drifts_unresolved": 1.0}, "reconciliation_drift"),
    ({"shadow_fill_gap": 1.0}, "shadow_fills"),
    ({"stop_placement_failures_total": 1.0}, "stop_placement_failures"),
    ({"orphan_stops": 1.0}, "orphan_stops"),
    ({"expired_stops": 1.0}, "expired_stops"),
    ({"unconfirmed_stepped_trailing_replacement": 1.0},
     "unconfirmed_stepped_trailing_replacement"),
    ({"stale_trailing_watermark_age_seconds": 5000.0}, "stale_trailing_watermark"),
    ({"uncovered_fills_total": 1.0}, "uncovered_fills"),
    ({"live_position_engine_offline": 1.0}, "live_position_engine_offline"),
    ({"unexpected_shorts": 1.0}, "unexpected_short"),
    ({"risk_breaches_total": 1.0}, "risk_breach"),
    ({"unhandled_exceptions_total": 1.0}, "unhandled_exception"),
])
def test_each_alert_condition_yields_issue(over: dict, code: str) -> None:
    issues = evaluate_alerts(_snapshot(**over))
    assert isinstance(issues, list) and issues
    match = [issue for issue in issues if issue.code == code]
    assert match, f"missing issue {code}"
    issue = match[0]
    assert isinstance(issue, HealthIssue)
    assert issue.severity in ("critical", "warning")
    assert issue.message.strip()
    assert issue.remediation_command.startswith("mike-trade ")


def test_metrics_registry_covers_catalog() -> None:
    registry = MetricsRegistry()
    snap = registry.snapshot()
    for name in (
        "feed_age_seconds", "feed_message_rate_per_sec", "scheduler_lag_seconds",
        "evaluation_duration_seconds", "signals_total", "rejections_total",
        "broker_rest_errors_total", "broker_rest_error_rate",
        "broker_ws_errors_total", "broker_ws_error_rate", "unknown_order_states",
        "position_drift_quantity", "shadow_fills_total", "realized_pnl",
        "unrealized_pnl", "heartbeat_age_seconds", "stop_placement_failures_total",
        "orphan_stops", "expired_stops", "unconfirmed_stepped_trailing_replacement",
        "stale_trailing_watermark_age_seconds", "uncovered_fills_total",
        "live_position_engine_offline", "unexpected_shorts", "risk_breaches_total",
        "stale_feed", "unhandled_exceptions_total",
    ):
        assert name in snap, name
    registry.inc("signals_total")
    registry.set_gauge("feed_age_seconds", 3.5)
    registry.observe("evaluation_duration_seconds", 1.25)
    assert registry.counter("signals_total") == 1.0
    assert registry.gauge("feed_age_seconds") == 3.5
    assert registry.histogram_stats("evaluation_duration_seconds")["count"] == 1


# ---------------------------------------------------------------------------
# Redaction + correlation context
# ---------------------------------------------------------------------------

def test_redaction_masks_secrets_and_account_ids() -> None:
    filt = RedactionFilter()
    record = logging.LogRecord("t", logging.INFO, __file__, 0, "msg", (), None)
    record.api_key = "sk-live-SECRET123"  # type: ignore[attr-defined]
    record.auth_token = "bearer-ABCDEF"  # type: ignore[attr-defined]
    record.account_scope_id = "shadow:ACCT9999:baseline"  # type: ignore[attr-defined]
    record.provider_payload = {"symbol": "SPY", "secret": "nope"}  # type: ignore[attr-defined]
    assert filt.filter(record) is True
    assert record.api_key == "***REDACTED***"  # type: ignore[attr-defined]
    assert record.auth_token == "***REDACTED***"  # type: ignore[attr-defined]
    assert "ACCT9999" not in str(record.account_scope_id)  # type: ignore[attr-defined]
    assert str(record.account_scope_id) == "****" + "shadow:ACCT9999:baseline"[-4:]  # type: ignore[attr-defined]
    assert record.provider_payload == {"symbol": "SPY"}  # type: ignore[attr-defined]


def test_history_output_redacts_account_scope(db_path: Path, capsys) -> None:
    code, payload = _run("health", _args(db_path), capsys)
    assert code == 0
    assert "acct-a" not in json.dumps(payload)


def test_context_binding_in_log_records() -> None:
    bind_log_context(run_id="run-9", cycle_id="cyc-9", signal_id="sig-9",
                     intent_id="intent-9", client_order_id="client-9")
    try:
        filt = RedactionFilter()
        record = logging.LogRecord("t", logging.INFO, __file__, 0, "hello", (), None)
        assert filt.filter(record) is True
        assert record.run_id == "run-9"  # type: ignore[attr-defined]
        assert record.cycle_id == "cyc-9"  # type: ignore[attr-defined]
        assert record.signal_id == "sig-9"  # type: ignore[attr-defined]
        assert record.intent_id == "intent-9"  # type: ignore[attr-defined]
        assert record.client_order_id == "client-9"  # type: ignore[attr-defined]
    finally:
        bind_log_context(run_id="", cycle_id="", signal_id="", intent_id="",
                         client_order_id="")
