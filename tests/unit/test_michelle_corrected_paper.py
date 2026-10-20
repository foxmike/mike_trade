"""PAPER lifecycle of the corrected Michelle strategies on the simulated Alpaca exchange.

Covers the broker-facing half of the rules: Stage 1 stop established with the fill, the one-way
breakeven replacement at fill + $0.50, the application-managed protective exit (classified as a
PROTECTIVE_STOP, never a reviewable strategy SELL) and the account-level $150 daily-loss control
that closes every position and blocks every strategy's new entries.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from michelle_corrected_support import (
    BUY_ID,
    OPEN,
    SELL_ID,
    ctx,
    developing_candle,
    qqq_bars,
    regular_bars,
)
from scripted_strategies import build_registry
from tests.live_support import start_paper
from tests.support.fake_alpaca import FakeAlpacaExchange, FakeClock

from mike_trade.market_data.context import MarketContext
from mike_trade.strategies.buy.vwap_bounce_break_michelle_corrected import (
    VwapBounceBreakMichelleCorrectedBuy,
)
from mike_trade.strategies.sell.vwap_bounce_break_michelle_corrected import (
    VwapBounceBreakMichelleCorrectedSell,
)

NOW = OPEN + timedelta(minutes=7, seconds=20)  # 09:37:20 New York, inside C+1


def registry():  # noqa: ANN201
    reg = build_registry()
    reg.register(VwapBounceBreakMichelleCorrectedBuy)
    reg.register(VwapBounceBreakMichelleCorrectedSell)
    return reg


def session_context(price: str, ask: str, *, high: str | None = None) -> MarketContext:
    candles = regular_bars(symbol="AAA")
    dev = developing_candle(7, price, high or price, price, price, symbol="AAA")
    context, _ = ctx(
        candles, developing=dev, trade=price, qqq=qqq_bars(7), qqq_price="408", symbol="AAA",
        as_of=NOW, quote=(price, ask),
    )
    return context


def rows(tmp_path: Path, sql: str, *args):  # noqa: ANN001, ANN201
    conn = sqlite3.connect(tmp_path / "paper.sqlite3")
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


@pytest.fixture
def exchange() -> FakeAlpacaExchange:
    ex = FakeAlpacaExchange(FakeClock(NOW))
    ex.set_price("AAA", "100.45")
    return ex


async def test_entry_establishes_the_fixed_stage_one_stop_immediately(tmp_path, exchange):
    runtime = await start_paper(
        tmp_path, exchange, [(BUY_ID, {})], [(SELL_ID, {})], registry=registry()
    )
    try:
        out = await runtime.lifecycle.process({"AAA": session_context("100.45", "100.47")})
        assert any(a.startswith("FILL:BUY") for a in out["live"]) and "PROTECTED" in out["live"]
        assert exchange.position_qty("AAA") == D("29")
        (stop,) = exchange.open_stops("AAA")
        assert (stop["side"], stop["time_in_force"], D(stop["stop_price"])) == (
            "sell", "gtc", D("99.70"))  # fill 100.45 - 0.75, not 0.5% of anything
        ((kind, status, price),) = rows(
            tmp_path, "SELECT kind, status, stop_price FROM trailing_stop_states")
        assert (kind, status, D(price)) == ("FIXED_THEN_BREAKEVEN_STOP", "ACTIVE", D("99.70"))
        # the setup is consumed: the same signal never opens a second position
        await runtime.lifecycle.process({"AAA": session_context("100.50", "100.52")})
        assert len([p for p in exchange.posts() if p["side"] == "buy"]) == 1
    finally:
        await runtime.stop()


async def test_stage_two_replaces_the_stop_with_breakeven_once_and_never_lowers_it(tmp_path, exchange):
    runtime = await start_paper(
        tmp_path, exchange, [(BUY_ID, {})], [(SELL_ID, {})], registry=registry()
    )
    try:
        life = runtime.lifecycle
        await life.process({"AAA": session_context("100.45", "100.47")})
        exchange.clock.advance(seconds=5)
        # fill + 0.49: still Stage 1
        await life.process({"AAA": session_context("100.94", "100.96")})
        assert D(exchange.open_stops("AAA")[0]["stop_price"]) == D("99.70")
        exchange.clock.advance(seconds=5)
        exchange.set_price("AAA", "100.95")
        life.observe_trade("AAA", D("100.95"), exchange.clock.now())  # fill + 0.50 exactly
        await life.process({"AAA": session_context("100.95", "100.97")})
        (stop,) = exchange.open_stops("AAA")
        assert D(stop["stop_price"]) == D("100.45")  # Stage 2: breakeven = the fill
        exchange.clock.advance(seconds=5)
        await life.process({"AAA": session_context("100.60", "100.62")})
        assert D(exchange.open_stops("AAA")[0]["stop_price"]) == D("100.45")  # never moves down
    finally:
        await runtime.stop()


async def test_breakeven_breach_is_an_immediate_full_protective_exit_not_a_reviewable_sell(
    tmp_path, exchange
):
    runtime = await start_paper(
        tmp_path, exchange, [(BUY_ID, {})], [(SELL_ID, {})], registry=registry()
    )
    try:
        life = runtime.lifecycle
        await life.process({"AAA": session_context("100.45", "100.47")})
        exchange.clock.advance(seconds=5)
        exchange.set_price("AAA", "100.95")
        life.observe_trade("AAA", D("100.95"), exchange.clock.now())
        await life.process({"AAA": session_context("100.95", "100.97")})
        exchange.clock.advance(seconds=5)
        # an intrabar print at the fill price: the strategy exits at once (the broker price is
        # unchanged, so only the application-managed rule can have fired)
        out = await life.process({"AAA": session_context("100.45", "100.47")})
        assert any(a.startswith("EXIT:FLAT") for a in out["live"]), out
        assert exchange.position_qty("AAA") == 0 and not exchange.open_orders("AAA")
        sells = [p for p in exchange.posts() if p["side"] == "sell" and p["type"] == "market"]
        assert len(sells) == 1 and D(sells[0]["qty"]) == D("29")  # 100% of the position
        ((state,),) = rows(tmp_path, "SELECT state FROM position_cycles")
        assert state == "CLOSED"
    finally:
        await runtime.stop()


async def test_daily_loss_limit_liquidates_everything_and_blocks_every_strategys_entries(
    tmp_path, exchange
):
    runtime = await start_paper(
        tmp_path, exchange,
        [("buy_alpha", {"fire_at": 3, "fire_every": 1}), (BUY_ID, {})],
        [(SELL_ID, {})], registry=registry(),
    )
    try:
        life = runtime.lifecycle
        out = await life.process({"AAA": session_context("100.00", "100.02")})
        assert any(a.startswith("FILL:BUY") for a in out["live"]), out
        held = exchange.position_qty("AAA")
        assert held > 0
        # the stock trades 6.00 below the entry without touching the broker stop (a feed hiccup
        # on the broker side): 29 sh x -6.00 = -174 <= -150 on the unrealized leg alone
        exchange.clock.advance(seconds=5)
        life.observe_trade("AAA", D("94.00"), exchange.clock.now())
        assert life._guard(life.scenario).latched(exchange.clock.now())
        await life._daily_loss_liquidate("test")
        assert exchange.position_qty("AAA") == 0
        assert any(
            p["side"] == "sell" and p["type"] == "market" for p in exchange.posts()
        )
        before = len([p for p in exchange.posts() if p["side"] == "buy"])
        exchange.clock.advance(minutes=1)
        out = await life.process({"AAA": session_context("100.00", "100.02")})
        assert len([p for p in exchange.posts() if p["side"] == "buy"]) == before  # blocked
        assert exchange.position_qty("AAA") == 0
        # durable block until the next session opens
        assert rows(
            tmp_path, "SELECT reason FROM entry_cooldowns WHERE reason = 'daily_loss_limit'"
        )
    finally:
        await runtime.stop()


async def test_migration_admits_the_breakeven_kind_in_both_protection_tables(tmp_path, exchange):
    runtime = await start_paper(
        tmp_path, exchange, [(BUY_ID, {})], [(SELL_ID, {})], registry=registry()
    )
    try:
        sql = " ".join(
            row[0] for row in rows(
                tmp_path,
                "SELECT sql FROM sqlite_schema WHERE name IN "
                "('trailing_stop_states', 'protection_orders')",
            )
        )
        assert sql.count("FIXED_THEN_BREAKEVEN_STOP") == 2
    finally:
        await runtime.stop()


def test_corrected_buy_without_the_corrected_sell_is_refused_at_build_time(tmp_path):
    from mike_trade.app import (
        AppSettings,
        StrategyAssignment,
        SymbolStrategySettings,
        build_runtime,
    )
    from mike_trade.domain.errors import ConfigurationError
    from mike_trade.runtime import ExecutionMode

    with pytest.raises(ConfigurationError, match="requires the corrected Michelle SELL"):
        build_runtime(
            AppSettings(
                mode=ExecutionMode.SHADOW,
                db_path=tmp_path / "pair.sqlite3",
                symbols=("AAA",),
                symbol_strategies=(
                    SymbolStrategySettings(
                        "AAA", (StrategyAssignment("c", BUY_ID, {}),), ()
                    ),
                ),
            ),
            overrides={"strategies": registry()},
        )
