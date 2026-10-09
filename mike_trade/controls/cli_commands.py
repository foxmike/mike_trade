"""Thin lifecycle, configuration, provider, strategy, mode and reporting CLI routes.

Concrete application services are injected through ``CLI_SERVICES`` by the
composition root. This module owns argument routing and safety checks only.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any

from mike_trade.cli import EXIT_ENGINE_OFFLINE, EXIT_REFUSED, EXIT_UNHEALTHY, require_live_confirmation, render

CLI_SERVICES: dict[str, Any] = {}
REQUIRED_DEPENDENCIES = (("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("pydantic", "pydantic"),
                        ("pydantic-settings", "pydantic_settings"), ("httpx", "httpx"), ("alpaca-py", "alpaca"))


class OperatorCommandError(RuntimeError):
    def __init__(self, message: str, exit_code: int = EXIT_UNHEALTHY):
        super().__init__(message)
        self.exit_code = exit_code


def _scope(args: Any) -> str:
    return str(args.account)


def _service(name: str) -> Any:
    service = CLI_SERVICES.get(name)
    if service is None:
        raise OperatorCommandError(f"{name}: service is not configured")
    return service


def _invoke(service: Any, operation: str, args: Any) -> Any:
    method = getattr(service, operation, None)
    if method is None and callable(service):
        method = service
    if method is None:
        raise OperatorCommandError(f"{operation}: service operation is unavailable")
    try:
        return method(scope=_scope(args), scenario=args.scenario, args=args)
    except TypeError:
        try:
            return method(_scope(args), args)
        except TypeError:
            return method(_scope(args))


def _run_report(name: str, args: Any) -> Any | None:
    """Use the s.1/s.2 reporting builders against an injected, scope-bound source."""
    provider = CLI_SERVICES.get("reporting-context")
    if provider is None:
        return None
    context = provider(scope=_scope(args), scenario=args.scenario, args=args)
    source = context["source"]
    report_filter = context["report_filter"]
    from dataclasses import replace
    from mike_trade.reporting import metrics

    period = getattr(args, "period", "all")
    report_filter = replace(report_filter, period=period)
    if name == "report-symbol":
        report_filter = replace(report_filter, symbol=args.symbol_or_name)
    elif name == "report-strategy":
        report_filter = replace(report_filter, buy_strategy=args.symbol_or_name)
    if name in {"report-pairs"}:
        return metrics.summarize_by_pair(source, report_filter)
    if name in {"report-symbol"}:
        return metrics.summarize_by_symbol(source, report_filter)
    if name in {"report-strategy"}:
        return metrics.summarize_by_strategy(source, report_filter)
    if name == "report-comparison":
        compare = context.get("compare_accounts")
        if compare is None:
            raise OperatorCommandError("comparison reporting requires two explicitly scoped accounts")
        return metrics.compare_accounts(source, compare, report_filter)
    return metrics.build_trade_cycles(source, report_filter)


def _doctor() -> int:
    import json
    from datetime import UTC, datetime, timedelta

    import mike_trade
    from mike_trade.app import AppSettings
    from mike_trade.runtime import ExecutionMode

    ok = True
    if sys.version_info >= (3, 12):
        print(f"[PASS] Python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        print(f"[FAIL] Python {sys.version_info.major}.{sys.version_info.minor} requires >= 3.12")
        ok = False
    print(f"[PASS] mike_trade importable: {mike_trade.__version__}")
    for dist, module in REQUIRED_DEPENDENCIES:
        try:
            importlib.import_module(module)
            print(f"[PASS] Dependency {dist} available")
        except ImportError:
            print(f"[FAIL] Dependency {dist} unavailable")
            ok = False
    ignore_file = Path(".gitignore")
    if ignore_file.exists():
        ignored = any(line.strip().rstrip("/") == "state" for line in ignore_file.read_text().splitlines())
        print(f"[{ 'PASS' if ignored else 'FAIL' }] state directory gitignored")
        ok = ok and ignored
    else:
        print("[INFO] source .gitignore check unavailable in packaged runtime")
    mode = AppSettings().mode
    safe = mode != ExecutionMode.LIVE
    print(f"[{ 'PASS' if safe else 'FAIL' }] default mode: {mode.value}")
    ok = ok and safe
    for key in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
        print(f"[INFO] Credential {key}: {'PRESENT' if os.environ.get(key) else 'ABSENT'}")
    backup_status_path = os.environ.get("MIKE_TRADE_BACKUP_STATUS_FILE")
    if backup_status_path:
        try:
            status = json.loads(Path(backup_status_path).read_text(encoding="utf-8"))
            for name, maximum_age in (("operational", timedelta(hours=36)),
                                      ("research", timedelta(days=8))):
                item = status[name]
                last = datetime.fromisoformat(item["last_success_at"])
                healthy = (item["last_status"] == "ok" and last.tzinfo is not None
                           and datetime.now(UTC) - last <= maximum_age)
                print(f"[{'PASS' if healthy else 'FAIL'}] {name} backup status")
                ok = ok and healthy
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"[FAIL] backup status unavailable: {type(exc).__name__}")
            ok = False
    return 0 if ok else EXIT_UNHEALTHY


def run_operator_command(name: str, args: Any) -> int:
    if name == "doctor":
        return _doctor()
    if name in {"pause", "resume", "pause-symbol", "resume-symbol", "entries-disable", "entries-enable"}:
        controls = _service("entry-controls")
        # These controls affect eligibility of new entries only. They do not
        # stop fill handling, protection, trailing management or reconciliation.
        method_name = {
            "pause": "pause_entries", "resume": "resume_entries",
            "pause-symbol": "pause_symbol_entries", "resume-symbol": "resume_symbol_entries",
            "entries-disable": "disable_entries", "entries-enable": "enable_entries",
        }[name]
        result = _invoke(controls, method_name, args)
        render(result if result is not None else {"command": name, "scope": _scope(args), "effect": "new entries only"}, args.format)
        return 0
    if name in {"live-enable", "live-start"}:
        try:
            result = _invoke(_service("live-preflight"), "run", args)
        except Exception as exc:
            print(f"Live preflight failed: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        if result is False or getattr(result, "ok", True) is False:
            print("Live preflight failed; operation refused.", file=sys.stderr)
            return EXIT_REFUSED
    if name == "stop":
        result = _invoke(_service(name), "stop_engine", args)
        print("Engine stopped. Open positions remain broker-protected.")
        if result is not None:
            render(result, args.format)
        return 0
    if name.startswith("report"):
        try:
            report = _run_report(name, args)
        except Exception as exc:
            raise OperatorCommandError(f"{name}: {exc}") from exc
        if report is not None:
            if name == "report-export":
                from mike_trade.reporting.export import export_json
                export_json(report, "trade_cycles", getattr(args, "output_path", None))
                print(f"Exported report to {args.output_path}" if args.output_path else "Report export rendered.")
            else:
                render(report, args.format)
            return 0
    service = _service(name)
    # Explicit operation mapping avoids converting CLI names into business logic.
    operation = {
        "start": "start_engine", "restart": "restart_engine", "status": "status",
        "doctor": "doctor", "config-show": "show_config", "providers": "list_providers",
        "provider-check": "check_provider", "broker-check": "check_broker", "data-check": "check_data",
        "strategies": "list_strategies", "strategy-info": "strategy_info", "strategy-check": "check_strategy",
        "strategy-enable": "enable_strategy", "strategy-disable": "disable_strategy", "strategy-set": "set_strategy",
        "symbols": "list_symbols", "symbols-add": "add_symbols", "symbols-remove": "remove_symbols",
        "symbols-enable": "enable_symbols", "symbols-disable": "disable_symbols",
        "dashboard": "dashboard_status", "dashboard-start": "start_dashboard", "dashboard-stop": "stop_dashboard", "dashboard-health": "dashboard_health",
        "shadow-start": "start_shadow", "shadow-stop": "stop_shadow", "shadow-list": "list_scenarios", "shadow-create": "create_scenario", "shadow-reset": "reset_scenario",
        "paper-start": "start_paper", "paper-stop": "stop_paper", "paper-status": "paper_status",
        "live-preflight": "run", "live-enable": "enable_live", "live-start": "start_live", "live-disable": "disable_live",
        "report": "report", "report-daily": "report_daily", "report-symbol": "report_symbol", "report-strategy": "report_strategy",
        "report-pairs": "report_pairs", "report-shadow": "report_shadow", "report-comparison": "report_comparison", "report-export": "report_export",
        "backtest": "run_backtest", "backtest-status": "backtest_status", "backtest-report": "backtest_report", "backtest-list": "backtest_list",
        "cache-status": "cache_status", "cache-refresh": "cache_refresh", "db-check": "db_check", "db-backup": "db_backup", "db-restore-dry-run": "db_restore_dry_run",
        "service-install": "service_install", "service-uninstall": "service_uninstall", "service-status": "service_status",
    }[name]
    try:
        result = _invoke(service, operation, args)
    except (ConnectionError, TimeoutError) as exc:
        raise OperatorCommandError(f"{name}: service unreachable: {exc}", EXIT_ENGINE_OFFLINE) from exc
    except Exception as exc:
        raise OperatorCommandError(f"{name}: {exc}") from exc
    if name.endswith("-check") or name in {"doctor", "live-preflight", "dashboard-health", "db-check"}:
        if result is False or getattr(result, "ok", True) is False:
            render(result, args.format)
            return EXIT_UNHEALTHY
    render(result, args.format)
    return 0
