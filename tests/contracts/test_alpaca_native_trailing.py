"""Offline contracts for Alpaca continuous broker-managed trailing stops."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from mike_trade.brokers.alpaca.trailing import (
    NativeTrailingState,
    build_trailing_stop_request,
    cancel_trailing_stop,
    parse_trailing_order_state,
    replace_trailing_stop,
    submit_trailing_stop,
)
from mike_trade.domain.trading import TimeInForce
from mike_trade.risk.stop_policy import (
    BrokerProtectionCapabilities,
    ProtectionMode,
    ProtectionPolicy,
)
from mike_trade.runtime import ExecutionMode


def policy(**overrides: Any) -> ProtectionPolicy:
    values: dict[str, Any] = {
        "mode": ProtectionMode.TRAILING_NATIVE,
        "trail_percent": Decimal("1.25"),
        "time_in_force": TimeInForce.GTC,
    }
    values.update(overrides)
    return ProtectionPolicy(**values)


def order_json(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": "order-native-trailing-1",
        "client_order_id": "mt-native-trailing-1",
        "status": "accepted",
        "symbol": "AAPL",
        "side": "sell",
        "type": "trailing_stop",
        "order_class": "simple",
        "time_in_force": "gtc",
        "qty": "10.5",
        "filled_qty": "0",
        "hwm": "201.37",
        "stop_price": "198.85",
        "trail_price": None,
        "trail_percent": "1.25",
        "updated_at": "2026-10-08T15:31:20.123456-04:00",
    }
    value.update(overrides)
    return value


def fractional_capabilities(*, paper_proven: bool) -> BrokerProtectionCapabilities:
    combination = frozenset({("TRAILING_STOP", TimeInForce.GTC)})
    return BrokerProtectionCapabilities(
        supported_order_types=frozenset({"TRAILING_STOP"}),
        paper_proven_order_types=frozenset({"TRAILING_STOP"}),
        standalone_trailing_parameters=frozenset({"TRAIL_PERCENT"}),
        paper_proven_standalone_trailing_parameters=frozenset({"TRAIL_PERCENT"}),
        fractional_combinations=combination,
        paper_proven_fractional_combinations=(
            combination if paper_proven else frozenset()
        ),
        supported_tifs=frozenset({TimeInForce.GTC}),
        paper_proven_tifs=frozenset({TimeInForce.GTC}),
    )


def test_request_is_standalone_sell_with_one_string_trail_value() -> None:
    request = build_trailing_stop_request(
        " aapl ", Decimal("10"), policy(), "client-1"
    )

    assert request == {
        "symbol": "AAPL",
        "qty": "10",
        "side": "sell",
        "type": "trailing_stop",
        "time_in_force": "gtc",
        "order_class": "simple",
        "client_order_id": "client-1",
        "trail_percent": "1.25",
    }
    assert isinstance(request["qty"], str)
    assert isinstance(request["trail_percent"], str)
    assert "trail_price" not in request

    by_price = build_trailing_stop_request(
        "MSFT",
        Decimal("2"),
        policy(trail_percent=None, trail_price=Decimal("3.50")),
        "client-2",
    )
    assert by_price["trail_price"] == "3.50"
    assert "trail_percent" not in by_price


@pytest.mark.parametrize("trail_price,trail_percent", [(None, None), (Decimal("2"), Decimal("1"))])
def test_request_rejects_both_or_neither_trail_value(
    trail_price: Decimal | None, trail_percent: Decimal | None
) -> None:
    invalid = policy()
    object.__setattr__(invalid, "trail_price", trail_price)
    object.__setattr__(invalid, "trail_percent", trail_percent)
    with pytest.raises(ValueError, match="exactly one"):
        build_trailing_stop_request("AAPL", Decimal("1"), invalid, "client-1")


@pytest.mark.parametrize("order_class", ["bracket", "oto", "oco"])
def test_linked_order_classes_are_rejected(order_class: str) -> None:
    with pytest.raises(ValueError, match="standalone"):
        build_trailing_stop_request(
            "AAPL",
            Decimal("1"),
            policy(),
            "client-1",
            order_class=order_class,
        )

    with pytest.raises(ValueError, match="standalone"):
        build_trailing_stop_request(
            "AAPL", Decimal("1"), policy(linked=True), "client-1"
        )


def test_quantity_and_side_validation_precede_transport() -> None:
    with pytest.raises(ValueError, match="qty"):
        build_trailing_stop_request("AAPL", Decimal("0"), policy(), "client-1")
    with pytest.raises(ValueError, match="SELL"):
        build_trailing_stop_request(
            "AAPL", Decimal("1"), policy(), "client-1", side="buy"
        )


def test_fractional_quantity_requires_supported_and_live_paper_proven_capability() -> None:
    fractional_policy = policy(
        allow_fractional_protection=True,
        documented_fractional_risk_ack="operator accepted fractional-order risks",
    )
    with pytest.raises(ValueError, match="BrokerProtectionCapabilities"):
        build_trailing_stop_request(
            "AAPL", Decimal("1.5"), fractional_policy, "client-1"
        )
    with pytest.raises(ValueError, match="paper_proven"):
        build_trailing_stop_request(
            "AAPL",
            Decimal("1.5"),
            fractional_policy,
            "client-1",
            capabilities=fractional_capabilities(paper_proven=False),
            execution_mode=ExecutionMode.LIVE,
        )

    request = build_trailing_stop_request(
        "AAPL",
        Decimal("1.5"),
        fractional_policy,
        "client-1",
        capabilities=fractional_capabilities(paper_proven=True),
        execution_mode=ExecutionMode.LIVE,
    )
    assert request["qty"] == "1.5"


def test_authoritative_hwm_stop_and_timestamp_are_parsed() -> None:
    state = parse_trailing_order_state(order_json())

    assert isinstance(state, NativeTrailingState)
    assert state.status == "ACTIVE"
    assert state.broker_status == "accepted"
    assert state.hwm == Decimal("201.37")
    assert state.high_water_mark == Decimal("201.37")
    assert state.stop_price == Decimal("198.85")
    assert state.trail_percent == Decimal("1.25")
    assert state.trail_price is None
    assert state.time_in_force == "gtc"
    assert state.updated_at == datetime(2026, 10, 8, 19, 31, 20, 123456, tzinfo=UTC)
    assert state.updated_at.tzinfo is UTC
    assert state.behavior == "continuous_broker_managed"
    assert "stepped" not in state.behavior


@pytest.mark.parametrize(
    "broker_status,expected",
    [
        ("new", "PENDING"),
        ("pending_new", "PENDING"),
        ("accepted", "ACTIVE"),
        ("replaced", "UPDATING"),
        ("pending_replace", "UPDATING"),
        ("filled", "TRIGGERED"),
        ("partially_filled", "TRIGGERED"),
        ("expired", "EXPIRED"),
        ("done_for_day", "EXPIRED"),
        ("canceled", "UNPROTECTED"),
        ("rejected", "UNPROTECTED"),
    ],
)
def test_status_mapping(broker_status: str, expected: str) -> None:
    assert parse_trailing_order_state(order_json(status=broker_status)).status == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"hwm": None},
        {"hwm": "not-a-decimal"},
        {"stop_price": None},
        {"stop_price": "not-a-decimal"},
    ],
)
def test_active_order_with_unconfirmed_broker_prices_is_unconfirmed(
    overrides: dict[str, Any],
) -> None:
    state = parse_trailing_order_state(order_json(**overrides))
    assert state.status == "UNCONFIRMED"
    if "hwm" in overrides:
        assert state.hwm is None
    if "stop_price" in overrides:
        assert state.stop_price is None


def test_partial_fill_uses_broker_quantities_for_residual_protection() -> None:
    state = parse_trailing_order_state(
        order_json(status="partially_filled", qty="10.5", filled_qty="3.25")
    )
    assert state.status == "TRIGGERED"
    assert state.qty == Decimal("10.5")
    assert state.filled_qty == Decimal("3.25")
    assert state.protected_quantity == Decimal("7.25")


class FakeHttpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str] | None]] = []

    def post(self, path: str, *, json_body: dict[str, str], params: Any) -> dict[str, Any]:
        self.calls.append(("POST", path, json_body))
        return order_json(client_order_id=json_body["client_order_id"])

    def patch(self, path: str, *, json_body: dict[str, str], params: Any) -> dict[str, Any]:
        self.calls.append(("PATCH", path, json_body))
        return order_json(trail_percent=None, trail_price=json_body.get("trail_price", "2"))

    def delete(self, path: str, *, params: Any) -> None:
        self.calls.append(("DELETE", path, None))


def test_mutations_use_only_the_injected_client() -> None:
    client = FakeHttpClient()
    submitted = submit_trailing_stop(
        client, "AAPL", Decimal("2"), policy(), "client-1"
    )
    replaced = replace_trailing_stop(
        client, submitted.order_id, trail_price=Decimal("2.50")
    )
    canceled = cancel_trailing_stop(client, submitted.order_id)

    assert submitted.client_order_id == "client-1"
    assert replaced.trail_price == Decimal("2.50")
    assert canceled is None
    assert [call[:2] for call in client.calls] == [
        ("POST", "/v2/orders"),
        ("PATCH", "/v2/orders/order-native-trailing-1"),
        ("DELETE", "/v2/orders/order-native-trailing-1"),
    ]
