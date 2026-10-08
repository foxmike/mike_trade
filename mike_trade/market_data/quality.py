"""Market-data freshness and provenance health for new-entry decisions only.

This module exposes no controls over broker fills, order updates, stops,
reconciliation, or manual closes. Its health assessment can only be queried
through :meth:`FeedQualityTracker.entries_allowed` for prospective entries.
Snapshots are immutable values suitable for status projections and dashboards.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any

__all__ = [
    "HealthState", "ChannelFreshness", "FeedProvenanceSnapshot", "RateLimitSnapshot",
    "SymbolHealthSnapshot", "ServiceHealthSnapshot", "EntryDecision", "FeedQualityTracker",
]


class HealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ChannelFreshness:
    symbol: str
    channel: str
    provider_source_ts: datetime
    receive_ts: datetime
    age_seconds: float
    transport_latency_seconds: float
    source: str
    stale: bool
    out_of_order_count: int
    duplicate_count: int


@dataclass(frozen=True, slots=True)
class FeedProvenanceSnapshot:
    feed_id: str | None
    consolidated: bool | None
    configured_feed_id: str | None
    entitlement_mismatch: bool


@dataclass(frozen=True, slots=True)
class RateLimitSnapshot:
    requests_used: int
    responses_429: int
    backoff_until: datetime | None
    backoff_seconds: float


@dataclass(frozen=True, slots=True)
class SymbolHealthSnapshot:
    symbol: str
    state: HealthState
    channels: Mapping[str, ChannelFreshness]
    feed: FeedProvenanceSnapshot
    unresolved_gaps: tuple[datetime, ...]
    stream_connected: bool
    stream_gap: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ServiceHealthSnapshot:
    state: HealthState
    stream_connected: bool
    rate_limit: RateLimitSnapshot
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EntryDecision:
    allowed: bool
    reason_code: str | None


class FeedQualityTracker:
    """Track data health; only ``entries_allowed`` expresses entry eligibility.

    The tracker cannot block broker fills, order updates, stops, reconciliation,
    or manual closes.

    ``session_calendar`` returns whether regular-session freshness rules apply
    at the supplied UTC time. Staleness thresholds are seconds by channel.
    Events may be normalized stream records, poll snapshots, or MarketEvent-like
    values carrying equivalent timestamp/provenance fields.
    """

    def __init__(
        self,
        *,
        clock: Any,
        staleness_seconds: Mapping[str, float],
        session_calendar: Callable[[datetime], bool],
        configured_feed_id: str | None = None,
        seen_window: int = 4096,
        stream_activity_gap_seconds: float = 30.0,
    ) -> None:
        if not callable(getattr(clock, "now", None)):
            raise TypeError("clock must expose now()")
        if not callable(session_calendar):
            raise TypeError("session_calendar must be callable")
        if seen_window < 1 or stream_activity_gap_seconds < 0:
            raise ValueError("seen_window must be positive and stream gap non-negative")
        if any(float(v) < 0 for v in staleness_seconds.values()):
            raise ValueError("staleness thresholds must be non-negative")
        self._clock = clock
        self._thresholds = {str(k).lower(): float(v) for k, v in staleness_seconds.items()}
        self._session = session_calendar
        self._configured_feed = configured_feed_id.lower() if configured_feed_id else None
        self._seen_window = seen_window
        self._stream_gap_seconds = float(stream_activity_gap_seconds)
        self._freshness: dict[tuple[str, str], ChannelFreshness] = {}
        self._latest_source_ts: dict[tuple[str, str], datetime] = {}
        self._seen: set[tuple[Any, ...]] = set()
        self._seen_order: deque[tuple[Any, ...]] = deque()
        self._feed_id: str | None = None
        self._stream_connected = True
        self._last_stream_activity: datetime | None = None
        self._gaps: dict[str, set[datetime]] = {}
        self._last_bar: dict[str, datetime] = {}
        self._requests_used = 0
        self._responses_429 = 0
        self._backoff_until: datetime | None = None

    def ingest(self, event: Any, *, channel: str | None = None, source: str = "stream") -> bool:
        """Record an event; returns False for a duplicate (not fresh activity)."""
        symbol = str(getattr(event, "symbol", "")).strip().upper()
        if not symbol:
            raise ValueError("event must have a symbol")
        ts = getattr(event, "provider_source_ts", None)
        if ts is None:
            ts = getattr(event, "event_time", None)
        receive = getattr(event, "receive_ts", None)
        if receive is None:
            receive = getattr(event, "received_at", None)
        if ts is None or receive is None:
            raise ValueError("event must have provider source and receive timestamps")
        ts, receive = _utc(ts, "provider_source_ts"), _utc(receive, "receive_ts")
        kind = channel or getattr(event, "kind", "unknown")
        kind = getattr(kind, "value", kind)
        kind = str(kind).lower()
        kind = {"trade": "trades", "quote": "quotes", "bar": "bars"}.get(kind, kind)
        feed_id = getattr(event, "feed_id", None)
        provenance = getattr(event, "provenance", None)
        if feed_id is None and provenance is not None:
            feed_id = getattr(provenance, "feed", None)
        if feed_id:
            self._feed_id = str(feed_id).strip().lower()

        sequence = getattr(event, "sequence_id", None)
        trade_id = getattr(event, "trade_id", None)
        price = getattr(event, "price", getattr(event, "close", None))
        size = getattr(event, "size", getattr(event, "volume", None))
        if price is None and hasattr(event, "close"):
            price = event.close
        if sequence is not None or trade_id is not None:
            identity = ("id", symbol, kind, sequence if sequence is not None else trade_id)
        else:
            identity = ("fallback", symbol, ts, str(price), str(size))
        if identity in self._seen:
            key = (symbol, kind)
            previous = self._freshness.get(key)
            if previous:
                self._freshness[key] = ChannelFreshness(
                    previous.symbol, previous.channel, previous.provider_source_ts,
                    previous.receive_ts, previous.age_seconds, previous.transport_latency_seconds,
                    previous.source, previous.stale, previous.out_of_order_count,
                    previous.duplicate_count + 1,
                )
            return False
        self._seen.add(identity)
        self._seen_order.append(identity)
        if len(self._seen_order) > self._seen_window:
            self._seen.discard(self._seen_order.popleft())

        now = _utc(self._clock.now(), "clock")
        key = (symbol, kind)
        prior_ts = self._latest_source_ts.get(key)
        out_of_order = prior_ts is not None and ts < prior_ts
        if out_of_order:
            old = self._freshness.get(key)
            if old:
                self._freshness[key] = ChannelFreshness(
                    old.symbol, old.channel, old.provider_source_ts, old.receive_ts,
                    old.age_seconds, old.transport_latency_seconds, old.source, old.stale,
                    old.out_of_order_count + 1, old.duplicate_count,
                )
            if source == "stream":
                self._last_stream_activity = receive
            return True
        if not out_of_order:
            self._latest_source_ts[key] = ts
        old = self._freshness.get(key)
        threshold = self._thresholds.get(kind)
        stale = bool(threshold is not None and self._session(now) and (now - ts).total_seconds() > threshold)
        self._freshness[key] = ChannelFreshness(
            symbol, kind, ts, receive, (now - ts).total_seconds(),
            (receive - ts).total_seconds(), source, stale,
            (old.out_of_order_count if old else 0) + int(out_of_order),
            old.duplicate_count if old else 0,
        )
        if source == "stream":
            self._last_stream_activity = receive
        if kind == "bars":
            self._record_bar(symbol, ts, now)
        return True

    def _record_bar(self, symbol: str, ts: datetime, now: datetime) -> None:
        previous = self._last_bar.get(symbol)
        if previous is not None and ts > previous:
            cursor = previous + timedelta(minutes=1)
            while cursor < ts:
                if cursor + timedelta(minutes=1) <= now:
                    self._gaps.setdefault(symbol, set()).add(cursor)
                cursor += timedelta(minutes=1)
        if previous is None or ts > previous:
            self._last_bar[symbol] = ts

    def mark_gap_recovered(self, symbol: str, bar_start: datetime) -> None:
        self._gaps.get(symbol.strip().upper(), set()).discard(_utc(bar_start, "bar_start"))

    def set_stream_connected(self, connected: bool) -> None:
        self._stream_connected = bool(connected)

    def record_request(self, *, status_code: int | None = None, backoff_seconds: float = 0) -> None:
        self._requests_used += 1
        now = _utc(self._clock.now(), "clock")
        if status_code == 429:
            self._responses_429 += 1
            if backoff_seconds > 0:
                until = now + timedelta(seconds=float(backoff_seconds))
                self._backoff_until = max(self._backoff_until or until, until)

    def rate_limit_snapshot(self) -> RateLimitSnapshot:
        now = _utc(self._clock.now(), "clock")
        until = self._backoff_until if self._backoff_until and self._backoff_until > now else None
        return RateLimitSnapshot(
            self._requests_used, self._responses_429, until,
            max(0.0, (until - now).total_seconds()) if until else 0.0,
        )

    def _stream_gap(self, now: datetime) -> bool:
        return bool(self._last_stream_activity and self._session(now) and
                    (now - self._last_stream_activity).total_seconds() > self._stream_gap_seconds)

    def symbol_health(self, symbol: str) -> SymbolHealthSnapshot:
        symbol, now = symbol.strip().upper(), _utc(self._clock.now(), "clock")
        channels: dict[str, ChannelFreshness] = {}
        reasons: list[str] = []
        for (sym, channel), item in self._freshness.items():
            if sym != symbol:
                continue
            threshold = self._thresholds.get(channel)
            stale = bool(threshold is not None and self._session(now) and
                         (now - item.provider_source_ts).total_seconds() > threshold)
            channels[channel] = ChannelFreshness(
                item.symbol, item.channel, item.provider_source_ts, item.receive_ts,
                (now - item.provider_source_ts).total_seconds(),
                (item.receive_ts - item.provider_source_ts).total_seconds(), item.source,
                stale, item.out_of_order_count, item.duplicate_count,
            )
            if stale:
                reasons.append("STALE_DATA")
        feed_id = self._feed_id
        mismatch = bool(feed_id and self._configured_feed and feed_id != self._configured_feed)
        provenance = FeedProvenanceSnapshot(
            feed_id, False if feed_id == "iex" else (True if feed_id == "sip" else None),
            self._configured_feed, mismatch,
        )
        gaps = tuple(sorted(self._gaps.get(symbol, ())))
        if gaps:
            reasons.append("UNRESOLVED_GAP")
        if mismatch:
            reasons.append("ENTITLEMENT_MISMATCH")
        if feed_id not in {"iex", "sip"}:
            reasons.append("UNKNOWN_FEED_ID")
        fresh_fallback = any(c.source != "stream" and not c.stale for c in channels.values())
        gap = self._stream_gap(now) and not (not self._stream_connected and fresh_fallback)
        if gap:
            reasons.append("STREAM_ACTIVITY_GAP")
        if not self._stream_connected and not any(c.source != "stream" and not c.stale for c in channels.values()):
            reasons.append("STREAM_DISCONNECTED")
        if not channels:
            reasons.append("UNKNOWN_FRESHNESS")
        state = HealthState.OFFLINE if not channels or (not self._stream_connected and "STREAM_DISCONNECTED" in reasons) else (
            HealthState.DEGRADED if reasons else HealthState.HEALTHY
        )
        return SymbolHealthSnapshot(symbol, state, MappingProxyType(channels), provenance,
                                    gaps, self._stream_connected, gap, tuple(dict.fromkeys(reasons)))

    def service_health(self) -> ServiceHealthSnapshot:
        now = _utc(self._clock.now(), "clock")
        reasons = []
        if not self._stream_connected:
            reasons.append("STREAM_DISCONNECTED")
        if self.rate_limit_snapshot().backoff_until:
            reasons.append("RATE_LIMIT_BACKOFF")
        state = HealthState.OFFLINE if not self._stream_connected else HealthState.DEGRADED if reasons else HealthState.HEALTHY
        return ServiceHealthSnapshot(state, self._stream_connected, self.rate_limit_snapshot(), tuple(reasons))

    def entries_allowed(self, symbol: str) -> EntryDecision:
        health = self.symbol_health(symbol)
        priority = ("UNKNOWN_FRESHNESS", "STALE_DATA", "UNRESOLVED_GAP", "STREAM_DISCONNECTED",
                    "UNKNOWN_FEED_ID", "ENTITLEMENT_MISMATCH", "STREAM_ACTIVITY_GAP")
        for code in priority:
            if code in health.reasons:
                # A disconnected stream with fresh polling data is permitted.
                if code == "STREAM_DISCONNECTED" and any(
                    c.source != "stream" and not c.stale for c in health.channels.values()
                ):
                    continue
                return EntryDecision(False, code)
        return EntryDecision(True, None)
