"""Research/backtest command path for model-driven strategies (reference series, feeds, pins)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from model_fixtures import build_model

from mike_trade.research.commands import ExperimentSpec, _cached_snapshot_bars, _run_one
from mike_trade.research.datasets import DatasetSnapshot, HistoricalDatasetRepository

START = datetime(2026, 3, 10, 13, 30, tzinfo=UTC)
END = datetime(2026, 3, 10, 20, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
TAPE = [*["100"] * 35, "100.3", "100.6", "100.9", *["100.9"] * 12]
PARAMS = {
    "xgboost_probability_buy": {"model_id": "xgboost_buy_m", "model_version": "1.0.0"},
    "lightgbm_probability_sell": {"model_id": "lightgbm_sell_m", "model_version": "1.0.0"},
}


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: D
    high: D
    low: D
    close: D
    volume: int
    vwap: D | None
    trade_count: int | None = None


def bars(closes: list[str]) -> tuple[Bar, ...]:
    out, previous = [], D(closes[0])
    for index, text in enumerate(closes):
        close = D(text)
        out.append(
            Bar(
                START + index * MINUTE,
                previous,
                max(previous, close) + D("0.05"),
                min(previous, close) - D("0.05"),
                close,
                100000,
                close,
            )
        )
        previous = close
    return tuple(out)


def snapshot(symbol: str, feed: str = "iex") -> DatasetSnapshot:
    did = f"alpaca-{feed}-{symbol.lower()}-raw-1m-test"
    return DatasetSnapshot(
        dataset_id=did,
        provider="alpaca",
        feed=feed,
        symbol=symbol,
        asset_class="us_equity",
        timeframe="1m",
        adjustment="raw",
        acquired_at="2026-10-12T12:00:00+00:00",
        start="2026-03-01T00:00:00+00:00",
        end="2026-03-31T00:00:00+00:00",
        first_bar=START.isoformat(),
        last_bar=(START + 50 * MINUTE).isoformat(),
        bar_count=50,
        data_file=f"snapshots/{did}.parquet",
        sha256="ab" * 32,
    )


@pytest.fixture(scope="module")
def model_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("research-models")
    build_model(root, algorithm="xgboost", role="buy", model_id="xgboost_buy_m")
    build_model(root, algorithm="lightgbm", role="sell", model_id="lightgbm_sell_m")
    return root


def install(monkeypatch, tmp_path, snapshots, series) -> None:
    repo = MagicMock(spec=HistoricalDatasetRepository)
    repo.snapshots.return_value = snapshots
    monkeypatch.setattr(
        "mike_trade.research.commands.HistoricalDatasetRepository", lambda root: repo
    )
    _cached_snapshot_bars.cache_clear()

    def fake(root: str, symbol: str, start: str, end: str, dataset_id: str, **kwargs: Any):
        return series[symbol]

    monkeypatch.setattr("mike_trade.research.commands._cached_snapshot_bars", fake)


def run(tmp_path, models_dir, **overrides):
    arguments: dict[str, Any] = {
        "buy_strategies": ("xgboost_probability_buy",),
        "sell_strategies": ("lightgbm_probability_sell",),
        "parameters": PARAMS,
        "protective_stop": {"type": "native_trailing", "trail_percent": "1.5"},
        "model_root": str(models_dir),
        "end_of_day_close": {"enabled": False},
    }
    arguments.update(overrides)
    return _run_one(
        "AAA", START, END, str(tmp_path), str(tmp_path / "results"), 7, "model_pair", **arguments
    )


def test_run_one_runs_a_model_pair_with_same_feed_reference_bars(monkeypatch, tmp_path, model_root):
    install(
        monkeypatch,
        tmp_path,
        [snapshot("AAA"), snapshot("SPY")],
        {"AAA": bars(TAPE), "SPY": bars(["400"] * len(TAPE))},
    )
    result = run(tmp_path, model_root)
    assert result["legs"] == {
        "buy": ["xgboost_probability_buy"],
        "sell": ["lightgbm_probability_sell"],
    }
    ledger = result["trade_ledger"]
    assert len(ledger) == 1
    assert ledger[0]["entry_strategy"] == "xgboost_probability_buy"
    assert ledger[0]["exit_strategy"] == "lightgbm_probability_sell"


def test_run_one_without_a_model_root_fails_closed(monkeypatch, tmp_path, model_root):
    install(
        monkeypatch,
        tmp_path,
        [snapshot("AAA"), snapshot("SPY")],
        {"AAA": bars(TAPE), "SPY": bars(["400"] * len(TAPE))},
    )
    with pytest.raises(ValueError, match="model_root"):
        run(tmp_path, model_root, model_root=None)


def test_run_one_never_substitutes_a_missing_or_foreign_feed_reference(
    monkeypatch, tmp_path, model_root
):
    series = {"AAA": bars(TAPE), "SPY": bars(["400"] * len(TAPE))}
    install(monkeypatch, tmp_path, [snapshot("AAA")], series)  # no SPY snapshot at all
    with pytest.raises(ValueError, match="reference symbol SPY"):
        run(tmp_path, model_root)
    install(monkeypatch, tmp_path, [snapshot("AAA"), snapshot("SPY", feed="sip")], series)
    with pytest.raises(ValueError, match="reference symbol SPY"):  # an IEX model never takes SIP
        run(tmp_path, model_root)
    install(monkeypatch, tmp_path, [snapshot("AAA", "sip"), snapshot("SPY", "sip")], series)
    with pytest.raises(ValueError, match="feeds are never mixed"):
        run(tmp_path, model_root, feed="sip")


def test_experiment_spec_accepts_a_model_root_only_as_a_path_string():
    spec = ExperimentSpec(
        "AAA",
        "model_pair",
        "2026-03-10T13:30:00Z",
        "2026-03-10T20:00:00Z",
        buy_strategies=("xgboost_probability_buy",),
        model_root="research/models",
    )
    spec.validate()
    bad = ExperimentSpec(
        "AAA",
        "model_pair",
        "2026-03-10T13:30:00Z",
        "2026-03-10T20:00:00Z",
        buy_strategies=("xgboost_probability_buy",),
        model_root="  ",
    )
    with pytest.raises(ValueError, match="model_root"):
        bad.validate()


def test_technical_runs_do_not_touch_model_machinery(monkeypatch, tmp_path):
    install(monkeypatch, tmp_path, [snapshot("AAA")], {"AAA": bars(TAPE)})
    result = _run_one(
        "AAA",
        START,
        END,
        str(tmp_path),
        str(tmp_path / "results"),
        7,
        "vwap_bounce",
        end_of_day_close={"enabled": False},
    )
    assert result["legs"]["buy"] == ["vwap_bounce"]
