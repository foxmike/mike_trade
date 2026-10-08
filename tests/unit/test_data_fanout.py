from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from mike_trade.domain.market import Candle, FeedProvenance, Timeframe
from mike_trade.market_data.alpaca_stream import (
    Channel,
    ConnectionState,
    StateChange,
    StreamTrade,
)
from mike_trade.market_data.cache import (
    DecisionEvidence,
    FeedMismatchError,
    MarketDataCache,
    MarketDataUpdate,
)
from mike_trade.market_data.polling import PollSnapshot
from mike_trade.market_data.service import MarketDataService

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
PAIR = ("AAPL", Channel.TRADES)


class Clock:
    def now(self) -> datetime:
        return NOW


class FakeStream:
    def __init__(self) -> None:
        self.subscribe_calls: list[frozenset[tuple[str, Channel]]] = []
        self.unsubscribe_calls: list[frozenset[tuple[str, Channel]]] = []
        self._events: asyncio.Queue[Any] = asyncio.Queue()
        self._observer: Any = None
        self.closed = False

    def set_state_observer(self, observer: Any) -> None:
        self._observer = observer

    async def subscribe(self, pairs: Any) -> None:
        self.subscribe_calls.append(frozenset(pairs))

    async def unsubscribe(self, pairs: Any) -> None:
        self.unsubscribe_calls.append(frozenset(pairs))

    async def events(self) -> Any:
        while not self.closed:
            yield await self._events.get()

    async def emit(self, event: Any) -> None:
        await self._events.put(event)

    def state(self, value: ConnectionState) -> None:
        assert self._observer is not None
        self._observer(StateChange(value, NOW))

    async def close(self) -> None:
        self.closed = True


class FakePoller:
    poll_interval_seconds = 0.01

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def poll_cycle(self, symbols: tuple[str, ...]) -> tuple[PollSnapshot, ...]:
        self.calls.append(tuple(symbols))
        return (
            PollSnapshot(
                "AAPL",
                "trade",
                NOW,
                NOW + timedelta(milliseconds=2),
                "iex",
                "rest_poll",
                False,
                {"p": Decimal("100"), "s": Decimal("1")},
            ),
        )


def service(*, queue_size: int = 4) -> tuple[MarketDataService, FakeStream, FakePoller]:
    stream, poller = FakeStream(), FakePoller()
    result = MarketDataService(
        stream,
        poller,
        MarketDataCache(),
        Clock(),
        consumer_queue_size=queue_size,
    )
    return result, stream, poller


def trade(*, feed_id: str = "iex", price: str = "101") -> StreamTrade:
    return StreamTrade(
        symbol="AAPL",
        provider_source_ts=NOW,
        receive_ts=NOW + timedelta(milliseconds=1),
        feed_id=feed_id,
        price=Decimal(price),
        size=Decimal("2"),
    )


@pytest.mark.asyncio
async def test_shared_subscription_is_upstream_once_and_reference_counted() -> None:
    manager, stream, _poller = service()

    await manager.register("shadow-one", [PAIR])
    await manager.register("shadow-two", [PAIR])
    await manager.register("paper", [PAIR])

    assert stream.subscribe_calls == [frozenset({PAIR})]
    assert manager.subscription_counts[PAIR] == 3

    await manager.unregister("shadow-one")
    await manager.unregister("paper")
    assert stream.unsubscribe_calls == []
    await manager.unregister("shadow-two")
    assert stream.unsubscribe_calls == [frozenset({PAIR})]


@pytest.mark.asyncio
async def test_fanout_delivers_same_frozen_update_to_every_consumer() -> None:
    manager, _stream, _poller = service()
    first = await manager.register("first", [PAIR])
    second = await manager.register("second", [PAIR])

    published = manager.ingest(trade(), source="stream")
    received_one, received_two = await first.get(), await second.get()

    assert received_one is published
    assert received_two is published
    with pytest.raises(FrozenInstanceError):
        received_one.source = "changed"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_slow_consumer_overflow_does_not_block_other_consumers() -> None:
    manager, _stream, _poller = service()
    slow = await manager.register("slow", [PAIR], queue_size=1)
    fast = await manager.register("fast", [PAIR], queue_size=5)

    for price in ("101", "102", "103"):
        manager.ingest(trade(price=price), source="stream")

    assert slow.status.overflow_count == 2
    assert slow.status.degraded is True
    received = [await asyncio.wait_for(fast.get(), 0.1) for _ in range(3)]
    assert [event.price for event in received] == [Decimal("101"), Decimal("102"), Decimal("103")]


@pytest.mark.asyncio
async def test_disconnect_uses_rest_poll_then_recovery_uses_stream() -> None:
    manager, stream, poller = service(queue_size=10)
    consumer = await manager.register("paper", [PAIR])
    await manager.start()
    try:
        stream.state(ConnectionState.DISCONNECTED)
        fallback = await asyncio.wait_for(consumer.get(), 1)
        assert fallback.source == "rest_poll"
        assert poller.calls[0] == ("AAPL",)
        assert manager.cache_snapshot("AAPL").freshness.source == "rest_poll"  # type: ignore[union-attr]

        stream.state(ConnectionState.AUTHENTICATED)
        await stream.emit(trade(price="102"))
        recovered = await asyncio.wait_for(consumer.get(), 1)
        assert recovered.source == "stream"
        assert manager.cache_snapshot("AAPL").freshness.source == "stream"  # type: ignore[union-attr]
        assert manager.fallback_active is False
    finally:
        await manager.close()


def test_cache_evidence_and_completed_candles_are_bounded() -> None:
    cache = MarketDataCache(
        max_evidence_per_symbol=3,
        max_completed_candles_per_timeframe=2,
    )
    provenance = FeedProvenance("alpaca", "iex", "stream", False)
    for index in range(10):
        at = NOW + timedelta(minutes=index)
        cache.append_evidence(DecisionEvidence("AAPL", str(index), index, at, at, "iex"))
        cache.append_completed_candle(
            Candle(
                "AAPL",
                Timeframe.M1,
                at,
                at + timedelta(minutes=1),
                Decimal("100"),
                Decimal("101"),
                Decimal("99"),
                Decimal("100"),
                Decimal("10"),
                provenance,
            )
        )

    assert [item.name for item in cache.decision_evidence("AAPL")] == ["7", "8", "9"]
    assert [item.start for item in cache.completed_candles("AAPL", Timeframe.M1)] == [
        NOW + timedelta(minutes=8),
        NOW + timedelta(minutes=9),
    ]


def test_cache_refuses_mixed_feed_until_explicit_reset() -> None:
    cache = MarketDataCache()
    cache.store_event(trade(feed_id="iex"), source="stream")

    with pytest.raises(FeedMismatchError, match="reset the feed first"):
        cache.store_event(trade(feed_id="sip"), source="stream")

    cache.reset_feed("AAPL", "sip")
    assert cache.store_event(trade(feed_id="sip"), source="stream").feed_id == "sip"


def test_cache_accessors_are_immutable_snapshots_with_freshness_metadata() -> None:
    cache = MarketDataCache()
    event = MarketDataUpdate(
        "AAPL",
        "quote",
        NOW,
        NOW + timedelta(milliseconds=3),
        "iex",
        "stream",
        {"bid": Decimal("100"), "ask": Decimal("101"), "nested": [1, 2]},
    )
    cache.store_event(event)

    snapshot = cache.snapshot("AAPL")
    assert snapshot.latest_quote is event
    assert snapshot.freshness is not None
    assert snapshot.freshness.provider_source_ts == NOW
    assert snapshot.freshness.receive_ts == NOW + timedelta(milliseconds=3)
    assert snapshot.freshness.feed_id == "iex"
    assert event.get("nested") == (1, 2)
    with pytest.raises(FrozenInstanceError):
        snapshot.feed_id = "sip"  # type: ignore[misc]
