"""Focused tests for gap and previous-session-reference BUY strategies."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.backtest.runner import BacktestRequest, BacktestRunner
from mike_trade.domain.market import Candle as ReplayCandle
from mike_trade.domain.market import FeedProvenance, Timeframe
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.market_data.session_reference import RegularSessionOHLC, SessionReference
from mike_trade.research.commands import _research_registry
from mike_trade.scheduling.calendar import SessionStatus, previous_session, session_for
from mike_trade.strategies.base import ParameterValidationError, ReasonCode
from mike_trade.strategies.buy.gap_and_go import GapAndGoBuy
from mike_trade.strategies.buy.gap_fill import GapFillBuy
from mike_trade.strategies.buy.gap_reversal import GapReversalBuy
from mike_trade.strategies.buy.previous_close_reclaim import PreviousCloseReclaimBuy
from mike_trade.strategies.buy.previous_day_high_breakout import PreviousDayHighBreakoutBuy
from mike_trade.strategies.buy.previous_day_low_reversal import PreviousDayLowReversalBuy
from mike_trade.strategies.registry import StrategyRegistry

D = Decimal
DAY = date(2026, 10, 9)
WATCHING = {"eligibility": "WATCHING_BUY"}


def bar(
    index: int,
    *,
    open_: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
    volume: str = "1000",
) -> Candle:
    start = session_for(DAY).open_utc + timedelta(minutes=index)
    return Candle(
        symbol="SPY",
        timeframe=CandleTimeframe.M1,
        start=start,
        end=start + timedelta(minutes=1),
        open=D(open_),
        high=D(high),
        low=D(low),
        close=D(close),
        volume=D(volume),
        feed_id="sip",
        consolidated=True,
        origin=CandleOrigin.REPLAY,
        price_basis=PriceBasis.RAW,
        status=CandleStatus.CLOSED,
    )


def prior_reference(
    *,
    current_open: str,
    prior_open: str = "99",
    high: str = "105",
    low: str = "95",
    close: str = "100",
) -> SessionReference:
    current = session_for(DAY)
    prior = previous_session(DAY)
    return SessionReference(
        current_session_id=current.session_id,
        previous_session_id=prior.session_id,
        current_open=D(current_open),
        previous=RegularSessionOHLC(
            session_id=prior.session_id,
            open=D(prior_open),
            high=D(high),
            low=D(low),
            close=D(close),
            open_time=prior.open_utc,
            close_time=prior.close_utc,
        ),
    )


def context(
    bars: list[Candle],
    reference: SessionReference | None,
    *,
    vwap: str | None = None,
) -> MarketContext:
    as_of = bars[-1].end
    return MarketContext(
        symbol="SPY",
        as_of=as_of,
        version=len(bars),
        feed_id="sip",
        consolidated=True,
        price_basis=PriceBasis.RAW,
        session_status=SessionStatus.REGULAR,
        session_id=session_for(DAY).session_id,
        freshness=ContextFreshness(
            newest_data_ts=as_of,
            age=timedelta(0),
            max_age=timedelta(minutes=5),
            stale=False,
            reason=None,
        ),
        candles={CandleTimeframe.M1: tuple(bars)},
        indicators={"vwap": None if vwap is None else D(vwap)},
        session_reference=reference,
    )


def test_gap_and_go_breaks_above_one_minute_opening_range() -> None:
    bars = [
        bar(0, open_="105", high="106", low="104.5", close="105.5"),
        bar(1, open_="105.5", high="106.4", low="105.4", close="106.2"),
    ]
    result = GapAndGoBuy().evaluate(
        context(bars, prior_reference(current_open="105")), {}, WATCHING
    )
    assert result.is_signal and result.reference_price == D("106.2")
    assert not GapAndGoBuy().evaluate(
        context(bars, prior_reference(current_open="105")),
        {"min_gap_percent": D("10")},
        WATCHING,
    ).is_signal


def test_gap_fill_requires_gap_down_and_session_open_reclaim() -> None:
    bars = [
        bar(0, open_="98", high="98.1", low="97.5", close="97.8"),
        bar(1, open_="97.8", high="98.4", low="97.7", close="98.2"),
    ]
    assert GapFillBuy().evaluate(
        context(bars, prior_reference(current_open="98")), {}, WATCHING
    ).is_signal
    assert not GapFillBuy().evaluate(
        context(bars, prior_reference(current_open="101")), {}, WATCHING
    ).is_signal


def test_gap_reversal_requires_opening_range_break_and_vwap_recovery() -> None:
    opening = [
        bar(index, open_="97", high="99", low="96", close="98")
        for index in range(5)
    ]
    trigger = bar(5, open_="98", high="99.4", low="97.8", close="99.2")
    ctx = context(
        opening + [trigger], prior_reference(current_open="97"), vwap="98.8"
    )
    assert GapReversalBuy().evaluate(
        ctx, {"opening_range_minutes": 5}, WATCHING
    ).is_signal
    no_vwap = context(
        opening + [trigger], prior_reference(current_open="97"), vwap="99.3"
    )
    assert not GapReversalBuy().evaluate(
        no_vwap, {"opening_range_minutes": 5}, WATCHING
    ).is_signal


@pytest.mark.parametrize(
    ("strategy", "bars", "reference"),
    [
        (
            PreviousDayHighBreakoutBuy(),
            [bar(0, high="105", close="104.8"), bar(1, high="105.4", close="105.2")],
            prior_reference(current_open="100"),
        ),
        (
            PreviousDayLowReversalBuy(),
            [bar(0, open_="96", high="96.2", low="94.8", close="95.2")],
            prior_reference(current_open="96"),
        ),
    ],
)
def test_previous_session_level_entries(strategy, bars, reference) -> None:
    assert strategy.evaluate(context(bars, reference), {}, WATCHING).is_signal


def test_previous_close_reclaim_is_confirmed_red_to_green_move() -> None:
    bars = [
        bar(index, open_="99", high="99.8", low="98.8", close="99.5")
        for index in range(30)
    ]
    bars.append(
        bar(
            30,
            open_="99.5",
            high="100.4",
            low="99.4",
            close="100.2",
            volume="1600",
        )
    )
    ctx = context(bars, prior_reference(current_open="99"), vwap="99.8")
    assert PreviousCloseReclaimBuy().evaluate(ctx, {}, WATCHING).is_signal
    assert not PreviousCloseReclaimBuy().evaluate(
        ctx, {"volume_multiplier": D("2")}, WATCHING
    ).is_signal


def test_previous_day_low_reversal_suppresses_same_bar_replay() -> None:
    bars = [bar(0, open_="96", high="96.2", low="94.8", close="95.2")]
    strategy = PreviousDayLowReversalBuy()
    ctx = context(bars, prior_reference(current_open="96"))
    first = strategy.evaluate(ctx, {}, WATCHING)
    assert first.is_signal
    assert not strategy.evaluate(ctx, {}, {**WATCHING, **first.state_updates}).is_signal


@pytest.mark.parametrize(
    "strategy",
    [
        GapAndGoBuy(),
        GapFillBuy(),
        GapReversalBuy(),
        PreviousCloseReclaimBuy(),
        PreviousDayHighBreakoutBuy(),
        PreviousDayLowReversalBuy(),
    ],
)
def test_missing_previous_session_reference_is_insufficient(strategy) -> None:
    bars = [bar(0), bar(1)]
    result = strategy.evaluate(context(bars, None, vwap="100"), {}, WATCHING)
    assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_gap_parameters_validate_and_required_bars_follow_range() -> None:
    assert GapAndGoBuy.required_bars({"opening_range_minutes": 7}) == 8
    assert GapReversalBuy.required_bars({"opening_range_minutes": 20}) == 21
    with pytest.raises(ParameterValidationError):
        GapFillBuy().evaluate(
            context([bar(0), bar(1)], prior_reference(current_open="98")),
            {"min_gap_percent": D("-1")},
            WATCHING,
        )
    invalid = (
        (GapAndGoBuy(), {"opening_range_minutes": 0}),
        (GapReversalBuy(), {"opening_range_minutes": 4}),
        (PreviousCloseReclaimBuy(), {"volume_multiplier": D("0.9")}),
    )
    for strategy, params in invalid:
        with pytest.raises(ParameterValidationError):
            strategy.evaluate(
                context([bar(0), bar(1)], prior_reference(current_open="98"), vwap="100"),
                params,
                WATCHING,
            )


def test_opening_range_gap_methods_reject_a_partial_current_session() -> None:
    missing_open = [bar(index) for index in range(1, 7)]
    reference = prior_reference(current_open="105")
    for strategy, params in (
        (GapAndGoBuy(), {}),
        (GapReversalBuy(), {"opening_range_minutes": 5}),
    ):
        result = strategy.evaluate(
            context(missing_open, reference, vwap="100"), params, WATCHING
        )
        assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_six_reference_strategies_are_registered_in_production_and_research() -> None:
    import inspect

    from mike_trade import app

    expected = {
        "gap_and_go",
        "gap_fill",
        "gap_reversal",
        "previous_close_reclaim",
        "previous_day_high_breakout",
        "previous_day_low_reversal",
    }
    registry = _research_registry()
    for strategy_id in expected:
        cls = registry.get(strategy_id)
        assert cls.spec.side is StrategyRole.BUY
        assert cls.spec.source
        assert inspect.getsource(app.build_runtime).count(f"{cls.__name__},") == 1


@pytest.mark.asyncio
async def test_backtest_replay_builds_the_same_point_in_time_gap_reference(
    tmp_path: Path,
) -> None:
    prior = session_for(previous_session(DAY).session_date)
    current = session_for(DAY)
    provenance = FeedProvenance("replay", "IEX", "synthetic", False)
    events: list[ReplayEvent] = []
    count = int((prior.close_utc - prior.open_utc) / timedelta(minutes=1))
    for index in range(count):
        start = prior.open_utc + timedelta(minutes=index)
        events.append(
            ReplayEvent.bar(
                ReplayCandle(
                    "SPY",
                    Timeframe.M1,
                    start,
                    start + timedelta(minutes=1),
                    D("100"),
                    D("101"),
                    D("99"),
                    D("100"),
                    D("1000"),
                    provenance,
                )
            )
        )
    current_bars = (
        ("105", "106", "104.5", "105.5"),
        ("105.5", "106.4", "105.4", "106.2"),
    )
    for index, values in enumerate(current_bars):
        start = current.open_utc + timedelta(minutes=index)
        events.append(
            ReplayEvent.bar(
                ReplayCandle(
                    "SPY",
                    Timeframe.M1,
                    start,
                    start + timedelta(minutes=1),
                    *(D(value) for value in values),
                    D("1000"),
                    provenance,
                )
            )
        )

    clock = ReplayClock(prior.open_utc)
    feed = HistoricalReplayFeed(
        events,
        DataProvenance("synthetic", "IEX", False, "session-reference-test"),
        clock=clock,
        seed=7,
    )
    execution = HistoricalExecutionSimulator(feed, clock)
    registry = StrategyRegistry()
    registry.register(GapAndGoBuy)
    request = BacktestRequest(
        symbols=("SPY",),
        window_start=prior.open_utc,
        window_end=current.open_utc + timedelta(minutes=2),
        period_role=PeriodRole.RESEARCH,
        config={},
        seed=7,
        starting_capital=D("10000"),
        strategy_ids=("gap_and_go",),
        context_history_bars=500,
        result_directory=tmp_path,
    )
    result = await BacktestRunner(registry, feed, clock, execution).run(request)
    assert [row["strategy"] for row in result.signals] == ["gap_and_go"]
    assert result.signals[0]["bar_time"] == current.open_utc + timedelta(minutes=1)
