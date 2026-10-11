"""Console command gateway: REST endpoints over the existing durable ControlInbox.

Design contract (this module adds no new command system):

1. Single authoritative path.  Every POST here builds a
   :class:`~mike_trade.controls.commands.ControlCommand` and submits it through
   the existing :class:`~mike_trade.controls.inbox.ControlInbox` — the same
   durable, idempotent inbox and ``operator_commands``/``domain_events`` storage
   the CLI uses.  No second queue, no broker access, no execution here: the
   engine is responsible for draining executable commands.
2. Truthful statuses.  The response carries the command's durable outcome
   (PENDING / APPLIED / REJECTED / EXPIRED) exactly as recorded.  Executable
   commands are PENDING until the engine's ``ControlInbox.process_pending``
   drains them; a command type without an engine handler is REJECTED as
   ``unsupported`` by the inbox itself — this gateway never reports an
   executed position change.  STATE commands (PAUSE/RESUME/DISABLE_ENTRIES/
   ENABLE_ENTRIES) apply immediately at submission, engine online or not.
3. Authentication.  Command-changing requests require the pre-shared token
   from ``MIKE_TRADE_CONSOLE_TOKEN`` (never hardcoded, never returned).  Reads
   are unrestricted; this is a small personal operator console, not a
   user-management platform.  LIVE confirmation semantics stay inside the
   inbox: risk-loosening commands on LIVE scopes require ``confirm_live``.
4. Mounting (Wave 2 integration, intentionally not done here because
   ``server.py`` is owned by another agent)::

       from mike_trade.dashboard.v2.controls import (
           create_controls_gateway, create_controls_v2_router,
       )

       gateway = create_controls_gateway(db_path, settings, clock)
       token = os.environ.get("MIKE_TRADE_CONSOLE_TOKEN", "")
       app.include_router(create_controls_v2_router(gateway, token=token))

   The integrating server must also relax its GET-only middleware for exactly
   these POST paths.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from mike_trade.controls.commands import (
    CommandExpiredError,
    CommandOutcome,
    CommandStatus,
    ControlCommand,
    ControlCommandError,
    ControlCommandType,
    EngineOfflineError,
    IdempotencyConflictError,
    LiveConfirmationRequiredError,
)
from mike_trade.controls.inbox import (
    ControlInbox,
    ControlState,
    load_control_state,
)
from mike_trade.domain.accounts import AccountEnvironment
from mike_trade.domain.errors import ModeIsolationError
from mike_trade.persistence.readstore import ReadStore, ReadStoreUnavailableError
from mike_trade.persistence.schema import from_db_timestamp
from mike_trade.persistence.transaction import WriterSession
from mike_trade.runtime import ClockPort, SystemClock

__all__ = [
    "ControlCommandGateway",
    "HeartbeatLiveness",
    "create_controls_gateway",
    "create_controls_v2_router",
]

_TOKEN_ENV = "MIKE_TRADE_CONSOLE_TOKEN"


# --------------------------------------------------------------------------- models


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class ClosePositionRequest(_Request):
    account_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1)
    actor: str = Field(default="console-operator", min_length=1)
    confirm_live: bool = False

    @field_validator("symbol")
    @classmethod
    def _clean_symbol(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned or any(ch.isspace() for ch in cleaned):
            raise ValueError("symbol must be a ticker without whitespace")
        return cleaned


class AccountCommandRequest(_Request):
    account_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1)
    actor: str = Field(default="console-operator", min_length=1)
    confirm_live: bool = False


class CancelOrderRequest(_Request):
    account_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    client_order_id: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1)
    actor: str = Field(default="console-operator", min_length=1)
    confirm_live: bool = False


class CommandResponse(_Response):
    command_id: str
    idempotency_key: str
    account_id: str
    command_type: str
    status: Literal["PENDING", "APPLIED", "REJECTED", "EXPIRED"]
    detail: str
    duplicate: bool
    issued_at: datetime
    decided_at: datetime | None = None
    expires_at: datetime | None = None


class CommandSummary(_Response):
    command_id: str
    idempotency_key: str
    command_type: str
    symbol: str | None = None
    reason: str | None = None
    actor: str | None = None
    live_confirmed: bool = False
    status: Literal["PENDING", "APPLIED", "REJECTED", "EXPIRED"]
    detail: str
    issued_at: datetime
    decided_at: datetime | None = None


class CommandListResponse(_Response):
    account_id: str
    server_time: datetime
    commands: list[CommandSummary]


class ControlStateResponse(_Response):
    account_id: str
    paused: bool
    entries_disabled: bool
    allows_new_entries: bool
    allows_strategy_exits: bool
    updated_at: datetime | None = None
    last_command_id: str | None = None
    engine_online: bool
    server_time: datetime


# --------------------------------------------------------------------- liveness


class HeartbeatLiveness:
    """Engine liveness from the persisted ``service_health`` heartbeat (read-only).

    Mirrors the dashboard rule: a scope is online while its engine heartbeat
    exists and its age is within ``offline_after_seconds``.
    """

    def __init__(self, db_path: str | Path, clock: ClockPort,
                 offline_after_seconds: float = 60) -> None:
        self._db_path = db_path
        self._clock = clock
        self._offline_after = float(offline_after_seconds)

    def is_engine_online(self, account_scope_id: str) -> bool:
        try:
            store = ReadStore.open(self._db_path)
        except ReadStoreUnavailableError:
            return False
        try:
            with store.snapshot(account_scope_id) as conn:
                row = conn.execute(
                    "SELECT heartbeat_at FROM service_health "
                    "WHERE account_scope_id = ? AND component = 'engine'",
                    (account_scope_id,),
                ).fetchone()
        finally:
            store.close()
        if row is None or row[0] is None:
            return False
        try:
            heartbeat = from_db_timestamp(str(row[0]))
        except ValueError:
            return False
        now = self._clock.now()
        return (now - heartbeat).total_seconds() <= self._offline_after


# ---------------------------------------------------------------------- gateway


def _scope_environment(account_scope_id: str) -> AccountEnvironment:
    prefix = account_scope_id.split(":", 1)[0].upper()
    try:
        return AccountEnvironment(prefix)
    except ValueError as error:
        raise ValueError(
            f"account_id {account_scope_id!r} must start with live:, paper: or shadow:"
        ) from error


class ControlCommandGateway:
    """Submits console commands to the engine's durable ControlInbox."""

    def __init__(
        self,
        db_path: str | Path,
        clock: ClockPort | None = None,
        *,
        engine_liveness: Any | None = None,
        offline_after_seconds: float = 60,
        executable_max_age_seconds: float = 30,
        config_applier: Any | None = None,
    ) -> None:
        self._db_path = str(db_path)
        self._clock = clock or SystemClock()
        self._offline_after_seconds = float(offline_after_seconds)
        self.liveness = engine_liveness or HeartbeatLiveness(
            self._db_path, self._clock, offline_after_seconds
        )
        self._inbox = ControlInbox(
            lambda: WriterSession(self._db_path),
            self._clock,
            self.liveness,
            executable_max_age_seconds,
            config_applier=config_applier,
        )

    # ---- submission ------------------------------------------------------

    def submit(self, command: ControlCommand) -> Any:
        return self._inbox.submit(command)

    def build_command(
        self,
        *,
        account_id: str,
        command_type: ControlCommandType,
        idempotency_key: str,
        reason: str,
        actor: str = "console-operator",
        payload: dict[str, Any] | None = None,
        live_confirmed: bool = False,
    ) -> ControlCommand:
        try:
            environment = _scope_environment(account_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        now = self._clock.now()
        try:
            return ControlCommand(
                idempotency_key=idempotency_key,
                account_scope_id=account_id,
                environment=environment,
                command_type=command_type,
                payload=payload or {},
                actor=actor,
                reason=reason,
                live_confirmed=live_confirmed,
                created_at=now if now.tzinfo is not None else now.replace(tzinfo=UTC),
            )
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    # ---- reads -----------------------------------------------------------

    def status(self, account_id: str, command_id: str) -> CommandOutcome | None:
        return self._inbox.get_status(account_id, command_id)

    def recent(self, account_id: str, limit: int = 50) -> list[tuple[ControlCommand, CommandOutcome]]:
        return self._inbox.list_recent(account_id, limit)

    def state(self, account_id: str) -> ControlState:
        try:
            store = ReadStore.open(self._db_path)
        except ReadStoreUnavailableError as error:
            raise HTTPException(status_code=503, detail="database unavailable") from error
        try:
            with store.snapshot(account_id) as conn:
                return load_control_state(conn, account_id)
        finally:
            store.close()

    def engine_online(self, account_id: str) -> bool:
        return bool(self.liveness.is_engine_online(account_id))

    def scope_registered(self, account_id: str) -> bool:
        try:
            store = ReadStore.open(self._db_path)
        except ReadStoreUnavailableError as error:
            raise HTTPException(status_code=503, detail="database unavailable") from error
        try:
            with store.snapshot(account_id) as conn:
                row = conn.execute(
                    "SELECT 1 FROM account_scopes WHERE account_scope_id = ?", (account_id,)
                ).fetchone()
        except sqlite3.DatabaseError as error:
            raise HTTPException(status_code=503, detail=f"database unavailable: {error}") from error
        finally:
            store.close()
        return row is not None


def _outcome_response(gateway: ControlCommandGateway, result: Any) -> CommandResponse:
    command: ControlCommand = result.command
    outcome: CommandOutcome | None = gateway.status(
        command.account_scope_id, command.command_id
    )
    decided_at = outcome.decided_at if outcome is not None else None
    detail = outcome.detail if outcome is not None else ""
    if command.command_type.category.value == "EXECUTABLE" and result.status is CommandStatus.PENDING:
        detail = detail or "queued for the engine's command drain"
    return CommandResponse(
        command_id=command.command_id,
        idempotency_key=command.idempotency_key,
        account_id=command.account_scope_id,
        command_type=command.command_type.value,
        status=result.status.value,
        detail=detail,
        duplicate=result.duplicate,
        issued_at=command.created_at,
        decided_at=decided_at,
        expires_at=command.expires_at,
    )


def _require_registered_scope(gateway: ControlCommandGateway, account_id: str) -> None:
    if not gateway.scope_registered(account_id):
        raise HTTPException(status_code=404, detail=f"unknown account_id: {account_id}")


# ------------------------------------------------------------------ router


def create_controls_v2_router(
    gateway: ControlCommandGateway,
    *,
    token: str | None = None,
    clock: ClockPort | None = None,
) -> APIRouter:
    """Build the /api/v2/controls router.

    ``token`` is the pre-shared command token (normally
    ``os.environ.get("MIKE_TRADE_CONSOLE_TOKEN")``).  When it is empty the
    command endpoints answer 503 until an operator configures a token; GETs
    remain viewable.  The token is compared in constant time and never
    echoed in any response.
    """
    router = APIRouter(prefix="/api/v2/controls", tags=["controls"])
    clock = clock or SystemClock()
    token_value = token or os.environ.get(_TOKEN_ENV, "")

    def authorize(header_token: str | None, authorization: str | None) -> None:
        if not token_value:
            raise HTTPException(
                status_code=503,
                detail="command token not configured (set MIKE_TRADE_CONSOLE_TOKEN)",
            )
        supplied = header_token
        if authorization is not None and authorization.lower().startswith("bearer "):
            supplied = authorization[len("Bearer "):].strip()
        if supplied is None or not secrets.compare_digest(supplied, token_value):
            raise HTTPException(status_code=401, detail="invalid or missing command token")

    def process(
        command: ControlCommand,
        confirm_live: bool = False,
    ) -> CommandResponse:
        if confirm_live and not command.live_confirmed:
            command = ControlCommand(
                idempotency_key=command.idempotency_key,
                account_scope_id=command.account_scope_id,
                environment=command.environment,
                command_type=command.command_type,
                payload=dict(command.payload),
                actor=command.actor,
                reason=command.reason,
                live_confirmed=True,
                created_at=command.created_at,
            )
        try:
            result = gateway.submit(command)
        except LiveConfirmationRequiredError as error:
            raise HTTPException(
                status_code=422,
                detail=f"{error.message} (retry with confirm_live; the refusal is audited)",
            ) from error
        except IdempotencyConflictError as error:
            raise HTTPException(
                status_code=409,
                detail=f"{error.message}; use a new idempotency_key to submit a different command",
            ) from error
        except CommandExpiredError:
            # durably recorded EXPIRED; fall through to the recorded outcome
            outcome = gateway.status(command.account_scope_id, command.command_id)
            if outcome is None:
                raise HTTPException(status_code=422, detail="command expired") from None
            return _recorded_response(gateway, command, outcome)
        except ModeIsolationError as error:
            raise HTTPException(status_code=400, detail=str(getattr(error, "message", error))) from error
        except EngineOfflineError:
            # durably recorded REJECTED (engine offline); return the record
            outcome = gateway.status(command.account_scope_id, command.command_id)
            if outcome is None:
                raise HTTPException(status_code=503, detail="engine offline") from None
            return _recorded_response(gateway, command, outcome)
        except ControlCommandError as error:
            code = getattr(error, "code", "")
            status_code = 404 if code == "scope_not_registered" else 400
            raise HTTPException(status_code=status_code, detail=str(getattr(error, "message", error))) from error
        return _outcome_response(gateway, result)

    def _recorded_response(
        gateway: ControlCommandGateway, command: ControlCommand, outcome: CommandOutcome
    ) -> CommandResponse:
        return CommandResponse(
            command_id=command.command_id,
            idempotency_key=command.idempotency_key,
            account_id=command.account_scope_id,
            command_type=command.command_type.value,
            status=outcome.status.value,
            detail=outcome.detail,
            duplicate=False,
            issued_at=command.created_at,
            decided_at=outcome.decided_at,
            expires_at=command.expires_at,
        )

    # ---- POST (token required) -------------------------------------------

    @router.post("/positions/close", response_model=CommandResponse)
    def close_position(request: ClosePositionRequest,
                       x_console_token: Annotated[str | None, Header()] = None,
                       authorization: Annotated[str | None, Header()] = None) -> Any:
        authorize(x_console_token, authorization)
        _require_registered_scope(gateway, request.account_id)
        command = gateway.build_command(
            account_id=request.account_id,
            command_type=ControlCommandType.CLOSE_POSITION,
            idempotency_key=request.idempotency_key,
            reason=request.reason,
            actor=request.actor,
            payload={"symbol": request.symbol},
            live_confirmed=request.confirm_live,
        )
        return process(command)

    @router.post("/positions/close-all", response_model=CommandResponse)
    def close_all(request: AccountCommandRequest,
                  x_console_token: Annotated[str | None, Header()] = None,
                  authorization: Annotated[str | None, Header()] = None) -> Any:
        authorize(x_console_token, authorization)
        _require_registered_scope(gateway, request.account_id)
        command = gateway.build_command(
            account_id=request.account_id,
            command_type=ControlCommandType.CLOSE_ALL,
            idempotency_key=request.idempotency_key,
            reason=request.reason,
            actor=request.actor,
            live_confirmed=request.confirm_live,
        )
        return process(command)

    @router.post("/positions/flatten", response_model=CommandResponse)
    def flatten(request: AccountCommandRequest,
                x_console_token: Annotated[str | None, Header()] = None,
                authorization: Annotated[str | None, Header()] = None) -> Any:
        authorize(x_console_token, authorization)
        _require_registered_scope(gateway, request.account_id)
        command = gateway.build_command(
            account_id=request.account_id,
            command_type=ControlCommandType.FLATTEN,
            idempotency_key=request.idempotency_key,
            reason=request.reason,
            actor=request.actor,
            live_confirmed=request.confirm_live,
        )
        return process(command)

    @router.post("/orders/cancel", response_model=CommandResponse)
    def cancel_order(request: CancelOrderRequest,
                     x_console_token: Annotated[str | None, Header()] = None,
                     authorization: Annotated[str | None, Header()] = None) -> Any:
        authorize(x_console_token, authorization)
        _require_registered_scope(gateway, request.account_id)
        order_id = request.order_id.strip()
        payload: dict[str, Any] = {"order": order_id}
        if request.client_order_id is not None and request.client_order_id.strip():
            payload["client_order_id"] = request.client_order_id.strip()
        command = gateway.build_command(
            account_id=request.account_id,
            command_type=ControlCommandType.CANCEL_ORDER,
            idempotency_key=request.idempotency_key,
            reason=request.reason,
            actor=request.actor,
            payload=payload,
            live_confirmed=request.confirm_live,
        )
        return process(command)

    @router.post("/pause", response_model=CommandResponse)
    def pause(request: AccountCommandRequest,
              x_console_token: Annotated[str | None, Header()] = None,
              authorization: Annotated[str | None, Header()] = None) -> Any:
        authorize(x_console_token, authorization)
        _require_registered_scope(gateway, request.account_id)
        command = gateway.build_command(
            account_id=request.account_id,
            command_type=ControlCommandType.PAUSE,
            idempotency_key=request.idempotency_key,
            reason=request.reason,
            actor=request.actor,
            live_confirmed=request.confirm_live,
        )
        return process(command)

    @router.post("/resume", response_model=CommandResponse)
    def resume(request: AccountCommandRequest,
               x_console_token: Annotated[str | None, Header()] = None,
               authorization: Annotated[str | None, Header()] = None) -> Any:
        authorize(x_console_token, authorization)
        _require_registered_scope(gateway, request.account_id)
        command = gateway.build_command(
            account_id=request.account_id,
            command_type=ControlCommandType.RESUME,
            idempotency_key=request.idempotency_key,
            reason=request.reason,
            actor=request.actor,
            live_confirmed=request.confirm_live,
        )
        return process(command)

    @router.post("/entries/disable", response_model=CommandResponse)
    def disable_entries(request: AccountCommandRequest,
                        x_console_token: Annotated[str | None, Header()] = None,
                        authorization: Annotated[str | None, Header()] = None) -> Any:
        authorize(x_console_token, authorization)
        _require_registered_scope(gateway, request.account_id)
        command = gateway.build_command(
            account_id=request.account_id,
            command_type=ControlCommandType.DISABLE_ENTRIES,
            idempotency_key=request.idempotency_key,
            reason=request.reason,
            actor=request.actor,
            live_confirmed=request.confirm_live,
        )
        return process(command)

    @router.post("/entries/enable", response_model=CommandResponse)
    def enable_entries(request: AccountCommandRequest,
                       x_console_token: Annotated[str | None, Header()] = None,
                       authorization: Annotated[str | None, Header()] = None) -> Any:
        authorize(x_console_token, authorization)
        _require_registered_scope(gateway, request.account_id)
        command = gateway.build_command(
            account_id=request.account_id,
            command_type=ControlCommandType.ENABLE_ENTRIES,
            idempotency_key=request.idempotency_key,
            reason=request.reason,
            actor=request.actor,
            live_confirmed=request.confirm_live,
        )
        return process(command)

    # ---- GET (viewing unrestricted) --------------------------------------

    @router.get("/commands/{command_id}", response_model=CommandResponse)
    def command_status(
        command_id: str,
        account_id: Annotated[str, Query()],
    ) -> Any:
        try:
            outcome = gateway.status(account_id, command_id)
        except (ControlCommandError, sqlite3.DatabaseError) as error:
            raise HTTPException(status_code=503, detail="database unavailable") from error
        if outcome is None:
            raise HTTPException(status_code=404, detail="unknown command_id for this account")
        row = _row_for(gateway, account_id, command_id)
        return CommandResponse(
            command_id=command_id,
            idempotency_key=row.get("idempotency_key", ""),
            account_id=account_id,
            command_type=row.get("command_type", ""),
            status=outcome.status.value,
            detail=outcome.detail,
            duplicate=False,
            issued_at=row.get("issued_at", outcome.decided_at or clock.now()),
            decided_at=outcome.decided_at,
            expires_at=row.get("expires_at"),
        )

    def _row_for(gateway: ControlCommandGateway, account_id: str, command_id: str) -> dict[str, Any]:
        for command, _ in gateway.recent(account_id, limit=200):
            if command.command_id == command_id:
                return {
                    "idempotency_key": command.idempotency_key,
                    "command_type": command.command_type.value,
                    "issued_at": command.created_at,
                    "expires_at": command.expires_at,
                }
        return {}

    @router.get("/commands", response_model=CommandListResponse)
    def list_commands(
        account_id: Annotated[str, Query()],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> Any:
        try:
            recent = gateway.recent(account_id, limit)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (ControlCommandError, sqlite3.DatabaseError) as error:
            raise HTTPException(status_code=503, detail="database unavailable") from error
        return CommandListResponse(
            account_id=account_id,
            server_time=clock.now(),
            commands=[
                CommandSummary(
                    command_id=command.command_id,
                    idempotency_key=command.idempotency_key,
                    command_type=command.command_type.value,
                    symbol=command.payload.get("symbol"),
                    reason=command.reason,
                    actor=command.actor,
                    live_confirmed=command.live_confirmed,
                    status=outcome.status.value,
                    detail=outcome.detail,
                    issued_at=command.created_at,
                    decided_at=outcome.decided_at,
                )
                for command, outcome in recent
            ],
        )

    @router.get("/state", response_model=ControlStateResponse)
    def control_state(account_id: Annotated[str, Query()]) -> Any:
        try:
            state = gateway.state(account_id)
        except HTTPException:
            raise
        except (ControlCommandError, sqlite3.DatabaseError) as error:
            raise HTTPException(status_code=503, detail="database unavailable") from error
        return ControlStateResponse(
            account_id=account_id,
            paused=state.paused,
            entries_disabled=state.entries_disabled,
            allows_new_entries=state.allows_new_entries,
            allows_strategy_exits=state.allows_strategy_exits,
            updated_at=state.updated_at,
            last_command_id=state.last_command_id,
            engine_online=gateway.engine_online(account_id),
            server_time=clock.now(),
        )

    return router


# ------------------------------------------------------------------ factory


def create_controls_gateway(
    db_path: str | Path,
    clock: ClockPort | None = None,
    *,
    engine_liveness: Any | None = None,
    offline_after_seconds: float = 60,
    executable_max_age_seconds: float = 30,
    config_applier: Any | None = None,
) -> ControlCommandGateway:
    """Build the gateway over ``db_path`` — the same database the engine and
    dashboard use; no second command queue."""
    return ControlCommandGateway(
        db_path,
        clock,
        engine_liveness=engine_liveness,
        offline_after_seconds=offline_after_seconds,
        executable_max_age_seconds=executable_max_age_seconds,
        config_applier=config_applier,
    )
