"""Focused rule, indicator, continuity, and registration tests for SELL batch 5."""

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
from mike_trade.domain.trading import (
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    TimeInForce,
)
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
from mike_trade.strategies.indicators.volume_flow import (
    AccumulationDistributionCalculator,
    OnBalanceVolumeCalculator,
)
from mike_trade.strategies.indicators.windows import is_shooting_star

D = Decimal
OPEN = datetime(2026, 10, 8, 9, 30, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
REGISTRY = _research_registry()
IDS = (
    "accumulation_distribution_breakdown",
    "bearish_momentum_divergence",
    "double_top_reversal",
    "head_and_shoulders_breakdown",
    "ichimoku_bearish_exit",
    "obv_breakdown",
    "shooting_star_reversal",
    "supertrend_bearish_flip",
)
RECURSIVE = ("accumulation_distribution_breakdown", "obv_breakdown", "supertrend_bearish_flip")


def candle(
    index: int,
    close: object,
    *,
    open_: object | None = None,
    high: object | None = None,
    low: object | None = None,
    volume: object = 100,
) -> Candle:
    price = D(str(close))
    start = OPEN + timedelta(minutes=index)
    return Candle(
        "SPY",
        CandleTimeframe.M1,
        start,
        start + timedelta(minutes=1),
        price if open_ is None else D(str(open_)),
        price + D("0.5") if high is None else D(str(high)),
        price - D("0.5") if low is None else D(str(low)),
        price,
        D(str(volume)),
        "sip",
        True,
        CandleOrigin.REPLAY,
        PriceBasis.RAW,
        CandleStatus.CLOSED,
    )


def closes(values: list[object], *, volume: object = 100) -> list[Candle]:
    return [candle(i, value, volume=volume) for i, value in enumerate(values)]


def context(
    series: list[Candle], *, stale: bool = False, session: SessionStatus = SessionStatus.REGULAR
) -> MarketContext:
    return MarketContext(
        symbol="SPY",
        as_of=series[-1].end + timedelta(seconds=1),
        version=len(series),
        feed_id="sip",
        consolidated=True,
        price_basis=PriceBasis.RAW,
        session_status=session,
        session_id="2026-10-08",
        freshness=ContextFreshness(
            series[-1].end,
            timedelta(seconds=1),
            timedelta(minutes=5),
            stale,
            "DATA_TOO_OLD" if stale else None,
        ),
        candles={CandleTimeframe.M1: tuple(series)},
        indicators={},
    )


def double_top() -> list[Candle]:
    values = [60, 70, 80, 90, 95, 92, 85, 88, 94, 98, 100, 95, 90, 87, 86, 83]
    series = closes(values)
    series[4] = candle(4, 95, high=100, low=94)
    series[6] = candle(6, 85, high=86, low=84)
    series[10] = candle(10, 100, high=102, low=99)
    series[-1] = candle(15, 83, high=86, low=82)
    return series


def head_and_shoulders() -> list[Candle]:
    values = [
        60,
        65,
        75,
        68,
        70,
        80,
        85,
        78,
        70,
        88,
        95,
        80,
        72,
        82,
        84,
        80,
        79,
        78,
        D("77.5"),
        77,
        75,
    ]
    series = closes(values)
    series[2] = candle(2, 75, high=80, low=74)
    series[6] = candle(6, 85, high=90, low=84)
    series[8] = candle(8, 70, high=71, low=D("69.5"))
    series[10] = candle(10, 95, high=100, low=94)
    series[12] = candle(12, 72, high=73, low=D("71.5"))
    series[14] = candle(14, 84, high=88, low=83)
    series[-1] = candle(20, 75, high=77, low=74)
    return series


def signal_cases() -> dict[str, tuple[dict[str, object], list[Candle]]]:
    supertrend = closes([20, 19, 18, 17, 16, 15, 14, 15, 17, 18, 19, 15])
    ichimoku = [candle(i, 100, open_=100, high=101, low=99) for i in range(78)]
    ichimoku.append(candle(78, 95, open_=100, high=100, low=94))
    obv = [candle(i, value, volume=100) for i, value in enumerate([10, 11, 12, 11, 10])]
    ad = [candle(i, 11 if i < 3 else 9, open_=10, high=11, low=9, volume=100) for i in range(5)]
    shooting = closes([10, 11, 12])
    shooting.extend(
        [
            candle(3, D("12.1"), open_=12, high=15, low=D("11.9")),
            candle(4, D("11.5"), high=D("11.8"), low=11),
        ]
    )
    divergence = closes([7, 7, 8, 10, 12, 10, 9, 10, 12, D("12.5"), 13, 12])
    divergence[4] = candle(4, 12, high=D("12.5"), low=D("11.5"))
    divergence[10] = candle(10, 13, high=D("13.5"), low=D("12.5"))
    return {
        "supertrend_bearish_flip": ({"atr_period": 3, "multiplier": D(1)}, supertrend),
        "ichimoku_bearish_exit": ({}, ichimoku),
        "obv_breakdown": ({"support_period": 3}, obv),
        "accumulation_distribution_breakdown": ({"support_period": 3}, ad),
        "shooting_star_reversal": ({"trend_bars": 2}, shooting),
        "bearish_momentum_divergence": (
            {"momentum_period": 2, "strength": 1, "lookback": 10},
            divergence,
        ),
        "double_top_reversal": (
            {"strength": 1, "lookback": 15, "peak_tolerance": D("0.03"), "min_decline": D("0.10")},
            double_top(),
        ),
        "head_and_shoulders_breakdown": ({"strength": 1, "lookback": 20}, head_and_shoulders()),
    }


def test_eight_new_sells_registered_in_production_and_research() -> None:
    production_source = inspect.getsource(app.build_runtime)
    assert set(signal_cases()) == set(IDS)
    for strategy_id in IDS:
        cls = REGISTRY.get(strategy_id)
        assert cls.spec.side is StrategyRole.SELL and cls.spec.category == "technical"
        assert cls.spec.source.strip() and cls.spec.completed_bars_only
        assert cls.spec.warmup_bars == cls.required_bars({})
        assert production_source.count(f"{cls.__name__},") == 1
    assert len(REGISTRY.list_strategies(StrategyRole.BUY)) == 93
    assert len(REGISTRY.list_strategies(StrategyRole.SELL)) == 43


@pytest.mark.parametrize("strategy_id", IDS)
def test_signal_non_signal_warmup_replay_and_guards(strategy_id: str) -> None:
    params, series = signal_cases()[strategy_id]
    cls = REGISTRY.get(strategy_id)
    need = cls.required_bars(params)
    assert len(series) >= need
    assert cls().evaluate(context(series[-(need - 1) :]), params, {}).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,
    )
    result = cls().evaluate(context(series), params, {})
    assert result.is_signal, strategy_id
    assert not cls().evaluate(context(series), params, result.state_updates).is_signal
    quiet = list(series)
    latest = quiet[-1]
    if strategy_id == "accumulation_distribution_breakdown":
        quiet[-1] = replace(latest, close=latest.high)
    else:
        quiet[-1] = replace(
            latest,
            open=latest.open + D(10),
            high=latest.high + D(10),
            low=latest.low + D(10),
            close=latest.close + D(10),
        )
    assert not cls().evaluate(context(quiet), params, {}).is_signal
    assert cls().evaluate(context(series, stale=True), params, {}).reason_codes == (
        ReasonCode.STALE_DATA,
    )
    assert cls().evaluate(
        context(series, session=SessionStatus.CLOSED), params, {}
    ).reason_codes == (ReasonCode.NOT_ELIGIBLE,)


@pytest.mark.parametrize("strategy_id", IDS)
def test_missing_latest_observation_cannot_signal(strategy_id: str) -> None:
    params, series = signal_cases()[strategy_id]
    latest = series[-1]
    series[-1] = replace(
        latest, start=latest.start + timedelta(minutes=1), end=latest.end + timedelta(minutes=1)
    )
    assert REGISTRY.get(strategy_id)().evaluate(context(series), params, {}).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,
    )


@pytest.mark.parametrize("strategy_id", IDS)
def test_missing_internal_observation_cannot_define_a_pattern(strategy_id: str) -> None:
    params, series = signal_cases()[strategy_id]
    split = len(series) // 2
    shifted = [
        bar
        if index < split
        else replace(
            bar, start=bar.start + timedelta(minutes=1), end=bar.end + timedelta(minutes=1)
        )
        for index, bar in enumerate(series)
    ]
    assert REGISTRY.get(strategy_id)().evaluate(context(shifted), params, {}).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,
    )


@pytest.mark.parametrize(
    "strategy_id,params",
    [
        ("supertrend_bearish_flip", {"multiplier": D(0)}),
        ("obv_breakdown", {"support_period": 1}),
        ("accumulation_distribution_breakdown", {"support_period": 201}),
        ("shooting_star_reversal", {"shadow_ratio": D("0.5")}),
        ("bearish_momentum_divergence", {"strength": 5, "lookback": 10}),
        ("double_top_reversal", {"strength": 10, "lookback": 15}),
        ("head_and_shoulders_breakdown", {"strength": 10, "lookback": 20}),
        ("ichimoku_bearish_exit", {"period": 9}),
    ],
)
def test_invalid_parameters_are_rejected(strategy_id: str, params: dict[str, object]) -> None:
    with pytest.raises(ParameterValidationError):
        REGISTRY.get(strategy_id)().evaluate(context(closes([100] * 350)), params, {})


def test_volume_flow_math_support_and_snapshot_restore() -> None:
    series = [
        candle(i, close, open_=10, high=11, low=9, volume=volume)
        for i, (close, volume) in enumerate([(10, 10), (11, 20), (11, 30), (9, 40)])
    ]
    obv = OnBalanceVolumeCalculator(2)
    assert [obv.update(bar) for bar in series] == [
        (D(10), None),
        (D(30), None),
        (D(30), D(10)),
        (D(-10), D(30)),
    ]
    obv_clone = OnBalanceVolumeCalculator.restore(obv.snapshot())
    nxt = candle(4, 10, volume=50)
    assert obv_clone.update(nxt) == obv.update(nxt)

    ad = AccumulationDistributionCalculator(2)
    expected = [(D(0), None), (D(20), None), (D(50), D(0)), (D(10), D(20))]
    assert [ad.update(bar) for bar in series] == expected
    flat = candle(4, 10, open_=10, high=10, low=10, volume=999)
    ad_clone = AccumulationDistributionCalculator.restore(ad.snapshot())
    assert ad_clone.update(flat) == ad.update(flat)


def test_shooting_star_shape_boundaries() -> None:
    assert is_shooting_star(candle(0, D("10.1"), open_=10, high=12, low=10), D(2), D("0.1"))
    assert not is_shooting_star(candle(0, D("10.8"), open_=10, high=11, low=10), D(2), D("0.1"))
    assert not is_shooting_star(candle(0, D("10.1"), open_=10, high=12, low=9), D(2), D("0.1"))
    assert not is_shooting_star(candle(0, 10, open_=10, high=10, low=10), D(2), D("0.1"))


@pytest.mark.parametrize("strategy_id", RECURSIVE)
def test_recursive_state_survives_rolling_history_and_json_restart(strategy_id: str) -> None:
    series = [candle(i, 100 + (i % 7) - (i % 3), volume=100 + i) for i in range(90)]
    cls = REGISTRY.get(strategy_id)
    params = (
        {"support_period": 5}
        if "breakdown" in strategy_id
        else {"atr_period": 3, "multiplier": D(1)}
    )
    need = cls.required_bars(params)

    def trace(window: int | None) -> list[tuple[bool, object]]:
        state: dict[str, object] = {}
        output = []
        for end in range(need, len(series) + 1):
            view = series[:end] if window is None else series[max(0, end - window) : end]
            result = cls().evaluate(context(view), params, state)
            state.update(result.state_updates)
            if end == need + 5:
                state = json.loads(json.dumps(state))
            saved = state.get("indicator_state")
            output.append((result.is_signal, saved["latest"] if isinstance(saved, dict) else None))
        return output

    assert trace(None) == trace(need + 2)


def test_obv_breakdown_exits_an_owned_long_in_backtest(tmp_path: Path) -> None:
    prices = [10, 10, 12, 13, 12, 11, 10]
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
        events, DataProvenance("synthetic", "IEX", False, "batch5"), clock=clock, seed=51
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
        seed=51,
        starting_capital=D(100000),
        strategy_ids=("moving_average_reclaim", "obv_breakdown"),
        strategy_params={
            "moving_average_reclaim": {"period": 2},
            "obv_breakdown": {"support_period": 3},
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
    assert any(row["strategy"] == "obv_breakdown" for row in result.signals)
    assert all(row["trigger_bar_end"] <= row["time"] for row in result.signals)
