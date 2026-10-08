"""Offline reproducibility and research-metric checks for backtest artifacts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import ExecutionAssumptions, HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import (
    HoldoutLock, HoldoutViolationError, PeriodRole, RunManifest, config_hash,
    write_manifest,
)
from mike_trade.backtest.runner import BacktestRequest, BacktestRunner, compute_metrics
from mike_trade.domain.market import Candle, FeedProvenance, Timeframe
from mike_trade.strategies.presets.buy_presets import build_buy_registry


UTC0 = datetime(2025, 3, 10, 13, 30, tzinfo=UTC)
MIN = timedelta(minutes=1)
PROV = DataProvenance("synthetic", "IEX", False, "r1")
FEED_PROV = FeedProvenance("replay", "IEX", "synthetic", False)


def _event(minute: int, price: str = "100") -> ReplayEvent:
    start = UTC0 + minute * MIN
    px = D(price)
    return ReplayEvent.bar(Candle("AAA", Timeframe.M1, start, start+MIN,
        px, px, px, px, D("1000"), FEED_PROV))


def _runner(seed: int = 7):
    clock = ReplayClock(UTC0)
    feed = HistoricalReplayFeed([_event(0), _event(1)], PROV, clock=clock, seed=seed)
    execution = HistoricalExecutionSimulator(feed, clock, ExecutionAssumptions())
    return clock, feed, execution


def _request(tmp_path, seed=7, role=PeriodRole.RESEARCH, lock=None):
    return BacktestRequest(
        symbols=("AAA",), window_start=UTC0, window_end=UTC0+timedelta(minutes=5),
        period_role=role, config={"model": "baseline"}, seed=seed,
        starting_capital=D("1000"), strategy_ids=("momentum_breakout",),
        strategy_params={"momentum_breakout": {"lookback_bars": 2, "volume_multiple": D("1")}},
        holdout_lock=lock, result_directory=tmp_path,
    )


@pytest.mark.asyncio
async def test_identical_runs_and_seed_changes_are_content_addressed(tmp_path):
    c1, f1, e1 = _runner()
    a = await BacktestRunner(build_buy_registry(), f1, c1, e1).run(_request(tmp_path / "a"))
    c2, f2, e2 = _runner()
    b = await BacktestRunner(build_buy_registry(), f2, c2, e2).run(_request(tmp_path / "b"))
    assert a.manifest.canonical() == b.manifest.canonical()
    assert a.results_hash == b.results_hash
    c3, f3, e3 = _runner(8)
    other = await BacktestRunner(build_buy_registry(), f3, c3, e3).run(_request(tmp_path / "c", 8))
    assert other.manifest.config_hash != a.manifest.config_hash
    assert other.run_id != a.run_id


def test_manifest_is_immutable_and_write_once(tmp_path):
    manifest = RunManifest(
        "run-1", UTC0, "abc", ("AAA",), UTC0, UTC0+MIN, PeriodRole.RESEARCH,
        {"commit":"deadbeef","dirty":False}, {"source":"synthetic"}, "r1", "r1",
        "f"*64, "1000", {}, {}, {"bar_fill_policy":"NEXT_BAR_OPEN"}, {}, 3,
        ({"name":"momentum_breakout","version":"1.0.0"},),
    )
    restored = RunManifest.from_json(manifest.canonical())
    assert restored.content_hash == manifest.content_hash
    with pytest.raises(TypeError):
        manifest.code_revision["dirty"] = True
    write_manifest(manifest, tmp_path)
    with pytest.raises(FileExistsError):
        write_manifest(manifest, tmp_path)


@pytest.mark.asyncio
async def test_holdout_requires_lock_and_rejects_retuning(tmp_path):
    clock, feed, execution = _runner()
    runner = BacktestRunner(build_buy_registry(), feed, clock, execution)
    with pytest.raises(HoldoutViolationError):
        await runner.run(_request(tmp_path / "missing", role=PeriodRole.HOLDOUT))
    req = _request(tmp_path / "retuned", role=PeriodRole.HOLDOUT,
                   lock=HoldoutLock("operator-token", "0"*64, UTC0, UTC0+timedelta(days=1)))
    with pytest.raises(HoldoutViolationError, match="differs"):
        await runner.run(req)


@pytest.mark.asyncio
async def test_dev_overlap_and_real_broker_are_refused(tmp_path):
    clock, feed, execution = _runner()
    runner = BacktestRunner(build_buy_registry(), feed, clock, execution)
    lock = HoldoutLock("token", "1"*64, UTC0+timedelta(minutes=2), UTC0+timedelta(days=1))
    with pytest.raises(HoldoutViolationError, match="overlaps"):
        await runner.run(_request(tmp_path / "overlap", lock=lock))

    class AlpacaPaperClient:
        environment = "PAPER"

    bad = _request(tmp_path / "broker")
    object.__setattr__(bad, "broker", AlpacaPaperClient())
    with pytest.raises(ValueError, match="broker clients"):
        await runner.run(bad)


def test_hand_computed_metrics_and_excursions():
    t0 = UTC0
    fills = [
        {"symbol":"AAA","side":"BUY","quantity":D("10"),"price":D("100"),"fee":D("1"),"time":t0},
        {"symbol":"AAA","side":"SELL","quantity":D("10"),"price":D("110"),"fee":D("1"),"time":t0+2*MIN},
        {"symbol":"BBB","side":"BUY","quantity":D("10"),"price":D("110"),"fee":D("1"),"time":t0+3*MIN},
        {"symbol":"BBB","side":"SELL","quantity":D("10"),"price":D("100"),"fee":D("1"),"time":t0+5*MIN},
    ]
    bars = [
        {"symbol":"AAA","time":t0+MIN,"high":D("112"),"low":D("99")},
        {"symbol":"BBB","time":t0+4*MIN,"high":D("115"),"low":D("95")},
    ]
    m = compute_metrics(fills, starting_capital=D("1000"), seed=11, bars=bars)
    assert [t["pnl"] for t in m.trades] == [D("98"), D("-102")]
    assert m.max_drawdown == D("102")/D("1098")
    assert m.profit_factor == D("98")/D("102")
    assert m.win_rate == D("0.5")
    assert m.expectancy_net == D("-2")
    assert [(x["mfe"],x["mae"]) for x in m.mfe_mae] == [(D("120"),D("-10")),(D("50"),D("-150"))]
    assert "small sample" in m.warnings[0]

