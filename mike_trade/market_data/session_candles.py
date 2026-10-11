"""Completed daily (D1) and weekly (W1) session candles: conversion, aggregation, validation, store.

This module extends the existing candle contract (``market_data.candles.Candle`` with the
calendar-defined ``D1``/``W1`` timeframes) rather than adding a second market-data framework.

Contract
========
* **What a D1 candle is.** One regular NYSE session: ``start`` is that session's open and ``end``
  its close (an early close ends earlier).  The span is validated by ``Candle`` against the
  exchange calendar, so there are no midnight/UTC-day assumptions.
* **What a W1 candle is.** One Monday-Friday calendar week: the first session's open to the last
  session's close.  W1 candles are *derived from D1* here (never fetched), and only for weeks in
  which **every** trading session of the week is present - a week with a missing session is
  omitted, never patched.
* **Completed means settled.** A D1 candle becomes available to a decision at
  ``end + settle_delay`` (default one hour: after the provider's end-of-day bar is final, and
  before the next session).  A W1 candle is available when its last session's D1 candle is.  The
  current, incomplete day or week is therefore never visible, and a decision at ``as_of`` sees
  exactly what a live system could have known at ``as_of``.  The same ``get_completed_candles``
  serves SHADOW (clock ``as_of``) and BACKTEST replay (event ``available_at``).
* **Price basis.** Only RAW (unadjusted) daily candles are accepted - the same basis as the 1-minute
  candles and every stop-relevant price.  Split-adjusted bars are refused: adjusting history with
  corporate actions announced later would leak future information into earlier decisions and
  could not be compared with RAW intraday prices.  A raw series across a split shows an
  overnight discontinuity; :func:`suspect_corporate_action` lets a strategy fail closed on it.
* **Provenance lock.** Like ``CandleBuilder``, a symbol's series locks (feed, consolidated,
  price basis) on first ingest and rejects a different provenance, so a daily series can never be
  silently mixed with another feed.
* **Idempotence.** The first completed bar for a session wins; an identical re-fetch is a
  duplicate, a differing one is counted as a conflict and ignored (history never mutates).
"""

from __future__ import annotations

import hashlib
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from mike_trade.domain.errors import MikeTradeError
from mike_trade.market_data.alpaca_history import Adjustment, HistoricalBar
from mike_trade.market_data.candles import (
    NY,
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
    SeriesMismatchError,
)
from mike_trade.scheduling.calendar import Session, is_trading_day, next_session, session_for

__all__ = [
    "DEFAULT_SETTLE_DELAY",
    "DEFAULT_MAX_SESSIONS",
    "SessionCandleError",
    "SessionCandleSource",
    "IngestSummary",
    "SessionCandleStore",
    "daily_candle_from_bar",
    "settled_daily_candles",
    "aggregate_weeks",
    "last_settled_session",
    "missing_sessions",
    "is_consecutive",
    "suspect_corporate_action",
    "history_issue",
    "week_start",
]

#: A D1 candle is usable once ``end + settle_delay`` has passed.
DEFAULT_SETTLE_DELAY = timedelta(hours=1)
#: Daily candles retained per symbol (about two and a half trading years).
DEFAULT_MAX_SESSIONS = 640
_ONE_DAY = timedelta(days=1)


class SessionCandleError(MikeTradeError, ValueError):
    """Invalid daily/weekly input (wrong basis, mis-stamped bar, unknown session)."""


class SessionCandleSource(Protocol):
    """Read-only source of completed D1/W1 candles as known at ``as_of``."""

    def get_completed_candles(
        self,
        symbol: str,
        timeframe: CandleTimeframe,
        limit: int | None = None,
        *,
        as_of: datetime,
    ) -> tuple[Candle, ...]: ...


# ------------------------------------------------------------------------ conversion


def daily_candle_from_bar(
    bar: HistoricalBar, *, origin: CandleOrigin = CandleOrigin.HISTORICAL_REST
) -> Candle:
    """Convert one provider daily bar into a validated, RAW, CLOSED D1 candle.

    Alpaca stamps a daily bar at the start of its trading date in New York (04:00/05:00 UTC).  Any
    other stamp is refused instead of being guessed, so a differently-stamped provider cannot be
    assigned to the wrong session.  A bar for the current, still developing day is the caller's
    to exclude (availability is enforced at read time by :class:`SessionCandleStore`).
    """
    if bar.adjustment is not Adjustment.RAW:
        raise SessionCandleError(
            f"{bar.symbol}: daily candles must be RAW; {bar.adjustment.value}-adjusted history "
            "would mix a different price basis (and later corporate actions) into the series",
            code="session_candle_price_basis",
        )
    local = bar.timestamp.astimezone(NY)
    if (local.hour, local.minute, local.second, local.microsecond) != (0, 0, 0, 0):
        raise SessionCandleError(
            f"{bar.symbol}: daily bar stamp {bar.timestamp.isoformat()} is not New York "
            "midnight of a trading date",
            code="session_candle_stamp",
        )
    day = local.date()
    try:
        session = session_for(day)
    except ValueError as exc:
        raise SessionCandleError(
            f"{bar.symbol}: daily bar for {day.isoformat()}, which is not a trading session",
            code="session_candle_not_a_session",
        ) from exc
    trade_count = bar.trade_count
    if isinstance(trade_count, Decimal):
        trade_count = int(trade_count) if trade_count == trade_count.to_integral_value() else None
    return Candle(
        symbol=bar.symbol,
        timeframe=CandleTimeframe.D1,
        start=session.open_utc,
        end=session.close_utc,
        open=Decimal(bar.open),
        high=Decimal(bar.high),
        low=Decimal(bar.low),
        close=Decimal(bar.close),
        volume=Decimal(bar.volume),
        feed_id=bar.feed_id,
        consolidated=bar.feed_id != "iex",
        origin=origin,
        price_basis=PriceBasis.RAW,
        status=CandleStatus.CLOSED,
        trade_count=trade_count,
    )


def settled_daily_candles(
    bars: Iterable[HistoricalBar],
    *,
    now: datetime,
    settle_delay: timedelta = DEFAULT_SETTLE_DELAY,
    origin: CandleOrigin = CandleOrigin.HISTORICAL_REST,
) -> tuple[Candle, ...]:
    """D1 candles from provider bars, keeping only sessions that have settled at ``now``.

    A REST range that includes the current day returns its developing bar; ingesting it would
    freeze a partial day as the completed one (first bar wins), so it is dropped here.  A
    mis-stamped or non-session bar raises :class:`SessionCandleError` for the caller to handle.
    """
    at = now.astimezone(UTC)
    return tuple(
        candle
        for candle in (daily_candle_from_bar(bar, origin=origin) for bar in bars)
        if candle.end + settle_delay <= at
    )


def _session_date(candle: Candle) -> date:
    return candle.start.astimezone(NY).date()


def week_start(day: date) -> date:
    """The Monday of ``day``'s calendar week."""
    return day - timedelta(days=day.weekday())


def aggregate_weeks(daily: Sequence[Candle]) -> tuple[Candle, ...]:
    """Derive W1 candles from D1 candles; only weeks holding every trading session are produced."""
    by_week: dict[date, dict[date, Candle]] = {}
    for candle in daily:
        if candle.timeframe is not CandleTimeframe.D1 or not candle.is_closed:
            raise SessionCandleError("aggregate_weeks needs completed D1 candles",
                                     code="session_candle_input")
        by_week.setdefault(week_start(_session_date(candle)), {})[_session_date(candle)] = candle
    weeks: list[Candle] = []
    for monday in sorted(by_week):
        expected = [
            monday + timedelta(days=i) for i in range(5) if is_trading_day(monday + timedelta(days=i))
        ]
        present = by_week[monday]
        if sorted(present) != expected:
            continue  # a missing (or extra) session: the week is not completed knowledge
        parts = [present[d] for d in expected]
        identity = {(c.feed_id, c.consolidated, c.price_basis) for c in parts}
        if len(identity) != 1:
            continue
        origins = {c.origin for c in parts}
        counts = [c.trade_count for c in parts]
        weeks.append(
            Candle(
                symbol=parts[0].symbol,
                timeframe=CandleTimeframe.W1,
                start=parts[0].start,
                end=parts[-1].end,
                open=parts[0].open,
                high=max(c.high for c in parts),
                low=min(c.low for c in parts),
                close=parts[-1].close,
                volume=sum((c.volume for c in parts), Decimal(0)),
                feed_id=parts[0].feed_id,
                consolidated=parts[0].consolidated,
                origin=next(iter(origins)) if len(origins) == 1 else CandleOrigin.MIXED,
                price_basis=parts[0].price_basis,
                status=CandleStatus.CLOSED,
                trade_count=None if any(n is None for n in counts) else sum(counts),  # type: ignore[arg-type]
            )
        )
    return tuple(weeks)


# ------------------------------------------------------------- calendar / coverage helpers


def last_settled_session(as_of: datetime, settle_delay: timedelta = DEFAULT_SETTLE_DELAY) -> Session | None:
    """The most recent session whose daily candle is available at ``as_of`` (``None`` if none)."""
    at = as_of.astimezone(UTC)
    day = at.astimezone(NY).date()
    for _ in range(14):
        if is_trading_day(day):
            session = session_for(day)
            if session.close_utc + settle_delay <= at:
                return session
        day -= _ONE_DAY
    return None


def is_consecutive(prior: Candle, latest: Candle) -> bool:
    """True when ``latest`` is the very next exchange session (D1) or week (W1) after ``prior``."""
    if prior.timeframe is not latest.timeframe:
        return False
    if prior.timeframe is CandleTimeframe.D1:
        return latest.start == next_session(_session_date(prior)).open_utc
    return week_start(_session_date(latest)) == week_start(_session_date(prior)) + timedelta(days=7)


def missing_sessions(candles: Sequence[Candle]) -> tuple[date, ...]:
    """Trading sessions absent between the first and last D1 candle of a series."""
    if len(candles) < 2:
        return ()
    present = {_session_date(c) for c in candles}
    day, last, missing = _session_date(candles[0]), _session_date(candles[-1]), []
    while day < last:
        if is_trading_day(day) and day not in present:
            missing.append(day)
        day += _ONE_DAY
    return tuple(missing)


def suspect_corporate_action(candles: Sequence[Candle], max_overnight_move: Decimal) -> bool:
    """True if any session opens more than ``max_overnight_move`` (a fraction) from the prior close.

    A RAW series shows a split or reverse split as an overnight discontinuity.  This is a
    conservative, provider-independent detector (not a corporate-action feed): a real gap beyond
    the threshold also trips it, which only makes a pattern fail closed.
    """
    for prior, latest in zip(candles, candles[1:], strict=False):
        move = abs(latest.open / prior.close - 1)
        if move > max_overnight_move:
            return True
    return False


def history_issue(
    candles: Sequence[Candle],
    *,
    as_of: datetime,
    settle_delay: timedelta = DEFAULT_SETTLE_DELAY,
    max_overnight_move: Decimal | None = None,
) -> str | None:
    """Why a D1 history cannot be trusted for a multi-session pattern, or ``None``.

    Checks, in order: empty, consecutive sessions (no missing session), the newest candle is the
    most recently settled session (staleness), and (optionally) no suspected unadjusted
    corporate action.  Strategies treat any issue as INSUFFICIENT_DATA.
    """
    if not candles:
        return "no daily history"
    if any(c.timeframe is not CandleTimeframe.D1 for c in candles):
        return "not a daily series"
    gaps = missing_sessions(candles)
    if gaps:
        return f"missing session {gaps[0].isoformat()}"
    expected = last_settled_session(as_of, settle_delay)
    if expected is None or _session_date(candles[-1]) != expected.session_date:
        return "daily history is not up to date"
    if max_overnight_move is not None and suspect_corporate_action(candles, max_overnight_move):
        return "overnight discontinuity (possible unadjusted corporate action)"
    return None


# --------------------------------------------------------------------------------- store


@dataclass(frozen=True, slots=True)
class IngestSummary:
    added: int
    duplicates: int
    conflicts: int


@dataclass(slots=True)
class _Series:
    lock: tuple[str, bool, PriceBasis]
    daily: dict[date, Candle]
    conflicts: int = 0
    version: int = 0
    #: ascending D1 candles and their ends, rebuilt lazily after an ingest
    sorted_version: int = -1
    candles: list[Candle] = field(default_factory=list)
    ends: list[datetime] = field(default_factory=list)
    #: (timeframe, available count) -> result, valid for one ``version``
    cache: dict[tuple[CandleTimeframe, int], tuple[Candle, ...]] = field(default_factory=dict)


class SessionCandleStore:
    """Bounded per-symbol history of completed D1 candles, serving D1 and derived W1.

    Pure in-memory and provider-independent: acquisition (Alpaca REST in SHADOW, immutable dataset
    snapshots in BACKTEST) feeds it through :meth:`ingest_daily`.  Reads are point-in-time through
    ``as_of`` and never expose a developing day or week.
    """

    def __init__(
        self,
        *,
        settle_delay: timedelta = DEFAULT_SETTLE_DELAY,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
    ) -> None:
        if not isinstance(settle_delay, timedelta) or settle_delay < timedelta(0):
            raise SessionCandleError("settle_delay must be a non-negative timedelta",
                                     code="session_candle_config")
        if isinstance(max_sessions, bool) or not isinstance(max_sessions, int) or max_sessions < 10:
            raise SessionCandleError("max_sessions must be an int >= 10",
                                     code="session_candle_config")
        self.settle_delay = settle_delay
        self.max_sessions = max_sessions
        self._series: dict[str, _Series] = {}

    # ------------------------------------------------------------------ ingest

    def ingest_daily(self, symbol: str, candles: Iterable[Candle]) -> IngestSummary:
        """Add completed D1 candles; the first candle for a session wins."""
        key = symbol.strip().upper()
        added = duplicates = conflicts = 0
        for candle in sorted(candles, key=lambda c: c.start):
            if (
                candle.timeframe is not CandleTimeframe.D1
                or not candle.is_closed
                or candle.symbol != key
            ):
                raise SessionCandleError(
                    f"{key}: only completed D1 candles of that symbol can be ingested",
                    code="session_candle_input",
                )
            if candle.price_basis is not PriceBasis.RAW:
                raise SessionCandleError(f"{key}: daily candles must be RAW",
                                         code="session_candle_price_basis")
            lock = (candle.feed_id, candle.consolidated, candle.price_basis)
            series = self._series.get(key)
            if series is None:
                series = self._series[key] = _Series(lock, {})
            elif series.lock != lock:
                raise SeriesMismatchError(
                    f"{key}: daily candle provenance {lock!r} does not match series {series.lock!r}",
                    code="series_mismatch",
                    context={"symbol": key},
                )
            day = _session_date(candle)
            existing = series.daily.get(day)
            if existing is not None:
                if existing == candle:
                    duplicates += 1
                else:
                    series.conflicts += 1
                    conflicts += 1
                continue
            series.daily[day] = candle
            series.version += 1
            added += 1
            if len(series.daily) > self.max_sessions:
                del series.daily[min(series.daily)]
        return IngestSummary(added, duplicates, conflicts)

    # ------------------------------------------------------------------- reads

    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._series))

    def has(self, symbol: str) -> bool:
        series = self._series.get(symbol.strip().upper())
        return series is not None and bool(series.daily)

    def conflicts(self, symbol: str) -> int:
        series = self._series.get(symbol.strip().upper())
        return 0 if series is None else series.conflicts

    def fingerprint(self) -> str:
        """Content hash of every held candle (for run provenance); stable across processes."""
        digest = hashlib.sha256()
        for symbol in sorted(self._series):
            series = self._series[symbol]
            digest.update(f"{symbol}|{series.lock}".encode())
            for day in sorted(series.daily):
                c = series.daily[day]
                digest.update(
                    f"|{c.start.isoformat()}|{c.open}|{c.high}|{c.low}|{c.close}|{c.volume}".encode()
                )
        return digest.hexdigest()

    @staticmethod
    def _refresh_sorted(series: _Series) -> None:
        if series.sorted_version != series.version:
            series.candles = [series.daily[d] for d in sorted(series.daily)]
            series.ends = [c.end for c in series.candles]
            series.cache = {}
            series.sorted_version = series.version

    def get_completed_candles(
        self,
        symbol: str,
        timeframe: CandleTimeframe,
        limit: int | None = None,
        *,
        as_of: datetime,
    ) -> tuple[Candle, ...]:
        """Completed D1/W1 candles available at ``as_of`` (oldest first); never a partial one."""
        if not timeframe.is_session_based:
            raise SessionCandleError(f"{timeframe.value} is not a session timeframe",
                                     code="session_candle_timeframe")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise SessionCandleError("limit must be a non-negative int or None",
                                     code="session_candle_config")
        series = self._series.get(symbol.strip().upper())
        if series is None:
            return ()
        self._refresh_sorted(series)
        available = bisect_right(series.ends, as_of.astimezone(UTC) - self.settle_delay)
        cache_key = (timeframe, available)
        candles = series.cache.get(cache_key)
        if candles is None:
            daily = series.candles[:available]
            candles = tuple(daily) if timeframe is CandleTimeframe.D1 else aggregate_weeks(daily)
            series.cache[cache_key] = candles
        if limit is not None:
            candles = candles[len(candles) - limit:] if limit else ()
        return candles

    def needs_refresh(self, symbol: str, as_of: datetime) -> bool:
        """True when the most recently settled session is not yet in the series."""
        expected = last_settled_session(as_of, self.settle_delay)
        if expected is None:
            return False
        series = self._series.get(symbol.strip().upper())
        return series is None or expected.session_date not in series.daily
