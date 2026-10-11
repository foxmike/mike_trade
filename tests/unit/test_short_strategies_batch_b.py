"""Causal signals, eligibility and registration for SHORT Batch B."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from mike_trade import app
from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import ExecutionAssumptions, HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.backtest.runner import BacktestRequest, BacktestRunner
from mike_trade.domain import market as domain_market
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, ContextIntegrityError, MarketContext
from mike_trade.research.commands import _research_registry
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import ParameterValidationError, ReasonCode, ShortStrategy

D = Decimal
OPEN = datetime(2026, 10, 8, 13, 30, tzinfo=UTC)
ARMED = {"eligibility": "WATCHING_SHORT"}
REGISTRY = _research_registry()
IDS = (
    "short_bearish_engulfing",
    "short_shooting_star_reversal",
    "short_double_top_breakdown",
    "short_head_and_shoulders_breakdown",
    "short_trendline_breakdown",
    "short_previous_swing_low_break",
    "short_lower_low_confirmation",
    "short_failed_breakout_reversal",
    "short_bear_flag_breakdown",
    "short_bearish_momentum_divergence",
)


def candle(
    index: int,
    close: object,
    *,
    open_: object | None = None,
    high: object | None = None,
    low: object | None = None,
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
        D(100),
        "sip",
        True,
        CandleOrigin.REPLAY,
        PriceBasis.RAW,
        CandleStatus.CLOSED,
    )


def closes(values: list[object]) -> list[Candle]:
    return [candle(i, value) for i, value in enumerate(values)]


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


def examples() -> dict[str, tuple[dict[str, object], list[Candle]]]:
    engulf = closes([10, 11, 12, 13, 11])
    engulf[3] = candle(3, 13, open_=12, high=14, low=11)
    engulf[4] = candle(4, 11, open_=14, high=14, low=10)
    shooting = closes([10, 11, 12])
    shooting += [
        candle(3, D("12.1"), open_=12, high=15, low=D("11.9")),
        candle(4, D("11.5"), high=D("11.8"), low=11),
    ]
    double = closes([60, 70, 80, 90, 95, 92, 85, 88, 94, 98, 100, 95, 90, 87, 86, 83])
    double[4] = candle(4, 95, high=100, low=94)
    double[6] = candle(6, 85, high=86, low=84)
    double[10] = candle(10, 100, high=102, low=99)
    double[-1] = candle(15, 83, high=86, low=82)
    shoulders = closes(
        [
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
    )
    shoulders[2] = candle(2, 75, high=80, low=74)
    shoulders[6] = candle(6, 85, high=90, low=84)
    shoulders[8] = candle(8, 70, high=71, low=D("69.5"))
    shoulders[10] = candle(10, 95, high=100, low=94)
    shoulders[12] = candle(12, 72, high=73, low=D("71.5"))
    shoulders[14] = candle(14, 84, high=88, low=83)
    shoulders[-1] = candle(20, 75, high=77, low=74)
    trend = closes([4, 3, 2, 3, 4, 3, 4, 4, D("2.5")])
    trend_lows = [4, 3, 1, 3, 4, 2, 4, 4, D("2.4")]
    trend_highs = [4, 3, 3, 3, 4, 4, 4, 4, 3]
    trend = [
        candle(i, bar.close, high=trend_highs[i], low=trend_lows[i]) for i, bar in enumerate(trend)
    ]
    lower = closes([10, 9, 6, 9, 8, 5, 8, 9])
    lower_lows = [10, 9, 5, 9, 8, 4, 8, 9]
    lower_highs = [10, 9, 7, 9, 8, 6, 8, 9]
    lower = [
        candle(i, bar.close, high=lower_highs[i], low=lower_lows[i]) for i, bar in enumerate(lower)
    ]
    previous = closes([10, 9, 7, 9, 10, 6])
    previous = [
        candle(i, bar.close, high=bar.close, low=bar.close) for i, bar in enumerate(previous)
    ]
    failed = closes([10, 10, 12, 9])
    failed[2] = candle(2, 12, high=12, low=11)
    flag = closes([100] * 15 + [98, 95, 90, D("90.5"), 91, D("91.5"), D("89.8")])
    divergence = closes([7, 7, 8, 10, 12, 10, 9, 10, 12, D("12.5"), 13, 12])
    divergence[4] = candle(4, 12, high=D("12.5"), low=D("11.5"))
    divergence[10] = candle(10, 13, high=D("13.5"), low=D("12.5"))
    return {
        IDS[0]: ({"trend_bars": 3}, engulf),
        IDS[1]: ({"trend_bars": 2}, shooting),
        IDS[2]: ({"strength": 1, "lookback": 15}, double),
        IDS[3]: ({"strength": 1, "lookback": 20}, shoulders),
        IDS[4]: ({"lookback": 8}, trend),
        IDS[5]: ({"lookback": 5}, previous),
        IDS[6]: ({"lookback": 7}, lower),
        IDS[7]: ({"period": 2}, failed),
        IDS[8]: ({"lookback": 21, "pole_bars": 5, "min_flag_bars": 3, "max_flag_bars": 5}, flag),
        IDS[9]: ({"momentum_period": 2, "strength": 1, "lookback": 10}, divergence),
    }


def evaluate(
    strategy_id: str,
    series: list[Candle],
    params: dict[str, object],
    state: dict[str, object] | None = None,
    **options: object,
):
    return REGISTRY.get(strategy_id)().evaluate(
        context(series, **options),
        params,
        ARMED if state is None else state,
    )


def test_registration_and_contract() -> None:
    assert set(examples()) == set(IDS)
    assert {role: len(REGISTRY.list_strategies(role)) for role in StrategyRole} == {
        StrategyRole.BUY: 75,
        StrategyRole.SELL: 40,
        StrategyRole.SHORT: 28,
        StrategyRole.COVER: 3,
    }
    assert len(REGISTRY.list_strategies()) == 146
    source = inspect.getsource(app.build_runtime)
    for strategy_id in IDS:
        cls = REGISTRY.get(strategy_id)
        assert issubclass(cls, ShortStrategy)
        assert cls.spec.side is StrategyRole.SHORT
        assert cls.spec.warmup_bars == cls.required_bars({})
        assert cls.spec.completed_bars_only and cls.spec.source
        assert source.count(f"{cls.__name__},") == 1


def test_short_only_symbol_configuration_and_completed_candles() -> None:
    assignments = tuple(app.StrategyAssignment(sid, sid) for sid in IDS)
    symbol = app.SymbolStrategySettings("spy", (), shorts=assignments)
    assert symbol.symbol == "SPY" and len(symbol.shorts) == 10
    for assignment in symbol.shorts:
        assert REGISTRY.validate_parameters(assignment.plugin, assignment.parameters)
    params, series = examples()[IDS[7]]
    assert evaluate(IDS[7], series, params).is_signal
    with pytest.raises(ContextIntegrityError):
        context([*series[:-1], replace(series[-1], status=CandleStatus.DEVELOPING)])


@pytest.mark.parametrize("strategy_id", IDS)
def test_signal_edge_history_guards_and_restart(strategy_id: str) -> None:
    params, series = examples()[strategy_id]
    cls = REGISTRY.get(strategy_id)
    result = evaluate(strategy_id, series, params)
    assert result.is_signal, strategy_id
    assert result.side is StrategyRole.SHORT
    assert result.bar_time == series[-1].start
    state = json.loads(json.dumps({**ARMED, **result.state_updates}))
    assert not evaluate(strategy_id, series, params, state).is_signal
    assert not evaluate(strategy_id, series, params, {}).is_signal
    assert evaluate(strategy_id, series, params, stale=True).reason_codes == (
        ReasonCode.STALE_DATA,
    )
    assert evaluate(strategy_id, series, params, session=SessionStatus.CLOSED).reason_codes == (
        ReasonCode.NOT_ELIGIBLE,
    )
    need = cls.required_bars(params)
    assert len(series) >= need
    assert evaluate(strategy_id, series[-need + 1 :], params).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,
    )
    gap = list(series)
    gap[-1] = replace(
        gap[-1], start=gap[-1].start + timedelta(minutes=1), end=gap[-1].end + timedelta(minutes=1)
    )
    assert evaluate(strategy_id, gap, params).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


@pytest.mark.parametrize("strategy_id", IDS)
def test_no_early_confirmation_or_persistent_bearish_retrigger(strategy_id: str) -> None:
    params, series = examples()[strategy_id]
    cls = REGISTRY.get(strategy_id)
    prior = series[:-1]
    if len(prior) >= cls.required_bars(params):
        assert not evaluate(strategy_id, prior, params).is_signal
    quiet = list(series)
    latest = quiet[-1]
    if strategy_id == "short_lower_low_confirmation":
        quiet[-1] = candle(len(quiet) - 1, 3, high=4, low=2)
    else:
        quiet[-1] = replace(
            latest,
            open=latest.open + D(20),
            high=latest.high + D(20),
            low=latest.low + D(20),
            close=latest.close + D(20),
        )
    assert not evaluate(strategy_id, quiet, params).is_signal


@pytest.mark.parametrize("strategy_id", IDS)
def test_missing_internal_bar_invalidates_entire_pattern(strategy_id: str) -> None:
    params, series = examples()[strategy_id]
    midpoint = len(series) // 2
    gap = [
        bar
        if i < midpoint
        else replace(
            bar,
            start=bar.start + timedelta(minutes=1),
            end=bar.end + timedelta(minutes=1),
        )
        for i, bar in enumerate(series)
    ]
    assert evaluate(strategy_id, gap, params).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_reversal_lookalikes_are_rejected() -> None:
    engulf_params, engulf = examples()[IDS[0]]
    no_advance = list(engulf)
    no_advance[0] = candle(0, 15)
    assert not evaluate(IDS[0], no_advance, engulf_params).is_signal

    shooting_params, shooting = examples()[IDS[1]]
    no_confirmation = list(shooting)
    no_confirmation[-1] = candle(4, 12, high=12, low=11)
    assert not evaluate(IDS[1], no_confirmation, shooting_params).is_signal

    double_params, double = examples()[IDS[2]]
    unconfirmed_trough = list(double)
    unconfirmed_trough[7] = candle(7, 82, high=88, low=82)
    assert not evaluate(IDS[2], unconfirmed_trough, double_params).is_signal

    divergence_params, divergence = examples()[IDS[9]]
    no_divergence = list(divergence)
    no_divergence[10] = candle(10, 16, high=D("16.5"), low=D("15.5"))
    assert not evaluate(IDS[9], no_divergence, divergence_params).is_signal


@pytest.mark.parametrize(
    "short_id,sell_id",
    [
        (IDS[0], "bearish_engulfing_exit"),
        (IDS[1], "shooting_star_reversal"),
        (IDS[2], "double_top_reversal"),
        (IDS[3], "head_and_shoulders_breakdown"),
        (IDS[4], "trendline_breakdown"),
        (IDS[5], "previous_swing_low_break"),
        (IDS[6], "lower_low_confirmation"),
        (IDS[9], "bearish_momentum_divergence"),
    ],
)
def test_matching_sell_reference_calculation(short_id: str, sell_id: str) -> None:
    params, series = examples()[short_id]
    sell_params = {key: value for key, value in params.items() if key != "trend_bars"}
    if short_id == IDS[1]:
        sell_params = params
    assert REGISTRY.get(sell_id)().evaluate(context(series), sell_params, {}).is_signal
    assert evaluate(short_id, series, params).is_signal


@pytest.mark.parametrize(
    "strategy_id,params",
    [
        (IDS[0], {"trend_bars": 1}),
        (IDS[1], {"shadow_ratio": D("0.5")}),
        (IDS[2], {"strength": 10, "lookback": 15}),
        (IDS[3], {"strength": 10, "lookback": 20}),
        (IDS[4], {"lookback": 7}),
        (IDS[5], {"lookback": 4}),
        (IDS[6], {"lookback": 6}),
        (IDS[7], {"period": 1}),
        (IDS[8], {"max_flag_bars": 60, "lookback": 20}),
        (IDS[9], {"strength": 10, "lookback": 10}),
    ],
)
def test_bad_parameters_rejected(strategy_id: str, params: dict[str, object]) -> None:
    with pytest.raises(ParameterValidationError):
        evaluate(strategy_id, closes([100] * 350), params)


def test_failed_breakout_uses_immediately_preceding_reference() -> None:
    params = {"period": 2}
    breakout = closes([20, 10, 10, 12, 9])
    assert evaluate(IDS[7], breakout, params).is_signal
    assert not evaluate(IDS[7], breakout[:-1], params).is_signal
    equality = closes([10, 10, 12, D("10.5")])
    equality[-1] = candle(3, D("10.5"), high=D("10.5"), low=10)
    assert not evaluate(IDS[7], equality, params).is_signal


def test_bear_flag_rejects_flat_or_deep_correction() -> None:
    params, series = examples()[IDS[8]]
    flat = list(series)
    flat[20] = candle(20, flat[18].close, high=91, low=90)
    assert not evaluate(IDS[8], flat, params).is_signal
    deep = list(series)
    deep[19] = replace(deep[19], high=D(98))
    assert not evaluate(IDS[8], deep, params).is_signal


@pytest.mark.parametrize("strategy_id", (IDS[0], IDS[7]))
def test_direct_signal_matches_historical_replay(strategy_id: str, tmp_path: Path) -> None:
    params, series = examples()[strategy_id]
    assert evaluate(strategy_id, series, params).is_signal
    provenance = domain_market.FeedProvenance("replay", "IEX", "synthetic", False)
    events = [
        ReplayEvent.bar(
            domain_market.Candle(
                "SPY",
                domain_market.Timeframe.M1,
                bar.start,
                bar.end,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                provenance,
            )
        )
        for bar in series
    ]
    clock = ReplayClock(OPEN)
    feed = HistoricalReplayFeed(
        events,
        DataProvenance("synthetic", "IEX", False, "short-batch-b"),
        clock=clock,
        seed=73,
    )
    simulator = HistoricalExecutionSimulator(feed, clock, ExecutionAssumptions())
    request = BacktestRequest(
        symbols=("SPY",),
        window_start=OPEN,
        window_end=OPEN + timedelta(hours=1),
        period_role=PeriodRole.RESEARCH,
        config={"short_selling_enabled": True},
        seed=73,
        starting_capital=D(100000),
        strategy_ids=(strategy_id,),
        strategy_params={strategy_id: params},
        context_history_bars=30,
        result_directory=tmp_path,
    )
    result = asyncio.run(BacktestRunner(REGISTRY, feed, clock, simulator).run(request))
    matches = [row for row in result.signals if row["strategy"] == strategy_id]
    assert len(matches) == 1
    assert matches[0]["bar_time"] == series[-1].start
    assert matches[0]["trigger_bar_end"] <= matches[0]["time"]
