from datetime import UTC, datetime
from decimal import Decimal
from tempfile import TemporaryDirectory

import duckdb

from mike_trade.market_data.alpaca_history import Adjustment, HistoricalBar
from mike_trade.research.datasets import HistoricalDatasetRepository, _missing_minute_ranges


class _BarsClient:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, path, params=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        stamp = datetime(2026, 7, 9, 13, 30, tzinfo=UTC).isoformat().replace("+00:00", "Z")
        return {
            "bars": {
                "NVDA": [
                    {
                        "t": stamp,
                        "o": "100",
                        "h": "101",
                        "l": "99",
                        "c": "100.5",
                        "v": 1000,
                        "vw": "100.2",
                        "n": 12,
                    }
                ]
            }
        }


def test_parquet_snapshot_integrity_and_fully_covered_acquisition_reuse() -> None:
    start = datetime(2026, 7, 9, 0, tzinfo=UTC)
    end = datetime(2026, 7, 10, 0, tzinfo=UTC)
    with TemporaryDirectory() as directory:
        repository = HistoricalDatasetRepository(directory)
        client = _BarsClient()
        first = repository.acquire(client, ["NVDA"], start, end)
        assert len(first) == 1
        assert first[0].bar_count == 1
        bars = repository.available("NVDA", start, end, feed="iex")
        assert len(bars) == 1
        assert bars[0].timestamp == datetime(2026, 7, 9, 13, 30, tzinfo=UTC)
        second = repository.acquire(client, ["NVDA"], start, end)
        assert second[0].dataset_id == first[0].dataset_id
        assert client.calls == 1


def _bar(symbol: str, stamp: datetime) -> HistoricalBar:
    return HistoricalBar(
        symbol,
        stamp,
        Decimal("100"),
        Decimal("101"),
        Decimal("99"),
        Decimal("100.5"),
        1000,
        Decimal("100.2"),
        12,
        "iex",
        Adjustment.RAW,
    )


class _RepairBarsClient:
    def __init__(self, symbol: str, bars: tuple[HistoricalBar, ...]) -> None:
        self.symbol = symbol
        self.bars = bars
        self.calls: list[dict[str, object]] = []

    def get(self, path: str, params: dict[str, object]) -> object:
        self.calls.append(params)
        start = datetime.fromisoformat(str(params["start"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(params["end"]).replace("Z", "+00:00"))
        selected = [
            {
                "t": bar.timestamp.isoformat().replace("+00:00", "Z"),
                "o": str(bar.open), "h": str(bar.high), "l": str(bar.low),
                "c": str(bar.close), "v": bar.volume, "vw": str(bar.vwap),
                "n": bar.trade_count,
            }
            for bar in self.bars
            if start <= bar.timestamp < end
        ]
        return {"bars": {self.symbol: selected}}


def test_incremental_repair_finds_internal_session_gap_and_retains_extended_hours(
    tmp_path,
) -> None:
    repo = HistoricalDatasetRepository(tmp_path)
    start = datetime(2026, 7, 9, 0, tzinfo=UTC)
    end = datetime(2026, 7, 11, 0, tzinfo=UTC)
    existing = (
        _bar("TSLA", datetime(2026, 7, 9, 12, 0, tzinfo=UTC)),
        _bar("TSLA", datetime(2026, 7, 9, 13, 30, tzinfo=UTC)),
    )
    old = repo.persist(
        "TSLA", existing, feed="iex", coverage_start=start, coverage_end=end
    )
    fetched = (
        _bar("TSLA", datetime(2026, 7, 10, 12, 0, tzinfo=UTC)),
        _bar("TSLA", datetime(2026, 7, 10, 13, 30, tzinfo=UTC)),
    )
    client = _RepairBarsClient("TSLA", fetched)

    (repaired,) = repo.acquire(
        client, ["TSLA"], start, end, minimum_premarket_bars=1
    )

    assert repaired.dataset_id != old.dataset_id
    assert len(client.calls) == 1
    assert str(client.calls[0]["start"]).startswith("2026-07-10T08:00:00")
    bars = repo.available("TSLA", start, end, feed="iex", dataset_id=repaired.dataset_id)
    assert {bar.timestamp for bar in bars} == {bar.timestamp for bar in (*existing, *fetched)}
    report = repo.minute_coverage_report(
        "TSLA", start, end, dataset_id=repaired.dataset_id, minimum_premarket_bars=1
    )
    assert report["missing_rth_sessions"] == []
    assert report["premarket_sessions_represented"] == 2
    assert report["sufficient_ema_warmup_sessions"] == 2
    assert report["duplicate_timestamps"] == 0
    with duckdb.connect(str(repo.catalog_path), read_only=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM datasets WHERE dataset_id = ?", [repaired.dataset_id]
        ).fetchone()[0] == 1

    calls = len(client.calls)
    assert repo.acquire(
        client, ["TSLA"], start, end, minimum_premarket_bars=1
    )[0].dataset_id == repaired.dataset_id
    assert len(client.calls) == calls


def test_incremental_repair_deduplicates_identical_overlapping_premarket_bar(tmp_path) -> None:
    repo = HistoricalDatasetRepository(tmp_path)
    start = datetime(2026, 7, 9, 0, tzinfo=UTC)
    end = datetime(2026, 7, 10, 0, tzinfo=UTC)
    premarket = _bar("QQQ", datetime(2026, 7, 9, 12, 0, tzinfo=UTC))
    regular = _bar("QQQ", datetime(2026, 7, 9, 13, 30, tzinfo=UTC))
    repo.persist(
        "QQQ", (premarket, regular), feed="iex", coverage_start=start, coverage_end=end
    )
    second = _bar("QQQ", datetime(2026, 7, 9, 12, 1, tzinfo=UTC))
    client = _RepairBarsClient("QQQ", (premarket, second))

    (repaired,) = repo.acquire(
        client, ["QQQ"], start, end, minimum_premarket_bars=2
    )
    bars = repo.available("QQQ", start, end, feed="iex", dataset_id=repaired.dataset_id)
    assert len(bars) == 3
    assert len({bar.timestamp for bar in bars}) == 3
    assert repo.minute_coverage_report(
        "QQQ", start, end, dataset_id=repaired.dataset_id, minimum_premarket_bars=2
    )["sufficient_ema_warmup_sessions"] == 1


def test_interrupted_repair_checkpoints_completed_missing_session(tmp_path) -> None:
    repo = HistoricalDatasetRepository(tmp_path)
    start = datetime(2026, 7, 9, 0, tzinfo=UTC)
    end = datetime(2026, 7, 11, 0, tzinfo=UTC)
    existing = _bar("TSLA", datetime(2026, 7, 9, 13, 30, tzinfo=UTC))
    repo.persist("TSLA", (existing,), feed="iex", coverage_start=start, coverage_end=end)
    repaired = _bar("TSLA", datetime(2026, 7, 10, 13, 30, tzinfo=UTC))

    class InterruptedClient(_RepairBarsClient):
        def get(self, path: str, params: dict[str, object]) -> object:
            if self.calls:
                raise RuntimeError("synthetic provider interruption")
            return super().get(path, params)

    client = InterruptedClient("TSLA", (repaired,))
    import pytest

    with pytest.raises(RuntimeError, match="synthetic provider interruption"):
        repo.acquire(client, ["TSLA"], start, end, minimum_premarket_bars=1)

    newest = max(repo.snapshots(), key=lambda item: item.acquired_at)
    bars = repo.available("TSLA", start, end, feed="iex", dataset_id=newest.dataset_id)
    assert {bar.timestamp for bar in bars} == {existing.timestamp, repaired.timestamp}
    assert newest.bar_count == 2


def test_repair_reuses_observations_across_immutable_snapshots(tmp_path) -> None:
    repo = HistoricalDatasetRepository(tmp_path)
    start = datetime(2026, 7, 7, 0, tzinfo=UTC)
    end = datetime(2026, 7, 11, 0, tzinfo=UTC)
    first = _bar("TSLA", datetime(2026, 7, 7, 13, 30, tzinfo=UTC))
    last = _bar("TSLA", datetime(2026, 7, 10, 13, 30, tzinfo=UTC))
    repo.persist("TSLA", (first,), feed="iex", coverage_start=start, coverage_end=end)
    repo.persist("TSLA", (last,), feed="iex", coverage_start=start, coverage_end=end)

    assert repo.all_bars("TSLA", feed="iex") == (first, last)


def test_missing_rth_sessions_are_coalesced_without_premarket_only_requests() -> None:
    start = datetime(2026, 7, 7, 0, tzinfo=UTC)
    end = datetime(2026, 7, 11, 0, tzinfo=UTC)
    bars = (
        _bar("TSLA", datetime(2026, 7, 7, 13, 30, tzinfo=UTC)),
        _bar("TSLA", datetime(2026, 7, 10, 13, 30, tzinfo=UTC)),
    )

    missing = _missing_minute_ranges(bars, start, end, minimum_premarket_bars=0)

    assert missing == [
        (
            datetime(2026, 7, 8, 8, 0, tzinfo=UTC),
            datetime(2026, 7, 9, 20, 0, tzinfo=UTC),
        )
    ]
