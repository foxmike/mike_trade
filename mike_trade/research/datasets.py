"""Immutable Parquet snapshots and incremental Alpaca history acquisition."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from mike_trade.market_data.alpaca_history import Adjustment, AlpacaHistoricalBars, HistoricalBar
from mike_trade.market_data.candles import NY, Candle
from mike_trade.market_data.session_candles import (
    SessionCandleError,
    daily_candle_from_bar,
    settled_daily_candles,
)
from mike_trade.scheduling.calendar import session_for


@dataclass(frozen=True, slots=True)
class DatasetSnapshot:
    dataset_id: str
    provider: str
    feed: str
    symbol: str
    asset_class: str
    timeframe: str
    adjustment: str
    acquired_at: str
    start: str
    end: str
    first_bar: str
    last_bar: str
    bar_count: int
    data_file: str
    sha256: str
    schema_version: int = 1


class HistoricalDatasetRepository:
    """Read-many, write-new Parquet snapshots with content-addressed identity."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.data_dir = self.root / "snapshots"
        self.manifest_dir = self.root / "manifests"
        self.catalog_path = self.root / "catalog.duckdb"

    def snapshots(self) -> list[DatasetSnapshot]:
        if not self.manifest_dir.is_dir():
            return []
        result = []
        for path in sorted(self.manifest_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                result.append(DatasetSnapshot(**raw))
            except (OSError, ValueError, TypeError):
                continue
        return sorted(result, key=lambda item: (item.symbol, item.start, item.dataset_id))

    def sync_catalog(self) -> None:
        """Backfill/update the persistent SQL catalog from immutable manifests."""
        for snapshot in self.snapshots():
            self._record_catalog(snapshot)

    def available(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        feed: str,
        adjustment: str = "raw",
        timeframe: str = "1m",
        dataset_id: str | None = None,
    ) -> tuple[HistoricalBar, ...]:
        symbol = symbol.upper()
        selected = [
            s
            for s in self.snapshots()
            if s.symbol == symbol
            and s.feed == feed.lower()
            and s.adjustment == adjustment.lower()
            and s.timeframe == timeframe
            and (dataset_id is None or s.dataset_id == dataset_id)
            and datetime.fromisoformat(s.start) <= start.astimezone(UTC)
            and datetime.fromisoformat(s.end) >= end.astimezone(UTC)
        ]
        if not selected:
            return ()
        snapshot = max(selected, key=lambda item: item.acquired_at)
        path = self.root / snapshot.data_file
        if _sha256(path) != snapshot.sha256:
            raise ValueError(f"dataset integrity failure: {snapshot.dataset_id}")
        connection = duckdb.connect(":memory:")
        try:
            table = connection.execute(
                "SELECT * FROM read_parquet(?) "
                "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
                [str(path), start.astimezone(UTC), end.astimezone(UTC)],
            ).to_arrow_table()
            return tuple(_bar(row, snapshot.feed) for row in table.to_pylist())
        finally:
            connection.close()

    def all_bars(
        self, symbol: str, *, feed: str, adjustment: str = "raw", timeframe: str = "1m"
    ) -> tuple[HistoricalBar, ...]:
        snapshots = [
            s
            for s in self.snapshots()
            if s.symbol == symbol.upper()
            and s.feed == feed.lower()
            and s.adjustment == adjustment.lower()
            and s.timeframe == timeframe
        ]
        if not snapshots:
            return ()
        # Repair planning must reuse observations from every compatible immutable snapshot.
        # Manifest envelopes are not evidence that a particular session is present.
        paths: list[str] = []
        for snapshot in snapshots:
            path = self.root / snapshot.data_file
            if _sha256(path) != snapshot.sha256:
                raise ValueError(f"dataset integrity failure: {snapshot.dataset_id}")
            paths.append(str(path))
        connection = duckdb.connect(":memory:")
        try:
            rows = connection.execute(
                "SELECT DISTINCT * FROM read_parquet(?) ORDER BY timestamp", [paths]
            ).to_arrow_table().to_pylist()
        finally:
            connection.close()
        by_time: dict[datetime, HistoricalBar] = {}
        for row in rows:
            bar = _bar(row, feed.lower())
            old = by_time.setdefault(bar.timestamp, bar)
            if old != bar:
                raise ValueError(
                    f"conflicting duplicate bar {symbol.upper()} {bar.timestamp.isoformat()}"
                )
        return tuple(by_time[timestamp] for timestamp in sorted(by_time))

    def persist(
        self,
        symbol: str,
        bars: Iterable[HistoricalBar],
        *,
        feed: str,
        timeframe: str = "1m",
        adjustment: str = "raw",
        asset_class: str = "us_equity",
        coverage_start: datetime | None = None,
        coverage_end: datetime | None = None,
    ) -> DatasetSnapshot:
        if asset_class not in {"us_equity", "us_etf"}:
            raise ValueError("asset_class must be us_equity or us_etf")
        rows_by_time: dict[datetime, HistoricalBar] = {}
        for bar in bars:
            if bar.symbol != symbol.upper() or bar.feed_id != feed.lower():
                raise ValueError("bar symbol/feed does not match snapshot identity")
            _validate_bar(bar)
            if bar.timestamp in rows_by_time and rows_by_time[bar.timestamp] != bar:
                raise ValueError(f"conflicting duplicate bar {symbol} {bar.timestamp.isoformat()}")
            rows_by_time[bar.timestamp] = bar
        ordered = [rows_by_time[t] for t in sorted(rows_by_time)]
        if not ordered:
            raise ValueError("cannot persist an empty dataset")
        rows = [
            {
                "timestamp": b.timestamp,
                "symbol": b.symbol,
                "open": str(b.open),
                "high": str(b.high),
                "low": str(b.low),
                "close": str(b.close),
                "volume": str(b.volume),
                "vwap": None if b.vwap is None else str(b.vwap),
                "trade_count": None if b.trade_count is None else str(b.trade_count),
            }
            for b in ordered
        ]
        table = pa.Table.from_pylist(rows)
        canonical_rows = [{**row, "timestamp": row["timestamp"].isoformat()} for row in rows]
        start = (coverage_start or ordered[0].timestamp).astimezone(UTC)
        end = (coverage_end or ordered[-1].timestamp + timedelta(minutes=1)).astimezone(UTC)
        if start > ordered[0].timestamp or end <= ordered[-1].timestamp:
            raise ValueError("declared coverage does not contain all bars")
        identity = {
            "rows": canonical_rows,
            "coverage_start": start.isoformat(),
            "coverage_end": end.isoformat(),
        }
        content = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        dataset_id = (
            f"alpaca-{feed.lower()}-{symbol.upper()}-{timeframe}-{adjustment}-{content[:20]}"
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        relative = Path("snapshots") / f"{dataset_id}.parquet"
        data_path = self.root / relative
        with tempfile.NamedTemporaryFile(
            dir=self.data_dir, suffix=".parquet", delete=False
        ) as tmp:
            temp = Path(tmp.name)
        try:
            pq.write_table(table, temp, compression="zstd", version="2.6")
            with temp.open("rb") as stream:
                os.fsync(stream.fileno())
            try:
                os.link(temp, data_path)  # Never replace an immutable snapshot.
                _fsync_dir(self.data_dir)
            except FileExistsError:
                if _sha256(temp) != _sha256(data_path):
                    raise ValueError(f"conflicting existing dataset file: {dataset_id}") from None
        finally:
            temp.unlink(missing_ok=True)
        digest = _sha256(data_path)
        manifest = DatasetSnapshot(
            dataset_id,
            "alpaca",
            feed.lower(),
            symbol.upper(),
            asset_class,
            timeframe,
            adjustment.lower(),
            datetime.now(UTC).isoformat(),
            start.isoformat(),
            end.isoformat(),
            ordered[0].timestamp.isoformat(),
            ordered[-1].timestamp.isoformat(),
            len(ordered),
            str(relative),
            digest,
        )
        path = self.manifest_dir / f"{dataset_id}.json"
        if not path.exists():
            payload = (
                json.dumps(
                    manifest.__dict__
                    if hasattr(manifest, "__dict__")
                    else {name: getattr(manifest, name) for name in manifest.__dataclass_fields__},
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            )
            try:
                _atomic_new_text(path, payload)
            except FileExistsError:
                pass
        recorded = DatasetSnapshot(**json.loads(path.read_text(encoding="utf-8")))
        for name in (
            "dataset_id", "feed", "symbol", "asset_class", "timeframe",
            "adjustment", "start", "end", "bar_count", "sha256",
        ):
            if getattr(recorded, name) != getattr(manifest, name):
                raise ValueError(f"conflicting existing dataset manifest: {dataset_id}")
        manifest = recorded
        self._record_catalog(manifest)
        return manifest

    def _record_catalog(self, manifest: DatasetSnapshot) -> None:
        """Serialize catalog mutations; Parquet readers remain lock-free."""
        self.root.mkdir(parents=True, exist_ok=True)
        with _catalog_lock(self.root / ".catalog-write.lock"):
            connection = duckdb.connect(str(self.catalog_path))
            try:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS datasets (
                      dataset_id VARCHAR PRIMARY KEY, provider VARCHAR NOT NULL,
                      feed VARCHAR NOT NULL, symbol VARCHAR NOT NULL,
                      asset_class VARCHAR NOT NULL, timeframe VARCHAR NOT NULL,
                      adjustment VARCHAR NOT NULL, acquired_at TIMESTAMPTZ NOT NULL,
                      coverage_start TIMESTAMPTZ NOT NULL, coverage_end TIMESTAMPTZ NOT NULL,
                      first_bar TIMESTAMPTZ NOT NULL, last_bar TIMESTAMPTZ NOT NULL,
                      bar_count BIGINT NOT NULL, data_file VARCHAR NOT NULL,
                      sha256 VARCHAR NOT NULL, schema_version INTEGER NOT NULL
                    )
                """)
                connection.execute(
                    """
                    INSERT INTO datasets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (dataset_id) DO NOTHING
                """,
                    [
                        manifest.dataset_id,
                        manifest.provider,
                        manifest.feed,
                        manifest.symbol,
                        manifest.asset_class,
                        manifest.timeframe,
                        manifest.adjustment,
                        manifest.acquired_at,
                        manifest.start,
                        manifest.end,
                        manifest.first_bar,
                        manifest.last_bar,
                        manifest.bar_count,
                        manifest.data_file,
                        manifest.sha256,
                        manifest.schema_version,
                    ],
                )
                connection.execute("CHECKPOINT")
            finally:
                connection.close()

    def acquire(
        self,
        client: Any,
        symbols: Iterable[str],
        start: datetime,
        end: datetime,
        *,
        feed: str = "iex",
        adjustment: str = "raw",
        minimum_premarket_bars: int = 0,
        asset_classes: Mapping[str, str] | None = None,
        sync_catalog_after: bool = True,
        progress: Any = None,
    ) -> list[DatasetSnapshot]:
        if (
            isinstance(minimum_premarket_bars, bool)
            or not isinstance(minimum_premarket_bars, int)
            or minimum_premarket_bars < 0
        ):
            raise ValueError("minimum_premarket_bars must be a nonnegative integer")
        emit = progress if callable(progress) else lambda _message: None
        adapter = AlpacaHistoricalBars(client, max_pages=500)
        manifests = []
        for symbol in dict.fromkeys(s.upper() for s in symbols):
            existing = self.all_bars(symbol, feed=feed, adjustment=adjustment)
            # Actual observations, rather than the declared manifest envelope, determine reuse.
            # This detects internal holes left by interrupted or adjacent acquisitions.
            missing = _missing_minute_ranges(
                existing,
                start,
                end,
                minimum_premarket_bars=minimum_premarket_bars,
            )
            if not missing and existing:
                selected = [
                    snapshot
                    for snapshot in self.snapshots()
                    if snapshot.symbol == symbol
                    and snapshot.feed == feed.lower()
                    and snapshot.adjustment == adjustment.lower()
                    and snapshot.timeframe == "1m"
                    and datetime.fromisoformat(snapshot.start) <= start.astimezone(UTC)
                    and datetime.fromisoformat(snapshot.end) >= end.astimezone(UTC)
                ]
                if selected:
                    chosen = max(selected, key=lambda item: item.acquired_at)
                    self._record_catalog(chosen)
                    manifests.append(chosen)
                    emit(f"{symbol}: complete; reused {chosen.dataset_id}")
                    continue
            fetched = list(existing)
            known = [
                snapshot
                for snapshot in self.snapshots()
                if snapshot.symbol == symbol
                and snapshot.feed == feed.lower()
                and snapshot.adjustment == adjustment.lower()
                and snapshot.timeframe == "1m"
            ]
            coverage_start = min(
                [start.astimezone(UTC), *(datetime.fromisoformat(item.start) for item in known)]
            )
            coverage_end = max(
                [end.astimezone(UTC), *(datetime.fromisoformat(item.end) for item in known)]
            )
            published: DatasetSnapshot | None = None
            dirty = False

            def checkpoint(label: str) -> None:
                nonlocal dirty, published
                if not dirty or not fetched:
                    return
                published = self.persist(
                    symbol,
                    fetched,
                    feed=feed,
                    adjustment=adjustment,
                    asset_class=(asset_classes or {}).get(symbol, "us_equity"),
                    coverage_start=coverage_start,
                    coverage_end=coverage_end,
                )
                dirty = False
                emit(f"{symbol}: {label} {published.dataset_id}")

            emit(f"{symbol}: {len(missing)} missing acquisition interval(s)")
            for index, (left, right) in enumerate(missing, 1):
                if left < right:
                    if (
                        index == 1
                        or index == len(missing)
                        or index % 10 == 0
                        or right - left > timedelta(hours=6)
                    ):
                        emit(
                            f"{symbol}: interval={index}/{len(missing)} "
                            f"range={left.isoformat()}..{right.isoformat()}"
                        )
                    try:
                        additions = adapter.fetch_bars(
                            symbol, "1m", left, right, feed=feed, adjustment=Adjustment(adjustment)
                        )
                    except Exception:
                        checkpoint("failure checkpoint")
                        raise
                    fetched.extend(additions)
                    dirty = dirty or bool(additions)
                    # Bound interruption loss without rewriting a full symbol snapshot after
                    # every isolated session. A later exception publishes pending additions.
                    if index % 10 == 0:
                        checkpoint("checkpoint")
            checkpoint("published")
            if published is None and existing:
                published = max(known, key=lambda item: item.acquired_at)
            if published is not None:
                manifests.append(published)
        if sync_catalog_after:
            self.sync_catalog()
        return manifests

    def minute_coverage_report(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        feed: str = "iex",
        adjustment: str = "raw",
        dataset_id: str | None = None,
        minimum_premarket_bars: int = 20,
    ) -> dict[str, Any]:
        """Measure actual minute coverage with a timestamp-only Parquet scan.

        Manifest ranges are intentionally ignored as evidence of observations.  Premarket is
        04:00 through the exchange open in New York time; regular hours use the exchange calendar,
        including early closes.  A missing IEX minute is never manufactured.
        """
        if start >= end:
            raise ValueError("start must precede end")
        snapshots = [
            item
            for item in self.snapshots()
            if item.symbol == symbol.upper()
            and item.feed == feed.lower()
            and item.adjustment == adjustment.lower()
            and item.timeframe == "1m"
            and (dataset_id is None or item.dataset_id == dataset_id)
            and datetime.fromisoformat(item.start) <= start.astimezone(UTC)
            and datetime.fromisoformat(item.end) >= end.astimezone(UTC)
        ]
        if not snapshots:
            raise ValueError(f"no matching minute snapshot for {symbol.upper()}")
        snapshot = max(snapshots, key=lambda item: item.acquired_at)
        path = self.root / snapshot.data_file
        sessions = _covered_sessions(start, end)
        connection = duckdb.connect(":memory:")
        try:
            connection.execute(
                "CREATE TEMP TABLE expected_sessions "
                "(session_date DATE, premarket_start TIMESTAMPTZ, market_open TIMESTAMPTZ, "
                "market_close TIMESTAMPTZ)"
            )
            connection.executemany(
                "INSERT INTO expected_sessions VALUES (?, ?, ?, ?)",
                [
                    (session.session_date, _premarket_start(session.session_date),
                     session.open_utc, session.close_utc)
                    for session in sessions
                ],
            )
            total, distinct, earliest, latest = connection.execute(
                "SELECT count(*), count(DISTINCT timestamp), min(timestamp), max(timestamp) "
                "FROM read_parquet(?) WHERE timestamp >= ? AND timestamp < ?",
                [str(path), start.astimezone(UTC), end.astimezone(UTC)],
            ).fetchone()
            rows = connection.execute(
                """
                SELECT e.session_date,
                       count(b.timestamp) FILTER (
                           WHERE b.timestamp >= e.market_open AND b.timestamp < e.market_close
                       ) AS rth_bars,
                       count(b.timestamp) FILTER (
                           WHERE b.timestamp >= e.premarket_start AND b.timestamp < e.market_open
                       ) AS premarket_bars
                FROM expected_sessions e
                LEFT JOIN read_parquet(?) b
                  ON b.timestamp >= e.premarket_start AND b.timestamp < e.market_close
                GROUP BY e.session_date
                ORDER BY e.session_date
                """,
                [str(path)],
            ).fetchall()
        finally:
            connection.close()
        missing = [day.isoformat() for day, rth, _premarket in rows if int(rth) == 0]
        premarket = [day.isoformat() for day, _rth, count in rows if int(count) > 0]
        sufficient = [
            day.isoformat()
            for day, _rth, count in rows
            if int(count) >= minimum_premarket_bars
        ]
        return {
            "symbol": symbol.upper(),
            "dataset_id": snapshot.dataset_id,
            "coverage_start": snapshot.start,
            "coverage_end_exclusive": snapshot.end,
            "actual_earliest_timestamp": earliest.isoformat() if earliest is not None else None,
            "actual_latest_timestamp": latest.isoformat() if latest is not None else None,
            "observations": int(total),
            "duplicate_timestamps": int(total) - int(distinct),
            "expected_rth_sessions": len(rows),
            "rth_sessions_represented": len(rows) - len(missing),
            "missing_rth_sessions": missing,
            "premarket_sessions_represented": len(premarket),
            "premarket_sessions": premarket,
            "minimum_premarket_bars": minimum_premarket_bars,
            "sufficient_ema_warmup_sessions": len(sufficient),
            "sufficient_ema_warmup_dates": sufficient,
        }


    # ------------------------------------------------------------------ daily history

    def acquire_daily(
        self,
        client: Any,
        symbols: Iterable[str],
        start: date,
        end: date,
        *,
        feed: str = "iex",
        adjustment: str = "raw",
        now: datetime | None = None,
    ) -> list[DatasetSnapshot]:
        """Acquire completed RAW daily bars for ``start <= session date <= end`` as snapshots.

        Same immutable, content-addressed ``1d`` snapshots as the 1-minute datasets.  Only RAW
        daily bars are accepted (they must match the RAW 1-minute basis; adjusted history would
        embed later corporate actions).  A bar for a session that has not settled at acquisition
        time is dropped, so a developing day can never be persisted as a completed one.
        """
        if adjustment.lower() != "raw":
            raise ValueError("daily history must be acquired with adjustment='raw'")
        if start > end:
            raise ValueError("start must not be after end")
        acquired_at = (now or datetime.now(UTC)).astimezone(UTC)
        window_start = datetime.combine(start, datetime.min.time(), NY).astimezone(UTC)
        window_end = datetime.combine(end + timedelta(days=1), datetime.min.time(), NY).astimezone(
            UTC
        )
        adapter = AlpacaHistoricalBars(client, max_pages=50)
        manifests: list[DatasetSnapshot] = []
        for symbol in dict.fromkeys(s.upper() for s in symbols):
            cached = [
                snap
                for snap in self.snapshots()
                if snap.symbol == symbol and snap.feed == feed.lower()
                and snap.adjustment == "raw" and snap.timeframe == "1d"
                and datetime.fromisoformat(snap.start) <= window_start
                and datetime.fromisoformat(snap.end) >= window_end
            ]
            if cached:
                manifests.append(max(cached, key=lambda item: item.acquired_at))
                continue
            fetched = adapter.fetch_bars(
                symbol, "1d", window_start, window_end, feed=feed, adjustment=Adjustment.RAW
            )
            # rejects mis-stamped / non-session bars and drops a still-developing day
            keep = {c.start for c in settled_daily_candles(fetched, now=acquired_at)}
            settled = [bar for bar in fetched if session_open_of(bar) in keep]
            if not settled:
                raise ValueError(f"{symbol}: no settled daily bars in {start}..{end}")
            manifests.append(
                self.persist(
                    symbol, settled, feed=feed, timeframe="1d", adjustment="raw",
                    coverage_start=window_start, coverage_end=window_end,
                )
            )
        self.sync_catalog()
        return manifests

    def daily_candles(
        self, symbol: str, start: date, end: date, *, feed: str = "iex"
    ) -> tuple[Candle, ...]:
        """Completed RAW D1 candles for ``start..end`` from an immutable ``1d`` snapshot.

        Raises when no snapshot declares coverage of the whole range (coverage is declared at
        acquisition, so a quiet or holiday stretch is never mistaken for missing data).
        """
        window_start = datetime.combine(start, datetime.min.time(), NY).astimezone(UTC)
        window_end = datetime.combine(end + timedelta(days=1), datetime.min.time(), NY).astimezone(
            UTC
        )
        bars = self.available(
            symbol, window_start, window_end, feed=feed, adjustment="raw", timeframe="1d"
        )
        if not bars:
            raise ValueError(
                f"no immutable daily (1d) snapshot covers {symbol.upper()} {start}..{end}; "
                "run `research-data acquire --timeframe 1d`"
            )
        try:
            return tuple(daily_candle_from_bar(bar) for bar in bars)
        except SessionCandleError as exc:
            raise ValueError(f"{symbol.upper()}: invalid daily snapshot: {exc}") from exc


def _premarket_start(day: date) -> datetime:
    return datetime.combine(day, time(4, 0), NY).astimezone(UTC)


def _covered_sessions(start: datetime, end: datetime) -> list[Any]:
    """Exchange sessions whose entire regular market lies inside ``[start, end)``."""
    start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
    current = start_utc.astimezone(NY).date()
    final = (end_utc - timedelta(microseconds=1)).astimezone(NY).date()
    result: list[Any] = []
    while current <= final:
        try:
            session = session_for(current)
        except ValueError:
            pass
        else:
            if session.open_utc >= start_utc and session.close_utc <= end_utc:
                result.append(session)
        current += timedelta(days=1)
    return result


def _missing_minute_ranges(
    bars: Iterable[HistoricalBar],
    start: datetime,
    end: datetime,
    *,
    minimum_premarket_bars: int = 0,
) -> list[tuple[datetime, datetime]]:
    """Return actual missing RTH sessions and deficient premarket windows.

    A session is present when at least one provider observation falls inside its regular market.
    Sparse one-minute feeds are not filled.  The optional warmup check requests only that day's
    04:00-to-open interval and retains any observations the provider returns.
    """
    start_utc, end_utc = start.astimezone(UTC), end.astimezone(UTC)
    relevant = [bar for bar in bars if start_utc <= bar.timestamp < end_utc]
    sessions = _covered_sessions(start_utc, end_utc)
    if not sessions:
        return [] if relevant else [(start_utc, end_utc)]
    by_day = {session.session_date: session for session in sessions}
    rth_counts: dict[date, int] = {day: 0 for day in by_day}
    premarket_counts: dict[date, int] = {day: 0 for day in by_day}
    for bar in relevant:
        day = bar.timestamp.astimezone(NY).date()
        session = by_day.get(day)
        if session is None:
            continue
        if session.open_utc <= bar.timestamp < session.close_utc:
            rth_counts[day] += 1
        elif _premarket_start(day) <= bar.timestamp < session.open_utc:
            premarket_counts[day] += 1
    regular_ranges: list[tuple[datetime, datetime]] = []
    premarket_ranges: list[tuple[datetime, datetime]] = []
    open_missing_range: tuple[datetime, datetime] | None = None
    for session in sessions:
        rth = rth_counts[session.session_date]
        premarket_start = max(start_utc, _premarket_start(session.session_date))
        premarket = premarket_counts[session.session_date]
        if rth == 0:
            if open_missing_range is None:
                open_missing_range = (premarket_start, min(end_utc, session.close_utc))
            else:
                open_missing_range = (
                    open_missing_range[0], min(end_utc, session.close_utc)
                )
        elif premarket < minimum_premarket_bars and premarket_start < session.open_utc:
            if open_missing_range is not None:
                regular_ranges.append(open_missing_range)
                open_missing_range = None
            premarket_ranges.append((premarket_start, session.open_utc))
        elif open_missing_range is not None:
            regular_ranges.append(open_missing_range)
            open_missing_range = None
    if open_missing_range is not None:
        regular_ranges.append(open_missing_range)
    missing = regular_ranges
    # Missing-session requests already include their premarket window.
    missing.extend(premarket_ranges)
    # Repair missing regular sessions first so a later premarket failure cannot discard them.
    return list(dict.fromkeys(missing))


def session_open_of(bar: HistoricalBar) -> datetime:
    """The regular-session open (UTC) of a provider daily bar's trading date."""
    return daily_candle_from_bar(bar).start


def _validate_bar(bar: HistoricalBar) -> None:
    if bar.timestamp.tzinfo is None or bar.timestamp.utcoffset() is None:
        raise ValueError("bar timestamp must be timezone-aware")
    if bar.volume < 0 or min(bar.open, bar.high, bar.low, bar.close) <= 0:
        raise ValueError("OHLC must be positive and volume nonnegative")
    if (
        bar.low > min(bar.open, bar.close)
        or bar.high < max(bar.open, bar.close)
        or bar.low > bar.high
    ):
        raise ValueError(f"invalid OHLC relationship at {bar.timestamp.isoformat()}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_text(path: Path, text: str) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        _fsync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def _atomic_new_text(path: Path, text: str) -> None:
    """Publish one immutable manifest without replacing an existing name."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(name, path)
        _fsync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _bar(row: dict[str, Any], feed: str = "iex") -> HistoricalBar:
    return HistoricalBar(
        row["symbol"],
        row["timestamp"],
        Decimal(row["open"]),
        Decimal(row["high"]),
        Decimal(row["low"]),
        Decimal(row["close"]),
        Decimal(row["volume"]),
        None if row["vwap"] is None else Decimal(row["vwap"]),
        None if row["trade_count"] is None else Decimal(row["trade_count"]),
        feed,
        Adjustment.RAW,
    )


@contextmanager
def _catalog_lock(path: Path):
    """Cross-process advisory lock for the single DuckDB catalog writer."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
