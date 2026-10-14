"""Exchange-session-aligned candle aggregation from stored one-minute bars.

One implementation serves training and streaming: :class:`CandleAggregator` consumes completed
one-minute :class:`ModelBar` values in order and emits N-minute candles; :func:`aggregate_bars` is
nothing but that aggregator driven over a sequence.

Rules (all enforced here, none left to callers):

* **Session alignment.** Buckets start at the regular-session open (09:30 ET) and are exactly
  ``minutes`` long, so 5-minute candles begin 09:30, 09:35, ... and 15-minute candles begin 09:30,
  09:45, ... .  Early-close sessions (13:00) use the same grid.  A bucket must fit inside its
  session; a candle never contains a pre-market, after-hours or overnight minute (the dataset
  provider already emits regular-session minutes only, and a bar outside its session is refused).
* **Completed candles only.**  A candle is emitted only once its time window has elapsed, i.e. when a
  later minute (or the next session) is seen, when the window's last minute arrives, or when the
  caller declares the data complete through a time (``flush``).  A bucket that is still open is never
  emitted, so no candle ever contains data from after its own end.
* **OHLCV.**  open = first minute's open, high = maximum high, low = minimum low, close = last
  minute's close, volume = sum, vwap = volume-weighted mean of the minute VWAPs (``None`` unless
  every contributing minute has one), trade count = sum (``None`` unless all minutes have it).
* **No bridging.**  A minute that has no bar is simply absent: no flat or zero-volume minute is
  invented, and a bucket with fewer than ``minimum_minutes`` bars (default 1) is dropped instead of
  being filled.  The Alpaca IEX feed is a single venue: a minute without a bar means no IEX trade
  printed in that minute, so a candle built from the minutes that do exist is exactly the IEX N-minute
  bar.  Consecutive emitted candles are contiguous only when no bucket was dropped between them;
  downstream pipelines treat any non-contiguous start as a break.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from mike_trade.modeling.contracts import ModelBar
from mike_trade.scheduling.calendar import session_for

__all__ = ["AggregationPolicy", "AggregationStats", "CandleAggregator", "aggregate_bars"]

_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class AggregationPolicy:
    """How one-minute bars become ``minutes``-long candles."""

    minutes: int
    #: Minimum number of one-minute bars a bucket needs to become a candle (1 = any trade printed).
    minimum_minutes: int = 1
    source_timeframe: str = "1m"

    def __post_init__(self) -> None:
        if isinstance(self.minutes, bool) or not isinstance(self.minutes, int) or self.minutes < 2:
            raise ValueError("aggregation minutes must be an int >= 2")
        if (
            isinstance(self.minimum_minutes, bool)
            or not isinstance(self.minimum_minutes, int)
            or not 1 <= self.minimum_minutes <= self.minutes
        ):
            raise ValueError("minimum_minutes must be an int in [1, minutes]")
        if 390 % self.minutes != 0 or 210 % self.minutes != 0:
            raise ValueError(
                "aggregation minutes must divide both a regular (390 min) and an early-close "
                "(210 min) session so every candle lies inside its session"
            )

    @property
    def timeframe(self) -> str:
        return f"{self.minutes}m"


@dataclass(slots=True)
class AggregationStats:
    """Counters describing what the aggregation kept and dropped (recorded with the model)."""

    minute_bars: int = 0
    candles: int = 0
    candles_with_all_minutes: int = 0
    candles_with_missing_minutes: int = 0
    buckets_dropped_below_minimum: int = 0
    minute_bars_dropped: int = 0

    def merge(self, other: AggregationStats) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def as_mapping(self) -> dict[str, int]:
        return {name: int(getattr(self, name)) for name in self.__dataclass_fields__}


@dataclass(slots=True)
class _Bucket:
    key: tuple[str, int]
    start: datetime
    end: datetime
    bars: list[ModelBar] = field(default_factory=list)


class CandleAggregator:
    """Stateful, strictly point-in-time minute-to-candle aggregator for one symbol."""

    def __init__(self, policy: AggregationPolicy) -> None:
        self.policy = policy
        self.stats = AggregationStats()
        self._bucket: _Bucket | None = None
        self._last_start: datetime | None = None
        self._session_cache: dict[str, tuple[datetime, datetime]] = {}

    # -------------------------------------------------------------- bucketing

    def _session_bounds(self, bar: ModelBar) -> tuple[datetime, datetime]:
        bounds = self._session_cache.get(bar.session_id)
        if bounds is None:
            day = datetime.fromisoformat(bar.session_id.removeprefix("XNYS-")).date()
            session = session_for(day)
            bounds = (session.open_utc, session.close_utc)
            if len(self._session_cache) > 8:
                self._session_cache.clear()
            self._session_cache[bar.session_id] = bounds
        return bounds

    def _locate(self, bar: ModelBar) -> _Bucket:
        opened, closed = self._session_bounds(bar)
        offset = bar.start - opened
        if offset < timedelta(0):
            raise ValueError(f"{bar.symbol}: bar {bar.start} precedes its session open")
        index = int(offset // timedelta(minutes=self.policy.minutes))
        start = opened + timedelta(minutes=index * self.policy.minutes)
        end = start + timedelta(minutes=self.policy.minutes)
        if bar.end > end:
            raise ValueError(f"{bar.symbol}: bar {bar.start} straddles a candle boundary")
        if end > closed:
            raise ValueError(f"{bar.symbol}: candle {start} would extend past the session close")
        return _Bucket((bar.session_id, index), start, end)

    # ------------------------------------------------------------------ build

    def _build(self, bucket: _Bucket) -> ModelBar | None:
        bars = bucket.bars
        if len(bars) < self.policy.minimum_minutes:
            self.stats.buckets_dropped_below_minimum += 1
            self.stats.minute_bars_dropped += len(bars)
            return None
        volume = sum((item.volume for item in bars), _ZERO)
        vwap: Decimal | None = None
        if volume > _ZERO and all(item.vwap is not None for item in bars):
            weighted = sum((item.vwap * item.volume for item in bars if item.vwap), _ZERO)
            vwap = weighted / volume
        trades: int | None = None
        if all(item.trade_count is not None for item in bars):
            trades = sum(int(item.trade_count or 0) for item in bars)
        first, last = bars[0], bars[-1]
        self.stats.candles += 1
        if len(bars) == self.policy.minutes:
            self.stats.candles_with_all_minutes += 1
        else:
            self.stats.candles_with_missing_minutes += 1
        return ModelBar(
            symbol=first.symbol,
            start=bucket.start,
            end=bucket.end,
            open=first.open,
            high=max(item.high for item in bars),
            low=min(item.low for item in bars),
            close=last.close,
            volume=volume,
            feed=first.feed,
            timeframe=self.policy.timeframe,
            adjustment=first.adjustment,
            session_id=first.session_id,
            vwap=vwap,
            trade_count=trades,
        )

    # -------------------------------------------------------------------- API

    def update(self, bar: ModelBar) -> list[ModelBar]:
        """Consume the next completed one-minute bar; return the candles it completed."""
        if bar.timeframe != self.policy.source_timeframe:
            raise ValueError(
                f"aggregator expects {self.policy.source_timeframe} bars; got {bar.timeframe}"
            )
        if self._last_start is not None and bar.start <= self._last_start:
            raise ValueError(f"{bar.symbol}: minute bars must be strictly ascending")
        self._last_start = bar.start
        self.stats.minute_bars += 1
        emitted: list[ModelBar] = []
        located = self._locate(bar)
        current = self._bucket
        if current is not None and current.key != located.key:
            candle = self._build(current)  # a later bucket began: the earlier window has elapsed
            if candle is not None:
                emitted.append(candle)
            current = self._bucket = None
        if current is None:
            current = self._bucket = located
        current.bars.append(bar)
        if bar.end >= current.end:  # the window's last minute arrived: complete right now
            candle = self._build(current)
            self._bucket = None
            if candle is not None:
                emitted.append(candle)
        return emitted

    def flush(self, through: datetime) -> list[ModelBar]:
        """Complete the open bucket if its window has elapsed by ``through`` (caller's clock)."""
        current = self._bucket
        if current is None or current.end > through.astimezone(UTC):
            return []
        self._bucket = None
        candle = self._build(current)
        return [] if candle is None else [candle]

    @property
    def open_bucket_start(self) -> datetime | None:
        return None if self._bucket is None else self._bucket.start


def aggregate_bars(
    bars: Iterable[ModelBar] | Sequence[ModelBar],
    policy: AggregationPolicy,
    *,
    complete_through: datetime | None = None,
    stats: AggregationStats | None = None,
) -> tuple[ModelBar, ...]:
    """Aggregate ascending one-minute bars of ONE symbol into completed candles.

    ``complete_through`` declares the data complete up to that instant (the end of the selected
    range): a trailing bucket whose window ended by then is emitted; without it a trailing bucket
    is emitted only if its last minute is present, so an unfinished candle never leaks out.
    """
    aggregator = CandleAggregator(policy)
    result: list[ModelBar] = []
    for bar in bars:
        result.extend(aggregator.update(bar))
    if complete_through is not None:
        result.extend(aggregator.flush(complete_through))
    if stats is not None:
        stats.merge(aggregator.stats)
    return tuple(result)
