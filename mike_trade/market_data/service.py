"""Shared market-data subscription, ingestion, fallback, and fanout service."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from mike_trade.market_data.alpaca_stream import (
    Channel,
    ConnectionState,
    StateChange,
    Subscription,
    SubscriptionUpdate,
)
from mike_trade.market_data.cache import (
    MarketDataCache,
    MarketDataUpdate,
    SymbolCacheSnapshot,
    normalize_market_data,
)
from mike_trade.market_data.polling import RestMarketDataPoller

__all__ = [
    "ConsumerClosed",
    "ConsumerStatus",
    "ConsumerSubscription",
    "MarketDataService",
]


class ConsumerClosed(RuntimeError):
    """Raised when reading a consumer subscription after it is closed."""


@dataclass(frozen=True, slots=True)
class ConsumerStatus:
    consumer_id: str
    subscriptions: frozenset[Subscription]
    queue_size: int
    queue_capacity: int
    overflow_count: int
    degraded: bool
    closed: bool


def _consumer_id(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("consumer_id must be a non-empty string")
    return value.strip()


def _pairs(values: Iterable[Subscription]) -> frozenset[Subscription]:
    if isinstance(values, (str, bytes)):
        raise TypeError("subscriptions must contain (symbol, channel) pairs")
    result: set[Subscription] = set()
    for value in values:
        try:
            symbol, channel = value
        except (TypeError, ValueError):
            raise TypeError("each subscription must be a (symbol, channel) pair") from None
        normalized = symbol.strip().upper() if isinstance(symbol, str) else ""
        if not normalized:
            raise ValueError("subscription symbols must be non-empty")
        result.add((normalized, Channel(channel)))
    if not result:
        raise ValueError("at least one subscription is required")
    return frozenset(result)


def _channel_for(update: MarketDataUpdate) -> Channel:
    return {
        "trade": Channel.TRADES,
        "quote": Channel.QUOTES,
        "bar": Channel.BARS,
    }[update.kind]


class ConsumerSubscription:
    """Read-only consumer view backed by one bounded service-owned queue."""

    __slots__ = (
        "_closed",
        "_consumer_id",
        "_degraded",
        "_overflow_count",
        "_queue",
        "_subscriptions",
    )

    def __init__(
        self,
        consumer_id: str,
        subscriptions: frozenset[Subscription],
        queue_capacity: int,
    ) -> None:
        self._consumer_id = consumer_id
        self._subscriptions = subscriptions
        self._queue: asyncio.Queue[MarketDataUpdate] = asyncio.Queue(maxsize=queue_capacity)
        self._overflow_count = 0
        self._degraded = False
        self._closed = False

    @property
    def consumer_id(self) -> str:
        return self._consumer_id

    @property
    def subscriptions(self) -> frozenset[Subscription]:
        return self._subscriptions

    @property
    def overflow_count(self) -> int:
        return self._overflow_count

    @property
    def drop_count(self) -> int:
        """Alias emphasizing that an overflow drops the new event."""
        return self._overflow_count

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def status(self) -> ConsumerStatus:
        return ConsumerStatus(
            consumer_id=self._consumer_id,
            subscriptions=self._subscriptions,
            queue_size=self._queue.qsize(),
            queue_capacity=self._queue.maxsize,
            overflow_count=self._overflow_count,
            degraded=self._degraded,
            closed=self._closed,
        )

    async def get(self) -> MarketDataUpdate:
        if self._closed and self._queue.empty():
            raise ConsumerClosed(f"consumer {self._consumer_id!r} is closed")
        return await self._queue.get()

    def get_nowait(self) -> MarketDataUpdate:
        if self._closed and self._queue.empty():
            raise ConsumerClosed(f"consumer {self._consumer_id!r} is closed")
        return self._queue.get_nowait()

    async def events(self) -> AsyncIterator[MarketDataUpdate]:
        while not self._closed or not self._queue.empty():
            yield await self.get()

    def __aiter__(self) -> AsyncIterator[MarketDataUpdate]:
        return self.events()

    def _offer(self, update: MarketDataUpdate) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(update)
        except asyncio.QueueFull:
            self._overflow_count += 1
            self._degraded = True

    def _set_subscriptions(self, values: frozenset[Subscription]) -> None:
        self._subscriptions = values

    def _close(self) -> None:
        self._closed = True


class MarketDataService:
    """Acquire market data once and fan it out to isolated bounded consumers.

    ``stream`` must expose ``subscribe(pairs)``, ``unsubscribe(pairs)``,
    ``events()``, and ``close()``. ``poller`` must expose ``poll_cycle(symbols)``.
    Both concrete Alpaca adapters and small fakes satisfy this contract.
    """

    def __init__(
        self,
        stream: Any,
        poller: Any,
        cache: MarketDataCache,
        clock: Any,
        *,
        consumer_queue_size: int = 256,
        poll_interval_seconds: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        for name in ("subscribe", "unsubscribe", "events", "close"):
            if not callable(getattr(stream, name, None)):
                raise TypeError(f"stream must expose {name}()")
        if not callable(getattr(poller, "poll_cycle", None)):
            raise TypeError("poller must expose poll_cycle(symbols)")
        if not isinstance(cache, MarketDataCache):
            raise TypeError("cache must be MarketDataCache")
        if not callable(getattr(clock, "now", None)):
            raise TypeError("clock must expose now()")
        if (
            isinstance(consumer_queue_size, bool)
            or not isinstance(consumer_queue_size, int)
            or consumer_queue_size < 1
        ):
            raise ValueError("consumer_queue_size must be a positive integer")
        interval = (
            getattr(poller, "poll_interval_seconds", 1.0)
            if poll_interval_seconds is None
            else poll_interval_seconds
        )
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
            raise ValueError("poll_interval_seconds must be positive")

        self._stream = stream
        self._poller = poller
        self._cache = cache
        self._clock = clock
        self._consumer_queue_size = consumer_queue_size
        self._poll_interval = float(interval)
        self._sleep = sleep
        self._consumers: dict[str, ConsumerSubscription] = {}
        self._registry: dict[Subscription, set[str]] = {}
        self._subscription_lock = asyncio.Lock()
        self._fallback = asyncio.Event()
        self._subscriptions_changed = asyncio.Event()
        self._running = False
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._stream_error: BaseException | None = None
        self._install_state_observer()

    # -- lifecycle ------------------------------------------------------

    def _install_state_observer(self) -> None:
        callback = self.on_stream_state
        setter = getattr(self._stream, "set_state_observer", None)
        if callable(setter):
            setter(callback)
            return
        adder = getattr(self._stream, "add_state_observer", None)
        if callable(adder):
            adder(callback)
            return
        # AlpacaMarketDataStream receives its observer at construction.  Chain
        # that callback here so an existing operator observer is preserved.
        if hasattr(self._stream, "_on_state"):
            previous = self._stream._on_state

            def combined(change: StateChange) -> None:
                if callable(previous):
                    previous(change)
                callback(change)

            self._stream._on_state = combined

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("service is closed")
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._stream_task = asyncio.create_task(self._ingest_stream(), name="market-data-stream")
        self._poll_task = asyncio.create_task(self._poll_fallback(), name="market-data-poller")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._running = False
        self._fallback.set()
        self._subscriptions_changed.set()
        await self._stream.close()
        tasks = tuple(task for task in (self._stream_task, self._poll_task) if task is not None)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for consumer in self._consumers.values():
            consumer._close()

    async def __aenter__(self) -> MarketDataService:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # -- subscriptions --------------------------------------------------

    async def register(
        self,
        consumer_id: str,
        subscriptions: Iterable[Subscription],
        *,
        queue_size: int | None = None,
    ) -> ConsumerSubscription:
        consumer_id = _consumer_id(consumer_id)
        wanted = _pairs(subscriptions)
        capacity = self._consumer_queue_size if queue_size is None else queue_size
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("queue_size must be a positive integer")
        async with self._subscription_lock:
            current = self._consumers.get(consumer_id)
            existing = current.subscriptions if current is not None else frozenset()
            additions = wanted - existing
            first = frozenset(pair for pair in additions if pair not in self._registry)
            if first:
                await self._stream.subscribe(first)
            if current is None:
                current = ConsumerSubscription(consumer_id, wanted, capacity)
                self._consumers[consumer_id] = current
            else:
                current._set_subscriptions(existing | wanted)
            for pair in additions:
                self._registry.setdefault(pair, set()).add(consumer_id)
            self._subscriptions_changed.set()
            return current

    # A familiar alias for composition code.
    subscribe = register
    register_consumer = register

    async def unregister(
        self,
        consumer_id: str,
        subscriptions: Iterable[Subscription] | None = None,
    ) -> None:
        consumer_id = _consumer_id(consumer_id)
        async with self._subscription_lock:
            consumer = self._consumers.get(consumer_id)
            if consumer is None:
                return
            removals = consumer.subscriptions if subscriptions is None else (
                _pairs(subscriptions) & consumer.subscriptions
            )
            last = frozenset(
                pair for pair in removals if self._registry.get(pair) == {consumer_id}
            )
            if last:
                await self._stream.unsubscribe(last)
            for pair in removals:
                interested = self._registry.get(pair)
                if interested is None:
                    continue
                interested.discard(consumer_id)
                if not interested:
                    del self._registry[pair]
            remaining = consumer.subscriptions - removals
            consumer._set_subscriptions(remaining)
            if not remaining:
                consumer._close()
                del self._consumers[consumer_id]
            self._subscriptions_changed.set()

    unsubscribe = unregister
    unregister_consumer = unregister

    # -- ingest/fanout --------------------------------------------------

    async def _ingest_stream(self) -> None:
        try:
            async for event in self._stream.events():
                if isinstance(event, SubscriptionUpdate):
                    continue
                # Receiving an event is direct proof that stream data recovered,
                # even for simple injected adapters without a state callback.
                self._set_fallback(False)
                self.ingest(event, source="stream")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._stream_error = exc
            self._set_fallback(True)

    def ingest(self, event: Any, *, source: str | None = None) -> MarketDataUpdate:
        """Synchronously cache and fan out one adapter event."""
        update = self._cache.store_event(event, source=source)
        pair = (update.symbol, _channel_for(update))
        for consumer_id in tuple(self._registry.get(pair, ())):
            consumer = self._consumers.get(consumer_id)
            if consumer is not None:
                consumer._offer(update)
        return update

    publish = ingest

    async def poll_once(self) -> tuple[MarketDataUpdate, ...]:
        symbols = tuple(sorted({symbol for symbol, _channel in self._registry}))
        if not symbols:
            return ()
        if isinstance(self._poller, RestMarketDataPoller):
            # The production poller wraps a synchronous HTTP client and token
            # bucket; keep both away from the ingest/event-loop path.
            result = await asyncio.to_thread(self._poller.poll_cycle, symbols)
        else:
            result = self._poller.poll_cycle(symbols)
        if inspect.isawaitable(result):
            result = await result
        updates: list[MarketDataUpdate] = []
        for event in result:
            update = normalize_market_data(event, source="rest_poll")
            pair = (update.symbol, _channel_for(update))
            if pair not in self._registry:
                continue
            updates.append(self.ingest(update))
        return tuple(updates)

    async def _poll_fallback(self) -> None:
        try:
            while not self._closed:
                await self._fallback.wait()
                if self._closed:
                    return
                if not self._registry:
                    self._subscriptions_changed.clear()
                    await self._subscriptions_changed.wait()
                    continue
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A fallback failure does not kill later bounded cycles.
                    pass
                await self._sleep(self._poll_interval)
        except asyncio.CancelledError:
            raise

    # -- stream state ---------------------------------------------------

    def on_stream_state(self, change: StateChange | ConnectionState | Any) -> None:
        state = getattr(change, "state", change)
        try:
            state = state if isinstance(state, ConnectionState) else ConnectionState(state)
        except (TypeError, ValueError):
            return
        disconnected = state is ConnectionState.DISCONNECTED
        recovered = state in (ConnectionState.AUTHENTICATED, ConnectionState.SUBSCRIBED)
        if not disconnected and not recovered:
            return
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._set_fallback, disconnected)
        else:
            self._set_fallback(disconnected)

    handle_state_change = on_stream_state

    def _set_fallback(self, active: bool) -> None:
        if active:
            self._fallback.set()
        else:
            self._fallback.clear()

    # -- read-only views ------------------------------------------------

    @property
    def active_subscriptions(self) -> frozenset[Subscription]:
        return frozenset(self._registry)

    @property
    def subscription_counts(self) -> Mapping[Subscription, int]:
        counts = {pair: len(consumers) for pair, consumers in self._registry.items()}
        return MappingProxyType(counts)

    @property
    def fallback_active(self) -> bool:
        return self._fallback.is_set()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def stream_error(self) -> BaseException | None:
        return self._stream_error

    def consumer_status(self, consumer_id: str) -> ConsumerStatus | None:
        consumer = self._consumers.get(_consumer_id(consumer_id))
        return None if consumer is None else consumer.status

    def cache_snapshot(self, symbol: str) -> SymbolCacheSnapshot:
        return self._cache.snapshot(symbol)
