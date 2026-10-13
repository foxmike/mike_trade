"""Conversions from operational completed candles into model inputs."""

from __future__ import annotations

from decimal import Decimal

from mike_trade.market_data.candles import Candle, CandleStatus, PriceBasis
from mike_trade.modeling.contracts import ModelBar
from mike_trade.scheduling.calendar import NEW_YORK, is_trading_day, session_for


def model_bar_from_candle(
    candle: Candle,
    *,
    vwap: Decimal | None = None,
    session_id: str | None = None,
) -> ModelBar:
    """Adapt an existing live/replay candle only after it is fully completed."""
    if not isinstance(candle, Candle):
        raise TypeError("candle must be mike_trade.market_data.candles.Candle")
    if candle.status is not CandleStatus.CLOSED:
        raise ValueError("incomplete candles cannot produce prediction features")
    if candle.price_basis is not PriceBasis.RAW:
        raise ValueError("model inputs currently require RAW price basis")
    resolved = session_id
    if resolved is None:
        day = candle.start.astimezone(NEW_YORK).date()
        if not is_trading_day(day):
            raise ValueError("candle is outside a supported trading session")
        session = session_for(day)
        if candle.start < session.open_utc or candle.end > session.close_utc:
            raise ValueError("initial intraday features require regular-session candles")
        resolved = session.session_id
    return ModelBar(
        symbol=candle.symbol,
        start=candle.start,
        end=candle.end,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
        feed=candle.feed_id,
        timeframe=candle.timeframe.value,
        adjustment="raw",
        session_id=resolved,
        vwap=vwap,
        trade_count=candle.trade_count,
    )
