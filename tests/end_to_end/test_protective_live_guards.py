"""Offline end-to-end checks for protection coordination and mode isolation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from mike_trade.app import AlpacaBrokerAdapter, AppSettings, build_runtime
from mike_trade.brokers.alpaca.auth import (
    AlpacaEnvironment,
    EnvironmentMismatchError,
    LiveAuthorizationRequired,
    credential_fingerprint,
    AlpacaCredentials,
)
from mike_trade.brokers.alpaca.capabilities import (
    CapabilityDescriptor,
    CapabilityUnavailable,
    default_alpaca_capabilities,
)
from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.errors import LiveTradingNotAuthorizedError
from mike_trade.risk.protection import ProtectionCoordinator
from mike_trade.risk.stop_policy import (
    BrokerProtectionCapabilities,
    ProtectionMode,
    ProtectionPolicy,
)
from mike_trade.risk.trailing import (
    DistanceKind,
    SteppedTrailingPolicy,
    advance,
    initial_state,
)
from mike_trade.runtime import ExecutionMode
from mike_trade.domain.trading import TimeInForce

D = Decimal
NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)


class Clock:
    def now(self) -> datetime:
        return NOW


class BrokerRecorder:
    """In-memory broker port used by the production coordinator/runtime."""

    def __init__(self, *, fail_cancel: bool = False, fail_protection: bool = False) -> None:
        self.fail_cancel = fail_cancel
        self.fail_protection = fail_protection
        self.calls: list[tuple[str, object]] = []
        self.sequence = 0

    async def submit_protective_order(self, request: object) -> dict[str, str]:
        self.calls.append(("protect", request.quantity))
        if self.fail_protection:
            raise RuntimeError("protection state unknown")
        self.sequence += 1
        return {"broker_order_id": f"stop-{self.sequence}"}

    async def submit_exit_order(self, request: object) -> dict[str, str]:
        self.calls.append(("exit", request.quantity))
        self.sequence += 1
        return {"broker_order_id": f"exit-{self.sequence}"}

    async def cancel_order(self, order_id: str) -> None:
        self.calls.append(("cancel", order_id))
        if self.fail_cancel:
            raise RuntimeError("cancel outcome unknown")


def fixed_policy() -> ProtectionPolicy:
    return ProtectionPolicy(
        mode=ProtectionMode.FIXED, distance_percent=D("5"),
        time_in_force=TimeInForce.GTC,
    )


def caps() -> BrokerProtectionCapabilities:
    return BrokerProtectionCapabilities(
        supported_order_types=frozenset({"STOP_MARKET", "TRAILING_STOP"}),
        paper_proven_order_types=frozenset({"STOP_MARKET", "TRAILING_STOP"}),
        standalone_trailing_parameters=frozenset({"TRAIL_PERCENT"}),
        paper_proven_standalone_trailing_parameters=frozenset({"TRAIL_PERCENT"}),
        supported_tifs=frozenset({TimeInForce.GTC}),
        paper_proven_tifs=frozenset({TimeInForce.GTC}),
    )


@pytest.mark.asyncio
async def test_protection_waits_for_positive_fill_and_tracks_partial_fill_quantity() -> None:
    broker = BrokerRecorder()
    coordinator = ProtectionCoordinator(broker, Clock(), fixed_policy())
    assert await coordinator.on_confirmed_fill("c1", D("0"), D("100"), confirmed=True) is None
    assert await coordinator.on_confirmed_fill("c1", D("2"), D("100"), confirmed=False) is None
    assert broker.calls == []

    first = await coordinator.on_confirmed_fill("c1", D("2"), D("100"), confirmed=True)
    assert first is not None and first.protective_working_quantity == D("2")
    stop_id = first.protective_order_id
    assert stop_id == "stop-1"

    # The entry's cumulative fill grows. Existing protection is canceled, then
    # replaced for the actual cumulative remaining position after confirmation.
    replacement_pending = await coordinator.on_confirmed_fill(
        "c1", D("5"), D("101"), confirmed=True
    )
    assert replacement_pending is not None
    assert replacement_pending.pending_replacement
    assert ("cancel", stop_id) in broker.calls
    replaced = await coordinator.on_broker_order_update("c1", stop_id, "CANCELED", filled_qty=D("0"))
    assert replaced.protective_working_quantity == D("5")
    assert broker.calls[-1] == ("protect", D("5"))


def test_long_stepped_stop_is_monotonic_on_rising_and_falling_observations() -> None:
    policy = SteppedTrailingPolicy(
        distance_kind=DistanceKind.USD, distance_value=D("2"),
        step_kind=DistanceKind.USD, step_value=D("1"),
        min_replace_interval=timedelta(0), time_in_force=TimeInForce.GTC,
        policy_revision="offline-guard",
    )
    state = initial_state(D("100"), policy)
    stops = [state.candidate_stop]
    for high in (D("101"), D("104"), D("99"), D("106"), D("103")):
        state = advance(state, high, policy)
        stops.append(state.candidate_stop)
    assert stops == sorted(stops)
    assert stops[-1] > stops[0]


@pytest.mark.asyncio
async def test_overlapping_discretionary_exit_and_stop_never_submit_two_sells() -> None:
    broker = BrokerRecorder()
    coordinator = ProtectionCoordinator(broker, Clock(), fixed_policy())
    await coordinator.on_confirmed_fill("c2", D("3"), D("50"))
    stop_id = coordinator.current_protection("c2").protective_order_id
    await coordinator.on_exit_requested("c2", D("3"), "manual")
    with pytest.raises((RuntimeError, ValueError), match="already working|exceeds confirmed"):
        await coordinator.on_exit_requested("c2", D("1"), "second manual")

    # If the stop wins the race while cancel is pending, it closes the cycle and
    # the queued discretionary sell is discarded.
    closed = await coordinator.on_broker_order_update("c2", stop_id, "FILLED", filled_qty=D("3"))
    assert closed.confirmed_remaining_quantity == D("0")
    assert not any(call[0] == "exit" for call in broker.calls)
    assert sum(value for name, value in broker.calls if name == "protect") == D("3")


@pytest.mark.asyncio
async def test_unknown_protective_order_state_blocks_new_entries() -> None:
    broker = BrokerRecorder(fail_cancel=True)
    coordinator = ProtectionCoordinator(broker, Clock(), fixed_policy())
    active = await coordinator.on_confirmed_fill("c3", D("1"), D("20"))
    with pytest.raises(RuntimeError, match="protective cancellation failed"):
        await coordinator.on_exit_requested("c3", D("1"), "manual")
    assert coordinator.entry_block_reasons == ("UNPROTECTED_POSITION",)
    assert active is not None and coordinator.current_protection("c3").protective_order_id


def test_fractional_stop_and_trailing_combinations_fail_closed() -> None:
    matrix = default_alpaca_capabilities()
    for descriptor in (
        CapabilityDescriptor("stop", "simple", "fractional", "day", "sell", "submit"),
        CapabilityDescriptor("trailing_stop", "simple", "fractional", "day", "sell", "submit"),
    ):
        with pytest.raises(CapabilityUnavailable):
            matrix.require(descriptor, AlpacaEnvironment.PAPER)
    decision = ProtectionCoordinator(BrokerRecorder(), Clock(), fixed_policy()).check_entry_eligibility(
        fixed_policy(), caps(), ExecutionMode.PAPER, D("0.5")
    )
    assert not decision.entry_allowed
    assert decision.reason_code == "FRACTIONAL_NOT_SUPPORTED"


def test_live_runtime_refuses_start_without_explicit_live_authorization(tmp_path: Path) -> None:
    with pytest.raises(LiveTradingNotAuthorizedError):
        build_runtime(AppSettings(mode=ExecutionMode.LIVE, data_dir=tmp_path))
    # Even if credential-shaped values are supplied explicitly, the production
    # adapter rejects LIVE startup before constructing a client or making I/O.
    adapter = AlpacaBrokerAdapter(
        ExecutionMode.LIVE, "offline-live-check", Clock(), authorized=False,
        environ={
            "ALPACA_LIVE_KEY_ID": uuid4().hex,
            "ALPACA_LIVE_SECRET_KEY": uuid4().hex,
        },
    )
    with pytest.raises(LiveAuthorizationRequired):
        adapter.validate_startup()
    assert adapter._client is None and adapter._orders is None


def test_paper_and_live_credentials_endpoints_and_scopes_are_isolated() -> None:
    env = {
        "ALPACA_PAPER_KEY_ID": uuid4().hex,
        "ALPACA_PAPER_SECRET_KEY": uuid4().hex,
        "ALPACA_LIVE_KEY_ID": uuid4().hex,
        "ALPACA_LIVE_SECRET_KEY": uuid4().hex,
    }
    paper = AlpacaCredentials.from_env(AlpacaEnvironment.PAPER, env)
    live = AlpacaCredentials.from_env(AlpacaEnvironment.LIVE, env)
    assert paper.environment is AlpacaEnvironment.PAPER
    assert live.environment is AlpacaEnvironment.LIVE
    assert credential_fingerprint(paper) != credential_fingerprint(live)
    assert AlpacaEnvironment.PAPER.base_url != AlpacaEnvironment.LIVE.base_url
    paper_scope = AccountScope(AccountEnvironment.PAPER, "same-account")
    live_scope = AccountScope(AccountEnvironment.LIVE, "same-account")
    assert paper_scope.scope_id != live_scope.scope_id
    with pytest.raises(EnvironmentMismatchError):
        AlpacaEnvironment.from_base_url(AlpacaEnvironment.LIVE.base_url.replace("api.alpaca", "other.alpaca"))
    # Each environment loader rejects the other environment's only credential pair.
    with pytest.raises(ValueError):
        AlpacaCredentials.from_env(
            AlpacaEnvironment.PAPER,
            {"ALPACA_LIVE_KEY_ID": uuid4().hex, "ALPACA_LIVE_SECRET_KEY": uuid4().hex},
        )
    with pytest.raises(ValueError):
        AlpacaCredentials.from_env(
            AlpacaEnvironment.LIVE,
            {"ALPACA_PAPER_KEY_ID": uuid4().hex, "ALPACA_PAPER_SECRET_KEY": uuid4().hex},
        )


def test_runtime_shutdown_leaves_valid_protective_stop_untouched(tmp_path: Path) -> None:
    broker = BrokerRecorder()
    broker.valid_protective_stops = {"paper-or-shadow-stop-1"}
    runtime = build_runtime(
        AppSettings(mode=ExecutionMode.SHADOW, data_dir=tmp_path, symbols=("SPY",)),
        overrides={"broker": broker},
    )
    before = set(broker.valid_protective_stops)
    status = asyncio.run(runtime.stop())
    assert status.state.value == "STOPPED"
    assert broker.valid_protective_stops == before
    assert not any(call[0] == "cancel" for call in broker.calls)
