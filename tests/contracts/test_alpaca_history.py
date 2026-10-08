"""Offline contract tests for historical acquisition and bounded REST polling."""

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mike_trade.market_data.alpaca_history import (
    Adjustment,
    AlpacaHistoricalBars,
    BarProvenanceError,
    HistoricalBar,
    PaginationLimitError,
    merge_bars,
)
from mike_trade.market_data.polling import PollKind, RestMarketDataPoller, TokenBucket

T0 = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)


def bar(timestamp: str, **extra: object) -> dict[str, object]:
    return {
        "t": timestamp,
        "o": 10.1,
        "h": 11.2,
        "l": 9.9,
        "c": 10.5,
        "v": 100,
        "vw": 10.4,
        "n": 12,
        **extra,
    }


class Pages:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def get(self, path: str, params: dict[str, object] | None = None) -> dict[str, object]:
        self.calls.append((path, params))
        return self.pages[len(self.calls) - 1]


def test_history_paginates_orders_bars_and_preserves_decimal_types() -> None:
    client = Pages(
        [
            {"bars": [bar("2026-01-02T15:01:00Z")], "next_page_token": "p2"},
            {"bars": [bar("2026-01-02T15:00:00Z")], "next_page_token": None},
        ]
    )
    bars = AlpacaHistoricalBars(client).fetch_bars(
        "aapl", "1Min", T0, T0 + timedelta(minutes=3), adjustment="split"
    )
    assert len(client.calls) == 2
    assert client.calls[1][1]["page_token"] == "p2"  # type: ignore[index]
    assert [item.timestamp for item in bars] == sorted(item.timestamp for item in bars)
    assert bars[0].timestamp.tzinfo is UTC
    assert bars[0].feed_id == "iex" and bars[0].adjustment is Adjustment.SPLIT
    for item in bars:
        assert all(
            isinstance(getattr(item, field), Decimal)
            for field in ("open", "high", "low", "close", "vwap")
        )
        assert isinstance(item.volume, (int, Decimal))
        assert not any(isinstance(getattr(item, field.name), float) for field in fields(item))


def test_history_honors_page_bound() -> None:
    client = Pages([{"bars": [], "next_page_token": "again"}] * 3)
    with pytest.raises(PaginationLimitError):
        AlpacaHistoricalBars(client, max_pages=2).fetch_bars(
            "AAPL", "1Min", T0, T0 + timedelta(minutes=1)
        )
    assert len(client.calls) == 2


def test_mixed_feed_or_adjustment_provenance_is_rejected() -> None:
    first = HistoricalBar(
        "AAPL",
        T0,
        Decimal("1"),
        Decimal("2"),
        Decimal("1"),
        Decimal("2"),
        1,
        Decimal("1.5"),
        1,
        "iex",
        Adjustment.RAW,
    )
    second = HistoricalBar(
        "AAPL",
        T0,
        Decimal("1"),
        Decimal("2"),
        Decimal("1"),
        Decimal("2"),
        1,
        Decimal("1.5"),
        1,
        "sip",
        Adjustment.RAW,
    )
    with pytest.raises(BarProvenanceError):
        merge_bars([first], [second])
    client = Pages([{"feed": "sip", "bars": []}])
    with pytest.raises(BarProvenanceError):
        AlpacaHistoricalBars(client).fetch_bars("AAPL", "1Min", T0, T0 + timedelta(minutes=1))


class FakeClock:
    def __init__(self) -> None:
        self.value = T0

    def now(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class PollTransport:
    def __init__(self, clock: FakeClock, *, rate_limit: bool = False) -> None:
        self.clock = clock
        self.calls: list[tuple[str, dict[str, object] | None]] = []
        self.rate_limit = rate_limit
        self.fixed_ts: datetime | None = None

    def get(self, path: str, params: dict[str, object] | None = None) -> object:
        self.calls.append((path, params))
        if self.rate_limit and len(self.calls) == 1:
            return Response(429, {"Retry-After": "3"})
        key = "trades"
        if "/quotes/" in path:
            key = "quotes"
        elif "/bars/" in path:
            key = "bars"
        timestamp = (self.fixed_ts or self.clock.now()).isoformat().replace("+00:00", "Z")
        payload = {"t": timestamp, "feed_id": "iex", "p": "10.25", "s": 2}
        return {key: {"AAPL": payload, "MSFT": payload}}


class Response:
    def __init__(self, status_code: int, headers: dict[str, str]) -> None:
        self.status_code = status_code
        self.headers = headers


def test_poll_cycle_batches_symbols_instead_of_polling_per_consumer() -> None:
    clock = FakeClock()
    client = PollTransport(clock)
    poller = RestMarketDataPoller(client, clock=clock, sleep=clock.sleep, kinds=(PollKind.TRADE,))
    snapshots = poller.poll_cycle(["AAPL", "MSFT"])
    assert len(client.calls) == 1
    assert client.calls[0][1]["symbols"] == "AAPL,MSFT"  # type: ignore[index]
    assert len(snapshots) == 2
    assert all(s.source == "rest_poll" and s.feed_id == "iex" for s in snapshots)


def test_token_bucket_defers_and_retry_after_delays_next_request() -> None:
    clock = FakeClock()
    delays: list[float] = []

    def sleep(seconds: float) -> None:
        delays.append(seconds)
        clock.sleep(seconds)

    limited = PollTransport(clock, rate_limit=True)
    poller = RestMarketDataPoller(
        limited, clock=clock, sleep=sleep, requests_per_second=1, burst=1, kinds=(PollKind.TRADE,)
    )
    poller.poll_cycle(["AAPL"])
    assert any(delay >= 3 for delay in delays)
    bucket = TokenBucket(capacity=1, refill_per_second=1, clock=clock.now, sleep=sleep)
    before = clock.now()
    bucket.acquire()
    bucket.acquire()
    assert (clock.now() - before).total_seconds() >= 1


def test_unchanged_or_old_provider_timestamp_is_stale() -> None:
    clock = FakeClock()
    client = PollTransport(clock)
    poller = RestMarketDataPoller(
        client, clock=clock, sleep=clock.sleep, kinds=(PollKind.TRADE,), stale_after_seconds=2
    )
    first = poller.poll_cycle(["AAPL"])[0]
    second = poller.poll_cycle(["AAPL"])[0]
    assert not first.stale
    assert second.stale
    clock.value += timedelta(seconds=3)
    client.fixed_ts = T0
    old = poller.poll_cycle(["AAPL"])[0]
    assert old.stale


def test_gap_recovery_is_bounded_and_uses_only_requested_window() -> None:
    clock = FakeClock()
    client = Pages([{"bars": [bar("2026-01-02T15:01:00Z")]}])
    poller = RestMarketDataPoller(
        client, clock=clock, sleep=clock.sleep, kinds=(PollKind.BAR,), max_gap=timedelta(minutes=5)
    )
    recovered = poller.recover_gap("AAPL", "1Min", T0, T0 + timedelta(minutes=2))
    assert len(recovered) == 1
    params = client.calls[0][1]
    assert params["start"] == "2026-01-02T15:00:00Z"  # type: ignore[index]
    assert params["end"] == "2026-01-02T15:02:00Z"  # type: ignore[index]
    with pytest.raises(ValueError, match="maximum"):
        poller.recover_gap("AAPL", "1Min", T0, T0 + timedelta(minutes=6))
