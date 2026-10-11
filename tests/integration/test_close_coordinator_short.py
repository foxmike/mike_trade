"""CloseCoordinator for SHORT positions: covers, short stops and orphan detection.

The coordinator serializes every position-closing mutation.  For a short the exit is a
BUY_TO_COVER: a working SELL is not a committed cover, a short stop may only FALL, re-protection
after a failed cover is requested for the short, and orphaned cover/stop orders are reported.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from test_stop_close_races import NOW, Clock, Events, FakeBroker

from mike_trade.execution.close_coordinator import (
    CloseCoordinator,
    SellIntent,
    detect_orphan_orders,
)


class ShortBroker(FakeBroker):
    """A fake broker holding a SHORT: every exit/stop order it creates is a BUY side order."""

    def __init__(self, quantity: str = "10") -> None:
        super().__init__(quantity)
        self.protect_kwargs: list[dict[str, Any]] = []
        self.replace_kwargs: list[dict[str, Any]] = []

    def _order(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        order = super()._order(*args, **kwargs)
        order["side"] = "BUY_TO_COVER"
        return order

    def add_working(self, order_id: str, side: str, quantity: str, client_order_id: str) -> None:
        order = super()._order(order_id, quantity=D(quantity), order_type="LIMIT",
                               client_order_id=client_order_id)
        order["side"] = side
        self.orders[order_id] = order

    async def submit_protective_stop(self, position_id, symbol, quantity, stop_price,
                                     client_order_id, **kwargs):
        self.protect_kwargs.append(kwargs)
        return await super().submit_protective_stop(
            position_id, symbol, quantity, stop_price, client_order_id)

    async def replace_protective_stop(self, order_id, position_id, symbol, quantity, stop_price,
                                      client_order_id, **kwargs):
        self.replace_kwargs.append(kwargs)
        return await super().replace_protective_stop(
            order_id, position_id, symbol, quantity, stop_price, client_order_id)


def coordinator(broker, *, stop_price="105"):
    events = Events()
    value = CloseCoordinator(broker, Clock(), events)
    value.register_protective_stop(position_id="position-1", symbol="AAPL", order_id="stop-1",
                                   stop_price=D(stop_price))
    return value, events


def cover_intent(kind="STRATEGY", *, quantity="10", direction="SHORT") -> SellIntent:
    return SellIntent(
        position_id="position-1", symbol="AAPL", quantity=D(quantity), kind=kind,
        client_order_id=f"cover-{kind.lower()}", direction=direction,
        first_confirmed_entry_fill_at=NOW - timedelta(days=1))


def test_the_exit_direction_is_validated_and_defaults_to_long():
    assert SellIntent("p", "AAPL", D("1"), "STRATEGY", "c").exit_side == "SELL"
    assert cover_intent().exit_side == "BUY_TO_COVER"
    with pytest.raises(ValueError, match="LONG or SHORT"):
        cover_intent(direction="FLAT")


async def test_a_cover_cancels_the_stop_first_and_covers_the_full_short():
    broker = ShortBroker()
    broker.add_stop(stop_price="105")
    broker.next_exit_status = "FILLED"
    value, _ = coordinator(broker)
    outcome = await value.close(cover_intent())
    assert broker.calls[0] == ("cancel", "stop-1")           # the stop is removed first
    assert broker.calls[1] == ("exit", D("10"))
    assert outcome.flat and outcome.submitted_quantity == D("10")
    assert broker.positions["position-1"] == D("0")
    assert broker.orders["exit-1"]["side"] == "BUY_TO_COVER"


async def test_a_working_sell_is_not_a_committed_cover_but_a_working_cover_is():
    broker = ShortBroker()
    broker.add_stop(stop_price="105")
    broker.add_working("sell-1", "SELL", "10", "unrelated-sell")   # not a cover
    value, _ = coordinator(broker)
    broker.next_exit_status = "FILLED"
    assert (await value.close(cover_intent())).flat                 # the SELL did not block it
    other = ShortBroker()
    other.add_stop(stop_price="105")
    other.add_working("cover-1", "BUY_TO_COVER", "4", "working-cover")
    value, _ = coordinator(other)
    outcome = await value.close(cover_intent(quantity="10"))
    assert outcome.status == "REJECTED"                             # only 6 of 10 are uncommitted
    assert "exceeds broker-confirmed position minus working sells" in outcome.detail


async def test_a_short_stop_replacement_may_only_fall_never_rise():
    broker = ShortBroker()
    broker.add_stop(stop_price="105")
    value, _ = coordinator(broker, stop_price="105")
    rose = await value.replace_protective_stop(
        position_id="position-1", symbol="AAPL", order_id="stop-1", stop_price=D("106"),
        client_order_id="r1", direction="SHORT")
    assert rose.status == "REJECTED"
    assert "may not raise the last confirmed short stop" in rose.detail
    assert broker.replace_kwargs == []                       # nothing reached the broker
    fell = await value.replace_protective_stop(
        position_id="position-1", symbol="AAPL", order_id="stop-1", stop_price=D("103"),
        client_order_id="r2", direction="SHORT")
    assert fell.status == "REPLACED" and fell.stop_price == D("103")
    assert broker.replace_kwargs == [{"direction": "SHORT"}]  # the adapter is told it is a short
    # the long rule is unchanged: a long stop may not LOWER
    longer = ShortBroker()
    longer.add_stop(stop_price="95")
    value, _ = coordinator(longer, stop_price="95")
    lowered = await value.replace_protective_stop(
        position_id="position-1", symbol="AAPL", order_id="stop-1", stop_price=D("94"),
        client_order_id="r3")
    assert lowered.status == "REJECTED" and "may not lower" in lowered.detail


async def test_a_failed_cover_reprotects_the_short_with_a_short_side_stop():
    broker = ShortBroker()
    broker.add_stop(stop_price="105")

    async def failing_exit(intent, quantity):
        raise RuntimeError("exit rejected by the broker")

    broker.submit_exit = failing_exit  # type: ignore[method-assign]
    value, _ = coordinator(broker, stop_price="105")
    outcome = await value.close(cover_intent())
    assert broker.protect_kwargs == [{"direction": "SHORT"}]  # re-protected as a SHORT
    assert outcome.protective_order_id and outcome.protective_order_id.startswith(
        "stop-reprotected")
    assert broker.positions["position-1"] == D("10")  # nothing was covered: still protected


def test_orphaned_covers_and_stops_are_reported_for_a_short():
    cover = {"broker_order_id": "o1", "client_order_id": "unknown", "symbol": "AAPL",
             "side": "BUY_TO_COVER", "order_type": "LIMIT", "status": "ACCEPTED",
             "quantity": D("5"), "filled_quantity": D("0")}
    stop = {"broker_order_id": "o2", "client_order_id": "also-unknown", "symbol": "MSFT",
            "side": "BUY_TO_COVER", "order_type": "STOP", "status": "ACCEPTED",
            "quantity": D("5"), "filled_quantity": D("0")}
    found = detect_orphan_orders([cover, stop], local_intents=[], local_positions={"AAPL": D("5")})
    reasons = {o.broker_order_id: o.reason for o in found}
    assert reasons == {"o1": "ORPHAN_SELL_NO_LOCAL_INTENT", "o2": "ORPHAN_STOP_FLAT_POSITION"}
