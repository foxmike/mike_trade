"""Offline contracts for the Alpaca market-data WebSocket adapter.

A fake transport replays synthetic Alpaca JSON frames; there is no network, no
real credentials, and no real sleeping.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from mike_trade.domain.errors import ConfigurationError
from mike_trade.domain.market import MarketEventKind
from mike_trade.market_data import alpaca_stream
from mike_trade.market_data.alpaca_stream import (
    AlpacaMarketDataStream,
    AlpacaStreamAuthError,
    AlpacaStreamEntitlementError,
    AlpacaStreamReconnectExhausted,
    AlpacaStreamSyntaxError,
    BackoffPolicy,
    Channel,
    ConnectionClosed,
    ConnectionState,
    StateChange,
    StreamBar,
    StreamCredentials,
    StreamQuote,
    StreamTrade,
    SubscriptionUpdate,
)

KEY = "FIXTURE-KEY-ID"
SECRET = "fixture-secret-value-do-not-log"
T0 = datetime(2026, 10, 8, 14, 30, 5, tzinfo=UTC)

BLOCK = object()  # recv() waits forever (until the stream is closed)


def frame(*messages: dict[str, Any] | str) -> str:
    """Encode messages as one Alpaca frame; raw strings keep exact number text."""
    parts = [m if isinstance(m, str) else json.dumps(m) for m in messages]
    return "[" + ",".join(parts) + "]"


WELCOME = frame({"T": "success", "msg": "connected"})
AUTHED = frame({"T": "success", "msg": "authenticated"})


def ack(**channels: list[str]) -> str:
    body: dict[str, Any] = {"T": "subscription", "trades": [], "quotes": [], "bars": []}
    body.update(channels)
    return frame(body)


def error_frame(code: int, msg: str) -> str:
    return frame({"T": "error", "code": code, "msg": msg})


class FakeConnection:
    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._stop = asyncio.Event()

    async def send(self, text: str) -> None:
        if self.closed:
            raise ConnectionClosed("closed")
        self.sent.append(json.loads(text))

    async def recv(self) -> str | bytes:
        if not self._script:
            raise ConnectionClosed("script exhausted")
        item = self._script.pop(0)
        if item is BLOCK:
            await self._stop.wait()
            raise ConnectionClosed("closed while blocked")
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, (str, bytes))
        return item

    async def close(self) -> None:
        self.closed = True
        self._stop.set()


class FakeTransport:
    """Each connect() consumes the next scripted connection (or raises it)."""

    def __init__(self, *scripts: list[Any] | BaseException) -> None:
        self._scripts = list(scripts)
        self.urls: list[str] = []
        self.connections: list[FakeConnection] = []

    async def connect(self, url: str) -> FakeConnection:
        self.urls.append(url)
        script = self._scripts.pop(0)
        if isinstance(script, BaseException):
            raise script
        conn = FakeConnection(script)
        self.connections.append(conn)
        return conn


class FakeClock:
    def __init__(self) -> None:
        self._now = T0

    def now(self) -> datetime:
        current = self._now
        self._now += timedelta(milliseconds=10)
        return current


class Harness:
    def __init__(self, *scripts: list[Any] | BaseException, **kwargs: Any) -> None:
        self.transport = FakeTransport(*scripts)
        self.delays: list[float] = []
        self.states: list[StateChange] = []

        async def sleep(delay: float) -> None:
            self.delays.append(delay)

        kwargs.setdefault("rng", lambda: 0.5)  # neutral jitter
        self.stream = AlpacaMarketDataStream(
            StreamCredentials(KEY, SECRET),
            self.transport,
            FakeClock(),
            sleep=sleep,
            on_state=self.states.append,
            **kwargs,
        )

    @property
    def state_names(self) -> list[str]:
        return [s.state.value for s in self.states]

    async def take(self, count: int) -> list[Any]:
        """Collect ``count`` events, then stop the stream."""
        out: list[Any] = []
        gen: AsyncIterator[Any] = self.stream.events()
        try:
            async for event in gen:
                out.append(event)
                if len(out) == count:
                    break
        finally:
            await gen.aclose()  # type: ignore[attr-defined]
        return out

    async def drain(self) -> list[Any]:
        """Run until the stream ends or raises (callers wrap in pytest.raises)."""
        return [event async for event in self.stream.events()]


def good_session(*frames: str, then: Any = BLOCK) -> list[Any]:
    return [WELCOME, AUTHED, *frames, then]


TRADE = '{"T":"t","S":"AAPL","i":96921,"x":"D","p":126.55,"s":100,"c":["@","I"],' \
    '"z":"C","t":"2026-10-08T14:30:04.208123456Z"}'
QUOTE = '{"T":"q","S":"AMD","bx":"U","bp":87.66,"bs":1,"ax":"Q","ap":87.68,"as":4,' \
    '"c":["R"],"z":"C","t":"2026-10-08T14:30:04.5Z"}'
BAR = '{"T":"b","S":"SPY","o":388.985,"h":389.13,"l":388.975,"c":389.12,"v":49378,' \
    '"t":"2026-10-08T14:29:00Z","n":461,"vw":389.0}'


# -- configuration -----------------------------------------------------------


async def test_feed_defaults_to_iex_and_connects_to_iex_url() -> None:
    h = Harness(good_session(TRADE))
    assert h.stream.feed_id == "iex"
    assert h.stream.url == "wss://stream.data.alpaca.markets/v2/iex"
    await h.take(1)
    assert h.transport.urls == ["wss://stream.data.alpaca.markets/v2/iex"]


async def test_sip_requires_explicit_entitlement_flag() -> None:
    with pytest.raises(ConfigurationError):
        Harness(feed="sip")
    with pytest.raises(ConfigurationError):
        Harness(feed="sip", sip_entitled=False)
    assert Harness(feed="sip", sip_entitled=True).stream.url.endswith("/v2/sip")
    with pytest.raises(ConfigurationError):
        Harness(feed="otc")


def test_credentials_never_expose_the_secret() -> None:
    creds = StreamCredentials(KEY, SECRET)
    assert SECRET not in repr(creds)
    assert SECRET not in str(creds)
    with pytest.raises(ValueError):
        StreamCredentials(KEY, " ")


# -- auth handshake -----------------------------------------------------------


async def test_auth_handshake_sends_injected_credentials_once() -> None:
    h = Harness(good_session(frame(json.loads(TRADE))))
    events = await h.take(1)
    assert isinstance(events[0], StreamTrade)
    assert h.transport.urls == ["wss://stream.data.alpaca.markets/v2/iex"]
    assert h.transport.connections[0].sent == [{"action": "auth", "key": KEY, "secret": SECRET}]
    assert h.state_names == ["CONNECTING", "AUTHENTICATED", "DISCONNECTED"]


async def test_secret_never_logged_or_emitted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    h = Harness(
        [WELCOME, error_frame(402, "auth failed")],
    )
    with pytest.raises(AlpacaStreamAuthError) as info:
        await h.drain()
    blob = caplog.text + repr(info.value) + str(info.value) + repr(h.states)
    assert SECRET not in blob


@pytest.mark.parametrize(
    ("code", "exc_type"),
    [
        (401, AlpacaStreamAuthError),
        (402, AlpacaStreamAuthError),
        (403, AlpacaStreamAuthError),
        (408, AlpacaStreamAuthError),
        (409, AlpacaStreamEntitlementError),
        (410, AlpacaStreamEntitlementError),
        (400, AlpacaStreamSyntaxError),
        (405, AlpacaStreamSyntaxError),
    ],
)
async def test_fatal_provider_errors_are_typed_and_not_retried(
    code: int, exc_type: type[Exception]
) -> None:
    # A second script is provided; retrying would consume it.
    h = Harness([WELCOME, error_frame(code, "nope")], good_session())
    with pytest.raises(exc_type) as info:
        await h.drain()
    assert info.value.provider_code == code  # type: ignore[attr-defined]
    assert len(h.transport.urls) == 1
    assert h.delays == []
    assert h.transport.connections[0].closed
    assert h.states[-1].state is ConnectionState.DISCONNECTED


async def test_entitlement_error_after_authentication_is_fatal() -> None:
    h = Harness(good_session(error_frame(409, "insufficient subscription")), good_session())
    await h.stream.subscribe([("AAPL", Channel.TRADES)])
    with pytest.raises(AlpacaStreamEntitlementError):
        await h.drain()
    assert len(h.transport.urls) == 1
    assert h.delays == []


async def test_connection_limit_406_retries_with_backoff_then_recovers() -> None:
    h = Harness(
        [WELCOME, error_frame(406, "connection limit exceeded")],
        [WELCOME, error_frame(406, "connection limit exceeded")],
        good_session(TRADE),
        backoff=BackoffPolicy(base_delay_seconds=1.0, max_delay_seconds=30.0, jitter_ratio=0.0),
    )
    events = await h.take(1)
    assert isinstance(events[0], StreamTrade)
    assert h.delays == [1.0, 2.0]
    assert len(h.transport.urls) == 3
    assert "RECONNECTING" in h.state_names


async def test_reconnect_gives_up_after_max_attempts() -> None:
    h = Harness(
        ConnectionRefusedError("down"),
        ConnectionRefusedError("down"),
        ConnectionRefusedError("down"),
        backoff=BackoffPolicy(base_delay_seconds=1.0, jitter_ratio=0.0, max_attempts=2),
    )
    with pytest.raises(AlpacaStreamReconnectExhausted):
        await h.drain()
    assert h.delays == [1.0, 2.0]
    assert len(h.transport.urls) == 3
    assert h.states[-1].state is ConnectionState.DISCONNECTED


# -- backoff ----------------------------------------------------------------


def test_backoff_is_exponential_capped_and_jittered() -> None:
    policy = BackoffPolicy(base_delay_seconds=1.0, max_delay_seconds=10.0, jitter_ratio=0.5)
    mid = [policy.delay_for_attempt(n, lambda: 0.5) for n in range(1, 7)]
    assert mid == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0]
    assert policy.delay_for_attempt(1, lambda: 0.0) == pytest.approx(0.5)
    assert policy.delay_for_attempt(1, lambda: 1.0) == pytest.approx(1.5)
    # Jitter can never push the delay past the configured cap.
    assert policy.delay_for_attempt(50, lambda: 1.0) == 10.0
    with pytest.raises(ValueError):
        policy.delay_for_attempt(0, lambda: 0.5)
    with pytest.raises(ValueError):
        BackoffPolicy(base_delay_seconds=5.0, max_delay_seconds=1.0)


# -- subscriptions ------------------------------------------------------------


def subs(h: Harness, index: int = 0) -> list[dict[str, Any]]:
    return [m for m in h.transport.connections[index].sent if m["action"] != "auth"]


async def test_subscribe_before_connect_sends_full_set_once_after_auth() -> None:
    h = Harness(good_session(ack(trades=["AAPL"], quotes=["AAPL", "MSFT"])))
    await h.stream.subscribe([("aapl", Channel.TRADES), ("AAPL", Channel.QUOTES)])
    await h.stream.subscribe([("MSFT", Channel.QUOTES)])
    assert h.transport.connections == []  # nothing sent without a connection
    await h.take(1)
    assert subs(h) == [
        {"action": "subscribe", "quotes": ["AAPL", "MSFT"], "trades": ["AAPL"]},
    ]


async def test_subscribe_is_idempotent_and_sends_only_the_diff() -> None:
    h = Harness(good_session(ack(trades=["AAPL"]), ack(trades=["AAPL", "MSFT"]), TRADE))
    await h.stream.subscribe([("AAPL", Channel.TRADES)])

    gen = h.stream.events()
    first = await anext(gen)  # initial ack
    assert isinstance(first, SubscriptionUpdate)
    assert len(subs(h)) == 1

    await h.stream.subscribe([("AAPL", Channel.TRADES)])  # already active
    assert len(subs(h)) == 1
    await h.stream.subscribe([("AAPL", Channel.TRADES), ("MSFT", Channel.TRADES)])
    assert subs(h)[1] == {"action": "subscribe", "trades": ["MSFT"]}
    assert len(subs(h)) == 2
    await gen.aclose()  # type: ignore[attr-defined]


async def test_unsubscribe_is_idempotent_and_updates_active_set() -> None:
    h = Harness(good_session(ack(trades=["AAPL", "MSFT"])))
    await h.stream.subscribe([("AAPL", Channel.TRADES), ("MSFT", Channel.TRADES)])
    gen = h.stream.events()
    await anext(gen)
    await h.stream.unsubscribe([("GOOG", Channel.TRADES)])  # never active
    assert len(subs(h)) == 1
    await h.stream.unsubscribe([("MSFT", Channel.TRADES)])
    assert subs(h)[1] == {"action": "unsubscribe", "trades": ["MSFT"]}
    await h.stream.unsubscribe([("MSFT", Channel.TRADES)])
    assert len(subs(h)) == 2
    assert h.stream.active_subscriptions == {("AAPL", Channel.TRADES)}
    await gen.aclose()  # type: ignore[attr-defined]


async def test_reconnect_resends_the_full_active_set() -> None:
    h = Harness(
        good_session(
            ack(trades=["AAPL"]), ack(trades=["AAPL", "MSFT"]), then=ConnectionClosed("drop")
        ),
        good_session(ack(trades=["AAPL", "MSFT"]), TRADE),
    )
    await h.stream.subscribe([("AAPL", Channel.TRADES)])
    gen = h.stream.events()
    await anext(gen)  # first ack
    await h.stream.subscribe([("MSFT", Channel.TRADES), ("SPY", Channel.BARS)])
    await h.stream.unsubscribe([("SPY", Channel.BARS)])
    events = [await anext(gen) for _ in range(3)]  # ack, (drop), ack, trade
    await gen.aclose()  # type: ignore[attr-defined]
    assert isinstance(events[-1], StreamTrade)
    assert subs(h, 1) == [
        {"action": "subscribe", "trades": ["AAPL", "MSFT"]},
    ]
    assert h.transport.connections[0].closed
    assert h.delays == [0.5]


async def test_invalid_subscriptions_are_rejected_locally() -> None:
    h = Harness()
    with pytest.raises(ValueError):
        await h.stream.subscribe([("not a symbol!", Channel.TRADES)])
    with pytest.raises(ValueError):
        await h.stream.subscribe([("AAPL", "trade_updates")])  # type: ignore[list-item]
    with pytest.raises(TypeError):
        await h.stream.subscribe("AAPL")  # type: ignore[arg-type]
    assert h.stream.active_subscriptions == frozenset()


# -- normalization -----------------------------------------------------------


async def test_trade_quote_bar_are_normalized_to_frozen_domain_events() -> None:
    h = Harness(good_session(frame(TRADE, QUOTE, BAR)))
    trade, quote, bar = await h.take(3)

    assert isinstance(trade, StreamTrade)
    assert (trade.symbol, trade.price, trade.size) == ("AAPL", Decimal("126.55"), Decimal(100))
    assert trade.sequence_id == 96921
    assert trade.feed_id == "iex"
    assert trade.conditions == ("@", "I")
    assert trade.provider_source_ts == datetime(2026, 10, 8, 14, 30, 4, 208123, tzinfo=UTC)
    assert trade.provider_source_ts.utcoffset() == timedelta(0)
    assert trade.receive_ts.tzinfo is not None
    assert trade.receive_ts > T0  # from the injected clock, not the provider

    assert isinstance(quote, StreamQuote)
    assert (quote.bid_price, quote.ask_price) == (Decimal("87.66"), Decimal("87.68"))
    assert (quote.bid_size, quote.ask_size) == (Decimal(1), Decimal(4))
    assert quote.provider_source_ts == datetime(2026, 10, 8, 14, 30, 4, 500000, tzinfo=UTC)

    assert isinstance(bar, StreamBar)
    assert (bar.open, bar.high, bar.low, bar.close) == (
        Decimal("388.985"), Decimal("389.13"), Decimal("388.975"), Decimal("389.12"),
    )
    assert (bar.volume, bar.trade_count, bar.vwap) == (Decimal(49378), 461, Decimal("389.0"))
    assert bar.provider_source_ts == datetime(2026, 10, 8, 14, 29, tzinfo=UTC)

    for event in (trade, quote, bar):
        with pytest.raises(FrozenInstanceError):
            event.symbol = "X"  # type: ignore[misc]


async def test_decimals_have_no_float_rounding_for_numbers_and_strings() -> None:
    exact = (
        '{"T":"t","S":"AAPL","p":0.1000000000000000055511151231257827,'
        '"s":"1.10","t":"2026-10-08T14:30:04Z"}'
    )
    h = Harness(good_session(frame(exact)))
    (trade,) = await h.take(1)
    assert trade.price == Decimal("0.1000000000000000055511151231257827")
    assert trade.size == Decimal("1.10")
    assert isinstance(trade.price, Decimal) and isinstance(trade.size, Decimal)


async def test_events_convert_to_domain_market_events_with_iex_provenance() -> None:
    h = Harness(good_session(frame(TRADE, QUOTE, BAR)))
    trade, quote, bar = await h.take(3)
    kinds = []
    for event in (trade, quote, bar):
        domain = event.to_market_event()
        kinds.append(domain.kind)
        assert domain.provenance.feed == "iex"
        assert domain.provenance.is_consolidated is False
        assert domain.event_time == event.provider_source_ts
        assert domain.received_at == event.receive_ts
    assert kinds == [MarketEventKind.TRADE, MarketEventKind.QUOTE, MarketEventKind.BAR]


async def test_sip_events_report_the_configured_feed() -> None:
    h = Harness(good_session(frame(TRADE)), feed="sip", sip_entitled=True)
    (trade,) = await h.take(1)
    assert trade.feed_id == "sip"
    assert trade.to_market_event().provenance.is_consolidated is True


async def test_subscription_ack_is_a_control_event_and_marks_subscribed() -> None:
    h = Harness(good_session(ack(trades=["aapl"], bars=["SPY"])))
    await h.stream.subscribe([("AAPL", Channel.TRADES), ("SPY", Channel.BARS)])
    (update,) = await h.take(1)
    assert isinstance(update, SubscriptionUpdate)
    assert update.subscriptions == {("AAPL", Channel.TRADES), ("SPY", Channel.BARS)}
    assert update.feed_id == "iex"
    assert h.state_names == ["CONNECTING", "AUTHENTICATED", "SUBSCRIBED", "DISCONNECTED"]


async def test_malformed_and_out_of_scope_messages_do_not_kill_the_stream() -> None:
    bad_price = '{"T":"t","S":"AAPL","p":"abc","s":1,"t":"2026-10-08T14:30:04Z"}'
    no_ts = '{"T":"t","S":"AAPL","p":1.5,"s":1}'
    naive_ts = '{"T":"t","S":"AAPL","p":1.5,"s":1,"t":"2026-10-08T14:30:04"}'
    out_of_scope = '{"T":"d","S":"AAPL","o":1,"h":1,"l":1,"c":1,"v":1}'
    order_update = '{"stream":"trade_updates","data":{"event":"fill"}}'
    h = Harness(good_session(frame(bad_price, no_ts, naive_ts, out_of_scope, order_update, TRADE)))
    (trade,) = await h.take(1)
    assert isinstance(trade, StreamTrade)
    assert h.stream.dropped_message_count == 3


# -- connection states ---------------------------------------------------------


async def test_state_transitions_are_emitted_across_a_reconnect() -> None:
    h = Harness(
        good_session(ack(trades=["AAPL"]), then=ConnectionClosed("drop")),
        good_session(ack(trades=["AAPL"]), TRADE),
    )
    await h.stream.subscribe([("AAPL", Channel.TRADES)])
    await h.take(3)
    assert h.state_names == [
        "CONNECTING",
        "AUTHENTICATED",
        "SUBSCRIBED",
        "DISCONNECTED",
        "RECONNECTING",
        "CONNECTING",
        "AUTHENTICATED",
        "SUBSCRIBED",
        "DISCONNECTED",
    ]
    reconnecting = h.states[4]
    assert reconnecting.attempt == 1 and reconnecting.delay_seconds == 0.5
    assert all(s.at.tzinfo is not None for s in h.states)


async def test_observer_failure_does_not_break_the_stream() -> None:
    h = Harness(good_session(TRADE))

    def boom(_: StateChange) -> None:
        raise RuntimeError("observer bug")

    h.stream._on_state = boom  # noqa: SLF001
    (trade,) = await h.take(1)
    assert isinstance(trade, StreamTrade)


async def test_transport_errors_during_receive_reconnect_with_backoff() -> None:
    h = Harness(
        good_session(then=OSError("reset")),
        ConnectionRefusedError("still down"),
        good_session(TRADE),
        backoff=BackoffPolicy(base_delay_seconds=1.0, max_delay_seconds=3.0, jitter_ratio=0.0),
    )
    (trade,) = await h.take(1)
    assert isinstance(trade, StreamTrade)
    assert h.delays == [1.0, 2.0]


# -- scope guards -------------------------------------------------------------


async def test_only_market_data_actions_are_ever_sent() -> None:
    h = Harness(good_session(ack(trades=["AAPL"], quotes=["AAPL"], bars=["AAPL"]), TRADE))
    await h.stream.subscribe(
        [("AAPL", Channel.TRADES), ("AAPL", Channel.QUOTES), ("AAPL", Channel.BARS)]
    )
    await h.take(2)
    sent = h.transport.connections[0].sent
    assert {m["action"] for m in sent} <= {"auth", "subscribe", "unsubscribe"}
    assert all(set(m) <= {"action", "key", "secret", "trades", "quotes", "bars"} for m in sent)
    assert {c.value for c in Channel} == {"trades", "quotes", "bars"}


async def test_second_events_iterator_cannot_open_another_connection() -> None:
    h = Harness(good_session(TRADE))
    gen = h.stream.events()
    await anext(gen)
    with pytest.raises(RuntimeError):
        await anext(h.stream.events())
    await gen.aclose()  # type: ignore[attr-defined]
    assert len(h.transport.urls) == 1


async def test_close_stops_the_stream_without_reconnecting() -> None:
    h = Harness(good_session(TRADE), good_session(TRADE))
    gen = h.stream.events()
    await anext(gen)
    await h.stream.close()
    with pytest.raises(StopAsyncIteration):
        await anext(gen)
    assert len(h.transport.urls) == 1
    assert h.transport.connections[0].closed
    assert h.delays == []


def test_module_has_no_broker_sdk_trade_updates_or_persistence_dependencies() -> None:
    source = Path(alpaca_stream.__file__).read_text()
    imports = [
        line.split("#")[0].strip()
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    ]
    joined = "\n".join(imports)
    for forbidden in ("alpaca", "brokers", "sqlite3", "persistence", "os\n", "strategy"):
        assert forbidden not in joined, forbidden
    assert "os.environ" not in source and "getenv" not in source
