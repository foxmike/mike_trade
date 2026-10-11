"""Focused correctness checks for current-session BUY strategies."""

from __future__ import annotations

from datetime import UTC, date, time, timedelta
from decimal import Decimal

import pytest

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
from mike_trade.scheduling.calendar import NEW_YORK, SessionStatus, session_for
from mike_trade.strategies.base import ParameterValidationError, ReasonCode
from mike_trade.strategies.buy.afternoon_breakout import AfternoonBreakoutBuy
from mike_trade.strategies.buy.high_of_day_momentum import HighOfDayMomentumBuy
from mike_trade.strategies.buy.midday_range_breakout import MiddayRangeBreakoutBuy
from mike_trade.strategies.buy.opening_drive import OpeningDriveBuy
from mike_trade.strategies.buy.opening_range_retest import OpeningRangeRetestBuy
from mike_trade.strategies.buy.power_hour_momentum import PowerHourMomentumBuy

D = Decimal
WATCHING = {"eligibility": "WATCHING_BUY"}
DAY = date(2026, 10, 8)


def bar(day: date, index: int, *, open_: str = "100", high: str = "101",
        low: str = "99", close: str = "100") -> Candle:
    start = session_for(day).open_utc + timedelta(minutes=index)
    return Candle(
        symbol="SPY", timeframe=CandleTimeframe.M1, start=start,
        end=start + timedelta(minutes=1), open=D(open_), high=D(high), low=D(low),
        close=D(close), volume=D("1000"), feed_id="sip", consolidated=True,
        origin=CandleOrigin.REPLAY, price_basis=PriceBasis.RAW, status=CandleStatus.CLOSED,
    )


def series(count: int, *, day: date = DAY,
           overrides: dict[int, dict[str, str]] | None = None) -> list[Candle]:
    changes = overrides or {}
    return [bar(day, index, **changes.get(index, {})) for index in range(count)]


def context(bars: list[Candle], *, stale: bool = False) -> MarketContext:
    as_of = bars[-1].end + timedelta(seconds=1)
    session = session_for(bars[-1].start.astimezone(NEW_YORK).date())
    return MarketContext(
        symbol="SPY", as_of=as_of, version=len(bars), feed_id="sip", consolidated=True,
        price_basis=PriceBasis.RAW, session_status=SessionStatus.REGULAR,
        session_id=session.session_id,
        freshness=ContextFreshness(
            newest_data_ts=bars[-1].end, age=as_of - bars[-1].end,
            max_age=timedelta(minutes=5), stale=stale,
            reason="DATA_TOO_OLD" if stale else None,
        ), candles={CandleTimeframe.M1: tuple(bars)}, indicators={},
    )


def test_opening_drive_signals_only_at_completed_drive_window() -> None:
    bars = series(5, overrides={
        0: {"open_": "100", "high": "101", "low": "99.8", "close": "100.5"},
        1: {"open_": "100.4", "high": "101.2", "low": "100", "close": "100.8"},
        2: {"open_": "100.7", "high": "101.4", "low": "100.2", "close": "101"},
        3: {"open_": "100.9", "high": "101.6", "low": "100.4", "close": "101.2"},
        4: {"open_": "101.1", "high": "101.8", "low": "100.6", "close": "101.5"},
    })
    result = OpeningDriveBuy().evaluate(context(bars), {}, WATCHING)
    assert result.is_signal and result.bar_time == bars[-1].start
    assert not OpeningDriveBuy().evaluate(context(bars + [bar(DAY, 5)]), {}, WATCHING).is_signal


@pytest.mark.parametrize("day", [date(2026, 3, 6), date(2026, 3, 9)])
def test_opening_drive_uses_exchange_open_across_dst(day: date) -> None:
    bars = series(2, day=day, overrides={
        0: {"open_": "100", "high": "101", "low": "99.9", "close": "100.5"},
        1: {"open_": "100.4", "high": "101.2", "low": "100", "close": "101"},
    })
    assert OpeningDriveBuy().evaluate(
        context(bars), {"drive_minutes": 2}, WATCHING
    ).is_signal
    expected_hour = 14 if day == date(2026, 3, 6) else 13
    assert bars[0].start.astimezone(UTC).hour == expected_hour


def test_opening_range_retest_waits_for_breakout_then_first_retest() -> None:
    opening = series(15)
    breakout = bar(DAY, 15, open_="100.5", high="102.5", low="100.4", close="102")
    assert not OpeningRangeRetestBuy().evaluate(
        context(opening + [breakout]), {}, WATCHING
    ).is_signal
    retest = bar(DAY, 16, open_="102", high="102.2", low="100.8", close="101.5")
    result = OpeningRangeRetestBuy().evaluate(
        context(opening + [breakout, retest]), {}, WATCHING
    )
    assert result.is_signal and result.reference_price == D("101.5")


def test_opening_range_failed_first_retest_cannot_signal_later() -> None:
    bars = series(15) + [
        bar(DAY, 15, open_="100.5", high="102.5", low="100.4", close="102"),
        bar(DAY, 16, open_="102", high="102", low="99.5", close="100"),
        bar(DAY, 17, open_="100", high="102", low="100", close="101.5"),
    ]
    assert not OpeningRangeRetestBuy().evaluate(context(bars), {}, WATCHING).is_signal


def test_high_of_day_uses_only_highs_before_trigger_bar() -> None:
    bars = series(31, overrides={
        30: {"open_": "100", "high": "102.5", "low": "99.8", "close": "102"},
    })
    assert HighOfDayMomentumBuy().evaluate(context(bars), {}, WATCHING).is_signal
    # A wick above the old HOD whose completed close remains below it is not a signal.
    bars[-1] = bar(DAY, 30, open_="100", high="103", low="99.8", close="100.5")
    assert not HighOfDayMomentumBuy().evaluate(context(bars), {}, WATCHING).is_signal


def test_fixed_session_ranges_emit_on_completed_close_crosses() -> None:
    midday = series(211, overrides={
        210: {"open_": "100", "high": "102.5", "low": "99.8", "close": "102"},
    })
    assert MiddayRangeBreakoutBuy().evaluate(context(midday), {}, WATCHING).is_signal
    assert AfternoonBreakoutBuy().evaluate(context(midday), {}, WATCHING).is_signal

    power = series(331, overrides={
        329: {"open_": "100", "high": "101.2", "low": "99", "close": "101"},
        330: {"open_": "101", "high": "102.5", "low": "100.8", "close": "102"},
    })
    assert PowerHourMomentumBuy().evaluate(context(power), {}, WATCHING).is_signal


def test_missing_open_or_missing_minute_fails_closed() -> None:
    full = series(211)
    missing_open = full[1:]
    missing_midday = full[:175] + full[176:]
    for bars in (missing_open, missing_midday):
        result = MiddayRangeBreakoutBuy().evaluate(context(bars), {}, WATCHING)
        assert not result.is_signal
        assert result.reason_codes == (ReasonCode.INSUFFICIENT_DATA,)


def test_guards_replay_and_parameter_validation() -> None:
    bars = series(31, overrides={
        30: {"open_": "100", "high": "102.5", "low": "99.8", "close": "102"},
    })
    strategy = HighOfDayMomentumBuy()
    signal = strategy.evaluate(context(bars), {}, WATCHING)
    replay_state = {**WATCHING, **signal.state_updates}
    assert not strategy.evaluate(context(bars), {}, replay_state).is_signal
    assert strategy.evaluate(context(bars, stale=True), {}, WATCHING).reason_codes == (
        ReasonCode.STALE_DATA,
    )
    with pytest.raises(ParameterValidationError):
        strategy.evaluate(
            context(bars), {"entry_start": time(15), "entry_end": time(14)}, WATCHING
        )


def test_six_session_strategies_are_registered_for_buy() -> None:
    import inspect

    from mike_trade import app

    registry = _research_registry()
    expected = {
        "opening_drive", "opening_range_retest", "high_of_day_momentum",
        "midday_range_breakout", "afternoon_breakout", "power_hour_momentum",
    }
    for strategy_id in expected:
        cls = registry.get(strategy_id)
        assert cls.spec.side is StrategyRole.BUY
        assert cls.spec.category == "technical"
        assert cls.spec.source
        assert inspect.getsource(app.build_runtime).count(f"{cls.__name__},") == 1
