from __future__ import annotations

from decimal import Decimal

import pytest

from mike_trade.domain.protection import ProtectionKind
from mike_trade.portfolio.sizing import (
    SizingInput,
    SizingRejection,
    size_long_entry,
)

D = Decimal
FIXED_WHOLE = frozenset({(ProtectionKind.FIXED_STOP, False)})
FIXED_BOTH = frozenset(
    {
        (ProtectionKind.FIXED_STOP, False),
        (ProtectionKind.FIXED_STOP, True),
    }
)


def sizing_input(**changes: object) -> SizingInput:
    values: dict[str, object] = {
        "entry_price": D("10"),
        "remaining_symbol_budget_usd": D("1000"),
        "remaining_total_budget_usd": D("1000"),
        "broker_buying_power_usd": D("1000"),
        "reserve_usd": D("0"),
        "slippage_cushion_usd": D("0"),
        "quantity_increment": D("1"),
        "minimum_order_value_usd": D("1"),
        "protection_kind": ProtectionKind.FIXED_STOP,
        "supported_protection_combinations": FIXED_WHOLE,
        "protective_stop_price": D("9"),
    }
    values.update(changes)
    return SizingInput(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("symbol_budget", "total_budget", "buying_power", "expected_quantity"),
    [
        ("100", "500", "500", "9"),
        ("500", "120", "500", "11"),
        ("500", "500", "140", "8"),
    ],
)
def test_quantity_is_bounded_by_smallest_cap_after_reserve_and_slippage(
    symbol_budget: str,
    total_budget: str,
    buying_power: str,
    expected_quantity: str,
) -> None:
    result = size_long_entry(
        sizing_input(
            remaining_symbol_budget_usd=D(symbol_budget),
            remaining_total_budget_usd=D(total_budget),
            broker_buying_power_usd=D(buying_power),
            reserve_usd=D("50"),
            slippage_cushion_usd=D("10"),
        )
    )

    assert result.accepted
    assert result.quantity == D(expected_quantity)
    assert result.notional == result.quantity * D("10")
    assert result.notional <= min(
        D(symbol_budget), D(total_budget), D(buying_power) - D("50")
    ) - D("10")


def test_max_planned_loss_caps_quantity_after_rounding_down() -> None:
    result = size_long_entry(
        sizing_input(
            quantity_increment=D("0.01"),
            supported_protection_combinations=FIXED_BOTH,
            max_planned_loss_usd=D("10"),
            require_max_planned_loss=True,
            protective_stop_price=D("8.5"),
        )
    )

    assert result.quantity == D("6.66")
    assert result.planned_loss == D("9.990")
    assert result.planned_loss <= D("10")


@pytest.mark.parametrize("stop_price", [None, D("0"), D("10"), D("11")])
def test_required_loss_budget_rejects_missing_or_invalid_long_stop(
    stop_price: Decimal | None,
) -> None:
    result = size_long_entry(
        sizing_input(
            max_planned_loss_usd=D("50"),
            require_max_planned_loss=True,
            protective_stop_price=stop_price,
        )
    )

    assert result.rejection is SizingRejection.INVALID_PROTECTIVE_STOP


@pytest.mark.parametrize("stop_distance", [D("0"), D("-1")])
def test_loss_budget_rejects_non_positive_stop_distance(
    stop_distance: Decimal,
) -> None:
    result = size_long_entry(
        sizing_input(
            max_planned_loss_usd=D("50"),
            protective_stop_price=None,
            protective_stop_distance=stop_distance,
        )
    )

    assert result.rejection is SizingRejection.INVALID_PROTECTIVE_STOP


def test_required_loss_budget_rejects_missing_limit() -> None:
    result = size_long_entry(
        sizing_input(max_planned_loss_usd=None, require_max_planned_loss=True)
    )

    assert result.rejection is SizingRejection.MAX_PLANNED_LOSS_REQUIRED


def test_stop_distance_can_supply_the_loss_per_share() -> None:
    result = size_long_entry(
        sizing_input(
            protective_stop_price=None,
            protective_stop_distance=D("1.5"),
            max_planned_loss_usd=D("10"),
            quantity_increment=D("0.01"),
            supported_protection_combinations=FIXED_BOTH,
        )
    )

    assert result.quantity == D("6.66")
    assert result.planned_loss == D("9.990")


def test_whole_share_quantity_rounds_down() -> None:
    result = size_long_entry(
        sizing_input(remaining_symbol_budget_usd=D("79"))
    )

    assert result.quantity == D("7")
    assert result.notional == D("70")


def test_quantity_that_rounds_to_zero_has_explicit_rejection() -> None:
    result = size_long_entry(
        sizing_input(remaining_symbol_budget_usd=D("9"))
    )

    assert result.rejection is SizingRejection.QUANTITY_ROUNDS_TO_ZERO
    assert result.quantity == D("0")


def test_notional_below_minimum_order_value_is_rejected() -> None:
    result = size_long_entry(
        sizing_input(
            remaining_symbol_budget_usd=D("4.9"),
            quantity_increment=D("0.1"),
            minimum_order_value_usd=D("5"),
            supported_protection_combinations=FIXED_BOTH,
        )
    )

    assert result.quantity == D("0")
    assert result.rejection is SizingRejection.BELOW_MINIMUM_ORDER_VALUE


def test_fractional_protection_combination_fails_closed_until_demonstrated() -> None:
    unsupported = size_long_entry(
        sizing_input(
            remaining_symbol_budget_usd=D("75"),
            quantity_increment=D("0.1"),
            supported_protection_combinations=FIXED_WHOLE,
        )
    )
    supported = size_long_entry(
        sizing_input(
            remaining_symbol_budget_usd=D("75"),
            quantity_increment=D("0.1"),
            supported_protection_combinations=FIXED_BOTH,
        )
    )

    assert unsupported.rejection is SizingRejection.FRACTIONAL_PROTECTION_UNSUPPORTED
    assert supported.accepted
    assert supported.quantity == D("7.5")


def test_fractional_trailing_protection_requires_its_own_explicit_evidence() -> None:
    result = size_long_entry(
        sizing_input(
            protection_kind=ProtectionKind.BROKER_TRAILING_STOP,
            quantity_increment=D("0.1"),
            supported_protection_combinations=frozenset(
                {(ProtectionKind.BROKER_TRAILING_STOP, False)}
            ),
        )
    )

    assert result.rejection is SizingRejection.FRACTIONAL_PROTECTION_UNSUPPORTED


def test_unavailable_whole_share_protection_is_rejected() -> None:
    result = size_long_entry(
        sizing_input(supported_protection_combinations=frozenset())
    )

    assert result.rejection is SizingRejection.PROTECTION_CANNOT_BE_ESTABLISHED


def test_missing_stop_without_loss_budget_cannot_establish_protection() -> None:
    result = size_long_entry(
        sizing_input(protective_stop_price=None, protective_stop_distance=None)
    )

    assert result.rejection is SizingRejection.PROTECTION_CANNOT_BE_ESTABLISHED


def test_insufficient_buying_power_and_disabled_margin_are_distinct() -> None:
    buying_power = size_long_entry(
        sizing_input(broker_buying_power_usd=D("50"), reserve_usd=D("50"))
    )
    margin = size_long_entry(
        sizing_input(margin_required=True, margin_enabled=False)
    )

    assert buying_power.rejection is SizingRejection.INSUFFICIENT_BUYING_POWER
    assert margin.rejection is SizingRejection.MARGIN_NOT_ENABLED


def test_margin_is_allowed_only_when_explicitly_enabled() -> None:
    result = size_long_entry(
        sizing_input(margin_required=True, margin_enabled=True)
    )

    assert result.accepted


def test_short_side_is_rejected() -> None:
    result = size_long_entry(sizing_input(side="short"))

    assert result.rejection is SizingRejection.SHORT_NOT_SUPPORTED


def test_money_and_quantity_inputs_reject_float() -> None:
    with pytest.raises(TypeError, match="entry_price must be Decimal"):
        sizing_input(entry_price=10.0)
