from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from mike_trade.market_data.candles import CandleTimeframe
from mike_trade.research.commands import (
    MULTIDAY_STRATEGIES,
    ExperimentSpec,
    _cached_snapshot_bars,
    _daily_history,
    _resolve_eod_close,
    _run_one,
    register,
    run_experiment_batch,
)
from mike_trade.research.datasets import DatasetSnapshot, HistoricalDatasetRepository


def _make_snapshot(
    feed: str,
    dataset_id: str | None = None,
    symbol: str = "IBM",
    start: str = "2026-07-09T00:00:00+00:00",
    end: str = "2026-10-09T00:00:00+00:00",
) -> DatasetSnapshot:
    did = dataset_id or f"alpaca-{feed}-{symbol.lower()}-raw-1m-20261012-abc12345"
    return DatasetSnapshot(
        dataset_id=did,
        provider="alpaca",
        feed=feed,
        symbol=symbol,
        asset_class="us_equity",
        timeframe="1m",
        adjustment="raw",
        acquired_at="2026-10-12T12:00:00+00:00",
        start=start,
        end=end,
        first_bar="2026-07-09T13:30:00+00:00",
        last_bar="2026-10-08T19:59:00+00:00",
        bar_count=19890 if feed == "sip" else 14922,
        data_file=f"snapshots/{did}.parquet",
        sha256="abc12345" * 8,
    )


def test_experiment_spec_feed_and_eod_validation() -> None:
    # Valid feed specs
    spec_sip = ExperimentSpec(
        "IBM", "vwap_bounce", "2026-07-09T00:00:00Z", "2026-10-09T00:00:00Z", feed="sip"
    )
    spec_sip.validate()
    assert spec_sip.feed == "sip"

    spec_iex = ExperimentSpec(
        "IBM", "vwap_bounce", "2026-07-09T00:00:00Z", "2026-10-09T00:00:00Z", feed="iex"
    )
    spec_iex.validate()

    # Invalid feed
    with pytest.raises(ValueError, match="experiment feed must be 'iex' or 'sip'"):
        ExperimentSpec(
            "IBM", "vwap_bounce", "2026-07-09T00:00:00Z", "2026-10-09T00:00:00Z", feed="nasdaq"
        ).validate()

    # Valid EOD close specs
    spec_eod = ExperimentSpec(
        "IBM",
        "vwap_bounce",
        "2026-07-09T00:00:00Z",
        "2026-10-09T00:00:00Z",
        end_of_day_close={"enabled": True, "at": "15:55"},
    )
    spec_eod.validate()

    spec_eod_disabled = ExperimentSpec(
        "IBM",
        "vwap_bounce",
        "2026-07-09T00:00:00Z",
        "2026-10-09T00:00:00Z",
        end_of_day_close={"enabled": False},
    )
    spec_eod_disabled.validate()

    # Invalid EOD close specs
    with pytest.raises(ValueError, match="at must be HH:MM before 16:00"):
        ExperimentSpec(
            "IBM",
            "vwap_bounce",
            "2026-07-09T00:00:00Z",
            "2026-10-09T00:00:00Z",
            end_of_day_close={"enabled": True, "at": "16:00"},
        ).validate()

    with pytest.raises(ValueError, match="accepts only enabled and at"):
        ExperimentSpec(
            "IBM",
            "vwap_bounce",
            "2026-07-09T00:00:00Z",
            "2026-10-09T00:00:00Z",
            end_of_day_close={"enabled": True, "extra": 123},
        ).validate()


def test_resolve_eod_close_intraday_default() -> None:
    # Intraday strategies default to 15:55 New York time
    resolved = _resolve_eod_close(None, ("vwap_bounce_buy", "vwap_bounce_sell"))
    assert resolved == {"enabled": True, "at": "15:55"}

    resolved_michelle = _resolve_eod_close(None, ("five_minute_orb_michelle_buy",))
    assert resolved_michelle == {"enabled": True, "at": "15:55"}


def test_resolve_eod_close_explicit_overrides() -> None:
    # Explicit disable
    resolved_off = _resolve_eod_close({"enabled": False}, ("vwap_bounce_buy",))
    assert resolved_off == {"enabled": False}

    # Explicit custom time
    resolved_custom = _resolve_eod_close(
        {"enabled": True, "at": "15:50"}, ("vwap_bounce_buy",)
    )
    assert resolved_custom == {"enabled": True, "at": "15:50"}


def test_resolve_eod_close_multiday_strategies() -> None:
    # Multi-day strategies do NOT default to EOD liquidation
    assert "cup_and_handle" in MULTIDAY_STRATEGIES
    assert "volatility_contraction_pattern" in MULTIDAY_STRATEGIES

    resolved_ch = _resolve_eod_close(None, ("cup_and_handle",))
    assert resolved_ch is None

    resolved_vcp = _resolve_eod_close(None, ("volatility_contraction_pattern",))
    assert resolved_vcp is None

    # Multi-day strategies can still explicitly configure EOD liquidation if desired
    explicit_ch = _resolve_eod_close({"enabled": True, "at": "15:55"}, ("cup_and_handle",))
    assert explicit_ch == {"enabled": True, "at": "15:55"}


def test_batch_snapshot_resolution_iex_vs_sip(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    iex_snap = _make_snapshot("iex", dataset_id="snap-iex-1")
    sip_snap = _make_snapshot("sip", dataset_id="snap-sip-1")

    repo_mock = MagicMock(spec=HistoricalDatasetRepository)
    repo_mock.snapshots.return_value = [iex_snap, sip_snap]
    monkeypatch.setattr(
        "mike_trade.research.commands.HistoricalDatasetRepository",
        lambda root: repo_mock,
    )

    # Job requesting SIP
    job_sip = ExperimentSpec(
        "IBM", "vwap_bounce", "2026-07-09T00:00:00Z", "2026-10-09T00:00:00Z", feed="sip"
    )
    # Job requesting IEX
    job_iex = ExperimentSpec(
        "IBM", "vwap_bounce", "2026-07-09T00:00:00Z", "2026-10-09T00:00:00Z", feed="iex"
    )
    # Job with default feed (None -> iex)
    job_default = ExperimentSpec(
        "IBM", "vwap_bounce", "2026-07-09T00:00:00Z", "2026-10-09T00:00:00Z"
    )

    executed_jobs: list[dict[str, Any]] = []

    def fake_execute(job_data: dict[str, Any], root: str, results: str) -> dict[str, Any]:
        executed_jobs.append(job_data)
        return {
            "symbol": job_data["symbol"],
            "strategy": job_data["strategy"],
            "dataset_id": job_data["dataset_id"],
            "status": "completed",
            "run_id": "test-run",
        }

    import concurrent.futures

    monkeypatch.setattr(
        "mike_trade.research.commands.ProcessPoolExecutor",
        concurrent.futures.ThreadPoolExecutor,
    )
    monkeypatch.setattr("mike_trade.research.commands._execute_experiment", fake_execute)

    run_experiment_batch(
        [job_sip, job_iex, job_default],
        workers=1,
        root=str(tmp_path),
        results=str(tmp_path / "results"),
        batch_file=str(tmp_path / "batch.json"),
    )

    assert len(executed_jobs) == 3
    # SIP job resolved to sip snapshot
    assert executed_jobs[0]["dataset_id"] == "snap-sip-1"
    assert executed_jobs[0]["feed"] == "sip"

    # IEX job resolved to iex snapshot
    assert executed_jobs[1]["dataset_id"] == "snap-iex-1"
    assert executed_jobs[1]["feed"] == "iex"

    # Default job resolved to iex snapshot
    assert executed_jobs[2]["dataset_id"] == "snap-iex-1"
    assert executed_jobs[2]["feed"] is None


def test_run_one_feed_mismatch_rejection(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    sip_snap = _make_snapshot("sip", dataset_id="snap-sip-ibm-1")
    repo_mock = MagicMock(spec=HistoricalDatasetRepository)
    repo_mock.snapshots.return_value = [sip_snap]
    monkeypatch.setattr(
        "mike_trade.research.commands.HistoricalDatasetRepository",
        lambda root: repo_mock,
    )

    # Attempt to request feed="iex" while specifying a dataset_id that has feed="sip"
    with pytest.raises(ValueError, match="does not match requested feed 'iex'"):
        _run_one(
            "IBM",
            datetime(2026, 7, 9, tzinfo=UTC),
            datetime(2026, 10, 9, tzinfo=UTC),
            str(tmp_path),
            str(tmp_path / "results"),
            271828,
            "vwap_bounce",
            dataset_id="snap-sip-ibm-1",
            feed="iex",
        )


def test_cached_snapshot_bars_derives_feed_from_dataset_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    repo_mock = MagicMock(spec=HistoricalDatasetRepository)
    repo_mock.available.return_value = ()
    monkeypatch.setattr(
        "mike_trade.research.commands.HistoricalDatasetRepository",
        lambda root: repo_mock,
    )

    # Explicit feed
    _cached_snapshot_bars.cache_clear()
    _cached_snapshot_bars(
        str(tmp_path), "IBM", "2026-07-09T00:00:00+00:00", "2026-10-09T00:00:00+00:00",
        "custom-id", feed="sip"
    )
    repo_mock.available.assert_called_with(
        "IBM",
        datetime(2026, 7, 9, tzinfo=UTC),
        datetime(2026, 10, 9, tzinfo=UTC),
        feed="sip",
        adjustment="raw",
        dataset_id="custom-id",
    )

    # Derived from standard dataset_id prefix
    _cached_snapshot_bars.cache_clear()
    _cached_snapshot_bars(
        str(tmp_path), "IBM", "2026-07-09T00:00:00+00:00", "2026-10-09T00:00:00+00:00",
        "alpaca-sip-ibm-raw-1m-20261012-abc12345"
    )
    repo_mock.available.assert_called_with(
        "IBM",
        datetime(2026, 7, 9, tzinfo=UTC),
        datetime(2026, 10, 9, tzinfo=UTC),
        feed="sip",
        adjustment="raw",
        dataset_id="alpaca-sip-ibm-raw-1m-20261012-abc12345",
    )


def test_daily_history_forwards_matching_feed_to_repository() -> None:
    repo_mock = MagicMock(spec=HistoricalDatasetRepository)
    repo_mock.daily_candles.return_value = ()
    registry_mock = MagicMock()
    strategy_mock = MagicMock()
    strategy_mock.required_history.return_value = {CandleTimeframe.D1: 15}
    registry_mock.get.return_value = strategy_mock

    # Passing feed="sip" passes feed="sip" to repo.daily_candles
    try:
        _daily_history(
            repo_mock,
            registry_mock,
            "IBM",
            datetime(2026, 7, 9, tzinfo=UTC),
            datetime(2026, 10, 9, tzinfo=UTC),
            {"five_minute_orb_michelle_buy": {}},
            feed="sip",
        )
    except Exception:
        pass  # Ingest of empty candles is fine to fail, we check call args

    assert repo_mock.daily_candles.call_count >= 1
    args, kwargs = repo_mock.daily_candles.call_args
    assert kwargs.get("feed") == "sip"


def test_daily_history_wilder_atr_failure_when_missing() -> None:
    repo_mock = MagicMock(spec=HistoricalDatasetRepository)
    repo_mock.daily_candles.side_effect = ValueError(
        "no immutable daily (1d) snapshot covers IBM 2026-06-01..2026-10-09; "
        "run `research-data acquire --timeframe 1d`"
    )
    registry_mock = MagicMock()
    strategy_mock = MagicMock()
    # Michelle's Five-Minute ORB requires 15 D1 bars
    strategy_mock.required_history.return_value = {CandleTimeframe.D1: 15}
    registry_mock.get.return_value = strategy_mock

    with pytest.raises(ValueError, match="no immutable daily \\(1d\\) snapshot covers"):
        _daily_history(
            repo_mock,
            registry_mock,
            "IBM",
            datetime(2026, 7, 9, tzinfo=UTC),
            datetime(2026, 10, 9, tzinfo=UTC),
            {"five_minute_orb_michelle_buy": {}},
            feed="sip",
        )


def test_cli_argument_parsing() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register(subparsers)

    args = parser.parse_args([
        "research-backtest",
        "--symbol", "IBM",
        "--strategy", "vwap_bounce",
        "--start", "2026-07-09T00:00:00Z",
        "--end", "2026-10-09T00:00:00Z",
        "--feed", "sip",
        "--dataset-id", "snap-custom-1",
        "--eod-close-at", "15:50",
    ])
    assert args.symbol == "IBM"
    assert args.strategy == "vwap_bounce"
    assert args.feed == "sip"
    assert args.dataset_id == "snap-custom-1"
    assert args.eod_close_at == "15:50"
    assert args.disable_eod_close is False

    args_disabled = parser.parse_args([
        "research-backtest",
        "--symbol", "IBM",
        "--strategy", "vwap_bounce",
        "--start", "2026-07-09T00:00:00Z",
        "--end", "2026-10-09T00:00:00Z",
        "--disable-eod-close",
    ])
    assert args_disabled.disable_eod_close is True
    assert args_disabled.feed is None


def test_run_one_effective_eod_and_feed_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from mike_trade.market_data.alpaca_history import Adjustment, HistoricalBar

    iex_snap = _make_snapshot("iex", dataset_id="snap-iex-ibm-1")
    sip_snap = _make_snapshot("sip", dataset_id="snap-sip-ibm-1")

    repo_mock = MagicMock(spec=HistoricalDatasetRepository)
    repo_mock.snapshots.return_value = [iex_snap, sip_snap]
    monkeypatch.setattr(
        "mike_trade.research.commands.HistoricalDatasetRepository",
        lambda root: repo_mock,
    )

    sample_bar = HistoricalBar(
        symbol="IBM",
        timestamp=datetime(2026, 7, 9, 14, 0, tzinfo=UTC),
        open=Decimal("140.00"),
        high=Decimal("141.00"),
        low=Decimal("139.50"),
        close=Decimal("140.50"),
        volume=1000,
        vwap=Decimal("140.25"),
        trade_count=100,
        feed_id="sip",
        adjustment=Adjustment.RAW,
    )
    monkeypatch.setattr(
        "mike_trade.research.commands._cached_snapshot_bars",
        lambda *args, **kwargs: (sample_bar,),
    )

    captured_requests = []
    captured_runners = []

    async def fake_run(self: Any, request: Any) -> Any:
        captured_requests.append(request)
        captured_runners.append(self)
        result_mock = MagicMock()
        result_mock.run_id = "test-run-id"
        result_mock.results_hash = "test-hash"
        result_mock.evaluations = ()
        result_mock.signals = ()
        result_mock.fills = ()
        result_mock.equity_curve = ()
        return result_mock

    monkeypatch.setattr("mike_trade.backtest.runner.BacktestRunner.run", fake_run)
    metrics_mock = MagicMock()
    metrics_mock.trades = ()
    metrics_mock.win_rate = Decimal("0")
    metrics_mock.total_return = Decimal("0")
    metrics_mock.net_return = Decimal("0")
    metrics_mock.expectancy_net = Decimal("0")
    metrics_mock.profit_factor = None
    metrics_mock.max_drawdown = Decimal("0")
    monkeypatch.setattr(
        "mike_trade.backtest.runner.compute_metrics", lambda *a, **kw: metrics_mock
    )

    # Case 1: SIP intraday experiment -> defaults to 15:55 ET EOD liquidation, SIP feed provenance
    res_sip = _run_one(
        "IBM",
        datetime(2026, 7, 9, tzinfo=UTC),
        datetime(2026, 10, 9, tzinfo=UTC),
        str(tmp_path),
        str(tmp_path / "results"),
        271828,
        "vwap_bounce",
        feed="sip",
    )
    assert res_sip["feed"] == "sip"
    assert res_sip["dataset_id"] == "snap-sip-ibm-1"
    assert res_sip["end_of_day_close"] == {"enabled": True, "at": "15:55"}
    req_sip = captured_requests[-1]
    assert req_sip.config["dataset_id"] == "snap-sip-ibm-1"
    assert req_sip.config["end_of_day_close"] == {"enabled": True, "at": "15:55"}
    runner_sip = captured_runners[-1]
    assert runner_sip.feed.provenance.feed == "SIP"
    assert runner_sip.feed.provenance.is_consolidated is True

    # Case 2: IEX intraday experiment -> defaults to 15:55 ET EOD liquidation, IEX feed provenance
    res_iex = _run_one(
        "IBM",
        datetime(2026, 7, 9, tzinfo=UTC),
        datetime(2026, 10, 9, tzinfo=UTC),
        str(tmp_path),
        str(tmp_path / "results"),
        271828,
        "vwap_bounce",
        feed="iex",
    )
    assert res_iex["feed"] == "iex"
    assert res_iex["dataset_id"] == "snap-iex-ibm-1"
    assert res_iex["end_of_day_close"] == {"enabled": True, "at": "15:55"}
    req_iex = captured_requests[-1]
    assert req_iex.config["dataset_id"] == "snap-iex-ibm-1"
    assert req_iex.config["end_of_day_close"] == {"enabled": True, "at": "15:55"}
    runner_iex = captured_runners[-1]
    assert runner_iex.feed.provenance.feed == "IEX"
    assert runner_iex.feed.provenance.is_consolidated is False

    # Case 3: Explicit disable of EOD close
    res_no_eod = _run_one(
        "IBM",
        datetime(2026, 7, 9, tzinfo=UTC),
        datetime(2026, 10, 9, tzinfo=UTC),
        str(tmp_path),
        str(tmp_path / "results"),
        271828,
        "vwap_bounce",
        feed="sip",
        end_of_day_close={"enabled": False},
    )
    assert res_no_eod["end_of_day_close"] == {"enabled": False}
    req_no_eod = captured_requests[-1]
    assert req_no_eod.config["end_of_day_close"] == {"enabled": False}

    # Case 4: Multiday strategy leaves EOD close unforced (omitted)
    res_ch = _run_one(
        "IBM",
        datetime(2026, 7, 9, tzinfo=UTC),
        datetime(2026, 10, 9, tzinfo=UTC),
        str(tmp_path),
        str(tmp_path / "results"),
        271828,
        "cup_and_handle",
        buy_strategies=("cup_and_handle",),
        sell_strategies=(),
        feed="sip",
    )
    assert res_ch["end_of_day_close"] is None
    req_ch = captured_requests[-1]
    assert "end_of_day_close" not in req_ch.config

