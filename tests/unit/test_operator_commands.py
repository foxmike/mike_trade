from __future__ import annotations

import json
import sqlite3
import sys
import types
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from mike_trade import cli
from mike_trade.controls import cli_commands, position_commands


class FrozenClock:
    def now(self):
        return datetime(2026, 1, 2, tzinfo=UTC)


@pytest.fixture(autouse=True)
def reset_services():
    cli_commands.CLI_SERVICES.clear()
    position_commands.POSITION_SERVICES.clear()
    yield
    cli_commands.CLI_SERVICES.clear()
    position_commands.POSITION_SERVICES.clear()


def test_every_canonical_command_parses():
    parser = cli.build_parser()
    for command in cli.CANONICAL_COMMANDS:
        assert parser.parse_args([command]).subcommand == command


def test_stop_does_not_cancel_or_close(capsys):
    class Lifecycle:
        def __init__(self): self.calls = []
        def stop_engine(self, **kwargs): self.calls.append("stop"); return {"stopped": True}
        def cancel(self, *a): self.calls.append("cancel")
        def close(self, *a): self.calls.append("close")
    lifecycle = Lifecycle()
    cli_commands.CLI_SERVICES["stop"] = lifecycle
    assert cli.main(["stop"]) == 0
    assert lifecycle.calls == ["stop"]
    assert "remain broker-protected" in capsys.readouterr().out


def test_pause_only_changes_entry_eligibility():
    class EntryControls:
        def __init__(self): self.calls = []
        def pause_entries(self, **kwargs): self.calls.append("entries-paused")
    controls = EntryControls()
    cli_commands.CLI_SERVICES["entry-controls"] = controls
    assert cli.main(["pause"]) == 0
    assert controls.calls == ["entries-paused"]


def test_live_close_confirmation_guard_then_env_override(monkeypatch, capsys):
    class Engine:
        calls = []
        def is_online(self, scope): self.calls.append(("online", scope)); return True
        def send_and_wait(self, name, **kwargs): self.calls.append((name, kwargs["scope"])); return {"ack": True}
    engine = Engine()
    position_commands.POSITION_SERVICES["engine_control"] = engine
    assert cli.main(["--account", "LIVE:acct", "close", "AAPL"]) == 3
    assert engine.calls == []
    monkeypatch.setenv("CONFIRM_LIVE", "YES")
    assert cli.main(["--account", "LIVE:acct", "close", "AAPL"]) == 0
    assert engine.calls == [("online", "LIVE:acct"), ("close", "LIVE:acct")]


def test_risk_reducing_controls_need_no_live_confirmation():
    calls = []
    controls = types.SimpleNamespace(
        disable_entries=lambda **kw: calls.append(("disable", kw["scope"])),
        disable_live=lambda **kw: calls.append(("live-disable", kw["scope"])),
    )
    cli_commands.CLI_SERVICES["entry-controls"] = controls
    cli_commands.CLI_SERVICES["live-disable"] = controls
    assert cli.main(["--account", "LIVE:acct", "entries-disable"]) == 0
    assert cli.main(["--account", "LIVE:acct", "live-disable"]) != 3
    assert calls[0] == ("disable", "LIVE:acct")


def test_offline_close_does_not_write_queued_intent(tmp_path):
    db = sqlite3.connect(tmp_path / "operator.db")
    db.execute("create table intents (id integer primary key)")
    class Engine:
        def __init__(self, clock, connection):
            self.clock = clock
            self.connection = connection
        def is_online(self, scope): return False
        def send_and_wait(self, *a, **kw):
            self.connection.execute("insert into intents default values")
            raise AssertionError("must not send while offline")
    clock = FrozenClock()
    engine = Engine(clock, db)
    position_commands.POSITION_SERVICES["engine_control"] = engine
    assert cli.main(["close", "AAPL"]) == 4
    assert db.execute("select count(*) from intents").fetchone()[0] == 0
    assert engine.clock.now().year == 2026


def test_live_start_runs_preflight_and_refuses_failure():
    calls = []
    class Preflight:
        def run(self, **kwargs): calls.append("preflight"); return False
    cli_commands.CLI_SERVICES["live-preflight"] = Preflight()
    cli_commands.CLI_SERVICES["live-start"] = lambda **kwargs: calls.append("start")
    assert cli.main(["--account", "LIVE:acct", "--confirm-live", "YES", "live-start"]) == 3
    assert calls == ["preflight"]


def test_account_scope_passed_to_service():
    seen = []
    cli_commands.CLI_SERVICES["status"] = lambda **kwargs: seen.append(kwargs["scope"]) or {"ok": True}
    assert cli.main(["--account", "PAPER:subaccount", "status"]) == 0
    assert seen == ["PAPER:subaccount"]


def test_json_decimal_is_string_and_account_identifier_masked(capsys):
    cli_commands.CLI_SERVICES["status"] = lambda **kwargs: {
        "cash": Decimal("12.30"), "account_id": "private-account"
    }
    assert cli.main(["status", "--format", "json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"account_id": "***", "cash": "12.30"}


@pytest.mark.parametrize("name", ["signals", "evaluations", "events", "logs", "health"])
def test_history_commands_are_lazily_routed(monkeypatch, name):
    module = types.ModuleType("mike_trade.controls.history_commands")
    calls = []
    module.run_history_command = lambda command, args: calls.append((command, args.account)) or 0
    monkeypatch.setitem(sys.modules, "mike_trade.controls.history_commands", module)
    assert cli.main([name]) == 0
    assert calls == [(name, "SHADOW:default")]
