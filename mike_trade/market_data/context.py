"""Immutable strategy market-context snapshots and the reference-symbol binder.

Pure in-memory assembly: no network, provider, or broker I/O, and no on-demand
fetching.  The builder only reads state already held by its injected sources (the
h.1 candle builder, the quote/trade cache, the h.3 calendar, and a clock).  It makes
no BUY/SELL decisions.

Design choices:
1. ``MarketContext`` here is the strategy-facing snapshot keyed by
   ``(symbol, as_of, version)``; it is distinct from the older
   ``mike_trade.domain.market.MarketContext`` and reuses the h.1 ``Candle`` type.
2. ``candles`` holds CLOSED candles only (enforced by the value object).  A
   developing candle appears solely in ``developing_candles`` and only when the
   strategy declared ``completed_bars_only=False``.  Indicators are always computed
   from closed candles, never from the developing one.
3. Integrity fails closed: mixed ``(feed_id, consolidated)`` pairs, any non-RAW candle
   (stop-relevant prices are always RAW), a developing candle in ``candles``, or data
   timestamped after ``as_of`` raise ``ContextIntegrityError`` with a reason code.
4. Staleness is data age relative to ``as_of``; a stale context is still a valid value
   but is flagged, and :func:`select_context` never presents it as fresh.
5. The builder does not call ``CandleBuilder.close_due``; the owner of the candle
   builder drives bar closing, so a build never mutates a source.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol

from mike_trade.domain.errors import MikeTradeError
from mike_trade.domain.instruments import require_utc
from mike_trade.market_data.cache import MarketDataUpdate
from mike_trade.market_data.candles import (
    Candle,
    CandleBuilder,
    CandleStatus,
    CandleTimeframe,
    ClockLike,
    PriceBasis,
)
from mike_trade.research.service import ResearchContext
from mike_trade.scheduling import calendar as _nyse
from mike_trade.scheduling.calendar import Session, SessionStatus

__all__ = [
    "REASON_NO_DATA",
    "REASON_DATA_TOO_OLD",
    "REASON_PROVIDER_STALE",
    "CODE_FEED_MISMATCH",
    "CODE_PRICE_BASIS_MISMATCH",
    "CODE_DEVELOPING_IN_COMPLETED",
    "CODE_FUTURE_DATA",
    "CODE_CLOCK_REGRESSION",
    "ContextError",
    "InvalidContextInputError",
    "ContextIntegrityError",
    "UndeclaredSymbolError",
    "ContextBindingError",
    "UnknownIndicatorError",
    "IndicatorInput",
    "IndicatorSpec",
    "CalendarLike",
    "NyseCalendar",
    "QuoteSnapshot",
    "TradeSnapshot",
    "ContextFreshness",
    "MarketContext",
    "ContextBuilder",
    "SelectionStatus",
    "ContextSelection",
    "select_context",
    "StrategyInputs",
    "BoundContexts",
    "ReferenceSymbolBinder",
]

REASON_NO_DATA = "NO_DATA"
REASON_DATA_TOO_OLD = "DATA_TOO_OLD"
REASON_PROVIDER_STALE = "PROVIDER_STALE"

CODE_FEED_MISMATCH = "feed_mismatch"
CODE_PRICE_BASIS_MISMATCH = "price_basis_mismatch"
CODE_DEVELOPING_IN_COMPLETED = "developing_in_completed"
CODE_FUTURE_DATA = "future_data"
CODE_CLOCK_REGRESSION = "clock_regression"

_ZERO = timedelta(0)


# ---------------------------------------------------------------------- errors


class ContextError(MikeTradeError):
    """Base class for market-context failures."""


class InvalidContextInputError(ContextError, ValueError):
    """Raised for an invalid context field, source value, or builder parameter."""


class ContextIntegrityError(ContextError):
    """Raised when assembling a context would mix feeds/bases or leak future data."""


class UndeclaredSymbolError(ContextError, KeyError):
    """Raised when a strategy accesses a symbol it did not declare."""

    def __str__(self) -> str:  # KeyError would otherwise repr() the message
        return self.message


class ContextBindingError(ContextError):
    """Raised when declared inputs cannot be bound to the supplied contexts."""


class UnknownIndicatorError(ContextError):
    """Raised when a strategy declares an indicator the builder has no calculator for."""


def _invalid(message: str) -> InvalidContextInputError:
    return InvalidContextInputError(message, code="invalid_context_input")


def _integrity(code: str, message: str, **context: Any) -> ContextIntegrityError:
    return ContextIntegrityError(message, code=code, context=context)


# ---------------------------------------------------------------------- helpers


def _utc(value: object, field: str) -> datetime:
    try:
        return require_utc(value, field=field)  # type: ignore[arg-type]
    except ValueError as exc:
        raise _invalid(str(exc)) from exc


def _symbol(value: object) -> str:
    text = value.strip().upper() if isinstance(value, str) else ""
    if not text:
        raise _invalid("symbol must be a non-empty str")
    return text


def _feed(value: object) -> str:
    text = value.strip().lower() if isinstance(value, str) else ""
    if not text:
        raise _invalid("feed_id must be a non-empty str")
    return text


def _decimal(value: object, field: str, *, positive: bool = True) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise _invalid(f"{field} must be a Decimal or int")
    result = Decimal(value)
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        raise _invalid(f"{field} must be finite and {'positive' if positive else 'non-negative'}")
    return result


def _timeframe(value: object) -> CandleTimeframe:
    if isinstance(value, CandleTimeframe):
        return value
    if isinstance(value, str):
        try:
            return CandleTimeframe(value.strip().lower())
        except ValueError:
            pass
    raise _invalid(f"timeframe must be a CandleTimeframe or its label; got {value!r}")


def _consolidated_for(feed_id: str) -> bool:
    return feed_id != "iex"


# ---------------------------------------------------------------------- calendar


class CalendarLike(Protocol):
    """Subset of the h.3 calendar the builder needs."""

    def session_status(self, at_utc: datetime) -> SessionStatus: ...

    def session_id(self, at_utc: datetime) -> str | None: ...


class NyseCalendar:
    """Adapter over the module-level h.3 NYSE calendar functions."""

    def session_status(self, at_utc: datetime) -> SessionStatus:
        return _nyse.session_status(at_utc)

    def session_id(self, at_utc: datetime) -> str | None:
        """Session id of the trading day containing ``at_utc`` (ET), or None."""
        if _nyse.session_status(at_utc) is SessionStatus.UNKNOWN:
            return None
        day: date = at_utc.astimezone(_nyse.NEW_YORK).date()
        if not _nyse.is_trading_day(day):
            return None
        session: Session = _nyse.session_for(day)
        return session.session_id


# ---------------------------------------------------------------------- indicators


class IndicatorInput(Enum):
    """What a configured h.2 calculator consumes from each closed candle."""

    CLOSE = "close"  # calc.update(close)
    VOLUME = "volume"  # calc.update(volume)
    BAR = "bar"  # calc.update(candle)
    SESSION_BAR = "session_bar"  # calc.update(bar with session_id)
    SESSION_VOLUME = "session_volume"  # calc.update(volume, session_id)


@dataclass(frozen=True, slots=True)
class _SessionBar:
    open: Decimal  # noqa: A003 - OHLC field names
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    session_id: object


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    """How to compute one named indicator: calculator factory, timeframe, input kind.

    ``factory`` must return a fresh h.2 calculator on every call; the builder replays
    the closed candles through it on each build, so no calculator state is shared
    between snapshots.
    """

    timeframe: CandleTimeframe
    factory: Callable[[], Any]
    input: IndicatorInput = IndicatorInput.CLOSE

    def __post_init__(self) -> None:
        object.__setattr__(self, "timeframe", _timeframe(self.timeframe))
        if not callable(self.factory):
            raise _invalid("factory must be callable")
        if not isinstance(self.input, IndicatorInput):
            raise _invalid("input must be IndicatorInput")


# ---------------------------------------------------------------------- snapshots


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    """Latest quote with provider and receive timestamps (RAW prices)."""

    symbol: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal | None
    ask_size: Decimal | None
    provider_ts: datetime
    receive_ts: datetime
    feed_id: str
    price_basis: PriceBasis = PriceBasis.RAW

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "bid", _decimal(self.bid, "bid"))
        object.__setattr__(self, "ask", _decimal(self.ask, "ask"))
        for name in ("bid_size", "ask_size"):
            size = getattr(self, name)
            if size is not None:
                object.__setattr__(self, name, _decimal(size, name, positive=False))
        object.__setattr__(self, "provider_ts", _utc(self.provider_ts, "provider_ts"))
        object.__setattr__(self, "receive_ts", _utc(self.receive_ts, "receive_ts"))
        object.__setattr__(self, "feed_id", _feed(self.feed_id))
        if self.price_basis is not PriceBasis.RAW:
            raise _integrity(CODE_PRICE_BASIS_MISMATCH, "quote prices must be RAW")


@dataclass(frozen=True, slots=True)
class TradeSnapshot:
    """Latest trade print with provider and receive timestamps (RAW price)."""

    symbol: str
    price: Decimal
    size: Decimal | None
    provider_ts: datetime
    receive_ts: datetime
    feed_id: str
    price_basis: PriceBasis = PriceBasis.RAW

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "price", _decimal(self.price, "price"))
        if self.size is not None:
            object.__setattr__(self, "size", _decimal(self.size, "size", positive=False))
        object.__setattr__(self, "provider_ts", _utc(self.provider_ts, "provider_ts"))
        object.__setattr__(self, "receive_ts", _utc(self.receive_ts, "receive_ts"))
        object.__setattr__(self, "feed_id", _feed(self.feed_id))
        if self.price_basis is not PriceBasis.RAW:
            raise _integrity(CODE_PRICE_BASIS_MISMATCH, "trade price must be RAW")


def _quote_snapshot(update: MarketDataUpdate, symbol: str) -> QuoteSnapshot:
    if update.kind != "quote" or update.symbol != symbol:
        raise _invalid(f"cache returned a non-quote or foreign-symbol update for {symbol}")
    return QuoteSnapshot(
        symbol=symbol,
        bid=update.bid,  # type: ignore[arg-type]
        ask=update.ask,  # type: ignore[arg-type]
        bid_size=_optional_size(update.get("bid_size")),
        ask_size=_optional_size(update.get("ask_size")),
        provider_ts=update.provider_source_ts,
        receive_ts=update.receive_ts,
        feed_id=update.feed_id,
    )


def _trade_snapshot(update: MarketDataUpdate, symbol: str) -> TradeSnapshot:
    if update.kind != "trade" or update.symbol != symbol:
        raise _invalid(f"cache returned a non-trade or foreign-symbol update for {symbol}")
    return TradeSnapshot(
        symbol=symbol,
        price=update.price,  # type: ignore[arg-type]
        size=_optional_size(update.get("size")),
        provider_ts=update.provider_source_ts,
        receive_ts=update.receive_ts,
        feed_id=update.feed_id,
    )


def _optional_size(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float):  # sizes from some providers arrive as floats
        value = Decimal(str(value))
    return _decimal(value, "size", positive=False)


@dataclass(frozen=True, slots=True)
class ContextFreshness:
    """Data age relative to ``as_of`` plus an explicit stale flag and reason code."""

    newest_data_ts: datetime | None
    age: timedelta | None
    max_age: timedelta
    stale: bool
    reason: str | None

    def __post_init__(self) -> None:
        if self.newest_data_ts is not None:
            object.__setattr__(self, "newest_data_ts", _utc(self.newest_data_ts, "newest_data_ts"))
        if (self.newest_data_ts is None) != (self.age is None):
            raise _invalid("newest_data_ts and age must both be set or both be None")
        if self.age is not None and (not isinstance(self.age, timedelta) or self.age < _ZERO):
            raise _invalid("age must be a non-negative timedelta")
        if not isinstance(self.max_age, timedelta) or self.max_age <= _ZERO:
            raise _invalid("max_age must be a positive timedelta")
        if not isinstance(self.stale, bool):
            raise _invalid("stale must be bool")
        if self.stale and not (isinstance(self.reason, str) and self.reason):
            raise _invalid("stale freshness requires a reason code")
        if not self.stale and self.reason is not None:
            raise _invalid("fresh freshness must not carry a reason")


# ---------------------------------------------------------------------- the context


def _feed_pairs(
    candles: Iterable[Candle],
    quote: QuoteSnapshot | None,
    trade: TradeSnapshot | None,
) -> frozenset[tuple[str, bool]]:
    pairs = {(c.feed_id, c.consolidated) for c in candles}
    pairs.update((s.feed_id, _consolidated_for(s.feed_id)) for s in (quote, trade) if s)
    return frozenset(pairs)


def _check_feed(pairs: frozenset[tuple[str, bool]], symbol: str) -> None:
    if len(pairs) > 1:
        raise _integrity(
            CODE_FEED_MISMATCH,
            f"{symbol}: mixed feed identities {sorted(pairs)!r}",
            symbol=symbol,
        )


@dataclass(frozen=True, slots=True)
class MarketContext:
    """Frozen strategy snapshot keyed by ``(symbol, as_of, version)``.

    ``candles`` contains CLOSED candles only.  ``developing_candles`` is empty unless
    the strategy explicitly declared ``completed_bars_only=False``.  ``price_basis`` is
    the basis of every stop-relevant price in the snapshot and is always RAW.
    """

    symbol: str
    as_of: datetime
    version: int
    feed_id: str | None
    consolidated: bool
    price_basis: PriceBasis
    session_status: SessionStatus
    session_id: str | None
    freshness: ContextFreshness
    candles: Mapping[CandleTimeframe, tuple[Candle, ...]]
    indicators: Mapping[str, Decimal | None]
    latest_quote: QuoteSnapshot | None = None
    latest_trade: TradeSnapshot | None = None
    developing_candles: Mapping[CandleTimeframe, Candle] | None = None
    completed_bars_only: bool = True
    research: ResearchContext | None = None

    def __post_init__(self) -> None:
        symbol = _symbol(self.symbol)
        object.__setattr__(self, "symbol", symbol)
        as_of = _utc(self.as_of, "as_of")
        object.__setattr__(self, "as_of", as_of)
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise _invalid("version must be a positive int")
        if not isinstance(self.price_basis, PriceBasis):
            raise _invalid("price_basis must be PriceBasis")
        if self.price_basis is not PriceBasis.RAW:
            raise _integrity(
                CODE_PRICE_BASIS_MISMATCH,
                f"{symbol}: stop-relevant prices must be RAW",
                symbol=symbol,
            )
        if not isinstance(self.consolidated, bool):
            raise _invalid("consolidated must be bool")
        if self.feed_id is not None:
            feed = _feed(self.feed_id)
            object.__setattr__(self, "feed_id", feed)
            if feed == "iex" and self.consolidated:
                raise _invalid("IEX feed is not consolidated")
        if not isinstance(self.session_status, SessionStatus):
            raise _invalid("session_status must be SessionStatus")
        if self.session_id is not None and not (
            isinstance(self.session_id, str) and self.session_id
        ):
            raise _invalid("session_id must be a non-empty str or None")
        if not isinstance(self.freshness, ContextFreshness):
            raise _invalid("freshness must be ContextFreshness")
        if not isinstance(self.completed_bars_only, bool):
            raise _invalid("completed_bars_only must be bool")
        if self.latest_quote is not None and not isinstance(self.latest_quote, QuoteSnapshot):
            raise _invalid("latest_quote must be QuoteSnapshot or None")
        if self.latest_trade is not None and not isinstance(self.latest_trade, TradeSnapshot):
            raise _invalid("latest_trade must be TradeSnapshot or None")
        if self.research is not None and not isinstance(self.research, ResearchContext):
            raise _invalid("research must be ResearchContext or None")

        closed = self._freeze_closed(self.candles, symbol, as_of)
        developing = self._freeze_developing(self.developing_candles, symbol)
        if self.completed_bars_only and developing:
            raise _invalid("developing candles require completed_bars_only=False")
        object.__setattr__(self, "candles", closed)
        object.__setattr__(self, "developing_candles", developing)
        object.__setattr__(self, "indicators", self._freeze_indicators(self.indicators))

        for quote_or_trade in (self.latest_quote, self.latest_trade):
            if quote_or_trade is None:
                continue
            if quote_or_trade.symbol != symbol:
                raise _invalid(f"{quote_or_trade.symbol} snapshot in {symbol} context")
            if quote_or_trade.receive_ts > as_of:
                raise _integrity(
                    CODE_FUTURE_DATA, f"{symbol}: quote/trade received after as_of", symbol=symbol
                )
        every_candle = [c for seq in closed.values() for c in seq] + list(developing.values())
        if any(c.price_basis is not self.price_basis for c in every_candle):
            raise _integrity(
                CODE_PRICE_BASIS_MISMATCH,
                f"{symbol}: SPLIT_ADJUSTED candles cannot accompany RAW stop prices",
                symbol=symbol,
            )
        pairs = _feed_pairs(every_candle, self.latest_quote, self.latest_trade)
        if self.feed_id is not None:
            pairs = pairs | {(self.feed_id, self.consolidated)}
        _check_feed(pairs, symbol)

    @staticmethod
    def _freeze_closed(
        candles: Mapping[CandleTimeframe, Iterable[Candle]], symbol: str, as_of: datetime
    ) -> Mapping[CandleTimeframe, tuple[Candle, ...]]:
        if not isinstance(candles, Mapping):
            raise _invalid("candles must be a mapping of CandleTimeframe to candles")
        frozen: dict[CandleTimeframe, tuple[Candle, ...]] = {}
        for key, value in candles.items():
            tf = _timeframe(key)
            seq = tuple(value)
            for candle in seq:
                if not isinstance(candle, Candle):
                    raise _invalid(f"candles[{tf.value}] must contain Candle objects")
                if candle.status is not CandleStatus.CLOSED:
                    raise _integrity(
                        CODE_DEVELOPING_IN_COMPLETED,
                        f"{symbol}: developing candle in completed candles[{tf.value}]",
                        symbol=symbol,
                    )
                if candle.symbol != symbol or candle.timeframe is not tf:
                    raise _invalid(f"candles[{tf.value}] holds a foreign symbol or timeframe")
                if candle.end > as_of:
                    raise _integrity(
                        CODE_FUTURE_DATA,
                        f"{symbol}: candle {candle.start.isoformat()} closes after as_of",
                        symbol=symbol,
                    )
            for prev, cur in zip(seq, seq[1:], strict=False):
                if cur.start < prev.end:
                    raise _invalid(f"candles[{tf.value}] must be ascending and non-overlapping")
            frozen[tf] = seq
        return MappingProxyType(frozen)

    @staticmethod
    def _freeze_developing(
        candles: Mapping[CandleTimeframe, Candle] | None, symbol: str
    ) -> Mapping[CandleTimeframe, Candle]:
        if candles is None:
            return MappingProxyType({})
        if not isinstance(candles, Mapping):
            raise _invalid("developing_candles must be a mapping of CandleTimeframe to Candle")
        frozen: dict[CandleTimeframe, Candle] = {}
        for key, candle in candles.items():
            tf = _timeframe(key)
            if not isinstance(candle, Candle) or candle.status is not CandleStatus.DEVELOPING:
                raise _invalid(f"developing_candles[{tf.value}] must be a DEVELOPING Candle")
            if candle.symbol != symbol or candle.timeframe is not tf:
                raise _invalid(f"developing_candles[{tf.value}] holds a foreign symbol/timeframe")
            frozen[tf] = candle
        return MappingProxyType(frozen)

    @staticmethod
    def _freeze_indicators(
        indicators: Mapping[str, Decimal | None],
    ) -> Mapping[str, Decimal | None]:
        if not isinstance(indicators, Mapping):
            raise _invalid("indicators must be a mapping of name to Decimal | None")
        frozen: dict[str, Decimal | None] = {}
        for name, value in indicators.items():
            if not isinstance(name, str) or not name:
                raise _invalid("indicator names must be non-empty strings")
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
                raise _invalid(f"indicator {name} must be a finite Decimal or None")
            frozen[name] = value
        return MappingProxyType(frozen)

    @property
    def key(self) -> tuple[str, datetime, int]:
        return (self.symbol, self.as_of, self.version)

    @property
    def is_stale(self) -> bool:
        return self.freshness.stale


# ---------------------------------------------------------------------- strategy inputs


@dataclass(frozen=True, slots=True)
class StrategyInputs:
    """A strategy's declared market inputs (the only symbols it may read)."""

    primary_symbol: str
    reference_symbols: tuple[str, ...] = ()
    timeframes: tuple[CandleTimeframe, ...] = ()
    indicators: tuple[str, ...] = ()
    completed_bars_only: bool = True

    def __post_init__(self) -> None:
        primary = _symbol(self.primary_symbol)
        object.__setattr__(self, "primary_symbol", primary)
        if isinstance(self.reference_symbols, (str, bytes)):
            raise _invalid("reference_symbols must be a tuple of symbols")
        refs = tuple(_symbol(s) for s in self.reference_symbols)
        if primary in refs or len(set(refs)) != len(refs):
            raise _invalid("reference_symbols must be unique and exclude the primary symbol")
        object.__setattr__(self, "reference_symbols", refs)
        if isinstance(self.timeframes, (str, bytes)):
            raise _invalid("timeframes must be a tuple")
        frames = tuple(dict.fromkeys(_timeframe(tf) for tf in self.timeframes))
        object.__setattr__(self, "timeframes", frames)
        if isinstance(self.indicators, (str, bytes)):
            raise _invalid("indicators must be a tuple of names")
        names = tuple(self.indicators)
        if any(not isinstance(n, str) or not n.strip() for n in names):
            raise _invalid("indicator names must be non-empty strings")
        object.__setattr__(self, "indicators", tuple(dict.fromkeys(n.strip() for n in names)))
        if not isinstance(self.completed_bars_only, bool):
            raise _invalid("completed_bars_only must be bool")

    @property
    def symbols(self) -> tuple[str, ...]:
        return (self.primary_symbol, *self.reference_symbols)


# ---------------------------------------------------------------------- builder


class QuoteTradeSource(Protocol):
    """Read-only view of the quote/trade cache (``MarketDataCache`` satisfies this)."""

    def latest_quote(self, symbol: str) -> MarketDataUpdate | None: ...

    def latest_trade(self, symbol: str) -> MarketDataUpdate | None: ...


class ContextBuilder:
    """Assembles :class:`MarketContext` snapshots from injected in-memory sources.

    Versions increase by one per symbol per successful build.  ``as_of`` is always the
    injected clock's current time and may not move backwards for a symbol.
    """

    def __init__(
        self,
        *,
        candle_builder: CandleBuilder,
        quote_trade_cache: QuoteTradeSource,
        clock: ClockLike,
        calendar: CalendarLike | None = None,
        indicator_specs: Mapping[str, IndicatorSpec] | None = None,
        max_age: timedelta = timedelta(seconds=60),
        candle_limit: int | None = None,
    ) -> None:
        if not isinstance(max_age, timedelta) or max_age <= _ZERO:
            raise _invalid("max_age must be a positive timedelta")
        if candle_limit is not None and (
            isinstance(candle_limit, bool) or not isinstance(candle_limit, int) or candle_limit < 1
        ):
            raise _invalid("candle_limit must be a positive int or None")
        if not callable(getattr(clock, "now", None)):
            raise _invalid("clock must provide now()")
        self._candles = candle_builder
        self._cache = quote_trade_cache
        self._clock = clock
        self._calendar: CalendarLike = calendar if calendar is not None else NyseCalendar()
        specs = dict(indicator_specs or {})
        if any(not isinstance(k, str) or not k for k in specs) or not all(
            isinstance(v, IndicatorSpec) for v in specs.values()
        ):
            raise _invalid("indicator_specs must map names to IndicatorSpec")
        self._specs = MappingProxyType(specs)
        self._max_age = max_age
        self._candle_limit = candle_limit
        self._versions: dict[str, int] = {}
        self._last_as_of: dict[str, datetime] = {}

    # -- public ----------------------------------------------------------

    def build(
        self,
        symbol: str,
        *,
        timeframes: Iterable[CandleTimeframe | str] = (),
        indicators: Iterable[str] = (),
        completed_bars_only: bool = True,
        research: ResearchContext | None = None,
    ) -> MarketContext:
        """Build one snapshot stamped with the clock's current time."""
        return self._build_at(
            self._now(),
            _symbol(symbol),
            tuple(dict.fromkeys(map(_timeframe, timeframes))),
            tuple(dict.fromkeys(indicators)),
            completed_bars_only,
            research,
        )

    def build_for_inputs(
        self, inputs: StrategyInputs, *, research: ResearchContext | None = None
    ) -> Mapping[str, MarketContext]:
        """Build contexts for every declared symbol at one shared ``as_of``."""
        if not isinstance(inputs, StrategyInputs):
            raise _invalid("inputs must be StrategyInputs")
        as_of = self._now()
        built = {
            symbol: self._build_at(
                as_of, symbol, inputs.timeframes, inputs.indicators,
                inputs.completed_bars_only, research,
            )
            for symbol in inputs.symbols
        }
        return MappingProxyType(built)

    # -- internals -------------------------------------------------------

    def _now(self) -> datetime:
        return _utc(self._clock.now(), "clock.now()")

    def _build_at(
        self,
        as_of: datetime,
        symbol: str,
        timeframes: tuple[CandleTimeframe, ...],
        indicator_names: tuple[str, ...],
        completed_bars_only: bool,
        research: ResearchContext | None,
    ) -> MarketContext:
        if not isinstance(completed_bars_only, bool):
            raise _invalid("completed_bars_only must be bool")
        last = self._last_as_of.get(symbol)
        if last is not None and as_of < last:
            raise _integrity(
                CODE_CLOCK_REGRESSION,
                f"{symbol}: clock moved backwards ({as_of.isoformat()} < {last.isoformat()})",
                symbol=symbol,
            )
        unknown = [n for n in indicator_names if n not in self._specs]
        if unknown:
            raise UnknownIndicatorError(
                f"no calculator configured for indicators {unknown!r}",
                code="unknown_indicator",
                context={"indicators": tuple(unknown)},
            )

        closed = {
            tf: self._candles.get_completed_candles(symbol, tf, self._candle_limit)
            for tf in timeframes
        }
        developing: dict[CandleTimeframe, Candle] = {}
        if not completed_bars_only:
            for tf in timeframes:
                candle = self._candles.get_developing_candle(symbol, tf)
                if candle is not None:
                    developing[tf] = candle

        update_q = self._cache.latest_quote(symbol)
        update_t = self._cache.latest_trade(symbol)
        quote = _quote_snapshot(update_q, symbol) if update_q is not None else None
        trade = _trade_snapshot(update_t, symbol) if update_t is not None else None

        values = {name: self._indicator(symbol, self._specs[name]) for name in indicator_names}

        pairs = _feed_pairs(
            [c for seq in closed.values() for c in seq] + list(developing.values()), quote, trade
        )
        _check_feed(pairs, symbol)
        feed_id, consolidated = next(iter(pairs)) if pairs else (None, False)

        context = MarketContext(
            symbol=symbol,
            as_of=as_of,
            version=self._versions.get(symbol, 0) + 1,
            feed_id=feed_id,
            consolidated=consolidated,
            price_basis=PriceBasis.RAW,
            session_status=self._calendar.session_status(as_of),
            session_id=self._calendar.session_id(as_of),
            freshness=self._freshness(as_of, closed, quote, trade, update_q, update_t),
            candles=closed,
            indicators=values,
            latest_quote=quote,
            latest_trade=trade,
            developing_candles=developing,
            completed_bars_only=completed_bars_only,
            research=research,
        )
        self._versions[symbol] = context.version
        self._last_as_of[symbol] = as_of
        return context

    def _freshness(
        self,
        as_of: datetime,
        closed: Mapping[CandleTimeframe, tuple[Candle, ...]],
        quote: QuoteSnapshot | None,
        trade: TradeSnapshot | None,
        update_q: MarketDataUpdate | None,
        update_t: MarketDataUpdate | None,
    ) -> ContextFreshness:
        stamps = [s.provider_ts for s in (quote, trade) if s is not None]
        stamps += [seq[-1].end for seq in closed.values() if seq]
        if not stamps:
            return ContextFreshness(None, None, self._max_age, True, REASON_NO_DATA)
        newest = max(stamps)
        age = max(as_of - newest, _ZERO)
        if any(u is not None and u.stale for u in (update_q, update_t)):
            return ContextFreshness(newest, age, self._max_age, True, REASON_PROVIDER_STALE)
        if age > self._max_age:
            return ContextFreshness(newest, age, self._max_age, True, REASON_DATA_TOO_OLD)
        return ContextFreshness(newest, age, self._max_age, False, None)

    def _indicator(self, symbol: str, spec: IndicatorSpec) -> Decimal | None:
        calc = spec.factory()
        value: Decimal | None = None
        candles = self._candles.get_completed_candles(symbol, spec.timeframe, self._candle_limit)
        for candle in candles:
            value = self._feed_calc(calc, spec.input, candle)
        return value

    def _feed_calc(self, calc: Any, kind: IndicatorInput, candle: Candle) -> Decimal | None:
        result: Decimal | None
        if kind is IndicatorInput.CLOSE:
            result = calc.update(candle.close)
        elif kind is IndicatorInput.VOLUME:
            result = calc.update(candle.volume)
        elif kind is IndicatorInput.BAR:
            result = calc.update(candle)
        else:
            session_id = self._calendar.session_id(candle.start)
            if kind is IndicatorInput.SESSION_VOLUME:
                result = calc.update(candle.volume, session_id)
            else:
                result = calc.update(
                    _SessionBar(
                        candle.open, candle.high, candle.low, candle.close, candle.volume,
                        session_id,
                    )
                )
        return result


# ---------------------------------------------------------------------- selection


class SelectionStatus(Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"


@dataclass(frozen=True, slots=True)
class ContextSelection:
    """Result of :func:`select_context`; ``context`` is set only when FRESH."""

    status: SelectionStatus
    evaluation_time: datetime
    context: MarketContext | None
    reason: str | None
    age: timedelta | None = None

    def __post_init__(self) -> None:
        if (self.status is SelectionStatus.FRESH) != (self.context is not None):
            raise _invalid("only FRESH selections carry a context")
        if self.status is not SelectionStatus.FRESH and not self.reason:
            raise _invalid("STALE/MISSING selections require a reason")

    @property
    def is_fresh(self) -> bool:
        return self.status is SelectionStatus.FRESH


def select_context(
    history: Iterable[MarketContext], evaluation_time: datetime, max_age: timedelta
) -> ContextSelection:
    """Pick the newest context with ``as_of <= evaluation_time`` that is fresh.

    Fresh means ``evaluation_time - as_of <= max_age`` and the context's own data
    freshness is not flagged stale.  Contexts after ``evaluation_time`` are never
    considered.  If none qualifies the result is STALE (candidates existed) or MISSING
    (none at or before the evaluation time); a stale snapshot is never returned.
    """
    when = _utc(evaluation_time, "evaluation_time")
    if not isinstance(max_age, timedelta) or max_age <= _ZERO:
        raise _invalid("max_age must be a positive timedelta")
    contexts = tuple(history)
    if any(not isinstance(c, MarketContext) for c in contexts):
        raise _invalid("history must contain MarketContext objects")
    if len({c.symbol for c in contexts}) > 1:
        raise _invalid("history must hold a single symbol")
    eligible = [c for c in contexts if c.as_of <= when]
    if not eligible:
        return ContextSelection(SelectionStatus.MISSING, when, None, "NO_CONTEXT_AT_OR_BEFORE")
    newest = max(eligible, key=lambda c: (c.as_of, c.version))
    fresh = [c for c in eligible if when - c.as_of <= max_age and not c.freshness.stale]
    if fresh:
        chosen = max(fresh, key=lambda c: (c.as_of, c.version))
        return ContextSelection(
            SelectionStatus.FRESH, when, chosen, None, when - chosen.as_of
        )
    age = when - newest.as_of
    reason = newest.freshness.reason if age <= max_age else None
    reason = reason or REASON_DATA_TOO_OLD
    return ContextSelection(SelectionStatus.STALE, when, None, reason, age)


# ---------------------------------------------------------------------- binder


@dataclass(frozen=True, slots=True)
class BoundContexts:
    """Read-only view of exactly the symbols a strategy declared."""

    inputs: StrategyInputs
    _contexts: Mapping[str, MarketContext]

    @property
    def primary(self) -> MarketContext:
        return self._contexts[self.inputs.primary_symbol]

    @property
    def symbols(self) -> tuple[str, ...]:
        return self.inputs.symbols

    def __contains__(self, symbol: object) -> bool:
        return isinstance(symbol, str) and symbol.strip().upper() in self._contexts

    def __getitem__(self, symbol: str) -> MarketContext:
        key = symbol.strip().upper() if isinstance(symbol, str) else ""
        try:
            return self._contexts[key]
        except KeyError:
            raise UndeclaredSymbolError(
                f"symbol {symbol!r} was not declared by this strategy",
                code="undeclared_symbol",
                context={"symbol": key, "declared": self.inputs.symbols},
            ) from None

    def reference(self, symbol: str) -> MarketContext:
        """Return a declared *reference* symbol's context (not the primary)."""
        key = symbol.strip().upper() if isinstance(symbol, str) else ""
        if key == self.inputs.primary_symbol:
            raise UndeclaredSymbolError(
                f"{key} is the primary symbol, not a declared reference symbol",
                code="undeclared_symbol",
                context={"symbol": key, "declared": self.inputs.reference_symbols},
            )
        return self[symbol]


class ReferenceSymbolBinder:
    """Binds a strategy's declared inputs to the contexts of the declared symbols only."""

    def __init__(self, builder: ContextBuilder | None = None) -> None:
        self._builder = builder

    def bind(self, inputs: StrategyInputs, contexts: Mapping[str, MarketContext]) -> BoundContexts:
        """Bind already-built contexts; undeclared symbols in ``contexts`` are dropped."""
        if not isinstance(inputs, StrategyInputs):
            raise _invalid("inputs must be StrategyInputs")
        bound: dict[str, MarketContext] = {}
        for symbol in inputs.symbols:
            context = contexts.get(symbol)
            if not isinstance(context, MarketContext) or context.symbol != symbol:
                raise ContextBindingError(
                    f"no context supplied for declared symbol {symbol}",
                    code="missing_context",
                    context={"symbol": symbol},
                )
            bound[symbol] = self._restrict(inputs, context)
        return BoundContexts(inputs, MappingProxyType(bound))

    def build(
        self, inputs: StrategyInputs, *, research: ResearchContext | None = None
    ) -> BoundContexts:
        """Build and bind contexts for the declared symbols at one shared ``as_of``."""
        if self._builder is None:
            raise ContextBindingError("binder has no ContextBuilder", code="no_builder")
        return self.bind(inputs, self._builder.build_for_inputs(inputs, research=research))

    @staticmethod
    def _restrict(inputs: StrategyInputs, context: MarketContext) -> MarketContext:
        missing_tf = [tf.value for tf in inputs.timeframes if tf not in context.candles]
        missing_ind = [n for n in inputs.indicators if n not in context.indicators]
        if missing_tf or missing_ind:
            raise ContextBindingError(
                f"{context.symbol} context lacks declared inputs "
                f"timeframes={missing_tf!r} indicators={missing_ind!r}",
                code="missing_declared_input",
                context={"symbol": context.symbol},
            )
        developing = (
            {}
            if inputs.completed_bars_only
            else {
                tf: c
                for tf, c in (context.developing_candles or {}).items()
                if tf in inputs.timeframes
            }
        )
        return replace(
            context,
            candles={tf: context.candles[tf] for tf in inputs.timeframes},
            indicators={n: context.indicators[n] for n in inputs.indicators},
            developing_candles=developing,
            completed_bars_only=inputs.completed_bars_only,
        )
