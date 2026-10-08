"""Point-in-time historical replay feed over a bounded, offline dataset.

``HistoricalReplayFeed`` is injected in place of the live Alpaca feed.  It wraps
operator-supplied in-memory records (or a local JSON fixture file) and never
touches the network, broker credentials, the wall clock, or real-time sleeps.

Design choices (7):
1. The existing port is ``mike_trade.market_data.ports.MarketDataPort`` (there
   is no ``MarketDataProvider``).  The feed implements it structurally and
   unchanged: ``subscribe`` yields ``domain.market.MarketEvent`` BAR/QUOTE
   events (BAR carries close/volume; full OHLCV is available through
   ``fetch_bars``/``events``/``as_of``).  Splits and data-quality markers have no
   ``MarketEvent`` shape, so they are exposed only as ``ReplayEvent`` values.
2. ``ReplayEvent`` is the single operator-facing record type; its ``kind`` is a
   plain string (``ReplayEvent.KIND_*``).  Equal-availability ties break by kind
   rank (SPLIT, DATA_QUALITY, BAR, QUOTE), then symbol, then sequence (explicit,
   else input position), then input position.
3. Availability: a 1m bar ``[t, t+1m)`` is available at ``t+1m + latency``; a
   quote at ``time + latency``; splits and markers at their own time.  Only
   completed bars (``Candle.closed``) are accepted; developing candles are
   rejected, never exposed.  ``as_of(t)`` returns nothing with ``available_at > t``
   and only the latest vintage of a bar.
4. Fail closed on future-adjusted data.  A non-RAW adjustment policy, or a bar
   whose revision vintage / adjustment-announcement time is later than the time
   it would first be served, raises ``FutureAdjustedDataError`` unless the
   provenance declares ``point_in_time=True``.  When declared, such a bar is
   withheld until ``max(completion + latency, revision/announcement time)``.
5. Missing minutes between two bars of one regular session become
   ``DATA_QUALITY`` markers (reason ``UNRESOLVED_GAP``, ``DataFreshness.STALE``,
   ``HealthState.DEGRADED``) available when the revealing later bar is; halts
   are operator-supplied markers (reason ``TRADING_HALT``) and suppress gap
   markers for halted minutes.  Nothing is ever forward-filled into a bar.
6. Quote fidelity is UNAVAILABLE without quote records; otherwise the declared
   ``DataProvenance.quote_fidelity`` (default SAMPLED).
7. ``seed`` is exposed and feeds ``rng()``; ordering itself uses no randomness,
   so equal dataset + seed always yield an identical sequence.  The dataset
   content hash is computed from the supplied records and verified against the
   declared one when present.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from bisect import bisect_right
from collections import defaultdict
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from mike_trade.backtest.clock import ReplayClock
from mike_trade.domain.accounts import MarketDataCapabilities
from mike_trade.domain.errors import MikeTradeError
from mike_trade.domain.market import (
    Candle,
    DataFreshness,
    FeedProvenance,
    MarketEvent,
    MarketEventKind,
    Timeframe,
)
from mike_trade.market_data.quality import HealthState
from mike_trade.scheduling import calendar as _nyse
from mike_trade.scheduling.calendar import NEW_YORK, Session

__all__ = [
    "LookaheadError",
    "FutureAdjustedDataError",
    "QuoteFidelity",
    "DataProvenance",
    "ReplayEvent",
    "HistoricalReplayFeed",
]

_MINUTE = timedelta(minutes=1)
_ADJUSTMENTS = frozenset({"RAW", "SPLIT_ADJUSTED", "TOTAL_RETURN_ADJUSTED"})
_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_DEFAULT_MAX_EVENTS = 1_000_000
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024


class LookaheadError(MikeTradeError):
    """Raised when a query asks for data beyond the replay clock's present."""


class FutureAdjustedDataError(MikeTradeError):
    """Raised when data embeds knowledge from after its evaluation time."""


class QuoteFidelity(StrEnum):
    """How much quote (bid/ask) evidence the dataset can support."""

    UNAVAILABLE = "UNAVAILABLE"
    BAR_DERIVED = "BAR_DERIVED"
    SAMPLED = "SAMPLED"
    FULL = "FULL"


# ----------------------------------------------------------------- validation


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be a datetime; got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _opt_utc(value: object, field: str) -> datetime | None:
    return None if value is None else _utc(value, field)


def _symbol(value: object, field: str = "symbol") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be str; got {type(value).__name__}")
    text = value.strip().upper()
    if _SYMBOL_RE.fullmatch(text) is None:
        raise ValueError(f"{field} is invalid: {value!r}")
    return text


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty str")
    return value.strip()


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise ValueError(f"{field} must be Decimal; got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    return value


# ------------------------------------------------------------------ provenance


@dataclass(frozen=True, slots=True)
class DataProvenance:
    """Explicit origin of a replay dataset.

    ``feed`` is the exchange feed (e.g. ``"IEX"``; ``is_consolidated=False`` so
    it is never mistaken for SIP), ``revision`` the dataset revision/vintage
    label, ``adjustment`` one of RAW / SPLIT_ADJUSTED / TOTAL_RETURN_ADJUSTED,
    and ``point_in_time`` whether adjusted/revised values are vintage-correct.
    ``dataset_hash`` is the SHA-256 of the supplied records; ``None`` lets the
    feed fill it in, a value is verified.
    """

    source: str
    feed: str
    is_consolidated: bool
    revision: str
    adjustment: str = "RAW"
    point_in_time: bool = False
    quote_fidelity: QuoteFidelity = QuoteFidelity.SAMPLED
    dataset_hash: str | None = None

    def __post_init__(self) -> None:
        for name in ("source", "feed", "revision"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if not isinstance(self.is_consolidated, bool) or not isinstance(self.point_in_time, bool):
            raise ValueError("is_consolidated and point_in_time must be bool")
        adjustment = _text(self.adjustment, "adjustment").upper()
        if adjustment not in _ADJUSTMENTS:
            raise ValueError(
                f"adjustment must be one of {sorted(_ADJUSTMENTS)}; got {adjustment!r}"
            )
        object.__setattr__(self, "adjustment", adjustment)
        if not isinstance(self.quote_fidelity, QuoteFidelity):
            raise ValueError("quote_fidelity must be QuoteFidelity")
        if self.dataset_hash is not None and (
            not isinstance(self.dataset_hash, str) or _HASH_RE.fullmatch(self.dataset_hash) is None
        ):
            raise ValueError("dataset_hash must be a lowercase sha256 hex digest or None")

    def feed_provenance(self) -> FeedProvenance:
        """Domain-level provenance carried by emitted candles/events."""
        return FeedProvenance(
            provider="replay",
            feed=self.feed,
            source=self.source,
            is_consolidated=self.is_consolidated,
        )


# ---------------------------------------------------------------------- events


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    """One dataset record; ``available_at`` is assigned by the feed."""

    KIND_SPLIT: ClassVar[str] = "SPLIT"
    KIND_DATA_QUALITY: ClassVar[str] = "DATA_QUALITY"
    KIND_BAR: ClassVar[str] = "BAR"
    KIND_QUOTE: ClassVar[str] = "QUOTE"
    REASON_HALT: ClassVar[str] = "TRADING_HALT"
    REASON_GAP: ClassVar[str] = "UNRESOLVED_GAP"
    KINDS: ClassVar[tuple[str, ...]] = ("SPLIT", "DATA_QUALITY", "BAR", "QUOTE")

    kind: str
    symbol: str
    event_time: datetime
    sequence: int | None = None
    available_at: datetime | None = None
    candle: Candle | None = None
    quote: MarketEvent | None = None
    split_ratio: Decimal | None = None
    freshness: DataFreshness | None = None
    health: HealthState | None = None
    reason: str | None = None
    detail: str = ""
    window_end: datetime | None = None
    revision_at: datetime | None = None
    adjustment_announced_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind not in self.KINDS:
            raise ValueError(f"kind must be one of {self.KINDS}; got {self.kind!r}")
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "event_time", _utc(self.event_time, "event_time"))
        for name in ("available_at", "window_end", "revision_at", "adjustment_announced_at"):
            object.__setattr__(self, name, _opt_utc(getattr(self, name), name))
        if self.sequence is not None and (
            isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence < 0
        ):
            raise ValueError("sequence must be an int >= 0 or None")
        if self.kind != self.KIND_BAR and (
            self.revision_at is not None or self.adjustment_announced_at is not None
        ):
            raise ValueError("revision_at/adjustment_announced_at apply to BAR records only")
        if self.kind == self.KIND_BAR:
            self._check_bar()
        elif self.kind == self.KIND_QUOTE:
            quote = self.quote
            if not isinstance(quote, MarketEvent) or quote.kind is not MarketEventKind.QUOTE:
                raise ValueError("QUOTE records require a QUOTE MarketEvent")
            if quote.symbol != self.symbol or quote.event_time != self.event_time:
                raise ValueError("QUOTE symbol/event_time must match its MarketEvent")
        elif self.kind == self.KIND_SPLIT:
            ratio = _decimal(self.split_ratio, "split_ratio")
            if ratio <= 0 or ratio == 1:
                raise ValueError("split_ratio must be > 0 and != 1 (new shares per old share)")
        else:
            if not isinstance(self.freshness, DataFreshness) or not isinstance(
                self.health, HealthState
            ):
                raise ValueError("DATA_QUALITY records require freshness and health")
            object.__setattr__(self, "reason", _text(self.reason, "reason"))
            if self.window_end is not None and self.window_end <= self.event_time:
                raise ValueError("window_end must be later than event_time")

    def _check_bar(self) -> None:
        candle = self.candle
        if not isinstance(candle, Candle):
            raise ValueError("BAR records require a Candle")
        if candle.timeframe is not Timeframe.M1:
            raise ValueError("only 1-minute bars are supported")
        if not candle.closed:
            raise ValueError("developing/partial candles cannot be replayed as completed bars")
        if candle.start.second or candle.start.microsecond:
            raise ValueError("bar start must be aligned to a minute boundary")
        if candle.symbol != self.symbol or candle.start != self.event_time:
            raise ValueError("BAR symbol/event_time must match its candle")

    @property
    def knowledge_time(self) -> datetime | None:
        """Latest revision/adjustment-announcement time embedded in this record."""
        times = [t for t in (self.revision_at, self.adjustment_announced_at) if t is not None]
        return max(times) if times else None

    @classmethod
    def bar(
        cls,
        candle: Candle,
        *,
        sequence: int | None = None,
        revision_at: datetime | None = None,
        adjustment_announced_at: datetime | None = None,
    ) -> ReplayEvent:
        return cls(
            cls.KIND_BAR,
            candle.symbol,
            candle.start,
            sequence=sequence,
            candle=candle,
            revision_at=revision_at,
            adjustment_announced_at=adjustment_announced_at,
        )

    @classmethod
    def from_quote(cls, quote: MarketEvent, *, sequence: int | None = None) -> ReplayEvent:
        return cls(cls.KIND_QUOTE, quote.symbol, quote.event_time, sequence=sequence, quote=quote)

    @classmethod
    def split(
        cls, symbol: str, effective_at: datetime, ratio: Decimal, *, sequence: int | None = None
    ) -> ReplayEvent:
        """Dated split, applied from ``effective_at`` (``ratio`` = new per old shares)."""
        return cls(
            cls.KIND_SPLIT, symbol, effective_at, sequence=sequence, split_ratio=ratio
        )

    @classmethod
    def halt(
        cls,
        symbol: str,
        start: datetime,
        resume: datetime | None = None,
        *,
        sequence: int | None = None,
    ) -> ReplayEvent:
        return cls.quality(
            symbol,
            start,
            cls.REASON_HALT,
            DataFreshness.UNKNOWN,
            HealthState.DEGRADED,
            window_end=resume,
            sequence=sequence,
        )

    @classmethod
    def quality(
        cls,
        symbol: str,
        at: datetime,
        reason: str,
        freshness: DataFreshness,
        health: HealthState,
        *,
        detail: str = "",
        window_end: datetime | None = None,
        sequence: int | None = None,
    ) -> ReplayEvent:
        return cls(
            cls.KIND_DATA_QUALITY,
            symbol,
            at,
            sequence=sequence,
            freshness=freshness,
            health=health,
            reason=reason,
            detail=detail,
            window_end=window_end,
        )


_KIND_RANK = {kind: rank for rank, kind in enumerate(ReplayEvent.KINDS)}


# ------------------------------------------------------------- canonical hash


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else format(value.normalize(), "f")


def _canonical(event: ReplayEvent) -> str:
    candle, quote = event.candle, event.quote
    payload: dict[str, Any] = {
        "kind": event.kind,
        "symbol": event.symbol,
        "event_time": _iso(event.event_time),
        "sequence": event.sequence,
        "split_ratio": _dec(event.split_ratio),
        "freshness": event.freshness.value if event.freshness else None,
        "health": event.health.value if event.health else None,
        "reason": event.reason,
        "detail": event.detail,
        "window_end": _iso(event.window_end),
        "revision_at": _iso(event.revision_at),
        "adjustment_announced_at": _iso(event.adjustment_announced_at),
        "candle": None
        if candle is None
        else [
            _dec(candle.open), _dec(candle.high), _dec(candle.low), _dec(candle.close),
            _dec(candle.volume), _dec(candle.vwap), candle.trade_count,
            candle.provenance.feed, candle.provenance.is_consolidated,
        ],
        "quote": None
        if quote is None
        else [
            _dec(quote.bid), _dec(quote.ask), _dec(quote.bid_size), _dec(quote.ask_size),
            _dec(quote.price), _dec(quote.size), quote.provenance.feed,
            quote.provenance.is_consolidated,
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


# ------------------------------------------------------------------------ feed


@dataclass(frozen=True, slots=True)
class _Entry:
    event: ReplayEvent  # available_at is always set
    index: int

    @property
    def key(self) -> tuple[datetime, int, str, int, int]:
        ev = self.event
        assert ev.available_at is not None
        return (ev.available_at, _KIND_RANK[ev.kind], ev.symbol, ev.sequence or 0, self.index)


class HistoricalReplayFeed:
    """Deterministic as-of ``MarketDataPort`` over a bounded offline dataset."""

    def __init__(
        self,
        events: Iterable[ReplayEvent],
        provenance: DataProvenance,
        *,
        availability_latency: timedelta = timedelta(0),
        seed: int = 0,
        clock: ReplayClock | None = None,
        max_events: int = _DEFAULT_MAX_EVENTS,
    ) -> None:
        if not isinstance(provenance, DataProvenance):
            raise TypeError("provenance must be DataProvenance")
        if not isinstance(availability_latency, timedelta) or availability_latency < timedelta(0):
            raise ValueError("availability_latency must be a non-negative timedelta")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an int")
        if clock is not None and not isinstance(clock, ReplayClock):
            raise TypeError("clock must be a ReplayClock or None")
        if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events < 1:
            raise ValueError("max_events must be an int >= 1")
        if provenance.adjustment != "RAW" and not provenance.point_in_time:
            raise FutureAdjustedDataError(
                f"{provenance.adjustment} data is refused unless declared point-in-time",
                code="future_adjusted_series",
            )
        supplied: list[ReplayEvent] = []
        for item in events:
            if len(supplied) >= max_events:
                raise ValueError(f"dataset exceeds max_events={max_events}")
            if not isinstance(item, ReplayEvent):
                raise TypeError("events must be ReplayEvent values")
            if item.available_at is not None:
                raise ValueError("input records must not preset available_at")
            supplied.append(replace(item, sequence=len(supplied) if item.sequence is None
                                    else item.sequence))
        self._provenance_in = provenance
        self._latency = availability_latency
        self._seed = seed
        self._clock = clock
        self._max_events = max_events
        self._closed = False
        self._active: set[str] = set()

        digest = self.compute_dataset_hash(supplied)
        if provenance.dataset_hash is not None and provenance.dataset_hash != digest:
            raise ValueError("dataset content hash does not match provenance.dataset_hash")
        self._provenance = replace(provenance, dataset_hash=digest)

        has_quotes = any(e.kind == ReplayEvent.KIND_QUOTE for e in supplied)
        if has_quotes and provenance.quote_fidelity is QuoteFidelity.UNAVAILABLE:
            raise ValueError(
                "dataset has quotes but provenance declares quote_fidelity UNAVAILABLE"
            )

        entries = [_Entry(self._stamp(ev), i) for i, ev in enumerate(supplied)]
        self._check_unique_bars(entries)
        entries.extend(self._derive_gap_markers(entries))
        entries.sort(key=lambda e: e.key)
        self._entries = tuple(entries)
        self._avail = [e.key[0] for e in entries]
        self._quote_symbols = frozenset(
            e.event.symbol for e in entries if e.event.kind == ReplayEvent.KIND_QUOTE
        )

    # ------------------------------------------------------------- construction

    @classmethod
    def from_json_file(
        cls,
        path: str | Path,
        provenance: DataProvenance,
        *,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        **kwargs: Any,
    ) -> HistoricalReplayFeed:
        """Load a local JSON fixture: ``{"events": [{"kind": ..., ...}, ...]}``.

        Numbers must be JSON strings (floats are rejected); timestamps are ISO-8601
        with an explicit offset.  Only the local filesystem is read.
        """
        file = Path(path)
        if file.stat().st_size > max_bytes:
            raise ValueError(f"fixture exceeds max_bytes={max_bytes}")

        def _no_float(text: str) -> Any:
            raise ValueError(f"fixture numbers must be JSON strings, got float {text}")

        document = json.loads(file.read_text(encoding="utf-8"), parse_float=_no_float)
        records = document.get("events") if isinstance(document, dict) else None
        if not isinstance(records, list):
            raise ValueError('fixture must be an object with an "events" list')
        feed_prov = provenance.feed_provenance()
        return cls((_record_to_event(r, feed_prov) for r in records), provenance, **kwargs)

    @staticmethod
    def compute_dataset_hash(events: Iterable[ReplayEvent]) -> str:
        """SHA-256 over the canonical, order-independent form of the records."""
        lines = sorted(_canonical(e) for e in events)
        return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()

    def _stamp(self, event: ReplayEvent) -> ReplayEvent:
        prov = self._provenance_in
        candle, quote = event.candle, event.quote
        carried = candle.provenance if candle is not None else quote.provenance if quote else None
        if carried is not None and (
            carried.feed.lower() != prov.feed.lower()
            or carried.is_consolidated != prov.is_consolidated
        ):
            raise ValueError(
                f"{event.symbol} record feed {carried.feed!r} does not match dataset feed "
                f"{prov.feed!r}"
            )
        if event.kind == ReplayEvent.KIND_BAR:
            assert candle is not None
            base = candle.end + self._latency
        elif event.kind == ReplayEvent.KIND_QUOTE:
            base = event.event_time + self._latency
        else:
            base = event.event_time
        known = event.knowledge_time
        if known is not None and known > base:
            if not prov.point_in_time:
                raise FutureAdjustedDataError(
                    f"{event.symbol} bar {event.event_time.isoformat()} embeds a revision/"
                    f"adjustment from {known.isoformat()}, after its availability "
                    f"{base.isoformat()}",
                    code="future_revision",
                )
            base = known
        return replace(event, available_at=base)

    @staticmethod
    def _check_unique_bars(entries: Sequence[_Entry]) -> None:
        seen: set[tuple[str, datetime, datetime | None]] = set()
        for entry in entries:
            ev = entry.event
            if ev.kind != ReplayEvent.KIND_BAR:
                continue
            ident = (ev.symbol, ev.event_time, ev.knowledge_time)
            if ident in seen:
                raise ValueError(
                    f"duplicate bar for {ev.symbol} at {ev.event_time.isoformat()} and vintage"
                )
            seen.add(ident)

    def _derive_gap_markers(self, entries: Sequence[_Entry]) -> list[_Entry]:
        halts: dict[str, list[tuple[datetime, datetime | None]]] = defaultdict(list)
        first_seen: dict[str, dict[datetime, datetime]] = defaultdict(dict)
        for entry in entries:
            ev = entry.event
            assert ev.available_at is not None
            if ev.kind == ReplayEvent.KIND_DATA_QUALITY and ev.reason == ReplayEvent.REASON_HALT:
                halts[ev.symbol].append((ev.event_time, ev.window_end))
            elif ev.kind == ReplayEvent.KIND_BAR:
                seen = first_seen[ev.symbol]
                seen[ev.event_time] = min(seen.get(ev.event_time, ev.available_at), ev.available_at)
        derived: list[_Entry] = []
        next_seq = len(entries)
        for symbol in sorted(first_seen):
            starts = sorted(first_seen[symbol])
            for prev, cur in zip(starts, starts[1:], strict=False):
                if cur - prev <= _MINUTE or not _same_regular_session(prev, cur):
                    continue
                missing = 0
                first_missing: datetime | None = None
                minute = prev + _MINUTE
                while minute < cur:
                    if not any(s <= minute and (e is None or minute < e) for s, e in halts[symbol]):
                        missing += 1
                        first_missing = first_missing or minute
                    minute += _MINUTE
                if first_missing is None:
                    continue
                marker = ReplayEvent.quality(
                    symbol,
                    first_missing,
                    ReplayEvent.REASON_GAP,
                    DataFreshness.STALE,
                    HealthState.DEGRADED,
                    detail=f"missing_bars={missing}",
                    window_end=cur,
                    sequence=next_seq,
                )
                derived.append(
                    _Entry(replace(marker, available_at=first_seen[symbol][cur]), next_seq)
                )
                next_seq += 1
        return derived

    # --------------------------------------------------------------- properties

    @property
    def provenance(self) -> DataProvenance:
        """Provenance with the verified dataset content hash filled in."""
        return self._provenance

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def availability_latency(self) -> timedelta:
        return self._latency

    def rng(self, stream: str = "") -> random.Random:
        """Fresh deterministic generator derived from the seed and a stream label."""
        digest = hashlib.sha256(f"{self._seed}:{stream}".encode()).digest()
        return random.Random(int.from_bytes(digest[:8], "big"))

    def quote_fidelity(self, symbol: str | None = None) -> QuoteFidelity:
        """UNAVAILABLE when there are no quotes (for ``symbol``), else the declared level."""
        if symbol is None:
            has = bool(self._quote_symbols)
        else:
            has = _symbol(symbol) in self._quote_symbols
        return self._provenance.quote_fidelity if has else QuoteFidelity.UNAVAILABLE

    # ----------------------------------------------------------------- queries

    def events(self, *, symbols: Iterable[str] | None = None) -> Iterator[ReplayEvent]:
        """All events in strict (availability, kind, symbol, sequence) order."""
        wanted = None if symbols is None else {_symbol(s) for s in symbols}
        for entry in self._entries:
            if wanted is None or entry.event.symbol in wanted:
                yield entry.event

    def as_of(
        self,
        t: datetime,
        *,
        symbols: Iterable[str] | None = None,
        kinds: Iterable[str] | None = None,
        since: datetime | None = None,
    ) -> tuple[ReplayEvent, ...]:
        """Events with ``since < available_at <= t``, superseded bar vintages removed."""
        at = _utc(t, "t")
        if self._clock is not None and at > self._clock.now():
            raise LookaheadError(
                f"as_of({at.isoformat()}) is ahead of the replay clock "
                f"{self._clock.now().isoformat()}",
                code="lookahead",
            )
        wanted = None if symbols is None else {_symbol(s) for s in symbols}
        kind_set = None if kinds is None else set(kinds)
        floor = _opt_utc(since, "since")
        window = self._entries[: bisect_right(self._avail, at)]
        latest: dict[tuple[str, datetime], int] = {}
        for pos, entry in enumerate(window):
            if entry.event.kind == ReplayEvent.KIND_BAR:
                latest[(entry.event.symbol, entry.event.event_time)] = pos
        out: list[ReplayEvent] = []
        for pos, entry in enumerate(window):
            ev = entry.event
            assert ev.available_at is not None
            if floor is not None and ev.available_at <= floor:
                continue
            if wanted is not None and ev.symbol not in wanted:
                continue
            if kind_set is not None and ev.kind not in kind_set:
                continue
            if ev.kind == ReplayEvent.KIND_BAR and latest[(ev.symbol, ev.event_time)] != pos:
                continue
            out.append(ev)
        return tuple(out)

    def split_factor(self, symbol: str, t: datetime) -> Decimal:
        """Product of split ratios effective at or before ``t`` (1 if none)."""
        factor = Decimal(1)
        for ev in self.as_of(t, symbols=[symbol], kinds=[ReplayEvent.KIND_SPLIT]):
            assert ev.split_ratio is not None
            factor *= ev.split_ratio
        return factor

    # -------------------------------------------------------------- port methods

    def capabilities(self) -> MarketDataCapabilities:
        return MarketDataCapabilities(
            provider="replay",
            feed=self._provenance.feed,
            is_consolidated=self._provenance.is_consolidated,
            supports_streaming=True,
            supported_timeframes=frozenset({Timeframe.M1.value}),
        )

    async def subscribe(self, symbols: Sequence[str]) -> AsyncIterator[MarketEvent]:
        if self._closed:
            raise RuntimeError("feed is closed")
        if isinstance(symbols, str):
            raise TypeError("symbols must be a sequence of str")
        self._active.update(_symbol(s) for s in symbols)
        return self._stream()

    async def unsubscribe(self, symbols: Sequence[str]) -> None:
        self._active.difference_update(_symbol(s) for s in symbols)

    async def fetch_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> Sequence[Candle]:
        if timeframe is not Timeframe.M1:
            raise ValueError("only 1-minute bars are supported")
        lo, hi = _utc(start, "start"), _utc(end, "end")
        if hi < lo:
            raise ValueError("end must not be earlier than start")
        evaluation = self._clock.now() if self._clock is not None else hi
        bars = self.as_of(evaluation, symbols=[symbol], kinds=[ReplayEvent.KIND_BAR])
        return tuple(
            ev.candle for ev in bars
            if ev.candle is not None and ev.candle.start >= lo and ev.candle.end <= hi
        )

    async def close(self) -> None:
        self._closed = True
        self._active.clear()

    async def _stream(self) -> AsyncIterator[MarketEvent]:
        for entry in self._entries:
            if self._closed:
                return
            ev = entry.event
            if ev.kind not in (ReplayEvent.KIND_BAR, ReplayEvent.KIND_QUOTE):
                continue
            if ev.symbol not in self._active:
                continue
            assert ev.available_at is not None
            if self._clock is not None and ev.available_at > self._clock.now():
                self._clock.advance_to(ev.available_at)
            yield self._market_event(ev)

    @staticmethod
    def _market_event(ev: ReplayEvent) -> MarketEvent:
        assert ev.available_at is not None
        if ev.candle is not None:
            c = ev.candle
            return MarketEvent(
                symbol=c.symbol,
                kind=MarketEventKind.BAR,
                event_time=c.start,
                received_at=ev.available_at,
                provenance=c.provenance,
                price=c.close,
                size=c.volume,
            )
        assert ev.quote is not None
        return replace(ev.quote, received_at=ev.available_at)


# --------------------------------------------------------------------- helpers


def _same_regular_session(first: datetime, second: datetime) -> bool:
    """True when both bar starts fall inside one regular session."""
    day = first.astimezone(NEW_YORK).date()
    if not _nyse.SUPPORTED_YEAR_START <= day.year <= _nyse.SUPPORTED_YEAR_END:
        return False
    if not _nyse.is_trading_day(day):
        return False
    session: Session = _nyse.session_for(day)
    return session.open_utc <= first and second < session.close_utc


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 string")
    return datetime.fromisoformat(value)


def _parse_dec(value: object, field: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a numeric string")
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{field} is not a valid decimal: {value!r}") from None


def _opt(record: Mapping[str, Any], key: str, parse: Any) -> Any:
    return None if record.get(key) is None else parse(record[key], key)


def _record_to_event(record: object, prov: FeedProvenance) -> ReplayEvent:
    if not isinstance(record, dict):
        raise ValueError("fixture events must be objects")
    kind = str(record.get("kind", "")).upper()
    symbol = record.get("symbol")
    sequence = record.get("sequence")
    if kind == "BAR":
        start = _parse_time(record.get("start"), "start")
        candle = Candle(
            symbol=_symbol(symbol),
            timeframe=Timeframe.M1,
            start=start,
            end=start + _MINUTE,
            open=_parse_dec(record.get("open"), "open"),
            high=_parse_dec(record.get("high"), "high"),
            low=_parse_dec(record.get("low"), "low"),
            close=_parse_dec(record.get("close"), "close"),
            volume=_parse_dec(record.get("volume"), "volume"),
            provenance=prov,
            vwap=_opt(record, "vwap", _parse_dec),
            trade_count=record.get("trade_count"),
        )
        return ReplayEvent.bar(
            candle,
            sequence=sequence,
            revision_at=_opt(record, "revision_at", _parse_time),
            adjustment_announced_at=_opt(record, "adjustment_announced_at", _parse_time),
        )
    if kind == "QUOTE":
        at = _parse_time(record.get("time"), "time")
        quote = MarketEvent(
            symbol=_symbol(symbol),
            kind=MarketEventKind.QUOTE,
            event_time=at,
            received_at=at,
            provenance=prov,
            bid=_parse_dec(record.get("bid"), "bid"),
            ask=_parse_dec(record.get("ask"), "ask"),
            bid_size=_opt(record, "bid_size", _parse_dec),
            ask_size=_opt(record, "ask_size", _parse_dec),
        )
        return ReplayEvent.from_quote(quote, sequence=sequence)
    if kind == "SPLIT":
        return ReplayEvent.split(
            _symbol(symbol),
            _parse_time(record.get("effective_at"), "effective_at"),
            _parse_dec(record.get("ratio"), "ratio"),
            sequence=sequence,
        )
    if kind == "HALT":
        return ReplayEvent.halt(
            _symbol(symbol),
            _parse_time(record.get("start"), "start"),
            _opt(record, "resume", _parse_time),
            sequence=sequence,
        )
    if kind == "DATA_QUALITY":
        return ReplayEvent.quality(
            _symbol(symbol),
            _parse_time(record.get("time"), "time"),
            str(record.get("reason", "")),
            DataFreshness(str(record.get("freshness", ""))),
            HealthState(str(record.get("health", ""))),
            detail=str(record.get("detail", "")),
            window_end=_opt(record, "window_end", _parse_time),
            sequence=sequence,
        )
    raise ValueError(f"unknown fixture event kind {kind!r}")
