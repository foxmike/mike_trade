"""Point-in-time VWAP and first-pullback tests for BUY backlog C."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
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
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.research.commands import _research_registry
from mike_trade.scheduling.calendar import SessionStatus
from mike_trade.strategies.base import ParameterValidationError, ReasonCode
from mike_trade.strategies.registry import StrategyRegistryError

D = Decimal
OPEN = datetime(2026, 10, 8, 13, 30, tzinfo=UTC)
REGISTRY = _research_registry()
ARMED = {"eligibility": "WATCHING_BUY"}
IDS = ("vwap_rejection", "anchored_vwap_breakout", "first_pullback_buy")


def candle(
    i: int,
    close: object,
    *,
    open_: object | None = None,
    high: object | None = None,
    low: object | None = None,
    volume: object = 100,
) -> Candle:
    price = D(str(close))
    start = OPEN + timedelta(minutes=i)
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


def reference_vwaps(bars: list[Candle]) -> list[Decimal]:
    """Independent cumulative weighted typical-price oracle; no production calculator."""
    weighted = D(0)
    volume = D(0)
    output = []
    with localcontext() as arithmetic:
        arithmetic.prec = 28
        for bar in bars:
            weighted += ((bar.high + bar.low + bar.close) / D(3)) * bar.volume
            volume += bar.volume
            output.append(weighted / volume)
    return output


def context(
    bars: list[Candle],
    *,
    session: SessionStatus = SessionStatus.REGULAR,
    stale: bool = False,
    vwap: bool = False,
) -> MarketContext:
    indicators: dict[str, object] = {}
    if vwap:
        values = reference_vwaps(bars)
        indicators = {"vwap": values[-1], "vwap_prev": values[-2]}
    return MarketContext(
        symbol="SPY",
        as_of=bars[-1].end + timedelta(seconds=1),
        version=len(bars),
        feed_id="sip",
        consolidated=True,
        price_basis=PriceBasis.RAW,
        session_status=session,
        session_id=bars[-1].start.date().isoformat(),
        freshness=ContextFreshness(
            bars[-1].end,
            timedelta(seconds=1),
            timedelta(minutes=5),
            stale,
            "DATA_TOO_OLD" if stale else None,
        ),
        candles={CandleTimeframe.M1: tuple(bars)},
        indicators=indicators,
    )


def rejection() -> list[Candle]:
    return [
        candle(0, 100, open_=100, high=101, low=99),
        candle(1, D("101.5"), open_=101, high=102, low=D("100.8")),
        candle(2, D("101.2"), open_=D("100.8"), high=D("101.5"), low=D("100.4")),
    ]


def anchored() -> list[Candle]:
    return [
        candle(0, 10, high=11, low=10),
        candle(1, 9, high=10, low=8),
        candle(2, 10, high=10, low=9),
        candle(3, 9, high=D("9.5"), low=D("8.8")),
        candle(4, 11, high=D("11.5"), low=9),
    ]


def first_pullback() -> list[Candle]:
    return [
        candle(0, D("10.1"), open_=10, high=D("10.2"), low=D("9.9")),
        candle(1, D("10.3"), open_=D("10.1"), high=D("10.4"), low=10),
        candle(2, D("10.6"), open_=D("10.3"), high=D("10.7"), low=D("10.2"), volume=200),
        candle(3, D("10.5"), open_=D("10.6"), high=D("10.65"), low=D("10.4"), volume=50),
        candle(4, D("10.8"), open_=D("10.5"), high=D("10.9"), low=D("10.5")),
    ]


def evaluate(
    strategy_id: str,
    bars: list[Candle],
    params: dict[str, object] | None = None,
    state: dict[str, object] | None = None,
    **options: object,
):
    return REGISTRY.get(strategy_id)().evaluate(
        context(bars, vwap=strategy_id == IDS[0], **options),
        params or {},
        ARMED if state is None else state,
    )


def test_registration_and_equivalence_choices() -> None:
    assert {role: len(REGISTRY.list_strategies(role)) for role in StrategyRole} == {
        StrategyRole.BUY: 87,
        StrategyRole.SELL: 41,
        StrategyRole.SHORT: 28,
        StrategyRole.COVER: 28,
    }
    assert len(REGISTRY.list_strategies()) == 184
    source = inspect.getsource(app.build_runtime)
    for strategy_id in IDS:
        cls = REGISTRY.get(strategy_id)
        assert cls.spec.side is StrategyRole.BUY
        assert cls.spec.warmup_bars == cls.required_bars({})
        assert source.count(f"{cls.__name__},") == 1
    for redundant in (
        "vwap_mean_reversion",
        "vwap_trend_continuation",
        "morning_momentum_continuation",
    ):
        with pytest.raises(StrategyRegistryError):
            REGISTRY.get(redundant)
    symbol = app.SymbolStrategySettings(
        "SPY",
        tuple(app.StrategyAssignment(sid, sid) for sid in IDS),
    )
    assert len(symbol.buys) == 3


def test_all_three_compose_as_per_symbol_shadow_buys(tmp_path: Path) -> None:
    from mike_trade.runtime import ExecutionMode

    settings = app.AppSettings(
        mode=ExecutionMode.SHADOW,
        db_path=tmp_path / "c.sqlite3",
        symbols=("SPY",),
        shadow_scenarios=(app.ShadowScenarioSettings(),),
        symbol_strategies=(
            app.SymbolStrategySettings(
                "SPY",
                tuple(app.StrategyAssignment(sid, sid) for sid in IDS),
            ),
        ),
    )
    buys = app.build_runtime(settings).lifecycle.collections("SPY").buys
    assert {binding.strategy_id for binding in buys} == set(IDS)
    assert {binding.role for binding in buys} == {"buy"}


def test_rejection_uses_historical_vwap_not_current_for_prior() -> None:
    bars = rejection()
    prior, latest = reference_vwaps(bars)[-2:]
    assert bars[-2].low > prior and bars[-1].low < latest < bars[-1].close
    signal = evaluate(IDS[0], bars)
    assert signal.is_signal and signal.bar_time == bars[-1].start
    assert not evaluate(IDS[0], bars, state={**ARMED, **signal.state_updates}).is_signal
    assert not evaluate(IDS[0], [bars[0], bars[-1]]).is_signal
    deep = list(bars)
    deep[-1] = replace(deep[-1], low=D(99))
    assert not evaluate(IDS[0], deep).is_signal
    bearish = list(bars)
    bearish[-1] = replace(bearish[-1], open=D("101.3"))
    assert not evaluate(IDS[0], bearish).is_signal
    assert not evaluate(IDS[0], bars, {"max_penetration_pct": D("0.1")}).is_signal
    assert not REGISTRY.get("vwap_bounce")().evaluate(context(bars, vwap=True), {}, ARMED).is_signal


def test_anchored_low_confirmation_and_vwap_math() -> None:
    bars = anchored()
    params = {"strength": 1}
    confirming = evaluate(IDS[1], bars[:3], params)
    assert not confirming.is_signal
    snapshot = confirming.state_updates["anchor_state"]
    assert snapshot["anchor_time"] == bars[1].start.isoformat()
    assert D(snapshot["last_vwap"]) == reference_vwaps(bars[1:3])[-1]
    next_result = evaluate(
        IDS[1],
        bars[:4],
        params,
        {**ARMED, **json.loads(json.dumps(dict(confirming.state_updates)))},
    )
    assert not next_result.is_signal
    assert (
        D(next_result.state_updates["anchor_state"]["last_vwap"])
        == (reference_vwaps(bars[1:4])[-1])
    )
    assert (
        D(next_result.state_updates["anchor_state"]["last_vwap"]) != (reference_vwaps(bars[:4])[-1])
    )
    restored = {**ARMED, **json.loads(json.dumps(dict(next_result.state_updates)))}
    signal = evaluate(IDS[1], bars, params, restored)
    assert signal.is_signal and signal.bar_time == bars[-1].start
    assert not evaluate(IDS[1], bars, params, {**restored, **signal.state_updates}).is_signal


def test_first_pullback_consumes_first_setup_and_restores() -> None:
    bars = first_pullback()
    lead = evaluate(IDS[2], bars[:3])
    assert lead.state_updates["setup_state"]["phase"] == "ADVANCE"
    setup = evaluate(IDS[2], bars[:4], state={**ARMED, **lead.state_updates})
    assert not setup.is_signal
    assert setup.state_updates["setup_state"]["phase"] == "PULLBACK"
    restored = {**ARMED, **json.loads(json.dumps(dict(setup.state_updates)))}
    signal = evaluate(IDS[2], bars, state=restored)
    assert signal.is_signal and signal.bar_time == bars[-1].start
    assert signal.state_updates["setup_state"]["phase"] == "DONE"
    assert not evaluate(IDS[2], bars, state={**restored, **signal.state_updates}).is_signal
    deep = list(bars)
    deep[3] = replace(deep[3], low=10)
    assert not evaluate(IDS[2], deep).is_signal
    heavy = list(bars)
    heavy[3] = replace(heavy[3], volume=D(300))
    assert not evaluate(IDS[2], heavy).is_signal


@pytest.mark.parametrize(
    "strategy_id,bars,params,prefix",
    [
        (IDS[1], anchored(), {"strength": 1}, 4),
        (IDS[2], first_pullback(), {}, 4),
    ],
)
def test_snapshot_advance_only_short_history_and_session_reset(
    strategy_id: str, bars: list[Candle], params: dict[str, object], prefix: int
) -> None:
    blocked = evaluate(strategy_id, bars[:prefix], params, state={})
    assert blocked.reason_codes == (ReasonCode.NOT_ELIGIBLE,)
    saved = {**ARMED, **json.loads(json.dumps(dict(blocked.state_updates)))}
    assert evaluate(strategy_id, bars[-1:], params, saved).is_signal
    missing = replace(
        bars[-1],
        start=bars[-1].start + timedelta(minutes=1),
        end=bars[-1].end + timedelta(minutes=1),
    )
    assert evaluate(strategy_id, [missing], params, saved).reason_codes == (
        ReasonCode.INSUFFICIENT_DATA,
    )
    next_open = OPEN + timedelta(days=1)
    first_next_day = replace(
        bars[0],
        start=next_open,
        end=next_open + timedelta(minutes=1),
    )
    reset = evaluate(strategy_id, [first_next_day], params, saved)
    assert not reset.is_signal
    key = "anchor_state" if strategy_id == IDS[1] else "setup_state"
    assert reset.state_updates[key]["session"] == "2026-10-09"


def test_corrupt_snapshots_fail_closed_without_complete_history() -> None:
    for strategy_id, bars, params, key in (
        (IDS[1], anchored(), {"strength": 1}, "anchor_state"),
        (IDS[2], first_pullback(), {}, "setup_state"),
    ):
        saved = dict(evaluate(strategy_id, bars[:4], params).state_updates)
        broken = {**saved[key], "last_end": "not-a-date"}
        result = evaluate(strategy_id, bars[-1:], params, {**ARMED, key: broken})
        assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


@pytest.mark.parametrize(
    "strategy_id,bars,params",
    [
        (IDS[0], rejection(), {}),
        (IDS[1], anchored(), {"strength": 1}),
        (IDS[2], first_pullback(), {}),
    ],
)
def test_gaps_stale_session_eligibility_and_warmup(
    strategy_id: str, bars: list[Candle], params: dict[str, object]
) -> None:
    assert evaluate(strategy_id, bars, params).is_signal
    assert evaluate(strategy_id, bars, params, stale=True).reason_codes == (ReasonCode.STALE_DATA,)
    assert evaluate(strategy_id, bars, params, session=SessionStatus.CLOSED).reason_codes == (
        ReasonCode.NOT_ELIGIBLE,
    )
    assert not evaluate(strategy_id, bars, params, state={}).is_signal
    need = REGISTRY.get(strategy_id).required_bars(params)
    if len(bars[: need - 1]) > 1:
        assert not evaluate(strategy_id, bars[: need - 1], params).is_signal
    gap = list(bars)
    gap[-1] = replace(
        gap[-1], start=gap[-1].start + timedelta(minutes=1), end=gap[-1].end + timedelta(minutes=1)
    )
    assert evaluate(strategy_id, gap, params).reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


@pytest.mark.parametrize(
    "strategy_id,params",
    [
        (IDS[0], {"max_penetration_pct": D(6)}),
        (IDS[1], {"strength": 0}),
        (IDS[2], {"impulse_bars": 2}),
        (IDS[2], {"max_retrace": D(1)}),
    ],
)
def test_parameter_bounds(strategy_id: str, params: dict[str, object]) -> None:
    with pytest.raises(ParameterValidationError):
        evaluate(strategy_id, first_pullback(), params)


@pytest.mark.parametrize(
    "strategy_id,bars,params",
    [
        (IDS[1], anchored(), {"strength": 1}),
        (IDS[2], first_pullback(), {}),
    ],
)
def test_direct_signal_matches_backtest(
    strategy_id: str, bars: list[Candle], params: dict[str, object], tmp_path: Path
) -> None:
    assert evaluate(strategy_id, bars, params).is_signal
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
        for bar in bars
    ]
    clock = ReplayClock(OPEN)
    feed = HistoricalReplayFeed(
        events,
        DataProvenance("synthetic", "IEX", False, "buy-batch-c"),
        clock=clock,
        seed=61,
    )
    simulator = HistoricalExecutionSimulator(feed, clock, ExecutionAssumptions())
    request = BacktestRequest(
        symbols=("SPY",),
        window_start=OPEN,
        window_end=OPEN + timedelta(hours=1),
        period_role=PeriodRole.RESEARCH,
        config={},
        seed=61,
        starting_capital=D(100000),
        strategy_ids=(strategy_id,),
        strategy_params={strategy_id: params},
        context_history_bars=50,
        result_directory=tmp_path,
    )
    result = asyncio.run(BacktestRunner(REGISTRY, feed, clock, simulator).run(request))
    matches = [row for row in result.signals if row["strategy"] == strategy_id]
    assert len(matches) == 1
    assert matches[0]["bar_time"] == bars[-1].start
    assert matches[0]["trigger_bar_end"] <= matches[0]["time"]
