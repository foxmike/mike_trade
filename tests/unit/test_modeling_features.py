from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.modeling.contracts import ModelBar
from mike_trade.modeling.features import IntradayFeatureConfig, IntradayFeaturePipeline
from mike_trade.modeling.inputs import model_bar_from_candle

START = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


def _bars(
    count: int,
    *,
    symbol: str = "AAA",
    start: datetime = START,
    session: str = "XNYS-2026-03-02",
    offset: int = 0,
) -> tuple[ModelBar, ...]:
    result = []
    for number in range(count):
        moment = start + number * MINUTE
        price = D("100") + D(number + offset) / D("10")
        result.append(
            ModelBar(
                symbol,
                moment,
                moment + MINUTE,
                price,
                price + D("0.2"),
                price - D("0.2"),
                price + D("0.1"),
                D(1000 + number),
                "iex",
                "1m",
                "raw",
                session,
                price + D("0.05"),
                10,
            )
        )
    return tuple(result)


def test_feature_output_is_deterministic() -> None:
    bars = _bars(30)
    first = tuple(IntradayFeaturePipeline().transform(bars))
    second = tuple(IntradayFeaturePipeline().transform(bars))
    assert first == second
    assert tuple(first[-1].values) == IntradayFeaturePipeline().schema.names


def test_historical_and_streaming_features_are_identical_with_references() -> None:
    config = IntradayFeatureConfig(reference_symbols=("SPY",))
    primary, reference = _bars(30), _bars(30, symbol="SPY", offset=20)
    batch = tuple(IntradayFeaturePipeline(config).transform(primary, references={"SPY": reference}))
    live_pipeline = IntradayFeaturePipeline(config)
    streaming = tuple(
        result
        for bar, ref in zip(primary, reference, strict=True)
        if (result := live_pipeline.update(bar, {"SPY": ref})) is not None
    )
    assert streaming == batch
    assert "relative_strength_spy_5" in batch[-1].values


def test_incomplete_live_candle_is_rejected() -> None:
    candle = Candle(
        "AAA",
        CandleTimeframe.M1,
        START,
        START + MINUTE,
        D("100"),
        D("101"),
        D("99"),
        D("100"),
        D("1000"),
        "iex",
        False,
        CandleOrigin.STREAM_BAR,
        PriceBasis.RAW,
        CandleStatus.DEVELOPING,
    )
    with pytest.raises(ValueError, match="incomplete"):
        model_bar_from_candle(candle, vwap=D("100"))


def test_session_boundary_and_missing_minute_reset_history() -> None:
    pipeline = IntradayFeaturePipeline()
    assert tuple(pipeline.transform(_bars(21)))
    next_session_start = datetime(2026, 3, 3, 14, 30, tzinfo=UTC)
    assert (
        pipeline.update(
            _bars(
                1,
                start=next_session_start,
                session="XNYS-2026-03-03",
            )[0]
        )
        is None
    )
    for bar in _bars(21, start=next_session_start, session="XNYS-2026-03-03")[1:]:
        result = pipeline.update(bar)
    assert result is not None

    gap_pipeline = IntradayFeaturePipeline()
    for bar in _bars(21):
        ready = gap_pipeline.update(bar)
    assert ready is not None
    after_gap = _bars(1, start=START + 22 * MINUTE)[0]
    assert gap_pipeline.update(after_gap) is None


def test_invalid_numeric_model_bar_is_rejected() -> None:
    with pytest.raises(ValueError, match="close"):
        ModelBar(
            "AAA",
            START,
            START + MINUTE,
            D("100"),
            D("101"),
            D("99"),
            D("NaN"),
            D("1"),
            "iex",
            "1m",
            "raw",
            "session",
        )
