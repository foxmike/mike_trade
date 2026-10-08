"""Offline contracts for Alpaca equity order operations."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC
from decimal import Decimal
from typing import Any

import pytest

from mike_trade.brokers.alpaca.assets import AlpacaAsset
from mike_trade.brokers.alpaca.auth import AlpacaEnvironment, LiveAuthorizationRequired
from mike_trade.brokers.alpaca.http import (
    AlpacaNotFound,
    AlpacaTransportError,
    AlpacaUnknownOutcome,
    AlpacaUnprocessable,
)
from mike_trade.brokers.alpaca.orders import (
    AlpacaOrder,
    AlpacaOrderService,
    CancelResult,
    CapabilityUnavailable,
    NotSubmitted,
    OrderOutcomeUnresolved,
    OrderPurpose,
    OrderSizing,
    OversellRejected,
    make_client_order_id,
    parse_client_order_id,
)

FP = "paper-fingerprint"


def asset(
    *,
    fractionable: bool = True,
    environment: AlpacaEnvironment = AlpacaEnvironment.PAPER,
) -> AlpacaAsset:
    return AlpacaAsset(
        environment=environment,
        fingerprint=FP,
        asset_id="asset-aapl",
        symbol="AAPL",
        exchange="NASDAQ",
        asset_class="us_equity",
        status="active",
        tradable=True,
        marginable=True,
        shortable=True,
        easy_to_borrow=True,
        fractionable=fractionable,
        min_order_size=Decimal("0.0001"),
        min_trade_increment=Decimal("0.0001"),
        price_increment=Decimal("0.01"),
    )


def order_json(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": "order-1",
        "client_order_id": "client-1",
        "symbol": "AAPL",
        "status": "accepted",
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "order_class": "simple",
        "qty": "1.2500",
        "notional": None,
        "filled_qty": "0.2500",
        "filled_avg_price": "182.15",
        "limit_price": "183.25",
        "stop_price": "175.25",
        "trail_price": "2.50",
        "trail_percent": "1.25",
        "hwm": "186.75",
        "legs": [],
        "created_at": "2026-10-08T13:00:00-04:00",
        "updated_at": "2026-10-08T17:01:00Z",
        "submitted_at": "2026-10-08T17:00:01Z",
        "filled_at": None,
        "expired_at": None,
        "canceled_at": None,
        "failed_at": None,
        "replaced_at": None,
    }
    result.update(overrides)
    return result


class FakeClient:
    def __init__(
        self,
        *,
        environment: AlpacaEnvironment = AlpacaEnvironment.PAPER,
        live_authorized: bool | None = None,
    ) -> None:
        self.environment = environment
        self.fingerprint = FP
        if live_authorized is not None:
            self.live_authorized = live_authorized
        self.calls: list[tuple[str, str, Any]] = []
        self.post_action: Any = order_json()
        self.patch_action: Any = order_json()
        self.delete_action: Any = None
        self.get_actions: dict[str, Any] = {}

    @staticmethod
    def _resolve(action: Any, *args: Any) -> Any:
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action(*args)
        return action

    def get(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path, params))
        action = self.get_actions.get(path, AlpacaNotFound("missing", status_code=404))
        return self._resolve(action, path, params)

    def post(
        self,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        self.calls.append(("POST", path, dict(json_body or {})))
        return self._resolve(self.post_action, path, json_body, params)

    def patch(
        self,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        self.calls.append(("PATCH", path, dict(json_body or {})))
        return self._resolve(self.patch_action, path, json_body, params)

    def delete(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        self.calls.append(("DELETE", path, params))
        return self._resolve(self.delete_action, path, params)


def service(
    client: FakeClient | None = None,
    *,
    persist: Any = lambda client_order_id, request: None,
) -> tuple[AlpacaOrderService, FakeClient]:
    fake = client or FakeClient()
    return AlpacaOrderService(fake, persist_before_send=persist), fake


def test_client_order_id_is_deterministic_bounded_and_auditable() -> None:
    inputs = (
        AlpacaEnvironment.PAPER,
        FP,
        "cycle-with-a-very-long-opaque-id",
        OrderPurpose.ENTRY,
        12,
    )
    first = make_client_order_id(*inputs)
    assert first == make_client_order_id(*inputs)
    assert len(first) <= 48
    assert parse_client_order_id(first) == {
        "version": 1,
        "purpose": OrderPurpose.ENTRY,
        "attempt": 12,
        "digest": first.rsplit("-", 1)[1],
        "client_order_id": first,
    }
    variants = {
        make_client_order_id(AlpacaEnvironment.LIVE, FP, inputs[2], inputs[3], inputs[4]),
        make_client_order_id(inputs[0], "another-account", inputs[2], inputs[3], inputs[4]),
        make_client_order_id(inputs[0], FP, "another-cycle", inputs[3], inputs[4]),
        make_client_order_id(inputs[0], FP, inputs[2], OrderPurpose.EXIT, inputs[4]),
        make_client_order_id(inputs[0], FP, inputs[2], inputs[3], 13),
    }
    assert first not in variants
    assert len(variants) == 5


def test_persist_happens_before_post_and_failure_aborts_transport() -> None:
    events: list[str] = []
    fake = FakeClient()

    def persist(client_order_id: str, request: Mapping[str, Any]) -> None:
        events.append(f"persist:{client_order_id}")
        assert request["client_order_id"] == client_order_id
        raise RuntimeError("disk full")

    svc = AlpacaOrderService(fake, persist_before_send=persist)
    with pytest.raises(RuntimeError, match="disk full"):
        svc.submit_order(
            asset=asset(),
            side="buy",
            order_type="market",
            qty=Decimal("1"),
            cycle_id="cycle-1",
            purpose=OrderPurpose.ENTRY,
        )
    assert len(events) == 1
    assert fake.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"qty": Decimal("1"), "notional": Decimal("100")},
    ],
)
def test_quantity_and_notional_are_mutually_exclusive(kwargs: dict[str, Any]) -> None:
    svc, fake = service()
    with pytest.raises(ValueError, match="exactly one"):
        svc.submit_order(
            asset=asset(),
            side="buy",
            order_type="market",
            cycle_id="cycle-1",
            purpose=OrderPurpose.ENTRY,
            **kwargs,
        )
    assert fake.calls == []


def test_fractional_and_notional_require_fractionable_asset() -> None:
    svc, fake = service()
    with pytest.raises(CapabilityUnavailable, match="fractional"):
        svc.submit_order(
            asset=asset(fractionable=False),
            side="buy",
            order_type="market",
            qty=Decimal("1.5"),
            cycle_id="cycle-1",
            purpose=OrderPurpose.ENTRY,
        )
    with pytest.raises(CapabilityUnavailable, match="notional"):
        svc.submit_order(
            asset=asset(fractionable=False),
            side="buy",
            order_type="market",
            notional=Decimal("25.00"),
            cycle_id="cycle-1",
            purpose=OrderPurpose.ENTRY,
        )
    assert fake.calls == []


def test_fractional_and_notional_require_day_tif() -> None:
    svc, fake = service()
    with pytest.raises(CapabilityUnavailable, match="DAY"):
        svc.submit_order(
            asset=asset(),
            side="buy",
            order_type="limit",
            qty=Decimal("1.5"),
            limit_price=Decimal("10.00"),
            time_in_force="gtc",
            cycle_id="cycle-1",
            purpose=OrderPurpose.ENTRY,
        )
    assert fake.calls == []


def test_quantity_limit_and_notional_precision_are_strict() -> None:
    svc, fake = service()
    with pytest.raises(ValueError, match="min_trade_increment"):
        svc.submit_order(
            asset=asset(),
            side="buy",
            order_type="market",
            qty=Decimal("1.00001"),
            cycle_id="cycle-1",
            purpose=OrderPurpose.ENTRY,
        )
    with pytest.raises(ValueError, match="price_increment"):
        svc.submit_order(
            asset=asset(),
            side="buy",
            order_type="limit",
            notional=Decimal("25.001"),
            limit_price=Decimal("180.001"),
            cycle_id="cycle-1",
            purpose=OrderPurpose.ENTRY,
        )
    assert fake.calls == []


def test_trailing_stop_is_standalone_sell_with_exactly_one_trail_field() -> None:
    captured: dict[str, Any] = {}
    fake = FakeClient()

    def response(_path: str, body: Mapping[str, Any], _params: Any) -> dict[str, Any]:
        captured.update(body)
        return order_json(
            client_order_id=body["client_order_id"],
            side="sell",
            type="trailing_stop",
            order_class=None,
            qty=body["qty"],
            limit_price=None,
            stop_price=None,
            trail_price=body.get("trail_price"),
            trail_percent=body.get("trail_percent"),
        )

    fake.post_action = response
    svc, _ = service(fake)
    result = svc.submit_trailing_stop_sell(
        asset=asset(),
        qty=Decimal("2"),
        trail_percent=Decimal("1.5"),
        known_position_qty=Decimal("2"),
        capability_check=lambda _: True,
        cycle_id="cycle-1",
    )
    assert isinstance(result, AlpacaOrder)
    assert captured["side"] == "sell"
    assert captured["type"] == "trailing_stop"
    assert captured["trail_percent"] == "1.5"
    assert "trail_price" not in captured
    assert "order_class" not in captured

    with pytest.raises(ValueError, match="exactly one"):
        svc.submit_trailing_stop_sell(
            asset=asset(),
            qty=Decimal("2"),
            known_position_qty=Decimal("2"),
            capability_check=lambda _: True,
            cycle_id="cycle-1",
        )
    with pytest.raises(ValueError, match="exactly one"):
        svc.submit_trailing_stop_sell(
            asset=asset(),
            qty=Decimal("2"),
            trail_price=Decimal("2"),
            trail_percent=Decimal("1"),
            known_position_qty=Decimal("2"),
            capability_check=lambda _: True,
            cycle_id="cycle-1",
        )


def test_protective_orders_fail_closed_without_proven_capability() -> None:
    svc, fake = service()
    with pytest.raises(CapabilityUnavailable):
        svc.submit_stop_sell(
            asset=asset(),
            qty=Decimal("2"),
            stop_price=Decimal("170.00"),
            known_position_qty=Decimal("2"),
            capability_check=lambda _: False,
            cycle_id="cycle-1",
        )
    with pytest.raises(CapabilityUnavailable):
        svc.submit_trailing_stop_sell(
            asset=asset(),
            qty=Decimal("2"),
            trail_price=Decimal("2.00"),
            known_position_qty=Decimal("2"),
            capability_check=None,
            cycle_id="cycle-1",
        )
    assert fake.calls == []


def test_sell_quantity_cannot_exceed_known_position() -> None:
    svc, fake = service()
    with pytest.raises(OversellRejected):
        svc.submit_order(
            asset=asset(),
            side="sell",
            order_type="market",
            sizing=OrderSizing(qty=Decimal("3")),
            known_position_qty=Decimal("2"),
            cycle_id="cycle-1",
            purpose=OrderPurpose.EXIT,
        )
    assert fake.calls == []


def test_timeout_resolves_by_same_client_id_without_second_post() -> None:
    fake = FakeClient()
    fake.post_action = AlpacaUnknownOutcome("unknown")

    def found(_path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        return order_json(client_order_id=params["client_order_id"])

    fake.get_actions["/v2/orders:by_client_order_id"] = found
    persisted: list[str] = []
    svc, _ = service(fake, persist=lambda client_id: persisted.append(client_id))
    result = svc.submit_order(
        asset=asset(),
        side="buy",
        order_type="market",
        qty=Decimal("1"),
        cycle_id="cycle-1",
        purpose=OrderPurpose.ENTRY,
    )
    assert isinstance(result, AlpacaOrder)
    assert result.client_order_id == persisted[0]
    assert [call[0] for call in fake.calls] == ["POST", "GET"]
    assert fake.calls[1][2] == {"client_order_id": persisted[0]}


def test_confirmed_404_after_unknown_outcome_returns_not_submitted() -> None:
    fake = FakeClient()
    fake.post_action = AlpacaUnknownOutcome("unknown")
    fake.get_actions["/v2/orders:by_client_order_id"] = AlpacaNotFound(
        "not found", status_code=404
    )
    svc, _ = service(fake)
    result = svc.submit_order(
        asset=asset(),
        side="buy",
        order_type="market",
        qty=Decimal("1"),
        cycle_id="cycle-1",
        purpose=OrderPurpose.ENTRY,
    )
    assert isinstance(result, NotSubmitted)
    assert result.retry_with_same_client_order_id is True
    assert [call[0] for call in fake.calls] == ["POST", "GET"]


def test_failed_unknown_outcome_lookup_does_not_post_again_or_mint_another_id() -> None:
    fake = FakeClient()
    fake.post_action = AlpacaUnknownOutcome("unknown")
    fake.get_actions["/v2/orders:by_client_order_id"] = AlpacaTransportError("lookup lost")
    persisted: list[str] = []
    svc, _ = service(fake, persist=lambda client_id: persisted.append(client_id))
    with pytest.raises(OrderOutcomeUnresolved) as raised:
        svc.submit_order(
            asset=asset(),
            side="buy",
            order_type="market",
            qty=Decimal("1"),
            cycle_id="cycle-1",
            purpose=OrderPurpose.ENTRY,
        )
    original_id = raised.value.client_order_id
    with pytest.raises(OrderOutcomeUnresolved) as repeated:
        svc.submit_order(
            asset=asset(),
            side="buy",
            order_type="market",
            qty=Decimal("1"),
            cycle_id="cycle-1",
            purpose=OrderPurpose.ENTRY,
            attempt=1,
        )
    assert repeated.value.client_order_id == original_id
    assert persisted == [original_id]
    assert [call[0] for call in fake.calls] == ["POST", "GET", "GET"]


def test_cancel_404_or_422_is_typed_already_terminal_result() -> None:
    for error in (
        AlpacaNotFound("terminal", status_code=404),
        AlpacaUnprocessable("terminal", status_code=422),
    ):
        fake = FakeClient()
        fake.delete_action = error
        svc, _ = service(fake)
        result = svc.cancel_order("order-1")
        assert result == CancelResult(
            order_id="order-1",
            cancel_requested=False,
            already_terminal=True,
            status="already_terminal",
        )


def test_notional_order_replacement_is_typed_rejection_before_patch() -> None:
    fake = FakeClient()
    fake.get_actions["/v2/orders/order-1"] = order_json(qty=None, notional="100.00")
    svc, _ = service(fake)
    with pytest.raises(CapabilityUnavailable, match="cannot be replaced"):
        svc.replace_order(
            "order-1",
            asset=asset(),
            capability_check=lambda _: True,
            limit_price=Decimal("181.00"),
        )
    assert [call[0] for call in fake.calls] == ["GET"]


def test_linked_replacement_is_never_assumed() -> None:
    fake = FakeClient()
    fake.get_actions["/v2/orders/order-1"] = order_json(order_class="oto")
    svc, _ = service(fake)
    with pytest.raises(CapabilityUnavailable, match="linked"):
        svc.replace_order(
            "order-1",
            asset=asset(),
            capability_check=lambda _: True,
            stop_price=Decimal("175.00"),
        )
    assert [call[0] for call in fake.calls] == ["GET"]


def test_capability_proven_limit_price_replacement_uses_patch() -> None:
    fake = FakeClient()
    fake.get_actions["/v2/orders/order-1"] = order_json()

    def replaced(_path: str, body: Mapping[str, Any], _params: Any) -> dict[str, Any]:
        assert body == {"limit_price": "181.00"}
        return order_json(limit_price=body["limit_price"], status="replaced")

    fake.patch_action = replaced
    svc, _ = service(fake)
    result = svc.replace_order(
        "order-1",
        asset=asset(),
        capability_check=lambda capability: capability == "replace_limit_price",
        limit_price=Decimal("181.00"),
    )
    assert result.limit_price == Decimal("181.00")
    assert [call[0] for call in fake.calls] == ["GET", "PATCH"]


def test_lookup_list_and_decimal_utc_normalization() -> None:
    fake = FakeClient()
    payload = order_json()
    fake.get_actions["/v2/orders/order-1"] = payload
    fake.get_actions["/v2/orders:by_client_order_id"] = payload
    fake.get_actions["/v2/orders"] = [payload]
    svc, _ = service(fake)
    by_id = svc.get_order("order-1")
    by_client = svc.get_order_by_client_order_id("client-1")
    listed = svc.list_open_orders(limit=25, nested=True)
    assert by_id == by_client == listed[0]
    assert by_id is not None
    for field in (
        "qty",
        "notional",
        "filled_qty",
        "filled_avg_price",
        "limit_price",
        "stop_price",
        "trail_price",
        "trail_percent",
        "hwm",
    ):
        value = getattr(by_id, field)
        assert value is None or isinstance(value, Decimal), field
        assert not isinstance(value, float)
    assert by_id.qty == Decimal("1.2500")
    assert by_id.hwm == Decimal("186.75")
    assert by_id.created_at is not None and by_id.created_at.tzinfo is UTC
    assert by_id.created_at.hour == 17
    assert by_id.environment is AlpacaEnvironment.PAPER
    assert fake.calls[-1] == (
        "GET",
        "/v2/orders",
        {"status": "open", "limit": 25, "nested": True},
    )


@pytest.mark.parametrize("operation", ["submit", "replace", "cancel"])
def test_live_mutations_require_authorization(operation: str) -> None:
    fake = FakeClient(environment=AlpacaEnvironment.LIVE, live_authorized=False)
    svc, _ = service(fake)
    with pytest.raises(LiveAuthorizationRequired):
        if operation == "submit":
            svc.submit_order(
                asset=asset(environment=AlpacaEnvironment.LIVE),
                side="buy",
                order_type="market",
                qty=Decimal("1"),
                cycle_id="cycle-1",
                purpose=OrderPurpose.ENTRY,
            )
        elif operation == "replace":
            svc.replace_order(
                "order-1",
                asset=asset(environment=AlpacaEnvironment.LIVE),
                capability_check=lambda _: True,
                limit_price=Decimal("180.00"),
            )
        else:
            svc.cancel_order("order-1")
    assert fake.calls == []
