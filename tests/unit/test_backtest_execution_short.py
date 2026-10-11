"""HistoricalExecutionSimulator for SHORT positions (bar and quote evidence).

Mirror of the long behavior: a short is opened by SELL_SHORT and closed by BUY_TO_COVER, its stop
sits ABOVE the market and fires when price RISES to it, a trailing stop only ever falls, slippage
is adverse on both legs (sell lower, cover higher), and nothing can reverse a position.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

import pytest
from test_backtest_execution import (
    ENTRY,
    EXIT,
    STOP_PURPOSE,
    Env,
    bar,
    flat,
    quote_events,
)

from mike_trade.domain.errors import BrokerRejectedError
from mike_trade.domain.trading import OrderSide, OrderType, PositionDirection

SHORT, COVER, BUY, SELL = (
    OrderSide.SELL_SHORT, OrderSide.BUY_TO_COVER, OrderSide.BUY, OrderSide.SELL)


async def sell_short(env, qty=100):
    return await env.submit(SHORT, ENTRY, OrderType.MARKET, qty)


async def enter_short(env, qty=100):
    """Hold a ``qty`` short sold at the 09:31 open; leaves the clock at 09:32."""
    env.go(9, 31)
    await sell_short(env, qty)
    return env.go(9, 32)


async def cover_stop(env, qty, price):
    return await env.submit(COVER, STOP_PURPOSE, OrderType.STOP, qty, stop_price=price)


def covers(env):
    return [e for e in env.sim.executions if e.side is COVER]


# ------------------------------------------------------------------------ admission


async def test_a_short_is_held_with_positive_quantity_and_an_explicit_direction():
    env = Env([*flat((9, 30), 8)])
    (entry,) = await enter_short(env, 40)
    assert entry.side is SHORT and entry.quantity == D("40") and entry.price == D("100")
    assert env.sim.held_quantity("AAA") == D("40")
    assert env.sim.position_direction("AAA") is PositionDirection.SHORT
    (position,) = await env.sim.list_positions()
    assert position.direction is PositionDirection.SHORT and position.quantity == D("40")
    assert position.avg_entry_price == D("100") and position.cost_basis == D("4000")


@pytest.mark.parametrize("setup, side, code", [
    ("flat", COVER, "no_short_position"),
    ("short", SELL, "would_short"),       # a SELL never closes (or adds to) a short
    ("short", BUY, "opposite_position_open"),
    ("long", SHORT, "opposite_position_open"),
    ("long", COVER, "no_short_position"),
])
async def test_no_order_can_open_a_reversal_or_close_the_wrong_book(setup, side, code):
    env = Env([*flat((9, 30), 8)])
    if setup == "short":
        await enter_short(env)
    elif setup == "long":
        await env.enter(100)
    purpose = ENTRY if side.is_entry else EXIT
    with pytest.raises(BrokerRejectedError) as raised:
        await env.submit(side, purpose, OrderType.MARKET, 10)
    assert raised.value.code == code


async def test_a_cover_cannot_exceed_the_short():
    env = Env([*flat((9, 30), 8)])
    await enter_short(env, 40)
    with pytest.raises(BrokerRejectedError) as raised:
        await env.submit(COVER, EXIT, OrderType.MARKET, 41)
    assert raised.value.code == "exceeds_held_quantity"


# --------------------------------------------------------------- slippage (adverse)


async def test_slippage_is_adverse_on_both_legs_a_short_sells_lower_and_covers_higher():
    env = Env([*flat((9, 30), 8)], slippage_bps=D("10"))
    (entry,) = await enter_short(env, 100)
    assert entry.price == D("99.9000") and entry.slippage == D("0.1")   # sells BELOW the open
    await env.submit(COVER, EXIT, OrderType.MARKET, 100)
    (cover,) = env.go(9, 33)
    assert cover.price == D("100.1000") and cover.slippage == D("0.1")  # covers ABOVE the open
    assert env.sim.held_quantity("AAA") == D("0")


# ------------------------------------------------------------- bar stops (above)


async def test_a_short_stop_fires_when_the_high_reaches_it_not_when_the_low_falls():
    env = Env([
        bar((9, 30), 100, 100, 100, 100),
        bar((9, 31), 100, 100, 100, 100),
        bar((9, 32), 100, "101.5", 90, 100),    # falls to 90 (favorable) but stays below 102
        bar((9, 33), 100, 102, 99, 101),         # reaches the 102 stop
        *flat((9, 34), 3, price="101"),
    ])
    await enter_short(env, 100)
    await cover_stop(env, 100, "102")
    assert covers(env) == [] and env.go(9, 33) == ()   # 09:32's low never triggers a short stop
    (ex,) = [e for e in env.go(9, 34) if e.side is COVER]
    assert ex.order_type is OrderType.STOP and ex.price == D("102")
    assert env.sim.held_quantity("AAA") == D("0")


async def test_a_gap_through_a_short_stop_fills_at_the_gapped_open_not_the_stop():
    env = Env([bar((9, 30), 100, 100, 100, 100), bar((9, 31), 100, 100, 100, 100),
               bar((9, 32), 100, 100, 100, 100), bar((9, 33), 105, 106, 104, 105),
               *flat((9, 34), 3, price="105")])
    await enter_short(env, 100)
    await cover_stop(env, 100, "102")
    (ex,) = [e for e in env.go(9, 34) if e.side is COVER]
    assert ex.price == D("105") and ex.price > D("102")     # worse than the stop: the gap price


async def test_a_short_native_trailing_stop_ratchets_down_with_lows_and_never_up():
    env = Env([
        bar((9, 30), 100, 100, 100, 100),
        bar((9, 31), 100, 100, 100, 100),
        bar((9, 32), 100, 100, 95, 96),          # a new low: the stop falls to 2% above 95
        bar((9, 33), 96, "96.5", 95, 96),        # rebound stays below the stop; no loosening
        bar((9, 34), 96, "97.5", 96, 97),        # reaches 96.90
        *flat((9, 35), 3, price="97"),
    ])
    await enter_short(env, 100)
    trail = await env.submit(COVER, STOP_PURPOSE, OrderType.TRAILING_STOP, 100, trail_percent=2)
    state = env.sim.protection_state(trail.broker_order_id)
    assert state.direction is PositionDirection.SHORT
    assert state.stop_price == D("102.00")                    # 2% ABOVE the last price (100)
    env.go(9, 33)
    assert env.sim.protection_state(trail.broker_order_id).stop_price == D("96.90")
    env.go(9, 34)
    assert env.sim.protection_state(trail.broker_order_id).stop_price == D("96.90")  # never up
    (ex,) = [e for e in env.go(9, 35) if e.side is COVER]
    assert ex.price == D("96.90")


async def test_a_fixed_short_stop_may_be_tightened_downward_but_never_loosened():
    env = Env([*flat((9, 30), 8)], stop_replace_latency=timedelta(0))
    await enter_short(env, 100)
    stop = await cover_stop(env, 100, "104")
    with pytest.raises(ValueError, match="short protective stop must never move upward"):
        await env.sim.replace_order(stop.broker_order_id, stop_price=D("105"))
    await env.sim.replace_order(stop.broker_order_id, stop_price=D("103"))   # tightening is fine
    assert env.sim.protective_coverage("AAA") == D("100")


# ------------------------------------------------------------------ quote evidence


async def test_in_quote_mode_a_short_stop_triggers_on_the_ask_and_covers_at_the_ask():
    env = Env(quote_events(), slippage_ticks=D("1"))
    env.go(9, 31, 5)
    await sell_short(env, 50)
    env.go(9, 31, 20)
    await cover_stop(env, 50, "100.40")
    assert env.go(9, 31, 30) == ()                      # the ask is still below the stop
    executions = env.go(9, 33, 20)
    (ex,) = [e for e in executions if e.side is COVER]
    assert ex.order_type is OrderType.STOP and ex.evidence == "QUOTE"
    assert ex.reference_price == D("100.40")            # the ask that reached the stop
    assert ex.price == D("100.41")                      # covers ABOVE it: one adverse tick
    assert env.sim.held_quantity("AAA") == D("0")
