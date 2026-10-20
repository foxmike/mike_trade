"""Synthetic one-session fixtures shared by the corrected Michelle tests (not a test module).

The regular-session path is engineered so that the corrected rules see: setup start 09:33,
hammer 09:35 (Pattern 1), three closes below VWAP, confirmation candle 09:36 closing 100.30
(Confirmation_Close) above VWAP, then the entry window C+1 = 09:37.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal as D

from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import (
    ContextFreshness,
    MarketContext,
    QuoteSnapshot,
    TradeSnapshot,
)
from mike_trade.scheduling.calendar import SessionStatus, session_for

DAY = date(2026, 10, 8)
SESSION = session_for(DAY)
OPEN = SESSION.open_utc
MIN = timedelta(minutes=1)
BUY_ID = "vwap_bounce_break_michelle_corrected_buy"
SELL_ID = "vwap_bounce_break_michelle_corrected_sell"


def cd(symbol: str, start: datetime, o: str, h: str, low: str, c: str, v: str = "1000") -> Candle:
    return Candle(
        symbol, CandleTimeframe.M1, start, start + MIN, D(o), D(h), D(low), D(c), D(v), "sip",
        True, CandleOrigin.REPLAY, PriceBasis.RAW, CandleStatus.CLOSED,
    )


def premarket(symbol: str = "ABC", price: str = "99") -> list[Candle]:
    return [cd(symbol, OPEN - (30 - i) * MIN, price, price, price, price, "100") for i in range(30)]


# regular-session path engineered so the setup is: start 09:33, hammer 09:35, confirm 09:36
REGULAR = [
    ("100.0", "101.6", "99.9", "101.4", "20000"),
    ("101.4", "101.5", "100.4", "100.5", "8000"),
    ("100.5", "100.6", "99.6", "99.7", "6000"),
    ("99.7", "99.8", "98.9", "99.0", "6000"),  # 09:33 setup start (red, below VWAP/EMAs)
    ("99.0", "99.1", "98.6", "98.7", "5000"),
    ("98.7", "98.75", "98.2", "98.7", "4000"),  # 09:35 hammer (doji, long lower wick)
    ("98.7", "100.5", "98.6", "100.3", "9000"),  # 09:36 confirmation: green, touches VWAP, closes above
]


def regular_bars(
    extra: tuple[tuple[str, str, str, str, str], ...] | list[tuple[str, str, str, str, str]] = (),
    symbol: str = "ABC",
) -> list[Candle]:
    return [cd(symbol, OPEN + i * MIN, *spec) for i, spec in enumerate([*REGULAR, *extra])]


def qqq_bars(count: int, symbol: str = "QQQ") -> list[Candle]:
    return [
        cd(symbol, OPEN + i * MIN, str(400 + i), str(401 + i), str(399 + i), str(400.6 + i), "5000")
        for i in range(count)
    ]


def ctx(
    candles: list[Candle],
    *,
    eligibility: bool = True,
    developing: Candle | None = None,
    trade: str | None = None,
    qqq: list[Candle] | None = None,
    qqq_price: str | None = None,
    stale: bool = False,
    symbol: str = "ABC",
    as_of: datetime | None = None,
    quote: tuple[str, str] | None = None,
) -> tuple[MarketContext, dict[str, object]]:
    as_of = as_of or (
        candles[-1].end if developing is None else developing.start + timedelta(seconds=20)
    )
    trade_snap = (
        None if trade is None else TradeSnapshot(symbol, D(trade), D("1"), as_of, as_of, "sip")
    )
    quote_snap = (
        None
        if quote is None
        else QuoteSnapshot(symbol, D(quote[0]), D(quote[1]), D("100"), D("100"), as_of, as_of, "sip")
    )
    context = MarketContext(
        symbol=symbol, as_of=as_of, version=max(1, len(candles)), feed_id="sip", consolidated=True,
        price_basis=PriceBasis.RAW, session_status=SessionStatus.REGULAR,
        session_id=SESSION.session_id,
        freshness=ContextFreshness(
            as_of, timedelta(0), timedelta(minutes=2), stale, "DATA_TOO_OLD" if stale else None
        ),
        candles={CandleTimeframe.M1: tuple(premarket(symbol) + candles)},
        indicators={}, latest_trade=trade_snap, latest_quote=quote_snap,
        developing_candles=None if developing is None else {CandleTimeframe.M1: developing},
        completed_bars_only=developing is None,
        reference_candles=None if qqq is None else {"QQQ": tuple(qqq)},
        reference_prices=None if qqq_price is None else {"QQQ": D(qqq_price)},
    )
    return context, ({"eligibility": "WATCHING_BUY"} if eligibility else {})


def developing_candle(minute: int, o: str, h: str, low: str, c: str, symbol: str = "ABC") -> Candle:
    start = OPEN + minute * MIN
    return Candle(
        symbol, CandleTimeframe.M1, start, start + MIN, D(o), D(h), D(low), D(c), D("500"), "sip",
        True, CandleOrigin.STREAM_TRADE, PriceBasis.RAW, CandleStatus.DEVELOPING,
    )


