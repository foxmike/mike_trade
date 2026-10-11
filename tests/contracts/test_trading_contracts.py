from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mike_trade.domain.protection import (
    ProtectionKind,
    ProtectionPolicy,
    ProtectionStatus,
    TrailingStopState,
    combined_sell_exposure,
    start_protection,
)
from mike_trade.domain.trading import (
    TERMINAL_ORDER_STATUSES,
    BrokerOrder,
    BrokerOrderStatus,
    BudgetPolicy,
    EntryEligibilityPolicy,
    EntryEligibilityState,
    Fill,
    ManagementStatus,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    Position,
    PositionCycle,
    PositionCycleState,
    TimeInForce,
)

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)


def fill(
    fill_id: str,
    side: OrderSide,
    quantity: str,
    price: str,
    *,
    fee: object = "0",
    filled_at: datetime = NOW,
) -> Fill:
    return Fill(
        fill_id=fill_id,
        broker_order_id=f"order-{fill_id}",
        account_scope_id="paper-main",
        symbol="AAPL",
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal(fee) if isinstance(fee, str) else fee,  # type: ignore[arg-type]
        filled_at=filled_at,
    )


def cycle() -> PositionCycle:
    return PositionCycle(cycle_id="cycle-1", account_scope_id="paper-main", symbol="aapl")


def intent(**overrides: object) -> OrderIntent:
    values: dict[str, object] = {
        "intent_id": "intent-1",
        "client_order_id": "client-1",
        "account_scope_id": "paper-main",
        "cycle_id": "cycle-1",
        "symbol": "AAPL",
        "side": OrderSide.BUY,
        "purpose": OrderPurpose.ENTRY,
        "order_type": OrderType.MARKET,
        "time_in_force": TimeInForce.DAY,
        "created_at": NOW,
        "quantity": Decimal("1.25"),
    }
    values.update(overrides)
    return OrderIntent(**values)  # type: ignore[arg-type]


def broker_order(**overrides: object) -> BrokerOrder:
    values: dict[str, object] = {
        "broker_order_id": "broker-1",
        "client_order_id": "client-1",
        "account_scope_id": "paper-main",
        "symbol": "AAPL",
        "side": OrderSide.BUY,
        "order_type": OrderType.MARKET,
        "status": BrokerOrderStatus.ACCEPTED,
        "quantity": Decimal("2"),
        "filled_quantity": Decimal("0"),
        "submitted_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return BrokerOrder(**values)  # type: ignore[arg-type]


def stop_state(**overrides: object) -> TrailingStopState:
    values: dict[str, object] = {
        "cycle_id": "cycle-1",
        "account_scope_id": "paper-main",
        "symbol": "AAPL",
        "kind": ProtectionKind.STEPPED_TRAILING_STOP,
        "status": ProtectionStatus.ACTIVE,
        "covered_quantity": Decimal("2"),
        "stop_price": Decimal("95"),
        "high_water_mark": Decimal("100"),
    }
    values.update(overrides)
    return TrailingStopState(**values)  # type: ignore[arg-type]


def test_partial_entry_fills_are_accepted_and_weighted() -> None:
    first = cycle().apply_entry_fill(fill("entry-1", OrderSide.BUY, "0.4", "100"))
    second = first.apply_entry_fill(fill("entry-2", OrderSide.BUY, "0.6", "110"))

    assert first.state is PositionCycleState.OPEN
    assert second.entry_quantity == Decimal("1.0")
    assert second.remaining_quantity == Decimal("1.0")
    assert second.avg_entry_price == Decimal("106")


def test_entry_fill_after_exit_begins_is_rejected() -> None:
    exiting = cycle().apply_entry_fill(fill("entry", OrderSide.BUY, "1", "100")).begin_exit()

    with pytest.raises(ValueError, match="before exit begins"):
        exiting.apply_entry_fill(fill("late-entry", OrderSide.BUY, "1", "90"))


def test_over_exit_is_rejected_and_complete_exit_closes_cycle() -> None:
    exiting = cycle().apply_entry_fill(fill("entry", OrderSide.BUY, "2", "100")).begin_exit()

    with pytest.raises(ValueError, match="must not exceed"):
        exiting.apply_exit_fill(fill("too-large", OrderSide.SELL, "2.1", "110"))

    partially_exited = exiting.apply_exit_fill(fill("exit-1", OrderSide.SELL, "0.5", "110"))
    closed = partially_exited.apply_exit_fill(
        fill("exit-2", OrderSide.SELL, "1.5", "112", filled_at=NOW + timedelta(minutes=1))
    )
    assert partially_exited.state is PositionCycleState.EXITING
    assert closed.state is PositionCycleState.CLOSED
    assert closed.remaining_quantity == Decimal("0")
    assert closed.closed_at == NOW + timedelta(minutes=1)


def test_realized_pnl_includes_all_fees() -> None:
    opened = cycle().apply_entry_fill(
        fill("entry", OrderSide.BUY, "2", "100", fee="1.25")
    )
    closed = opened.begin_exit().apply_exit_fill(
        fill("exit", OrderSide.SELL, "2", "110", fee="0.75")
    )

    assert closed.realized_pnl == Decimal("18.00")
    assert closed.avg_exit_price == Decimal("110")


def test_fill_ids_are_idempotency_keys_within_a_cycle() -> None:
    opened = cycle().apply_entry_fill(fill("same", OrderSide.BUY, "1", "100"))
    with pytest.raises(ValueError, match="fill_id"):
        opened.apply_entry_fill(fill("same", OrderSide.BUY, "1", "100"))


def test_entry_requires_an_opening_side() -> None:
    with pytest.raises(ValueError, match="ENTRY requires BUY or SELL_SHORT"):
        intent(side=OrderSide.SELL)
    with pytest.raises(ValueError, match="ENTRY requires BUY or SELL_SHORT"):
        intent(side=OrderSide.BUY_TO_COVER)
    assert intent(side=OrderSide.SELL_SHORT).side is OrderSide.SELL_SHORT


def test_exit_intents_require_a_closing_side() -> None:
    with pytest.raises(ValueError, match="exit/protection intents require"):
        intent(side=OrderSide.SELL_SHORT, purpose=OrderPurpose.STRATEGY_EXIT)
    assert intent(side=OrderSide.BUY_TO_COVER, purpose=OrderPurpose.STRATEGY_EXIT)


@pytest.mark.parametrize(
    ("quantity", "notional"),
    [(None, None), (Decimal("1"), Decimal("100"))],
)
def test_order_intent_requires_exactly_one_sizing_method(
    quantity: Decimal | None, notional: Decimal | None
) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        intent(quantity=quantity, notional=notional)


def test_order_type_specific_prices_are_enforced() -> None:
    stop_limit = intent(
        order_type=OrderType.STOP_LIMIT,
        limit_price=Decimal("99"),
        stop_price=Decimal("100"),
    )
    trailing = intent(
        purpose=OrderPurpose.PROTECTIVE_STOP,
        side=OrderSide.SELL,
        order_type=OrderType.TRAILING_STOP,
        trail_percent=Decimal("2"),
    )

    assert stop_limit.limit_price == Decimal("99")
    assert trailing.trail_percent == Decimal("2")
    with pytest.raises(ValueError, match="exactly one trail"):
        intent(order_type=OrderType.TRAILING_STOP)


def test_broker_order_fill_bounds_and_average_price_are_enforced() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        broker_order(filled_quantity=Decimal("2.1"), filled_avg_price=Decimal("100"))
    with pytest.raises(ValueError, match="filled_avg_price"):
        broker_order(filled_quantity=Decimal("1"))


def test_terminal_broker_order_property_uses_frozen_status_set() -> None:
    assert broker_order(status=BrokerOrderStatus.FILLED).is_terminal
    assert not broker_order(status=BrokerOrderStatus.PARTIALLY_FILLED).is_terminal
    assert BrokerOrderStatus.REJECTED in TERMINAL_ORDER_STATUSES
    assert isinstance(TERMINAL_ORDER_STATUSES, frozenset)


def test_external_position_defaults_unmanaged_and_managed_requires_cycle() -> None:
    external = Position(
        account_scope_id="paper-main",
        symbol="AAPL",
        quantity=Decimal("1.125"),
        avg_entry_price=Decimal("100.01"),
        cost_basis=Decimal("112.51125"),
        as_of=NOW,
    )

    assert external.management is ManagementStatus.UNMANAGED
    with pytest.raises(ValueError, match="cycle_id"):
        Position(
            account_scope_id="paper-main",
            symbol="AAPL",
            quantity=Decimal("1"),
            avg_entry_price=Decimal("100"),
            cost_basis=Decimal("100"),
            as_of=NOW,
            management=ManagementStatus.MANAGED,
        )


def test_budget_policy_validates_caps_without_rounding() -> None:
    policy = BudgetPolicy(
        per_symbol_budget=Decimal("1000.005"),
        total_application_budget=Decimal("5000.005"),
        max_open_positions=5,
    )
    assert policy.per_symbol_budget == Decimal("1000.005")
    with pytest.raises(ValueError, match="total_application_budget"):
        BudgetPolicy(Decimal("100"), Decimal("99.99"), 1)


def test_minimum_hold_applies_only_to_strategy_exit() -> None:
    policy = EntryEligibilityPolicy(
        max_filled_entry_cycles_per_symbol_per_session=1,
        min_hold_for_strategy_exit=timedelta(hours=1),
        entry_kill_switch=True,
    )

    assert not policy.exit_allowed(OrderPurpose.STRATEGY_EXIT, timedelta(minutes=59))
    assert policy.exit_allowed(OrderPurpose.STRATEGY_EXIT, timedelta(hours=1))
    for purpose in (
        OrderPurpose.PROTECTIVE_STOP,
        OrderPurpose.MANUAL_CLOSE,
        OrderPurpose.SAFETY_EXIT,
        OrderPurpose.EOD_CLOSE,
    ):
        assert policy.exit_allowed(purpose, timedelta(0))


def test_entry_state_reports_active_lock_without_affecting_exit_contract() -> None:
    state = EntryEligibilityState(
        account_scope_id="paper-main",
        symbol="AAPL",
        session_date=date(2026, 10, 8),
        filled_entry_cycles=1,
        consecutive_losses=2,
        locked_until=NOW + timedelta(minutes=5),
        block_reasons=("cycle_limit",),
    )

    assert state.entry_blocked(NOW) == ("cycle_limit", "locked_until")
    assert state.entry_blocked(NOW + timedelta(minutes=5)) == ("cycle_limit",)


@pytest.mark.parametrize(
    "bad_value",
    [1.0, Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("-1")],
)
def test_decimal_fields_reject_float_nonfinite_and_negative(bad_value: object) -> None:
    with pytest.raises(ValueError):
        fill("bad", OrderSide.BUY, "1", "100", fee=bad_value)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        combined_sell_exposure(bad_value, Decimal("0"), Decimal("1"))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad_time",
    [
        datetime(2026, 10, 8, 14, 30),
        datetime(
            2026, 10, 8, 10, 30, tzinfo=timezone(timedelta(hours=-4))
        ),
    ],
)
def test_datetime_fields_require_utc(bad_time: datetime) -> None:
    with pytest.raises(ValueError, match="UTC"):
        fill("bad-time", OrderSide.BUY, "1", "100", filled_at=bad_time)


def test_contracts_are_frozen_and_slotted() -> None:
    created = fill("entry", OrderSide.BUY, "1", "100")
    with pytest.raises(FrozenInstanceError):
        created.fee = Decimal("1")  # type: ignore[misc]
    assert not hasattr(created, "__dict__")


def test_protection_policy_has_kind_specific_validation() -> None:
    fixed = ProtectionPolicy(
        kind=ProtectionKind.FIXED_STOP, stop_offset_amount=Decimal("5")
    )
    stepped = ProtectionPolicy(
        kind=ProtectionKind.STEPPED_TRAILING_STOP,
        trail_percent=Decimal("0.05"),
        step_size=Decimal("1"),
        activation_gain=Decimal("2"),
    )
    assert fixed.require_broker_held
    assert stepped.activation_gain == Decimal("2")
    with pytest.raises(ValueError, match="exactly one"):
        ProtectionPolicy(kind=ProtectionKind.FIXED_STOP)
    with pytest.raises(ValueError, match="only a trail"):
        ProtectionPolicy(
            kind=ProtectionKind.BROKER_TRAILING_STOP,
            trail_amount=Decimal("2"),
            step_size=Decimal("1"),
        )


def test_long_stop_ratchets_up_but_never_down() -> None:
    state = stop_state()
    moved = state.ratchet(Decimal("97"), Decimal("103"))
    assert moved.stop_price == Decimal("97")
    assert moved.high_water_mark == Decimal("103")
    with pytest.raises(ValueError, match="never move downward"):
        moved.ratchet(Decimal("96"), Decimal("104"))


def test_coverage_resizes_to_actual_remaining_quantity() -> None:
    resized = stop_state().with_covered_quantity(Decimal("1.25"))
    assert resized.covered_quantity == Decimal("1.25")
    with pytest.raises(ValueError):
        resized.with_covered_quantity(Decimal("-0.01"))
    with pytest.raises(ValueError, match="must not exceed"):
        resized.with_covered_quantity(Decimal("1.5"))


def test_protection_refused_before_fill_and_started_after_fill() -> None:
    policy = ProtectionPolicy(
        kind=ProtectionKind.FIXED_STOP, stop_offset_amount=Decimal("5")
    )
    with pytest.raises(ValueError, match="positive filled entry"):
        start_protection(policy, cycle(), Decimal("100"))

    opened = cycle().apply_entry_fill(fill("entry", OrderSide.BUY, "1.5", "100"))
    protection = start_protection(policy, opened, Decimal("100"))
    assert protection.covered_quantity == Decimal("1.5")
    assert protection.stop_price == Decimal("95")
    assert protection.status is ProtectionStatus.PENDING

    percentage_policy = ProtectionPolicy(
        kind=ProtectionKind.FIXED_STOP, stop_offset_percent=Decimal("5")
    )
    assert start_protection(percentage_policy, opened, Decimal("100")).stop_price == Decimal(
        "95"
    )


def test_overlapping_sell_detection() -> None:
    assert combined_sell_exposure(Decimal("5"), Decimal("1"), Decimal("5"))
    assert not combined_sell_exposure(Decimal("4"), Decimal("1"), Decimal("5"))
