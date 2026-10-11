"""Offline contracts for Alpaca short-side order safety.

Alpaca transports a short sale as ``sell`` and a cover as ``buy``.  The adapter therefore has to
prove the *position effect* of every order from the verified broker position: a short entry only
from flat, a cover only against a verified short, a sell only against a long, and no order may
ever open, add to or reverse a position by accident.  Short order shapes are UNPROVEN by
default, so nothing reaches the broker until an operator records a verification.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from test_alpaca_orders import asset, order_json, service

from mike_trade.brokers.alpaca.capabilities import default_alpaca_capabilities
from mike_trade.brokers.alpaca.orders import (
    CapabilityUnavailable,
    OrderPurpose,
    OrderSizing,
    parse_client_order_id,
)
from mike_trade.domain.errors import BrokerRejectedError

PROVEN = lambda _name: True  # noqa: E731 - a verified-capability stand-in
UNPROVEN_BY_DEFAULT = default_alpaca_capabilities().as_legacy_checker("PAPER")


def short_entry(svc, **overrides):
    values = dict(
        asset=asset(fractionable=False), side="sell", order_type="market",
        sizing=OrderSizing(qty=Decimal("5")), cycle_id="cycle-1",
        purpose=OrderPurpose.ENTRY_SHORT, capability_check=PROVEN,
    )
    values.update(overrides)
    return svc.submit_order(**values)


def cover(svc, **overrides):
    values = dict(
        asset=asset(fractionable=False), side="buy", order_type="market",
        sizing=OrderSizing(qty=Decimal("5")), cycle_id="cycle-1",
        purpose=OrderPurpose.EXIT_SHORT, known_position_qty=Decimal("5"),
        known_position_side="short",
    )
    values.update(overrides)
    return svc.submit_order(**values)


# ------------------------------------------------------------------ short entry


def test_short_entry_is_unproven_by_default_and_never_reaches_the_broker():
    svc, fake = service()
    with pytest.raises(CapabilityUnavailable):
        short_entry(svc, capability_check=UNPROVEN_BY_DEFAULT)
    with pytest.raises(CapabilityUnavailable):
        short_entry(svc, capability_check=None)
    assert fake.calls == []


def test_a_proven_short_entry_is_a_whole_share_sell_with_short_lineage():
    svc, fake = service()
    fake.post_action = order_json(side="sell", qty="5", filled_qty="0", client_order_id="x")
    short_entry(svc)
    ((method, path, body),) = fake.calls
    assert (method, path) == ("POST", "/v2/orders")
    assert body["side"] == "sell" and body["qty"] == "5" and body["type"] == "market"
    assert parse_client_order_id(body["client_order_id"])["purpose"] is OrderPurpose.ENTRY_SHORT


def test_fractional_short_sales_are_refused():
    svc, fake = service()
    with pytest.raises(BrokerRejectedError) as raised:
        short_entry(svc, asset=asset(fractionable=True), sizing=OrderSizing(qty=Decimal("1.5")))
    assert raised.value.code == "short_requires_whole_shares" and fake.calls == []


@pytest.mark.parametrize("known_side, qty", [("long", "3"), ("short", "3")])
def test_a_short_entry_requires_a_flat_symbol(known_side, qty):
    svc, fake = service()
    with pytest.raises(BrokerRejectedError) as raised:
        short_entry(svc, known_position_qty=Decimal(qty), known_position_side=known_side)
    assert raised.value.code == "position_already_open" and fake.calls == []


# ------------------------------------------------------------------------ cover


def test_a_cover_is_a_buy_against_a_verified_short_with_cover_lineage():
    svc, fake = service()
    fake.post_action = order_json(side="buy", qty="5", filled_qty="0")
    cover(svc)
    ((_, _, body),) = fake.calls
    assert body["side"] == "buy"
    assert parse_client_order_id(body["client_order_id"])["purpose"] is OrderPurpose.EXIT_SHORT


@pytest.mark.parametrize("overrides, code", [
    (dict(known_position_qty=None, known_position_side=None), "cover_without_short"),
    (dict(known_position_qty=Decimal("5"), known_position_side="long"), "cover_without_short"),
    (dict(known_position_qty=Decimal("0"), known_position_side=None), "cover_without_short"),
    (dict(known_position_qty=Decimal("3")), "overcover"),
])
def test_a_cover_without_a_verified_matching_short_is_refused(overrides, code):
    svc, fake = service()
    with pytest.raises(BrokerRejectedError) as raised:
        cover(svc, **overrides)
    assert raised.value.code == code and fake.calls == []  # nothing is sent: no reversal


def test_a_cover_must_be_a_buy():
    svc, fake = service()
    with pytest.raises(BrokerRejectedError) as raised:
        cover(svc, side="sell")
    assert raised.value.code == "cover_side" and fake.calls == []


# ------------------------------------------------------- no accidental reversal


def test_a_sell_against_a_short_is_refused_not_added_to_it():
    svc, fake = service()
    with pytest.raises(BrokerRejectedError) as raised:
        svc.submit_order(
            asset=asset(fractionable=False), side="sell", order_type="market",
            sizing=OrderSizing(qty=Decimal("2")), cycle_id="cycle-1", purpose=OrderPurpose.EXIT,
            known_position_qty=Decimal("5"), known_position_side="short")
    assert raised.value.code == "sell_against_short" and fake.calls == []


@pytest.mark.parametrize("known_side", ["short", "long", None])
def test_a_buy_entry_requires_a_flat_symbol(known_side):
    svc, fake = service()
    with pytest.raises(BrokerRejectedError) as raised:
        svc.submit_order(
            asset=asset(fractionable=False), side="buy", order_type="market",
            sizing=OrderSizing(qty=Decimal("2")), cycle_id="cycle-1", purpose=OrderPurpose.ENTRY,
            known_position_qty=Decimal("5"), known_position_side=known_side)
    assert raised.value.code == "position_already_open" and fake.calls == []


def test_long_paths_are_unchanged_for_legacy_callers():
    svc, fake = service()
    fake.post_action = order_json(side="sell", qty="2", filled_qty="0")
    svc.submit_order(  # a long exit with only a quantity (no side): the legacy contract
        asset=asset(fractionable=False), side="sell", order_type="market",
        sizing=OrderSizing(qty=Decimal("2")), cycle_id="cycle-1", purpose=OrderPurpose.EXIT,
        known_position_qty=Decimal("5"))
    assert len(fake.calls) == 1
    fake.calls.clear()
    fake.post_action = order_json(side="buy", qty="2", filled_qty="0")
    svc.submit_order(  # a flat long entry sends no position knowledge at all
        asset=asset(fractionable=False), side="buy", order_type="market",
        sizing=OrderSizing(qty=Decimal("2")), cycle_id="cycle-1", purpose=OrderPurpose.ENTRY)
    assert len(fake.calls) == 1


# ---------------------------------------------------------- short-side protection


def test_short_side_protective_stops_are_buy_stops_behind_a_capability():
    svc, fake = service()
    kwargs = dict(asset=asset(fractionable=False), qty=Decimal("5"), stop_price=Decimal("104"),
                  known_position_qty=Decimal("5"), cycle_id="cycle-1")
    with pytest.raises(CapabilityUnavailable):
        svc.submit_stop_buy_to_cover(capability_check=UNPROVEN_BY_DEFAULT, **kwargs)
    with pytest.raises(CapabilityUnavailable):
        svc.submit_trailing_stop_buy_to_cover(
            asset=asset(fractionable=False), qty=Decimal("5"), trail_percent=Decimal("2"),
            known_position_qty=Decimal("5"), capability_check=UNPROVEN_BY_DEFAULT,
            cycle_id="cycle-1")
    assert fake.calls == []
    fake.post_action = order_json(side="buy", type="stop", qty="5", filled_qty="0")
    svc.submit_stop_buy_to_cover(capability_check=PROVEN, **kwargs)
    ((_, _, body),) = fake.calls
    assert body["side"] == "buy" and body["type"] == "stop" and body["stop_price"] == "104"
    with pytest.raises(BrokerRejectedError) as raised:  # cannot cover more than the short
        svc.submit_stop_buy_to_cover(
            capability_check=PROVEN, **{**kwargs, "known_position_qty": Decimal("3")})
    assert raised.value.code == "overcover"


def test_the_short_capability_names_resolve_to_unproven_descriptors():
    check = default_alpaca_capabilities().as_legacy_checker("PAPER")
    for name in ("short_entry_sell:whole:day:market", "short_entry_sell:whole:gtc:limit",
                 "standalone_stop_buy_to_cover:whole:gtc",
                 "standalone_trailing_stop_buy_to_cover:whole:day"):
        assert check(name) is False  # parsed, structurally valid, but not proven
    assert check("short_entry_sell:whole:day:stop") is False  # unknown order type fails closed
    # the long-side docs-proven shape still resolves True: the checker is not just "always no"
    assert check("standalone_trailing_stop_sell:whole:day") is True
