"""Unit tests for the historical execution simulator.

Small synthetic in-memory bars/quotes only: no network, credentials, or Alpaca imports.
Dates are regular NYSE sessions; ``at`` builds New York wall-clock instants in UTC.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from mike_trade.backtest import execution as execution_module
from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import (
    AMBIGUOUS_INTRABAR_ORDER,
    EXECUTION_SIMULATOR_VERSION,
    AmbiguityPolicy,
    BarFillPolicy,
    ExecutionAssumptions,
    HistoricalExecutionSimulator,
)
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.brokers.ports import BrokerPort
from mike_trade.domain.accounts import AccountEnvironment
from mike_trade.domain.errors import BrokerRejectedError, UnsupportedOrderCapabilityError
from mike_trade.domain.market import (
    Candle,
    FeedProvenance,
    MarketEvent,
    MarketEventKind,
    Timeframe,
)
from mike_trade.domain.protection import ProtectionStatus
from mike_trade.domain.trading import (
    BrokerOrderStatus,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    TimeInForce,
)
from mike_trade.scheduling.calendar import NEW_YORK

D = Decimal
MIN = timedelta(minutes=1)
DAY = date(2025, 3, 10)
EARLY_CLOSE_DAY = date(2025, 11, 28)  # day after Thanksgiving: closes 13:00 ET
PROV = DataProvenance(source="fixture", feed="IEX", is_consolidated=False, revision="r1")
FEED = FeedProvenance(provider="replay", feed="IEX", source="fixture", is_consolidated=False)
BUY, SELL = OrderSide.BUY, OrderSide.SELL
ENTRY, EXIT, STOP_PURPOSE = (
    OrderPurpose.ENTRY,
    OrderPurpose.STRATEGY_EXIT,
    OrderPurpose.PROTECTIVE_STOP,
)


def at(hour: int, minute: int, second: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=NEW_YORK).astimezone(
        UTC
    )


def bar(hm: tuple[int, int], o, h, low, c, volume="100000", day: date = DAY) -> ReplayEvent:
    start = at(hm[0], hm[1], day=day)
    return ReplayEvent.bar(
        Candle(
            symbol="AAA", timeframe=Timeframe.M1, start=start, end=start + MIN,
            open=D(str(o)), high=D(str(h)), low=D(str(low)), close=D(str(c)),
            volume=D(volume), provenance=FEED,
        )
    )


def flat(first: tuple[int, int], count: int, price="100", volume="100000", day: date = DAY):
    """``count`` consecutive flat bars from ``first``."""
    minute0 = first[0] * 60 + first[1]
    return [
        bar(((minute0 + i) // 60, (minute0 + i) % 60), price, price, price, price, volume, day)
        for i in range(count)
    ]


def quote(hms: tuple[int, int, int], bid, ask, bid_size="1000", ask_size="1000") -> ReplayEvent:
    when = at(*hms)
    return ReplayEvent.from_quote(
        MarketEvent(
            symbol="AAA", kind=MarketEventKind.QUOTE, event_time=when, received_at=when,
            provenance=FEED, bid=D(str(bid)), ask=D(str(ask)),
            bid_size=None if bid_size is None else D(bid_size),
            ask_size=None if ask_size is None else D(ask_size),
        )
    )


class Env:
    """Feed + clock + simulator harness driven by explicit ``go`` calls."""

    def __init__(self, events, *, start=(9, 0), day: date = DAY, feed_latency=timedelta(0),
                 **assumptions):
        self.day = day
        self.clock = ReplayClock(at(start[0], start[1], day=day))
        self.feed = HistoricalReplayFeed(
            events, PROV, availability_latency=feed_latency, clock=self.clock
        )
        self.sim = HistoricalExecutionSimulator(
            self.feed, self.clock, ExecutionAssumptions(**assumptions)
        )
        self.count = 0

    def go(self, h: int, m: int, s: int = 0):
        return self.sim.advance_to(at(h, m, s, day=self.day))

    def intent(self, side, purpose, otype, qty, *, tif=TimeInForce.GTC, **px) -> OrderIntent:
        self.count += 1
        return OrderIntent(
            intent_id=f"i{self.count}", client_order_id=px.pop("cid", f"c{self.count}"),
            account_scope_id="bt", cycle_id="cyc", symbol="AAA", side=side, purpose=purpose,
            order_type=otype, time_in_force=tif, created_at=self.clock.now(),
            quantity=D(str(qty)), **{k: D(str(v)) for k, v in px.items()},
        )

    async def submit(self, side, purpose, otype, qty, **kw):
        tif = kw.pop("tif", TimeInForce.GTC)
        return await self.sim.submit_order(self.intent(side, purpose, otype, qty, tif=tif, **kw))

    async def buy(self, qty):
        return await self.submit(BUY, ENTRY, OrderType.MARKET, qty)

    async def stop(self, qty, price):
        return await self.submit(SELL, STOP_PURPOSE, OrderType.STOP, qty, stop_price=price)

    async def enter(self, qty=100):
        """Hold ``qty`` bought at the 09:31 open; leaves the clock at 09:32."""
        self.go(9, 31)
        await self.buy(qty)
        return self.go(9, 32)


def sells(env: Env):
    return [e for e in env.sim.executions if e.side is SELL]


# ----------------------------------------------------------------------- assumptions


def test_assumptions_default_to_pessimistic_policies_and_serialise_to_strings():
    a = ExecutionAssumptions()
    assert a.ambiguity_policy is AmbiguityPolicy.STOP_FIRST
    assert a.bar_fill_policy is BarFillPolicy.NEXT_BAR_OPEN
    mapping = a.as_mapping()
    assert mapping["ambiguity_policy"] == "STOP_FIRST"
    assert mapping["fractional_order_types"] == "LIMIT,MARKET"
    assert all(isinstance(v, str) for v in mapping.values())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"slippage_bps": 1.5},
        {"fee_per_share": D("-0.01")},
        {"fee_per_order": 1},
        {"participation": D("0")},
        {"participation": D("1.5")},
        {"submission_latency": timedelta(seconds=-1)},
        {"stop_replace_latency": 3},
        {"bar_fill_policy": "NEXT_BAR_OPEN"},
        {"ambiguity_policy": "FAVORABLE_FIRST"},
        {"fractional_order_types": frozenset({OrderType.STOP})},
    ],
)
def test_assumptions_reject_invalid_values(kwargs):
    with pytest.raises((ValueError, TypeError)):
        ExecutionAssumptions(**kwargs)


def test_constructor_requires_feed_and_clock_types():
    env = Env(flat((9, 30), 2))
    with pytest.raises(TypeError):
        HistoricalExecutionSimulator(object(), env.clock)
    with pytest.raises(TypeError):
        HistoricalExecutionSimulator(env.feed, object())
    with pytest.raises(TypeError):
        HistoricalExecutionSimulator(env.feed, env.clock, object())


# ----------------------------------------------------------------------- broker shape


async def test_adapter_has_broker_port_shape_and_backtest_environment():
    env = Env(flat((9, 30), 3))
    assert isinstance(env.sim, BrokerPort)
    assert env.sim.environment is AccountEnvironment.BACKTEST
    caps = env.sim.capabilities()
    assert caps.supports_order("market", False)
    assert caps.supports_order("stop", False)
    assert not caps.supports_order("stop_limit", False)
    assert not caps.supports_order("market", True)
    with pytest.raises(NotImplementedError):
        await env.sim.get_account()


async def test_order_lifecycle_queries_cancel_and_idempotent_client_id():
    env = Env(flat((9, 30), 6))
    env.go(9, 31)
    first = await env.sim.submit_order(env.intent(BUY, ENTRY, OrderType.MARKET, 10, cid="dup"))
    again = await env.sim.submit_order(env.intent(BUY, ENTRY, OrderType.MARKET, 99, cid="dup"))
    assert again == first and first.status is BrokerOrderStatus.ACCEPTED
    assert await env.sim.get_order(first.broker_order_id) == first
    assert await env.sim.get_order_by_client_id("dup") == first
    assert await env.sim.get_order("missing") is None
    assert [o.broker_order_id for o in await env.sim.list_open_orders()] == [first.broker_order_id]
    await env.sim.cancel_order(first.broker_order_id)
    await env.sim.cancel_order(first.broker_order_id)  # idempotent
    assert (await env.sim.get_order(first.broker_order_id)).status is BrokerOrderStatus.CANCELED
    assert env.go(9, 33) == () and await env.sim.list_open_orders() == ()
    with pytest.raises(BrokerRejectedError):
        await env.sim.cancel_order("missing")


async def test_fills_positions_and_status_flow_through_the_port():
    env = Env(flat((9, 30), 6))
    await env.enter(10)
    (order,) = [await env.sim.get_order("bt-order-000001")]
    assert order.status is BrokerOrderStatus.FILLED and order.filled_avg_price == D("100")
    (position,) = await env.sim.list_positions()
    assert (position.symbol, position.quantity, position.avg_entry_price) == (
        "AAA", D("10"), D("100"),
    )
    fills = await env.sim.list_fills(at(9, 0))
    assert [f.fill_id for f in fills] == ["bt-fill:bt-order-000001:1"]
    assert await env.sim.list_fills(at(9, 40)) == ()


# ----------------------------------------------------------------------- capability


async def test_fractional_and_notional_orders_fail_closed_by_default():
    env = Env(flat((9, 30), 3))
    with pytest.raises(UnsupportedOrderCapabilityError):
        await env.buy("1.5")
    notional = OrderIntent(
        intent_id="n", client_order_id="n", account_scope_id="bt", cycle_id="cyc", symbol="AAA",
        side=BUY, purpose=ENTRY, order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY,
        created_at=env.clock.now(), notional=D("100"),
    )
    with pytest.raises(UnsupportedOrderCapabilityError):
        await env.sim.submit_order(notional)


async def test_fractional_requires_explicit_config_order_type_and_day_tif():
    env = Env(flat((9, 30), 5), allow_fractional=True)
    ok = await env.submit(BUY, ENTRY, OrderType.MARKET, "1.5", tif=TimeInForce.DAY)
    assert ok.quantity == D("1.5")
    with pytest.raises(UnsupportedOrderCapabilityError):  # GTC fractional
        await env.submit(BUY, ENTRY, OrderType.MARKET, "1.5", tif=TimeInForce.GTC)
    with pytest.raises(UnsupportedOrderCapabilityError):  # fractional stop never verified
        await env.submit(
            SELL, STOP_PURPOSE, OrderType.STOP, "0.5", tif=TimeInForce.DAY, stop_price=95
        )


async def test_fractional_whole_and_fractional_fill_quantisation():
    env = Env(flat((9, 30), 5), allow_fractional=True)
    env.go(9, 31)
    await env.submit(BUY, ENTRY, OrderType.MARKET, "1.5", tif=TimeInForce.DAY)
    (ex,) = env.go(9, 32)
    assert ex.quantity == D("1.5") and env.sim.held_quantity("AAA") == D("1.5")


async def test_stop_limit_and_buy_stops_are_unsupported():
    env = Env(flat((9, 30), 3))
    with pytest.raises(UnsupportedOrderCapabilityError):
        await env.submit(SELL, EXIT, OrderType.STOP_LIMIT, 1, stop_price=95, limit_price=94)
    with pytest.raises(UnsupportedOrderCapabilityError):
        await env.submit(BUY, ENTRY, OrderType.STOP, 1, stop_price=105)


async def test_sell_without_position_or_beyond_held_is_rejected_never_short():
    env = Env(flat((9, 30), 6))
    env.go(9, 31)
    with pytest.raises(BrokerRejectedError) as short:
        await env.submit(SELL, EXIT, OrderType.MARKET, 1)
    assert short.value.code == "would_short"
    await env.buy(10)
    env.go(9, 32)
    with pytest.raises(BrokerRejectedError) as over:
        await env.submit(SELL, EXIT, OrderType.MARKET, 11)
    assert over.value.code == "exceeds_held_quantity"


async def test_protection_starts_only_after_confirmed_positive_fill():
    env = Env(flat((9, 30), 6))
    env.go(9, 31)
    await env.buy(10)  # accepted but not yet filled
    with pytest.raises(BrokerRejectedError):
        await env.stop(10, 95)
    env.go(9, 32)
    stop = await env.stop(10, 95)
    assert stop.status is BrokerOrderStatus.ACCEPTED
    with pytest.raises(UnsupportedOrderCapabilityError):  # a protective stop must be a stop
        await env.submit(SELL, STOP_PURPOSE, OrderType.MARKET, 5)


# ----------------------------------------------------------------------- bar-only fills


async def test_bar_only_market_fills_at_next_bar_open_never_signal_bar_close():
    events = [
        bar((9, 30), 100, 101, 99, "100.5"),
        bar((9, 31), 101, 102, 100, "101.5"),
        bar((9, 32), 103, 104, 102, 103),
    ]
    env = Env(events)
    env.go(9, 31)  # the 09:30 signal bar is now complete
    await env.buy(10)
    assert env.sim.executions == ()  # the signal bar's close is not a fill price
    (ex,) = env.go(9, 32)
    assert ex.price == D("101")  # open of 09:31, not 100.5 and not 101.5
    assert ex.fill.filled_at == at(9, 31)
    assert ex.fidelity_limitation == "BAR_ONLY" and ex.evidence == "BAR"


async def test_execution_record_carries_labels_assumptions_and_provenance():
    env = Env(flat((9, 30), 4), slippage_bps=D("10"), slippage_ticks=D("1"))
    (ex,) = await env.enter(10)
    assert ex.estimated is True and ex.broker_confirmed is False
    assert ex.simulator_version == EXECUTION_SIMULATOR_VERSION
    assert ex.assumptions["slippage_bps"] == "10" and ex.assumptions["bar_fill_policy"] == (
        "NEXT_BAR_OPEN"
    )
    assert ex.provenance["feed"] == "IEX" and ex.provenance["is_consolidated"] == "False"
    assert ex.provenance["quote_fidelity"] == "UNAVAILABLE"
    assert ex.provenance["dataset_hash"] == env.feed.provenance.dataset_hash
    assert ex.provenance["bar_start"] == at(9, 31).isoformat()
    assert ex.seed == env.feed.seed
    # 100 + 100*10bps + 1 tick = 100.11, adverse to the buyer
    assert (ex.reference_price, ex.price, ex.slippage) == (D("100"), D("100.11"), D("0.11"))
    assert ex.slippage_cost == D("1.10")
    for value in (ex.price, ex.fee, ex.slippage, ex.quantity):
        assert isinstance(value, Decimal)


async def test_sell_slippage_is_adverse_and_rounded_against_trader():
    env = Env(flat((9, 30), 8), slippage_ticks=D("1"), price_quantum=D("0.01"))
    await env.enter(10)
    await env.submit(SELL, EXIT, OrderType.MARKET, 10)
    (ex,) = env.go(9, 33)
    assert ex.price == D("99.99") and ex.slippage == D("0.01")


async def test_submission_latency_defers_eligibility_in_simulated_time():
    env = Env(flat((9, 30), 8), submission_latency=timedelta(minutes=2))
    env.go(9, 31)
    await env.buy(10)  # eligible 09:33
    assert env.go(9, 32) == () and env.go(9, 33) == ()  # bars 09:31, 09:32 start before 09:33
    (ex,) = env.go(9, 34)
    assert ex.fill.filled_at == at(9, 33)


async def test_no_bar_fill_policy_refuses_bar_only_fills():
    env = Env(flat((9, 30), 5), bar_fill_policy=BarFillPolicy.NO_BAR_FILL)
    env.go(9, 31)
    order = await env.buy(10)
    assert env.go(9, 35) == ()
    assert any(r == "bar_only_fills_disabled" for _, _, r in env.sim.diagnostics)
    assert (await env.sim.get_order(order.broker_order_id)).status is BrokerOrderStatus.ACCEPTED


async def test_fees_per_order_share_and_bps_charge_the_per_order_fee_once():
    env = Env(
        flat((9, 30), 6, volume="100"), participation=D("0.5"), fee_per_order=D("1"),
        fee_per_share=D("0.005"), fee_bps=D("1"),
    )
    env.go(9, 31)
    await env.buy(80)  # 50 shares per bar available -> 50 then 30
    first, second = env.go(9, 32)[0], env.go(9, 33)[0]
    assert (first.quantity, second.quantity) == (D("50"), D("30"))
    assert first.fee == D("1.75")  # 1 per order + 50 x 0.005 + 1bp of 5000
    assert second.fee == D("0.45")  # 30 x 0.005 + 1bp of 3000; no second per-order fee


# ----------------------------------------------------------------------- partial fills


async def test_participation_cap_leaves_remainder_pending_across_bars():
    env = Env(flat((9, 30), 8, volume="1000"), participation=D("0.1"))
    env.go(9, 31)
    order = await env.buy(250)
    quantities = [ex.quantity for step in (32, 33, 34) for ex in env.go(9, step)]
    assert quantities == [D("100"), D("100"), D("50")]
    assert [e.order_remaining for e in env.sim.executions] == [D("150"), D("50"), D("0")]
    assert (await env.sim.get_order(order.broker_order_id)).status is BrokerOrderStatus.FILLED
    assert env.sim.held_quantity("AAA") == D("250")


async def test_partial_status_and_whole_share_flooring():
    env = Env(flat((9, 30), 6, volume="333"), participation=D("0.1"))
    env.go(9, 31)
    order = await env.buy(100)
    (ex,) = env.go(9, 32)
    assert ex.quantity == D("33")  # 33.3 floors to whole shares
    assert (await env.sim.get_order(order.broker_order_id)).status is (
        BrokerOrderStatus.PARTIALLY_FILLED
    )


async def test_liquidity_is_shared_between_orders_in_one_bar():
    env = Env(flat((9, 30), 5, volume="100"), participation=D("0.5"))
    env.go(9, 31)
    await env.buy(40)
    await env.buy(40)
    executions = env.go(9, 32)
    assert [e.quantity for e in executions] == [D("40"), D("10")]  # 50-share pool in total


async def test_zero_volume_bar_produces_no_fill():
    events = [*flat((9, 30), 2), bar((9, 32), 100, 100, 100, 100, volume="0"), *flat((9, 33), 3)]
    env = Env(events)
    env.go(9, 32)
    await env.buy(10)
    assert env.go(9, 33) == ()
    assert any(r == "no_bar_volume" for _, _, r in env.sim.diagnostics)
    assert len(env.go(9, 34)) == 1


async def test_protective_coverage_never_exceeds_filled_remaining_quantity():
    env = Env(flat((9, 30), 8, volume="1000"), participation=D("0.1"))
    env.go(9, 31)
    await env.buy(250)
    env.go(9, 32)  # 100 filled so far
    with pytest.raises(BrokerRejectedError):
        await env.stop(250, 95)  # cannot cover quantity that is not yet filled
    stop = await env.stop(100, 95)
    assert env.sim.protective_coverage("AAA") == env.sim.held_quantity("AAA") == D("100")
    env.go(9, 33)  # entry fills 100 more
    await env.sim.replace_order(stop.broker_order_id, quantity=D("200"))
    assert env.sim.protective_coverage("AAA") == env.sim.held_quantity("AAA") == D("200")
    with pytest.raises(BrokerRejectedError):
        await env.sim.replace_order(stop.broker_order_id, quantity=D("300"))


async def test_protective_stop_is_clamped_when_the_position_shrinks():
    env = Env(flat((9, 30), 8))
    await env.enter(100)
    stop = await env.stop(100, 95)
    await env.submit(SELL, EXIT, OrderType.MARKET, 40)
    env.go(9, 33)
    assert env.sim.held_quantity("AAA") == D("60")
    assert env.sim.protective_coverage("AAA") == D("60")
    assert (await env.sim.get_order(stop.broker_order_id)).quantity == D("60")
    assert env.sim.protection_state(stop.broker_order_id).covered_quantity == D("60")


# ----------------------------------------------------------------------- stops


async def test_stop_triggers_when_low_touches_and_fills_at_the_stop_price():
    events = [
        *flat((9, 30), 3),
        bar((9, 33), 99.5, 99.8, 97.5, 98),
        *flat((9, 34), 3, price="98"),
    ]
    env = Env(events, slippage_ticks=D("1"))
    await env.enter(100)
    stop = await env.stop(100, 98)
    (ex,) = [e for e in env.go(9, 34) if e.side is SELL]
    assert ex.reference_price == D("98") and ex.price == D("97.99")
    assert ex.slippage_cost == D("1.00") and ex.fill.filled_at == at(9, 34)
    assert ex.flags == () and ex.position_after == D("0")
    assert (await env.sim.get_order(stop.broker_order_id)).status is BrokerOrderStatus.FILLED
    assert env.sim.protection_state(stop.broker_order_id).status is ProtectionStatus.TRIGGERED


async def test_stop_does_not_trigger_while_low_stays_above_it():
    events = [*flat((9, 30), 3), bar((9, 33), 100, 101, 98.01, 100), *flat((9, 34), 3)]
    env = Env(events)
    await env.enter(100)
    await env.stop(100, 98)
    assert [e for e in env.go(9, 35) if e.side is SELL] == []
    assert env.sim.held_quantity("AAA") == D("100")


async def test_gap_through_open_fills_at_the_open_not_the_stop():
    events = [*flat((9, 30), 3), bar((9, 33), 96, 97, 95, 96.5), *flat((9, 34), 3, price="96")]
    env = Env(events)
    await env.enter(100)
    await env.stop(100, 98)
    (ex,) = [e for e in env.go(9, 34) if e.side is SELL]
    assert ex.price == D("96") and ex.reference_price == D("96")
    assert ex.fill.filled_at == at(9, 33) and ex.flags == ()


async def test_stop_is_not_active_on_the_bar_before_it_was_submitted():
    events = [*flat((9, 30), 2), bar((9, 32), 100, 100, 90, 95), *flat((9, 33), 3, price="95")]
    env = Env(events)
    await env.enter(100)  # clock 09:32: the 09:32 crash bar is processed on the next advance
    await env.stop(100, 98)
    assert [e for e in env.go(9, 33) if e.side is SELL][0].price == D("98")
    # a stop submitted after the crash bar cannot retroactively trigger on it
    env2 = Env(events)
    await env2.enter(100)
    env2.go(9, 33)
    await env2.stop(100, 92)
    assert [e for e in env2.go(9, 35) if e.side is SELL] == []


async def test_replacement_is_inactive_until_latency_elapses_then_applies_at_new_price():
    events = [
        *flat((9, 30), 3),
        *[bar((9, m), 100, "100.5", "99.5", 100) for m in (33, 34, 35, 36)],
        *flat((9, 37), 3),
    ]
    env = Env(events, stop_replace_latency=timedelta(minutes=2))
    await env.enter(100)
    stop = await env.stop(100, 98)
    env.go(9, 33)
    await env.sim.replace_order(stop.broker_order_id, stop_price=D("99.8"))  # effective 09:35
    assert (await env.sim.get_order(stop.broker_order_id)).stop_price == D("98")
    assert env.sim.pending_replacements(stop.broker_order_id) == 1
    # bars 09:33 and 09:34 start before the replacement is live: 99.8 would have hit
    assert [e for e in env.go(9, 35) if e.side is SELL] == []
    (ex,) = [e for e in env.go(9, 36) if e.side is SELL]  # the 09:35 bar
    assert ex.reference_price == D("99.8") and ex.fill.filled_at == at(9, 36)
    assert env.sim.pending_replacements(stop.broker_order_id) == 0


async def test_replacement_not_active_before_latency_but_old_stop_still_fills():
    events = [*flat((9, 30), 3), bar((9, 33), 99, 99.2, 97.5, 98), *flat((9, 34), 3, price="98")]
    env = Env(events, stop_replace_latency=timedelta(minutes=5))
    await env.enter(100)
    stop = await env.stop(100, 98)
    await env.sim.replace_order(stop.broker_order_id, stop_price=D("101"))
    (ex,) = [e for e in env.go(9, 34) if e.side is SELL]
    assert ex.reference_price == D("98")  # the old stop executed, not the pending 101


async def test_long_stop_never_moves_down():
    env = Env(flat((9, 30), 8), stop_replace_latency=timedelta(minutes=3))
    await env.enter(100)
    stop = await env.stop(100, 98)
    with pytest.raises(ValueError, match="never move downward"):
        await env.sim.replace_order(stop.broker_order_id, stop_price=D("97"))
    await env.sim.replace_order(stop.broker_order_id, stop_price=D("99"))
    with pytest.raises(ValueError, match="never move downward"):  # below the pending one
        await env.sim.replace_order(stop.broker_order_id, stop_price=D("98.5"))
    with pytest.raises(UnsupportedOrderCapabilityError):
        await env.sim.replace_order(stop.broker_order_id, trail=D("1"))
    with pytest.raises(ValueError):
        await env.sim.replace_order(stop.broker_order_id)


# ----------------------------------------------------------------------- trailing


async def test_native_trailing_stop_ratchets_on_completed_bars_and_never_moves_down():
    events = [
        bar((9, 30), 100, 100, 100, 100),
        bar((9, 31), 100, 101, 100, "100.5"),
        bar((9, 32), "100.5", 103, 100, 102),
        bar((9, 33), 102, "102.5", "101.5", 102),
        bar((9, 34), 102, 104, "100.5", 101),
        *flat((9, 35), 3, price="101"),
    ]
    env = Env(events)
    await env.enter(100)
    trail = await env.submit(SELL, STOP_PURPOSE, OrderType.TRAILING_STOP, 100, trail_percent=2)
    state = env.sim.protection_state(trail.broker_order_id)
    assert state.stop_price == D("98.49")  # 2% below the last completed close, 100.5
    env.go(9, 33)  # bar 09:32 (high 103) completes: ratchet applies to later bars
    assert env.sim.protection_state(trail.broker_order_id).stop_price == D("100.94")
    env.go(9, 34)  # lower high: no change (never down)
    assert env.sim.protection_state(trail.broker_order_id).stop_price == D("100.94")
    (ex,) = [e for e in env.go(9, 35) if e.side is SELL]
    # bar 09:34's high (104) would ratchet to 101.92, but only earlier bars count for it
    assert ex.reference_price == D("100.94") and ex.price == D("100.94")
    assert AMBIGUOUS_INTRABAR_ORDER in ex.flags


async def test_trailing_reference_comes_from_confirmed_prices_not_future_bars():
    env = Env([*flat((9, 30), 3), bar((9, 33), 100, 120, 100, 119), *flat((9, 34), 3, price="119")])
    await env.enter(100)
    trail = await env.submit(SELL, STOP_PURPOSE, OrderType.TRAILING_STOP, 100, trail_price=3)
    assert env.sim.protection_state(trail.broker_order_id).stop_price == D("97")
    env.go(9, 34)
    assert env.sim.protection_state(trail.broker_order_id).stop_price == D("117")


# ----------------------------------------------------------------------- ambiguity


async def _held_with_stop_and_target(env: Env, stop_qty: int, target_qty: int):
    await env.enter(100)
    stop = await env.stop(stop_qty, 98)
    target = await env.submit(SELL, EXIT, OrderType.LIMIT, target_qty, limit_price=103)
    return stop, target


async def test_bar_touching_stop_and_target_is_ambiguous_and_stop_goes_first():
    events = [*flat((9, 30), 3), bar((9, 33), 100, 104, 97, 100), *flat((9, 34), 3)]
    env = Env(events)
    stop, target = await _held_with_stop_and_target(env, 100, 100)
    executions = [e for e in env.go(9, 34) if e.side is SELL]
    assert len(executions) == 1
    (ex,) = executions
    assert ex.order_type is OrderType.STOP and ex.price == D("98")
    assert AMBIGUOUS_INTRABAR_ORDER in ex.flags
    assert env.sim.held_quantity("AAA") == D("0")
    assert (await env.sim.get_order(target.broker_order_id)).status is BrokerOrderStatus.CANCELED


async def test_stop_first_lets_target_take_only_the_remainder():
    events = [*flat((9, 30), 3), bar((9, 33), 100, 104, 97, 100), *flat((9, 34), 3)]
    env = Env(events)
    await _held_with_stop_and_target(env, 50, 50)
    executions = [e for e in env.go(9, 34) if e.side is SELL]
    assert [(e.order_type, e.price, e.quantity) for e in executions] == [
        (OrderType.STOP, D("98"), D("50")),
        (OrderType.LIMIT, D("103"), D("50")),
    ]
    assert all(AMBIGUOUS_INTRABAR_ORDER in e.flags for e in executions)


async def test_stop_only_policy_withholds_favorable_orders_on_ambiguous_bars():
    events = [*flat((9, 30), 3), bar((9, 33), 100, 104, 97, 100), *flat((9, 34), 3)]
    env = Env(events, ambiguity_policy=AmbiguityPolicy.STOP_ONLY)
    _, target = await _held_with_stop_and_target(env, 50, 50)
    executions = [e for e in env.go(9, 34) if e.side is SELL]
    assert [e.order_type for e in executions] == [OrderType.STOP]
    assert env.sim.held_quantity("AAA") == D("50")
    assert (await env.sim.get_order(target.broker_order_id)).status is BrokerOrderStatus.ACCEPTED


async def test_unambiguous_bars_carry_no_ambiguity_flag():
    only_target = [*flat((9, 30), 3), bar((9, 33), 100, 104, "99.5", 103), *flat((9, 34), 3)]
    env = Env(only_target)
    await _held_with_stop_and_target(env, 100, 100)
    (ex,) = [e for e in env.go(9, 34) if e.side is SELL]
    assert ex.order_type is OrderType.LIMIT and ex.price == D("103") and ex.flags == ()
    only_stop = [*flat((9, 30), 3), bar((9, 33), 100, 101, 97, 99), *flat((9, 34), 3)]
    env2 = Env(only_stop)
    await _held_with_stop_and_target(env2, 100, 100)
    (ex2,) = [e for e in env2.go(9, 34) if e.side is SELL]
    assert ex2.order_type is OrderType.STOP and ex2.flags == ()


async def test_gap_through_stop_precedes_the_bar_so_it_is_not_ambiguous():
    events = [*flat((9, 30), 3), bar((9, 33), 97, 104, 96, 100), *flat((9, 34), 3)]
    env = Env(events)
    await _held_with_stop_and_target(env, 100, 100)
    (ex,) = [e for e in env.go(9, 34) if e.side is SELL]
    assert ex.price == D("97") and ex.flags == ()


async def test_new_entry_bar_protection_marks_stop_first_trailing_ambiguity():
    events = [
        *flat((9, 30), 2),
        bar((9, 32), 100, 110, 97, 105),
        *flat((9, 33), 2, price="105"),
    ]
    env = Env(events)
    env.go(9, 32)
    await env.buy(100)
    (entry,) = env.go(9, 33)
    trailing = await env.sim.submit_order(
        env.intent(
            SELL,
            STOP_PURPOSE,
            OrderType.TRAILING_STOP,
            100,
            trail_percent=2,
        ),
        protection_reference=entry.price,
    )

    candle = events[2].candle
    assert candle is not None
    (stopped,) = env.sim.process_entry_bar_protection(
        trailing.broker_order_id, candle, allow_trailing_ratchet=True
    )
    assert stopped.price == D("98")
    assert stopped.flags == (AMBIGUOUS_INTRABAR_ORDER,)
    assert env.sim.held_quantity("AAA") == D("0")


async def test_intrabar_entry_does_not_consume_an_earlier_favorable_extreme():
    events = [
        *flat((9, 30), 2),
        bar((9, 32), 110, 120, 99, 110),
        *flat((9, 33), 2, price="110"),
    ]
    env = Env(events)
    env.go(9, 32)
    await env.submit(BUY, ENTRY, OrderType.LIMIT, 100, limit_price=100)
    (entry,) = env.go(9, 33)
    trailing = await env.sim.submit_order(
        env.intent(
            SELL,
            STOP_PURPOSE,
            OrderType.TRAILING_STOP,
            100,
            trail_percent=2,
        ),
        protection_reference=entry.price,
    )

    candle = events[2].candle
    assert candle is not None
    assert env.sim.process_entry_bar_protection(trailing.broker_order_id, candle) == ()
    assert env.sim.protection_state(trailing.broker_order_id).stop_price == D("98")


# ----------------------------------------------------------------------- overlap


async def test_strategy_sell_and_stop_cannot_oversell():
    events = [*flat((9, 30), 3), bar((9, 33), 99, "99.5", 97, 98), *flat((9, 34), 3, price="98")]
    env = Env(events)
    await env.enter(100)
    stop = await env.stop(100, 98)
    await env.submit(SELL, EXIT, OrderType.MARKET, 100)
    executions = [e for e in env.go(9, 35) if e.side is SELL]
    assert sum(e.quantity for e in executions) == D("100")
    assert env.sim.held_quantity("AAA") == D("0")
    assert (await env.sim.get_order(stop.broker_order_id)).status is BrokerOrderStatus.CANCELED


async def test_overlapping_sells_under_a_liquidity_cap_never_go_short():
    thin = {"volume": "1000"}
    events = [
        *flat((9, 30), 3),
        bar((9, 33), 99, "99.5", 97, 98, **thin),
        bar((9, 34), 98, 98, 97, 98, **thin),
        *flat((9, 35), 3, price="98", **thin),
    ]
    env = Env(events, participation=D("0.06"))  # 6000 shares on the entry bars, 60 on thin ones
    await env.enter(100)
    env.go(9, 33)
    stop = await env.stop(100, "97.5")
    await env.submit(SELL, EXIT, OrderType.MARKET, 100)
    (first,) = env.go(9, 34)
    assert first.quantity == D("60") and env.sim.held_quantity("AAA") == D("40")
    assert env.sim.protective_coverage("AAA") == D("40")  # stop clamped to what is still held
    (second,) = env.go(9, 35)
    assert second.quantity == D("40") and second.position_after == D("0")
    assert sum(e.quantity for e in sells(env)) == D("100")
    assert all(e.position_after >= D("0") for e in env.sim.executions)
    assert all(e.order_type is OrderType.MARKET for e in sells(env))
    assert (await env.sim.get_order(stop.broker_order_id)).status is BrokerOrderStatus.CANCELED


# ----------------------------------------------------------------------- session rules


async def test_no_fills_outside_regular_hours_until_the_session_opens():
    env = Env([bar((9, 28), 100, 100, 100, 100), bar((9, 29), 100, 100, 100, 100),
               *flat((9, 30), 4, price="101")])
    env.go(9, 29)
    await env.buy(10)
    assert env.go(9, 30) == ()  # 09:29 pre-open bar
    assert any(r == "outside_regular_session" for _, _, r in env.sim.diagnostics)
    (ex,) = env.go(9, 31)
    assert ex.fill.filled_at == at(9, 30) and ex.price == D("101")


async def test_no_fills_after_the_close():
    env = Env([*flat((15, 58), 2), *flat((16, 0), 3)])
    env.go(15, 59)
    await env.buy(10)
    assert len(env.go(16, 0)) == 1  # the 15:59 bar is still regular
    await env.submit(SELL, EXIT, OrderType.MARKET, 10)
    assert env.go(16, 3) == ()  # 16:00+ bars are after the close
    assert env.sim.held_quantity("AAA") == D("10")


async def test_early_close_session_is_respected():
    day = EARLY_CLOSE_DAY
    env = Env([*flat((12, 57), 2, day=day), *flat((13, 0), 3, day=day)], day=day, start=(12, 0))
    env.go(12, 58)
    await env.buy(10)
    assert len(env.go(12, 59)) == 1  # 12:58 bar fills; session closes at 13:00
    await env.submit(SELL, EXIT, OrderType.MARKET, 10)
    assert env.go(13, 3) == ()  # 13:00 bars are outside the shortened session


async def test_halt_blocks_fills_until_resume():
    events = [*flat((9, 30), 12), ReplayEvent.halt("AAA", at(9, 33), at(9, 36))]
    env = Env(events)
    env.go(9, 33)
    await env.buy(10)
    assert env.go(9, 34) == () and env.go(9, 36) == ()  # bars 09:33..09:35 are halted
    assert any(r == "trading_halt" for _, _, r in env.sim.diagnostics)
    (ex,) = env.go(9, 37)
    assert ex.fill.filled_at == at(9, 36)


async def test_missing_bar_gives_no_fill_and_is_flagged_when_data_resumes():
    events = [*flat((9, 30), 2), *flat((9, 33), 3)]  # no 09:32 bar
    env = Env(events)
    env.go(9, 32)
    await env.buy(10)
    assert env.go(9, 33) == ()
    (ex,) = env.go(9, 34)
    assert ex.fill.filled_at == at(9, 33)
    assert "DATA_GAP_BEFORE_FILL" in ex.flags


async def test_day_orders_expire_at_the_close_and_never_fill_later():
    env = Env(flat((15, 30), 30), start=(15, 0))
    env.go(15, 30)
    order = await env.submit(
        BUY, ENTRY, OrderType.LIMIT, 10, tif=TimeInForce.DAY, limit_price=90
    )
    env.go(16, 0)
    assert (await env.sim.get_order(order.broker_order_id)).status is BrokerOrderStatus.EXPIRED


async def test_limit_orders_need_a_trade_through_and_marketable_ones_fill_at_the_open():
    events = [
        *flat((9, 30), 2),
        bar((9, 32), 100, 101, 99, 100),     # touches 99 exactly: no fill
        bar((9, 33), 100, 101, "98.9", 100),  # trades through 99
        bar((9, 34), 97, 98, 96, 97),
        *flat((9, 35), 2, price="97"),
    ]
    env = Env(events)
    env.go(9, 32)
    await env.submit(BUY, ENTRY, OrderType.LIMIT, 10, limit_price=99)
    assert env.go(9, 33) == ()
    (ex,) = env.go(9, 34)
    assert ex.price == D("99") and ex.fill.filled_at == at(9, 34)
    await env.submit(BUY, ENTRY, OrderType.LIMIT, 10, limit_price=99)
    (open_fill,) = env.go(9, 35)  # 09:34 opens at 97, already below the limit
    assert open_fill.price == D("97")


async def test_splits_with_exposure_fail_closed():
    events = [*flat((9, 30), 6), ReplayEvent.split("AAA", at(9, 34), D("2"))]
    env = Env(events)
    await env.enter(10)
    with pytest.raises(NotImplementedError):
        env.go(9, 35)
    flat_env = Env([*flat((9, 30), 6), ReplayEvent.split("AAA", at(9, 34), D("2"))])
    flat_env.go(9, 35)  # no exposure: ignored


# ----------------------------------------------------------------------- quotes


def quote_events():
    return [
        quote((9, 30, 30), "99.90", "100.00"),
        bar((9, 30), 100, 100, 100, 100),
        quote((9, 31, 10), "100.10", "100.20"),
        quote((9, 31, 40), "100.30", "100.40"),
        bar((9, 31), 150, 150, 150, 150),
        quote((9, 33, 10), "99.40", "99.50"),
        quote((9, 34, 10), "99.60", "99.70"),
        bar((9, 32), 160, 160, 160, 160),
    ]


async def test_quote_evidence_buys_at_ask_sells_at_bid_with_slippage():
    env = Env(quote_events(), slippage_ticks=D("1"))
    env.go(9, 31, 5)
    await env.buy(50)
    (buy,) = env.go(9, 31, 20)
    assert buy.reference_price == D("100.20") and buy.price == D("100.21")
    assert buy.evidence == "QUOTE" and buy.fidelity_limitation is None
    assert buy.provenance["quote_fidelity"] == "SAMPLED" and buy.provenance["ask"] == "100.20"
    assert buy.estimated and not buy.broker_confirmed
    assert buy.simulator_version == EXECUTION_SIMULATOR_VERSION
    await env.submit(SELL, EXIT, OrderType.MARKET, 50)
    (sell,) = env.go(9, 31, 50)
    assert sell.reference_price == D("100.30") and sell.price == D("100.29")


async def test_quote_mode_never_fills_from_bar_prices():
    env = Env(quote_events())
    env.go(9, 31, 5)
    await env.buy(10)
    executions = env.go(9, 35)
    assert [e.evidence for e in executions] == ["QUOTE"]
    assert D("150") not in {e.price for e in executions}


async def test_quote_stop_fills_at_the_bid_when_it_gaps_below_the_stop():
    env = Env(quote_events(), slippage_ticks=D("1"))
    env.go(9, 31, 5)
    await env.buy(50)
    env.go(9, 31, 20)
    await env.stop(50, "99.50")
    assert env.go(9, 31, 30) == ()
    (ex,) = env.go(9, 33, 20)
    assert ex.order_type is OrderType.STOP and ex.reference_price == D("99.40")
    assert ex.price == D("99.39") and ex.price < D("99.50")  # below the stop: gap fills at bid
    assert ex.evidence == "QUOTE" and env.sim.held_quantity("AAA") == D("0")


async def test_quote_participation_cap_uses_displayed_size_and_keeps_the_remainder():
    events = [
        quote((9, 30, 30), "99.90", "100.00", ask_size="100"),
        quote((9, 31, 10), "100.00", "100.10", ask_size="100"),
        quote((9, 31, 20), "100.00", "100.10", ask_size="100"),
    ]
    env = Env(events, participation=D("0.5"))
    env.go(9, 31, 5)
    await env.buy(80)
    executions = env.go(9, 31, 30)
    assert [e.quantity for e in executions] == [D("50"), D("30")]


async def test_stale_or_unsized_quotes_do_not_fill():
    env = Env(quote_events(), feed_latency=timedelta(seconds=10))
    env.go(9, 31, 5)
    await env.buy(10)
    assert env.go(9, 31, 30) == ()  # received 10s after the provider timestamp > max age
    assert any(r == "quote_stale" for _, _, r in env.sim.diagnostics)
    unsized = Env([quote((9, 30, 30), "99.9", "100.0", ask_size=None),
                   quote((9, 31, 10), "100.0", "100.1", ask_size=None)])
    unsized.go(9, 31, 5)
    await unsized.buy(10)
    assert unsized.go(9, 31, 30) == ()
    assert any(r == "quote_size_unavailable" for _, _, r in unsized.sim.diagnostics)


# ----------------------------------------------------------------------- determinism


async def _scenario():
    events = [
        *flat((9, 30), 3, volume="1000"),
        bar((9, 33), 100, 104, 97, 100, volume="1000"),
        *flat((9, 34), 4, volume="1000"),
    ]
    env = Env(events, participation=D("0.1"), slippage_bps=D("5"), fee_per_share=D("0.01"),
              submission_latency=timedelta(seconds=30))
    await env.enter(150)
    env.go(9, 33)
    await env.stop(100, 98)
    await env.submit(SELL, EXIT, OrderType.LIMIT, 100, limit_price=103)
    env.go(9, 38)
    return [
        (e.fill, e.flags, e.reference_price, e.slippage_cost, e.order_remaining,
         dict(e.assumptions), dict(e.provenance), e.sequence)
        for e in env.sim.executions
    ]


async def test_same_inputs_and_config_replay_identically():
    first, second = await _scenario(), await _scenario()
    assert first and first == second


# ----------------------------------------------------------------------- source rules


def test_module_uses_no_broker_client_floats_wall_clock_or_randomness():
    source = Path(execution_module.__file__).read_text(encoding="utf-8")
    needles = ("float(", "import alpaca", "from alpaca", "brokers.alpaca", "uuid", "import random",
               "datetime.now", "time.sleep", "utcnow")
    for needle in needles:
        assert needle not in source, needle
