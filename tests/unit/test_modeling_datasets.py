from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from mike_trade.market_data.alpaca_history import Adjustment, HistoricalBar
from mike_trade.modeling.datasets import FeatureDatasetRepository, ParquetDatasetProvider
from mike_trade.modeling.features import IntradayFeaturePipeline
from mike_trade.research.datasets import HistoricalDatasetRepository


def _historical_bars() -> tuple[HistoricalBar, ...]:
    start = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
    result = []
    for number in range(25):
        price = D("100") + D(number) / D("10")
        result.append(
            HistoricalBar(
                "AAA",
                start + number * timedelta(minutes=1),
                price,
                price + D("0.2"),
                price - D("0.2"),
                price + D("0.1"),
                1000 + number,
                price,
                10,
                "iex",
                Adjustment.RAW,
            )
        )
    return tuple(result)


def test_existing_snapshot_adapter_and_versioned_feature_partition(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    repository = HistoricalDatasetRepository(raw_root)
    bars = _historical_bars()
    snapshot = repository.persist(
        "AAA",
        bars,
        feed="iex",
        coverage_start=bars[0].timestamp,
        coverage_end=bars[-1].timestamp + timedelta(minutes=1),
    )
    provider = ParquetDatasetProvider(repository)
    selection = provider.select(
        ("AAA",),
        bars[0].timestamp,
        bars[-1].timestamp + timedelta(minutes=1),
        feed="iex",
        timeframe="1m",
        adjustment="raw",
        universe_id="synthetic_v1",
        dataset_ids={"AAA": snapshot.dataset_id},
    )
    model_bars = tuple(provider.iter_bars(selection, "AAA", batch_size=3))
    assert len(model_bars) == len(bars)
    assert selection.sources[0].sha256 == snapshot.sha256

    pipeline = IntradayFeaturePipeline()
    features = tuple(pipeline.transform(model_bars))
    derived = FeatureDatasetRepository(tmp_path / "derived").persist_partition(
        features,
        selection=selection,
        schema=pipeline.schema,
        symbol="AAA",
    )
    assert derived.row_count == len(features)
    assert derived.source_dataset_ids == (snapshot.dataset_id,)


def test_dataset_selection_fails_closed_on_wrong_feed(tmp_path: Path) -> None:
    repository = HistoricalDatasetRepository(tmp_path)
    bars = _historical_bars()
    repository.persist(
        "AAA",
        bars,
        feed="iex",
        coverage_start=bars[0].timestamp,
        coverage_end=bars[-1].timestamp + timedelta(minutes=1),
    )
    provider = ParquetDatasetProvider(repository)
    with pytest.raises(ValueError, match="no immutable dataset"):
        provider.select(
            ("AAA",),
            bars[0].timestamp,
            bars[-1].timestamp + timedelta(minutes=1),
            feed="sip",
            timeframe="1m",
            adjustment="raw",
            universe_id="synthetic_v1",
        )
