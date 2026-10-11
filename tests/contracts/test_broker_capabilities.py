"""Contracts for the Alpaca broker capability matrix (Phase E).

Fixture/fake-only unit tests plus one credential-gated paper integration test
that is skipped unless explicit paper credentials and the operator opt-in
``MIKE_TRADE_ALPACA_PAPER_IT=1`` are present. The integration test performs
read-only paper checks only: it never references LIVE endpoints and never
places, replaces, or cancels orders.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from mike_trade.brokers.alpaca.assets import AlpacaAsset
from mike_trade.brokers.alpaca.auth import AlpacaEnvironment
from mike_trade.brokers.alpaca.capabilities import (
    BrokerCapabilities,
    CapabilityDescriptor,
    CapabilityEvidence,
    CapabilityStatus,
    CapabilityUnavailable,
    check_adapter_conformance,
    default_alpaca_capabilities,
    record_paper_verification,
)
from mike_trade.brokers.alpaca.http import AlpacaNotFound
from mike_trade.brokers.alpaca.orders import AlpacaOrder, AlpacaOrderService
from mike_trade.domain.errors import UnsupportedOrderCapabilityError

FP = "paper-fingerprint"
PAPER_IT_ENV_VAR = "MIKE_TRADE_ALPACA_PAPER_IT"

ALL_ORDER_TYPES = ("market", "limit", "stop", "stop_limit", "trailing_stop")
ALL_ORDER_CLASSES = ("simple", "oto", "oco", "bracket")
ALL_SIZINGS = ("whole", "fractional", "notional")
ALL_TIFS = ("day", "gtc", "opg", "cls", "ioc", "fok")
ALL_SIDES = ("buy", "sell", "short")
ALL_OPERATIONS = ("submit", "replace")
ALL_ENVS = (AlpacaEnvironment.PAPER, AlpacaEnvironment.LIVE)

EXPECTED_OPERATIONS = (
    "account",
    "positions",
    "assets",
    "clock",
    "open_orders",
    "lookup_by_id",
    "lookup_by_client_order_id",
    "submit",
    "replace",
    "cancel",
    "activities",
    "trade_update_stream",
)


def desc(**overrides: Any) -> CapabilityDescriptor:
    base: dict[str, Any] = {
        "order_type": "market",
        "order_class": "simple",
        "sizing": "whole",
        "time_in_force": "day",
        "side": "buy",
        "operation": "submit",
    }
    base.update(overrides)
    return CapabilityDescriptor(**base)


@pytest.fixture
def caps() -> BrokerCapabilities:
    return default_alpaca_capabilities()


# -- (a) unproven combinations fail closed ------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"order_type": "stop"},
        {"order_type": "stop", "side": "sell"},
        {"order_type": "stop_limit", "side": "sell"},
        {"order_class": "oto", "order_type": "stop", "side": "sell"},
        {"order_class": "oco"},
        {"order_class": "bracket"},
        {"order_class": "bracket", "order_type": "limit", "side": "sell"},
        {"sizing": "fractional", "order_type": "stop", "side": "sell"},
        {"sizing": "fractional", "order_class": "oto", "side": "sell"},
        {"sizing": "fractional", "order_type": "trailing_stop", "side": "sell"},
        {"sizing": "fractional", "time_in_force": "gtc"},
        {"sizing": "notional", "order_type": "limit", "operation": "submit"},
        {"order_type": "limit", "operation": "replace"},
        {"order_type": "stop", "operation": "replace", "side": "sell"},
        {"order_type": "trailing_stop", "operation": "replace", "side": "sell"},
        {"order_class": "oto", "operation": "replace"},
        {"time_in_force": "opg"},
        {"time_in_force": "cls"},
        {"time_in_force": "ioc"},
        {"time_in_force": "fok"},
        {"order_type": "stop_limit", "order_class": "oco", "sizing": "fractional",
         "time_in_force": "cls", "side": "sell", "operation": "replace"},
    ],
)
def test_unproven_combinations_fail_closed(
    caps: BrokerCapabilities, overrides: dict[str, Any]
) -> None:
    descriptor = desc(**overrides)
    for env in ALL_ENVS:
        assert caps.status_of(descriptor, env) is CapabilityStatus.UNPROVEN
        assert caps.is_available(descriptor, env) is False
        with pytest.raises(CapabilityUnavailable):
            caps.require(descriptor, env)


def test_stop_only_oto_bracket_oco_and_linked_replaces_are_unproven(
    caps: BrokerCapabilities,
) -> None:
    cases = [
        desc(order_type="stop", order_class="oto", side="sell"),
        desc(order_type="stop", order_class="bracket", side="sell"),
        desc(order_type="limit", order_class="oco", side="sell"),
        desc(order_type="limit", order_class="bracket", side="sell"),
        desc(order_type="stop", order_class="oto", side="sell", operation="replace"),
        desc(order_type="limit", order_class="oco", side="sell", operation="replace"),
        desc(order_type="limit", order_class="bracket", side="sell", operation="replace"),
        desc(order_type="stop", side="sell", operation="replace"),
        desc(order_type="trailing_stop", side="sell", operation="replace"),
        desc(sizing="fractional", time_in_force="gtc"),
    ]
    for descriptor in cases:
        for env in ALL_ENVS:
            assert caps.status_of(descriptor, env) is CapabilityStatus.UNPROVEN
            with pytest.raises(CapabilityUnavailable):
                caps.require(descriptor, env)


# -- (b) paper evidence does not promote LIVE ----------------------------------


def test_paper_evidence_promotes_only_paper(caps: BrokerCapabilities) -> None:
    descriptor = desc(order_type="stop", side="sell")
    assert caps.status_of(descriptor, AlpacaEnvironment.PAPER) is CapabilityStatus.UNPROVEN
    assert caps.status_of(descriptor, AlpacaEnvironment.LIVE) is CapabilityStatus.UNPROVEN

    promoted = record_paper_verification(
        caps,
        descriptor,
        record_id="PV-2026-001",
        verified_on="2026-10-08",
        note="paper fill observed",
    )

    assert promoted.is_available(descriptor, AlpacaEnvironment.PAPER) is True
    assert promoted.status_of(descriptor, AlpacaEnvironment.PAPER) is CapabilityStatus.PROVEN
    evidence = promoted.evidence_of(descriptor, AlpacaEnvironment.PAPER)
    assert isinstance(evidence, CapabilityEvidence)
    assert evidence.kind == "paper_verification"
    assert evidence.reference == "PV-2026-001"

    assert promoted.status_of(descriptor, AlpacaEnvironment.LIVE) is CapabilityStatus.UNPROVEN
    assert promoted.is_available(descriptor, AlpacaEnvironment.LIVE) is False
    with pytest.raises(CapabilityUnavailable):
        promoted.require(descriptor, AlpacaEnvironment.LIVE)

    # The input matrix is not mutated.
    assert caps.status_of(descriptor, AlpacaEnvironment.PAPER) is CapabilityStatus.UNPROVEN


def test_live_promotion_requires_explicit_operator_record(
    caps: BrokerCapabilities,
) -> None:
    descriptor = desc(order_type="stop", side="sell")
    promoted = record_paper_verification(
        caps, descriptor, record_id="PV-2026-002", verified_on="2026-10-08"
    )
    paper_evidence = promoted.evidence_of(descriptor, AlpacaEnvironment.PAPER)
    assert paper_evidence is not None
    with pytest.raises(ValueError, match="operator_record"):
        promoted.with_live_promotion(descriptor, paper_evidence)

    operator_evidence = CapabilityEvidence(
        kind="operator_record",
        reference="OP-ALPACA-LIVE-2026-001",
        recorded_on="2026-10-09",
        note="operator reviewed paper verification PV-2026-002",
    )
    live = promoted.with_live_promotion(descriptor, operator_evidence)
    assert live.is_available(descriptor, AlpacaEnvironment.LIVE) is True
    # PAPER entry is untouched by the LIVE promotion.
    assert live.evidence_of(descriptor, AlpacaEnvironment.PAPER) == paper_evidence


# -- (c) trailing_stop is standalone-only --------------------------------------


@pytest.mark.parametrize("order_class", ["oto", "oco", "bracket"])
@pytest.mark.parametrize("env", ALL_ENVS)
def test_trailing_stop_non_simple_is_never_available(
    caps: BrokerCapabilities, order_class: str, env: AlpacaEnvironment
) -> None:
    descriptor = desc(
        order_type="trailing_stop",
        order_class=order_class,
        sizing="whole",
        time_in_force="day",
        side="sell",
        operation="submit",
    )
    assert caps.status_of(descriptor, env) is CapabilityStatus.UNSUPPORTED
    assert caps.is_available(descriptor, env) is False
    with pytest.raises(CapabilityUnavailable):
        caps.require(descriptor, env)
    with pytest.raises(CapabilityUnavailable):
        record_paper_verification(
            caps, descriptor, record_id="PV-NEVER", verified_on="2026-10-08"
        )


def test_documented_standalone_trailing_stop_sell_is_proven(
    caps: BrokerCapabilities,
) -> None:
    for tif in ("day", "gtc"):
        descriptor = desc(
            order_type="trailing_stop",
            order_class="simple",
            sizing="whole",
            time_in_force=tif,
            side="sell",
            operation="submit",
        )
        for env in ALL_ENVS:
            assert caps.is_available(descriptor, env) is True
            assert caps.require(descriptor, env) == descriptor


# -- (d) notional replace is unavailable ---------------------------------------


def test_notional_replace_is_unavailable(caps: BrokerCapabilities) -> None:
    checked = 0
    for order_type, order_class, tif, side, env in itertools.product(
        ALL_ORDER_TYPES, ALL_ORDER_CLASSES, ALL_TIFS, ("buy", "sell"), ALL_ENVS
    ):
        descriptor = desc(
            order_type=order_type,
            order_class=order_class,
            sizing="notional",
            time_in_force=tif,
            side=side,
            operation="replace",
        )
        assert caps.status_of(descriptor, env) is CapabilityStatus.UNSUPPORTED
        assert caps.is_available(descriptor, env) is False
        with pytest.raises(CapabilityUnavailable):
            caps.require(descriptor, env)
        checked += 1
    assert checked == 5 * 4 * 6 * 2 * 2


# -- (e) short selling is unproven until broker-verified --------------------------


def test_short_selling_is_never_available_until_verified(caps: BrokerCapabilities) -> None:
    checked = 0
    for order_type, operation, env in itertools.product(
        ALL_ORDER_TYPES, ALL_OPERATIONS, ALL_ENVS
    ):
        descriptor = desc(order_type=order_type, side="short", operation=operation)
        # Short combinations are UNPROVEN (never PROVEN by default): fail closed until a
        # paper verification or explicit promotion is recorded.
        assert caps.status_of(descriptor, env) is not CapabilityStatus.PROVEN
        assert caps.is_available(descriptor, env) is False
        with pytest.raises(CapabilityUnavailable):
            caps.require(descriptor, env)
        checked += 1
    assert checked == 5 * 2 * 2


# -- (f) the matrix is complete -------------------------------------------------


def test_matrix_is_complete_every_descriptor_resolves(caps: BrokerCapabilities) -> None:
    proven = 0
    total = 0
    for values in itertools.product(
        ALL_ORDER_TYPES, ALL_ORDER_CLASSES, ALL_SIZINGS, ALL_TIFS, ALL_SIDES, ALL_OPERATIONS
    ):
        descriptor = CapabilityDescriptor(
            order_type=values[0],
            order_class=values[1],
            sizing=values[2],
            time_in_force=values[3],
            side=values[4],
            operation=values[5],
        )
        for env in ALL_ENVS:
            total += 1
            status = caps.status_of(descriptor, env)  # must never raise KeyError
            assert isinstance(status, CapabilityStatus)
            available = caps.is_available(descriptor, env)
            assert isinstance(available, bool)
            assert available is (status is CapabilityStatus.PROVEN)
            assert caps.evidence_of(descriptor, env) is None or isinstance(
                caps.evidence_of(descriptor, env), CapabilityEvidence
            )
            if available:
                assert caps.require(descriptor, env) == descriptor
                proven += 1
            else:
                with pytest.raises(CapabilityUnavailable):
                    caps.require(descriptor, env)
    assert total == 5 * 4 * 3 * 6 * 3 * 2 * 2
    # Only the documented basics are proven: 8 market/limit + 2 trailing per env.
    assert proven == (8 + 2) * 2


def test_initial_documented_basics_are_proven(caps: BrokerCapabilities) -> None:
    for order_type in ("market", "limit"):
        for tif in ("day", "gtc"):
            for side in ("buy", "sell"):
                descriptor = desc(order_type=order_type, time_in_force=tif, side=side)
                for env in ALL_ENVS:
                    assert caps.status_of(descriptor, env) is CapabilityStatus.PROVEN
                    evidence = caps.evidence_of(descriptor, env)
                    assert evidence is not None
                    assert evidence.reference


# -- (g) adapter conformance -----------------------------------------------------


class _FullFakeAdapter:
    """Fake exposing every required broker port operation."""

    def get_account(self) -> None: ...
    def list_positions(self) -> None: ...
    def get_asset(self, symbol: str) -> None: ...
    def list_assets(self) -> None: ...
    def get_clock(self) -> None: ...
    def list_open_orders(self) -> None: ...
    def get_order(self, order_id: str) -> None: ...
    def get_order_by_client_id(self, client_order_id: str) -> None: ...
    def submit_order(self, intent: Any) -> None: ...
    def replace_order(self, order_id: str) -> None: ...
    def cancel_order(self, order_id: str) -> None: ...
    def list_activities(self) -> None: ...
    def list_fills(self, since: Any) -> None: ...
    def events(self) -> None: ...


class _PartialFakeAdapter:
    """Fake exposing only read operations."""

    def get_account(self) -> None: ...
    def list_positions(self) -> None: ...


def test_conformance_full_fake_adapter_passes() -> None:
    report = check_adapter_conformance(_FullFakeAdapter())
    assert report["ok"] is True
    assert report["missing"] == ()
    assert set(report["present"]) == set(EXPECTED_OPERATIONS)


def test_conformance_partial_adapter_reports_missing() -> None:
    report = check_adapter_conformance(_PartialFakeAdapter())
    assert report["ok"] is False
    assert "account" in report["present"]
    assert "positions" in report["present"]
    assert "submit" in report["missing"]
    assert "cancel" in report["missing"]
    assert "trade_update_stream" in report["missing"]


def test_conformance_accepts_collection_of_services() -> None:
    services = {
        "account": _PartialFakeAdapter(),
        "orders": _FullFakeAdapter(),
    }
    report = check_adapter_conformance(services)
    assert report["ok"] is True
    assert report["missing"] == ()


def test_conformance_rejects_empty_and_non_adapters() -> None:
    with pytest.raises(ValueError):
        check_adapter_conformance([])
    with pytest.raises(TypeError):
        check_adapter_conformance("not-an-adapter")


def test_alpaca_modules_expose_required_operations() -> None:
    from mike_trade.brokers.alpaca import (
        account as account_module,
    )
    from mike_trade.brokers.alpaca import (
        activities as activities_module,
    )
    from mike_trade.brokers.alpaca import (
        assets as assets_module,
    )
    from mike_trade.brokers.alpaca import (
        orders as orders_module,
    )
    from mike_trade.brokers.alpaca import (
        updates as updates_module,
    )

    assert callable(getattr(account_module.AlpacaAccountService, "get_account", None))
    assert callable(getattr(account_module.AlpacaAccountService, "list_positions", None))
    assert callable(getattr(account_module.AlpacaAccountService, "get_clock", None))
    assert callable(getattr(assets_module.AlpacaAssetService, "get_asset", None))
    assert callable(getattr(assets_module.AlpacaAssetService, "list_assets", None))
    assert callable(getattr(orders_module.AlpacaOrderService, "submit_order", None))
    assert callable(getattr(orders_module.AlpacaOrderService, "replace_order", None))
    assert callable(getattr(orders_module.AlpacaOrderService, "cancel_order", None))
    assert callable(getattr(orders_module.AlpacaOrderService, "get_order", None))
    assert callable(
        getattr(orders_module.AlpacaOrderService, "get_order_by_client_order_id", None)
    )
    assert callable(getattr(orders_module.AlpacaOrderService, "list_open_orders", None))
    assert callable(getattr(activities_module.AlpacaActivityService, "list_activities", None))
    assert callable(getattr(updates_module.TradeUpdateStream, "events", None))
    assert callable(getattr(updates_module.TradeUpdate, "from_message", None))
    # Paper/live Trading API endpoints are distinct; Broker API hosts are rejected.
    assert "paper-api.alpaca.markets" in AlpacaEnvironment.PAPER.base_url
    assert "api.alpaca.markets" in AlpacaEnvironment.LIVE.base_url
    assert AlpacaEnvironment.PAPER.base_url != AlpacaEnvironment.LIVE.base_url


@pytest.mark.xfail(
    reason=(
        "Known gap: no unified AlpacaBrokerAdapter exists yet; e.1-e.4 expose "
        "separate services (account/assets/orders/activities/updates) behind "
        "the broker port. See docs/ALPACA.md."
    ),
    strict=True,
)
def test_unified_alpaca_adapter_exists() -> None:
    from mike_trade.brokers.alpaca import adapter as unified_adapter_module

    assert callable(getattr(unified_adapter_module, "AlpacaBrokerAdapter", None))


def _fractionable_asset() -> AlpacaAsset:
    return AlpacaAsset(
        environment=AlpacaEnvironment.PAPER,
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
        fractionable=True,
        min_order_size=Decimal("0.0001"),
        min_trade_increment=Decimal("0.0001"),
        price_increment=Decimal("0.01"),
    )


def _order_json(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "order-1",
        "client_order_id": "client-1",
        "symbol": "AAPL",
        "status": "accepted",
        "side": "sell",
        "type": "trailing_stop",
        "time_in_force": "day",
        "order_class": "simple",
        "qty": "1",
        "notional": None,
        "filled_qty": "0",
        "filled_avg_price": None,
        "limit_price": None,
        "stop_price": None,
        "trail_price": "2.50",
        "trail_percent": None,
        "hwm": None,
        "legs": [],
        "created_at": "2026-10-08T13:00:00Z",
        "updated_at": "2026-10-08T13:00:01Z",
        "submitted_at": "2026-10-08T13:00:01Z",
        "filled_at": None,
        "expired_at": None,
        "canceled_at": None,
        "failed_at": None,
        "replaced_at": None,
    }
    base.update(overrides)
    return base


class _OrderFakeClient:
    """Fake HTTP surface for the order service; records calls, no network."""

    def __init__(self) -> None:
        self.environment = AlpacaEnvironment.PAPER
        self.fingerprint = FP
        self.calls: list[tuple[str, str]] = []

    def get(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path))
        raise AlpacaNotFound("missing", status_code=404)

    def post(
        self,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        self.calls.append(("POST", path))
        return _order_json(
            client_order_id=(dict(json_body or {}).get("client_order_id") or "client-1")
        )

    def patch(
        self,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        self.calls.append(("PATCH", path))
        return _order_json()

    def delete(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        self.calls.append(("DELETE", path))
        return None


def test_legacy_checker_mapping() -> None:
    checker = default_alpaca_capabilities().as_legacy_checker(AlpacaEnvironment.PAPER)
    assert checker("standalone_trailing_stop_sell:whole:day") is True
    assert checker("standalone_trailing_stop_sell:whole:gtc") is True
    assert checker("standalone_trailing_stop_sell:fractional:day") is False
    assert checker("standalone_stop_sell:fractional:day") is False
    assert checker("standalone_stop_sell:whole:gtc") is False
    assert checker("replace_stop_price") is False
    assert checker("replace_trail_price") is False
    assert checker("replace_limit_price") is False
    assert checker("not-a-capability") is False
    assert checker("") is False


def test_order_service_rejects_unproven_combination_when_wired() -> None:
    capabilities = default_alpaca_capabilities()
    checker = capabilities.as_legacy_checker(AlpacaEnvironment.PAPER)
    client = _OrderFakeClient()
    service = AlpacaOrderService(
        client, persist_before_send=lambda client_order_id, request: None
    )
    # Fractional trailing-stop sell is UNPROVEN: rejected before any HTTP call.
    with pytest.raises(UnsupportedOrderCapabilityError):
        service.submit_trailing_stop_sell(
            asset=_fractionable_asset(),
            qty=Decimal("1.5"),
            known_position_qty=Decimal("5"),
            capability_check=checker,
            trail_price=Decimal("2.50"),
            cycle_id="cycle-1",
        )
    # Fractional protective stop sell is UNPROVEN as well.
    with pytest.raises(UnsupportedOrderCapabilityError):
        service.submit_stop_sell(
            asset=_fractionable_asset(),
            qty=Decimal("1.5"),
            stop_price=Decimal("170.00"),
            known_position_qty=Decimal("5"),
            capability_check=checker,
            cycle_id="cycle-1",
        )
    assert client.calls == []


def test_order_service_accepts_proven_combination_when_wired() -> None:
    capabilities = default_alpaca_capabilities()
    checker = capabilities.as_legacy_checker(AlpacaEnvironment.PAPER)
    client = _OrderFakeClient()
    service = AlpacaOrderService(
        client, persist_before_send=lambda client_order_id, request: None
    )
    result = service.submit_trailing_stop_sell(
        asset=_fractionable_asset(),
        qty=Decimal("1"),
        known_position_qty=Decimal("5"),
        capability_check=checker,
        trail_price=Decimal("2.50"),
        cycle_id="cycle-1",
    )
    assert isinstance(result, AlpacaOrder)
    assert result.side == "sell"
    assert ("POST", "/v2/orders") in client.calls


# -- (h) credential-gated paper integration (read-only) ---------------------------


@pytest.mark.paper
def test_paper_readonly_account_clock_asset() -> None:
    """Read-only paper verification: account, clock, and one asset lookup.

    Skipped unless ``MIKE_TRADE_ALPACA_PAPER_IT=1`` and paper credentials are
    present. Never references LIVE endpoints and never submits orders.
    """
    if os.environ.get(PAPER_IT_ENV_VAR) != "1":
        pytest.skip(f"paper integration requires {PAPER_IT_ENV_VAR}=1")
    key_id = os.environ.get("ALPACA_PAPER_KEY_ID", "").strip()
    secret_key = os.environ.get("ALPACA_PAPER_SECRET_KEY", "").strip()
    if not key_id or not secret_key:
        pytest.skip("paper integration requires ALPACA_PAPER_KEY_ID/ALPACA_PAPER_SECRET_KEY")

    from mike_trade.brokers.alpaca.account import AlpacaAccountService
    from mike_trade.brokers.alpaca.assets import AlpacaAssetService
    from mike_trade.brokers.alpaca.auth import AlpacaCredentials
    from mike_trade.brokers.alpaca.http import AlpacaHttpClient

    credentials = AlpacaCredentials(
        key_id=key_id, secret_key=secret_key, environment=AlpacaEnvironment.PAPER
    )
    client = AlpacaHttpClient(credentials)
    try:
        # Hard isolation guards: PAPER only, never LIVE, never Broker API.
        assert client.environment is AlpacaEnvironment.PAPER
        assert client.base_url == AlpacaEnvironment.PAPER.base_url
        assert "paper-api.alpaca.markets" in client.base_url
        assert "broker-api" not in client.base_url

        account_service = AlpacaAccountService(
            client,
            environment=AlpacaEnvironment.PAPER,
            fingerprint=client.fingerprint,
        )
        snapshot = account_service.get_account()
        assert snapshot.environment is AlpacaEnvironment.PAPER
        assert snapshot.fingerprint == client.fingerprint

        clock = account_service.get_clock()
        assert clock.environment is AlpacaEnvironment.PAPER
        assert isinstance(clock.is_open, bool)

        asset_service = AlpacaAssetService(
            client,
            environment=AlpacaEnvironment.PAPER,
            fingerprint=client.fingerprint,
        )
        asset = asset_service.get_asset("AAPL")
        assert asset.symbol == "AAPL"
        assert asset.environment is AlpacaEnvironment.PAPER
    finally:
        client.close()
