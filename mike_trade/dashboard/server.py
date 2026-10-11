"""Dashboard process: FastAPI app factory, static frontend hosting, and uvicorn entry.

Design choices (within the task's decision budget):
1. ``create_app`` registers only GET routes, and a middleware answers every
   non-GET/HEAD method with 405 (including unknown paths) so no write surface
   can exist by accident.
2. The compiled frontend is served from ``static_dir`` (default
   ``mike_trade/dashboard/frontend/dist``) with SPA fallback to ``index.html``
   for non-``/api`` paths.  No Vite dev server is proxied or required.  A missing
   build yields a 503 for UI routes only; ``/api`` keeps working.
3. Startup never touches the database or the engine: the read service opens
   SQLite per request, so the dashboard boots with no engine and no heartbeat.
4. Binding defaults to 127.0.0.1.  A non-loopback bind is refused unless
   ``DashboardSettings.allow_non_loopback`` is set (env opt-in
   ``MIKE_TRADE_DASHBOARD_ALLOW_NON_LOOPBACK=1``), and then logs a warning.
"""

from __future__ import annotations

import ipaddress
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import time
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from mike_trade.dashboard.api import (
    DashboardReadService,
    DashboardSettings,
    SymbolSettings,
    create_api_router,
)
from mike_trade.domain.errors import ConfigurationError
from mike_trade.observability.logging import configure_logging, get_logger
from mike_trade.runtime import ClockPort, SystemClock

__all__ = ["create_app", "is_loopback_host", "validate_bind", "main", "DEFAULT_STATIC_DIR"]

DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "frontend" / "dist"
_ALLOWED_METHODS = ("GET", "HEAD")
_BUILD_MISSING = (
    "Dashboard frontend build not found. Build it with the frontend production build "
    "(output: dashboard/frontend/dist); the /api endpoints remain available."
)


def _settings_from_config() -> DashboardSettings:
    """Load dashboard display limits from the same effective config as the engine."""
    from mike_trade.controls.config_commands import load_effective_config

    effective = load_effective_config(environ=os.environ)
    service = cast(dict[str, Any], effective["service"])
    dashboard = cast(dict[str, Any], service["dashboard"])
    market_data = cast(dict[str, Any], effective["market_data"])
    execution = cast(dict[str, Any], effective["execution"])
    risk = cast(dict[str, Any], effective["risk"])
    symbol_config = cast(dict[str, dict[str, Any]], effective["symbols"])
    bind_host = str(dashboard["bind_host"])
    port = int(dashboard["port"])
    cutoff_hour, cutoff_minute = map(int, str(execution["entry_cutoff"]).split(":"))
    symbols = {
        name: SymbolSettings(
            budget=item["budget"],
            planned_loss_cap=item["planned_loss_cap"],
            min_hold_seconds=int(item["min_hold_seconds"]),
            max_entry_cycles_per_session=int(item["max_entry_cycles_per_session"]),
            consecutive_loss_limit=item["consecutive_loss_limit"],
            consecutive_loss_reset=item["consecutive_loss_reset"],
        )
        for name, item in symbol_config.items()
        if item["enabled"]
    }
    backup = os.environ.get("MIKE_TRADE_BACKUP_DIR")
    backup_status = os.environ.get("MIKE_TRADE_BACKUP_STATUS_FILE")
    return DashboardSettings(
        bind_host=bind_host,
        port=port,
        allow_non_loopback=os.environ.get(
            "MIKE_TRADE_DASHBOARD_ALLOW_NON_LOOPBACK", ""
        ) == "1",
        heartbeat_stale_seconds=int(dashboard["heartbeat_stale_seconds"]),
        heartbeat_offline_seconds=int(dashboard["heartbeat_offline_seconds"]),
        max_bar_age_seconds=int(market_data["max_bar_age_seconds"]),
        entry_cutoff=time(cutoff_hour, cutoff_minute),
        total_application_budget=risk.get("total_application_budget"),
        symbols=symbols,
        data_dir=Path(str(service["data_dir"])),
        backup_dir=Path(backup) if backup else None,
        backup_status_file=Path(backup_status) if backup_status else None,
        research_root=Path(os.environ.get("MIKE_TRADE_RESEARCH_ROOT", "research")),
        research_dataset_root=Path(
            os.environ.get("MIKE_TRADE_RESEARCH_DATASET_ROOT", "research/data")
        ),
    )


def is_loopback_host(host: str) -> bool:
    """True for ``localhost`` and loopback IP literals; hostnames otherwise are not loopback."""
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def validate_bind(settings: DashboardSettings) -> None:
    """Refuse a non-loopback bind without explicit opt-in; warn when it is allowed."""
    if is_loopback_host(settings.bind_host):
        return
    if not settings.allow_non_loopback:
        raise ConfigurationError(
            f"refusing to bind dashboard to non-loopback address {settings.bind_host!r}; "
            "set the explicit non-loopback opt-in to allow it",
            code="DASHBOARD_NON_LOOPBACK_REFUSED",
            context={"bind_host": settings.bind_host},
        )
    get_logger("mike_trade.dashboard").warning(
        "dashboard bound to non-loopback address %s; remote exposure must sit behind "
        "private auth/TLS/VPN/reverse proxy (the dashboard has no authentication)",
        settings.bind_host,
    )


def create_app(
    settings: DashboardSettings,
    clock: ClockPort,
    db_path: str | Path,
    static_dir: str | Path | None = None,
) -> FastAPI:
    """Build the read-only dashboard application; performs no I/O."""
    app = FastAPI(title="mike_trade dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    service = DashboardReadService(settings, clock, db_path)
    from mike_trade.dashboard.v2.research import create_research_v2_router

    app.include_router(create_research_v2_router(service))
    app.include_router(create_api_router(service))
    dist = Path(static_dir) if static_dir is not None else DEFAULT_STATIC_DIR

    @app.middleware("http")
    async def read_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in _ALLOWED_METHODS:
            return JSONResponse(
                {"detail": "read-only dashboard: only GET and HEAD are allowed"},
                status_code=405,
                headers={"Allow": ", ".join(_ALLOWED_METHODS)},
            )
        return await call_next(request)

    @app.api_route("/{path:path}", methods=list(_ALLOWED_METHODS), include_in_schema=False)
    def ui(path: str) -> Response:
        index = dist / "index.html"
        if not index.is_file():
            return JSONResponse({"detail": _BUILD_MISSING}, status_code=503)
        root = dist.resolve()
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            headers = (
                {"Cache-Control": "public, max-age=31536000, immutable"}
                if path.startswith("assets/")
                else {}
            )
            return FileResponse(candidate, headers=headers)
        if path.startswith("assets/") or "." in Path(path).name:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


def main(argv: Sequence[str] | None = None) -> int:
    """Run the dashboard under uvicorn as its own process."""
    import uvicorn

    configure_logging(level="INFO")
    settings = _settings_from_config()
    try:
        validate_bind(settings)
    except ConfigurationError as error:
        print(f"[FAIL] {error.message}", file=sys.stderr)
        return 2
    db_path = Path(os.environ.get("MIKE_TRADE_DB_PATH", settings.data_dir / "mike_trade.sqlite3"))
    app = create_app(settings, SystemClock(), db_path)
    uvicorn.run(app, host=settings.bind_host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
