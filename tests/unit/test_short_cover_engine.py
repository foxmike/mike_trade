"""Bidirectional (LONG/SHORT) lifecycle in the BACKTEST engine.

Drives the real ``BacktestRunner`` and ``HistoricalExecutionSimulator`` with deterministic
scripted strategy doubles and synthetic completed bars; no network, broker or stored data.

Lifecycle under test::

    FLAT -> BUY_PENDING   -> LONG  -> SELL_PENDING  -> FLAT
    FLAT -> SHORT_PENDING -> SHORT -> COVER_PENDING -> FLAT

with no automatic reversal, one position per symbol and one full-position exit.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from scripted_strategies import build_bidirectional_registry

from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import ExecutionAssumptions, HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.backtest.runner import (
    BacktestRequest,
    BacktestResult,
    BacktestRunner,
    compute_metrics,
)
from mike_trade.domain.market import Candle, FeedProvenance, Timeframe
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import OrderIntent, OrderPurpose, OrderSide, OrderType, TimeInForce

OPEN = datetime(2025, 3, 10, 13, 30, tzinfo=UTC)  # 09:30 ET
MIN = timedelta(minutes=1)
PROV = DataProvenance("synthetic", "IEX", False, "r1")
FEED = FeedProvenance("replay", "IEX", "synthetic", False)

Bar = tuple[str, str, str, str]  # open, high, low, close
SHORT_ON = {"short_selling_enabled": True}

_SIDES = {
    StrategyRole.BUY: OrderSide.BUY,
    StrategyRole.SELL: OrderSide.SELL,
    StrategyRole.SHORT: OrderSide.SELL_SHORT,
    StrategyRole.COVER: OrderSide.BUY_TO_COVER,
}


def flat(price: str, n: int = 1) -> list[Bar]:
    return [(price, price, price, price)] * n


def events(bars: list[Bar]) -> list[ReplayEvent]:
    out = []
    for index, (o, h, low, c) in enumerate(bars):
        start = OPEN + index * MIN
        out.append(ReplayEvent.bar(
            Candle("AAA", Timeframe.M1, start, start + MIN, D(o), D(h), D(low), D(c),
                   D("100000"), FEED)))
    return out


def run(
    bars: list[Bar],
    strategy_ids: tuple[str, ...],
    params: dict,
    *,
    config: dict | None = None,
    quantity: str = "10",
    capital: str = "100000",
    result_dir: Path,
    assumptions: ExecutionAssumptions | None = None,
) -> BacktestResult:
    clock = ReplayClock(OPEN)
    feed = HistoricalReplayFeed(events(bars), PROV, clock=clock, seed=3)
    sim = HistoricalExecutionSimulator(feed, clock, assumptions or ExecutionAssumptions())

    def factory(evaluation, context, request):  # noqa: ANN001
        role = evaluation.side
        entry = role in (StrategyRole.BUY, StrategyRole.SHORT)
        return OrderIntent(
            intent_id=f"i-{evaluation.signal_id}",
            client_order_id=f"c-{evaluation.signal_id}",
            account_scope_id="backtest:test",
            cycle_id=f"cycle-{evaluation.symbol}",
            symbol=evaluation.symbol,
            side=_SIDES[role],
            purpose=OrderPurpose.ENTRY if entry else OrderPurpose.STRATEGY_EXIT,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            created_at=context.as_of,
            quantity=D(quantity) if entry else sim.held_quantity(evaluation.symbol),
        )

    request = BacktestRequest(
        symbols=("AAA",), window_start=OPEN, window_end=OPEN + timedelta(hours=6),
        period_role=PeriodRole.RESEARCH, config=config or {}, seed=3,
        starting_capital=D(capital), strategy_ids=strategy_ids, strategy_params=params,
        context_history_bars=50, result_directory=result_dir,
    )
    runner = BacktestRunner(build_bidirectional_registry(), feed, clock, sim,
                            intent_factory=factory)
    return asyncio.run(runner.run(request))


def sides(result: BacktestResult) -> list[str]:
    return [f["side"] for f in result.fills]


def fills(result: BacktestResult, side: str) -> list[dict]:
    return [f for f in result.fills if f["side"] == side]


def only(result: BacktestResult, side: str) -> dict:
    (match,) = fills(result, side)
    return match


# ------------------------------------------------------------------ lifecycle and P&L


def test_short_then_cover_lifecycle_books_the_gain_of_a_falling_price(tmp_path):
    # SHORT 10 @ 150, COVER 10 @ 144: gross profit $60.
    bars = flat("150", 6) + flat("144", 10)
    result = run(bars, ("short_alpha", "cover_alpha"),
                 {"short_alpha": {"fire_at": 3}, "cover_alpha": {"fire_at": 8}},
                 config=SHORT_ON, result_dir=tmp_path)
    assert sides(result) == ["SELL_SHORT", "BUY_TO_COVER"]
    short, cover = only(result, "SELL_SHORT"), only(result, "BUY_TO_COVER")
    assert (short["quantity"], short["price"]) == (D("10"), D("150"))
    assert (cover["quantity"], cover["price"]) == (D("10"), D("144"))
    assert cover["exit_reason"] == "STRATEGY_COVER" and short["strategy"] == "short_alpha"
    metrics = compute_metrics(result, starting_capital=D("100000"), bootstrap_samples=0)
    (trade,) = metrics.trades
    assert trade["direction"] == "SHORT" and trade["gross_pnl"] == D("60")
    assert trade["pnl"] == D("60") and trade["entry_strategy"] == "short_alpha"
    assert result.equity_curve[-1]["equity"] == D("100060")  # flat again: cash == equity
    assert result.equity_curve[-1]["cash"] == D("100060")


def test_short_loses_when_the_price_rises(tmp_path):
    # SHORT 10 @ 150, COVER 10 @ 156: gross loss $60.
    result = run(flat("150", 6) + flat("156", 10), ("short_alpha", "cover_alpha"),
                 {"short_alpha": {"fire_at": 3}, "cover_alpha": {"fire_at": 8}},
                 config=SHORT_ON, result_dir=tmp_path)
    (trade,) = compute_metrics(result, starting_capital=D("100000"), bootstrap_samples=0).trades
    assert trade["gross_pnl"] == D("-60") and trade["pnl"] == D("-60")
    assert result.equity_curve[-1]["equity"] == D("99940")


def test_short_equity_marks_the_liability_and_locks_the_proceeds(tmp_path):
    # While short: cash holds the proceeds but equity = cash - market value, and the proceeds
    # (x1.5) are restricted collateral rather than available cash.
    bars = flat("150", 6) + [("150", "150", "140", "140")] * 6
    result = run(bars, ("short_alpha", "cover_alpha"),
                 {"short_alpha": {"fire_at": 3}, "cover_alpha": {"fire_at": 99}},
                 config=SHORT_ON, result_dir=tmp_path)
    after = [row for row in result.equity_curve if row["time"] > OPEN + 8 * MIN][-1]
    assert after["cash"] == D("101500")                       # 100000 + 10 * 150 proceeds
    assert after["equity"] == D("101500") - D("10") * D("140")  # liability marked at 140
    assert after["restricted_collateral"] == D("2250")        # 1.5 x 1500 proceeds
    assert after["available_cash"] == D("101500") - D("2250")  # proceeds are NOT buying power
    assert after["available_cash"] < D("100000")


def test_shorting_is_disabled_unless_explicitly_authorized(tmp_path):
    result = run(flat("150", 12), ("short_alpha", "cover_alpha"),
                 {"short_alpha": {"fire_at": 3}, "cover_alpha": {"fire_at": 8}},
                 result_dir=tmp_path)  # no short_selling_enabled
    assert result.fills == ()


def test_short_margin_must_fit_in_own_cash_never_in_sale_proceeds(tmp_path):
    # 20 x $150 = $3000 notional needs $1500 own margin; only $1000 is available.
    result = run(flat("150", 10), ("short_alpha",), {"short_alpha": {"fire_at": 3}},
                 config=SHORT_ON, quantity="20", capital="1000", result_dir=tmp_path)
    assert result.fills == ()
    refusal = [d for d in result.decisions if d.get("kind") == "ENTRY_REFUSED"]
    assert [d["reason"] for d in refusal] == ["INSUFFICIENT_SHORT_MARGIN"]


def test_gross_exposure_ceiling_applies_to_short_entries(tmp_path):
    result = run(flat("150", 10), ("short_alpha",), {"short_alpha": {"fire_at": 3}},
                 config={**SHORT_ON, "max_gross_exposure": "1000"}, result_dir=tmp_path)
    assert result.fills == ()
    assert [d["reason"] for d in result.decisions if d.get("kind") == "ENTRY_REFUSED"] == [
        "GROSS_EXPOSURE_LIMIT"]


# ------------------------------------------------------ transitions go through FLAT


def test_sell_then_short_and_cover_then_buy_always_pass_through_flat(tmp_path):
    bars = flat("100", 40)
    result = run(
        bars, ("buy_alpha", "sell_alpha", "short_alpha", "cover_alpha", "buy_bravo"),
        {"buy_alpha": {"fire_at": 3}, "sell_alpha": {"fire_at": 8},
         "short_alpha": {"fire_at": 14}, "cover_alpha": {"fire_at": 20},
         "buy_bravo": {"fire_at": 26}},
        config=SHORT_ON, result_dir=tmp_path)
    assert sides(result) == ["BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER", "BUY"]
    position = D(0)
    for fill in result.fills:  # never long and short together, never a reversal through zero
        signed = {"BUY": 1, "BUY_TO_COVER": 1, "SELL": -1, "SELL_SHORT": -1}[fill["side"]]
        before = position
        position += signed * fill["quantity"]
        if fill["side"] in ("SELL", "BUY_TO_COVER"):
            assert before != 0 and position == 0
        if fill["side"] in ("BUY", "SELL_SHORT"):
            assert before == 0
    assert [c.get("direction", "LONG") for c in result.cycles] == [
        "LONG", "SHORT", "LONG"]


def test_a_sell_never_closes_a_short_and_a_cover_never_closes_a_long(tmp_path):
    # short held: SELL strategy fires every bar -> ignored; later the cover closes it.
    short_run = run(flat("100", 30), ("short_alpha", "sell_alpha", "cover_alpha"),
                    {"short_alpha": {"fire_at": 3}, "sell_alpha": {"fire_at": 6, "fire_every": 1},
                     "cover_alpha": {"fire_at": 20}},
                    config=SHORT_ON, result_dir=tmp_path / "s")
    assert sides(short_run) == ["SELL_SHORT", "BUY_TO_COVER"]
    long_run = run(flat("100", 30), ("buy_alpha", "cover_alpha", "sell_alpha"),
                   {"buy_alpha": {"fire_at": 3}, "cover_alpha": {"fire_at": 6, "fire_every": 1},
                    "sell_alpha": {"fire_at": 20}},
                   config=SHORT_ON, result_dir=tmp_path / "l")
    assert sides(long_run) == ["BUY", "SELL"]


# ------------------------------------------------------------------ arbitration


def test_buy_and_short_compete_for_entry_and_only_one_direction_trades(tmp_path):
    result = run(flat("100", 16), ("buy_alpha", "short_alpha"),
                 {"buy_alpha": {"fire_at": 3}, "short_alpha": {"fire_at": 3}},
                 config=SHORT_ON, result_dir=tmp_path)
    assert sides(result) == ["BUY"]  # equal bar time: the LONG entry ranks first
    entry = next(d for d in result.decisions if d["kind"] == "ENTRY")
    assert entry["side"] == "ENTRY"
    outcomes = {r["strategy_id"]: r["outcome"] for r in entry["records"]}
    assert outcomes == {"buy_alpha": "SELECTED", "short_alpha": "REJECTED"}


def test_multiple_short_strategies_open_exactly_one_short(tmp_path):
    result = run(flat("100", 16), ("short_alpha", "short_bravo", "short_charlie"),
                 {s: {"fire_at": 3, "fire_every": 1} for s in
                  ("short_alpha", "short_bravo", "short_charlie")},
                 config=SHORT_ON, result_dir=tmp_path)
    assert sides(result) == ["SELL_SHORT"]  # fire_every keeps signalling while short: no add
    assert only(result, "SELL_SHORT")["strategy"] == "short_alpha"  # deterministic winner
    assert only(result, "SELL_SHORT")["quantity"] == D("10")


def test_multiple_cover_strategies_close_the_whole_short_once(tmp_path):
    result = run(flat("100", 30), ("short_alpha", "cover_alpha", "cover_bravo", "cover_charlie"),
                 {"short_alpha": {"fire_at": 3},
                  **{c: {"fire_at": 10} for c in ("cover_alpha", "cover_bravo", "cover_charlie")}},
                 config=SHORT_ON, result_dir=tmp_path)
    assert sides(result) == ["SELL_SHORT", "BUY_TO_COVER"]
    assert only(result, "BUY_TO_COVER")["quantity"] == only(result, "SELL_SHORT")["quantity"]


# ------------------------------------------------------ indicator-state continuity (all roles)


def test_every_role_keeps_its_state_advancing_while_ineligible(tmp_path):
    """c9a4423 must hold for all four roles: ``count`` is each double's own evaluation count.

    Each double fires on its Nth evaluation; if it were skipped while ineligible it would
    fire later than bar N, so firing on bar N proves it was evaluated on every bar.
    """
    result = run(
        flat("100", 40),
        ("buy_alpha", "sell_alpha", "short_alpha", "cover_alpha"),
        {"buy_alpha": {"fire_at": 3},                    # long 4..9
         "sell_alpha": {"fire_at": 9},
         "short_alpha": {"fire_on_nth": 16},             # ineligible while long (bars 4-9)
         "cover_alpha": {"fire_on_nth": 24}},            # ineligible while flat/long (1-16)
        config=SHORT_ON, result_dir=tmp_path)
    shorts = fills(result, "SELL_SHORT")
    covers = fills(result, "BUY_TO_COVER")
    assert len(shorts) == 1 and len(covers) == 1
    # 16th evaluation -> signal on bar 16 -> filled at the next bar's open
    assert shorts[0]["time"] == OPEN + 16 * MIN
    assert covers[0]["time"] == OPEN + 24 * MIN


# ------------------------------------------------------------------ protective stops (BACKTEST)

FIXED = {"protective_stop": {"type": "fixed", "stop_percent": "2"}}
NATIVE = {"protective_stop": {"type": "native_trailing", "trail_percent": "2"}}
STEPPED = {"protective_stop": {"type": "stepped_trailing", "initial_stop_percent": "2",
                               "step_percent": "1", "step_trigger_percent": "1"}}
ONE_SHORT = ("short_alpha",)
SHORT_AT_3 = {"short_alpha": {"fire_at": 3}}


def test_a_fixed_short_stop_sits_above_entry_and_covers_when_price_rises_to_it(tmp_path):
    bars = flat("100", 6) + [("100", "103", "100", "101")] + flat("101", 4)
    result = run(bars, ONE_SHORT, SHORT_AT_3, config={**SHORT_ON, **FIXED}, result_dir=tmp_path)
    assert sides(result) == ["SELL_SHORT", "BUY_TO_COVER"]
    assert result.protection_events[0]["stop_price"] == D("102.00")  # ABOVE the 100 entry
    cover = only(result, "BUY_TO_COVER")
    assert cover["price"] == D("102") and cover["exit_reason"] == "PROTECTIVE_STOP"
    (trade,) = compute_metrics(result, starting_capital=D("100000"), bootstrap_samples=0).trades
    assert trade["pnl"] == D("-20") and trade["direction"] == "SHORT"


def test_a_gap_through_a_short_stop_fills_at_the_gapped_open_not_the_stop(tmp_path):
    bars = flat("100", 6) + [("105", "106", "104", "105")] + flat("105", 4)
    result = run(bars, ONE_SHORT, SHORT_AT_3, config={**SHORT_ON, **FIXED}, result_dir=tmp_path)
    assert only(result, "BUY_TO_COVER")["price"] == D("105")  # worse than the 102 stop
    (trade,) = compute_metrics(result, starting_capital=D("100000"), bootstrap_samples=0).trades
    assert trade["pnl"] == D("-50")


def test_slippage_is_adverse_on_both_legs_of_a_short(tmp_path):
    bars = flat("100", 6) + [("100", "103", "100", "101")] + flat("101", 4)
    result = run(bars, ONE_SHORT, SHORT_AT_3, config={**SHORT_ON, **FIXED}, result_dir=tmp_path,
                 assumptions=ExecutionAssumptions(slippage_bps=D("10")))
    assert only(result, "SELL_SHORT")["price"] == D("99.9")      # sells LOWER than the touch
    # the 2% stop is anchored to the 99.9 fill -> 101.90; the cover then pays 10 bps MORE
    assert result.protection_events[0]["stop_price"] == D("101.90")
    assert only(result, "BUY_TO_COVER")["price"] == D("102.0019")


def test_a_protective_stop_overrides_the_minimum_hold(tmp_path):
    bars = flat("100", 6) + [("100", "103", "100", "101")] + flat("101", 4)
    result = run(bars, ONE_SHORT, SHORT_AT_3,
                 config={**SHORT_ON, **FIXED, "minimum_hold_seconds": 3600}, result_dir=tmp_path)
    assert sides(result) == ["SELL_SHORT", "BUY_TO_COVER"]
    assert only(result, "BUY_TO_COVER")["exit_reason"] == "PROTECTIVE_STOP"


def test_a_stop_fill_never_creates_a_reverse_position(tmp_path):
    # after the stop covers the short, the cover/short doubles keep firing but nothing reopens
    bars = flat("100", 6) + [("100", "103", "100", "101")] + flat("101", 14)
    result = run(bars, ("short_alpha", "cover_alpha"),
                 {"short_alpha": {"fire_at": 3}, "cover_alpha": {"fire_at": 9, "fire_every": 1}},
                 config={**SHORT_ON, **FIXED}, result_dir=tmp_path)
    assert sides(result) == ["SELL_SHORT", "BUY_TO_COVER"]  # no BUY, no second short


def test_a_short_trailing_stop_ratchets_down_with_price_and_never_loosens(tmp_path):
    bars = (flat("100", 6) + [("100", "100", "95", "95"), ("95", "96.5", "95", "96"),
                              ("96", "97.5", "96", "97")] + flat("97", 3))
    result = run(bars, ONE_SHORT, SHORT_AT_3, config={**SHORT_ON, **NATIVE}, result_dir=tmp_path)
    stops = [e for e in result.protection_events if e["event_type"] == "STOP_MOVED"]
    assert [e["new_stop_price"] for e in stops] == [D("96.90")]      # 2% above the 95 low
    assert all(e["new_stop_price"] < e["previous_stop_price"] for e in stops)
    # the 96.5 rebound did NOT trigger or loosen it; the 97.5 high reaches 96.90
    cover = only(result, "BUY_TO_COVER")
    assert cover["price"] == D("96.9") and cover["exit_reason"] == "PROTECTIVE_STOP"
    (trade,) = compute_metrics(result, starting_capital=D("100000"), bootstrap_samples=0).trades
    assert trade["pnl"] == D("31.0")


def test_a_short_stepped_trailing_stop_steps_down_only(tmp_path):
    bars = (flat("100", 6) + [("100", "100", "97", "97"), ("97", "98.5", "97", "98"),
                              ("98", "99.5", "98", "99")] + flat("99", 3))
    result = run(bars, ONE_SHORT, SHORT_AT_3, config={**SHORT_ON, **STEPPED}, result_dir=tmp_path)
    moves = [e for e in result.protection_events if e["event_type"] == "STOP_MOVED"]
    assert [e["new_stop_price"] for e in moves] == [D("99.00")]  # 3 whole $1 steps, +$2 distance
    assert only(result, "BUY_TO_COVER")["price"] == D("99")


def test_a_long_stop_still_sits_below_entry_and_is_unchanged(tmp_path):
    bars = flat("100", 6) + [("100", "100", "97", "98")] + flat("98", 4)
    result = run(bars, ("buy_alpha",), {"buy_alpha": {"fire_at": 3}},
                 config=FIXED, result_dir=tmp_path)
    assert result.protection_events[0]["stop_price"] == D("98.00")
    assert only(result, "SELL")["price"] == D("98") and "direction" not in only(result, "SELL")
