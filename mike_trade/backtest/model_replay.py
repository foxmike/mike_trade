"""Point-in-time reference-bar history for model inference during a backtest.

Model features need same-minute bars of declared reference symbols (for example SPY).  The
replay feed orders simultaneous events alphabetically by symbol, so a reference bar available
at exactly the same instant as a primary bar may be processed *after* it.  Reading reference
bars from "whatever has been processed so far" would therefore make coverage depend on ticker
spelling.  This adapter instead indexes the reference events by their *availability time* and
answers "what was knowable at ``now``": every bar whose ``available_at <= now``, newest vintage
per bar start.  Nothing available later is ever returned.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from mike_trade.backtest.feed import ReplayEvent
from mike_trade.market_data.candles import (
    Candle as StrategyCandle,
)
from mike_trade.market_data.candles import (
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)

if TYPE_CHECKING:
    from mike_trade.modeling.serving import BarVwapLedger

__all__ = ["ReplayReferenceHistory", "strategy_candle"]


def strategy_candle(event_candle: object) -> StrategyCandle:
    """Adapt a replay (domain) 1-minute candle to the operational completed-candle type."""
    candle = event_candle
    return StrategyCandle(
        candle.symbol,  # type: ignore[attr-defined]
        CandleTimeframe(candle.timeframe.value),  # type: ignore[attr-defined]
        candle.start,  # type: ignore[attr-defined]
        candle.end,  # type: ignore[attr-defined]
        candle.open,  # type: ignore[attr-defined]
        candle.high,  # type: ignore[attr-defined]
        candle.low,  # type: ignore[attr-defined]
        candle.close,  # type: ignore[attr-defined]
        candle.volume,  # type: ignore[attr-defined]
        candle.provenance.feed,  # type: ignore[attr-defined]
        candle.provenance.is_consolidated,  # type: ignore[attr-defined]
        CandleOrigin.REPLAY,
        PriceBasis.RAW,
        CandleStatus.CLOSED,
        candle.trade_count,  # type: ignore[attr-defined]
    )


class ReplayReferenceHistory:
    """``CandleHistory`` over reference-symbol replay events, as knowable at ``now``."""

    def __init__(
        self,
        events: Iterable[ReplayEvent],
        symbols: Iterable[str],
        vwaps: BarVwapLedger,
    ) -> None:
        wanted = {symbol.strip().upper() for symbol in symbols}
        self._vwaps = vwaps
        self._now: datetime | None = None
        self._available: dict[str, list[datetime]] = {symbol: [] for symbol in wanted}
        self._events: dict[str, list[ReplayEvent]] = {symbol: [] for symbol in wanted}
        for event in events:
            if (
                event.kind != ReplayEvent.KIND_BAR
                or event.candle is None
                or event.available_at is None
                or event.symbol not in wanted
                or event.candle.timeframe.value != "1m"
            ):
                continue
            self._available[event.symbol].append(event.available_at)
            self._events[event.symbol].append(event)
        for symbol in wanted:  # availability order is the contract; enforce it
            order = sorted(
                range(len(self._events[symbol])), key=lambda i: self._available[symbol][i]
            )
            self._events[symbol] = [self._events[symbol][i] for i in order]
            self._available[symbol] = [self._available[symbol][i] for i in order]
        self._converted: dict[tuple[str, datetime, datetime], StrategyCandle] = {}

    def advance(self, now: datetime) -> None:
        """Set the instant the next queries are answered at (never moves backwards)."""
        if self._now is not None and now < self._now:
            raise ValueError("reference history cannot move backwards in time")
        self._now = now

    def get_completed_candles(
        self, symbol: str, timeframe: CandleTimeframe, limit: int | None = None
    ) -> Sequence[StrategyCandle]:
        if timeframe is not CandleTimeframe.M1 or self._now is None:
            return ()
        key = symbol.strip().upper()
        available = self._available.get(key)
        if not available:
            return ()
        upper = bisect_right(available, self._now)
        wanted = limit if limit is not None else upper
        chosen: dict[datetime, ReplayEvent] = {}
        for index in range(upper - 1, -1, -1):
            event = self._events[key][index]
            assert event.candle is not None
            if event.candle.start not in chosen:
                chosen[event.candle.start] = event  # newest vintage first
                if len(chosen) >= wanted:
                    break
        result: list[StrategyCandle] = []
        for start in sorted(chosen):
            event = chosen[start]
            assert event.candle is not None and event.available_at is not None
            cache_key = (key, start, event.available_at)
            converted = self._converted.get(cache_key)
            if converted is None:
                converted = strategy_candle(event.candle)
                if len(self._converted) > 8192:
                    self._converted.clear()
                self._converted[cache_key] = converted
            self._vwaps.record(key, start, event.candle.vwap)
            result.append(converted)
        return result
