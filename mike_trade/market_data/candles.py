"""Deterministic multi-timeframe candle types and intraday builders with closed-bar finalization.

Pure in-memory aggregation: no network, provider, or broker I/O, and no raw tick
archive.  Only bounded per-(symbol, timeframe) candle history is retained.

Design choices:
1. ``Candle`` here is a separate value type from ``mike_trade.domain.market.Candle``.
   The domain type has no DEVELOPING/CLOSED status, price basis, or origin, and its
   ``Timeframe`` enum has no 30m member, so this module carries its own
   ``CandleTimeframe`` (1m/5m/15m/30m/60m).  Numeric/UTC validation reuses the
   public helpers in ``mike_trade.domain.instruments``.
2. Inputs are ``TradeTick`` (trade ticks) or ``MinuteBar`` (completed 1-minute bars).
   Both build the 1m series; 5m/15m/30m/60m aggregate only from closed 1m candles.
3. Buckets are half-open ``[start, end)`` and aligned from an America/New_York anchor
   (midnight by default).  ``session_open`` is a plain ``datetime.time`` or a
   ``Callable[[date], time | None]`` so 60m buckets can start at e.g. 09:30 ET; the
   h.3 calendar module is not imported.
4. A bucket is closed once ``end <= horizon`` where ``horizon`` is the newest input
   timestamp seen for the symbol, or ``clock.now() - grace`` when a clock is injected.
   Closed candles are never mutated; a later event for a closed bucket is counted as
   late and dropped.  A 1m bar's timestamp is its start, so it closes earlier buckets
   but not its own.
5. The first accepted input locks the symbol's (feed_id, consolidated, price_basis);
   any later input that differs raises ``SeriesMismatchError`` before state changes.
6. Daily (``D1``) and weekly (``W1``) candles are *session based*: their span is the exchange
   calendar's regular-session interval(s), not a fixed duration (early closes are shorter, a
   week has four or five sessions).  The :class:`Candle` value type validates that span against
   the NYSE calendar; they are never built by :class:`CandleBuilder` (which only aggregates
   intraday bars) but supplied by ``market_data.session_candles``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from typing import Protocol
from zoneinfo import ZoneInfo

from mike_trade.domain.errors import MikeTradeError
from mike_trade.domain.instruments import (
    require_non_negative,
    require_positive,
    require_utc,
)
from mike_trade.scheduling.calendar import is_trading_day, session_for

__all__ = [
    "CandleTimeframe",
    "INTRADAY_TIMEFRAMES",
    "SESSION_TIMEFRAMES",
    "PriceBasis",
    "CandleStatus",
    "CandleOrigin",
    "CandleError",
    "InvalidCandleInputError",
    "SeriesMismatchError",
    "BucketConflictError",
    "Candle",
    "TradeTick",
    "MinuteBar",
    "IngestResult",
    "ClockLike",
    "CandleBuilder",
]

NY = ZoneInfo("America/New_York")
_MINUTE = timedelta(minutes=1)


class CandleError(MikeTradeError):
    """Base class for candle-builder failures."""


class InvalidCandleInputError(CandleError, ValueError):
    """Raised for an invalid tick, bar, candle, or builder parameter."""


class SeriesMismatchError(CandleError):
    """Raised when an input's feed_id, consolidated flag, or price_basis differs from the series."""


class BucketConflictError(CandleError):
    """Raised when an input would overlay data already present in a 1m bucket."""


class CandleTimeframe(Enum):
    """Supported candle timeframes; ``value`` is the label, ``duration`` the bar length."""

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    M60 = "60m"
    #: Completed regular-session daily candle (calendar-defined span).
    D1 = "1d"
    #: Completed calendar-week candle spanning that week's regular sessions.
    W1 = "1w"

    @property
    def is_session_based(self) -> bool:
        """True for D1/W1: the span follows the exchange calendar, not a fixed duration."""
        return self in (CandleTimeframe.D1, CandleTimeframe.W1)

    @property
    def duration(self) -> timedelta:
        if self.is_session_based:
            raise ValueError(f"{self.value} candles have a calendar-defined span, not a duration")
        return timedelta(minutes=int(self.value[:-1]))


#: Fixed-duration timeframes the :class:`CandleBuilder` aggregates from 1-minute bars.
INTRADAY_TIMEFRAMES: tuple[CandleTimeframe, ...] = (
    CandleTimeframe.M1, CandleTimeframe.M5, CandleTimeframe.M15, CandleTimeframe.M30,
    CandleTimeframe.M60,
)
#: Calendar-defined timeframes supplied by ``market_data.session_candles``.
SESSION_TIMEFRAMES: tuple[CandleTimeframe, ...] = (CandleTimeframe.D1, CandleTimeframe.W1)


class PriceBasis(Enum):
    RAW = "RAW"
    SPLIT_ADJUSTED = "SPLIT_ADJUSTED"


class CandleStatus(Enum):
    DEVELOPING = "DEVELOPING"
    CLOSED = "CLOSED"


class CandleOrigin(Enum):
    STREAM_TRADE = "stream_trade"
    STREAM_BAR = "stream_bar"
    HISTORICAL_REST = "historical_rest"
    REPLAY = "replay"
    MIXED = "mixed"  # aggregated candle whose 1m constituents had differing origins


_TICK_ORIGINS = frozenset({CandleOrigin.STREAM_TRADE, CandleOrigin.REPLAY})
_BAR_ORIGINS = frozenset(
    {CandleOrigin.STREAM_BAR, CandleOrigin.HISTORICAL_REST, CandleOrigin.REPLAY}
)


class ClockLike(Protocol):
    """Structurally compatible with ``mike_trade.runtime.ClockPort``."""

    def now(self) -> datetime: ...


def _wrap[T](fn: Callable[[], T]) -> T:
    try:
        return fn()
    except InvalidCandleInputError:
        raise
    except ValueError as exc:
        raise InvalidCandleInputError(str(exc), code="invalid_candle_input") from exc


def _utc(value: datetime, field: str) -> datetime:
    return _wrap(lambda: require_utc(value, field=field))


def _positive(value: Decimal | int | str, field: str) -> Decimal:
    return _wrap(lambda: require_positive(value, field=field))


def _non_negative(value: Decimal | int | str, field: str) -> Decimal:
    return _wrap(lambda: require_non_negative(value, field=field))


def _invalid(message: str) -> InvalidCandleInputError:
    return InvalidCandleInputError(message, code="invalid_candle_input")


def _symbol(value: object) -> str:
    text = value.strip().upper() if isinstance(value, str) else ""
    if not text:
        raise _invalid("symbol must be a non-empty str")
    return text


def _provenance(
    feed_id: object, consolidated: object, price_basis: object
) -> tuple[str, bool, PriceBasis]:
    feed = feed_id.strip().lower() if isinstance(feed_id, str) else ""
    if not feed:
        raise _invalid("feed_id must be a non-empty str")
    if not isinstance(consolidated, bool):
        raise _invalid("consolidated must be bool")
    if feed == "iex" and consolidated:
        raise _invalid("IEX feed is not consolidated")
    if not isinstance(price_basis, PriceBasis):
        raise _invalid("price_basis must be PriceBasis")
    return feed, consolidated, price_basis


def _trade_count(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid("trade_count must be a non-negative int or None")
    return value


def _check_ohlc(o: Decimal, h: Decimal, lo: Decimal, c: Decimal) -> None:
    if h < lo:
        raise _invalid(f"high ({h!s}) must be >= low ({lo!s})")
    if not lo <= o <= h:
        raise _invalid(f"open ({o!s}) must be within [low, high]")
    if not lo <= c <= h:
        raise _invalid(f"close ({c!s}) must be within [low, high]")


def _check_session_span(timeframe: CandleTimeframe, start: datetime, end: datetime) -> None:
    """A D1/W1 candle spans exactly its regular session(s): open of the first to close of the last.

    D1 is one trading session; W1 is every trading session of one Monday-Friday calendar week
    (holiday weeks have fewer, early closes end earlier).  Anything else - midnight stamps, a
    non-trading day, a partial week - is rejected, so a candle can never silently cover a
    different span than the exchange calendar defines.
    """
    day = start.astimezone(NY).date()
    try:
        if timeframe is CandleTimeframe.D1:
            first = last = session_for(day)
        else:
            monday = day - timedelta(days=day.weekday())
            sessions = [
                session_for(monday + timedelta(days=i))
                for i in range(5)
                if is_trading_day(monday + timedelta(days=i))
            ]
            if not sessions:
                raise ValueError("no trading session in that week")
            first, last = sessions[0], sessions[-1]
    except (ValueError, TypeError) as exc:
        raise _invalid(f"{timeframe.value} candle does not start on a trading session: {exc}") from exc
    if start != first.open_utc or end != last.close_utc:
        raise _invalid(
            f"{timeframe.value} candle must span {first.open_utc.isoformat()} .. "
            f"{last.close_utc.isoformat()} (exchange-calendar regular session span)"
        )


@dataclass(frozen=True, slots=True)
class Candle:
    """One immutable OHLCV candle over the half-open UTC interval ``[start, end)``."""

    symbol: str
    timeframe: CandleTimeframe
    start: datetime
    end: datetime
    open: Decimal  # noqa: A003 - OHLC field names
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    feed_id: str
    consolidated: bool
    origin: CandleOrigin
    price_basis: PriceBasis
    status: CandleStatus
    trade_count: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        if not isinstance(self.timeframe, CandleTimeframe):
            raise _invalid("timeframe must be CandleTimeframe")
        start = _utc(self.start, "start")
        end = _utc(self.end, "end")
        if self.timeframe.is_session_based:
            _check_session_span(self.timeframe, start, end)
        elif end != start + self.timeframe.duration:
            raise _invalid("end must equal start + timeframe duration")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        o = _positive(self.open, "open")
        h = _positive(self.high, "high")
        lo = _positive(self.low, "low")
        c = _positive(self.close, "close")
        _check_ohlc(o, h, lo, c)
        for name, val in (("open", o), ("high", h), ("low", lo), ("close", c)):
            object.__setattr__(self, name, val)
        object.__setattr__(self, "volume", _non_negative(self.volume, "volume"))
        feed, _, _ = _provenance(self.feed_id, self.consolidated, self.price_basis)
        object.__setattr__(self, "feed_id", feed)
        if not isinstance(self.origin, CandleOrigin):
            raise _invalid("origin must be CandleOrigin")
        if not isinstance(self.status, CandleStatus):
            raise _invalid("status must be CandleStatus")
        object.__setattr__(self, "trade_count", _trade_count(self.trade_count))

    @property
    def is_closed(self) -> bool:
        return self.status is CandleStatus.CLOSED


@dataclass(frozen=True, slots=True)
class TradeTick:
    """One trade print used to build the 1m series."""

    symbol: str
    ts: datetime
    price: Decimal
    size: Decimal
    feed_id: str
    consolidated: bool
    price_basis: PriceBasis = PriceBasis.RAW
    origin: CandleOrigin = CandleOrigin.STREAM_TRADE

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "ts", _utc(self.ts, "ts"))
        object.__setattr__(self, "price", _positive(self.price, "price"))
        object.__setattr__(self, "size", _non_negative(self.size, "size"))
        feed, consolidated, _ = _provenance(self.feed_id, self.consolidated, self.price_basis)
        object.__setattr__(self, "feed_id", feed)
        object.__setattr__(self, "consolidated", consolidated)
        if self.origin not in _TICK_ORIGINS:
            raise _invalid(f"tick origin must be one of stream_trade/replay; got {self.origin!r}")


@dataclass(frozen=True, slots=True)
class MinuteBar:
    """One completed 1-minute bar; ``start`` is the minute boundary it covers."""

    symbol: str
    start: datetime
    open: Decimal  # noqa: A003 - OHLC field names
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    feed_id: str
    consolidated: bool
    price_basis: PriceBasis = PriceBasis.RAW
    origin: CandleOrigin = CandleOrigin.STREAM_BAR
    trade_count: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        start = _utc(self.start, "start")
        if start.second or start.microsecond:
            raise _invalid("start must be aligned to a minute boundary")
        object.__setattr__(self, "start", start)
        o = _positive(self.open, "open")
        h = _positive(self.high, "high")
        lo = _positive(self.low, "low")
        c = _positive(self.close, "close")
        _check_ohlc(o, h, lo, c)
        for name, val in (("open", o), ("high", h), ("low", lo), ("close", c)):
            object.__setattr__(self, name, val)
        object.__setattr__(self, "volume", _non_negative(self.volume, "volume"))
        feed, consolidated, _ = _provenance(self.feed_id, self.consolidated, self.price_basis)
        object.__setattr__(self, "feed_id", feed)
        object.__setattr__(self, "consolidated", consolidated)
        if self.origin not in _BAR_ORIGINS:
            raise _invalid(
                f"bar origin must be one of stream_bar/historical_rest/replay; got {self.origin!r}"
            )
        object.__setattr__(self, "trade_count", _trade_count(self.trade_count))


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Outcome of one ingest call.

    ``late`` is True when the input targeted an already-closed bucket; it was counted
    and dropped.  ``closed`` holds candles finalized by this call, ordered by
    timeframe duration then start.
    """

    late: bool
    closed: tuple[Candle, ...]


@dataclass(slots=True)
class _Acc:
    """Mutable accumulator for one developing bucket (never escapes this module)."""

    start: datetime
    end: datetime
    open: Decimal
    open_ts: datetime
    high: Decimal
    low: Decimal
    close: Decimal
    close_ts: datetime
    volume: Decimal
    trade_count: int | None
    origins: set[CandleOrigin]
    from_bar: bool = False

    def absorb(
        self,
        *,
        first_ts: datetime,
        last_ts: datetime,
        open_: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        volume: Decimal,
        trade_count: int | None,
        origins: set[CandleOrigin],
    ) -> None:
        if first_ts < self.open_ts:
            self.open, self.open_ts = open_, first_ts
        if last_ts >= self.close_ts:
            self.close, self.close_ts = close, last_ts
        self.high = max(self.high, high)
        self.low = min(self.low, low)
        self.volume += volume
        self.trade_count = (
            None if self.trade_count is None or trade_count is None
            else self.trade_count + trade_count
        )
        self.origins |= origins


@dataclass(slots=True)
class _Series:
    developing: dict[datetime, _Acc]
    history: deque[Candle]


@dataclass(slots=True)
class _SymbolState:
    lock: tuple[str, bool, PriceBasis] | None
    ts_horizon: datetime | None
    series: dict[CandleTimeframe, _Series]


SessionOpen = time | Callable[[date], time | None]


class CandleBuilder:
    """Builds 1m/5m/15m/30m/60m candles from trade ticks or completed 1m bars.

    The 1m series is always built (higher timeframes derive from it); ``timeframes``
    selects which series are additionally kept.  Accessors are pure: callers (or any
    ingest call) drive clock-based closing through :meth:`close_due`.
    """

    def __init__(
        self,
        *,
        timeframes: tuple[CandleTimeframe, ...] = INTRADAY_TIMEFRAMES,
        max_history: int = 500,
        grace: timedelta = timedelta(0),
        clock: ClockLike | None = None,
        session_open: SessionOpen | None = None,
    ) -> None:
        if not timeframes or not all(isinstance(t, CandleTimeframe) for t in timeframes):
            raise _invalid("timeframes must be a non-empty tuple of CandleTimeframe")
        if any(t.is_session_based for t in timeframes):
            raise _invalid(
                "daily/weekly candles are session based and are supplied by "
                "market_data.session_candles, not aggregated by CandleBuilder"
            )
        if isinstance(max_history, bool) or not isinstance(max_history, int) or max_history < 1:
            raise _invalid("max_history must be a positive int")
        if not isinstance(grace, timedelta) or grace < timedelta(0):
            raise _invalid("grace must be a non-negative timedelta")
        if clock is not None and not callable(getattr(clock, "now", None)):
            raise _invalid("clock must provide now()")
        if session_open is not None and not isinstance(session_open, time) \
                and not callable(session_open):
            raise _invalid("session_open must be a time, a callable, or None")
        if isinstance(session_open, time):
            self._check_anchor(session_open)
        self._timeframes = tuple(
            sorted({CandleTimeframe.M1, *timeframes}, key=lambda t: t.duration)
        )
        self._max_history = max_history
        self._grace = grace
        self._clock = clock
        self._session_open = session_open
        self._clock_horizon: datetime | None = None
        self._symbols: dict[str, _SymbolState] = {}
        self._late: dict[tuple[str, CandleTimeframe], int] = {}

    # ------------------------------------------------------------------ alignment

    @staticmethod
    def _check_anchor(anchor: time) -> time:
        if anchor.tzinfo is not None or anchor.second or anchor.microsecond:
            raise _invalid("session_open must be a naive whole-minute ET wall time")
        return anchor

    def bucket_start(self, ts: datetime, timeframe: CandleTimeframe) -> datetime:
        """Return the UTC start of the ``timeframe`` bucket containing ``ts``."""
        ts = _utc(ts, "ts")
        local_date = ts.astimezone(NY).date()
        anchor = time(0, 0)
        if isinstance(self._session_open, time):
            anchor = self._session_open
        elif self._session_open is not None:
            produced = self._session_open(local_date)
            if produced is not None:
                anchor = self._check_anchor(produced)
        anchor_utc = datetime.combine(local_date, anchor, tzinfo=NY).astimezone(UTC)
        return anchor_utc + ((ts - anchor_utc) // timeframe.duration) * timeframe.duration

    # ------------------------------------------------------------------ ingest

    def on_trade(self, tick: TradeTick) -> IngestResult:
        """Ingest one trade tick into the 1m series (and, on closure, higher series)."""
        if not isinstance(tick, TradeTick):
            raise _invalid("tick must be TradeTick")
        return self._ingest(
            tick.symbol, (tick.feed_id, tick.consolidated, tick.price_basis), tick.ts,
            first_ts=tick.ts, last_ts=tick.ts, open_=tick.price, high=tick.price,
            low=tick.price, close=tick.price, volume=tick.size, trade_count=1,
            origin=tick.origin, bar=False,
        )

    def on_minute_bar(self, bar: MinuteBar) -> IngestResult:
        """Ingest one completed 1-minute bar into the 1m series."""
        if not isinstance(bar, MinuteBar):
            raise _invalid("bar must be MinuteBar")
        return self._ingest(
            bar.symbol, (bar.feed_id, bar.consolidated, bar.price_basis), bar.start,
            first_ts=bar.start, last_ts=bar.start, open_=bar.open, high=bar.high,
            low=bar.low, close=bar.close, volume=bar.volume, trade_count=bar.trade_count,
            origin=bar.origin, bar=True,
        )

    def close_due(self) -> tuple[Candle, ...]:
        """Close every bucket with ``end + grace <= clock.now()``; no-op without a clock."""
        if self._clock is None:
            return ()
        self._clock_horizon = self._effective_clock_horizon()
        closed: list[Candle] = []
        for symbol in sorted(self._symbols):
            closed.extend(self._advance(symbol))
        return tuple(closed)

    def _effective_clock_horizon(self) -> datetime | None:
        if self._clock is None:
            return self._clock_horizon
        horizon = _utc(self._clock.now(), "clock.now()") - self._grace
        if self._clock_horizon is not None:
            horizon = max(horizon, self._clock_horizon)
        return horizon

    @staticmethod
    def _horizon(
        state: _SymbolState | None, clock_horizon: datetime | None
    ) -> datetime | None:
        candidates = [
            h for h in (clock_horizon, state.ts_horizon if state else None) if h is not None
        ]
        return max(candidates) if candidates else None

    def _ingest(
        self,
        symbol: str,
        provenance: tuple[str, bool, PriceBasis],
        ts: datetime,
        *,
        first_ts: datetime,
        last_ts: datetime,
        open_: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        volume: Decimal,
        trade_count: int | None,
        origin: CandleOrigin,
        bar: bool,
    ) -> IngestResult:
        state = self._symbols.get(symbol)
        # --- checks only; nothing below mutates state until the commit section ---
        if state is not None and state.lock is not None and state.lock != provenance:
            raise SeriesMismatchError(
                f"{symbol}: input (feed_id, consolidated, price_basis)={provenance!r} "
                f"does not match series {state.lock!r}",
                code="series_mismatch",
                context={"symbol": symbol},
            )
        clock_horizon = self._effective_clock_horizon()
        start = self.bucket_start(ts, CandleTimeframe.M1)
        end = start + _MINUTE
        horizon = self._horizon(state, clock_horizon)
        is_late = horizon is not None and end <= horizon
        if not is_late and state is not None:
            acc = state.series[CandleTimeframe.M1].developing.get(start)
            if acc is not None and (bar or acc.from_bar):
                raise BucketConflictError(
                    f"{symbol}: 1m bucket {start.isoformat()} already holds data",
                    code="bucket_conflict",
                    context={"symbol": symbol, "start": start.isoformat()},
                )
        # --- commit ---
        if state is None:
            state = self._symbols[symbol] = _SymbolState(
                lock=provenance, ts_horizon=None,
                series={
                    tf: _Series({}, deque(maxlen=self._max_history)) for tf in self._timeframes
                },
            )
        elif state.lock is None:
            state.lock = provenance
        self._clock_horizon = clock_horizon
        if is_late:
            key = (symbol, CandleTimeframe.M1)
            self._late[key] = self._late.get(key, 0) + 1
            return IngestResult(late=True, closed=self._advance(symbol))
        m1 = state.series[CandleTimeframe.M1]
        acc = m1.developing.get(start)
        if acc is None:
            m1.developing[start] = _Acc(
                start, end, open_, first_ts, high, low, close, last_ts, volume, trade_count,
                {origin}, bar,
            )
        else:
            acc.absorb(
                first_ts=first_ts, last_ts=last_ts, open_=open_, high=high, low=low,
                close=close, volume=volume, trade_count=trade_count, origins={origin},
            )
        if state.ts_horizon is None or ts > state.ts_horizon:
            state.ts_horizon = ts
        return IngestResult(late=False, closed=self._advance(symbol))

    # ------------------------------------------------------------------ finalization

    def _advance(self, symbol: str) -> tuple[Candle, ...]:
        state = self._symbols[symbol]
        horizon = self._horizon(state, self._clock_horizon)
        if horizon is None:
            return ()
        closed: list[Candle] = []
        m1 = state.series[CandleTimeframe.M1]
        for start in sorted(s for s, a in m1.developing.items() if a.end <= horizon):
            candle = self._finalize(symbol, CandleTimeframe.M1, state, m1.developing.pop(start))
            m1.history.append(candle)
            closed.append(candle)
            self._cascade(state, candle)
        for tf in self._timeframes[1:]:
            series = state.series[tf]
            for start in sorted(s for s, a in series.developing.items() if a.end <= horizon):
                candle = self._finalize(symbol, tf, state, series.developing.pop(start))
                series.history.append(candle)
                closed.append(candle)
        return tuple(closed)

    def _cascade(self, state: _SymbolState, minute: Candle) -> None:
        """Fold a just-closed 1m candle into every higher-timeframe developing bucket."""
        origins = {minute.origin}
        for tf in self._timeframes[1:]:
            start = self.bucket_start(minute.start, tf)
            end = start + tf.duration
            series = state.series[tf]
            acc = series.developing.get(start)
            if acc is None:
                series.developing[start] = _Acc(
                    start, end, minute.open, minute.start, minute.high, minute.low,
                    minute.close, minute.start, minute.volume, minute.trade_count, origins,
                )
            else:
                acc.absorb(
                    first_ts=minute.start, last_ts=minute.start, open_=minute.open,
                    high=minute.high, low=minute.low, close=minute.close,
                    volume=minute.volume, trade_count=minute.trade_count, origins=set(origins),
                )

    def _finalize(
        self, symbol: str, tf: CandleTimeframe, state: _SymbolState, acc: _Acc
    ) -> Candle:
        return self._snapshot(symbol, tf, state, acc, CandleStatus.CLOSED)

    @staticmethod
    def _snapshot(
        symbol: str, tf: CandleTimeframe, state: _SymbolState, acc: _Acc, status: CandleStatus
    ) -> Candle:
        assert state.lock is not None
        feed, consolidated, basis = state.lock
        origin = next(iter(acc.origins)) if len(acc.origins) == 1 else CandleOrigin.MIXED
        return Candle(
            symbol=symbol, timeframe=tf, start=acc.start, end=acc.end, open=acc.open,
            high=acc.high, low=acc.low, close=acc.close, volume=acc.volume, feed_id=feed,
            consolidated=consolidated, origin=origin, price_basis=basis, status=status,
            trade_count=acc.trade_count,
        )

    # ------------------------------------------------------------------ accessors

    def get_completed_candles(
        self, symbol: str, timeframe: CandleTimeframe, limit: int | None = None
    ) -> tuple[Candle, ...]:
        """Return CLOSED candles, oldest first.  Never includes a DEVELOPING candle."""
        series = self._series(symbol, timeframe)
        candles = tuple(series.history) if series else ()
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise _invalid("limit must be a non-negative int or None")
            candles = candles[len(candles) - limit:] if limit else ()
        return candles

    def get_latest_completed_candle(
        self, symbol: str, timeframe: CandleTimeframe
    ) -> Candle | None:
        series = self._series(symbol, timeframe)
        return series.history[-1] if series and series.history else None

    def get_developing_candle(self, symbol: str, timeframe: CandleTimeframe) -> Candle | None:
        """Return a snapshot of the newest DEVELOPING candle, or None.  Not for decisions."""
        series = self._series(symbol, timeframe)
        if not series or not series.developing:
            return None
        acc = series.developing[max(series.developing)]
        return self._snapshot(
            _symbol(symbol), timeframe, self._symbols[_symbol(symbol)], acc, CandleStatus.DEVELOPING
        )

    def late_count(self, symbol: str, timeframe: CandleTimeframe = CandleTimeframe.M1) -> int:
        return self._late.get((_symbol(symbol), timeframe), 0)

    def late_counts(self) -> Mapping[tuple[str, CandleTimeframe], int]:
        return dict(self._late)

    def _series(self, symbol: str, timeframe: CandleTimeframe) -> _Series | None:
        if not isinstance(timeframe, CandleTimeframe):
            raise _invalid("timeframe must be CandleTimeframe")
        state = self._symbols.get(_symbol(symbol))
        return state.series.get(timeframe) if state else None
