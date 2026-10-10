"""Focused mathematical, signal, guard, and replay checks for technical batch 2."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import ExecutionAssumptions, HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.backtest.runner import BacktestRequest, BacktestRunner
from mike_trade.domain import market as domain_market
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import OrderIntent, OrderPurpose, OrderSide, OrderType, TimeInForce
from mike_trade.market_data.candles import Candle, CandleOrigin, CandleStatus, CandleTimeframe, PriceBasis
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.research.commands import _research_registry
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import ParameterValidationError, ReasonCode
from mike_trade.strategies.indicators.finite import (
    awesome_oscillator, hull_moving_average, linear_regression_slope,
    original_keltner, rate_of_change, weighted_moving_average,
)
from mike_trade import app

D = Decimal
OPEN = datetime(2026, 10, 8, 9, 30, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
REGISTRY = _research_registry()


def bar(index: int, close: str | int, *, high: str | int | None = None,
        low: str | int | None = None) -> Candle:
    start = OPEN + timedelta(minutes=index)
    price = D(close)
    return Candle("SPY", CandleTimeframe.M1, start, start + timedelta(minutes=1),
                  price, D(high) if high is not None else price,
                  D(low) if low is not None else price, price, D(100), "sip", True,
                  CandleOrigin.REPLAY, PriceBasis.RAW, CandleStatus.CLOSED)


def context(bars: list[Candle], *, stale: bool = False,
            session: SessionStatus = SessionStatus.REGULAR) -> MarketContext:
    as_of = bars[-1].end + timedelta(seconds=1)
    return MarketContext(
        symbol="SPY", as_of=as_of, version=len(bars), feed_id="sip", consolidated=True,
        price_basis=PriceBasis.RAW, session_status=session, session_id="2026-10-08",
        freshness=ContextFreshness(bars[-1].end, timedelta(seconds=1),
                                   timedelta(minutes=5), stale,
                                   "DATA_TOO_OLD" if stale else None),
        candles={CandleTimeframe.M1: tuple(bars)}, indicators={},
    )


def ordinary(values: list[int]) -> list[Candle]:
    return [bar(i, value) for i, value in enumerate(values)]


def cases() -> dict[str, tuple[dict[str, object], list[Candle]]]:
    nr = [bar(0, 10, high=11, low=9), bar(1, 10, high=12, low=9),
          bar(2, 10, high=11, low=8), bar(3, 10, high="10.5", low="9.5"),
          bar(4, 12, high=12, low=10)]
    upper = [bar(0, 10, high=11, low=9), bar(1, 10, high=11, low=9), bar(2, 16)]
    lower = [bar(0, 10, high=11, low=9), bar(1, 10, high=11, low=9), bar(2, 5)]
    return {
        "rate_of_change_momentum": ({"period": 2}, ordinary([10, 11, 9, 12])),
        "moving_average_envelope": ({"period": 2, "percent": D(10)}, ordinary([10, 10, 14])),
        "moving_average_reclaim": ({"period": 2}, ordinary([10, 9, 12])),
        "triple_moving_average": ({"fast_period": 2, "medium_period": 3,
                                    "slow_period": 4}, ordinary([10, 10, 10, 10, 20])),
        "hull_moving_average": ({"period": 4}, ordinary([10, 9, 8, 7, 6, 5, 20])),
        "linear_regression_trend": ({"period": 3}, ordinary([10, 9, 8, 11])),
        "nr7_breakout": ({"period": 4}, nr),
        "awesome_oscillator": ({}, ordinary([10] * 33 + [9, 12])),
        "keltner_channel_breakout": ({"period": 2}, upper),
        "awesome_oscillator_reversal": ({}, ordinary([10] * 33 + [11, 8])),
        "keltner_channel_breakdown": ({"period": 2}, lower),
        "previous_swing_low_break": ({"lookback": 5}, ordinary([10, 9, 7, 9, 10, 6])),
    }


def state_for(strategy_id: str) -> dict[str, object]:
    return {"eligibility": "WATCHING_BUY"} if REGISTRY.get(strategy_id).spec.side is StrategyRole.BUY else {}


def test_all_twelve_are_registered_in_production_and_research() -> None:
    source = inspect.getsource(app.build_runtime)
    expected = cases()
    assert len(expected) == 12
    assert sum(REGISTRY.get(sid).spec.side is StrategyRole.BUY for sid in expected) == 9
    for strategy_id in expected:
        cls = REGISTRY.get(strategy_id)
        assert source.count(f"{cls.__name__},") == 1


def test_finite_formulas_against_simple_independent_values() -> None:
    assert rate_of_change(D(11), D(10)) == D(10)
    assert linear_regression_slope([D(1), D(2), D(3)]) == D(1)
    assert abs(weighted_moving_average([D(1), D(2), D(3)]) - D(7) / 3) < D("1e-25")
    assert abs(hull_moving_average([D(1), D(2), D(3), D(4), D(5)], 4) - D(5)) < D("1e-25")
    channel = original_keltner([bar(0, 10, high=11, low=9), bar(1, 12, high=13, low=11)])
    assert channel == (D(9), D(11), D(13))
    ao = awesome_oscillator(ordinary([10] * 33 + [11]))
    assert abs(ao - (D(1) / 5 - D(1) / 34)) < D("1e-25")
    with localcontext() as arithmetic:
        arithmetic.prec = 5
        assert original_keltner([bar(0, 10, high=11, low=9),
                                 bar(1, 12, high=13, low=11)]) == channel


@pytest.mark.parametrize("strategy_id", sorted(cases()))
def test_new_strategy_signals_warmup_guards_and_replay(strategy_id: str) -> None:
    params, bars = cases()[strategy_id]
    cls = REGISTRY.get(strategy_id)
    assert cls.spec.category == "technical"
    assert cls.spec.trigger_timeframe is CandleTimeframe.M1
    assert cls.required_bars(params) == len(bars)
    assert cls.spec.warmup_bars == cls.required_bars({})
    assert cls().evaluate(context(bars[:-1]), params, state_for(strategy_id)).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,)
    evaluation = cls().evaluate(context(bars), params, state_for(strategy_id))
    assert evaluation.is_signal, strategy_id
    repeat_state = {**state_for(strategy_id), **evaluation.state_updates}
    assert not cls().evaluate(context(bars), params, repeat_state).is_signal
    assert cls().evaluate(context(bars, stale=True), params, state_for(strategy_id)
                          ).reason_codes == (ReasonCode.STALE_DATA,)
    assert cls().evaluate(context(bars, session=SessionStatus.CLOSED), params,
                          state_for(strategy_id)).reason_codes == (ReasonCode.NOT_ELIGIBLE,)
    if cls.spec.side is StrategyRole.BUY:
        assert cls().evaluate(context(bars), params, {}).reason_codes == (
            ReasonCode.NOT_ELIGIBLE,)


@pytest.mark.parametrize("strategy_id", sorted(cases()))
def test_missing_latest_minute_cannot_create_an_edge(strategy_id: str) -> None:
    cls = REGISTRY.get(strategy_id)
    bars = ordinary([10] * 120)
    bars[-1] = bar(121, 10)  # index 119 is absent; the latest observed bar is at 121
    result = cls().evaluate(context(bars), {}, state_for(strategy_id))
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


@pytest.mark.parametrize("strategy_id,invalid", [
    ("rate_of_change_momentum", {"period": 1}),
    ("moving_average_envelope", {"percent": D(0)}),
    ("moving_average_reclaim", {"period": 1}),
    ("triple_moving_average", {"fast_period": 10, "medium_period": 5}),
    ("hull_moving_average", {"period": 3}),
    ("linear_regression_trend", {"period": 1}),
    ("nr7_breakout", {"period": 3}),
    ("keltner_channel_breakout", {"period": 1}),
    ("keltner_channel_breakdown", {"period": 1}),
    ("previous_swing_low_break", {"lookback": 4}),
])
def test_invalid_parameters_are_rejected(strategy_id: str, invalid: dict[str, object]) -> None:
    with pytest.raises(ParameterValidationError):
        REGISTRY.get(strategy_id).required_bars(invalid)


def test_nr4_is_nr7_with_period_four_and_swing_confirmation_is_point_in_time() -> None:
    params, bars = cases()["nr7_breakout"]
    assert params == {"period": 4}
    assert REGISTRY.get("nr7_breakout")().evaluate(context(bars), params,
           state_for("nr7_breakout")).is_signal
    cls = REGISTRY.get("previous_swing_low_break")
    swing = cases()["previous_swing_low_break"][1]
    assert not cls().evaluate(context(swing[:-1]), {"lookback": 5}, {}).is_signal
    assert cls().evaluate(context(swing), {"lookback": 5}, {}).is_signal


def test_representative_buy_and_sell_run_through_backtest(tmp_path: Path) -> None:
    prices = [10] * 25 + [9, 12] + [12] * 20 + [6] * 6
    minute = timedelta(minutes=1)
    provenance = domain_market.FeedProvenance("replay", "IEX", "synthetic", False)
    events = []
    for index, value in enumerate(prices):
        start = OPEN + index * minute
        close = D(value)
        events.append(ReplayEvent.bar(domain_market.Candle(
            "SPY", domain_market.Timeframe.M1, start, start + minute,
            close, close, close, close, D(100000), provenance,
        )))
    clock = ReplayClock(OPEN)
    feed = HistoricalReplayFeed(events, DataProvenance("synthetic", "IEX", False, "r1"),
                                clock=clock, seed=13)
    simulator = HistoricalExecutionSimulator(feed, clock, ExecutionAssumptions())

    def intent(evaluation, market_context, request):  # noqa: ANN001, ARG001
        buying = evaluation.side is StrategyRole.BUY
        return OrderIntent(
            intent_id=f"i-{evaluation.signal_id}", client_order_id=f"c-{evaluation.signal_id}",
            account_scope_id="backtest:test", cycle_id=f"cycle-{evaluation.symbol}",
            symbol=evaluation.symbol, side=OrderSide.BUY if buying else OrderSide.SELL,
            purpose=OrderPurpose.ENTRY if buying else OrderPurpose.STRATEGY_EXIT,
            order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY,
            created_at=market_context.as_of,
            quantity=D(10) if buying else simulator.held_quantity(evaluation.symbol),
        )

    request = BacktestRequest(
        symbols=("SPY",), window_start=OPEN, window_end=OPEN + timedelta(hours=6),
        period_role=PeriodRole.RESEARCH, config={}, seed=13,
        starting_capital=D(100000),
        strategy_ids=("moving_average_reclaim", "awesome_oscillator_reversal"),
        strategy_params={"moving_average_reclaim": {"period": 2}},
        initial_strategy_state={"moving_average_reclaim": {"eligibility": "WATCHING_BUY"}},
        context_history_bars=50, result_directory=tmp_path,
    )
    result = asyncio.run(BacktestRunner(REGISTRY, feed, clock, simulator,
                                       intent_factory=intent).run(request))
    assert any(row["strategy"] == "moving_average_reclaim" for row in result.evaluations)
    assert any(row["strategy"] == "awesome_oscillator_reversal" for row in result.evaluations)
    assert any(fill["side"] == "BUY" for fill in result.fills)
    assert any(fill["side"] == "SELL" for fill in result.fills)
    assert all(row["trigger_bar_end"] <= row["time"] for row in result.signals)
