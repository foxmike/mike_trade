"""Immutable market-data value objects: events, candles, and context.

Standard library only (dataclasses, Enum, decimal, datetime, MappingProxyType).
Imports ``MarketSession``/``SessionKind`` from the sibling ``instruments``
module only; never broker SDKs, adapters, strategy code, or third-party
packages. Numeric/datetime validation mirrors ``instruments`` via private
helpers so this module has no other runtime ``mike_trade`` dependencies
besides ``instruments``.

Design choices (3):
1. ``Timeframe`` members are named M1/M5/M15/H1/D1 with string values
   ("1m", "5m", "15m", "1h", "1d") and expose a ``duration`` timedelta.
2. ``MarketContext`` validates that every candle's symbol matches the context
   symbol, every candle's timeframe matches its mapping key, and each tuple
   is in ascending, non-overlapping start order; mappings are defensively
   copied into ``MappingProxyType`` so callers can never mutate context state.
3. ``BAR`` market events only validate provided numerics (price > 0 when
   present, others >= 0); the bid<=ask cross-check applies whenever both bid
   and ask are present and positive, regardless of kind.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType

from mike_trade.domain.instruments import MarketSession, SessionKind

__all__ = [
    "Timeframe",
    "FeedProvenance",
    "MarketEventKind",
    "MarketEvent",
    "Candle",
    "DataFreshness",
    "MarketContext",
]


def _to_decimal(value: Decimal | int | str, *, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field} must be Decimal, int, or str; got {type(value).__name__}")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field} must be a non-empty numeric string")
        try:
            result = Decimal(text)
        except InvalidOperation:
            raise ValueError(f"{field} is not a valid decimal string: {value!r}") from None
    else:
        raise ValueError(f"{field} must be Decimal, int, or str; got {type(value).__name__}")
    if result.is_nan() or result.is_infinite():
        raise ValueError(f"{field} must be finite; got {result!s}")
    return result


def _opt_decimal(value: Decimal | int | str | None, *, field: str) -> Decimal | None:
    if value is None:
        return None
    return _to_decimal(value, field=field)


def _require_utc(ts: datetime, *, field: str) -> datetime:
    if not isinstance(ts, datetime):
        raise ValueError(f"{field} must be a datetime; got {type(ts).__name__}")
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return ts.astimezone(UTC)


def _require_symbol(value: object, *, field: str) -> str:
    import re as _re

    if not isinstance(value, str):
        raise ValueError(f"{field} must be str; got {type(value).__name__}")
    normalized = value.strip().upper()
    if _re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", normalized) is None:
        raise ValueError(f"{field} is invalid: {value!r}")
    return normalized


class Timeframe(Enum):
    """Supported candle timeframes with exact bar durations."""

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    D1 = "1d"

    @property
    def duration(self) -> timedelta:
        """Return the exact bar length for this timeframe."""
        return {
            Timeframe.M1: timedelta(minutes=1),
            Timeframe.M5: timedelta(minutes=5),
            Timeframe.M15: timedelta(minutes=15),
            Timeframe.H1: timedelta(hours=1),
            Timeframe.D1: timedelta(days=1),
        }[self]


@dataclass(frozen=True, slots=True)
class FeedProvenance:
    """Where a market data point came from.

    Alpaca Basic IEX is represented with ``is_consolidated=False`` so IEX
    provenance is never mistaken for consolidated SIP data.
    """

    provider: str
    feed: str
    source: str
    is_consolidated: bool

    def __post_init__(self) -> None:
        for field in ("provider", "feed", "source"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a non-empty str")
            object.__setattr__(self, field, value.strip())
        if not isinstance(self.is_consolidated, bool):
            raise ValueError("is_consolidated must be bool")


class MarketEventKind(Enum):
    """Kinds of streaming market events."""

    TRADE = "TRADE"
    QUOTE = "QUOTE"
    BAR = "BAR"


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """One immutable trade, quote, or bar event with feed provenance."""

    symbol: str
    kind: MarketEventKind
    event_time: datetime
    received_at: datetime
    provenance: FeedProvenance
    price: Decimal | None = None
    size: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _require_symbol(self.symbol, field="symbol"))
        if not isinstance(self.kind, MarketEventKind):
            raise ValueError(f"kind must be MarketEventKind; got {type(self.kind).__name__}")
        object.__setattr__(self, "event_time", _require_utc(self.event_time, field="event_time"))
        object.__setattr__(self, "received_at", _require_utc(self.received_at, field="received_at"))
        if not isinstance(self.provenance, FeedProvenance):
            raise ValueError("provenance must be FeedProvenance")
        price = _opt_decimal(self.price, field="price")
        size = _opt_decimal(self.size, field="size")
        bid = _opt_decimal(self.bid, field="bid")
        ask = _opt_decimal(self.ask, field="ask")
        bid_size = _opt_decimal(self.bid_size, field="bid_size")
        ask_size = _opt_decimal(self.ask_size, field="ask_size")
        for label, val in (("price", price), ("size", size), ("bid", bid),
                           ("ask", ask), ("bid_size", bid_size), ("ask_size", ask_size)):
            object.__setattr__(self, label, val)
        if self.kind is MarketEventKind.TRADE:
            if price is None or price <= 0:
                raise ValueError("TRADE events require price > 0")
            if size is None or size < 0:
                raise ValueError("TRADE events require size >= 0")
        elif self.kind is MarketEventKind.QUOTE:
            if bid is None or bid < 0:
                raise ValueError("QUOTE events require bid >= 0")
            if ask is None or ask < 0:
                raise ValueError("QUOTE events require ask >= 0")
        else:  # BAR
            if price is not None and price <= 0:
                raise ValueError("BAR price must be > 0 when provided")
            for label, val in (("size", size), ("bid", bid), ("ask", ask),
                               ("bid_size", bid_size), ("ask_size", ask_size)):
                if val is not None and val < 0:
                    raise ValueError(f"BAR {label} must be >= 0 when provided")
        if bid is not None and ask is not None and bid > 0 and ask > 0 and bid > ask:
            raise ValueError(f"bid ({bid!s}) must be <= ask ({ask!s}) when both are positive")


@dataclass(frozen=True, slots=True)
class Candle:
    """One immutable OHLCV candle with feed provenance."""

    symbol: str
    timeframe: Timeframe
    start: datetime
    end: datetime
    open: Decimal  # noqa: A003 - domain uses OHLC field names
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    provenance: FeedProvenance
    vwap: Decimal | None = None
    trade_count: int | None = None
    closed: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _require_symbol(self.symbol, field="symbol"))
        if not isinstance(self.timeframe, Timeframe):
            raise ValueError(f"timeframe must be Timeframe; got {type(self.timeframe).__name__}")
        start = _require_utc(self.start, field="start")
        end = _require_utc(self.end, field="end")
        if end != start + self.timeframe.duration:
            raise ValueError("end must equal start + timeframe duration")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        o = _to_decimal(self.open, field="open")
        h = _to_decimal(self.high, field="high")
        lo = _to_decimal(self.low, field="low")
        c = _to_decimal(self.close, field="close")
        for label, val in (("open", o), ("high", h), ("low", lo), ("close", c)):
            if val <= 0:
                raise ValueError(f"{label} must be > 0; got {val!s}")
            object.__setattr__(self, label, val)
        volume = _to_decimal(self.volume, field="volume")
        if volume < 0:
            raise ValueError(f"volume must be >= 0; got {volume!s}")
        object.__setattr__(self, "volume", volume)
        if lo > min(o, c):
            raise ValueError(f"low ({lo!s}) must be <= min(open, close)")
        if max(o, c) > h:
            raise ValueError(f"high ({h!s}) must be >= max(open, close)")
        vwap = _opt_decimal(self.vwap, field="vwap")
        if vwap is not None and vwap <= 0:
            raise ValueError(f"vwap must be > 0; got {vwap!s}")
        object.__setattr__(self, "vwap", vwap)
        if self.trade_count is not None:
            if isinstance(self.trade_count, bool) or not isinstance(self.trade_count, int):
                raise ValueError("trade_count must be int or None")
            if self.trade_count < 0:
                raise ValueError("trade_count must be >= 0")
        if not isinstance(self.closed, bool):
            raise ValueError("closed must be bool")
        if not isinstance(self.provenance, FeedProvenance):
            raise ValueError("provenance must be FeedProvenance")


class DataFreshness(Enum):
    """Freshness classification for a market context."""

    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class MarketContext:
    """Shared immutable snapshot handed to strategies for evaluation."""

    symbol: str
    as_of: datetime
    session: MarketSession | SessionKind
    freshness: DataFreshness
    candles: Mapping[Timeframe, tuple[Candle, ...]]
    indicators: Mapping[str, Decimal | None]
    provenance: FeedProvenance
    last_event_at: datetime | None = None
    research: Mapping[str, object] | None = None
    research_provenance: str | None = None

    def __post_init__(self) -> None:
        symbol = _require_symbol(self.symbol, field="symbol")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "as_of", _require_utc(self.as_of, field="as_of"))
        if not isinstance(self.session, (MarketSession, SessionKind)):
            raise ValueError("session must be MarketSession or SessionKind")
        if not isinstance(self.freshness, DataFreshness):
            raise ValueError("freshness must be DataFreshness")
        if not isinstance(self.provenance, FeedProvenance):
            raise ValueError("provenance must be FeedProvenance")
        if self.last_event_at is not None:
            object.__setattr__(
                self, "last_event_at", _require_utc(self.last_event_at, field="last_event_at")
            )
        object.__setattr__(self, "candles", self._freeze_candles(self.candles, symbol))
        object.__setattr__(self, "indicators", self._freeze_indicators(self.indicators))
        object.__setattr__(self, "research", self._freeze_research(self.research))
        if self.research_provenance is not None:
            prov = self.research_provenance
            if not isinstance(prov, str) or not prov.strip():
                raise ValueError("research_provenance must be a non-empty str or None")
            object.__setattr__(self, "research_provenance", prov.strip())

    @staticmethod
    def _freeze_candles(
        candles: Mapping[Timeframe, Iterable[Candle]], symbol: str
    ) -> Mapping[Timeframe, tuple[Candle, ...]]:
        if not isinstance(candles, Mapping):
            raise ValueError("candles must be a mapping of Timeframe to candles")
        frozen: dict[Timeframe, tuple[Candle, ...]] = {}
        for key, value in candles.items():
            if not isinstance(key, Timeframe):
                raise ValueError(f"candle key must be Timeframe; got {key!r}")
            if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
                raise ValueError(f"candles[{key.value}] must be an iterable of Candle")
            seq = tuple(value)
            for candle in seq:
                if not isinstance(candle, Candle):
                    raise ValueError(f"candles[{key.value}] must contain Candle objects")
                if candle.symbol != symbol:
                    raise ValueError(f"candle symbol {candle.symbol} != context symbol {symbol}")
                if candle.timeframe is not key:
                    raise ValueError("candle timeframe must match its mapping key")
            for prev, cur in zip(seq, seq[1:], strict=False):
                if not cur.start > prev.start:
                    raise ValueError("candles must be in ascending start order")
                if not cur.start >= prev.end:
                    raise ValueError("candles must not overlap")
            frozen[key] = seq
        return MappingProxyType(frozen)

    @staticmethod
    def _freeze_indicators(
        indicators: Mapping[str, Decimal | int | str | None],
    ) -> Mapping[str, Decimal | None]:
        if not isinstance(indicators, Mapping):
            raise ValueError("indicators must be a mapping of str to Decimal | None")
        frozen: dict[str, Decimal | None] = {}
        for key, value in indicators.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"indicator key must be a non-empty str; got {key!r}")
            frozen[key] = None if value is None else _to_decimal(value, field=f"ind[{key!r}]")
        return MappingProxyType(frozen)

    @staticmethod
    def _freeze_research(research: Mapping[str, object] | None) -> Mapping[str, object]:
        if research is None:
            return MappingProxyType({})
        if not isinstance(research, Mapping):
            raise ValueError("research must be a mapping or None")
        frozen: dict[str, object] = {}
        for key, value in research.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"research key must be a non-empty str; got {key!r}")
            frozen[key] = value
        return MappingProxyType(frozen)

    def latest_closed(self, timeframe: Timeframe) -> Candle | None:
        """Return the latest candle with ``closed=True``, or ``None``."""
        for candle in reversed(self.candles.get(timeframe, ())):
            if candle.closed:
                return candle
        return None
