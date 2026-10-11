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
            bars([9, 10, 10, 12, 9], highs=[10, 10, 10, 12, 10]),
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
            bars([10, 9, "8.5"], lows=["9.5", 9, 8], highs=["10.5", 10, 9]),
        ),
        "stochastic_rsi_reversal": (
            {"rsi_period": 2, "stochastic_period": 2, "overbought": D(80)},
            bars([10, 11, 10, 12, 11]),
        ),
        "trendline_breakdown": (
            {"lookback": 8},
            bars(
                [4, 3, 2, 3, 4, 3, 4, 4, "2.5"],
                lows=[4, 3, 1, 3, 4, 2, 4, 4, "2.4"],
                highs=[4, 3, 3, 3, 4, 4, 4, 4, 3],
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
        .evaluate(
            context(bars([9, 10, 10, 11, 10], highs=[10, 10, 11, 11, 10])), {"period": 2}, {}
        )
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
    with pytest.raises(ParameterValidationError):
        REGISTRY.get("trendline_breakdown").required_bars({"lookback": 7})


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


def test_opening_range_breakdown_first_post_range_bar_and_later_bar() -> None:
    cls = REGISTRY.get("opening_range_breakdown")
    # 2-minute range: bars 0 (09:30) and 1 (09:31).
    # Range low is min(9.5, 9.0) = 9.0.
    # 1. Breakdown on the very first post-range bar at 09:32:
    first_post_breakdown = bars(
        [10, 9, "8.5"], lows=["9.5", 9, 8], highs=["10.5", 10, 9]
    )
    eval_first = cls().evaluate(context(first_post_breakdown), {"opening_range_minutes": 2}, {})
    assert eval_first.is_signal

    # 2. Normal later breakdown:
    # Bar 2 at 09:32 stays inside the range (close 9.2 >= 9.0).
    # Bar 3 at 09:33 breaks down below 9.0 (close 8.7 < 9.0).
    later_breakdown = bars(
        [10, 9, "9.2", "8.7"],
        lows=["9.5", 9, "9.0", "8.5"],
        highs=["10.5", 10, "9.5", "9.0"],
    )
    eval_later = cls().evaluate(context(later_breakdown), {"opening_range_minutes": 2}, {})
    assert eval_later.is_signal

    # 3. Bar still inside the opening range cannot generate a signal:
    inside_range = bars([10, "8.5"], lows=["9.5", 8], highs=["10.5", 9])
    eval_inside = cls().evaluate(context(inside_range), {"opening_range_minutes": 2}, {})
    assert not eval_inside.is_signal
    assert eval_inside.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_trendline_breakdown_unconfirmed_trendline_cannot_generate_signal() -> None:
    cls = REGISTRY.get("trendline_breakdown")
    # In this 8-bar sequence, pivot 1 is at index 2 (low 1).
    # Potential pivot 2 is at index 5 (low 2).
    # However, index 5 requires two completed right-hand bars (index 6 and index 7).
    # At index 7 (the breakdown bar), index 5 is only confirmed BY index 7.
    # Prior to index 7, at index 6, pivot 2 was unconfirmed and no rising trendline existed.
    unconfirmed = bars(
        [4, 3, 2, 3, 4, 3, 4, "2.5"],
        lows=[4, 3, 1, 3, 4, 2, 4, "2.4"],
        highs=[4, 3, 3, 3, 4, 4, 4, 3],
    )
    eval_unconfirmed = cls().evaluate(context(unconfirmed), {"lookback": 8}, {})
    assert not eval_unconfirmed.is_signal

    # When both pivots are confirmed prior to the crossover comparison (9 bars):
    # Pivot 2 (index 5) is confirmed when index 7 completes.
    # At index 7 (bars[-2]), the trendline is fully established.
    # At index 8 (bars[-1]), price breaks below the established trendline.
    confirmed = bars(
        [4, 3, 2, 3, 4, 3, 4, 4, "2.5"],
        lows=[4, 3, 1, 3, 4, 2, 4, 4, "2.4"],
        highs=[4, 3, 3, 3, 4, 4, 4, 4, 3],
    )
    eval_confirmed = cls().evaluate(context(confirmed), {"lookback": 8}, {})
    assert eval_confirmed.is_signal


def test_adx_trend_weakening_zero_directional_movement_continuity_and_determinism() -> None:
    from mike_trade.strategies.sell.adx_trend_weakening import _ADXCalculator

    calc = _ADXCalculator(period=2)
    # Feed 4 flat bars (no directional movement: +DM = 0, -DM = 0)
    # Period is 2, so:
    # Bar 0: prev set
    # Bar 1: DMI seed 1
    # Bar 2: DMI seed 2 -> DMI produces +DI=0, -DI=0. ADX seed 1 = 0
    # Bar 3: DMI produces +DI=0, -DI=0. ADX seed 2 = 0 -> ADX initializes to Decimal(0)
    calc.update(bar(0, 10, low=9, high=10))
    calc.update(bar(1, 10, low=9, high=10))
    calc.update(bar(2, 10, low=9, high=10))
    plus3, minus3, adx3 = calc.update(bar(3, 10, low=9, high=10))
    assert plus3 == D(0)
    assert minus3 == D(0)
    assert adx3 == D(0)

    # Bar 4: zero directional movement continues after ADX initialization (+DI + -DI == 0)
    plus4, minus4, adx4 = calc.update(bar(4, 10, low=9, high=10))
    assert plus4 == D(0)
    assert minus4 == D(0)
    # ADX must NOT be None; continuity is preserved (under old buggy code this returned None)
    assert adx4 == D(0)

    # Now verify with an initialized non-zero ADX value
    calc._value = D("50")
    # Feed Bar 5 with zero directional movement
    plus5, minus5, adx5 = calc.update(bar(5, 10, low=9, high=10))
    assert plus5 == D(0)
    assert minus5 == D(0)
    # Wilder smoothing with dx=0: (50 * (2 - 1) + 0) / 2 = 25
    assert adx5 == D("25")

    # Determinism across snapshot / restore
    snapshot = calc.snapshot()
    restored = _ADXCalculator.restore(snapshot)
    assert restored._value == calc._value

    next_bar = bar(6, 12, low=11, high=13)
    _, _, adx_orig = calc.update(next_bar)
    _, _, adx_rest = restored.update(next_bar)
    assert adx_orig == adx_rest


def test_failed_breakout_exit_distinguishes_genuine_failure_from_ordinary_pullback() -> None:
    cls = REGISTRY.get("failed_breakout_exit")
    # 1. Genuine failed breakout:
    # Channel bars 0 & 1: high = 10.
    # Bar 2 (bars[-3]): close 10 <= 10 (no prior breakout).
    # Bar 3 (bars[-2]): close 12 > 10 (fresh breakout).
    # Bar 4 (bars[-1]): close 9 < 10 (immediate failure back below 10).
    genuine_failure = bars(
        [9, 10, 10, 12, 9],
        highs=[10, 10, 10, 12, 10],
    )
    eval_genuine = cls().evaluate(context(genuine_failure), {"period": 2}, {})
    assert eval_genuine.is_signal

    # 2. Ordinary pullback after an established breakout:
    # Channel bars 0 & 1: high = 10.
    # Bar 2 (bars[-3]): close 12 > 10 (breakout already occurred here).
    # Bar 3 (bars[-2]): close 14 > 10 (continuation higher, not a fresh breakout).
    # Bar 4 (bars[-1]): close 11 (ordinary pullback; still above original breakout level 10).
    ordinary_pullback = bars(
        [9, 10, 12, 14, 11],
        highs=[10, 10, 12, 14, 13],
    )
    eval_pullback = cls().evaluate(context(ordinary_pullback), {"period": 2}, {})
    assert not eval_pullback.is_signal


def test_lower_low_confirmation_methodology_clarification() -> None:
    cls = REGISTRY.get("lower_low_confirmation")
    assert (
        "formed two bars earlier is confirmed on the latest completed bar"
        in cls.spec.methodology
    )

