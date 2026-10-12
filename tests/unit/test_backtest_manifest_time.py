"""Regression coverage for time-of-day parameters in canonical backtest hashes."""

import json
from datetime import UTC, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import ExecutionAssumptions, HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed
from mike_trade.backtest.manifest import PeriodRole, canonical_json, config_hash
from mike_trade.backtest.runner import BacktestRequest, BacktestRunner
from mike_trade.research.commands import _research_registry


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (time(13), '"13:00:00.000000"'),
        (time(0, 0, 1, 23), '"00:00:01.000023"'),
        (time(15, 30, tzinfo=UTC), '"15:30:00.000000+00:00"'),
        (time(15, 30, tzinfo=timezone(timedelta(hours=-4))),
         '"15:30:00.000000-04:00"'),
    ],
)
def test_time_is_canonical_iso_with_microseconds(value, expected):
    assert canonical_json(value) == expected


def test_time_configuration_hash_is_stable_and_sensitive_to_parameter_changes():
    config = {"parameters": {"entry_start": time(13), "entry_end": time(15)}}
    reordered = {"parameters": {"entry_end": time(15), "entry_start": time(13)}}
    assert config_hash(config) == config_hash(reordered)
    assert config_hash(config) == config_hash(json.loads(canonical_json(config)))
    changed = {"parameters": {"entry_start": time(13, 0, 0, 1), "entry_end": time(15)}}
    assert config_hash(config) != config_hash(changed)


def test_existing_canonical_scalar_behavior_is_preserved():
    value = {
        "at": datetime(2026, 7, 9, 9, 30, tzinfo=timezone(timedelta(hours=-4))),
        "amount": Decimal("1.50"),
        "values": (True, None, 2),
    }
    assert canonical_json(value) == (
        '{"amount":"1.50","at":"2026-07-09T13:30:00.000000Z",'
        '"values":[true,null,2]}'
    )
    with pytest.raises(TypeError, match="floats are not allowed"):
        canonical_json(1.5)
    with pytest.raises(ValueError, match="timezone-aware"):
        canonical_json(datetime(2026, 7, 9))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "strategy_id",
    ["afternoon_breakout", "high_of_day_momentum", "midday_range_breakout",
     "power_hour_momentum"],
)
async def test_time_of_day_strategy_reaches_effective_configuration_hash(
    strategy_id, monkeypatch, tmp_path
):
    start = datetime(2026, 7, 9, 13, 30, tzinfo=UTC)
    clock = ReplayClock(start)
    feed = HistoricalReplayFeed(
        [], DataProvenance("synthetic", "IEX", False, "time-regression"),
        clock=clock, seed=271828,
    )
    execution = HistoricalExecutionSimulator(feed, clock, ExecutionAssumptions())
    runner = BacktestRunner(_research_registry(), feed, clock, execution)
    request = BacktestRequest(
        symbols=("IBM",), window_start=start, window_end=start + timedelta(minutes=1),
        period_role=PeriodRole.RESEARCH, config={}, seed=271828,
        starting_capital=Decimal("10000"), strategy_ids=(strategy_id,),
        strategy_params={strategy_id: {}}, result_directory=tmp_path,
    )

    class HashVerified(Exception):
        """Stop at the real hashing stage, before replay or artifact writes."""

    def verify_hash(effective):
        params = effective["strategy_collections"]["IBM"]["params"][strategy_id]
        assert any(isinstance(v, time) for v in params.values())
        digest = config_hash(effective)
        assert len(digest) == 64
        assert digest == config_hash(json.loads(canonical_json(effective)))
        raise HashVerified

    monkeypatch.setattr("mike_trade.backtest.runner.config_hash", verify_hash)
    with pytest.raises(HashVerified):
        await runner.run(request)
    assert not list(tmp_path.iterdir())
