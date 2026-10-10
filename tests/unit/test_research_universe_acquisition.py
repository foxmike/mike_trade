"""Focused checks for the operator-started universe acquisition path."""

from __future__ import annotations

import fcntl
import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest

from mike_trade.market_data.alpaca_history import AlpacaHistoricalBars
from mike_trade.research.universe_acquisition import (
    BoundedAlpacaClient,
    Universe,
    UniverseAcquisition,
    coverage,
)

_BAR = {
    "t": "2026-07-09T13:30:00Z",
    "o": "100",
    "h": "101",
    "l": "99",
    "c": "100.5",
    "v": 1000,
    "vw": "100.2",
    "n": 12,
}


class _FakeBars:
    def __init__(self, *, failures: int = 0) -> None:
        self.calls = 0
        self.failures = failures

    def get(self, path: str, params: object = None) -> object:
        self.calls += 1
        if self.calls <= self.failures:
            raise ValueError("temporary test failure")
        return {"bars": {"NVDA": [_BAR]}}


def _universe(tmp_path: Path) -> Universe:
    return Universe(
        path=tmp_path / "universe.json",
        universe_id="us-liquid-research-100-v1",
        selection_at="2026-10-10T14:42:28Z",
        symbols=("NVDA",),
        symbol_metadata={
            "NVDA": {"symbol": "NVDA", "asset_class": "us_equity", "group": "technology"}
        },
        start=datetime(2026, 7, 9, tzinfo=UTC),
        end=datetime(2026, 8, 9, tzinfo=UTC),
        sha256="test-universe-hash",
    )


def test_frozen_universe_has_exactly_100_unique_symbols(tmp_path: Path) -> None:
    path = Path(__file__).resolve().parents[2] / "config/research_universe_100_v1.json"
    universe = Universe.load(path)
    assert len(universe.symbols) == len(set(universe.symbols)) == 100
    assert len(universe.windows()) == 12
    raw = json.loads(path.read_text())
    raw["symbols"][1]["symbol"] = raw["symbols"][0]["symbol"]
    changed = tmp_path / "bad.json"
    changed.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="duplicate"):
        Universe.load(changed)


def test_preflight_probes_oldest_window_without_writing(tmp_path: Path) -> None:
    class Probe:
        def __init__(self) -> None:
            self.params: dict[str, object] = {}

        def get(self, path: str, params: dict[str, object]) -> object:
            self.params = params
            return {"bars": {"NVDA": [_BAR]}}

    job = UniverseAcquisition(_universe(tmp_path), tmp_path / "data")
    probe = Probe()
    report = job.preflight(probe, min_free_bytes=0)
    assert report["historical_api"] == "accessible"
    assert probe.params["start"] == "2025-10-09T13:30:00Z"
    assert probe.params["feed"] == "iex"
    assert not (tmp_path / "data").exists()


def test_pagination_and_exclusive_end_are_preserved() -> None:
    class Pages:
        def __init__(self) -> None:
            self.params: list[dict[str, object]] = []

        def get(self, path: str, params: dict[str, object]) -> object:
            self.params.append(params)
            if len(self.params) == 1:
                return {"bars": {"NVDA": [_BAR]}, "next_page_token": "next"}
            edge = dict(_BAR, t="2026-07-09T13:32:00Z")
            return {"bars": {"NVDA": [edge]}}

    client = Pages()
    bars = AlpacaHistoricalBars(client, max_pages=3, page_limit=10000).fetch_bars(
        "NVDA",
        "1m",
        datetime(2026, 7, 9, 13, 30, tzinfo=UTC),
        datetime(2026, 7, 9, 13, 32, tzinfo=UTC),
        feed="iex",
        adjustment="raw",
    )
    assert len(bars) == 1
    assert client.params[0]["limit"] == 10000
    assert client.params[1]["page_token"] == "next"


def test_interrupted_window_retries_and_final_snapshot_is_compatible(tmp_path: Path) -> None:
    job = UniverseAcquisition(_universe(tmp_path), tmp_path / "data")
    client = _FakeBars(failures=1)
    assert job.acquire(client, progress=lambda _: None) == 1
    assert len(job.status()["failures"]) == 1
    assert job.acquire(client, progress=lambda _: None) == 0
    assert client.calls == 2
    state = job.status()
    assert state["completed_windows"] == 1
    assert state["completed_symbols"] == ["NVDA"]
    assert state["missing_bar_minutes"] > 0
    assert job.verify()["ok"]
    assert len(job.repo.available("NVDA", job.universe.start, job.universe.end, feed="iex")) == 1
    assert job.acquire(client, progress=lambda _: None) == 0
    assert client.calls == 2


def test_resume_discovers_snapshot_published_before_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = UniverseAcquisition(_universe(tmp_path), tmp_path / "data")
    client = _FakeBars()
    original = job._record_window

    def interrupt(*args: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(job, "_record_window", interrupt)
    with pytest.raises(KeyboardInterrupt):
        job.acquire(client, progress=lambda _: None)
    assert client.calls == 1
    assert len(job.repo.snapshots()) == 1
    monkeypatch.setattr(job, "_record_window", original)
    assert job.acquire(client, progress=lambda _: None) == 0
    assert client.calls == 1
    assert job.verify()["ok"]


def test_existing_compatible_snapshot_is_reused_with_regular_session_filter(tmp_path: Path) -> None:
    job = UniverseAcquisition(_universe(tmp_path), tmp_path / "data")
    regular = AlpacaHistoricalBars(_FakeBars()).fetch_bars(
        "NVDA", "1m", job.universe.start, job.universe.end, feed="iex", adjustment="raw"
    )[0]
    premarket = replace(regular, timestamp=datetime(2026, 7, 9, 12, 0, tzinfo=UTC))
    job.repo.persist(
        "NVDA",
        (premarket, regular),
        feed="iex",
        coverage_start=job.universe.start,
        coverage_end=job.universe.end,
    )
    client = _FakeBars()
    assert job.acquire(client, progress=lambda _: None) == 0
    assert client.calls == 0
    assert job.status()["symbols"]["NVDA"]["final_bar_count"] == 1
    assert job.verify()["ok"]


def test_duplicate_run_lock_and_snapshot_checksum(tmp_path: Path) -> None:
    job = UniverseAcquisition(_universe(tmp_path), tmp_path / "data")
    job.progress_dir.mkdir(parents=True)
    with (job.progress_dir / ".acquire.lock").open("a+b") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already running"):
            job.acquire(_FakeBars(), progress=lambda _: None)
    assert job.acquire(_FakeBars(), progress=lambda _: None) == 0
    final = job._state()["final"]["NVDA"]
    snapshot = next(s for s in job.repo.snapshots() if s.dataset_id == final["dataset_id"])
    with (job.root / snapshot.data_file).open("ab") as stream:
        stream.write(b"corrupt")
    assert not job.verify()["ok"]


def test_failed_parquet_write_keeps_earlier_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = UniverseAcquisition(_universe(tmp_path), tmp_path / "data")
    bar = AlpacaHistoricalBars(_FakeBars()).fetch_bars(
        "NVDA", "1m", job.universe.start, job.universe.end, feed="iex", adjustment="raw"
    )[0]
    good = job.repo.persist(
        "NVDA", (bar,), feed="iex", coverage_start=job.universe.start, coverage_end=job.universe.end
    )
    original = (job.root / good.data_file).read_bytes()

    def fail_write(*args: object, **kwargs: object) -> None:
        raise OSError("interrupted write")

    monkeypatch.setattr(pq, "write_table", fail_write)
    with pytest.raises(OSError, match="interrupted write"):
        job.repo.persist(
            "NVDA",
            (bar,),
            feed="iex",
            coverage_start=job.universe.start,
            coverage_end=job.universe.end - timedelta(days=1),
        )
    assert (job.root / good.data_file).read_bytes() == original
    assert len(job.repo.snapshots()) == 1


def test_rate_limit_recovery_and_bounded_attempts() -> None:
    class RateLimited:
        def __init__(self) -> None:
            self.calls = 0

        def get(self, path: str, params: object = None) -> object:
            self.calls += 1
            if self.calls == 1:
                request = httpx.Request("GET", "https://data.alpaca.markets/v2/stocks/bars")
                response = httpx.Response(429, request=request, headers={"Retry-After": "2"})
                raise httpx.HTTPStatusError("rate limited", request=request, response=response)
            return {"bars": {"NVDA": [_BAR]}}

    inner = RateLimited()
    delays: list[float] = []
    elapsed = 0.0

    def sleep(seconds: float) -> None:
        nonlocal elapsed
        delays.append(seconds)
        elapsed += seconds

    client = BoundedAlpacaClient(
        inner,
        requests_per_minute=20,
        sleep=sleep,
        monotonic=lambda: elapsed,
        jitter=lambda: 0.0,
    )
    assert client.get("/v2/stocks/bars")
    assert inner.calls == 2
    assert delays == [2.0, 1.0]


def test_coverage_reports_missing_regular_minutes(tmp_path: Path) -> None:
    from decimal import Decimal

    from mike_trade.market_data.alpaca_history import Adjustment, HistoricalBar

    stamp = datetime(2026, 7, 9, 13, 30, tzinfo=UTC)
    bar = HistoricalBar(
        "NVDA",
        stamp,
        Decimal("100"),
        Decimal("101"),
        Decimal("99"),
        Decimal("100"),
        100,
        None,
        None,
        "iex",
        Adjustment.RAW,
    )
    report = coverage((bar,), stamp, stamp + timedelta(days=1))
    assert report["observed_bars"] == 1
    assert report["expected_minutes"] == 390
    assert report["missing_minutes"] == 389
    assert report["missing_intervals"] == [["2026-07-09T13:31:00Z", "2026-07-09T20:00:00Z"]]


def test_make_targets_are_readable_and_acquire_is_operator_started() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "make",
            "-n",
            "research-data-plan",
            "research-data-acquire",
            "research-data-status",
            "research-data-verify",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    for action in ("plan", "acquire-universe", "status", "verify"):
        assert f"research-data {action}" in result.stdout
    assert "docker compose" not in result.stdout
