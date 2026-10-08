"""Phase N integration: native stop protection exercised end to end.

Wires together, against an in-process fake Alpaca transport (no network, no
credentials):

* ``ProtectionCoordinator`` (n.1)
* the Alpaca standalone trailing adapter (n.2)
* fixed-stop coverage reconciliation (n.3)
* ``ProtectionStateRecord`` / ``apply_event`` with the in-memory repository (n.4)

Scenario map: (a) partial-fill trailing replacement, (b) DAY expiry,
(c) fractional eligibility, (d) linked-trailing rejection, (e) placement
failure, (f) offline projection, (g) manual-close overlap, (h) pause/hold
non-interference.
"""

from __future__ import annotations

import inspect
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from mike_trade.brokers.alpaca.trailing import (
    build_trailing_stop_request,
    cancel_trailing_stop,
    parse_trailing_order_state,
    submit_trailing_stop,
)
from mike_trade.domain.trading import BrokerOrderStatus, TimeInForce
from mike_trade.risk.fixed_stop import (
    CoverageState,
    ProtectiveOrderView,
    check_coverage,
)
from mike_trade.risk.protection import ProtectionCoordinator
from mike_trade.risk.stop_policy import (
    BrokerProtectionCapabilities,
    ProtectionPolicy,
    ProtectionMode,
    evaluate_policy,
)
from mike_trade.risk.stop_state import (
    InMemoryProtectionStateStorage,
    ProtectionCondition,
    ProtectionStateRecord,
    ProtectionStateRepository,
    entry_block_reasons,
    to_dashboard_view,
)
from mike_trade.runtime import ExecutionMode

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
D = Decimal

TERMINAL_BROKER_STATUSES = frozenset(
    {"canceled", "cancelled", "filled", "rejected", "expired", "done_for_day"}
)


@pytest.fixture(autouse=True)
def _no_broker_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every test with an empty environment: no credential can be read."""

    monkeypatch.setattr(os, "environ", {})


class Clock:
    def now(self) -> datetime:
        return NOW


def trailing_policy(**overrides: Any) -> ProtectionPolicy:
    values: dict[str, Any] = {
        "mode": ProtectionMode.TRAILING_NATIVE,
        "trail_percent": D("1.25"),
        "time_in_force": TimeInForce.GTC,
    }
    values.update(overrides)
    return ProtectionPolicy(**values)


def trailing_caps(
    *,
    fractional: bool = False,
    paper_proven_fractional: bool = False,
) -> BrokerProtectionCapabilities:
    combination = (
        frozenset({("TRAILING_STOP", TimeInForce.GTC)}) if fractional else frozenset()
    )
    return BrokerProtectionCapabilities(
        supported_order_types=frozenset({"TRAILING_STOP"}),
        paper_proven_order_types=frozenset({"TRAILING_STOP"}),
        standalone_trailing_parameters=frozenset({"TRAIL_PERCENT"}),
        paper_proven_standalone_trailing_parameters=frozenset({"TRAIL_PERCENT"}),
        fractional_combinations=combination,
        paper_proven_fractional_combinations=(
            combination if paper_proven_fractional else frozenset()
        ),
        supported_tifs=frozenset({TimeInForce.GTC}),
        paper_proven_tifs=frozenset({TimeInForce.GTC}),
    )


def alpaca_order_json(**overrides: Any) -> dict[str, Any]:
    """Recorded Alpaca-shaped trailing-stop payload (broker is authoritative)."""

    value: dict[str, Any] = {
        "id": "order-native-trailing-1",
        "client_order_id": "mt-native-trailing-1",
        "status": "accepted",
        "symbol": "AAPL",
        "side": "sell",
        "type": "trailing_stop",
        "order_class": "simple",
        "time_in_force": "gtc",
        "qty": "10",
        "filled_qty": "0",
        "hwm": "105.50",
        "stop_price": "102.30",
        "trail_price": None,
        "trail_percent": "1.25",
        "updated_at": NOW.isoformat(),
    }
    value.update(overrides)
    return value


class FakeAlpacaTransport:
    """In-process fake Alpaca HTTP surface: post/patch/delete only, no I/O."""

    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.patches: list[tuple[str, dict[str, str]]] = []
        self.deletes: list[str] = []
        self.exit_orders: dict[str, dict[str, str]] = {}
        self.fail_next_post: BaseException | None = None
        self._sequence = 0

    def post(
        self, path: str, *, json_body: dict[str, str], params: Any = None
    ) -> dict[str, Any]:
        self.posts.append((path, dict(json_body)))
        if self.fail_next_post is not None:
            failure, self.fail_next_post = self.fail_next_post, None
            raise failure
        self._sequence += 1
        order_id = f"fake-ord-{self._sequence:04d}"
        order = {
            "id": order_id,
            "client_order_id": json_body["client_order_id"],
            "status": "accepted",
            "symbol": json_body["symbol"],
            "side": json_body["side"],
            "type": json_body.get("type", "trailing_stop"),
            "order_class": json_body.get("order_class", "simple"),
            "time_in_force": json_body.get("time_in_force", "gtc"),
            "qty": json_body["qty"],
            "filled_qty": "0",
            # Broker-authored values: the adapter must read, never compute, these.
            "hwm": "105.50",
            "stop_price": "102.30",
            "trail_price": json_body.get("trail_price"),
            "trail_percent": json_body.get("trail_percent"),
            "updated_at": NOW.isoformat(),
        }
        self.orders[order_id] = order
        return dict(order)

    def patch(
        self, path: str, *, json_body: dict[str, str], params: Any = None
    ) -> dict[str, Any]:
        self.patches.append((path, dict(json_body)))
        order_id = path.rsplit("/", 1)[-1]
        order = dict(self.orders[order_id])
        order.update({k: v for k, v in json_body.items() if v is not None})
        order["status"] = "accepted"
        order["updated_at"] = NOW.isoformat()
        self.orders[order_id] = order
        return dict(order)

    def delete(self, path: str, *, params: Any = None) -> None:
        # Record the cancel request but leave the snapshot untouched: the broker
        # confirms asynchronously, which the test drives explicitly.
        self.deletes.append(path)
        return None

    def confirm_cancel(self, order_id: str) -> None:
        self.orders[order_id] = dict(self.orders[order_id], status="canceled")

    def working_sells(self) -> list[dict[str, Any]]:
        return [
            order
            for order in self.orders.values()
            if order.get("side") == "sell"
            and str(order.get("status")).lower() not in TERMINAL_BROKER_STATUSES
        ]


class TrailingBrokerPort:
    """Coordinator broker port: protective legs go through the trailing adapter."""

    def __init__(
        self,
        transport: FakeAlpacaTransport,
        symbol: str,
        policy: ProtectionPolicy,
        capabilities: BrokerProtectionCapabilities,
        mode: ExecutionMode,
    ) -> None:
        self.transport = transport
        self.symbol = symbol
        self.policy = policy
        self.capabilities = capabilities
        self.mode = mode
        self.native_states: list[Any] = []
        self.exit_submissions: list[str] = []
        self._sequence = 0

    def submit_protective_order(self, request: object) -> dict[str, str]:
        self._sequence += 1
        client_order_id = f"mt-{request.cycle_id}-{self._sequence}"  # type: ignore[attr-defined]
        state = submit_trailing_stop(
            self.transport,
            self.symbol,
            request.quantity,  # type: ignore[attr-defined]
            self.policy,
            client_order_id,
            capabilities=self.capabilities,
            execution_mode=self.mode,
        )
        self.native_states.append(state)
        return {"broker_order_id": state.order_id}

    def submit_exit_order(self, request: object) -> dict[str, str]:
        self._sequence += 1
        order_id = f"fake-exit-{self._sequence:04d}"
        self.transport.exit_orders[order_id] = {
            "qty": str(request.quantity),  # type: ignore[attr-defined]
            "reason": str(request.reason),  # type: ignore[attr-defined]
        }
        self.exit_submissions.append(order_id)
        return {"broker_order_id": order_id}

    def cancel_order(self, order_id: str) -> None:
        return cancel_trailing_stop(self.transport, order_id)


def make_coordinator(
    transport: FakeAlpacaTransport,
    policy: ProtectionPolicy,
    capabilities: BrokerProtectionCapabilities,
    mode: ExecutionMode = ExecutionMode.PAPER,
    symbol: str = "AAPL",
) -> tuple[ProtectionCoordinator, TrailingBrokerPort]:
    port = TrailingBrokerPort(transport, symbol, policy, capabilities, mode)
    return ProtectionCoordinator(port, Clock(), policy), port


# (a) Trailing-native partial fill ------------------------------------------------


async def test_a_trailing_native_partial_fill_replaces_without_overlap() -> None:
    transport = FakeAlpacaTransport()
    policy = trailing_policy()
    coordinator, port = make_coordinator(transport, policy, trailing_caps())

    await coordinator.on_confirmed_fill("cyc-a", D("4"), D("100"))
    first_id = coordinator.current_protection("cyc-a").protective_order_id
    assert first_id is not None

    stable_checkpoints: list[tuple[Decimal, Decimal]] = []

    def record_stable(expected_confirmed: Decimal) -> None:
        snapshot = coordinator.current_protection("cyc-a")
        assert snapshot is not None
        working = transport.working_sells()
        # Exactly one working protective SELL, sized to the confirmed fill.
        assert len(working) == 1
        assert D(str(working[0]["qty"])) == expected_confirmed
        assert snapshot.protective_working_quantity == expected_confirmed
        assert snapshot.exit_working_quantity == D("0")
        stable_checkpoints.append(
            (snapshot.protective_working_quantity, snapshot.confirmed_remaining_quantity)
        )
        # Fixed-stop coverage logic agrees: one effective order, fully covered.
        view = ProtectiveOrderView(
            client_order_id=str(working[0]["client_order_id"]),
            quantity=D(str(working[0]["qty"])),
            filled_quantity=D(str(working[0]["filled_qty"])),
            status=BrokerOrderStatus.ACCEPTED,
        )
        coverage = check_coverage(snapshot.confirmed_remaining_quantity, [view])
        assert coverage.state is CoverageState.COVERED

    record_stable(D("4"))

    # Broker hwm/stop_price come from the fake broker payload, never computed locally.
    first_state = port.native_states[-1]
    assert first_state.hwm == D("105.50") == D(str(transport.orders[first_id]["hwm"]))
    assert first_state.stop_price == D("102.30") == D(
        str(transport.orders[first_id]["stop_price"])
    )
    assert first_state.behavior == "continuous_broker_managed"

    await coordinator.on_confirmed_fill("cyc-a", D("10"), D("100"))
    # Replacement is cancel-then-place: the old order stays the single working
    # sell until its cancel is broker-confirmed, so no overlap is possible.
    in_flight = coordinator.current_protection("cyc-a")
    assert in_flight is not None
    assert in_flight.pending_replacement is True
    assert in_flight.protective_order_id == first_id
    assert len(transport.working_sells()) == 1
    assert in_flight.working_sell_quantity <= in_flight.confirmed_remaining_quantity

    transport.confirm_cancel(first_id)
    await coordinator.on_broker_order_update("cyc-a", first_id, "canceled")
    second_id = coordinator.current_protection("cyc-a").protective_order_id
    assert second_id is not None and second_id != first_id
    record_stable(D("10"))

    assert [qty for qty, _ in stable_checkpoints] == [D("4"), D("10")]
    for working_qty, remaining in stable_checkpoints:
        assert working_qty == remaining


# (b) DAY expiry ------------------------------------------------------------------


def test_b_day_expiry_blocks_fresh_entries() -> None:
    record = ProtectionStateRecord(
        cycle_id="cyc-b",
        account_id="ACCT-B",
        scenario_id=None,
        symbol="AAPL",
        mode="TRAILING_NATIVE",
        execution_mode="PAPER",
        broker_order_id="fake-ord-0001",
        client_order_id="mt-cyc-b-1",
        protected_qty=D("10"),
        remaining_position_qty=D("10"),
        stop_price=D("102.30"),
        broker_hwm=D("105.50"),
        tif="DAY",
        condition=ProtectionCondition.ACTIVE,
        last_broker_confirmed_at=NOW,
    )
    repository = ProtectionStateRepository(InMemoryProtectionStateStorage())
    updated = repository.save_event(
        record,
        {
            "type": "expiry",
            "status": "expired",
            "occurred_at": NOW,
            "broker_update_id": "exp-1",
        },
    )
    assert updated.condition is ProtectionCondition.EXPIRED
    assert updated.reason == "PROTECTION_EXPIRED/OVERNIGHT"
    blocks = entry_block_reasons([updated], now=NOW)
    assert blocks != []
    assert any("EXPIRED" in reason or "OVERNIGHT" in reason for reason in blocks)

    # The broker payload mapping agrees: expired/done_for_day mean EXPIRED.
    assert parse_trailing_order_state(alpaca_order_json(status="expired")).status == "EXPIRED"
    assert (
        parse_trailing_order_state(alpaca_order_json(status="done_for_day")).status
        == "EXPIRED"
    )


# (c) Fractional eligibility -------------------------------------------------------


def test_c_fractional_eligibility_gated_by_mode_and_acknowledgement() -> None:
    transport = FakeAlpacaTransport()
    capabilities = trailing_caps(fractional=True, paper_proven_fractional=False)
    acknowledged = trailing_policy(
        allow_fractional_protection=True,
        documented_fractional_risk_ack=(
            "operator accepts fractional protective-order risk in SHADOW/PAPER"
        ),
    )
    coordinator, _ = make_coordinator(transport, acknowledged, capabilities)
    fractional_qty = D("1.5")

    live = coordinator.check_entry_eligibility(
        acknowledged, capabilities, ExecutionMode.LIVE, fractional_qty
    )
    assert live.rejected
    assert live.entry_allowed is False
    assert live.reason_code == "FRACTIONAL_NOT_PAPER_PROVEN"
    # Rejected before entry submission: nothing reached the (fake) broker.
    assert transport.posts == []

    for mode in (ExecutionMode.SHADOW, ExecutionMode.PAPER):
        allowed = coordinator.check_entry_eligibility(
            acknowledged, capabilities, mode, fractional_qty
        )
        assert allowed.allowed
        assert allowed.entry_allowed is True
        assert allowed.risk_notes != ()


# (d) Linked trailing ---------------------------------------------------------------


def test_d_linked_trailing_legs_are_rejected() -> None:
    linked = trailing_policy(linked=True)
    for order_class in ("bracket", "oto", "oco"):
        with pytest.raises(ValueError, match="standalone"):
            build_trailing_stop_request(
                "AAPL", D("1"), linked, "client-1", order_class=order_class
            )
    with pytest.raises(ValueError, match="standalone"):
        build_trailing_stop_request(
            "AAPL", D("1"), trailing_policy(), "client-1", order_class="bracket"
        )
    # The provider-neutral policy gate agrees: no linked trailing capability.
    decision = evaluate_policy(linked, trailing_caps(), ExecutionMode.PAPER, D("1"))
    assert decision.rejected
    assert decision.reason_code == "LINKED_TRAILING_UNSUPPORTED"


# (e) Placement failure ---------------------------------------------------------------


async def test_e_placement_failure_yields_explicit_unprotected_block() -> None:
    transport = FakeAlpacaTransport()
    transport.fail_next_post = RuntimeError("fake broker rejected protective order")
    coordinator, _ = make_coordinator(transport, trailing_policy(), trailing_caps())

    with pytest.raises(RuntimeError, match="protective placement failed"):
        await coordinator.on_confirmed_fill("cyc-e", D("5"), D("100"))

    snapshot = coordinator.current_protection("cyc-e")
    assert snapshot is not None
    assert snapshot.status == "UNPROTECTED_POSITION"
    assert snapshot.protective_order_id is None
    # No concealed gap: the failure reason is recorded and blocks fresh entries.
    assert snapshot.last_error is not None and snapshot.last_error != ""
    assert coordinator.entry_block_reasons == ("UNPROTECTED_POSITION",)
    assert transport.working_sells() == []


# (f) Offline --------------------------------------------------------------------------


def test_f_offline_projection_is_stale_and_never_cancels_broker_order() -> None:
    transport = FakeAlpacaTransport()
    submitted = submit_trailing_stop(
        transport,
        "AAPL",
        D("6"),
        trailing_policy(),
        "mt-cyc-f-1",
        capabilities=trailing_caps(),
        execution_mode=ExecutionMode.PAPER,
    )
    assert submitted.status == "ACTIVE"

    record = ProtectionStateRecord(
        cycle_id="cyc-f",
        account_id="ACCT-F",
        scenario_id=None,
        symbol="AAPL",
        mode="TRAILING_NATIVE",
        execution_mode="PAPER",
        broker_order_id=submitted.order_id,
        client_order_id=submitted.client_order_id,
        protected_qty=D("6"),
        remaining_position_qty=D("6"),
        stop_price=submitted.stop_price,
        broker_hwm=submitted.hwm,
        tif="GTC",
        condition=ProtectionCondition.ACTIVE,
        last_broker_confirmed_at=NOW,
    )
    view = to_dashboard_view(
        record,
        NOW + timedelta(seconds=300),
        heartbeat_age=timedelta(seconds=300),
        offline_after=timedelta(seconds=60),
    )
    assert view["condition"] is ProtectionCondition.OFFLINE
    assert view["stale"] is True
    # Stale last-known broker snapshot stays visible, read from the broker payload.
    assert view["broker_hwm"] == submitted.hwm == D("105.50")
    assert view["stop_price"] == submitted.stop_price == D("102.30")
    assert view["broker_order_id"] == submitted.order_id

    # Shutdown must not cancel the broker-held order: it keeps protecting offline.
    assert transport.deletes == []
    assert str(transport.orders[submitted.order_id]["status"]).lower() == "accepted"


# (g) Manual close overlap ----------------------------------------------------------------


async def test_g_manual_close_serializes_before_exit_without_oversell() -> None:
    transport = FakeAlpacaTransport()
    coordinator, port = make_coordinator(transport, trailing_policy(), trailing_caps())

    await coordinator.on_confirmed_fill("cyc-g", D("10"), D("100"))
    timeline: list[tuple[str, Decimal, Decimal, bool]] = []

    def record(tag: str) -> None:
        snapshot = coordinator.current_protection("cyc-g")
        assert snapshot is not None
        overlap = (
            snapshot.protective_order_id is not None
            and snapshot.exit_order_id is not None
        )
        timeline.append(
            (
                tag,
                snapshot.working_sell_quantity,
                snapshot.confirmed_remaining_quantity,
                overlap,
            )
        )

    record("protected")
    await coordinator.on_exit_requested("cyc-g", D("10"), "MANUAL_CLOSE")
    record("exit-requested")

    mid = coordinator.current_protection("cyc-g")
    assert mid is not None
    protective_id = mid.protective_order_id
    assert protective_id is not None
    # Protection is cancel-confirmed (not merely requested) before the exit sell:
    # no exit order exists while the protective order is still effective.
    assert mid.exit_order_id is None
    assert port.exit_submissions == []

    transport.confirm_cancel(protective_id)
    await coordinator.on_broker_order_update("cyc-g", protective_id, "canceled")
    record("cancel-confirmed")

    done = coordinator.current_protection("cyc-g")
    assert done is not None
    assert done.protective_order_id is None
    assert done.exit_order_id is not None
    assert done.exit_working_quantity == D("10")

    assert timeline != []
    for tag, working_sell, remaining, overlap in timeline:
        assert working_sell <= remaining, tag
        assert not overlap, tag


# (h) Strategy pause and minimum hold -------------------------------------------------------


async def test_h_pause_and_minimum_hold_do_not_delay_protection() -> None:
    for method in (
        "on_confirmed_fill",
        "check_entry_eligibility",
        "on_broker_order_update",
        "on_exit_requested",
    ):
        parameters = inspect.signature(getattr(ProtectionCoordinator, method)).parameters
        assert not [
            name
            for name in parameters
            if "pause" in name.lower()
            or "hold" in name.lower()
            or "delay" in name.lower()
        ], method

    transport = FakeAlpacaTransport()
    coordinator, _ = make_coordinator(transport, trailing_policy(), trailing_caps())
    await coordinator.on_confirmed_fill("cyc-h", D("7"), D("50"))
    # Placement is immediate on the confirmed fill: one submission, fully sized.
    assert len(transport.posts) == 1
    snapshot = coordinator.current_protection("cyc-h")
    assert snapshot is not None
    assert snapshot.protective_working_quantity == D("7")


# Credential / network guard -----------------------------------------------------------------


def test_no_alpaca_credentials_are_read() -> None:
    for variable in (
        "ALPACA_LIVE_KEY_ID",
        "ALPACA_LIVE_SECRET_KEY",
        "ALPACA_PAPER_KEY_ID",
        "ALPACA_PAPER_SECRET_KEY",
        "ALPACA_KEY_ID",
        "ALPACA_SECRET_KEY",
    ):
        assert variable not in os.environ
        assert os.environ.get(variable) is None
