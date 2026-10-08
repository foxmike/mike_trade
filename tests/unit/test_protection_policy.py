from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from mike_trade.domain.trading import TimeInForce
from mike_trade.risk.protection import ProtectionCoordinator
from mike_trade.risk.stop_policy import (
    BrokerProtectionCapabilities,
    ProtectionMode,
    ProtectionPolicy,
    evaluate_policy,
)
from mike_trade.runtime import ExecutionMode

D = Decimal
NOW = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)


def fixed(**changes) -> ProtectionPolicy:
    values = {
        "mode": ProtectionMode.FIXED,
        "distance_percent": D("5"),
    }
    values.update(changes)
    return ProtectionPolicy(**values)


def capabilities(**changes) -> BrokerProtectionCapabilities:
    values = {
        "supported_order_types": frozenset({"STOP_MARKET", "STOP_LIMIT", "TRAILING_STOP"}),
        "paper_proven_order_types": frozenset(
            {"STOP_MARKET", "STOP_LIMIT", "TRAILING_STOP"}
        ),
        "standalone_trailing_parameters": frozenset({"TRAIL_PRICE", "TRAIL_PERCENT"}),
        "paper_proven_standalone_trailing_parameters": frozenset(
            {"TRAIL_PRICE", "TRAIL_PERCENT"}
        ),
        "fractional_combinations": frozenset(
            {
                ("STOP_MARKET", TimeInForce.GTC),
                ("TRAILING_STOP", TimeInForce.GTC),
            }
        ),
        "paper_proven_fractional_combinations": frozenset(
            {
                ("STOP_MARKET", TimeInForce.GTC),
                ("TRAILING_STOP", TimeInForce.GTC),
            }
        ),
        "supported_tifs": frozenset({TimeInForce.DAY, TimeInForce.GTC}),
        "paper_proven_tifs": frozenset({TimeInForce.DAY, TimeInForce.GTC}),
        "linked_order_types": frozenset({"STOP_MARKET", "STOP_LIMIT"}),
        "paper_proven_linked_order_types": frozenset({"STOP_MARKET", "STOP_LIMIT"}),
        "linked_trailing_parameters": frozenset(),
        "paper_proven_linked_trailing_parameters": frozenset(),
    }
    values.update(changes)
    return BrokerProtectionCapabilities(**values)


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"distance_percent": D("5"), "distance_usd": D("1")},
    ],
)
def test_fixed_requires_exactly_one_distance(values) -> None:
    with pytest.raises(ValueError, match="exactly one distance"):
        ProtectionPolicy(mode=ProtectionMode.FIXED, **values)


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"trail_price": D("1"), "trail_percent": D("5")},
    ],
)
def test_native_trailing_requires_exactly_one_trail_value(values) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ProtectionPolicy(mode=ProtectionMode.TRAILING_NATIVE, **values)


def test_stop_limit_requires_decimal_limit_offset() -> None:
    with pytest.raises(ValueError, match="requires limit_offset"):
        fixed(order_semantics="STOP_LIMIT")
    policy = fixed(order_semantics="STOP_LIMIT", limit_offset=D("0.10"))
    assert policy.limit_offset == D("0.10")


def test_allowed_policy_and_risk_notes_are_explicit() -> None:
    result = evaluate_policy(fixed(), capabilities(), ExecutionMode.LIVE, D("10"))
    assert result.allowed and result.entry_allowed
    assert any(
        "not a guaranteed execution price or maximum loss" in note
        for note in result.risk_notes
    )
    assert any("stop-limit order may not fill" in note for note in result.risk_notes)
    assert any(
        "DAY TIF expires" in note and "overnight residual" in note
        for note in result.risk_notes
    )


def test_unsupported_order_type_is_rejected() -> None:
    caps = capabilities(
        supported_order_types=frozenset({"STOP_LIMIT"}),
        paper_proven_order_types=frozenset({"STOP_LIMIT"}),
    )
    result = evaluate_policy(fixed(), caps, ExecutionMode.PAPER, D("10"))
    assert result.reason_code == "UNSUPPORTED_ORDER_TYPE"


def test_fractional_requires_policy_opt_in_and_documented_ack() -> None:
    result = evaluate_policy(fixed(), capabilities(), ExecutionMode.PAPER, D("1.5"))
    assert result.reason_code == "FRACTIONAL_NOT_SUPPORTED"
    result = evaluate_policy(
        fixed(allow_fractional_protection=True),
        capabilities(),
        ExecutionMode.PAPER,
        D("1.5"),
    )
    assert result.reason_code == "FRACTIONAL_NOT_SUPPORTED"


def test_live_fractional_combination_must_be_paper_proven() -> None:
    policy = fixed(
        allow_fractional_protection=True,
        documented_fractional_risk_ack="Reviewed gap and partial-fill risk.",
    )
    caps = capabilities(paper_proven_fractional_combinations=frozenset())
    result = evaluate_policy(policy, caps, ExecutionMode.LIVE, D("1.5"))
    assert result.reason_code == "FRACTIONAL_NOT_PAPER_PROVEN"
    assert evaluate_policy(policy, caps, ExecutionMode.PAPER, D("1.5")).allowed


def test_live_tif_must_be_paper_proven() -> None:
    caps = capabilities(paper_proven_tifs=frozenset({TimeInForce.DAY}))
    result = evaluate_policy(fixed(), caps, ExecutionMode.LIVE, D("2"))
    assert result.reason_code == "TIF_NOT_SUPPORTED"


def test_linked_native_trailing_leg_can_be_explicitly_unsupported() -> None:
    policy = ProtectionPolicy(
        mode=ProtectionMode.TRAILING_NATIVE,
        trail_percent=D("3"),
        linked=True,
    )
    result = evaluate_policy(policy, capabilities(), ExecutionMode.PAPER, D("2"))
    assert result.reason_code == "LINKED_TRAILING_UNSUPPORTED"


def test_stepped_mode_is_defined_but_not_available() -> None:
    policy = ProtectionPolicy(mode=ProtectionMode.TRAILING_STEPPED)
    result = evaluate_policy(policy, capabilities(), ExecutionMode.PAPER, D("2"))
    assert result.reason_code == "STEPPED_NOT_AVAILABLE"


def test_optional_ineligible_protection_does_not_reject_entry() -> None:
    result = evaluate_policy(
        fixed(required=False),
        BrokerProtectionCapabilities(),
        ExecutionMode.PAPER,
        D("2"),
    )
    assert result.rejected and result.entry_allowed


class Clock:
    def now(self) -> datetime:
        return NOW


class FakeBroker:
    def __init__(self, *, fail_protection: bool = False) -> None:
        self.fail_protection = fail_protection
        self.protective_requests = []
        self.exit_requests = []
        self.cancel_requests = []
        self.events = []
        self._next = 0

    def submit_protective_order(self, request):
        if self.fail_protection:
            raise RuntimeError("provider rejected stop")
        self._next += 1
        order_id = f"p-{self._next}"
        self.protective_requests.append(request)
        self.events.append(("protect", order_id, request.quantity))
        return order_id

    def submit_exit_order(self, request):
        self._next += 1
        order_id = f"e-{self._next}"
        self.exit_requests.append(request)
        self.events.append(("exit", order_id, request.quantity))
        return order_id

    def cancel_order(self, order_id):
        self.cancel_requests.append(order_id)
        self.events.append(("cancel", order_id))


@pytest.mark.asyncio
async def test_protection_requires_confirmed_positive_fill() -> None:
    broker = FakeBroker()
    coordinator = ProtectionCoordinator(broker, Clock(), fixed())
    assert await coordinator.on_confirmed_fill("c1", D("0"), D("10")) is None
    assert await coordinator.on_confirmed_fill(
        "c1", D("2"), D("10"), confirmed=False
    ) is None
    assert broker.protective_requests == []
    assert coordinator.current_protection("c1") is None


@pytest.mark.asyncio
async def test_replacement_waits_for_cancel_confirmation() -> None:
    broker = FakeBroker()
    coordinator = ProtectionCoordinator(broker, Clock(), fixed())
    await coordinator.on_confirmed_fill("c1", D("3"), D("10"))
    old = coordinator.current_protection("c1")
    assert old is not None and old.protective_order_id == "p-1"

    waiting = await coordinator.on_confirmed_fill("c1", D("5"), D("10.20"))
    assert waiting is not None and waiting.protective_order_id == "p-1"
    assert waiting.pending_replacement
    assert len(broker.protective_requests) == 1
    assert broker.cancel_requests == ["p-1"]

    active = await coordinator.on_broker_order_update("c1", "p-1", "CANCELED")
    assert active.protective_order_id == "p-2"
    assert active.protective_working_quantity == D("5")
    assert len(broker.protective_requests) == 2
    assert broker.events == [
        ("protect", "p-1", D("3")),
        ("cancel", "p-1"),
        ("protect", "p-2", D("5")),
    ]


@pytest.mark.asyncio
async def test_manual_close_waits_for_stop_cancel_and_never_overlaps() -> None:
    broker = FakeBroker()
    coordinator = ProtectionCoordinator(broker, Clock(), fixed())
    await coordinator.on_confirmed_fill("c1", D("10"), D("20"))

    waiting = await coordinator.on_exit_requested("c1", D("10"), "manual close")
    assert waiting.protective_working_quantity == D("10")
    assert waiting.exit_working_quantity == D("0")
    assert waiting.working_sell_quantity == D("10")
    assert broker.exit_requests == []

    exiting = await coordinator.on_broker_order_update("c1", "p-1", "CANCELED")
    assert exiting.protective_working_quantity == D("0")
    assert exiting.exit_working_quantity == D("10")
    assert exiting.working_sell_quantity == exiting.confirmed_remaining_quantity

    partial = await coordinator.on_broker_order_update(
        "c1", "e-2", "PARTIALLY_FILLED", filled_qty=D("4")
    )
    assert partial.confirmed_remaining_quantity == D("6")
    assert partial.exit_working_quantity == D("6")
    assert partial.working_sell_quantity == D("6")
    closed = await coordinator.on_broker_order_update(
        "c1", "e-2", "FILLED", filled_qty=D("10")
    )
    assert closed.status == "POSITION_CLOSED"
    assert closed.working_sell_quantity == D("0")


@pytest.mark.asyncio
async def test_partial_exit_is_reprotected_for_confirmed_remainder() -> None:
    broker = FakeBroker()
    coordinator = ProtectionCoordinator(broker, Clock(), fixed())
    await coordinator.on_confirmed_fill("c1", D("10"), D("20"))
    await coordinator.on_exit_requested("c1", D("4"), "strategy exit")
    await coordinator.on_broker_order_update("c1", "p-1", "CANCELED")
    result = await coordinator.on_broker_order_update(
        "c1", "e-2", "FILLED", filled_qty=D("4")
    )
    assert result.confirmed_remaining_quantity == D("6")
    assert result.protective_order_id == "p-3"
    assert result.protective_working_quantity == D("6")
    assert result.working_sell_quantity == D("6")


@pytest.mark.asyncio
async def test_protection_ignores_pause_and_minimum_hold_inputs_by_design() -> None:
    broker = FakeBroker()
    coordinator = ProtectionCoordinator(broker, Clock(), fixed())
    # These strategy controls exist on the test harness but are not coordinator inputs.
    strategy_paused = True
    minimum_hold_until = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
    assert strategy_paused and minimum_hold_until > NOW

    result = await coordinator.on_confirmed_fill("c1", D("2"), D("15"))
    assert result is not None and result.status == "PROTECTION_ACTIVE"
    assert len(broker.protective_requests) == 1


@pytest.mark.asyncio
async def test_placement_failure_marks_unprotected_and_blocks_fresh_entries() -> None:
    broker = FakeBroker(fail_protection=True)
    coordinator = ProtectionCoordinator(broker, Clock(), fixed())
    with pytest.raises(RuntimeError, match="protective placement failed"):
        await coordinator.on_confirmed_fill("c1", D("2"), D("15"))
    result = coordinator.current_protection("c1")
    assert result is not None and result.status == "UNPROTECTED_POSITION"
    assert coordinator.entry_block_reasons == ("UNPROTECTED_POSITION",)
    assert result.updated_at == NOW


def test_naive_clock_is_rejected_before_order_submission() -> None:
    class NaiveClock:
        def now(self):
            return datetime(2026, 10, 8, 15, 0)

    broker = FakeBroker()
    coordinator = ProtectionCoordinator(broker, NaiveClock(), fixed())

    async def exercise():
        with pytest.raises(ValueError, match="timezone-aware UTC"):
            await coordinator.on_confirmed_fill("c1", D("2"), D("15"))

    import asyncio

    asyncio.run(exercise())
    assert broker.protective_requests == []
