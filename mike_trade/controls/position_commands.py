"""Thin account-scoped position and risk-control routes.

The engine owns serialized command acknowledgement and order coordination.
The CLI refuses executable actions before submission when its heartbeat is
missing or stale, so no future intent is queued offline.
"""
from __future__ import annotations

import sys
from typing import Any

from mike_trade.cli import EXIT_ENGINE_OFFLINE, EXIT_REFUSED, EXIT_UNHEALTHY, render

POSITION_SERVICES: dict[str, Any] = {}


class PositionCommandError(RuntimeError):
    def __init__(self, message: str, exit_code: int = EXIT_UNHEALTHY):
        super().__init__(message)
        self.exit_code = exit_code


def _scope(args: Any) -> str:
    return str(args.account)


def _service(name: str) -> Any:
    value = POSITION_SERVICES.get(name)
    if value is None:
        raise PositionCommandError(f"{name}: service is not configured")
    return value


def _call(method: Any, scope: str, args: Any) -> Any:
    try:
        return method(scope=scope, scenario=args.scenario, args=args)
    except TypeError:
        try:
            return method(scope, args)
        except TypeError:
            return method(scope)


def _engine_command(name: str, args: Any) -> int:
    engine = _service("engine_control")
    scope = _scope(args)
    online = getattr(engine, "is_online", None) or getattr(engine, "is_engine_online", None)
    if online is None or not bool(online(scope)):
        print(f"{name} refused: engine heartbeat is absent or stale for the selected account; no command was queued.", file=sys.stderr)
        return EXIT_ENGINE_OFFLINE
    send = getattr(engine, "send_and_wait", None) or getattr(engine, "submit_and_wait", None) or getattr(engine, "send_control", None)
    if send is None:
        raise PositionCommandError("engine control channel is unavailable", EXIT_ENGINE_OFFLINE)
    payload = {"symbol": getattr(args, "symbol_or_name", None), "reason": args.reason,
               "live_confirmed": getattr(args, "confirm_live", None) == "YES"}
    try:
        result = send(name, scope=scope, payload=payload, timeout=getattr(engine, "ack_timeout", 30))
    except (ConnectionError, TimeoutError) as exc:
        raise PositionCommandError(f"{name} acknowledgement unavailable: {exc}", EXIT_ENGINE_OFFLINE) from exc
    if result is False or getattr(result, "acknowledged", True) is False:
        raise PositionCommandError(f"{name} was not acknowledged by the engine", EXIT_ENGINE_OFFLINE)
    render(result if result is not None else {"command": name, "account": scope, "status": "acknowledged"}, args.format)
    return 0


def run_operator_command(name: str, args: Any) -> int:
    if name in {"close", "close-all", "flatten"}:
        return _engine_command(name, args)
    service = _service(name)
    operation = {
        "positions": "positions", "orders": "orders", "balance": "balance", "budget": "budget",
        "reconcile": "reconcile", "stop-status": "stop_status", "stops-audit": "stops_audit",
        "trail-status": "trail_status", "trail-history": "trail_history", "trail-policy-show": "trail_policy_show",
        "trail-policy-set": "trail_policy_set", "trail-reconcile": "trail_reconcile",
        "entry-limits": "entry_limits", "entry-limits-reset": "entry_limits_reset", "cooldown-status": "cooldown_status",
    }[name]
    method = getattr(service, operation, None)
    if method is None:
        raise PositionCommandError(f"{name}: service operation is unavailable")
    try:
        result = _call(method, _scope(args), args)
    except (ConnectionError, TimeoutError) as exc:
        raise PositionCommandError(f"{name}: service unreachable: {exc}", EXIT_ENGINE_OFFLINE) from exc
    except Exception as exc:
        raise PositionCommandError(f"{name}: {exc}") from exc
    if getattr(result, "ok", True) is False:
        render(result, args.format)
        return EXIT_UNHEALTHY
    render(result, args.format)
    return 0
