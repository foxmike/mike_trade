"""Provider-neutral operator CLI.

Exit codes are stable: ``0`` ok, ``2`` usage, ``3`` refused by a safety or
confirmation guard, ``4`` engine offline or unreachable for an action that
needs it, and ``5`` unhealthy or check failed. Commands are scoped by account
and optional shadow scenario. LIVE mutations require ``--confirm-live YES`` or
``CONFIRM_LIVE=YES``. Output is redacted and Decimal values are JSON strings.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from decimal import Decimal
from typing import Any

from mike_trade.observability.logging import configure_logging

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_ENGINE_OFFLINE = 4
EXIT_UNHEALTHY = 5

CANONICAL_COMMANDS = (
    "doctor", "config-show", "providers", "provider-check", "broker-check", "data-check",
    "strategies", "strategy-info", "strategy-check", "strategy-enable", "strategy-disable", "strategy-set",
    "symbols", "symbols-add", "symbols-remove", "symbols-enable", "symbols-disable",
    "start", "stop", "restart", "status",
    "dashboard", "dashboard-start", "dashboard-stop", "dashboard-health",
    "shadow-start", "shadow-stop", "shadow-list", "shadow-create", "shadow-reset",
    "paper-start", "paper-stop", "paper-status",
    "live-preflight", "live-enable", "live-start", "live-disable",
    "pause", "resume", "pause-symbol", "resume-symbol", "entries-disable", "entries-enable",
    "close", "close-all", "flatten",
    "trail-status", "trail-history", "trail-policy-show", "trail-policy-set", "trail-reconcile",
    "entry-limits", "entry-limits-reset", "cooldown-status",
    "stop-status", "stops-audit",
    "positions", "orders", "balance", "budget", "reconcile",
    "signals", "evaluations", "events", "logs", "health",
    "report", "report-daily", "report-symbol", "report-strategy", "report-pairs", "report-shadow", "report-comparison", "report-export",
    "backtest", "backtest-status", "backtest-report", "backtest-list",
    "research-data", "research-backtest", "research-six", "research-batch", "research-results",
    "model-prepare", "model-train", "model-walk-forward", "model-evaluate", "model-inspect",
    "model-list", "model-compare", "model-report", "model-backtest", "model-backtest-compare",
    "model-approve", "model-pin", "model-check", "model-health",
    "cache-status", "cache-refresh",
    "db-check", "db-backup", "db-restore-dry-run",
    "service-install", "service-uninstall", "service-status",
)

OFFLINE_COMMANDS = {
    "research-data",
    "research-backtest",
    "research-six",
    "research-batch",
    "research-results",
    "model-prepare",
    "model-train",
    "model-walk-forward",
    "model-evaluate",
    "model-inspect",
    "model-list",
    "model-compare",
    "model-report",
    "model-backtest",
    "model-backtest-compare",
    "model-approve",
    "model-pin",
    "model-check",
    "model-health",
}

HISTORY_COMMANDS = frozenset({"signals", "evaluations", "events", "logs", "health"})
POSITION_COMMANDS = frozenset({
    "positions", "orders", "balance", "budget", "reconcile", "close", "close-all", "flatten",
    "stop-status", "stops-audit", "trail-status", "trail-history", "trail-policy-show",
    "trail-policy-set", "trail-reconcile", "entry-limits", "entry-limits-reset", "cooldown-status",
})
LIVE_CONFIRM_COMMANDS = frozenset({
    "close", "close-all", "flatten", "live-enable", "live-start", "entry-limits-reset",
    "trail-policy-set", "trail-reconcile", "strategy-enable", "strategy-disable", "strategy-set",
    "symbols-add", "symbols-remove", "symbols-enable", "symbols-disable", "resume", "resume-symbol", "entries-enable",
})


def require_live_confirmation(args: argparse.Namespace, *, command: str | None = None) -> bool:
    """Refuse guarded LIVE changes unless explicit CLI or environment approval exists."""
    account = str(getattr(args, "account", "")).upper()
    if (account == "LIVE" or account.startswith("LIVE:")) or getattr(args, "environment", "") == "LIVE":
        confirmed = getattr(args, "confirm_live", None) == "YES" or os.environ.get("CONFIRM_LIVE") == "YES"
        if not confirmed:
            label = command or getattr(args, "subcommand", "this command")
            print(f"Refused: {label} on a LIVE account requires --confirm-live YES or CONFIRM_LIVE=YES.", file=sys.stderr)
            return False
    return True


def _global_options(parser: argparse.ArgumentParser, *, defaults: bool) -> None:
    default = None if defaults else argparse.SUPPRESS
    parser.add_argument("--account", default=default, help="Account scope (safe default: SHADOW)")
    parser.add_argument("--scenario", default=default, help="Shadow scenario")
    parser.add_argument("--format", choices=("table", "json"), default="table" if defaults else argparse.SUPPRESS)
    parser.add_argument("--confirm-live", choices=("YES",), default=default, metavar="YES")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mike-trade", description="Provider-neutral mike_trade operator commands")
    _global_options(parser, defaults=True)
    subs = parser.add_subparsers(dest="subcommand", required=True)
    for name in CANONICAL_COMMANDS:
        if name in OFFLINE_COMMANDS:
            continue
        command = subs.add_parser(name)
        _global_options(command, defaults=False)
        if name in {"close", "pause-symbol", "resume-symbol", "strategy-info", "strategy-check", "strategy-enable", "strategy-disable", "strategy-set", "symbols-add", "symbols-remove", "symbols-enable", "symbols-disable", "report-symbol", "report-strategy", "backtest-report"}:
            command.add_argument("symbol_or_name", nargs="?", help="Symbol or strategy identifier")
        if name in {"close", "pause-symbol", "resume-symbol", "strategy-info", "strategy-check", "strategy-enable", "strategy-disable", "strategy-set", "symbols-add", "symbols-remove", "symbols-enable", "symbols-disable", "report-symbol", "report-strategy", "backtest-report"}:
            command.add_argument("value", nargs="?", help="Optional value or configuration")
        if name in {"trail-policy-set", "shadow-create", "backtest", "report-export", "db-backup", "db-restore-dry-run"}:
            command.add_argument("value", nargs="?", help="Policy, scenario, dataset, or path")
        if name == "report-export":
            command.add_argument("--output", dest="output_path")
        if name == "start":
            command.add_argument(
                "--config", default=None,
                help="configuration file (default: $MIKE_TRADE_CONFIG or config/mike_trade.yaml)",
            )
            command.add_argument(
                "--mode-config", default=None,
                help="optional mode overlay (default: $MIKE_TRADE_MODE_CONFIG)",
            )
            command.add_argument(
                "--set", dest="overrides", action="append", default=[],
                metavar="dotted.path=value",
                help="one-shot validated configuration override (repeatable)",
            )
        if name in {"backtest", "report", "report-daily", "report-symbol", "report-strategy", "report-pairs", "report-shadow", "report-comparison"}:
            command.add_argument("--period", default="all")
        command.add_argument("--reason", default="operator request")
    from mike_trade.research.commands import register as register_research
    register_research(subs)
    from mike_trade.modeling.commands import register as register_modeling
    register_modeling(subs)
    return parser


def _json_safe(value: Any, key: str = "") -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): ("***" if _secret_key(str(k)) else _json_safe(v, str(k))) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v, key) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict
        return _json_safe(asdict(value), key)
    return value


def _secret_key(key: str) -> bool:
    low = key.lower()
    return any(part in low for part in ("secret", "password", "token", "api_key", "key_id")) or low in {"account", "scope", "account_id", "account_scope_id"}


def render(value: Any, output_format: str = "table") -> None:
    value = _json_safe(value)
    if output_format == "json":
        print(json.dumps(value, sort_keys=True, default=str))
    elif isinstance(value, dict):
        if not value:
            print("(no data)")
        else:
            width = max(map(len, map(str, value.keys())))
            for key, item in value.items():
                print(f"{str(key):<{width}}  {item}")
    elif isinstance(value, list):
        for item in value:
            print(item)
    else:
        print(value)


def _history(name: str, args: argparse.Namespace) -> int:
    # Deliberately lazy: task s.5 owns the implementation and tests may replace it.
    module = importlib.import_module("mike_trade.controls.history_commands")
    return int(module.run_history_command(name, args))


def _start_engine(args: argparse.Namespace) -> int:
    """Run the configured production runtime as the foreground process."""
    from mike_trade.app import build_runtime, load_app_settings
    from mike_trade.runtime import StartupState

    async def run() -> int:
        settings = load_app_settings(
            getattr(args, "config", None),
            mode_path=getattr(args, "mode_config", None),
            cli_overrides=getattr(args, "overrides", ()),
        )
        runtime = build_runtime(settings)
        runtime.install_signal_handlers()
        try:
            status = await runtime.start()
            render(
                {
                    "state": status.state.value,
                    "mode": settings.mode.value,
                    "database": str(settings.database_path),
                    "symbols": list(settings.symbols),
                    "reconciliation": status.reconciliation_status,
                    "entries_allowed": status.entries_allowed,
                    "reasons": list(status.reasons),
                },
                args.format,
            )
            while runtime.state is not StartupState.STOPPED:
                await asyncio.sleep(0.25)
            return EXIT_OK
        finally:
            if runtime.state not in {StartupState.DRAINING, StartupState.STOPPED}:
                await runtime.stop()

    try:
        return asyncio.run(run())
    except Exception as exc:
        print(f"Engine startup failed: {exc}", file=sys.stderr)
        return EXIT_UNHEALTHY


def main(argv: list[str] | None = None) -> int:
    configure_logging(level="INFO")
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    args.account = args.account or os.environ.get("MIKE_TRADE_ACCOUNT", "SHADOW:default")
    if args.scenario is None and ":" in args.account and args.account.count(":") > 1:
        args.scenario = args.account.split(":", 2)[2]
    name = args.subcommand
    if name in OFFLINE_COMMANDS:
        try:
            return int(args.func(args))
        except Exception as exc:
            print(f"Offline command failed: {exc}", file=sys.stderr)
            return EXIT_UNHEALTHY
    if name in LIVE_CONFIRM_COMMANDS and not require_live_confirmation(args, command=name):
        return EXIT_REFUSED
    if name == "start":
        return _start_engine(args)
    if name in HISTORY_COMMANDS:
        try:
            return _history(name, args)
        except (ImportError, AttributeError) as exc:
            print(f"History command unavailable: {exc}", file=sys.stderr)
            return EXIT_UNHEALTHY
    try:
        module_name = "mike_trade.controls.position_commands" if name in POSITION_COMMANDS else "mike_trade.controls.cli_commands"
        module = importlib.import_module(module_name)
        return int(module.run_operator_command(name, args))
    except Exception as exc:
        from mike_trade.controls.cli_commands import OperatorCommandError
        from mike_trade.controls.position_commands import PositionCommandError
        if isinstance(exc, (OperatorCommandError, PositionCommandError)):
            print(str(exc), file=sys.stderr)
            return exc.exit_code
        print(f"Command failed: {exc}", file=sys.stderr)
        return EXIT_UNHEALTHY


if __name__ == "__main__":
    raise SystemExit(main())
