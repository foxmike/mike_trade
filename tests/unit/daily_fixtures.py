"""Synthetic daily-history fixtures with hand-controlled geometry (no network, no randomness).

Every candle is a real ``D1`` ``Candle`` on the NYSE calendar (holidays skipped, early closes
honoured by the candle's own span).  Highs/lows are ``close * (1 +/- WICK)`` so every expected
swing high, low, depth and pivot can be written down by hand in the tests.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal

from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.scheduling.calendar import previous_session, session_for

D = Decimal
WICK = D("0.005")
FEED = "iex"
SYMBOL = "SPY"
#: the last settled session of every fixture, and the session in which M1 breakouts are tested
LAST_DAILY = date(2026, 10, 8)
TODAY = date(2026, 10, 9)


def sessions_ending(count: int, last: date = LAST_DAILY) -> list[date]:
    """``count`` consecutive trading sessions ending with ``last`` (oldest first)."""
    days = [last]
    while len(days) < count:
        days.append(previous_session(days[-1]).session_date)
    return days[::-1]


def path(points: Sequence[tuple[int, str | int | float]]) -> list[Decimal]:
    """Piecewise-linear values through ``(index, value)`` corner points (index 0 first)."""
    out: list[Decimal] = []
    for (i0, v0), (i1, v1) in zip(points, points[1:], strict=False):
        a, b = D(str(v0)), D(str(v1))
        out += [a + (b - a) * D(i - i0) / D(i1 - i0) for i in range(i0, i1)]
    out.append(D(str(points[-1][1])))
    return out


def daily_candle(
    day: date,
    close: Decimal,
    *,
    open_: Decimal | None = None,
    high: Decimal | None = None,
    low: Decimal | None = None,
    volume: int | Decimal = 1000,
    symbol: str = SYMBOL,
    feed: str = FEED,
) -> Candle:
    session = session_for(day)
    o = close if open_ is None else open_
    h = max(o, close) * (1 + WICK) if high is None else high
    lo = min(o, close) * (1 - WICK) if low is None else low
    return Candle(
        symbol, CandleTimeframe.D1, session.open_utc, session.close_utc,
        o, max(h, o, close), min(lo, o, close), close, D(volume), feed, feed != "iex",
        CandleOrigin.HISTORICAL_REST, PriceBasis.RAW, CandleStatus.CLOSED,
    )


def daily_series(
    closes: Sequence[Decimal],
    *,
    volumes: Sequence[int | Decimal] | None = None,
    last: date = LAST_DAILY,
    symbol: str = SYMBOL,
    feed: str = FEED,
) -> list[Candle]:
    """D1 candles for consecutive sessions ending at ``last``; open = close, high/low = close +/- wick.

    (open == close keeps every high and low a pure function of that day's close, so swing pivots
    and depths can be written down by hand.)
    """
    days = sessions_ending(len(closes), last)
    return [
        daily_candle(day, close, symbol=symbol, feed=feed,
                     volume=1000 if volumes is None else volumes[i])
        for i, (day, close) in enumerate(zip(days, closes, strict=True))
    ]


def minute_candles(
    closes: Sequence[Decimal | str | float],
    *,
    day: date = TODAY,
    start_index: int = 0,
    volume: int | Decimal = 100,
    symbol: str = SYMBOL,
    feed: str = FEED,
) -> list[Candle]:
    """Consecutive regular-session M1 candles from the open (open = previous close)."""
    session = session_for(day)
    out: list[Candle] = []
    previous = D(str(closes[0]))
    for i, raw in enumerate(closes):
        close = D(str(raw))
        start = session.open_utc + timedelta(minutes=start_index + i)
        out.append(Candle(
            symbol, CandleTimeframe.M1, start, start + timedelta(minutes=1), previous,
            max(previous, close) + D("0.01"), min(previous, close) - D("0.01"), close,
            D(volume), feed, feed != "iex", CandleOrigin.REPLAY, PriceBasis.RAW,
            CandleStatus.CLOSED,
        ))
        previous = close
    return out


def vcp_closes() -> list[Decimal]:
    """260 sessions: a 50 -> 100 advance (Stage 2), then a base of three shrinking pullbacks.

    Swing highs (close): 100 @185, 98 @205, 97.5 @222.  Lows: 80 @193, 90 @210, 94.5 @226.  The base
    ends at 96.5, 1.5% under the pivot (high of the 97.5 close: 97.9875).
    """
    return path([(0, 50), (185, 100), (193, 80), (205, 98), (210, 90), (222, "97.5"),
                 (226, "94.5"), (259, "96.5")])


def vcp_volumes() -> list[int]:
    return [1000] * 250 + [700] * 10


def cup_closes() -> list[Decimal]:
    """260 sessions: flat 50, an advance to 100 @110+76, a parabolic cup (22% deep, 60 sessions),
    then a 14-session handle that dips to 94 and drifts back to 96.8."""
    lead = [D(50)] * 76
    rise = path([(0, 50), (110, 100)])[:-1]
    cup = [D(100) - D(22) * (1 - ((D(i) - 30) / 30) ** 2) for i in range(0, 61)]
    handle = [D(x) for x in ("99", "97.5", "96", "95", "94.5", "94", "94.2", "94.6", "95",
                             "95.4", "95.8", "96.2", "96.5", "96.8")]
    return lead + rise + cup + handle


def cup_volumes(count: int) -> list[int]:
    return [1000] * (count - 14) + [600] * 14


def fingerprint(candles: Sequence[Candle]) -> tuple[tuple[datetime, Decimal], ...]:
    return tuple((c.start, c.close) for c in candles)
