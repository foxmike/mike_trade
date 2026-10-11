"""Targeted tests for the console command gateway (/api/v2/controls).

Every test uses a disposable local SHADOW/PAPER/LIVE database in ``tmp_path``;
no broker calls and never the active DEV database.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mike_trade.dashboard.v2.controls import (
    create_controls_gateway,
    create_controls_v2_router,
)
from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.persistence.repositories import AccountScopeRepository
from mike_trade.persistence.schema import initialize_database
from mike_trade.persistence.transaction import WriterSession

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
TOKEN = "test-console-token"
SHADOW = AccountScope(AccountEnvironment.SHADOW, "ACCT1", "dev")
SHADOW_2 = AccountScope(AccountEnvironment.SHADOW, "ACCT2", "dev")
PAPER = AccountScope(AccountEnvironment.PAPER, "ACCT1")
LIVE = AccountScope(AccountEnvironment.LIVE, "ACCT9")


class FakeClock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current


class FakeLiveness:
    def __init__(self, online: bool = True) -> None:
        self.online = online

    def is_engine_online(self, account_scope_id: str) -> bool:
        return self.online


def make_db(tmp_path: Path, *scopes: AccountScope, name: str = "ops.db") -> Path:
    db_path = tmp_path / name
    initialize_database(db_path, scopes[0].environment, now=NOW).close()
    with WriterSession(db_path) as session:
        for scope in scopes:
            AccountScopeRepository(session.bind_scope(scope.scope_id), FakeClock()).register_scope(
                scope, broker="alpaca"
            )
    return db_path


def make_client(
    db_path: Path,
    *,
    online: bool = True,
    token: str | None = TOKEN,
) -> TestClient:
    gateway = create_controls_gateway(
        db_path, FakeClock(), engine_liveness=FakeLiveness(online)
    )
    app = FastAPI()
    app.include_router(
        create_controls_v2_router(gateway, token=token, clock=gateway._clock)
    )
    return TestClient(app, raise_server_exceptions=False)


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"X-Console-Token": token}


def submit(client: TestClient, path: str, body: dict[str, Any], **headers: Any) -> Any:
    return client.post(path, json=body, headers=headers or auth())


@pytest.fixture
def shadow(tmp_path: Path) -> tuple[TestClient, Path]:
    db_path = make_db(tmp_path, SHADOW, SHADOW_2)
    return make_client(db_path), db_path


# ------------------------------------------------------------------------ auth


def test_command_without_token_is_rejected(shadow: tuple[TestClient, Path]) -> None:
    client, _ = shadow
    response = client.post(
        "/api/v2/controls/pause",
        json={"account_id": SHADOW.scope_id, "idempotency_key": "k1", "reason": "test"},
    )
    assert response.status_code == 401
    assert "token" in response.json()["detail"].lower()


def test_command_with_wrong_token_is_rejected(shadow: tuple[TestClient, Path]) -> None:
    client, _ = shadow
    response = submit(
        client,
        "/api/v2/controls/pause",
        {"account_id": SHADOW.scope_id, "idempotency_key": "k1", "reason": "test"},
        **auth("wrong-token"),
    )
    assert response.status_code == 401


def test_bearer_authorization_header_is_accepted(tmp_path: Path) -> None:
    db_path = make_db(tmp_path, SHADOW)
    client = make_client(db_path)
    response = client.post(
        "/api/v2/controls/pause",
        json={"account_id": SHADOW.scope_id, "idempotency_key": "k1", "reason": "test"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "APPLIED"


def test_unconfigured_token_disables_commands_but_not_reads(tmp_path: Path) -> None:
    db_path = make_db(tmp_path, SHADOW)
    client = make_client(db_path, token="")
    post = client.post(
        "/api/v2/controls/pause",
        json={"account_id": SHADOW.scope_id, "idempotency_key": "k1", "reason": "test"},
        headers=auth(),
    )
    assert post.status_code == 503
    assert "not configured" in post.json()["detail"]
    read = client.get("/api/v2/controls/state", params={"account_id": SHADOW.scope_id})
    assert read.status_code == 200


# --------------------------------------------------------------- state commands


def test_pause_and_resume_change_control_state(shadow: tuple[TestClient, Path]) -> None:
    client, _ = shadow
    paused = submit(
        client,
        "/api/v2/controls/pause",
        {"account_id": SHADOW.scope_id, "idempotency_key": "pause-1", "reason": "operator pause"},
    )
    assert paused.status_code == 200
    body = paused.json()
    assert body["status"] == "APPLIED"
    assert body["command_type"] == "PAUSE"
    assert body["duplicate"] is False

    state = client.get("/api/v2/controls/state", params={"account_id": SHADOW.scope_id}).json()
    assert state["paused"] is True
    assert state["entries_disabled"] is False
    assert state["allows_new_entries"] is False
    assert state["allows_strategy_exits"] is False
    assert state["engine_online"] is True
    assert state["last_command_id"] == body["command_id"]

    resumed = submit(
        client,
        "/api/v2/controls/resume",
        {"account_id": SHADOW.scope_id, "idempotency_key": "resume-1", "reason": "operator resume"},
    )
    assert resumed.json()["status"] == "APPLIED"
    state = client.get("/api/v2/controls/state", params={"account_id": SHADOW.scope_id}).json()
    assert state["paused"] is False and state["allows_new_entries"] is True


def test_disable_and_enable_entries_toggle_kill_switch(shadow: tuple[TestClient, Path]) -> None:
    client, _ = shadow
    disabled = submit(
        client,
        "/api/v2/controls/entries/disable",
        {"account_id": SHADOW.scope_id, "idempotency_key": "kill-1", "reason": "risk"},
    )
    assert disabled.json()["status"] == "APPLIED"
    state = client.get("/api/v2/controls/state", params={"account_id": SHADOW.scope_id}).json()
    assert state["entries_disabled"] is True and state["paused"] is False
    assert state["allows_new_entries"] is False
    # disabling entries never blocks exits
    assert state["allows_strategy_exits"] is True

    enabled = submit(
        client,
        "/api/v2/controls/entries/enable",
        {"account_id": SHADOW.scope_id, "idempotency_key": "kill-off", "reason": "resume"},
    )
    assert enabled.json()["status"] == "APPLIED"
    state = client.get("/api/v2/controls/state", params={"account_id": SHADOW.scope_id}).json()
    assert state["allows_new_entries"] is True


# ------------------------------------------------------------------ executables


def test_close_position_is_queued_pending_with_correct_payload(
    shadow: tuple[TestClient, Path],
) -> None:
    client, db_path = shadow
    response = submit(
        client,
        "/api/v2/controls/positions/close",
        {
            "account_id": SHADOW.scope_id,
            "symbol": " nvda ",
            "idempotency_key": "close-1",
            "reason": "operator discretion",
        },
    )
    assert response.status_code == 200
    body = response.json()
    # never a fake success: the command is durably PENDING for the engine drain
    assert body["status"] == "PENDING"
    assert body["command_type"] == "CLOSE_POSITION"
    assert body["command_id"]
    assert body["detail"] == "queued for the engine's command drain"

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT command_kind, symbol, payload_json, account_scope_id FROM operator_commands "
            "WHERE command_id = ?",
            (body["command_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "CLOSE_POSITION"
    assert row[1] == "NVDA"
    assert '"symbol":"NVDA"' in row[2].replace(" ", "")
    assert row[3] == SHADOW.scope_id


def test_cancel_order_uses_the_existing_payload_vocabulary(
    shadow: tuple[TestClient, Path],
) -> None:
    client, db_path = shadow
    response = submit(
        client,
        "/api/v2/controls/orders/cancel",
        {
            "account_id": SHADOW.scope_id,
            "order_id": "order-123",
            "client_order_id": "client-123",
            "idempotency_key": "cancel-1",
            "reason": "stale order",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "PENDING"

    conn = sqlite3.connect(db_path)
    try:
        payload = conn.execute(
            "SELECT payload_json FROM operator_commands WHERE command_id = ?",
            (response.json()["command_id"],),
        ).fetchone()[0]
    finally:
        conn.close()
    assert '"order":"order-123"' in payload.replace(" ", "")
    assert '"client_order_id":"client-123"' in payload.replace(" ", "")


def test_executable_command_is_durably_rejected_while_engine_offline(
    shadow: tuple[TestClient, Path],
) -> None:
    client, db_path = shadow
    offline = make_client(db_path, online=False)
    response = submit(
        offline,
        "/api/v2/controls/positions/flatten",
        {"account_id": SHADOW.scope_id, "idempotency_key": "flat-1", "reason": "risk"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "REJECTED"
    assert body["detail"] == "engine offline"

    # the rejection is on record, not retried later
    conn = sqlite3.connect(db_path)
    try:
        events = conn.execute(
            "SELECT event_type FROM domain_events WHERE stream_type = 'control_command' "
            "ORDER BY event_seq"
        ).fetchall()
    finally:
        conn.close()
    assert [event[0] for event in events] == ["ControlCommandReceived", "ControlCommandRejected"]

    status = client.get(
        "/api/v2/controls/commands/" + body["command_id"],
        params={"account_id": SHADOW.scope_id},
    )
    assert status.json()["status"] == "REJECTED"


# ----------------------------------------------------------------- idempotency


def test_repeated_idempotency_key_returns_the_stored_command(
    shadow: tuple[TestClient, Path],
) -> None:
    client, db_path = shadow
    first = submit(
        client,
        "/api/v2/controls/pause",
        {"account_id": SHADOW.scope_id, "idempotency_key": "dup-1", "reason": "test"},
    )
    second = submit(
        client,
        "/api/v2/controls/pause",
        {"account_id": SHADOW.scope_id, "idempotency_key": "dup-1", "reason": "test"},
    )
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert second.json()["command_id"] == first.json()["command_id"]
    assert second.json()["status"] == "APPLIED"

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM operator_commands").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


def test_reused_key_with_different_payload_conflicts(shadow: tuple[TestClient, Path]) -> None:
    client, _ = shadow
    first = submit(
        client,
        "/api/v2/controls/pause",
        {"account_id": SHADOW.scope_id, "idempotency_key": "dup-2", "reason": "test"},
    )
    assert first.status_code == 200
    conflict = submit(
        client,
        "/api/v2/controls/positions/close",
        {
            "account_id": SHADOW.scope_id,
            "symbol": "NVDA",
            "idempotency_key": "dup-2",
            "reason": "test",
        },
    )
    assert conflict.status_code == 409
    assert "new idempotency_key" in conflict.json()["detail"]


# ---------------------------------------------------------------- scope isolation


def test_command_history_is_scope_specific(shadow: tuple[TestClient, Path]) -> None:
    client, _ = shadow
    created = submit(
        client,
        "/api/v2/controls/pause",
        {"account_id": SHADOW.scope_id, "idempotency_key": "scoped", "reason": "test"},
    )
    command_id = created.json()["command_id"]

    other_status = client.get(
        "/api/v2/controls/commands/" + command_id,
        params={"account_id": SHADOW_2.scope_id},
    )
    assert other_status.status_code == 404

    other_list = client.get(
        "/api/v2/controls/commands", params={"account_id": SHADOW_2.scope_id}
    ).json()
    assert other_list["commands"] == []

    mine = client.get(
        "/api/v2/controls/commands", params={"account_id": SHADOW.scope_id}
    ).json()
    assert [item["command_id"] for item in mine["commands"]] == [command_id]

    other_state = client.get(
        "/api/v2/controls/state", params={"account_id": SHADOW_2.scope_id}
    ).json()
    assert other_state["paused"] is False


def test_unknown_account_is_404(shadow: tuple[TestClient, Path]) -> None:
    client, _ = shadow
    response = submit(
        client,
        "/api/v2/controls/pause",
        {"account_id": "shadow:NOPE:missing", "idempotency_key": "k", "reason": "test"},
    )
    assert response.status_code == 404
    assert "unknown account_id" in response.json()["detail"]


def test_malformed_account_id_is_refused(shadow: tuple[TestClient, Path]) -> None:
    client, _ = shadow
    response = submit(
        client,
        "/api/v2/controls/pause",
        {"account_id": "no-scope-prefix", "idempotency_key": "k", "reason": "test"},
    )
    assert response.status_code in (404, 422)


# ------------------------------------------------------------- live confirmation


def test_live_resume_requires_confirm_live(tmp_path: Path) -> None:
    db_path = make_db(tmp_path, LIVE, name="live.db")
    client = make_client(db_path)

    refused = submit(
        client,
        "/api/v2/controls/resume",
        {"account_id": LIVE.scope_id, "idempotency_key": "live-1", "reason": "test"},
    )
    assert refused.status_code == 422
    assert "confirm_live" in refused.json()["detail"]

    conn = sqlite3.connect(db_path)
    try:
        commands = conn.execute("SELECT COUNT(*) FROM operator_commands").fetchone()[0]
        audit = conn.execute(
            "SELECT event_type FROM domain_events WHERE stream_type = 'control_audit'"
        ).fetchall()
    finally:
        conn.close()
    # the refusal is audited in the inbox's control_audit stream; no command row
    assert commands == 0
    assert [event[0] for event in audit] == ["CommandRejected"]

    confirmed = submit(
        client,
        "/api/v2/controls/resume",
        {
            "account_id": LIVE.scope_id,
            "idempotency_key": "live-1",
            "reason": "test",
            "confirm_live": True,
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "APPLIED"


def test_live_pause_needs_no_confirmation(tmp_path: Path) -> None:
    db_path = make_db(tmp_path, LIVE, name="live.db")
    client = make_client(db_path)
    response = submit(
        client,
        "/api/v2/controls/pause",
        {"account_id": LIVE.scope_id, "idempotency_key": "live-pause", "reason": "test"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "APPLIED"
    state = client.get("/api/v2/controls/state", params={"account_id": LIVE.scope_id}).json()
    assert state["paused"] is True


# ------------------------------------------------------------------ reads


def test_command_listing_is_newest_first_and_bounded(shadow: tuple[TestClient, Path]) -> None:
    client, _ = shadow
    keys = []
    for index in range(3):
        response = submit(
            client,
            "/api/v2/controls/pause",
            {"account_id": SHADOW.scope_id, "idempotency_key": f"list-{index}", "reason": "test"},
        )
        keys.append(response.json()["command_id"])
    listing = client.get(
        "/api/v2/controls/commands", params={"account_id": SHADOW.scope_id, "limit": 2}
    ).json()
    assert [item["command_id"] for item in listing["commands"]] == keys[::-1][:2]
    assert all(item["status"] in ("APPLIED", "PENDING", "REJECTED", "EXPIRED") for item in listing["commands"])


def test_status_endpoint_returns_pending_until_engine_drains(
    shadow: tuple[TestClient, Path],
) -> None:
    client, _ = shadow
    created = submit(
        client,
        "/api/v2/controls/positions/close-all",
        {"account_id": SHADOW.scope_id, "idempotency_key": "ca-1", "reason": "test"},
    )
    command_id = created.json()["command_id"]
    status = client.get(
        "/api/v2/controls/commands/" + command_id, params={"account_id": SHADOW.scope_id}
    ).json()
    assert status["status"] == "PENDING"
    assert status["command_type"] == "CLOSE_ALL"
    assert status["decided_at"] is None
