from datetime import UTC, datetime
from tempfile import TemporaryDirectory

from mike_trade.research.datasets import HistoricalDatasetRepository


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
