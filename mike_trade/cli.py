"""Command-line interface entry point for mike_trade.

Read-only operator surface: ``version`` and ``doctor``. No subcommand here
performs network or broker calls, and no default resolves to LIVE.

Any future live-mutating command must require an explicit live confirmation
flag (see the root parser description).
"""

import argparse
import importlib
import os
import sys
from pathlib import Path

import mike_trade
from mike_trade.app import AppSettings
from mike_trade.domain.errors import ConfigurationError, MikeTradeError
from mike_trade.observability.logging import configure_logging, get_logger
from mike_trade.runtime import ExecutionMode

# (pip distribution name, importable module name) for required runtime deps.
REQUIRED_DEPENDENCIES: tuple[tuple[str, str], ...] = (
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("pydantic", "pydantic"),
    ("pydantic-settings", "pydantic_settings"),
    ("httpx", "httpx"),
    ("alpaca-py", "alpaca"),
)

# Credential environment variables are reported as PRESENT/ABSENT only.
# Their values are never printed, logged, or committed.
CREDENTIAL_ENV_VARS: tuple[str, ...] = (
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
)


def _resolve_default_mode() -> ExecutionMode:
    """Resolves the default execution mode; never LIVE.

    Raises:
        ConfigurationError: If the resolved default mode is LIVE.
    """
    mode = AppSettings().mode
    if mode == ExecutionMode.LIVE:
        raise ConfigurationError(
            "Default execution mode must never resolve to LIVE.",
            code="DOCTOR_DEFAULT_MODE_LIVE",
            context={"mode": mode.value},
        )
    return mode


def run_doctor() -> int:
    """Performs local, read-only system diagnostic checks.

    Makes no network or broker calls. Returns 0 when all required checks
    pass, nonzero otherwise.
    """
    logger = get_logger("mike_trade.cli.doctor")
    logger.info("Executing operator doctor diagnostics")

    checks_passed = True

    # 1. Python version check (required).
    py_ver = sys.version_info
    py_ver_str = f"{py_ver.major}.{py_ver.minor}.{py_ver.micro}"
    if py_ver >= (3, 12):
        print(f"[PASS] Python version: {py_ver_str} (>= 3.12)")
    else:
        print(f"[FAIL] Python version: {py_ver_str} (requires >= 3.12)")
        checks_passed = False

    # 2. Package import check (required; this module already imports it).
    print(f"[PASS] mike_trade importable: version {mike_trade.__version__}")

    # 3. Required dependency import checks (required; imports only, no calls).
    for dist_name, module_name in REQUIRED_DEPENDENCIES:
        try:
            importlib.import_module(module_name)
            print(f"[PASS] Dependency '{dist_name}' resolvable (import '{module_name}')")
        except ImportError:
            print(f"[FAIL] Dependency '{dist_name}' not resolvable (import '{module_name}')")
            checks_passed = False

    # 4. State directory: resolvable path (required) and gitignored (required).
    state_path = Path("state").resolve()
    print(f"[PASS] State directory path resolvable: {state_path}")
    gitignore_path = Path(".gitignore")
    gitignored = False
    if gitignore_path.exists():
        lines = {
            line.strip().rstrip("/")
            for line in gitignore_path.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        gitignored = "state" in lines
    if gitignored:
        print("[PASS] State directory 'state/' is gitignored")
    else:
        print("[FAIL] State directory 'state/' is not gitignored")
        checks_passed = False

    # 5. Credential environment variables: presence only, never values.
    # Informational only; absence does not fail the doctor.
    for var_name in CREDENTIAL_ENV_VARS:
        status = "PRESENT" if os.environ.get(var_name) else "ABSENT"
        print(f"[PASS] Credential '{var_name}': {status}")

    # 6. Resolved default mode must not be LIVE (required).
    try:
        mode = _resolve_default_mode()
        print(f"[PASS] Resolved default mode: {mode.value} (never LIVE)")
    except MikeTradeError as exc:
        print(f"[FAIL] Resolved default mode is LIVE: {exc.message}")
        checks_passed = False

    return 0 if checks_passed else 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the operator surface."""
    configure_logging(level="INFO")

    parser = argparse.ArgumentParser(
        prog="mike-trade",
        description=(
            "mike_trade operator CLI for the single-engine trading application. "
            "Any future live-mutating command requires an explicit live "
            "confirmation flag (e.g. --confirm-live) in addition to "
            "live-mode selection; nothing here places orders or contacts "
            "a broker."
        ),
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    subparsers.add_parser("version", help="Print application version")
    subparsers.add_parser("doctor", help="Run local read-only diagnostic checks")

    args = parser.parse_args(argv)

    try:
        if args.subcommand == "version":
            print(mike_trade.__version__)
            return 0
        if args.subcommand == "doctor":
            return run_doctor()
    except MikeTradeError as exc:
        print(f"[FAIL] {exc.message}", file=sys.stderr)
        return 2

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
