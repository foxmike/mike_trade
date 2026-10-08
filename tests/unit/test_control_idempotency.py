"""Tests for the durable, idempotent control command inbox and control state."""

from __future__ import annotations

import dataclasses
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import yaml

from mike_trade.config.mutations import ConfigAuditEntry, ExposureSnapshot, apply_config_mutation
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
    UnknownCommandTypeError,
)
from mike_trade.controls.inbox import ControlInbox, ControlState, load_control_state
from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.errors import ModeIsolationError
from mike_trade.persistence.repositories import AccountScopeRepository
from mike_trade.persistence.schema import initialize_database
from mike_trade.persistence.transaction import WriterSession

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
PAPER = AccountScope(AccountEnvironment.PAPER, "ACCT1")
PAPER_2 = AccountScope(AccountEnvironment.PAPER, "ACCT2")
SHADOW = AccountScope(AccountEnvironment.SHADOW, "ACCT1", "scenario_a")
LIVE = AccountScope(AccountEnvironment.LIVE, "ACCT9")
T = ControlCommandType
PLUS_ONE = timezone(timedelta(hours=1))


class FakeClock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class FakeLiveness:
    def __init__(self, online: bool = True) -> None:
        self.online = online

    def is_engine_online(self, account_scope_id: str) -> bool:
        return self.online


class Recorder:
    """A handler that remembers every command it was asked to run."""

    def __init__(self, result: Any = None) -> None:
        self.calls: list[ControlCommand] = []
        self.result = result

    def __call__(self, command: ControlCommand) -> Any:
        self.calls.append(command)
        return self.result


class AuditSink:
    def __init__(self) -> None:
        self.entries: list[ConfigAuditEntry] = []

    def record(self, entry: ConfigAuditEntry) -> None:
        self.entries.append(entry)


class Env:
    def __init__(
        self, db_path: Path, scope: AccountScope, clock: FakeClock, liveness: FakeLiveness
    ) -> None:
        self.db_path = db_path
        self.scope = scope
        self.clock = clock
        self.liveness = liveness

    def inbox(self, **kwargs: Any) -> ControlInbox:
        return ControlInbox(
            lambda: WriterSession(self.db_path), self.clock, self.liveness, **kwargs
        )

    def command(
        self,
        command_type: ControlCommandType,
        key: str = "key-1",
        *,
        scope: AccountScope | None = None,
        payload: dict[str, Any] | None = None,
        confirmed: bool = False,
        **overrides: Any,
    ) -> ControlCommand:
        target = scope or self.scope
        fields: dict[str, Any] = {
            "idempotency_key": key,
            "account_scope_id": target.scope_id,
            "environment": target.environment,
            "command_type": command_type,
            "payload": payload or {},
            "actor": "operator",
            "reason": "test",
            "live_confirmed": confirmed,
            "created_at": self.clock.now(),
        }
        fields.update(overrides)
        return ControlCommand(**fields)

    def count(self, table: str) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            conn.close()

    def event_types(self, stream_type: str) -> list[str]:
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT event_type FROM domain_events WHERE stream_type = ? ORDER BY event_seq",
                (stream_type,),
            ).fetchall()
            return [row[0] for row in rows]
        finally:
            conn.close()

    def state(self, scope: AccountScope | None = None) -> ControlState:
        conn = sqlite3.connect(self.db_path)
        try:
            return load_control_state(conn, (scope or self.scope).scope_id)
        finally:
            conn.close()


def make_env(tmp_path: Path, *scopes: AccountScope, name: str = "ops.db") -> Env:
    db_path = tmp_path / name
    initialize_database(db_path, scopes[0].environment, now=NOW).close()
    clock = FakeClock()
    with WriterSession(db_path) as session:
        for scope in scopes:
            AccountScopeRepository(session.bind_scope(scope.scope_id), clock).register_scope(
                scope, broker="alpaca"
            )
    return Env(db_path, scopes[0], clock, FakeLiveness())


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path, PAPER, PAPER_2)


@pytest.fixture
def live_env(tmp_path: Path) -> Env:
    return make_env(tmp_path, LIVE, name="live.db")


# --------------------------------------------------------------------- idempotency


def test_duplicate_submit_returns_stored_command_and_writes_nothing(env: Env) -> None:
    inbox = env.inbox()
    first = inbox.submit(env.command(T.PAUSE, "dup", payload={"note": "a"}))
    commands_before = env.count("operator_commands")
    events_before = env.count("domain_events")

    second = inbox.submit(env.command(T.PAUSE, "dup", payload={"note": "a"}))

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.command.command_id == first.command.command_id
    assert second.status is first.status is CommandStatus.APPLIED
    assert env.count("operator_commands") == commands_before == 1
    assert env.count("domain_events") == events_before


def test_same_key_with_different_payload_or_type_conflicts(env: Env) -> None:
    inbox = env.inbox()
    inbox.submit(env.command(T.PAUSE, "k", payload={"note": "a"}))
    commands_before = env.count("operator_commands")
    events_before = env.count("domain_events")

    with pytest.raises(IdempotencyConflictError):
        inbox.submit(env.command(T.PAUSE, "k", payload={"note": "b"}))
    with pytest.raises(IdempotencyConflictError):
        inbox.submit(env.command(T.DISABLE_ENTRIES, "k", payload={"note": "a"}))

    assert env.count("operator_commands") == commands_before
    assert env.count("domain_events") == events_before


def test_duplicate_of_a_rejected_command_returns_the_stored_rejection(env: Env) -> None:
    env.liveness.online = False
    inbox = env.inbox()
    with pytest.raises(EngineOfflineError):
        inbox.submit(env.command(T.CLOSE_ALL, "offline-key"))

    env.liveness.online = True
    retry = inbox.submit(env.command(T.CLOSE_ALL, "offline-key"))

    assert retry.duplicate is True
    assert retry.status is CommandStatus.REJECTED
    assert env.count("operator_commands") == 1


def test_same_key_is_independent_across_scopes_in_one_database(env: Env) -> None:
    inbox = env.inbox()
    a = inbox.submit(env.command(T.PAUSE, "shared"))
    b = inbox.submit(env.command(T.DISABLE_ENTRIES, "shared", scope=PAPER_2))

    assert a.duplicate is False and b.duplicate is False
    assert env.state(PAPER).paused is True
    assert env.state(PAPER).entries_disabled is False
    assert env.state(PAPER_2).paused is False
    assert env.state(PAPER_2).entries_disabled is True


def test_same_key_is_independent_between_shadow_and_paper_databases(tmp_path: Path) -> None:
    shadow = make_env(tmp_path, SHADOW, name="shadow.db")
    paper = make_env(tmp_path, PAPER, name="paper.db")

    a = shadow.inbox().submit(shadow.command(T.PAUSE, "shared"))
    b = paper.inbox().submit(paper.command(T.DISABLE_ENTRIES, "shared"))

    assert a.duplicate is False and b.duplicate is False
    assert shadow.state().paused is True and shadow.state().entries_disabled is False
    assert paper.state().paused is False and paper.state().entries_disabled is True


def test_command_environment_must_match_its_scope(env: Env) -> None:
    mismatched = env.command(T.PAUSE, "env", environment=AccountEnvironment.SHADOW)

    with pytest.raises(ModeIsolationError):
        env.inbox().submit(mismatched)

    assert env.count("operator_commands") == 0


def test_unregistered_scope_is_refused(env: Env) -> None:
    ghost = AccountScope(AccountEnvironment.PAPER, "GHOST")

    with pytest.raises(ControlCommandError):
        env.inbox().submit(env.command(T.PAUSE, "ghost", scope=ghost))

    assert env.count("operator_commands") == 0


# ------------------------------------------------------------------- state commands


def test_state_commands_apply_immediately_while_engine_is_offline(env: Env) -> None:
    env.liveness.online = False
    inbox = env.inbox()
    assert env.state() == ControlState(False, False, None, None)

    pause = inbox.submit(env.command(T.PAUSE, "p"))
    assert pause.status is CommandStatus.APPLIED
    state = env.state()
    assert state.paused is True and state.entries_disabled is False
    assert state.last_command_id == pause.command.command_id
    assert state.updated_at == NOW

    disable = inbox.submit(env.command(T.DISABLE_ENTRIES, "d"))
    state = env.state()
    assert state.paused is True and state.entries_disabled is True
    assert state.last_command_id == disable.command.command_id
    assert state.allows_new_entries is False

    inbox.submit(env.command(T.RESUME, "r"))
    inbox.submit(env.command(T.ENABLE_ENTRIES, "e"))
    state = env.state()
    assert (state.paused, state.entries_disabled) == (False, False)
    assert state.allows_new_entries is True


def test_state_commands_are_never_pending_for_the_engine(env: Env) -> None:
    inbox = env.inbox()
    inbox.submit(env.command(T.PAUSE, "p"))
    handler = Recorder()

    assert inbox.process_pending({T.PAUSE: handler}) == []
    assert handler.calls == []


def test_control_state_can_be_read_from_a_writer_session(env: Env) -> None:
    env.inbox().submit(env.command(T.PAUSE, "p"))

    with WriterSession(env.db_path) as session:
        assert load_control_state(session, PAPER.scope_id).paused is True
        assert load_control_state(session.connection, PAPER_2.scope_id).paused is False


def test_control_state_semantics() -> None:
    assert ControlState(False, False, None, None).allows_new_entries is True
    assert ControlState(True, False, None, None).allows_new_entries is False
    assert ControlState(False, True, None, None).allows_new_entries is False
    # Disabling entries never stops discretionary exits; only a pause does.
    assert ControlState(False, True, None, None).allows_strategy_exits is True
    assert ControlState(True, False, None, None).allows_strategy_exits is False


def test_control_state_exposes_no_exit_blocking_attribute() -> None:
    fields = {field.name for field in dataclasses.fields(ControlState)}
    public = {name for name in dir(ControlState) if not name.startswith("_")}

    assert fields == {"paused", "entries_disabled", "updated_at", "last_command_id"}
    assert public - fields == {"allows_new_entries", "allows_strategy_exits"}
    for name in public:
        for word in ("stop", "fill", "reconcil", "close", "safety", "protect", "block"):
            assert word not in name
    assert "protective stops" in (ControlState.__doc__ or "")
    assert "safety exits" in (ControlState.__doc__ or "")


# -------------------------------------------------------------- live confirmation


def test_loosening_command_on_live_requires_confirmation(live_env: Env) -> None:
    inbox = live_env.inbox()

    with pytest.raises(LiveConfirmationRequiredError):
        inbox.submit(live_env.command(T.RESUME, "resume-1"))

    assert live_env.count("operator_commands") == 0
    assert live_env.event_types("control_command") == []
    assert live_env.event_types("control_audit") == ["CommandRejected"]
    assert live_env.state().paused is False

    confirmed = inbox.submit(live_env.command(T.RESUME, "resume-1", confirmed=True))
    assert confirmed.status is CommandStatus.APPLIED
    assert confirmed.duplicate is False
    assert live_env.count("operator_commands") == 1


def test_unconfirmed_retry_of_an_accepted_live_command_is_still_refused(live_env: Env) -> None:
    inbox = live_env.inbox()
    inbox.submit(live_env.command(T.ENABLE_ENTRIES, "e", confirmed=True))

    with pytest.raises(LiveConfirmationRequiredError):
        inbox.submit(live_env.command(T.ENABLE_ENTRIES, "e"))


def test_pause_on_live_needs_no_confirmation(live_env: Env) -> None:
    result = live_env.inbox().submit(live_env.command(T.PAUSE, "pause-1"))

    assert result.status is CommandStatus.APPLIED
    assert live_env.state().paused is True


def test_live_executable_needs_confirmation_before_the_offline_check(live_env: Env) -> None:
    live_env.liveness.online = False

    with pytest.raises(LiveConfirmationRequiredError):
        live_env.inbox().submit(live_env.command(T.CLOSE_ALL, "close"))

    assert live_env.count("operator_commands") == 0


def test_paper_loosening_command_needs_no_confirmation(env: Env) -> None:
    assert env.inbox().submit(env.command(T.RESUME, "r")).status is CommandStatus.APPLIED


# --------------------------------------------------------- executable commands


def test_executable_while_offline_is_rejected_and_never_replayed(env: Env) -> None:
    env.liveness.online = False
    inbox = env.inbox()
    command = env.command(T.CLOSE_ALL, "close-all")

    with pytest.raises(EngineOfflineError) as raised:
        inbox.submit(command)

    outcome = inbox.get_status(PAPER.scope_id, command.command_id)
    assert outcome is not None
    assert outcome.status is CommandStatus.REJECTED
    assert outcome.detail == "engine offline"
    assert raised.value.context["command_id"] == command.command_id

    env.liveness.online = True
    handler = Recorder()
    assert inbox.process_pending({T.CLOSE_ALL: handler}) == []
    assert handler.calls == []


def test_executable_gets_an_expiry_of_created_at_plus_max_age(env: Env) -> None:
    inbox = env.inbox(executable_max_age_seconds=30)

    result = inbox.submit(env.command(T.CLOSE_POSITION, "c1", payload={"symbol": "aapl"}))
    capped = inbox.submit(
        env.command(T.FLATTEN, "c2", expires_at=NOW + timedelta(hours=1))
    )
    shorter = inbox.submit(
        env.command(T.RECONCILE_NOW, "c3", expires_at=NOW + timedelta(seconds=5))
    )

    assert result.status is CommandStatus.PENDING
    assert result.command.expires_at == NOW + timedelta(seconds=30)
    assert capped.command.expires_at == NOW + timedelta(seconds=30)
    assert shorter.command.expires_at == NOW + timedelta(seconds=5)
    conn = sqlite3.connect(env.db_path)
    try:
        symbol = conn.execute(
            "SELECT symbol FROM operator_commands WHERE command_id = ?",
            (result.command.command_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert symbol == "AAPL"


def test_expired_executable_is_never_executed(env: Env) -> None:
    inbox = env.inbox()
    command = env.command(T.CLOSE_ALL, "close-all")
    inbox.submit(command)
    env.clock.advance(31)
    handler = Recorder()

    outcomes = inbox.process_pending({T.CLOSE_ALL: handler})

    assert [(o.command_id, o.status) for o in outcomes] == [
        (command.command_id, CommandStatus.EXPIRED)
    ]
    assert handler.calls == []
    stored = inbox.get_status(PAPER.scope_id, command.command_id)
    assert stored is not None and stored.status is CommandStatus.EXPIRED
    assert inbox.process_pending({T.CLOSE_ALL: handler}) == []


def test_executable_inside_its_window_is_executed(env: Env) -> None:
    inbox = env.inbox()
    command = env.command(T.CLOSE_ALL, "close-all")
    inbox.submit(command)
    env.clock.advance(30)
    handler = Recorder("closed")

    outcomes = inbox.process_pending({T.CLOSE_ALL: handler})

    assert [o.status for o in outcomes] == [CommandStatus.APPLIED]
    assert outcomes[0].detail == "closed"
    assert [c.command_id for c in handler.calls] == [command.command_id]


def test_executable_already_expired_at_submit_is_recorded_expired(env: Env) -> None:
    inbox = env.inbox()
    stale = env.command(T.CLOSE_ALL, "stale", created_at=NOW - timedelta(seconds=60))

    with pytest.raises(CommandExpiredError):
        inbox.submit(stale)

    outcome = inbox.get_status(PAPER.scope_id, stale.command_id)
    assert outcome is not None and outcome.status is CommandStatus.EXPIRED
    assert inbox.process_pending({T.CLOSE_ALL: Recorder()}) == []


# ------------------------------------------------------------------ process_pending


def test_each_command_is_applied_exactly_once_across_calls(env: Env) -> None:
    inbox = env.inbox()
    first = env.command(T.CLOSE_POSITION, "a", payload={"symbol": "AAPL"})
    second = env.command(T.CANCEL_ORDER, "b", payload={"order": "1"})
    inbox.submit(first)
    inbox.submit(second)
    close, cancel = Recorder(), Recorder()
    handlers = {T.CLOSE_POSITION: close, T.CANCEL_ORDER: cancel}

    one = inbox.process_pending(handlers)
    two = inbox.process_pending(handlers)

    assert [(o.command_id, o.status) for o in one] == [
        (first.command_id, CommandStatus.APPLIED),
        (second.command_id, CommandStatus.APPLIED),
    ]
    assert two == []
    assert len(close.calls) == 1 and len(cancel.calls) == 1
    conn = sqlite3.connect(env.db_path)
    try:
        outcome_events = conn.execute(
            "SELECT COUNT(*) FROM domain_events WHERE idempotency_key LIKE 'control-outcome:%'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert outcome_events == 2


def test_pending_commands_are_processed_in_created_at_order(env: Env) -> None:
    inbox = env.inbox()
    later = env.command(T.FLATTEN, "later", created_at=NOW + timedelta(seconds=2))
    earlier = env.command(T.FLATTEN, "earlier", created_at=NOW + timedelta(seconds=1))
    inbox.submit(later)
    inbox.submit(earlier)
    handler = Recorder()

    inbox.process_pending({T.FLATTEN: handler})

    assert [c.command_id for c in handler.calls] == [earlier.command_id, later.command_id]


def test_raising_handler_rejects_and_processing_continues(env: Env) -> None:
    inbox = env.inbox()
    bad = env.command(T.CLOSE_ALL, "bad", created_at=NOW)
    good = env.command(T.FLATTEN, "good", created_at=NOW + timedelta(seconds=1))
    inbox.submit(bad)
    inbox.submit(good)

    def explode(command: ControlCommand) -> None:
        raise RuntimeError("boom")

    handler = Recorder()
    outcomes = inbox.process_pending({T.CLOSE_ALL: explode, T.FLATTEN: handler})

    assert [(o.status, o.detail) for o in outcomes] == [
        (CommandStatus.REJECTED, "RuntimeError"),
        (CommandStatus.APPLIED, "applied"),
    ]
    assert len(handler.calls) == 1
    assert inbox.process_pending({T.CLOSE_ALL: explode, T.FLATTEN: handler}) == []


def test_missing_handler_rejects_as_unsupported(env: Env) -> None:
    inbox = env.inbox()
    command = env.command(T.CLEAR_COOLDOWN, "cc", payload={"symbol": "AAPL"})
    inbox.submit(command)

    outcomes = inbox.process_pending({})

    assert [(o.command_id, o.status, o.detail) for o in outcomes] == [
        (command.command_id, CommandStatus.REJECTED, "unsupported")
    ]


def test_handler_may_return_a_rejecting_outcome(env: Env) -> None:
    inbox = env.inbox()
    command = env.command(T.ADOPT_POSITION, "adopt", payload={"symbol": "AAPL"})
    inbox.submit(command)

    def refuse(cmd: ControlCommand) -> CommandOutcome:
        return CommandOutcome(cmd.command_id, CommandStatus.REJECTED, "no such position", None)

    outcomes = inbox.process_pending({T.ADOPT_POSITION: refuse})

    assert (outcomes[0].status, outcomes[0].detail) == (
        CommandStatus.REJECTED,
        "no such position",
    )
    assert env.event_types("control_command")[-1] == "ControlCommandRejected"


def test_handler_with_an_invalid_result_is_rejected(env: Env) -> None:
    inbox = env.inbox()
    inbox.submit(env.command(T.RECONCILE_NOW, "rec"))

    outcomes = inbox.process_pending({T.RECONCILE_NOW: lambda command: 42})

    assert outcomes[0].status is CommandStatus.REJECTED
    assert outcomes[0].detail == "invalid handler result"


def test_process_pending_can_be_limited_to_one_scope(env: Env) -> None:
    inbox = env.inbox()
    mine = env.command(T.FLATTEN, "m")
    other = env.command(T.FLATTEN, "o", scope=PAPER_2)
    inbox.submit(mine)
    inbox.submit(other)
    handler = Recorder()

    inbox.process_pending({T.FLATTEN: handler}, account_scope_id=PAPER_2.scope_id)

    assert [c.command_id for c in handler.calls] == [other.command_id]
    pending = inbox.get_status(PAPER.scope_id, mine.command_id)
    assert pending is not None and pending.status is CommandStatus.PENDING


# ----------------------------------------------------------------- config commands


def write_config(path: Path) -> bytes:
    document = {
        "schema_version": 1,
        "execution": {"account_mode": "paper", "live_trading_enabled": False},
        "symbols": {"AAPL": {"enabled": True, "buy_strategy": "s", "sell_strategy": "s"}},
        "strategies": {"s": {"kind": "momentum"}},
        "reporting": {"level": "summary"},
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path.read_bytes()


def config_applier(path: Path, sink: AuditSink, clock: FakeClock) -> Callable[..., Any]:
    return partial(
        apply_config_mutation,
        path,
        validate=lambda document: None,
        exposure=ExposureSnapshot(),
        audit=sink,
        clock=clock.now,
    )


def test_offline_config_mutation_refused_as_unsafe_ends_rejected(
    env: Env, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.yaml"
    original = write_config(config_path)
    sink = AuditSink()
    env.liveness.online = False
    inbox = env.inbox(config_applier=config_applier(config_path, sink, env.clock))
    command = env.command(
        T.APPLY_CONFIG_MUTATION,
        "cfg-bad",
        payload={"mutations": [{"path": "strategies.s", "remove": True}]},
    )

    result = inbox.submit(command)

    assert result.status is CommandStatus.REJECTED
    outcome = inbox.get_status(PAPER.scope_id, command.command_id)
    assert outcome is not None
    assert outcome.status is CommandStatus.REJECTED
    assert "removing strategy 's' that is still referenced is refused" in outcome.detail
    assert config_path.read_bytes() == original
    assert sink.entries == []
    assert env.event_types("control_command") == [
        "ControlCommandReceived",
        "ControlCommandRejected",
    ]


def test_offline_config_mutation_is_applied_by_the_submitting_process(
    env: Env, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(config_path)
    sink = AuditSink()
    env.liveness.online = False
    inbox = env.inbox(config_applier=config_applier(config_path, sink, env.clock))
    command = env.command(
        T.APPLY_CONFIG_MUTATION,
        "cfg-ok",
        payload={"mutations": [{"path": "reporting.level", "value": "detailed"}]},
    )

    result = inbox.submit(command)

    assert result.status is CommandStatus.APPLIED
    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["reporting"] == {
        "level": "detailed"
    }
    assert [entry.changed_paths for entry in sink.entries] == [("reporting.level",)]
    outcome = inbox.get_status(PAPER.scope_id, command.command_id)
    assert outcome is not None and outcome.detail == "changed: reporting.level"
    assert inbox.process_pending({}) == []


def test_online_config_mutation_waits_for_the_engine(env: Env, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    original = write_config(config_path)
    inbox = env.inbox(config_applier=config_applier(config_path, AuditSink(), env.clock))
    command = env.command(
        T.APPLY_CONFIG_MUTATION,
        "cfg-online",
        payload={"mutations": [{"path": "reporting.level", "value": "detailed"}]},
    )

    result = inbox.submit(command)

    assert result.status is CommandStatus.PENDING
    assert config_path.read_bytes() == original

    handler = Recorder("engine applied")
    outcomes = inbox.process_pending({T.APPLY_CONFIG_MUTATION: handler})
    assert [(o.status, o.detail) for o in outcomes] == [
        (CommandStatus.APPLIED, "engine applied")
    ]


def test_engine_falls_back_to_the_config_applier_without_a_handler(
    env: Env, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.yaml"
    original = write_config(config_path)
    inbox = env.inbox(config_applier=config_applier(config_path, AuditSink(), env.clock))
    inbox.submit(
        env.command(
            T.APPLY_CONFIG_MUTATION,
            "cfg-fallback",
            payload={"mutations": [{"path": "strategies.s", "remove": True}]},
        )
    )

    outcomes = inbox.process_pending({})

    assert outcomes[0].status is CommandStatus.REJECTED
    assert "still referenced" in outcomes[0].detail
    assert config_path.read_bytes() == original


def test_malformed_config_payload_is_rejected_not_raised(env: Env, tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    original = write_config(config_path)
    env.liveness.online = False
    inbox = env.inbox(config_applier=config_applier(config_path, AuditSink(), env.clock))

    result = inbox.submit(env.command(T.APPLY_CONFIG_MUTATION, "cfg-empty", payload={}))

    assert result.status is CommandStatus.REJECTED
    assert config_path.read_bytes() == original


def test_offline_config_without_an_applier_stays_pending(env: Env) -> None:
    env.liveness.online = False
    command = env.command(
        T.APPLY_CONFIG_MUTATION,
        "cfg-wait",
        payload={"mutations": [{"path": "reporting.level", "value": "detailed"}]},
    )

    assert env.inbox().submit(command).status is CommandStatus.PENDING


# ------------------------------------------------------------------------ reads


def test_list_recent_is_newest_first_and_bounded(env: Env) -> None:
    inbox = env.inbox()
    for index in range(3):
        env.clock.advance(1)
        inbox.submit(env.command(T.FLATTEN, f"k{index}"))
    env.clock.advance(1)
    inbox.submit(env.command(T.PAUSE, "other", scope=PAPER_2))

    recent = inbox.list_recent(PAPER.scope_id, limit=2)

    assert [command.idempotency_key for command, _ in recent] == ["k2", "k1"]
    assert all(outcome.status is CommandStatus.PENDING for _, outcome in recent)
    assert len(inbox.list_recent(PAPER.scope_id)) == 3
    with pytest.raises(ValueError):
        inbox.list_recent(PAPER.scope_id, limit=201)
    with pytest.raises(ValueError):
        inbox.list_recent(PAPER.scope_id, limit=0)


def test_get_status_is_scoped_and_unknown_ids_are_none(env: Env) -> None:
    inbox = env.inbox()
    command = env.command(T.PAUSE, "p")
    inbox.submit(command)

    found = inbox.get_status(PAPER.scope_id, command.command_id)
    assert found is not None and found.status is CommandStatus.APPLIED
    assert found.decided_at == NOW
    assert inbox.get_status(PAPER_2.scope_id, command.command_id) is None
    assert inbox.get_status(PAPER.scope_id, "missing") is None


# ----------------------------------------------------------------- append-only


def test_operator_commands_remain_append_only(env: Env) -> None:
    env.inbox().submit(env.command(T.PAUSE, "p"))
    conn = sqlite3.connect(env.db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE operator_commands SET reason = 'edited'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM operator_commands")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM domain_events")
        assert conn.execute("SELECT COUNT(*) FROM operator_commands").fetchone()[0] == 1
    finally:
        conn.close()


# --------------------------------------------------------------- command record


def test_command_type_categories_and_risk_direction() -> None:
    state = {T.PAUSE, T.RESUME, T.DISABLE_ENTRIES, T.ENABLE_ENTRIES}
    executable = {
        T.CLOSE_POSITION,
        T.CLOSE_ALL,
        T.FLATTEN,
        T.CANCEL_ORDER,
        T.ADOPT_POSITION,
        T.RECONCILE_NOW,
        T.RESET_ENTRY_LOCKOUT,
        T.CLEAR_COOLDOWN,
    }
    assert {t for t in T if t.category is CommandCategory.STATE} == state
    assert {t for t in T if t.category is CommandCategory.EXECUTABLE} == executable
    assert T.APPLY_CONFIG_MUTATION.category is CommandCategory.CONFIG
    assert {t for t in T if not t.loosens_risk} == {T.PAUSE, T.DISABLE_ENTRIES}


def test_command_payload_is_canonical_and_hashed() -> None:
    base = {
        "idempotency_key": "k",
        "account_scope_id": PAPER.scope_id,
        "environment": AccountEnvironment.PAPER,
        "command_type": T.FLATTEN,
        "actor": "op",
        "reason": "why",
        "created_at": NOW,
    }
    a = ControlCommand(**base, payload={"qty": Decimal("1.50"), "b": 1, "a": [Decimal("2")]})
    b = ControlCommand(**base, payload={"a": ["2"], "b": 1, "qty": "1.50"})
    c = ControlCommand(**base, payload={"a": ["2"], "b": 1, "qty": "1.5"})

    assert a.payload == {"qty": "1.50", "b": 1, "a": ["2"]}
    assert a.payload_sha256 == b.payload_sha256 != c.payload_sha256
    assert len(a.command_id) == 36
    with pytest.raises(TypeError):
        a.payload["x"] = 1  # type: ignore[index]


@pytest.mark.parametrize(
    "overrides",
    [
        {"idempotency_key": ""},
        {"idempotency_key": "   "},
        {"idempotency_key": "k" * 129},
        {"account_scope_id": " paper:ACCT1"},
        {"environment": AccountEnvironment.BACKTEST},
        {"actor": ""},
        {"reason": ""},
        {"created_at": datetime(2026, 10, 8, 14, 30)},
        {"created_at": datetime(2026, 10, 8, 14, 30, tzinfo=PLUS_ONE)},
        {"expires_at": NOW},
        {"payload": {"price": 1.5}},
        {"payload": {"when": datetime(2026, 1, 1)}},
    ],
)
def test_invalid_command_fields_are_refused(overrides: dict[str, Any]) -> None:
    fields: dict[str, Any] = {
        "idempotency_key": "k",
        "account_scope_id": PAPER.scope_id,
        "environment": AccountEnvironment.PAPER,
        "command_type": T.PAUSE,
        "actor": "op",
        "reason": "why",
        "created_at": NOW,
    }
    fields.update(overrides)

    with pytest.raises(ValueError):
        ControlCommand(**fields)


def test_key_of_exactly_128_characters_is_accepted() -> None:
    command = ControlCommand(
        idempotency_key="k" * 128,
        account_scope_id=PAPER.scope_id,
        environment=AccountEnvironment.PAPER,
        command_type=T.PAUSE,
        actor="op",
        reason="why",
        created_at=NOW,
    )
    assert len(command.idempotency_key) == 128


def test_unknown_command_type_is_refused() -> None:
    with pytest.raises(UnknownCommandTypeError):
        ControlCommand(
            idempotency_key="k",
            account_scope_id=PAPER.scope_id,
            environment=AccountEnvironment.PAPER,
            command_type="DROP_TABLES",  # type: ignore[arg-type]
            actor="op",
            reason="why",
            created_at=NOW,
        )
    assert ControlCommandType.parse("PAUSE") is T.PAUSE
