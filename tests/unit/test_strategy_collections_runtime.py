"""SHADOW lifecycle: per-symbol BUY/SELL collections, arbitration and stop policies.

Drives the real runtime graph (credentials-free SHADOW mode, simulated broker,
temporary SQLite) with scripted strategy doubles; nothing here touches a broker.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from scripted_strategies import build_registry

from mike_trade.app import (
    AppSettings,
    ShadowScenarioSettings,
    StrategyAssignment,
    SymbolStrategySettings,
    build_runtime,
)
from mike_trade.domain.errors import ConfigurationError
from mike_trade.domain.protection import ProtectionKind
from mike_trade.domain.trading import OrderPurpose, OrderSide
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, MarketContext, QuoteSnapshot
from mike_trade.runtime import ExecutionMode
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.buy.vwap_bounce import VwapBounceBuy
from mike_trade.strategies.sell.vwap_breakdown import VwapBreakdownSell

START = datetime(2026, 10, 8, 14, 32, tzinfo=UTC)
SCENARIO = "collections"


class Clock:
    def __init__(self, value: datetime = START) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def ctx(symbol: str, at: datetime, version: int, bid: str = "100", ask: str | None = None):
    bid_d = D(bid)
    ask_d = D(ask) if ask is not None else bid_d + D("0.1")
    bars = tuple(
        Candle(symbol, CandleTimeframe.M1, at - timedelta(minutes=2 - i),
               at - timedelta(minutes=1 - i), bid_d, bid_d + D("0.2"), bid_d - D("0.2"), bid_d,
               D("100"), "iex", False, CandleOrigin.REPLAY, PriceBasis.RAW, CandleStatus.CLOSED)
        for i in range(2)
    )
    return MarketContext(
        symbol=symbol, as_of=at, version=version, feed_id="iex", consolidated=False,
        price_basis=PriceBasis.RAW, session_status=SessionStatus.REGULAR,
        session_id=f"XNYS-{at.date().isoformat()}",
        freshness=ContextFreshness(at, timedelta(0), timedelta(seconds=90), False, None),
        candles={CandleTimeframe.M1: bars}, indicators={"vwap_prev": D("100"), "vwap": D("100")},
        latest_quote=QuoteSnapshot(symbol, bid_d, ask_d, D("100"), D("100"), at, at, "iex"),
    )


def assign(*pairs: tuple[str, dict]) -> tuple[StrategyAssignment, ...]:
    return tuple(StrategyAssignment(f"{pid}_cfg", pid, params) for pid, params in pairs)


async def start(tmp_path: Path, clock: Clock, settings: dict, *, registry=None, name="t.sqlite3"):
    scenario = ShadowScenarioSettings(
        name=SCENARIO, starting_cash=D("50000"), per_symbol_budget=D("3000"),
        total_budget=D("20000"), max_entry_cycles_per_session=3, consecutive_loss_limit=3,
        post_sale_cooldown=timedelta(0), minimum_hold=timedelta(minutes=1),
    )
    runtime = build_runtime(
        AppSettings(mode=ExecutionMode.SHADOW, db_path=tmp_path / name,
                    shadow_scenarios=(scenario,), **settings),
        overrides={"clock": clock, "strategies": registry or build_registry()},
    )
    await runtime.start()
    return runtime, runtime.broker.scenarios[SCENARIO]  # type: ignore[attr-defined]


def orders(shadow, purpose: OrderPurpose, side: OrderSide | None = None):
    return [o for o in shadow.account.state.orders.values()
            if o.purpose is purpose and (side is None or o.side is side)]


def one_symbol(buys, sells, stop=None, symbol="SPY"):
    cfg = {"symbols": (symbol,), "symbol_strategies": (
        SymbolStrategySettings(symbol, assign(*buys), assign(*sells)),)}
    if stop is not None:
        cfg["symbol_stop_policies"] = {symbol: stop}
    return cfg


async def test_simultaneous_buy_signals_open_one_position_and_never_scale(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, one_symbol(
        [("buy_charlie", {"fire_at": 3}), ("buy_alpha", {"fire_at": 3}),
         ("buy_bravo", {"fire_at": 3, "fire_every": 1})], []))
    try:
        life = runtime.lifecycle
        first = await life.process({"SPY": ctx("SPY", clock.value, 3)})
        assert sum(a.startswith("ENTRY:") for a in first[SCENARIO]) == 1
        replay = await life.process({"SPY": ctx("SPY", clock.value, 3)})
        clock.value += timedelta(minutes=1)
        later = await life.process({"SPY": ctx("SPY", clock.value, 4)})
        assert not any(a.startswith("ENTRY:") for a in (*replay[SCENARIO], *later[SCENARIO]))
        (entry,) = orders(shadow, OrderPurpose.ENTRY)
        assert entry.strategy_key == "buy_alpha"  # deterministic winner
        position = shadow.account.state.positions["SPY"]
        assert position.strategy_key == "buy_alpha" and position.quantity == entry.quantity
        (stop,) = orders(shadow, OrderPurpose.PROTECTIVE_STOP)
        assert stop.quantity == position.quantity
    finally:
        await runtime.stop()


async def test_multiple_sell_signals_close_once_without_oversell(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, one_symbol(
        [("buy_bravo", {"fire_at": 3})],
        [("sell_charlie", {"fire_at": 4}), ("sell_alpha", {"fire_at": 4}),
         ("sell_bravo", {"fire_at": 4})]))
    try:
        life = runtime.lifecycle
        await life.process({"SPY": ctx("SPY", clock.value, 3)})
        held = shadow.account.state.positions["SPY"].quantity
        clock.value += timedelta(minutes=2)
        actions = await life.process({"SPY": ctx("SPY", clock.value, 4)})
        assert sum(a.startswith("EXIT:") for a in actions[SCENARIO]) == 1
        (exit_order,) = orders(shadow, OrderPurpose.STRATEGY_EXIT)
        assert exit_order.quantity == held and exit_order.side is OrderSide.SELL
        assert "SPY" not in shadow.account.state.positions  # flat: no short, no residual
        assert shadow.stops.state(shadow.account.state.orders[exit_order.broker_order_id].cycle_id
                                  ).status == "CANCELED"
        clock.value += timedelta(minutes=1)
        again = await life.process({"SPY": ctx("SPY", clock.value, 4)})
        assert not any(a.startswith("EXIT:") for a in again[SCENARIO])
    finally:
        await runtime.stop()


async def test_minimum_hold_blocks_strategy_exit_but_not_the_protective_stop(tmp_path):
    clock = Clock()
    runtime, shadow = await start(tmp_path, clock, one_symbol(
        [("buy_alpha", {"fire_at": 3})], [("sell_alpha", {"fire_at": 4, "fire_every": 1})]))
    try:
        life = runtime.lifecycle
        await life.process({"SPY": ctx("SPY", clock.value, 3)})
        clock.value += timedelta(seconds=10)  # inside the 60 s minimum hold
        held = await life.process({"SPY": ctx("SPY", clock.value, 4)})
        assert not any(a.startswith("EXIT:") for a in held[SCENARIO])
        assert "SPY" in shadow.account.state.positions
        clock.value += timedelta(seconds=10)  # sell strategy still not eligible, price collapses
        crash = await life.process({"SPY": ctx("SPY", clock.value, 5, bid="97", ask="97.1")})
        assert "STOP_EXIT" in crash[SCENARIO] and "SPY" not in shadow.account.state.positions
    finally:
        await runtime.stop()


@pytest.mark.parametrize(
    ("stop", "kind"),
    [
        ({"type": "fixed", "stop_percent": D("1")}, ProtectionKind.FIXED_STOP),
        ({"type": "fixed", "stop_amount": D("1")}, ProtectionKind.FIXED_STOP),
        ({"type": "native_trailing", "trail_amount": D("1")}, ProtectionKind.BROKER_TRAILING_STOP),
        ({"type": "stepped_trailing", "initial_stop_percent": D("1"),
          "step_trigger_percent": D("1"), "step_percent": D("0.5")},
         ProtectionKind.STEPPED_TRAILING_STOP),
    ],
)
async def test_every_configured_stop_mode_protects_a_symbol_with_no_sell_strategy(
    tmp_path, stop, kind
):
    clock = Clock()
    runtime, shadow = await start(
        tmp_path, clock, one_symbol([("buy_alpha", {"fire_at": 3})], [], stop=stop))
    try:
        life = runtime.lifecycle
        await life.process({"SPY": ctx("SPY", clock.value, 3)})
        position = shadow.account.state.positions["SPY"]
        state = shadow.stops.state(position.cycle_id)
        assert state.policy.kind is kind and state.status == "ACTIVE"
        assert state.covered_quantity == position.quantity
        clock.value += timedelta(minutes=2)
        actions = await life.process({"SPY": ctx("SPY", clock.value, 4, bid="98", ask="98.1")})
        assert "STOP_EXIT" in actions[SCENARIO]  # no SELL strategy was needed
        assert "SPY" not in shadow.account.state.positions
    finally:
        await runtime.stop()


@pytest.mark.parametrize(
    "stop",
    [
        {"type": "native_trailing", "trail_percent": D("1.5")},
        {"type": "stepped_trailing", "initial_stop_percent": D("1.5"),
         "step_trigger_percent": D("1"), "step_percent": D("0.5")},
    ],
)
async def test_trailing_stop_follows_gains_up_and_never_moves_down(tmp_path, stop):
    clock = Clock()
    runtime, shadow = await start(
        tmp_path, clock, one_symbol([("buy_alpha", {"fire_at": 3})], [], stop=stop))
    try:
        life = runtime.lifecycle
        await life.process({"SPY": ctx("SPY", clock.value, 3)})
        cycle = shadow.account.state.positions["SPY"].cycle_id
        stops = [shadow.stops.state(cycle).stop_price]
        for index, bid in enumerate(("101.5", "103", "104", "103.5", "103.2", "103.6")):
            clock.value += timedelta(seconds=20)
            await life.process({"SPY": ctx("SPY", clock.value, 10 + index, bid=bid)})
            stops.append(shadow.stops.state(cycle).stop_price)
        assert stops[3] > stops[0]  # favorable moves raised the stop
        assert stops == sorted(stops)  # never lowered, including through the pullback
        assert stops[-1] == stops[-3] == max(stops)
        assert "SPY" in shadow.account.state.positions  # pullback stayed above the stop
    finally:
        await runtime.stop()


async def test_symbols_keep_independent_collections_state_and_parameters(tmp_path):
    clock = Clock()
    settings = {
        "symbols": ("SPY", "QQQ"),
        "symbol_strategies": (
            SymbolStrategySettings(
                "SPY", assign(("buy_alpha", {"fire_on_nth": 2})),
                assign(("sell_alpha", {"fire_on_nth": 1}))),
            SymbolStrategySettings(
                "QQQ", assign(("buy_bravo", {"fire_on_nth": 3}), ("buy_charlie", {"fire_at": 99})),
                ()),
        ),
    }
    runtime, shadow = await start(tmp_path, clock, settings)
    try:
        life = runtime.lifecycle
        assert [b.strategy_id for b in life.collections("SPY").buys] == ["buy_alpha"]
        assert [b.strategy_id for b in life.collections("QQQ").buys] == ["buy_bravo", "buy_charlie"]
        assert life.collections("QQQ").sells == ()
        spy_entered = qqq_entered = None
        for n in (1, 2, 3, 4):
            clock.value += timedelta(seconds=30)
            result = await life.process({
                "SPY": ctx("SPY", clock.value, n), "QQQ": ctx("QQQ", clock.value, n)})
            for action in result[SCENARIO]:
                pass
            spy_entered = spy_entered or ("SPY" in shadow.account.state.positions and n)
            qqq_entered = qqq_entered or ("QQQ" in shadow.account.state.positions and n)
        # Each strategy fired on ITS OWN Nth evaluation (state is per symbol+strategy).
        assert (spy_entered, qqq_entered) == (2, 3)
        assert shadow.account.state.positions["SPY"].strategy_key == "buy_alpha"
        assert shadow.account.state.positions["QQQ"].strategy_key == "buy_bravo"
        keys = set(shadow.states)
        assert ("buy", "buy_alpha", "SPY") in keys and ("buy", "buy_bravo", "QQQ") in keys
        assert ("buy", "buy_alpha", "QQQ") not in keys and ("sell", "sell_alpha", "QQQ") not in keys
    finally:
        await runtime.stop()


async def test_state_is_persisted_per_instance_and_legacy_ids_are_preserved(tmp_path):
    registry = build_registry()
    registry.register(VwapBounceBuy)
    registry.register(VwapBreakdownSell)
    clock = Clock()
    runtime, shadow = await start(
        tmp_path, clock,
        one_symbol([("vwap_bounce", {}), ("buy_alpha", {"fire_at": 99})], []),
        registry=registry, name="persist.sqlite3")
    try:
        await runtime.lifecycle.process({"SPY": ctx("SPY", clock.value, 1)})
    finally:
        await runtime.stop()
    with sqlite3.connect(tmp_path / "persist.sqlite3") as db:
        rows = {r[0]: r[1:] for r in db.execute(
            "SELECT instance_id, strategy_key, role, pair_key FROM strategy_instances")}
    legacy = [k for k in rows if k.endswith(":vwap_reference:buy:SPY")]
    scoped = [k for k in rows if k.endswith(":buy_alpha:buy:SPY")]
    assert len(legacy) == 1 and len(scoped) == 1 and len(rows) == 2
    assert rows[legacy[0]] == ("vwap_bounce", "BUY", "vwap_reference")
    assert rows[scoped[0]] == ("buy_alpha", "BUY", None)
    assert legacy[0].split(":vwap_reference")[0] == scoped[0].split(":buy_alpha")[0]


@pytest.mark.parametrize(
    ("buys", "sells", "code"),
    [
        ([("rsi_reversal", {})], [], "STRATEGY_SIDE_MISMATCH"),
        ([("vwap_bounce", {})], [("vwap_bounce", {})], "STRATEGY_SIDE_MISMATCH"),
        ([("vwap_bounce", {"bogus": 1})], [], "STRATEGY_NOT_USABLE"),
        ([("no_such_plugin", {})], [], "STRATEGY_NOT_USABLE"),
        ([("vwap_bounce", {})], [("rsi_reversal", {})], "STRATEGY_TIMEFRAME_UNSUPPORTED"),
        ([("momentum_breakout", {}), ("momentum_breakout", {"lookback_bars": 5})], [],
         "STRATEGY_DUPLICATE"),
    ],
)
def test_misconfigured_collections_fail_closed_at_build_time(tmp_path, buys, sells, code):
    with pytest.raises(ConfigurationError) as raised:
        build_runtime(
            AppSettings(mode=ExecutionMode.SHADOW, db_path=tmp_path / "bad.sqlite3",
                        symbols=("SPY",),
                        symbol_strategies=(SymbolStrategySettings(
                            "SPY", assign(*buys), assign(*sells)),)),
            overrides={"clock": Clock()},
        )
    assert raised.value.code == code
