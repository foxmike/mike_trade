"""Resumable, bounded acquisition of a frozen research universe.

This coordinates the existing Alpaca adapter and immutable dataset repository;
it does not own another bar format or catalog.
"""

from __future__ import annotations

import fcntl
import json
import os
import random
import re
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import duckdb
import httpx

from mike_trade.market_data.alpaca_history import AlpacaHistoricalBars, HistoricalBar
from mike_trade.research.datasets import (
    DatasetSnapshot,
    HistoricalDatasetRepository,
    _atomic_text,
    _sha256,
)
from mike_trade.scheduling.calendar import is_regular_hours, is_trading_day, session_for

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_MIN_FREE_BYTES = 10 * 1024**3


def _instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("universe timestamps must include UTC timezone")
    return result.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Universe:
    path: Path
    universe_id: str
    selection_at: str
    symbols: tuple[str, ...]
    symbol_metadata: Mapping[str, Mapping[str, str]]
    start: datetime
    end: datetime
    sha256: str

    @classmethod
    def load(cls, path: str | Path) -> Universe:
        file = Path(path)
        raw = json.loads(file.read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1 or raw.get("universe_id") != "us-liquid-research-100-v1":
            raise ValueError("unsupported research universe identity/schema")
        if (
            raw.get("provider"),
            raw.get("feed"),
            raw.get("adjustment"),
            raw.get("timeframe"),
            raw.get("session"),
            raw.get("end_exclusive"),
        ) != ("alpaca", "iex", "raw", "1m", "us_regular", True):
            raise ValueError("research universe must pin Alpaca IEX raw 1m regular-session bars")
        start, end = _instant(raw["start"]), _instant(raw["end"])
        if (start.isoformat(), end.isoformat()) != (
            "2025-10-09T00:00:00+00:00",
            "2026-10-09T00:00:00+00:00",
        ):
            raise ValueError("research universe window differs from approved window")
        selected = _instant(raw["selected_at"])
        if selected.date().isoformat() != raw.get("membership_as_of"):
            raise ValueError("selection date and membership date differ")
        members = raw.get("symbols")
        if not isinstance(members, list) or len(members) != 100:
            raise ValueError("research universe must contain exactly 100 symbols")
        metadata: dict[str, Mapping[str, str]] = {}
        for item in members:
            if not isinstance(item, dict):
                raise ValueError("universe member must be a mapping")
            symbol = item.get("symbol")
            if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol) or symbol in metadata:
                raise ValueError(f"invalid or duplicate universe symbol: {symbol!r}")
            if item.get("asset_class") not in {"us_equity", "us_etf"} or not item.get("group"):
                raise ValueError(f"invalid identity metadata: {symbol}")
            metadata[symbol] = item
        if not raw.get("methodology") or not raw.get("metadata_sources"):
            raise ValueError("universe selection provenance is missing")
        listing = raw.get("listing_check", {})
        if (
            listing.get("equities_found")
            != sum(item["asset_class"] == "us_equity" for item in members)
            or listing.get("etfs_found") != sum(item["asset_class"] == "us_etf" for item in members)
            or _instant(listing["checked_at"]) < selected
        ):
            raise ValueError("universe listing check is inconsistent")
        return cls(
            file,
            raw["universe_id"],
            raw["selected_at"],
            tuple(metadata),
            metadata,
            start,
            end,
            _sha256(file),
        )

    def windows(self) -> tuple[tuple[datetime, datetime], ...]:
        result: list[tuple[datetime, datetime]] = []
        left = self.start
        while left < self.end:
            year, month = left.year + (left.month == 12), left.month % 12 + 1
            right = min(datetime(year, month, 9, tzinfo=UTC), self.end)
            result.append((left, right))
            left = right
        return tuple(result)


class BoundedAlpacaClient:
    """Thread-safe conservative pacing and bounded retry for the existing client."""

    def __init__(
        self,
        inner: Any,
        *,
        requests_per_minute: int = 20,
        max_attempts: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if not 1 <= requests_per_minute <= 60 or not 1 <= max_attempts <= 10:
            raise ValueError("request rate or retry bound is invalid")
        self.inner, self.requests_per_minute = inner, requests_per_minute
        self.max_attempts, self.sleep, self.monotonic, self.jitter = (
            max_attempts,
            sleep,
            monotonic,
            jitter,
        )
        self._lock = threading.Lock()
        self._next_request = 0.0
        self._cooldown_until = 0.0

    def get(self, path: str, params: Mapping[str, object] | None = None) -> Any:
        for attempt in range(self.max_attempts):
            with self._lock:
                now = self.monotonic()
                slot = max(now, self._next_request, self._cooldown_until)
                delay = slot - now
                self._next_request = slot + 60 / self.requests_per_minute
            if delay:
                self.sleep(delay)
            with self._lock:
                deferred = max(0.0, self._cooldown_until - self.monotonic())
            if deferred:
                self.sleep(deferred)
            try:
                return self.inner.get(path, params=params)
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)
                retryable = status in {429, 500, 502, 503, 504} or isinstance(
                    exc, httpx.TransportError
                )
                if not retryable:
                    raise RuntimeError(
                        f"Alpaca historical API rejected request (HTTP {status})"
                    ) from None
                if attempt + 1 == self.max_attempts:
                    raise RuntimeError(
                        f"Alpaca historical API exhausted retries (HTTP {status or 'transport'})"
                    ) from None
                header = response.headers.get("retry-after") if response is not None else None
                try:
                    retry_after = max(0.0, float(header)) if header is not None else 0.0
                except ValueError:
                    try:
                        retry_after = max(
                            0.0,
                            (
                                parsedate_to_datetime(header).astimezone(UTC) - datetime.now(UTC)
                            ).total_seconds(),
                        )
                    except (TypeError, ValueError, OverflowError):
                        retry_after = 0.0
                backoff = max(retry_after, min(60.0, 2**attempt + self.jitter()))
                with self._lock:
                    self._cooldown_until = max(self._cooldown_until, self.monotonic() + backoff)
                self.sleep(backoff)
        raise AssertionError("unreachable retry loop")


def _window_key(symbol: str, left: datetime) -> str:
    return f"{symbol}:{left.date().isoformat()}"


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _session_minutes(left: datetime, right: datetime) -> list[datetime]:
    minutes: list[datetime] = []
    day = left.date()
    while day <= right.date():
        if is_trading_day(day):
            session = session_for(day)
            moment = session.open_utc
            while moment < session.close_utc:
                if left <= moment < right:
                    minutes.append(moment)
                moment += timedelta(minutes=1)
        day += timedelta(days=1)
    return minutes


def coverage(bars: tuple[HistoricalBar, ...], left: datetime, right: datetime) -> dict[str, Any]:
    observed = {bar.timestamp for bar in bars}
    expected = _session_minutes(left, right)
    absent = [moment for moment in expected if moment not in observed]
    intervals: list[list[str]] = []
    for moment in absent:
        if intervals and _instant(intervals[-1][1]) == moment:
            intervals[-1][1] = _utc_text(moment + timedelta(minutes=1))
        else:
            intervals.append([_utc_text(moment), _utc_text(moment + timedelta(minutes=1))])
    return {
        "expected_minutes": len(expected),
        "observed_bars": len(bars),
        "missing_minutes": len(absent),
        "missing_intervals": intervals,
        "first_bar": _utc_text(bars[0].timestamp) if bars else None,
        "last_bar": _utc_text(bars[-1].timestamp) if bars else None,
    }


class UniverseAcquisition:
    def __init__(self, universe: Universe, root: str | Path) -> None:
        self.universe = universe
        self.root = Path(root)
        self.repo = HistoricalDatasetRepository(self.root)
        self.progress_dir = self.root / "acquisition" / universe.universe_id
        self.state_path = self.progress_dir / "state.json"
        self._manifests: list[DatasetSnapshot] | None = None

    def _known_manifests(self) -> list[DatasetSnapshot]:
        if self._manifests is None:
            self._manifests = self.repo.snapshots()
        return self._manifests

    def _remember(self, snapshot: DatasetSnapshot) -> None:
        known = self._known_manifests()
        if all(item.dataset_id != snapshot.dataset_id for item in known):
            known.append(snapshot)

    def _state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "universe_id": self.universe.universe_id,
                "universe_sha256": self.universe.sha256,
                "windows": {},
                "final": {},
                "failures": {},
            }
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state.get("universe_sha256") != self.universe.sha256:
            raise ValueError("universe file changed after acquisition began; use a new version")
        return state

    def _save(self, state: dict[str, Any]) -> None:
        self.progress_dir.mkdir(parents=True, exist_ok=True)
        _atomic_text(self.state_path, json.dumps(state, sort_keys=True, indent=2) + "\n")

    def _snapshot(
        self, dataset_id: str, symbol: str, left: datetime, right: datetime
    ) -> DatasetSnapshot | None:
        for item in self._known_manifests():
            if (
                item.dataset_id == dataset_id
                and item.symbol == symbol
                and item.asset_class == self.universe.symbol_metadata[symbol]["asset_class"]
                and item.feed == "iex"
                and item.adjustment == "raw"
                and item.timeframe == "1m"
                and _instant(item.start) <= left
                and _instant(item.end) >= right
            ):
                path = self.root / item.data_file
                if path.is_file() and _sha256(path) == item.sha256:
                    return item
        return None

    def _cached(self, symbol: str, left: datetime, right: datetime) -> DatasetSnapshot | None:
        for item in self._known_manifests():
            if (
                item.symbol == symbol
                and item.asset_class == self.universe.symbol_metadata[symbol]["asset_class"]
                and item.feed == "iex"
                and item.adjustment == "raw"
                and item.timeframe == "1m"
                and _instant(item.start) <= left
                and _instant(item.end) >= right
            ):
                found = self._snapshot(item.dataset_id, symbol, left, right)
                if found:
                    return found
        return None

    def plan(self) -> dict[str, Any]:
        state = self._state()
        windows = self.universe.windows()
        completed = 0
        cached = 0
        for symbol in self.universe.symbols:
            for left, right in windows:
                key = _window_key(symbol, left)
                if key in state["windows"] and self._snapshot(
                    state["windows"][key]["dataset_id"], symbol, left, right
                ):
                    completed += 1
                elif self._cached(symbol, left, right):
                    cached += 1
        total = len(self.universe.symbols) * len(windows)
        return {
            "universe_id": self.universe.universe_id,
            "selected_at": self.universe.selection_at,
            "universe_sha256": self.universe.sha256,
            "symbols": list(self.universe.symbols),
            "symbol_count": len(self.universe.symbols),
            "window_count": total,
            "completed_windows": completed,
            "cached_windows": cached,
            "pending_windows": total - completed - cached,
            "completed_symbols": sorted(state["final"]),
            "failed_windows": state["failures"],
            "provider": "alpaca",
            "feed": "iex",
            "adjustment": "raw",
            "timeframe": "1m",
            "session": "US regular hours",
            "start": _utc_text(self.universe.start),
            "end_exclusive": _utc_text(self.universe.end),
            "storage_root": str(self.root.resolve()),
            "estimated_minimum_requests": total - completed - cached,
            "estimated_maximum_requests_before_retries": (total - completed - cached) * 3,
            "estimated_storage_gib": "1-10 (range; IEX bar density and compression vary)",
        }

    def preflight(self, client: Any, *, min_free_bytes: int = _MIN_FREE_BYTES) -> dict[str, Any]:
        plan = self.plan()
        parent = self.root if self.root.exists() else self.root.parent
        if not parent.exists() or not os.access(parent, os.W_OK):
            raise ValueError(f"research storage is not writable: {parent}")
        free = shutil.disk_usage(parent).free
        if free < min_free_bytes:
            raise ValueError(f"research storage has too little free space: {free} bytes")
        # One request near the oldest boundary tests the full-year entitlement.
        data = client.get(
            "/v2/stocks/bars",
            params={
                "symbols": "NVDA",
                "timeframe": "1Min",
                "feed": "iex",
                "adjustment": "raw",
                "start": "2025-10-09T13:30:00Z",
                "end": "2025-10-09T13:35:00Z",
                "limit": 10,
            },
        )
        if not isinstance(data, Mapping) or not isinstance(data.get("bars"), Mapping):
            raise ValueError("Alpaca historical preflight returned an invalid response")
        bars = data["bars"].get("NVDA", [])
        if not isinstance(bars, list) or not bars:
            raise ValueError(
                "Alpaca IEX preflight returned no NVDA bars; account coverage is unconfirmed"
            )
        return {
            **plan,
            "free_disk_bytes": free,
            "credentials": "present",
            "historical_api": "accessible",
            "probe_symbol": "NVDA",
            "probe_bars": len(bars),
        }

    def _fetch(
        self, client: Any, symbol: str, left: datetime, right: datetime
    ) -> tuple[HistoricalBar, ...]:
        bars = AlpacaHistoricalBars(client, max_pages=3, page_limit=10000).fetch_bars(
            symbol, "1m", left, right, feed="iex", adjustment="raw"
        )
        result = tuple(bar for bar in bars if is_regular_hours(bar.timestamp))
        if not result:
            raise ValueError("no regular-session IEX bars in window")
        return result

    def _record_window(
        self,
        state: dict[str, Any],
        symbol: str,
        left: datetime,
        right: datetime,
        bars: tuple[HistoricalBar, ...],
        snapshot: DatasetSnapshot,
    ) -> None:
        key = _window_key(symbol, left)
        report = {
            "universe_id": self.universe.universe_id,
            "universe_sha256": self.universe.sha256,
            "symbol": symbol,
            "symbol_identity": self.universe.symbol_metadata[symbol],
            "window_start": _utc_text(left),
            "window_end_exclusive": _utc_text(right),
            "dataset_id": snapshot.dataset_id,
            "data_sha256": snapshot.sha256,
            "verified_at": _utc_text(datetime.now(UTC)),
            **coverage(bars, left, right),
        }
        path = self.progress_dir / "coverage" / f"{symbol}-{left.date().isoformat()}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_text(path, json.dumps(report, sort_keys=True, indent=2) + "\n")
        state["windows"][key] = {
            "dataset_id": snapshot.dataset_id,
            "coverage_file": str(path.relative_to(self.progress_dir)),
            "coverage_sha256": _sha256(path),
            "bar_count": len(bars),
            "missing_minutes": report["missing_minutes"],
        }
        state["failures"].pop(key, None)
        self._save(state)

    def _finalize(self, state: dict[str, Any], symbol: str) -> None:
        bars_by_time: dict[datetime, HistoricalBar] = {}
        for left, right in self.universe.windows():
            record = state["windows"].get(_window_key(symbol, left))
            if not record or not self._snapshot(record["dataset_id"], symbol, left, right):
                return
            for bar in self.repo.available(
                symbol, left, right, feed="iex", dataset_id=record["dataset_id"]
            ):
                if not is_regular_hours(bar.timestamp):
                    continue
                old = bars_by_time.setdefault(bar.timestamp, bar)
                if old != bar:
                    raise ValueError(f"conflicting cached bar for {symbol} at {bar.timestamp}")
        if not bars_by_time:
            return
        manifest = self.repo.persist(
            symbol,
            bars_by_time.values(),
            feed="iex",
            asset_class=self.universe.symbol_metadata[symbol]["asset_class"],
            coverage_start=self.universe.start,
            coverage_end=self.universe.end,
        )
        self._remember(manifest)
        state["final"][symbol] = {
            "dataset_id": manifest.dataset_id,
            "sha256": manifest.sha256,
            "bar_count": manifest.bar_count,
            "completed_at": _utc_text(datetime.now(UTC)),
        }
        state["failures"].pop(f"{symbol}:final", None)
        self._save(state)

    def _finalize_if_ready(
        self, state: dict[str, Any], symbol: str, progress: Callable[[str], None]
    ) -> None:
        if any(
            _window_key(symbol, left) not in state["windows"] for left, _ in self.universe.windows()
        ):
            return
        existing = state["final"].get(symbol)
        if existing and self._snapshot(
            existing["dataset_id"], symbol, self.universe.start, self.universe.end
        ):
            return
        try:
            self._finalize(state, symbol)
            if symbol in state["final"]:
                progress(f"symbol complete: {symbol} {state['final'][symbol]['dataset_id']}")
        except Exception as exc:
            state["failures"][f"{symbol}:final"] = {
                "type": type(exc).__name__,
                "message": str(exc)[:300],
                "at": _utc_text(datetime.now(UTC)),
            }
            self._save(state)
            progress(f"FAILED {symbol}:final: {type(exc).__name__}: {str(exc)[:160]}")

    def acquire(
        self, client: Any, *, workers: int = 1, progress: Callable[[str], None] = print
    ) -> int:
        if not 1 <= workers <= 2:
            raise ValueError("workers must be 1 or 2 for bounded API acquisition")
        self.progress_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.progress_dir / ".acquire.lock"
        with lock_path.open("a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("another acquisition is already running") from None
            state = self._state()
            jobs: list[tuple[str, datetime, datetime]] = []
            for symbol in self.universe.symbols:
                for left, right in self.universe.windows():
                    key = _window_key(symbol, left)
                    record = state["windows"].get(key)
                    if record and self._snapshot(record["dataset_id"], symbol, left, right):
                        continue
                    cached = self._cached(symbol, left, right)
                    if cached:
                        bars = tuple(
                            bar
                            for bar in self.repo.available(
                                symbol, left, right, feed="iex", dataset_id=cached.dataset_id
                            )
                            if is_regular_hours(bar.timestamp)
                        )
                        if bars:
                            # A crash may have published the manifest before
                            # its catalog insert; replay that idempotent insert.
                            self.repo._record_catalog(cached)
                            self._record_window(state, symbol, left, right, bars, cached)
                            continue
                    jobs.append((symbol, left, right))
                self._finalize_if_ready(state, symbol, progress)
            progress(f"acquisition: {len(jobs)} pending monthly windows; workers={workers}")
            with ThreadPoolExecutor(max_workers=workers) as pool:
                iterator = iter(jobs)
                active: dict[Future[tuple[HistoricalBar, ...]], tuple[str, datetime, datetime]] = {}

                def fill() -> None:
                    while len(active) < workers * 2:
                        try:
                            job = next(iterator)
                        except StopIteration:
                            return
                        active[pool.submit(self._fetch, client, *job)] = job

                fill()
                finished = 0
                while active:
                    done, _ = wait(active, return_when=FIRST_COMPLETED)
                    for future in done:
                        symbol, left, right = active.pop(future)
                        key = _window_key(symbol, left)
                        try:
                            bars = future.result()
                            snapshot = self.repo.persist(
                                symbol,
                                bars,
                                feed="iex",
                                asset_class=self.universe.symbol_metadata[symbol]["asset_class"],
                                coverage_start=left,
                                coverage_end=right,
                            )
                            self._remember(snapshot)
                            self._record_window(state, symbol, left, right, bars, snapshot)
                            self._finalize_if_ready(state, symbol, progress)
                        except Exception as exc:
                            state["failures"][key] = {
                                "type": type(exc).__name__,
                                "message": str(exc)[:300],
                                "at": _utc_text(datetime.now(UTC)),
                            }
                            self._save(state)
                            progress(f"FAILED {key}: {type(exc).__name__}: {str(exc)[:160]}")
                        finished += 1
                        if finished % 10 == 0 or finished == len(jobs):
                            progress(
                                f"progress: {finished}/{len(jobs)} attempted; "
                                f"failures={len(state['failures'])}"
                            )
                    fill()
            for symbol in self.universe.symbols:
                if symbol in state["final"] and self._snapshot(
                    state["final"][symbol]["dataset_id"],
                    symbol,
                    self.universe.start,
                    self.universe.end,
                ):
                    final = next(
                        item
                        for item in self._known_manifests()
                        if item.dataset_id == state["final"][symbol]["dataset_id"]
                    )
                    self.repo._record_catalog(final)
                    continue
                self._finalize_if_ready(state, symbol, progress)
            progress(
                f"complete: {len(state['final'])}/{len(self.universe.symbols)} symbols; "
                f"failures={len(state['failures'])}"
            )
            return (
                0
                if len(state["final"]) == len(self.universe.symbols) and not state["failures"]
                else 1
            )

    def status(self) -> dict[str, Any]:
        state = self._state()
        planned = self.plan()
        total = len(self.universe.symbols) * len(self.universe.windows())
        missing = sum(item["missing_minutes"] for item in state["windows"].values())
        symbols: dict[str, Any] = {}
        manifests = {item.dataset_id: item for item in self._known_manifests()}
        for symbol in self.universe.symbols:
            records = [
                state["windows"][_window_key(symbol, left)]
                for left, _ in self.universe.windows()
                if _window_key(symbol, left) in state["windows"]
            ]
            final = state["final"].get(symbol)
            final_manifest = manifests.get(final["dataset_id"]) if final else None
            symbols[symbol] = {
                "completed_windows": len(records),
                "total_windows": len(self.universe.windows()),
                "observed_bars_in_windows": sum(record["bar_count"] for record in records),
                "missing_bar_minutes": sum(record["missing_minutes"] for record in records),
                "final_dataset_id": final["dataset_id"] if final else None,
                "coverage_start": final_manifest.start if final_manifest else None,
                "coverage_end_exclusive": final_manifest.end if final_manifest else None,
                "first_bar": final_manifest.first_bar if final_manifest else None,
                "last_bar": final_manifest.last_bar if final_manifest else None,
                "final_bar_count": final_manifest.bar_count if final_manifest else None,
            }
        lock_path = self.progress_dir / ".acquire.lock"
        running = False
        if lock_path.exists():
            with lock_path.open("rb") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                except BlockingIOError:
                    running = True
        return {
            "universe_id": self.universe.universe_id,
            "completed_symbols": sorted(state["final"]),
            "pending_symbols": [s for s in self.universe.symbols if s not in state["final"]],
            "completed_windows": len(state["windows"]),
            "total_windows": total,
            "pending_windows": total - len(state["windows"]),
            "cached_windows_not_checkpointed": planned["cached_windows"],
            "pending_api_windows": planned["pending_windows"],
            "missing_bar_minutes": missing,
            "failures": state["failures"],
            "running": running,
            "symbols": symbols,
            "storage_root": str(self.root.resolve()),
        }

    def verify(self) -> dict[str, Any]:
        state = self._state()
        errors: list[str] = []
        checked = 0
        catalog: dict[str, tuple[Any, ...]] = {}
        try:
            with duckdb.connect(str(self.repo.catalog_path), read_only=True) as connection:
                catalog = {
                    row[0]: row
                    for row in connection.execute(
                        "SELECT dataset_id, sha256, symbol, asset_class, feed, adjustment, "
                        "coverage_start, coverage_end, bar_count, data_file FROM datasets"
                    ).fetchall()
                }
        except duckdb.Error as exc:
            errors.append(f"catalog unavailable or unreadable: {type(exc).__name__}")

        def catalog_matches(snapshot: DatasetSnapshot) -> bool:
            row = catalog.get(snapshot.dataset_id)
            return row is not None and (
                row[1:6]
                == (
                    snapshot.sha256,
                    snapshot.symbol,
                    snapshot.asset_class,
                    snapshot.feed,
                    snapshot.adjustment,
                )
                and row[6].astimezone(UTC) == _instant(snapshot.start)
                and row[7].astimezone(UTC) == _instant(snapshot.end)
                and row[8] == snapshot.bar_count
                and row[9] == snapshot.data_file
            )

        for symbol in self.universe.symbols:
            for left, right in self.universe.windows():
                key = _window_key(symbol, left)
                record = state["windows"].get(key)
                if not record:
                    continue
                snapshot = self._snapshot(record["dataset_id"], symbol, left, right)
                cover = self.progress_dir / record["coverage_file"]
                if (
                    not snapshot
                    or not cover.is_file()
                    or _sha256(cover) != record["coverage_sha256"]
                ):
                    errors.append(f"{key}: missing or corrupt snapshot/coverage")
                    continue
                if not catalog_matches(snapshot):
                    errors.append(f"{key}: catalog differs from manifest")
                    continue
                try:
                    bars = tuple(
                        bar
                        for bar in self.repo.available(
                            symbol, left, right, feed="iex", dataset_id=snapshot.dataset_id
                        )
                        if is_regular_hours(bar.timestamp)
                    )
                    report = json.loads(cover.read_text(encoding="utf-8"))
                    measured = coverage(bars, left, right)
                    if (
                        len({bar.timestamp for bar in bars}) != len(bars)
                        or any(report.get(field) != value for field, value in measured.items())
                        or report.get("dataset_id") != snapshot.dataset_id
                    ):
                        errors.append(f"{key}: Parquet rows or coverage report differ")
                except (OSError, ValueError, duckdb.Error) as exc:
                    errors.append(f"{key}: {type(exc).__name__}")
                    continue
                checked += 1
            final = state["final"].get(symbol)
            if final:
                snapshot = self._snapshot(
                    final["dataset_id"], symbol, self.universe.start, self.universe.end
                )
                if not snapshot or snapshot.sha256 != final["sha256"]:
                    errors.append(f"{symbol}: missing or corrupt final snapshot")
                elif not catalog_matches(snapshot):
                    errors.append(f"{symbol}: catalog differs from final manifest")
                else:
                    with duckdb.connect(":memory:") as connection:
                        count, distinct, lo, hi = connection.execute(
                            "SELECT count(*), count(DISTINCT timestamp), "
                            "min(timestamp), max(timestamp) FROM read_parquet(?)",
                            [str(self.root / snapshot.data_file)],
                        ).fetchone()
                    window_count = sum(
                        state["windows"].get(_window_key(symbol, left), {}).get("bar_count", 0)
                        for left, _ in self.universe.windows()
                    )
                    if (
                        count != snapshot.bar_count
                        or count != distinct
                        or count != window_count
                        or lo < self.universe.start
                        or hi >= self.universe.end
                    ):
                        errors.append(f"{symbol}: Parquet count/identity/coverage mismatch")
        return {
            "verified_windows": checked,
            "expected_windows": len(self.universe.symbols) * len(self.universe.windows()),
            "final_snapshots": len(state["final"]),
            "expected_final_snapshots": len(self.universe.symbols),
            "errors": errors,
            "ok": not errors
            and not state["failures"]
            and len(state["final"]) == len(self.universe.symbols),
        }
