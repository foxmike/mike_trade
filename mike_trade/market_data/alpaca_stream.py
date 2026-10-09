"""Alpaca real-time stock market-data WebSocket adapter (market data only).

One :class:`AlpacaMarketDataStream` owns exactly one authenticated connection.
The feed defaults to Alpaca Basic IEX (``iex``); ``sip`` is accepted only with
an explicit entitlement flag. The WebSocket transport, clock, sleeper, and
jitter source are all injected, so contract tests replay JSON frames without a
network, real credentials, or real time.

Scope guards:
* Only ``auth``/``subscribe``/``unsubscribe`` frames are ever sent, and only for
  the ``trades``/``quotes``/``bars`` channels. Broker trade/order updates use a
  separate, higher-priority channel and are neither carried nor imported here.
* Credentials are injected (:class:`StreamCredentials`); nothing is read from
  the environment and the secret never appears in ``repr``, logs, or errors.
* Raw ticks are normalized in memory and handed to the consumer; nothing is
  persisted here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum, StrEnum
from typing import Any, Protocol

from mike_trade.domain.errors import ConfigurationError, MikeTradeError
from mike_trade.domain.market import FeedProvenance, MarketEvent, MarketEventKind

__all__ = [
    "Channel",
    "ConnectionState",
    "StateChange",
    "StreamCredentials",
    "BackoffPolicy",
    "StreamTrade",
    "StreamQuote",
    "StreamBar",
    "SubscriptionUpdate",
    "StreamEvent",
    "Subscription",
    "ConnectionClosed",
    "MarketDataConnection",
    "MarketDataTransport",
    "WebsocketsTransport",
    "AlpacaStreamError",
    "AlpacaStreamAuthError",
    "AlpacaStreamEntitlementError",
    "AlpacaStreamConnectionLimitError",
    "AlpacaStreamSyntaxError",
    "AlpacaStreamProviderError",
    "AlpacaStreamReconnectExhausted",
    "AlpacaMarketDataStream",
]

_log = logging.getLogger(__name__)

_STREAM_BASE_URL = "wss://stream.data.alpaca.markets/v2"
_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")


class Channel(StrEnum):
    """Market-data channels this adapter can subscribe to."""

    TRADES = "trades"
    QUOTES = "quotes"
    BARS = "bars"


Subscription = tuple[str, Channel]


class ConnectionState(Enum):
    CONNECTING = "CONNECTING"
    AUTHENTICATED = "AUTHENTICATED"
    SUBSCRIBED = "SUBSCRIBED"
    DISCONNECTED = "DISCONNECTED"
    RECONNECTING = "RECONNECTING"


@dataclass(frozen=True, slots=True)
class StateChange:
    """One connection state transition delivered to the observer."""

    state: ConnectionState
    at: datetime
    attempt: int = 0
    delay_seconds: float | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class StreamCredentials:
    """Market-data credentials from the configured mode's credential source.

    The secret is excluded from ``repr`` and ``str``.
    """

    key_id: str
    secret_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.key_id, str) or not self.key_id.strip():
            raise ValueError("key_id must be a non-empty string")
        if not isinstance(self.secret_key, str) or not self.secret_key.strip():
            raise ValueError("secret_key must be a non-empty string")

    def __repr__(self) -> str:
        return f"StreamCredentials(key_id={self.key_id!r})"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Exponential backoff with jitter and a configurable cap.

    ``max_attempts`` bounds consecutive failed reconnects; ``None`` retries
    indefinitely (still delayed by at least the backoff each time).
    """

    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0
    jitter_ratio: float = 0.25
    max_attempts: int | None = None

    def __post_init__(self) -> None:
        for name in ("base_delay_seconds", "max_delay_seconds", "jitter_ratio"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite non-negative number")
            if not math.isfinite(float(value)) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")
        if self.jitter_ratio > 1:
            raise ValueError("jitter_ratio must be <= 1")
        if self.max_attempts is not None and (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValueError("max_attempts must be an int >= 1 or None")

    def delay_for_attempt(self, attempt: int, rng: Callable[[], float]) -> float:
        """Delay before reconnect ``attempt`` (1-based); never exceeds the cap."""
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt must be an int >= 1")
        # Clamp the exponent so very long outages cannot overflow the float.
        base = min(
            float(self.max_delay_seconds),
            float(self.base_delay_seconds) * (2.0 ** min(attempt - 1, 64)),
        )
        draw = float(rng())
        if not math.isfinite(draw) or not 0 <= draw <= 1:
            raise ValueError("rng must return a finite value from 0 through 1")
        base *= 1.0 + float(self.jitter_ratio) * (2.0 * draw - 1.0)
        return max(0.0, min(float(self.max_delay_seconds), base))


# --------------------------------------------------------------------------
# Transport port (injected)
# --------------------------------------------------------------------------


class ConnectionClosed(ConnectionError):
    """Raised by a transport connection when the socket is closed."""


class MarketDataConnection(Protocol):
    async def send(self, text: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


class MarketDataTransport(Protocol):
    async def connect(self, url: str) -> MarketDataConnection: ...


class WebsocketsTransport:
    """Production transport for :class:`AlpacaMarketDataStream`.

    Importing ``websockets`` is deferred until connection time so configuration
    inspection and offline modes never open or require a network resource.
    """

    def __init__(self, *, open_timeout: float = 15.0, close_timeout: float = 10.0) -> None:
        self.open_timeout = open_timeout
        self.close_timeout = close_timeout

    async def connect(self, url: str) -> MarketDataConnection:
        from websockets.asyncio.client import connect

        return await connect(
            url,
            open_timeout=self.open_timeout,
            close_timeout=self.close_timeout,
            ping_interval=20,
            ping_timeout=20,
        )


class _Clock(Protocol):
    def now(self) -> datetime: ...


# --------------------------------------------------------------------------
# Typed provider errors
# --------------------------------------------------------------------------


class AlpacaStreamError(MikeTradeError):
    """Base class for Alpaca market-data stream failures."""

    #: Fatal errors never trigger automatic reconnects.
    fatal: bool = False

    def __init__(self, message: str, *, provider_code: int | None = None) -> None:
        super().__init__(
            message,
            code=f"alpaca_stream_{provider_code}" if provider_code is not None else None,
        )
        self.provider_code = provider_code


class AlpacaStreamAuthError(AlpacaStreamError):
    """Authentication failed or was refused (401/402/403/408)."""

    fatal = True


class AlpacaStreamEntitlementError(AlpacaStreamError):
    """Insufficient subscription or feed not available (409/410)."""

    fatal = True


class AlpacaStreamSyntaxError(AlpacaStreamError):
    """Invalid syntax or symbol limit exceeded (400/405)."""

    fatal = True


class AlpacaStreamConnectionLimitError(AlpacaStreamError):
    """Connection limit exceeded (406); retried with backoff."""


class AlpacaStreamProviderError(AlpacaStreamError):
    """Other provider error (404 auth timeout, 407 slow client, 500, ...); retried."""


class AlpacaStreamReconnectExhausted(AlpacaStreamError):
    """The configured number of consecutive reconnect attempts was used up."""

    fatal = True


_ERROR_CLASSES: Mapping[int, type[AlpacaStreamError]] = {
    400: AlpacaStreamSyntaxError,
    401: AlpacaStreamAuthError,
    402: AlpacaStreamAuthError,
    403: AlpacaStreamAuthError,
    405: AlpacaStreamSyntaxError,
    406: AlpacaStreamConnectionLimitError,
    408: AlpacaStreamAuthError,
    409: AlpacaStreamEntitlementError,
    410: AlpacaStreamEntitlementError,
}


def _provider_error(message: Mapping[str, Any]) -> AlpacaStreamError:
    raw_code = message.get("code")
    code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
    text = message.get("msg")
    text = text if isinstance(text, str) and text else "provider error"
    cls = _ERROR_CLASSES.get(code, AlpacaStreamProviderError) if code is not None else (
        AlpacaStreamProviderError
    )
    return cls(f"Alpaca market-data stream error {code}: {text}", provider_code=code)


# --------------------------------------------------------------------------
# Normalized events
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StreamTrade:
    symbol: str
    provider_source_ts: datetime
    receive_ts: datetime
    feed_id: str
    price: Decimal
    size: Decimal
    sequence_id: int | None = None
    exchange: str | None = None
    conditions: tuple[str, ...] = ()
    tape: str | None = None

    def to_market_event(self) -> MarketEvent:
        return MarketEvent(
            symbol=self.symbol,
            kind=MarketEventKind.TRADE,
            event_time=self.provider_source_ts,
            received_at=self.receive_ts,
            provenance=_provenance(self.feed_id),
            price=self.price,
            size=self.size,
        )


@dataclass(frozen=True, slots=True)
class StreamQuote:
    symbol: str
    provider_source_ts: datetime
    receive_ts: datetime
    feed_id: str
    bid_price: Decimal
    bid_size: Decimal
    ask_price: Decimal
    ask_size: Decimal
    sequence_id: int | None = None
    bid_exchange: str | None = None
    ask_exchange: str | None = None
    conditions: tuple[str, ...] = ()
    tape: str | None = None

    def to_market_event(self) -> MarketEvent:
        return MarketEvent(
            symbol=self.symbol,
            kind=MarketEventKind.QUOTE,
            event_time=self.provider_source_ts,
            received_at=self.receive_ts,
            provenance=_provenance(self.feed_id),
            bid=self.bid_price,
            ask=self.ask_price,
            bid_size=self.bid_size,
            ask_size=self.ask_size,
        )


@dataclass(frozen=True, slots=True)
class StreamBar:
    """Minute bar; ``provider_source_ts`` is the bar start time."""

    symbol: str
    provider_source_ts: datetime
    receive_ts: datetime
    feed_id: str
    open: Decimal  # noqa: A003 - OHLC field names
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    vwap: Decimal | None = None
    trade_count: int | None = None
    sequence_id: int | None = None

    def to_market_event(self) -> MarketEvent:
        return MarketEvent(
            symbol=self.symbol,
            kind=MarketEventKind.BAR,
            event_time=self.provider_source_ts,
            received_at=self.receive_ts,
            provenance=_provenance(self.feed_id),
            price=self.close,
            size=self.volume,
        )


@dataclass(frozen=True, slots=True)
class SubscriptionUpdate:
    """Provider acknowledgement of the full current subscription set."""

    receive_ts: datetime
    feed_id: str
    subscriptions: frozenset[Subscription]


StreamEvent = StreamTrade | StreamQuote | StreamBar | SubscriptionUpdate


def _provenance(feed_id: str) -> FeedProvenance:
    return FeedProvenance(
        provider="alpaca",
        feed=feed_id,
        source="websocket",
        is_consolidated=feed_id == "sip",
    )


# --------------------------------------------------------------------------
# Wire parsing helpers
# --------------------------------------------------------------------------


def _dec(value: Any, *, field_name: str) -> Decimal:
    # JSON is parsed with parse_float=Decimal, so floats never reach here.
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field_name} must be a decimal string or JSON number")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation:
            raise ValueError(f"{field_name} is not a valid decimal") from None
    else:
        raise ValueError(f"{field_name} is missing or not numeric")
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


def _opt_dec(value: Any, *, field_name: str) -> Decimal | None:
    return None if value is None else _dec(value, field_name=field_name)


def _opt_int(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an int")
    return value


def _opt_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _conditions(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(c, str) for c in value):
        raise ValueError("conditions must be a list of strings")
    return tuple(value)


_FRACTION_RE = re.compile(r"(\.\d+)")


def _parse_ts(value: Any) -> datetime:
    """Parse an RFC 3339 provider timestamp (nanosecond precision) to aware UTC."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("provider timestamp 't' is missing")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Truncate sub-microsecond digits; datetime holds microseconds only.
    text = _FRACTION_RE.sub(lambda m: m.group(1)[:7].ljust(7, "0"), text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("provider timestamp 't' is not valid RFC 3339") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("provider timestamp 't' must be timezone-aware")
    return parsed.astimezone(UTC)


def _symbol(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("symbol 'S' is missing")
    normalized = value.strip().upper()
    if _SYMBOL_RE.fullmatch(normalized) is None:
        raise ValueError(f"symbol {value!r} is invalid")
    return normalized


def _normalize_symbol(value: str) -> str:
    try:
        return _symbol(value)
    except ValueError:
        raise ValueError(f"invalid symbol: {value!r}") from None


def _normalize_pairs(pairs: Iterable[Subscription]) -> frozenset[Subscription]:
    if isinstance(pairs, (str, bytes)):
        raise TypeError("pairs must be an iterable of (symbol, channel) tuples")
    result: set[Subscription] = set()
    for item in pairs:
        try:
            symbol, channel = item
        except (TypeError, ValueError):
            raise TypeError("each subscription must be a (symbol, channel) pair") from None
        result.add((_normalize_symbol(symbol), Channel(channel)))
    return frozenset(result)


def _wire_symbols(pairs: Iterable[Subscription]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for symbol, channel in sorted(pairs, key=lambda p: (p[1].value, p[0])):
        grouped.setdefault(channel.value, []).append(symbol)
    return grouped


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------

_TRANSIENT = (ConnectionError, EOFError, OSError)


class AlpacaMarketDataStream:
    """Single authenticated Alpaca stock market-data connection."""

    def __init__(
        self,
        credentials: StreamCredentials,
        transport: MarketDataTransport,
        clock: _Clock,
        *,
        feed: str = "iex",
        sip_entitled: bool = False,
        backoff: BackoffPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rng: Callable[[], float] | None = None,
        on_state: Callable[[StateChange], None] | None = None,
    ) -> None:
        if not isinstance(credentials, StreamCredentials):
            raise TypeError("credentials must be StreamCredentials")
        if not callable(getattr(transport, "connect", None)):
            raise TypeError("transport must expose connect(url)")
        if not callable(getattr(clock, "now", None)):
            raise TypeError("clock must expose now()")
        feed_id = feed.strip().lower() if isinstance(feed, str) else ""
        if feed_id not in ("iex", "sip"):
            raise ConfigurationError(f"unknown Alpaca data feed {feed!r}; expected iex or sip")
        if feed_id == "sip" and sip_entitled is not True:
            raise ConfigurationError(
                "the SIP feed requires an explicit entitlement flag (sip_entitled=True)"
            )
        self._credentials = credentials
        self._transport = transport
        self._clock = clock
        self._feed_id = feed_id
        self._url = f"{_STREAM_BASE_URL}/{feed_id}"
        self._backoff = backoff or BackoffPolicy()
        self._sleep = sleep or asyncio.sleep
        self._rng = rng or random.random
        self._on_state = on_state
        self._state: ConnectionState | None = None
        self._active: set[Subscription] = set()
        self._sent: set[Subscription] = set()
        self._conn: MarketDataConnection | None = None
        self._send_lock = asyncio.Lock()
        self._running = False
        self._closed = False
        self._dropped = 0

    # -- introspection ------------------------------------------------------

    @property
    def feed_id(self) -> str:
        return self._feed_id

    @property
    def url(self) -> str:
        return self._url

    @property
    def state(self) -> ConnectionState | None:
        return self._state

    @property
    def active_subscriptions(self) -> frozenset[Subscription]:
        return frozenset(self._active)

    @property
    def dropped_message_count(self) -> int:
        """Malformed known-type messages that were dropped without crashing."""
        return self._dropped

    # -- subscription API ---------------------------------------------------

    async def subscribe(self, pairs: Iterable[Subscription]) -> None:
        """Add (symbol, channel) pairs; already-active pairs send nothing."""
        wanted = _normalize_pairs(pairs)
        if wanted <= self._active:
            return
        self._active |= wanted
        await self._sync()

    async def unsubscribe(self, pairs: Iterable[Subscription]) -> None:
        """Remove (symbol, channel) pairs; inactive pairs send nothing."""
        unwanted = _normalize_pairs(pairs) & self._active
        if not unwanted:
            return
        self._active -= unwanted
        await self._sync()

    async def _sync(self) -> None:
        """Send only the diff between the active set and what the wire has."""
        async with self._send_lock:
            conn = self._conn
            if conn is None:
                return  # the full active set is sent after (re)connect
            try:
                while True:
                    adds = self._active - self._sent
                    removes = self._sent - self._active
                    if not adds and not removes:
                        return
                    if removes:
                        await self._send(conn, {"action": "unsubscribe", **_wire_symbols(removes)})
                        self._sent -= removes
                    if adds:
                        await self._send(conn, {"action": "subscribe", **_wire_symbols(adds)})
                        self._sent |= adds
            except _TRANSIENT:
                # The receive loop observes the drop; reconnect re-sends the full set.
                _log.debug("subscription sync deferred: connection dropped")

    # -- lifecycle ----------------------------------------------------------

    async def close(self) -> None:
        """Stop the stream; ``events()`` terminates and no reconnect happens."""
        self._closed = True
        await self._teardown()
        self._set_state(ConnectionState.DISCONNECTED, reason="closed")

    async def events(self) -> AsyncIterator[StreamEvent]:
        """Connect, authenticate, and yield normalized events, reconnecting on drops.

        Fatal provider errors (auth, entitlement, invalid syntax) are raised as
        typed exceptions and are never retried.
        """
        if self._running:
            raise RuntimeError("events() is already running; one connection per stream")
        self._running = True
        attempt = 0
        try:
            while not self._closed:
                failure: BaseException
                try:
                    self._set_state(ConnectionState.CONNECTING, attempt=attempt)
                    conn = await self._transport.connect(self._url)
                    try:
                        await self._handshake(conn)
                    except BaseException:
                        await _safe_close(conn)
                        raise
                    attempt = 0
                    self._sent.clear()
                    self._conn = conn
                    await self._sync()
                    while True:
                        raw = await conn.recv()
                        receive_ts = self._now()
                        for message in _decode_frame(raw):
                            event = self._handle(message, receive_ts)
                            if event is not None:
                                yield event
                except AlpacaStreamError as exc:
                    if exc.fatal:
                        raise
                    failure = exc
                except _TRANSIENT as exc:
                    failure = exc
                await self._teardown()
                if self._closed:
                    break
                self._set_state(ConnectionState.DISCONNECTED, reason=_describe(failure))
                attempt += 1
                limit = self._backoff.max_attempts
                if limit is not None and attempt > limit:
                    raise AlpacaStreamReconnectExhausted(
                        f"gave up after {limit} consecutive reconnect attempts"
                    ) from failure
                delay = self._backoff.delay_for_attempt(attempt, self._rng)
                self._set_state(
                    ConnectionState.RECONNECTING, attempt=attempt, delay_seconds=delay
                )
                await self._sleep(delay)
        finally:
            await self._teardown()
            self._set_state(ConnectionState.DISCONNECTED, reason="stopped")
            self._running = False

    # -- internals ----------------------------------------------------------

    def _now(self) -> datetime:
        now = self._clock.now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    def _set_state(
        self,
        state: ConnectionState,
        *,
        attempt: int = 0,
        delay_seconds: float | None = None,
        reason: str | None = None,
    ) -> None:
        if state is self._state:
            return
        self._state = state
        if self._on_state is None:
            return
        change = StateChange(
            state=state,
            at=self._now(),
            attempt=attempt,
            delay_seconds=delay_seconds,
            reason=reason,
        )
        try:
            self._on_state(change)
        except Exception:  # an observer bug must not take down the feed
            _log.exception("connection state observer failed")

    async def _teardown(self) -> None:
        conn, self._conn = self._conn, None
        self._sent.clear()
        if conn is not None:
            await _safe_close(conn)

    @staticmethod
    async def _send(conn: MarketDataConnection, payload: Mapping[str, Any]) -> None:
        await conn.send(json.dumps(payload))

    async def _handshake(self, conn: MarketDataConnection) -> None:
        """Expect the welcome frame, authenticate, and wait for the confirmation."""
        await self._expect_success(conn, "connected")
        await self._send(
            conn,
            {
                "action": "auth",
                "key": self._credentials.key_id,
                "secret": self._credentials.secret_key,
            },
        )
        await self._expect_success(conn, "authenticated")
        self._set_state(ConnectionState.AUTHENTICATED)

    async def _expect_success(self, conn: MarketDataConnection, msg: str) -> None:
        while True:
            for message in _decode_frame(await conn.recv()):
                kind = message.get("T")
                if kind == "error":
                    raise _provider_error(message)
                if kind == "success" and message.get("msg") == msg:
                    return
                if kind == "success":
                    continue
                raise AlpacaStreamProviderError(
                    f"unexpected frame type {kind!r} while waiting for {msg!r}"
                )

    def _handle(self, message: Mapping[str, Any], receive_ts: datetime) -> StreamEvent | None:
        kind = message.get("T")
        if kind == "error":
            raise _provider_error(message)
        if kind in ("success",):
            return None
        try:
            if kind == "t":
                return self._trade(message, receive_ts)
            if kind == "q":
                return self._quote(message, receive_ts)
            if kind == "b":
                return self._bar(message, receive_ts)
            if kind == "subscription":
                return self._subscription(message, receive_ts)
        except ValueError as exc:
            self._dropped += 1
            _log.warning("dropped malformed %r message: %s", kind, exc)
            return None
        return None  # other message types are outside this adapter's scope

    def _trade(self, m: Mapping[str, Any], receive_ts: datetime) -> StreamTrade:
        price = _dec(m.get("p"), field_name="p")
        size = _dec(m.get("s"), field_name="s")
        if price <= 0 or size < 0:
            raise ValueError("trade price must be > 0 and size >= 0")
        return StreamTrade(
            symbol=_symbol(m.get("S")),
            provider_source_ts=_parse_ts(m.get("t")),
            receive_ts=receive_ts,
            feed_id=self._feed_id,
            price=price,
            size=size,
            sequence_id=_opt_int(m.get("i"), field_name="i"),
            exchange=_opt_text(m.get("x")),
            conditions=_conditions(m.get("c")),
            tape=_opt_text(m.get("z")),
        )

    def _quote(self, m: Mapping[str, Any], receive_ts: datetime) -> StreamQuote:
        bid = _dec(m.get("bp"), field_name="bp")
        ask = _dec(m.get("ap"), field_name="ap")
        bid_size = _dec(m.get("bs"), field_name="bs")
        ask_size = _dec(m.get("as"), field_name="as")
        if min(bid, ask, bid_size, ask_size) < 0:
            raise ValueError("quote prices and sizes must be >= 0")
        return StreamQuote(
            symbol=_symbol(m.get("S")),
            provider_source_ts=_parse_ts(m.get("t")),
            receive_ts=receive_ts,
            feed_id=self._feed_id,
            bid_price=bid,
            bid_size=bid_size,
            ask_price=ask,
            ask_size=ask_size,
            sequence_id=_opt_int(m.get("i"), field_name="i"),
            bid_exchange=_opt_text(m.get("bx")),
            ask_exchange=_opt_text(m.get("ax")),
            conditions=_conditions(m.get("c")),
            tape=_opt_text(m.get("z")),
        )

    def _bar(self, m: Mapping[str, Any], receive_ts: datetime) -> StreamBar:
        o, h, lo, c = (_dec(m.get(k), field_name=k) for k in ("o", "h", "l", "c"))
        volume = _dec(m.get("v"), field_name="v")
        if min(o, h, lo, c) <= 0 or volume < 0:
            raise ValueError("bar prices must be > 0 and volume >= 0")
        return StreamBar(
            symbol=_symbol(m.get("S")),
            provider_source_ts=_parse_ts(m.get("t")),
            receive_ts=receive_ts,
            feed_id=self._feed_id,
            open=o,
            high=h,
            low=lo,
            close=c,
            volume=volume,
            vwap=_opt_dec(m.get("vw"), field_name="vw"),
            trade_count=_opt_int(m.get("n"), field_name="n"),
            sequence_id=_opt_int(m.get("i"), field_name="i"),
        )

    def _subscription(self, m: Mapping[str, Any], receive_ts: datetime) -> SubscriptionUpdate:
        acked: set[Subscription] = set()
        for channel in Channel:
            symbols = m.get(channel.value, [])
            if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
                raise ValueError(f"subscription {channel.value} must be a list of symbols")
            acked.update((s.upper(), channel) for s in symbols)
        if acked:
            self._set_state(ConnectionState.SUBSCRIBED)
        elif self._state is ConnectionState.SUBSCRIBED:
            self._set_state(ConnectionState.AUTHENTICATED)
        return SubscriptionUpdate(
            receive_ts=receive_ts, feed_id=self._feed_id, subscriptions=frozenset(acked)
        )


def _decode_frame(raw: str | bytes) -> list[Mapping[str, Any]]:
    """Decode one WebSocket frame: a JSON array of messages (or a single object)."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        payload = json.loads(text, parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise AlpacaStreamProviderError("received a frame that is not valid JSON") from None
    items = payload if isinstance(payload, list) else [payload]
    return [item for item in items if isinstance(item, dict)]


async def _safe_close(conn: MarketDataConnection) -> None:
    try:
        await conn.close()
    except Exception:  # closing a dead socket must not mask the original failure
        _log.debug("error while closing market-data connection", exc_info=True)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
