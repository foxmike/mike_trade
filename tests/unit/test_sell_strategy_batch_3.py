"""Focused signal, guard, registration, and replay tests for SELL batch 3."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mike_trade import app
from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import ExecutionAssumptions, HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.backtest.runner import BacktestRequest, BacktestRunner
from mike_trade.domain import market as domain_market
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import OrderIntent, OrderPurpose, OrderSide, OrderType, TimeInForce
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.research.commands import _research_registry
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import ParameterValidationError, ReasonCode

D = Decimal
OPEN = datetime(2026, 10, 8, 9, 30, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
REGISTRY = _research_registry()
IDS = (
    "adx_trend_weakening",
    "bollinger_upper_band_reversal",
    "failed_breakout_exit",
    "lower_low_confirmation",
    "opening_range_breakdown",
    "stochastic_rsi_reversal",
    "trendline_breakdown",
    "ultimate_oscillator_reversal",
)


def bar(
    index: int, close: str | int, *, low: str | int | None = None, high: str | int | None = None
) -> Candle:
    price = D(close)
    start = OPEN + timedelta(minutes=index)
    return Candle(
        "SPY",
        CandleTimeframe.M1,
        start,
        start + timedelta(minutes=1),
        price,
        D(high) if high is not None else price,
        D(low) if low is not None else price,
        price,
        D(1000),
        "sip",
        True,
        CandleOrigin.REPLAY,
        PriceBasis.RAW,
        CandleStatus.CLOSED,
    )


def bars(
    closes: list[str | int],
    *,
    lows: list[str | int] | None = None,
    highs: list[str | int] | None = None,
) -> list[Candle]:
    return [
        bar(
            index,
            close,
            low=None if lows is None else lows[index],
            high=None if highs is None else highs[index],
        )
        for index, close in enumerate(closes)
    ]


def context(series: list[Candle]) -> MarketContext:
    as_of = series[-1].end + timedelta(seconds=1)
    return MarketContext(
        symbol="SPY",
        as_of=as_of,
        version=len(series),
        feed_id="sip",
        consolidated=True,
        price_basis=PriceBasis.RAW,
        session_status=SessionStatus.REGULAR,
        session_id="2026-10-08",
        freshness=ContextFreshness(
            series[-1].end, timedelta(seconds=1), timedelta(minutes=5), False, None
        ),
        candles={CandleTimeframe.M1: tuple(series)},
        indicators={},
    )


def signal_cases() -> dict[str, tuple[dict[str, object], list[Candle]]]:
    return {
        "adx_trend_weakening": (
            {"period": 2, "threshold": D(80)},
            bars([10, 12, 14, 16, 15], lows=[9, 11, 13, 15, 14], highs=[11, 13, 15, 17, 16]),
        ),
        "bollinger_upper_band_reversal": (
            {"period": 2, "deviations": D("0.5")},
            bars([10, 20, 15]),
        ),
        "failed_breakout_exit": (
            {"period": 2},
            bars([9, 10, 12, 10], highs=[10, 11, 12, 10]),
        ),
        "lower_low_confirmation": (
            {"lookback": 7},
            bars(
                [10, 9, 6, 9, 8, 5, 8, 9],
                lows=[10, 9, 5, 9, 8, 4, 8, 9],
                highs=[10, 9, 7, 9, 8, 6, 8, 9],
            ),
        ),
        "opening_range_breakdown": (
            {"opening_range_minutes": 2},
            bars([10, 9, 9, "8.5"], lows=["9.5", 9, "8.8", 8], highs=["10.5", 10, "9.5", 9]),
        ),
        "stochastic_rsi_reversal": (
            {"rsi_period": 2, "stochastic_period": 2, "overbought": D(80)},
            bars([10, 11, 10, 12, 11]),
        ),
        "trendline_breakdown": (
            {"lookback": 7},
            bars(
                [4, 3, 2, 3, 4, 3, 4, "2.5"],
                lows=[4, 3, 1, 3, 4, 2, 4, "2.4"],
                highs=[4, 3, 3, 3, 4, 4, 4, 3],
            ),
        ),
        "ultimate_oscillator_reversal": (
            {"short_period": 2, "medium_period": 3, "long_period": 4, "overbought": D(50)},
            bars(
                [10, 11, 12, 13, 14, 10],
                lows=[9, 10, 11, 12, 13, 9],
                highs=[11, 12, 13, 14, 15, 11],
            ),
        ),
    }


def test_all_eight_are_registered_in_production_and_research() -> None:
    source = inspect.getsource(app.build_runtime)
    assert set(signal_cases()) == set(IDS)
    for strategy_id in IDS:
        cls = REGISTRY.get(strategy_id)
        assert cls.spec.side is StrategyRole.SELL
        assert cls.spec.category == "technical"
        assert cls.spec.source.strip()
        assert cls.spec.warmup_bars == cls.required_bars({})
        assert source.count(f"{cls.__name__},") == 1


@pytest.mark.parametrize("strategy_id", IDS)
def test_signal_warmup_replay_and_missing_latest_bar(strategy_id: str) -> None:
    params, series = signal_cases()[strategy_id]
    cls = REGISTRY.get(strategy_id)
    need = cls.required_bars(params)
    assert len(series) == need
    assert cls().evaluate(context(series[:-1]), params, {}).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,
    )

    evaluation = cls().evaluate(context(series), params, {})
    assert evaluation.is_signal, strategy_id
    assert not cls().evaluate(context(series), params, evaluation.state_updates).is_signal

    latest = series[-1]
    gapped = [
        *series[:-1],
        replace(
            latest, start=latest.start + timedelta(minutes=1), end=latest.end + timedelta(minutes=1)
        ),
    ]
    assert cls().evaluate(context(gapped), params, {}).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,
    )


def test_representative_non_signal_and_parameter_boundaries() -> None:
    cls = REGISTRY.get("failed_breakout_exit")
    assert (
        not cls()
        .evaluate(context(bars([9, 10, 11, 10], highs=[10, 11, 11, 10])), {"period": 2}, {})
        .is_signal
    )
    with pytest.raises(ParameterValidationError):
        REGISTRY.get("ultimate_oscillator_reversal").required_bars(
            {"short_period": 4, "medium_period": 3, "long_period": 5}
        )
    with pytest.raises(ParameterValidationError):
        REGISTRY.get("stochastic_rsi_reversal").required_bars({"overbought": D(100)})
    with pytest.raises(ParameterValidationError):
        REGISTRY.get("opening_range_breakdown").required_bars({"opening_range_minutes": 0})


@pytest.mark.parametrize(
    "strategy_id,params",
    [
        ("adx_trend_weakening", {"period": 2, "threshold": D(50)}),
        (
            "stochastic_rsi_reversal",
            {"rsi_period": 2, "stochastic_period": 2, "overbought": D(80)},
        ),
    ],
)
def test_recursive_results_survive_rolling_history_and_json_restart(
    strategy_id: str, params: dict[str, object]
) -> None:
    closes = [10, 12, 14, 16, 15, 17, 14, 18, 13, 17, 12, 16, 11, 15]
    series = bars(
        closes, lows=[value - 1 for value in closes], highs=[value + 1 for value in closes]
    )
    cls = REGISTRY.get(strategy_id)
    need = cls.required_bars(params)
    full_state: dict[str, object] = {}
    rolling_state: dict[str, object] = {}
    for end in range(need, len(series) + 1):
        full = cls().evaluate(context(series[:end]), params, full_state)
        rolling = cls().evaluate(context(series[max(0, end - need) : end]), params, rolling_state)
        assert rolling.is_signal == full.is_signal
        full_state.update(full.state_updates)
        rolling_state.update(rolling.state_updates)
        if end == need + 2:
            rolling_state = json.loads(json.dumps(rolling_state))


def test_one_new_sell_exits_a_position_in_the_real_backtest(tmp_path: Path) -> None:
    prices = [10, 10, 9, 12, 8, 8]
    minute = timedelta(minutes=1)
    provenance = domain_market.FeedProvenance("replay", "IEX", "synthetic", False)
    events = []
    for index, value in enumerate(prices):
        start = OPEN + index * minute
        price = D(value)
        events.append(
            ReplayEvent.bar(
                domain_market.Candle(
                    "SPY",
                    domain_market.Timeframe.M1,
                    start,
                    start + minute,
                    price,
                    price,
                    price,
                    price,
                    D(100000),
                    provenance,
                )
            )
        )
    clock = ReplayClock(OPEN)
    feed = HistoricalReplayFeed(
        events, DataProvenance("synthetic", "IEX", False, "batch3"), clock=clock, seed=31
    )
    simulator = HistoricalExecutionSimulator(feed, clock, ExecutionAssumptions())

    def intent(evaluation, market_context, request):  # noqa: ANN001, ARG001
        buying = evaluation.side is StrategyRole.BUY
        return OrderIntent(
            intent_id=f"i-{evaluation.signal_id}",
            client_order_id=f"c-{evaluation.signal_id}",
            account_scope_id="backtest:test",
            cycle_id="cycle-SPY",
            symbol="SPY",
            side=OrderSide.BUY if buying else OrderSide.SELL,
            purpose=OrderPurpose.ENTRY if buying else OrderPurpose.STRATEGY_EXIT,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            created_at=market_context.as_of,
            quantity=D(10) if buying else simulator.held_quantity("SPY"),
        )

    request = BacktestRequest(
        symbols=("SPY",),
        window_start=OPEN,
        window_end=OPEN + timedelta(hours=1),
        period_role=PeriodRole.RESEARCH,
        config={},
        seed=31,
        starting_capital=D(100000),
        strategy_ids=("moving_average_reclaim", "failed_breakout_exit"),
        strategy_params={
            "moving_average_reclaim": {"period": 2},
            "failed_breakout_exit": {"period": 2},
        },
        initial_strategy_state={"moving_average_reclaim": {"eligibility": "WATCHING_BUY"}},
        context_history_bars=20,
        result_directory=tmp_path,
    )
    result = asyncio.run(
        BacktestRunner(REGISTRY, feed, clock, simulator, intent_factory=intent).run(request)
    )
    assert any(fill["side"] == "BUY" for fill in result.fills)
    assert any(fill["side"] == "SELL" for fill in result.fills)
    assert any(row["strategy"] == "failed_breakout_exit" for row in result.signals)
    assert all(row["trigger_bar_end"] <= row["time"] for row in result.signals)
