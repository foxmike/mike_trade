"""Bounded REST polling fallback for the shared market-data service."""

from __future__ import annotations

import email.utils
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from mike_trade.market_data.alpaca_history import AlpacaHistoricalBars, HistoricalBar

__all__ = ["PollKind", "PollSnapshot", "TokenBucket", "PollingError", "RestMarketDataPoller"]


class PollKind:
    TRADE = "trade"
    QUOTE = "quote"
    BAR = "bar"


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, "provider timestamp")
    if not isinstance(value, str):
        raise ValueError("provider timestamp must be ISO formatted")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return _utc(datetime.fromisoformat(text), "provider timestamp")


def _dec(value: Any) -> Decimal | int | None:
    if value is None:
        return None
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return Decimal(value)
    raise ValueError("snapshot numeric values must be int, Decimal, or string")


@dataclass(frozen=True, slots=True)
class PollSnapshot:
    symbol: str
    kind: str
    provider_source_ts: datetime
    receive_ts: datetime
    feed_id: str
    source: str
    stale: bool
    values: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(
            self, "provider_source_ts", _utc(self.provider_source_ts, "provider_source_ts")
        )
        object.__setattr__(self, "receive_ts", _utc(self.receive_ts, "receive_ts"))
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


class PollingError(RuntimeError):
    pass


class TokenBucket:
    """Synchronous token bucket driven by an injected datetime clock and sleeper."""

    def __init__(
        self,
        *,
        capacity: float,
        refill_per_second: float,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if capacity < 1 or refill_per_second <= 0:
            raise ValueError("capacity and refill_per_second must be positive")
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(capacity)
        self._updated = _utc(clock(), "clock")
        self._blocked_until = self._updated

    def defer(self, seconds: float) -> None:
        if seconds > 0:
            self._blocked_until = max(
                self._blocked_until, _utc(self._clock(), "clock") + timedelta(seconds=seconds)
            )

    def acquire(self) -> None:
        while True:
            now = _utc(self._clock(), "clock")
            if now < self._blocked_until:
                self._sleep((self._blocked_until - now).total_seconds())
                continue
            elapsed = max(0.0, (now - self._updated).total_seconds())
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
            self._updated = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            self._sleep((1 - self._tokens) / self.refill_per_second)


class _LimitedClient:
    def __init__(self, poller: RestMarketDataPoller) -> None:
        self._poller = poller

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._poller._get(path, params)


class RestMarketDataPoller:
    """Fetch each watched symbol once per market-data kind in one batched call."""

    _ENDPOINTS = {
        PollKind.TRADE: "/v2/stocks/trades/latest",
        PollKind.QUOTE: "/v2/stocks/quotes/latest",
        PollKind.BAR: "/v2/stocks/bars/latest",
    }

    def __init__(
        self,
        client: Any,
        *,
        clock: Any,
        feed: str = "iex",
        requests_per_second: float = 5.0,
        burst: int = 5,
        poll_interval_seconds: float = 1.0,
        max_poll_interval_seconds: float = 60.0,
        max_symbols_per_cycle: int = 100,
        stale_after_seconds: float = 30.0,
        max_gap: timedelta = timedelta(hours=1),
        sleep: Callable[[float], None] = time.sleep,
        kinds: Sequence[str] = (PollKind.TRADE, PollKind.QUOTE, PollKind.BAR),
    ) -> None:
        if not callable(getattr(client, "get", None)):
            raise TypeError("client must expose get(path, params)")
        if not callable(getattr(clock, "now", None)):
            raise TypeError("clock must expose now()")
        if poll_interval_seconds <= 0 or max_poll_interval_seconds <= 0:
            raise ValueError("poll intervals must be positive")
        if max_symbols_per_cycle < 1 or stale_after_seconds < 0 or max_gap <= timedelta(0):
            raise ValueError("poll bounds must be positive")
        if any(kind not in self._ENDPOINTS for kind in kinds) or not kinds:
            raise ValueError("kinds must contain trade, quote, and/or bar")
        self._client, self._clock, self.feed = client, clock, feed.strip().lower()
        self.poll_interval_seconds = min(poll_interval_seconds, max_poll_interval_seconds)
        self.max_poll_interval_seconds = max_poll_interval_seconds
        self.max_symbols_per_cycle = max_symbols_per_cycle
        self.stale_after_seconds = stale_after_seconds
        self.max_gap = max_gap
        self.kinds = tuple(dict.fromkeys(kinds))
        self._bucket = TokenBucket(
            capacity=burst, refill_per_second=requests_per_second, clock=clock.now, sleep=sleep
        )
        self._last_ts: dict[tuple[str, str], datetime] = {}
        self._retry_until: datetime | None = None
        self._sleep = sleep

    def _get(self, path: str, params: Mapping[str, Any] | None) -> Any:
        retries = 0
        while True:
            self._bucket.acquire()
            try:
                response = self._client.get(path, params=dict(params) if params else None)
            except Exception as exc:
                status = getattr(exc, "status_code", None) or getattr(
                    getattr(exc, "response", None), "status_code", None
                )
                if status != 429:
                    raise
                headers = getattr(exc, "headers", None) or getattr(
                    getattr(exc, "response", None), "headers", {}
                )
                delay = self._retry_after(headers)
                self._bucket.defer(delay)
                retries += 1
                if retries > 3:
                    raise PollingError("Alpaca remained rate limited after retries") from exc
                continue
            status = getattr(response, "status_code", None)
            if status == 429:
                delay = self._retry_after(getattr(response, "headers", {}))
                self._bucket.defer(delay)
                retries += 1
                if retries > 3:
                    raise PollingError("Alpaca remained rate limited after retries")
                continue
            if status is not None and status >= 400:
                raise PollingError(f"Alpaca request failed with HTTP {status}")
            return response.json() if callable(getattr(response, "json", None)) else response

    def _retry_after(self, headers: Mapping[str, Any]) -> float:
        value = next((v for k, v in headers.items() if str(k).lower() == "retry-after"), None)
        if value is None:
            return 1.0
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                return max(
                    0.0,
                    (
                        email.utils.parsedate_to_datetime(str(value)).astimezone(UTC)
                        - _utc(self._clock.now(), "clock")
                    ).total_seconds(),
                )
            except (TypeError, ValueError, OverflowError):
                return 1.0

    def poll_cycle(self, symbols: Sequence[str]) -> tuple[PollSnapshot, ...]:
        cleaned = tuple(dict.fromkeys(s.strip().upper() for s in symbols))
        if not cleaned or any(not s for s in cleaned):
            raise ValueError("symbols must be non-empty")
        if len(cleaned) > self.max_symbols_per_cycle:
            raise ValueError("symbol count exceeds max_symbols_per_cycle")
        snapshots: list[PollSnapshot] = []
        for kind in self.kinds:
            data = self._get(
                self._ENDPOINTS[kind], {"symbols": ",".join(cleaned), "feed": self.feed}
            )
            if not isinstance(data, Mapping):
                raise PollingError("latest snapshot response must be an object")
            field = {PollKind.TRADE: "trades", PollKind.QUOTE: "quotes", PollKind.BAR: "bars"}[kind]
            records = data.get(field, {})
            if not isinstance(records, Mapping):
                raise PollingError(f"response {field} must be an object keyed by symbol")
            receive_ts = _utc(self._clock.now(), "clock")
            for symbol in cleaned:
                item = records.get(symbol)
                if item is None:
                    continue
                if not isinstance(item, Mapping):
                    raise PollingError("snapshot item must be an object")
                source_ts = _parse_ts(item.get("t"))
                feed_id = str(item.get("feed_id", item.get("feed", self.feed))).lower()
                if feed_id != self.feed:
                    raise PollingError("snapshot feed differs from configured feed")
                key = (symbol, kind)
                stale = source_ts <= self._last_ts.get(key, source_ts - timedelta(microseconds=1))
                stale = stale or (receive_ts - source_ts).total_seconds() > self.stale_after_seconds
                self._last_ts[key] = source_ts
                values = {
                    k: (
                        _dec(v)
                        if k in {"p", "s", "bp", "bs", "ap", "as", "o", "h", "l", "c", "v", "vw"}
                        else v
                    )
                    for k, v in item.items()
                    if k not in {"t", "feed", "feed_id"}
                }
                snapshots.append(
                    PollSnapshot(
                        symbol, kind, source_ts, receive_ts, feed_id, "rest_poll", stale, values
                    )
                )
        return tuple(snapshots)

    def recover_gap(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> tuple[HistoricalBar, ...]:
        start_utc, end_utc = _utc(start, "start"), _utc(end, "end")
        if end_utc <= start_utc:
            raise ValueError("gap end must be after start")
        if end_utc - start_utc > self.max_gap:
            raise ValueError("gap window exceeds configured maximum")
        return AlpacaHistoricalBars(_LimitedClient(self), max_pages=20).fetch_bars(
            symbol, timeframe, start_utc, end_utc, feed=self.feed, adjustment="raw"
        )
