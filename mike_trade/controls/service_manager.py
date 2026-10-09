"""Minimal systemd service helpers for the optional future deployment templates.

This module only shells out to ``systemctl`` for ordinary service operations
(start/stop/restart/status/enable/disable/is-enabled) on the two template
units (:file:`deploy/systemd/mike-trade-engine.service` and
:file:`deploy/systemd/mike-trade-dashboard.service`). It performs NO
deployment: it never installs unit files, never writes outside the calling
process, never changes users/groups/permissions, never touches the network,
and never starts an engine or dashboard by itself.

Conventions (deliberately small; stdlib only):

* Authentication/authorization is plain OS permissions for ``systemctl``.
  There is no extra application-level credential or live-confirmation gate
  anywhere in this module, and none is required for any operation.
  In particular, ``stop`` and ``status`` never require confirmation.
* A normal stop only asks systemd to stop the unit (SIGTERM/drain). It never
  liquidates positions, never flattens, never cancels orders, and never
  cancels broker-held protective stops -- this module has no broker access
  at all and issues no trading commands.
* If a unit is not installed, the operation returns a ``ServiceResult`` with
  ``installed=False`` and a plain "not installed" detail. Units are never
  installed automatically.
* Secrets are never printed or logged: command output is summarized to short
  status words, and environment values are never captured or rendered.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

__all__ = [
    "ENGINE_UNIT",
    "DASHBOARD_UNIT",
    "KNOWN_UNITS",
    "ServiceResult",
    "start",
    "stop",
    "restart",
    "status",
    "enable",
    "disable",
    "is_enabled",
    "is_active",
]

ENGINE_UNIT = "mike-trade-engine.service"
DASHBOARD_UNIT = "mike-trade-dashboard.service"
KNOWN_UNITS = (ENGINE_UNIT, DASHBOARD_UNIT)

#: Output markers systemd uses when a unit file does not exist.
_NOT_INSTALLED_MARKERS = (
    "could not be found",
    "not found",
    "no such file",
    "not installed",
    "unit file could not be found",
)


@dataclass(frozen=True, slots=True)
class Completed:
    """Minimal completed-process view (mirrors the fields we use)."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


def _default_run(argv: Sequence[str]) -> Completed:
    proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=60)
    return Completed(proc.returncode, proc.stdout or "", proc.stderr or "")


_RunFn = Callable[[Sequence[str]], Completed]


@dataclass(frozen=True, slots=True)
class ServiceResult:
    """Outcome of one service operation. Never carries secrets."""

    unit: str
    operation: str
    ok: bool
    installed: bool = True
    active: bool | None = None
    enabled: bool | None = None
    detail: str = ""

    def message(self) -> str:
        """One-line human summary with no sensitive values."""
        if not self.installed:
            return f"{self.unit}: not installed (no unit file; nothing was changed)"
        state = "ok" if self.ok else "failed"
        return f"{self.unit}: {self.operation} {state}" + (
            f" ({self.detail})" if self.detail else ""
        )


def _looks_not_installed(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _NOT_INSTALLED_MARKERS)


def _parse_active_state(output: str) -> bool | None:
    word = output.strip().split()[0].lower() if output.strip() else ""
    if word in ("active", "activating", "reloading"):
        return True
    if word in ("inactive", "failed", "deactivating"):
        return False
    return None


def _invoke(argv: Sequence[str], run: _RunFn | None) -> Completed:
    runner = run or _default_run
    return runner(list(argv))


def _op(
    operation: str,
    unit: str,
    argv: Sequence[str],
    run: _RunFn | None = None,
) -> ServiceResult:
    try:
        proc = _invoke(argv, run)
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        return ServiceResult(
            unit,
            operation,
            False,
            installed=True,
            detail=f"systemctl unavailable: {type(exc).__name__}",
        )
    combined = f"{proc.stdout}\n{proc.stderr}"
    if _looks_not_installed(combined):
        return ServiceResult(unit, operation, False, installed=False, detail="not installed")
    if proc.returncode == 0:
        return ServiceResult(unit, operation, True, installed=True, detail="")
    first_line = combined.strip().splitlines()
    return ServiceResult(
        unit,
        operation,
        False,
        installed=True,
        detail=first_line[0][:160] if first_line else f"exit {proc.returncode}",
    )


def start(unit: str, *, run: _RunFn | None = None) -> ServiceResult:
    """Start a unit via ``systemctl start``. No confirmation required."""
    return _op("start", unit, ("systemctl", "start", unit), run)


def stop(unit: str, *, run: _RunFn | None = None) -> ServiceResult:
    """Stop a unit via ``systemctl stop`` (SIGTERM/drain).

    Never requires confirmation and never flattens positions or cancels
    broker-held protective stops; no trading command is issued.
    """
    return _op("stop", unit, ("systemctl", "stop", unit), run)


def restart(unit: str, *, run: _RunFn | None = None) -> ServiceResult:
    """Restart a unit via ``systemctl restart``. No confirmation required."""
    return _op("restart", unit, ("systemctl", "restart", unit), run)


def enable(unit: str, *, run: _RunFn | None = None) -> ServiceResult:
    """Enable a unit via ``systemctl enable``. No confirmation required."""
    return _op("enable", unit, ("systemctl", "enable", unit), run)


def disable(unit: str, *, run: _RunFn | None = None) -> ServiceResult:
    """Disable a unit via ``systemctl disable``. No confirmation required."""
    return _op("disable", unit, ("systemctl", "disable", unit), run)


def is_active(unit: str, *, run: _RunFn | None = None) -> ServiceResult:
    """Report whether a unit is active. Never requires confirmation."""
    try:
        proc = _invoke(("systemctl", "is-active", unit), run)
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return ServiceResult(unit, "is-active", False, installed=True)
    combined = f"{proc.stdout}\n{proc.stderr}"
    if _looks_not_installed(combined):
        return ServiceResult(unit, "is-active", False, installed=False, detail="not installed")
    line = proc.stdout.strip().splitlines()[0][:80] if proc.stdout.strip() else ""
    return ServiceResult(
        unit,
        "is-active",
        proc.returncode == 0,
        installed=True,
        active=_parse_active_state(proc.stdout),
        detail=line,
    )


def is_enabled(unit: str, *, run: _RunFn | None = None) -> ServiceResult:
    """Report whether a unit is enabled. Never requires confirmation."""
    try:
        proc = _invoke(("systemctl", "is-enabled", unit), run)
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return ServiceResult(unit, "is-enabled", False, installed=True)
    combined = f"{proc.stdout}\n{proc.stderr}"
    if _looks_not_installed(combined):
        return ServiceResult(unit, "is-enabled", False, installed=False, detail="not installed")
    word = proc.stdout.strip().splitlines()[0].lower() if proc.stdout.strip() else ""
    return ServiceResult(
        unit,
        "is-enabled",
        proc.returncode == 0,
        installed=True,
        enabled=word == "enabled",
        detail=word[:80],
    )


def status(unit: str, *, run: _RunFn | None = None) -> ServiceResult:
    """Summarize unit state (active/enabled) without sensitive output.

    Never requires confirmation. Returns ``installed=False`` with a plain
    "not installed" detail when the unit file is absent.
    """
    active = is_active(unit, run=run)
    if not active.installed:
        return ServiceResult(unit, "status", False, installed=False, detail="not installed")
    enabled = is_enabled(unit, run=run)
    parts: list[str] = ["active" if active.active else "inactive"]
    if enabled.installed and enabled.enabled is not None:
        parts.append("enabled" if enabled.enabled else "disabled")
    return ServiceResult(
        unit,
        "status",
        active.ok,
        installed=True,
        active=active.active,
        enabled=enabled.enabled,
        detail=", ".join(parts),
    )
