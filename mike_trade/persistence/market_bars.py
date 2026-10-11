"""Idempotent persistence for completed operational market bars."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from mike_trade.market_data.candles import Candle, CandleStatus
from mike_trade.persistence.schema import decimal_to_db, to_db_timestamp
from mike_trade.persistence.transaction import ScopedWriter

__all__ = ["CompletedMarketBar", "MarketBarRepository"]


@dataclass(frozen=True, slots=True)
class CompletedMarketBar:
    """A completed candle ready for the operational ``market_bars`` table."""

    symbol: str
    timeframe: str
    bar_start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal | None
    feed: str
    provenance: str

    @classmethod
    def from_candle(
        cls, candle: Candle, *, vwap: Decimal | None = None
    ) -> CompletedMarketBar:
        if not isinstance(candle, Candle):
            raise TypeError("candle must be a Candle")
        if candle.status is not CandleStatus.CLOSED:
            raise ValueError("only completed candles may be persisted")
        return cls(
            symbol=candle.symbol,
            timeframe=candle.timeframe.value,
            bar_start=candle.start,
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            vwap=vwap,
            feed=candle.feed_id,
            provenance=f"{candle.origin.value}:{candle.price_basis.value}",
        )


class MarketBarRepository:
    """Batch completed bars through the process's account-scoped writer."""

    def __init__(self, writer: ScopedWriter) -> None:
        self._writer = writer

    def append_completed(
        self, bars: Iterable[CompletedMarketBar], *, recorded_at: datetime
    ) -> int:
        rows = tuple(bars)
        if not rows:
            return 0
        if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        before = self._writer.connection.total_changes
        with self._writer.unit_of_work() as conn:
            conn.executemany(
                "INSERT INTO market_bars("
                "account_scope_id,symbol,timeframe,bar_start,open_price,high_price,"
                "low_price,close_price,vwap,volume,feed,provenance,recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account_scope_id,symbol,timeframe,bar_start,feed) DO NOTHING",
                (
                    (
                        self._writer.account_scope_id,
                        bar.symbol,
                        bar.timeframe,
                        to_db_timestamp(bar.bar_start),
                        decimal_to_db(bar.open),
                        decimal_to_db(bar.high),
                        decimal_to_db(bar.low),
                        decimal_to_db(bar.close),
                        None if bar.vwap is None else decimal_to_db(bar.vwap),
                        decimal_to_db(bar.volume),
                        bar.feed,
                        bar.provenance,
                        to_db_timestamp(recorded_at.astimezone(UTC)),
                    )
                    for bar in rows
                ),
            )
        return int(self._writer.connection.total_changes - before)
