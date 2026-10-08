"""Bounded immutable in-memory state for shared market data.

The cache is deliberately a working set, not a tick archive.  Mutable
containers are confined to this module and every value crossing its public
boundary is a frozen dataclass, a tuple, or another immutable value.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from mike_trade.domain.market import Candle, MarketEvent, MarketEventKind, Timeframe
from mike_trade.market_data.alpaca_stream import StreamBar, StreamQuote, StreamTrade
from mike_trade.market_data.polling import PollKind, PollSnapshot

__all__ = [
    "CacheCapacityError",
    "FeedMismatchError",
    "MarketDataUpdate",
    "FreshnessMetadata",
    "DecisionEvidence",
    "CandleBuilderInput",
    "IndicatorStateSlot",
    "SymbolCacheSnapshot",
    "MarketDataCache",
    "normalize_market_data",
]


class FeedMismatchError(ValueError):
    """Raised when data from two feeds would be mixed for one symbol."""


class CacheCapacityError(RuntimeError):
    """Raised instead of allowing a bounded cache collection to grow."""


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _symbol(value: str) -> str:
    normalized = value.strip().upper() if isinstance(value, str) else ""
    if not normalized:
        raise ValueError("symbol must be non-empty")
    return normalized


def _feed(value: str) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if not normalized:
        raise ValueError("feed_id must be non-empty")
    return normalized


def _positive_int(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _freeze(value: Any) -> Any:
    """Recursively freeze a compact cache value or reject an unsafe object."""
    if value is None or isinstance(value, (str, bytes, int, bool, Decimal, datetime, Enum)):
        return value
    if isinstance(value, float):
        # Floats may be evidence labels/metrics; preserving them is immutable.
        return value
    if isinstance(value, Mapping):
        return tuple((_freeze(key), _freeze(item)) for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    if is_dataclass(value):
        params = getattr(type(value), "__dataclass_params__", None)
        if params is not None and params.frozen:
            # Validate nested fields too.  Existing domain values are frozen,
            # so returning the object preserves its useful concrete type.
            for item in fields(value):
                _freeze(getattr(value, item.name))
            return value
    raise TypeError(f"cache values must be immutable; got {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class MarketDataUpdate:
    """Provider-neutral immutable event used by the cache and fanout."""

    symbol: str
    kind: str
    provider_source_ts: datetime
    receive_ts: datetime
    feed_id: str
    source: str
    values: tuple[tuple[str, Any], ...] | Mapping[str, Any] = ()
    stale: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        kind = self.kind.strip().lower() if isinstance(self.kind, str) else ""
        if kind not in (PollKind.TRADE, PollKind.QUOTE, PollKind.BAR):
            raise ValueError("kind must be trade, quote, or bar")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self, "provider_source_ts", _utc(self.provider_source_ts, "provider_source_ts")
        )
        object.__setattr__(self, "receive_ts", _utc(self.receive_ts, "receive_ts"))
        object.__setattr__(self, "feed_id", _feed(self.feed_id))
        source = self.source.strip() if isinstance(self.source, str) else ""
        if not source:
            raise ValueError("source must be non-empty")
        object.__setattr__(self, "source", source)
        items = self.values.items() if isinstance(self.values, Mapping) else self.values
        frozen: list[tuple[str, Any]] = []
        for key, value in items:
            if not isinstance(key, str) or not key:
                raise ValueError("event value keys must be non-empty strings")
            frozen.append((key, _freeze(value)))
        object.__setattr__(self, "values", tuple(frozen))
        if not isinstance(self.stale, bool):
            raise ValueError("stale must be bool")

    @property
    def channel(self) -> str:
        return f"{self.kind}s" if self.kind != PollKind.BAR else "bars"

    @property
    def event_time(self) -> datetime:
        return self.provider_source_ts

    @property
    def received_at(self) -> datetime:
        return self.receive_ts

    def get(self, key: str, default: Any = None) -> Any:
        items = self.values.items() if isinstance(self.values, Mapping) else self.values
        for name, value in items:
            if name == key:
                return value
        return default

    @property
    def price(self) -> Decimal | int | None:
        value = self.get("price", self.get("p", self.get("close", self.get("c"))))
        return value if isinstance(value, (Decimal, int)) and not isinstance(value, bool) else None

    @property
    def bid(self) -> Decimal | int | None:
        value = self.get("bid", self.get("bp"))
        return value if isinstance(value, (Decimal, int)) and not isinstance(value, bool) else None

    @property
    def ask(self) -> Decimal | int | None:
        value = self.get("ask", self.get("ap"))
        return value if isinstance(value, (Decimal, int)) and not isinstance(value, bool) else None


@dataclass(frozen=True, slots=True)
class FreshnessMetadata:
    provider_source_ts: datetime
    receive_ts: datetime
    feed_id: str
    source: str
    stale: bool = False


@dataclass(frozen=True, slots=True)
class DecisionEvidence:
    symbol: str
    name: str
    value: Any
    provider_source_ts: datetime
    receive_ts: datetime
    feed_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name must be non-empty")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "value", _freeze(self.value))
        object.__setattr__(
            self, "provider_source_ts", _utc(self.provider_source_ts, "provider_source_ts")
        )
        object.__setattr__(self, "receive_ts", _utc(self.receive_ts, "receive_ts"))
        object.__setattr__(self, "feed_id", _feed(self.feed_id))


@dataclass(frozen=True, slots=True)
class CandleBuilderInput:
    symbol: str
    timeframe: Timeframe
    update: MarketDataUpdate

    def __post_init__(self) -> None:
        symbol = _symbol(self.symbol)
        object.__setattr__(self, "symbol", symbol)
        if not isinstance(self.timeframe, Timeframe):
            raise ValueError("timeframe must be Timeframe")
        if not isinstance(self.update, MarketDataUpdate) or self.update.symbol != symbol:
            raise ValueError("update must be a MarketDataUpdate for the same symbol")


@dataclass(frozen=True, slots=True)
class IndicatorStateSlot:
    symbol: str
    timeframe: Timeframe
    name: str
    value: Any
    provider_source_ts: datetime
    receive_ts: datetime
    feed_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        if not isinstance(self.timeframe, Timeframe):
            raise ValueError("timeframe must be Timeframe")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("name must be non-empty")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "value", _freeze(self.value))
        object.__setattr__(
            self, "provider_source_ts", _utc(self.provider_source_ts, "provider_source_ts")
        )
        object.__setattr__(self, "receive_ts", _utc(self.receive_ts, "receive_ts"))
        object.__setattr__(self, "feed_id", _feed(self.feed_id))


@dataclass(frozen=True, slots=True)
class SymbolCacheSnapshot:
    symbol: str
    feed_id: str | None
    latest_quote: MarketDataUpdate | None
    latest_trade: MarketDataUpdate | None
    freshness: FreshnessMetadata | None
    evidence: tuple[DecisionEvidence, ...]
    candle_inputs: tuple[tuple[Timeframe, tuple[CandleBuilderInput, ...]], ...]
    completed_candles: tuple[tuple[Timeframe, tuple[Candle, ...]], ...]
    indicator_states: tuple[IndicatorStateSlot, ...]


def normalize_market_data(event: Any, *, source: str | None = None) -> MarketDataUpdate:
    """Convert stream, poll, or domain events to the common immutable shape."""
    if isinstance(event, MarketDataUpdate):
        if source is None or event.source == source:
            return event
        return MarketDataUpdate(
            event.symbol,
            event.kind,
            event.provider_source_ts,
            event.receive_ts,
            event.feed_id,
            source,
            event.values,
            event.stale,
        )
    if isinstance(event, PollSnapshot):
        return MarketDataUpdate(
            event.symbol,
            event.kind,
            event.provider_source_ts,
            event.receive_ts,
            event.feed_id,
            source or event.source,
            event.values,
            event.stale,
        )
    if isinstance(event, StreamTrade):
        values = {
            "price": event.price,
            "size": event.size,
            "sequence_id": event.sequence_id,
            "exchange": event.exchange,
            "conditions": event.conditions,
            "tape": event.tape,
        }
        kind = PollKind.TRADE
    elif isinstance(event, StreamQuote):
        values = {
            "bid": event.bid_price,
            "bid_size": event.bid_size,
            "ask": event.ask_price,
            "ask_size": event.ask_size,
            "sequence_id": event.sequence_id,
            "bid_exchange": event.bid_exchange,
            "ask_exchange": event.ask_exchange,
            "conditions": event.conditions,
            "tape": event.tape,
        }
        kind = PollKind.QUOTE
    elif isinstance(event, StreamBar):
        values = {
            "open": event.open,
            "high": event.high,
            "low": event.low,
            "close": event.close,
            "volume": event.volume,
            "vwap": event.vwap,
            "trade_count": event.trade_count,
            "sequence_id": event.sequence_id,
        }
        kind = PollKind.BAR
    elif isinstance(event, MarketEvent):
        kind = {
            MarketEventKind.TRADE: PollKind.TRADE,
            MarketEventKind.QUOTE: PollKind.QUOTE,
            MarketEventKind.BAR: PollKind.BAR,
        }[event.kind]
        values = {
            "price": event.price,
            "size": event.size,
            "bid": event.bid,
            "ask": event.ask,
            "bid_size": event.bid_size,
            "ask_size": event.ask_size,
        }
        return MarketDataUpdate(
            event.symbol,
            kind,
            event.event_time,
            event.received_at,
            event.provenance.feed,
            source
            or (
                "stream"
                if event.provenance.source == "websocket"
                else event.provenance.source
            ),
            values,
        )
    else:
        # A structural fallback keeps injected test/provider adapters simple.
        required = ("symbol", "provider_source_ts", "receive_ts", "feed_id")
        if not all(hasattr(event, name) for name in required):
            raise TypeError(f"unsupported market-data event: {type(event).__name__}")
        raw_kind = getattr(event, "kind", getattr(event, "channel", None))
        raw_kind = getattr(raw_kind, "value", raw_kind)
        kind = str(raw_kind).lower() if raw_kind is not None else ""
        if kind.endswith("s"):
            kind = kind[:-1]
        if kind not in (PollKind.TRADE, PollKind.QUOTE, PollKind.BAR):
            name = type(event).__name__.lower()
            kind = next((item for item in ("trade", "quote", "bar") if item in name), "")
        raw_values = getattr(event, "values", None)
        if raw_values is None:
            field_names = {
                PollKind.TRADE: ("price", "size"),
                PollKind.QUOTE: (
                    "bid",
                    "bid_price",
                    "bid_size",
                    "ask",
                    "ask_price",
                    "ask_size",
                ),
                PollKind.BAR: (
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "vwap",
                    "trade_count",
                ),
            }.get(kind, ())
            raw_values = {
                name: getattr(event, name) for name in field_names if hasattr(event, name)
            }
        event_source = source or getattr(event, "source", "stream")
        if not isinstance(event_source, str):
            raise TypeError("market-data event source must be a string")
        return MarketDataUpdate(
            event.symbol,
            kind,
            event.provider_source_ts,
            event.receive_ts,
            event.feed_id,
            event_source,
            raw_values,
            bool(getattr(event, "stale", False)),
        )
    return MarketDataUpdate(
        event.symbol,
        kind,
        event.provider_source_ts,
        event.receive_ts,
        event.feed_id,
        source or "stream",
        values,
    )


class MarketDataCache:
    """Bounded cache of the decision-time market-data working set."""

    def __init__(
        self,
        *,
        max_symbols: int = 500,
        max_evidence_per_symbol: int = 128,
        max_candle_inputs_per_timeframe: int = 512,
        max_completed_candles_per_timeframe: int = 512,
        max_indicator_slots_per_symbol: int = 128,
    ) -> None:
        self._max_symbols = _positive_int(max_symbols, "max_symbols")
        self._evidence_size = _positive_int(
            max_evidence_per_symbol, "max_evidence_per_symbol"
        )
        self._input_size = _positive_int(
            max_candle_inputs_per_timeframe, "max_candle_inputs_per_timeframe"
        )
        self._candle_size = _positive_int(
            max_completed_candles_per_timeframe, "max_completed_candles_per_timeframe"
        )
        self._indicator_size = _positive_int(
            max_indicator_slots_per_symbol, "max_indicator_slots_per_symbol"
        )
        self._feeds: dict[str, str] = {}
        self._quotes: dict[str, MarketDataUpdate] = {}
        self._trades: dict[str, MarketDataUpdate] = {}
        self._freshness: dict[str, FreshnessMetadata] = {}
        self._evidence: dict[str, deque[DecisionEvidence]] = {}
        self._inputs: dict[tuple[str, Timeframe], deque[CandleBuilderInput]] = {}
        self._candles: dict[tuple[str, Timeframe], deque[Candle]] = {}
        self._indicators: dict[str, OrderedDict[tuple[Timeframe, str], IndicatorStateSlot]] = {}

    def _accept_feed(self, symbol: str, feed_id: str) -> tuple[str, str]:
        symbol, feed_id = _symbol(symbol), _feed(feed_id)
        current = self._feeds.get(symbol)
        if current is not None and current != feed_id:
            raise FeedMismatchError(
                f"cannot merge {feed_id!r} data into {symbol} cache established as {current!r}; "
                "reset the feed first"
            )
        if current is None:
            if len(self._feeds) >= self._max_symbols:
                raise CacheCapacityError(f"cache already contains max_symbols={self._max_symbols}")
            self._feeds[symbol] = feed_id
        return symbol, feed_id

    def store_event(self, event: Any, *, source: str | None = None) -> MarketDataUpdate:
        update = normalize_market_data(event, source=source)
        symbol, _ = self._accept_feed(update.symbol, update.feed_id)
        if update.kind == PollKind.QUOTE:
            self._quotes[symbol] = update
        elif update.kind == PollKind.TRADE:
            self._trades[symbol] = update
        self._freshness[symbol] = FreshnessMetadata(
            update.provider_source_ts,
            update.receive_ts,
            update.feed_id,
            update.source,
            update.stale,
        )
        return update

    # Convenient synonym for callers treating the cache as an event sink.
    update = store_event

    def latest_quote(self, symbol: str) -> MarketDataUpdate | None:
        return self._quotes.get(_symbol(symbol))

    def latest_trade(self, symbol: str) -> MarketDataUpdate | None:
        return self._trades.get(_symbol(symbol))

    def freshness(self, symbol: str) -> FreshnessMetadata | None:
        return self._freshness.get(_symbol(symbol))

    def append_evidence(self, evidence: DecisionEvidence) -> None:
        if not isinstance(evidence, DecisionEvidence):
            raise TypeError("evidence must be DecisionEvidence")
        symbol, _ = self._accept_feed(evidence.symbol, evidence.feed_id)
        self._evidence.setdefault(symbol, deque(maxlen=self._evidence_size)).append(evidence)

    add_decision_evidence = append_evidence

    def decision_evidence(self, symbol: str) -> tuple[DecisionEvidence, ...]:
        return tuple(self._evidence.get(_symbol(symbol), ()))

    def append_candle_input(self, timeframe: Timeframe, event: Any) -> CandleBuilderInput:
        update = normalize_market_data(event)
        symbol, _ = self._accept_feed(update.symbol, update.feed_id)
        record = CandleBuilderInput(symbol, timeframe, update)
        self._inputs.setdefault(
            (symbol, timeframe), deque(maxlen=self._input_size)
        ).append(record)
        return record

    add_candle_input = append_candle_input

    def candle_inputs(
        self, symbol: str, timeframe: Timeframe
    ) -> tuple[CandleBuilderInput, ...]:
        return tuple(self._inputs.get((_symbol(symbol), timeframe), ()))

    def append_completed_candle(self, candle: Candle) -> None:
        if not isinstance(candle, Candle) or not candle.closed:
            raise ValueError("completed candle must be a closed Candle")
        symbol, _ = self._accept_feed(candle.symbol, candle.provenance.feed)
        self._candles.setdefault(
            (symbol, candle.timeframe), deque(maxlen=self._candle_size)
        ).append(candle)

    add_completed_candle = append_completed_candle

    def completed_candles(self, symbol: str, timeframe: Timeframe) -> tuple[Candle, ...]:
        return tuple(self._candles.get((_symbol(symbol), timeframe), ()))

    def set_indicator_state(self, slot: IndicatorStateSlot) -> None:
        if not isinstance(slot, IndicatorStateSlot):
            raise TypeError("slot must be IndicatorStateSlot")
        symbol, _ = self._accept_feed(slot.symbol, slot.feed_id)
        states = self._indicators.setdefault(symbol, OrderedDict())
        key = (slot.timeframe, slot.name)
        if key not in states and len(states) >= self._indicator_size:
            states.popitem(last=False)
        states[key] = slot
        states.move_to_end(key)

    def indicator_state(
        self, symbol: str, timeframe: Timeframe | None = None
    ) -> tuple[IndicatorStateSlot, ...]:
        values: Iterable[IndicatorStateSlot] = self._indicators.get(_symbol(symbol), {}).values()
        if timeframe is not None:
            values = (slot for slot in values if slot.timeframe is timeframe)
        return tuple(values)

    def reset_feed(self, symbol: str, feed_id: str | None = None) -> None:
        """Explicitly clear one symbol so a different feed can be established."""
        symbol = _symbol(symbol)
        self._feeds.pop(symbol, None)
        self._quotes.pop(symbol, None)
        self._trades.pop(symbol, None)
        self._freshness.pop(symbol, None)
        self._evidence.pop(symbol, None)
        self._indicators.pop(symbol, None)
        for collection in (self._inputs, self._candles):
            for key in tuple(collection):
                if key[0] == symbol:
                    del collection[key]
        if feed_id is not None:
            self._accept_feed(symbol, feed_id)

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(self._feeds)

    def snapshot(self, symbol: str) -> SymbolCacheSnapshot:
        symbol = _symbol(symbol)
        inputs = tuple(
            (timeframe, tuple(items))
            for (item_symbol, timeframe), items in self._inputs.items()
            if item_symbol == symbol
        )
        candles = tuple(
            (timeframe, tuple(items))
            for (item_symbol, timeframe), items in self._candles.items()
            if item_symbol == symbol
        )
        return SymbolCacheSnapshot(
            symbol=symbol,
            feed_id=self._feeds.get(symbol),
            latest_quote=self._quotes.get(symbol),
            latest_trade=self._trades.get(symbol),
            freshness=self._freshness.get(symbol),
            evidence=tuple(self._evidence.get(symbol, ())),
            candle_inputs=inputs,
            completed_candles=candles,
            indicator_states=tuple(self._indicators.get(symbol, {}).values()),
        )
