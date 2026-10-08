"""Durable, idempotent operator command inbox and the derived control state.

The CLI ``submit``s commands; the engine drains them with ``process_pending``.
Everything is stored in the phase c tables: one append-only ``operator_commands``
row per command plus ``domain_events`` that record intake and the single
terminal outcome.  Nothing is ever updated or deleted: a command's status is the
fold of its events (``ControlCommandReceived`` -> PENDING, then exactly one of
``ControlCommandApplied`` / ``ControlCommandRejected`` / ``ControlCommandExpired``).

Design choices (5, the task's decision budget):
1. Storage and state mapping.  ``record_command`` cannot be reused (it opens its
   own unit of work and models a different command vocabulary), so the inbox
   INSERTs the append-only ``operator_commands`` row itself, inside its unit of
   work.  Expiry, environment and the idempotency key live in the Received event.
   Terminal-event payloads hold no timestamps, so a repeated record is a journal
   duplicate; ``decided_at`` is the event's ``occurred_at``.  STATE commands drive
   ``EntryControlRepository`` joined into the same unit of work: PAUSE/RESUME set
   ``entry_controls.paused`` and DISABLE_ENTRIES/ENABLE_ENTRIES set
   ``kill_switch_enabled``, so the engine's eligibility read already honours them.
2. Retries and refusals.  A resubmitted key returns the stored command and its
   current status with ``duplicate=True`` even when that status is REJECTED or
   EXPIRED (use a new key to try again).  The LIVE confirmation check runs before
   the duplicate lookup, so an unconfirmed submit is never answered positively.
   A refusal is audited as a ``CommandRejected`` event in the separate
   ``control_audit`` stream (never a command row), so it can never become PENDING.
3. Executable expiry.  The effective ``expires_at`` is the earlier of the caller's
   value and ``created_at + executable_max_age_seconds``.  An executable already
   past it at submit is recorded EXPIRED and raises ``CommandExpiredError``.
   ``expires_at`` is ignored for STATE and CONFIG commands.
4. CONFIG commands.  The payload is ``{"mutations": [{"path", "value"} | {"path",
   "remove": true}], "expected_file_sha256"?}`` and ``live_confirmed`` becomes the
   request's ``confirm_live``.  Applying needs the optional ``config_applier``
   callable (``apply_config_mutation`` bound to its path, validator, exposure,
   audit sink and clock).  Offline, the intent is committed PENDING first, the
   applier runs in the submitting process, then APPLIED/REJECTED is committed in a
   second unit of work (the file write cannot roll back with the database).  With
   no applier the command stays PENDING for the engine, which also falls back to
   the applier when ``handlers`` lacks APPLY_CONFIG_MUTATION.  Refusals put the
   mutation's reasons in ``detail``.
5. Handler contract.  ``handler(command)`` returns ``None`` or ``str`` (APPLIED
   with that detail) or a ``CommandOutcome`` (its APPLIED/REJECTED status and
   detail are recorded); an exception becomes REJECTED with the class name.
   Handlers run outside any session or unit of work, so they may open their own
   writer session.  A crash between a handler and its outcome write leaves the
   command PENDING, so handlers must be idempotent on ``command_id``; executables
   stop being eligible once expired.  ``process_pending`` covers every scope in
   the database unless ``account_scope_id`` is given.

The writer-session factory must return a ``WriterSession`` used as a context
manager; the inbox closes it after each operation and never holds a session while
calling liveness, a handler, or the config applier.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from mike_trade.config.mutations import (
    REMOVE,
    ConfigMutation,
    ConfigMutationError,
    MutationRequest,
    MutationResult,
    UnsafeConfigMutationError,
)
from mike_trade.controls.commands import (
    CommandCategory,
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
from mike_trade.domain.accounts import AccountEnvironment
from mike_trade.domain.errors import ModeIsolationError
from mike_trade.persistence.journal import (
    DomainEventJournal,
    NewEvent,
    RecordedEvent,
    canonical_payload_json,
)
from mike_trade.persistence.repositories import EntryControlRepository
from mike_trade.persistence.schema import from_db_timestamp, to_db_timestamp
from mike_trade.persistence.transaction import ScopedWriter, WriterSession

__all__ = [
    "EngineLiveness",
    "SubmitResult",
    "ControlInbox",
    "ControlState",
    "load_control_state",
]

_STREAM = "control_command"
_AUDIT_STREAM = "control_audit"
_RECEIVED = "ControlCommandReceived"
_REFUSED = "CommandRejected"
_EVENT_BY_STATUS = {
    CommandStatus.APPLIED: "ControlCommandApplied",
    CommandStatus.REJECTED: "ControlCommandRejected",
    CommandStatus.EXPIRED: "ControlCommandExpired",
}
_STATUS_BY_EVENT = {event: status for status, event in _EVENT_BY_STATUS.items()}
_MAX_LIST_LIMIT = 200
_ROW_COLUMNS = (
    "command_id, account_scope_id, command_kind, reason, issued_by, live_confirmed, "
    "payload_json, issued_at"
)
_Row = tuple[Any, ...]
Handler = Callable[[ControlCommand], "str | CommandOutcome | None"]


class EngineLiveness(Protocol):
    """Whether the engine for a scope is alive (later backed by heartbeat age)."""

    def is_engine_online(self, account_scope_id: str) -> bool: ...


@dataclass(frozen=True)
class SubmitResult:
    command: ControlCommand
    status: CommandStatus
    duplicate: bool


@dataclass(frozen=True)
class ControlState:
    """Operator entry permission for one scope, derived from durable state.

    The operator can pause the system or disable new entries and nothing else
    here.  By construction no field or property of this class can block
    protective stops, fills, reconciliation, manual closes, or safety exits:
    ``allows_strategy_exits`` concerns only discretionary strategy exits, and is
    the only exit-related answer this class gives.
    """

    paused: bool
    entries_disabled: bool
    updated_at: datetime | None
    last_command_id: str | None

    @property
    def allows_new_entries(self) -> bool:
        return not (self.paused or self.entries_disabled)

    @property
    def allows_strategy_exits(self) -> bool:
        return not self.paused


def _connection(source: Any) -> Any:
    """Accept a connection-like object or anything exposing ``.connection``."""
    return getattr(source, "connection", source)


def load_control_state(read_conn_or_session: Any, account_scope_id: str) -> ControlState:
    """Read a scope's control state; a never-set scope is not paused, entries enabled."""
    if not isinstance(account_scope_id, str) or not account_scope_id.strip():
        raise ValueError("account_scope_id must be a non-empty str")
    conn = _connection(read_conn_or_session)
    row = conn.execute(
        "SELECT paused, kill_switch_enabled, updated_at FROM entry_controls "
        "WHERE account_scope_id = ?",
        (account_scope_id,),
    ).fetchone()
    if row is None:
        return ControlState(False, False, None, None)
    last = conn.execute(
        "SELECT json_extract(payload_json, '$.command_id') FROM domain_events "
        "WHERE account_scope_id = ? AND stream_type = ? AND event_type = ? "
        "AND json_extract(payload_json, '$.category') = ? ORDER BY event_seq DESC LIMIT 1",
        (
            account_scope_id,
            _STREAM,
            _EVENT_BY_STATUS[CommandStatus.APPLIED],
            CommandCategory.STATE.value,
        ),
    ).fetchone()
    return ControlState(
        paused=bool(row[0]),
        entries_disabled=bool(row[1]),
        updated_at=from_db_timestamp(row[2]),
        last_command_id=None if last is None else last[0],
    )


class _JoinedWriter(ScopedWriter):
    """A scoped writer whose ``unit_of_work`` joins the caller's open transaction."""

    def unit_of_work(self) -> AbstractContextManager[sqlite3.Connection]:
        return nullcontext(self.connection)


def _scope_environment(account_scope_id: str) -> str:
    return account_scope_id.split(":", 1)[0].upper()


def _fold(journal: DomainEventJournal, row: _Row) -> tuple[ControlCommand, CommandOutcome] | None:
    """Rebuild a command and its status from its row and events; None if not inbox-made."""
    command_id, scope, kind, reason, actor, confirmed, payload_json, issued_at = row
    events = journal.read_stream(scope, _STREAM, command_id)
    received = next((event for event in events if event.event_type == _RECEIVED), None)
    if received is None:
        return None
    meta = received.payload
    expires_at = meta["expires_at"]
    command = ControlCommand(
        command_id=command_id,
        idempotency_key=meta["idempotency_key"],
        account_scope_id=scope,
        environment=AccountEnvironment(meta["environment"]),
        command_type=ControlCommandType.parse(kind),
        payload=json.loads(payload_json),
        actor=actor,
        reason=reason,
        live_confirmed=bool(confirmed),
        created_at=from_db_timestamp(issued_at),
        expires_at=None if expires_at is None else from_db_timestamp(expires_at),
    )
    outcome = CommandOutcome(command_id, CommandStatus.PENDING, "", None)
    for event in events:
        status = _STATUS_BY_EVENT.get(event.event_type)
        if status is not None:
            outcome = CommandOutcome(
                command_id, status, event.payload.get("detail", ""), event.occurred_at
            )
            break
    return command, outcome


def _build_mutation_request(command: ControlCommand) -> MutationRequest:
    raw = command.payload.get("mutations")
    if not isinstance(raw, list) or not raw:
        raise ValueError("payload.mutations must be a non-empty list")
    mutations: list[ConfigMutation] = []
    for item in raw:
        if not isinstance(item, Mapping) or "path" not in item:
            raise ValueError("each mutation must be a mapping with a path")
        if item.get("remove") is True:
            value: Any = REMOVE
        elif "value" in item:
            value = item["value"]
        else:
            raise ValueError("each mutation needs a value or remove=true")
        mutations.append(ConfigMutation(item["path"], value))
    return MutationRequest(
        mutations=tuple(mutations),
        actor=command.actor,
        reason=command.reason,
        expected_file_sha256=command.payload.get("expected_file_sha256"),
        confirm_live=command.live_confirmed,
    )


class ControlInbox:
    def __init__(
        self,
        writer_session_factory: Callable[[], WriterSession],
        clock: Any,
        engine_liveness: EngineLiveness,
        executable_max_age_seconds: float = 30,
        *,
        config_applier: Callable[[MutationRequest], MutationResult] | None = None,
    ) -> None:
        if executable_max_age_seconds <= 0:
            raise ValueError("executable_max_age_seconds must be positive")
        self._factory = writer_session_factory
        self._clock = clock
        self._liveness = engine_liveness
        self._max_age = timedelta(seconds=executable_max_age_seconds)
        self._config_applier = config_applier

    # ------------------------------------------------------------------ submit

    def submit(self, command: ControlCommand) -> SubmitResult:
        scope = command.account_scope_id
        if _scope_environment(scope) != command.environment.value:
            raise ModeIsolationError(
                f"command environment {command.environment.value!r} differs from scope "
                f"{scope!r}",
                code="command_environment_mismatch",
            )
        command_type = command.command_type
        category = command_type.category
        online = True
        if category is not CommandCategory.STATE:
            online = bool(self._liveness.is_engine_online(scope))
        now = self._now()
        if category is CommandCategory.EXECUTABLE:
            limit = command.created_at + self._max_age
            expires_at = limit if command.expires_at is None else min(command.expires_at, limit)
            command = replace(command, expires_at=expires_at)

        with self._factory() as session:
            writer = session.bind_scope(scope)
            registered = session.connection.execute(
                "SELECT 1 FROM account_scopes WHERE account_scope_id = ?", (scope,)
            ).fetchone()
            if registered is None:
                raise ControlCommandError(
                    f"account scope {scope!r} is not registered", code="scope_not_registered"
                )
            if (
                command.environment is AccountEnvironment.LIVE
                and command_type.loosens_risk
                and not command.live_confirmed
            ):
                with writer.unit_of_work() as conn:
                    DomainEventJournal(conn, clock=self._clock).append_in_transaction(
                        [self._refusal_event(command, now)]
                    )
                raise LiveConfirmationRequiredError(
                    f"{command_type.value} on a LIVE scope requires live_confirmed",
                    code="live_confirmation_required",
                    context={"command_id": command.command_id},
                )
            error: ControlCommandError | None = None
            with writer.unit_of_work() as conn:
                journal = DomainEventJournal(conn, clock=self._clock)
                existing = self._find_by_key(journal, conn, command)
                if existing is not None:
                    stored, outcome = existing
                    return SubmitResult(stored, outcome.status, True)
                self._insert_row(conn, command)
                events = [self._received_event(command, now)]
                status = CommandStatus.PENDING
                detail = ""
                if category is CommandCategory.STATE:
                    journal.append_in_transaction(events)
                    events = []
                    status, detail = CommandStatus.APPLIED, self._apply_state(session, command)
                elif category is CommandCategory.EXECUTABLE:
                    if not online:
                        status, detail = CommandStatus.REJECTED, "engine offline"
                        error = EngineOfflineError(
                            f"engine is offline; {command_type.value} was rejected",
                            code="engine_offline",
                            context={"command_id": command.command_id},
                        )
                    elif command.expires_at is not None and command.expires_at <= now:
                        status, detail = CommandStatus.EXPIRED, "expired"
                        error = CommandExpiredError(
                            f"{command_type.value} expired before it was submitted",
                            code="command_expired",
                            context={"command_id": command.command_id},
                        )
                if status is not CommandStatus.PENDING:
                    events.append(self._terminal_event(command, status, detail, now))
                journal.append_in_transaction(events)
        if error is not None:
            raise error
        if (
            category is CommandCategory.CONFIG
            and not online
            and self._config_applier is not None
        ):
            status, detail = self._run_config(command)
            status = self._record_terminal(command, status, detail).status
        return SubmitResult(command, status, False)

    # ----------------------------------------------------------------- engine

    def process_pending(
        self,
        handlers: Mapping[ControlCommandType, Handler],
        *,
        account_scope_id: str | None = None,
    ) -> list[CommandOutcome]:
        return [
            self._decide(command, handlers) for command in self._load_pending(account_scope_id)
        ]

    def _load_pending(self, account_scope_id: str | None) -> list[ControlCommand]:
        sql = (
            f"SELECT {_ROW_COLUMNS} FROM operator_commands c "
            "WHERE EXISTS (SELECT 1 FROM domain_events r WHERE "
            "r.account_scope_id = c.account_scope_id "
            "AND r.idempotency_key = 'control:' || c.idempotency_key) "
            "AND NOT EXISTS (SELECT 1 FROM domain_events o WHERE "
            "o.account_scope_id = c.account_scope_id "
            "AND o.idempotency_key = 'control-outcome:' || c.command_id)"
        )
        params: tuple[Any, ...] = ()
        if account_scope_id is not None:
            sql += " AND c.account_scope_id = ?"
            params = (account_scope_id,)
        sql += " ORDER BY c.issued_at, c.rowid"
        pending: list[ControlCommand] = []
        with self._factory() as session:
            journal = DomainEventJournal(session.connection, clock=self._clock)
            for row in session.connection.execute(sql, params).fetchall():
                folded = _fold(journal, tuple(row))
                if folded is None:
                    continue
                if folded[0].command_type.category is not CommandCategory.STATE:
                    pending.append(folded[0])
        return pending

    def _decide(
        self, command: ControlCommand, handlers: Mapping[ControlCommandType, Handler]
    ) -> CommandOutcome:
        command_type = command.command_type
        if (
            command_type.category is CommandCategory.EXECUTABLE
            and command.expires_at is not None
            and self._now() > command.expires_at
        ):
            return self._record_terminal(command, CommandStatus.EXPIRED, "expired")
        handler = handlers.get(command_type)
        if handler is not None:
            status, detail = self._invoke(handler, command)
        elif (
            command_type is ControlCommandType.APPLY_CONFIG_MUTATION
            and self._config_applier is not None
        ):
            status, detail = self._run_config(command)
        else:
            status, detail = CommandStatus.REJECTED, "unsupported"
        return self._record_terminal(command, status, detail)

    @staticmethod
    def _invoke(handler: Handler, command: ControlCommand) -> tuple[CommandStatus, str]:
        try:
            result = handler(command)
        except Exception as exc:
            return CommandStatus.REJECTED, type(exc).__name__
        if result is None:
            return CommandStatus.APPLIED, "applied"
        if isinstance(result, str):
            return CommandStatus.APPLIED, result
        if isinstance(result, CommandOutcome) and result.status in (
            CommandStatus.APPLIED,
            CommandStatus.REJECTED,
        ):
            return result.status, result.detail
        return CommandStatus.REJECTED, "invalid handler result"

    def _run_config(self, command: ControlCommand) -> tuple[CommandStatus, str]:
        assert self._config_applier is not None
        try:
            result = self._config_applier(_build_mutation_request(command))
        except UnsafeConfigMutationError as exc:
            return CommandStatus.REJECTED, "; ".join(exc.reasons)
        except Exception as exc:
            if isinstance(exc, ConfigMutationError | ValueError | TypeError):
                return CommandStatus.REJECTED, f"{type(exc).__name__}: {exc}"
            return CommandStatus.REJECTED, type(exc).__name__
        if result.changed_paths:
            return CommandStatus.APPLIED, "changed: " + ", ".join(result.changed_paths)
        return CommandStatus.APPLIED, "no changes"

    # ------------------------------------------------------------------ reads

    def get_status(self, account_scope_id: str, command_id: str) -> CommandOutcome | None:
        with self._factory() as session:
            session.bind_scope(account_scope_id)
            row = session.connection.execute(
                f"SELECT {_ROW_COLUMNS} FROM operator_commands "
                "WHERE account_scope_id = ? AND command_id = ?",
                (account_scope_id, command_id),
            ).fetchone()
            if row is None:
                return None
            folded = _fold(DomainEventJournal(session.connection, clock=self._clock), tuple(row))
        return None if folded is None else folded[1]

    def list_recent(
        self, account_scope_id: str, limit: int = 50
    ) -> list[tuple[ControlCommand, CommandOutcome]]:
        """Newest-first commands with their current outcome."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError(f"limit must be an integer between 1 and {_MAX_LIST_LIMIT}")
        if not 1 <= limit <= _MAX_LIST_LIMIT:
            raise ValueError(f"limit must be an integer between 1 and {_MAX_LIST_LIMIT}")
        with self._factory() as session:
            session.bind_scope(account_scope_id)
            journal = DomainEventJournal(session.connection, clock=self._clock)
            rows = session.connection.execute(
                f"SELECT {_ROW_COLUMNS} FROM operator_commands c "
                "WHERE c.account_scope_id = ? AND EXISTS (SELECT 1 FROM domain_events r WHERE "
                "r.account_scope_id = c.account_scope_id "
                "AND r.idempotency_key = 'control:' || c.idempotency_key) "
                "ORDER BY c.issued_at DESC, c.rowid DESC LIMIT ?",
                (account_scope_id, limit),
            ).fetchall()
            folded = [_fold(journal, tuple(row)) for row in rows]
        return [item for item in folded if item is not None]

    # --------------------------------------------------------------- internals

    def _now(self) -> datetime:
        now_method = getattr(self._clock, "now", None)
        value = now_method() if callable(now_method) else self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ControlCommandError("clock must return an aware datetime", code="bad_clock")
        return value.astimezone(UTC)

    def _find_by_key(
        self, journal: DomainEventJournal, conn: sqlite3.Connection, command: ControlCommand
    ) -> tuple[ControlCommand, CommandOutcome] | None:
        row = conn.execute(
            f"SELECT {_ROW_COLUMNS} FROM operator_commands "
            "WHERE account_scope_id = ? AND idempotency_key = ?",
            (command.account_scope_id, command.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        folded = _fold(journal, tuple(row))
        if (
            folded is None
            or folded[0].command_type is not command.command_type
            or folded[0].payload_sha256 != command.payload_sha256
        ):
            raise IdempotencyConflictError(
                f"idempotency key {command.idempotency_key!r} was already used for a "
                "different command",
                code="idempotency_conflict",
                context={"idempotency_key": command.idempotency_key},
            )
        return folded

    @staticmethod
    def _insert_row(conn: sqlite3.Connection, command: ControlCommand) -> None:
        symbol = command.payload.get("symbol")
        conn.execute(
            "INSERT INTO operator_commands(command_id, account_scope_id, command_kind, symbol, "
            "reason, issued_by, live_confirmed, payload_json, issued_at, processed_at, "
            "idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (
                command.command_id,
                command.account_scope_id,
                command.command_type.value,
                symbol.strip().upper() if isinstance(symbol, str) and symbol.strip() else None,
                command.reason,
                command.actor,
                int(command.live_confirmed),
                canonical_payload_json(command.payload),
                to_db_timestamp(command.created_at),
                command.idempotency_key,
            ),
        )

    def _received_event(self, command: ControlCommand, now: datetime) -> NewEvent:
        return NewEvent(
            account_scope_id=command.account_scope_id,
            stream_type=_STREAM,
            stream_id=command.command_id,
            event_type=_RECEIVED,
            payload={
                "command_id": command.command_id,
                "idempotency_key": command.idempotency_key,
                "command_type": command.command_type.value,
                "category": command.command_type.category.value,
                "environment": command.environment.value,
                "actor": command.actor,
                "reason": command.reason,
                "live_confirmed": command.live_confirmed,
                "payload_sha256": command.payload_sha256,
                "created_at": command.created_at,
                "expires_at": command.expires_at,
            },
            occurred_at=now,
            idempotency_key=f"control:{command.idempotency_key}",
        )

    @staticmethod
    def _terminal_event(
        command: ControlCommand, status: CommandStatus, detail: str, now: datetime
    ) -> NewEvent:
        return NewEvent(
            account_scope_id=command.account_scope_id,
            stream_type=_STREAM,
            stream_id=command.command_id,
            event_type=_EVENT_BY_STATUS[status],
            payload={
                "command_id": command.command_id,
                "command_type": command.command_type.value,
                "category": command.command_type.category.value,
                "status": status.value,
                "detail": detail,
            },
            occurred_at=now,
            idempotency_key=f"control-outcome:{command.command_id}",
        )

    @staticmethod
    def _refusal_event(command: ControlCommand, now: datetime) -> NewEvent:
        return NewEvent(
            account_scope_id=command.account_scope_id,
            stream_type=_AUDIT_STREAM,
            stream_id=command.command_id,
            event_type=_REFUSED,
            payload={
                "command_id": command.command_id,
                "idempotency_key": command.idempotency_key,
                "command_type": command.command_type.value,
                "environment": command.environment.value,
                "actor": command.actor,
                "reason": command.reason,
                "detail": "live confirmation required",
            },
            occurred_at=now,
            idempotency_key=f"control-refused:{command.command_id}",
        )

    def _apply_state(self, session: WriterSession, command: ControlCommand) -> str:
        controls = EntryControlRepository(
            _JoinedWriter(session, command.account_scope_id), self._clock
        )
        command_type = command.command_type
        who, why = command.actor, command.reason
        if command_type is ControlCommandType.PAUSE:
            controls.set_paused(True, updated_by=who, reason=why)
            return "paused"
        if command_type is ControlCommandType.RESUME:
            controls.set_paused(False, updated_by=who, reason=why)
            return "resumed"
        if command_type is ControlCommandType.DISABLE_ENTRIES:
            controls.set_kill_switch(True, updated_by=who, reason=why)
            return "new entries disabled"
        controls.set_kill_switch(False, updated_by=who, reason=why)
        return "new entries enabled"

    def _record_terminal(
        self, command: ControlCommand, status: CommandStatus, detail: str
    ) -> CommandOutcome:
        """Commit the command's single terminal outcome; return what is on record."""
        event = self._terminal_event(command, status, detail, self._now())
        with self._factory() as session:
            writer = session.bind_scope(command.account_scope_id)
            with writer.unit_of_work() as conn:
                recorded: RecordedEvent = DomainEventJournal(
                    conn, clock=self._clock
                ).append_in_transaction([event]).recorded_events[0]
        return CommandOutcome(
            command.command_id,
            _STATUS_BY_EVENT[recorded.event_type],
            recorded.payload.get("detail", ""),
            recorded.occurred_at,
        )
