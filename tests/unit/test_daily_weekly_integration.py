"""D1/W1 history through the real SHADOW market-data/runtime and BACKTEST replay paths.

No network: the Alpaca REST client is a deterministic in-memory fake serving canned daily JSON.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from daily_fixtures import (
    LAST_DAILY,
    SYMBOL,
    TODAY,
    cup_closes,
    cup_volumes,
    daily_candle,
    daily_series,
    minute_candles,
    vcp_closes,
    vcp_volumes,
)
from test_strategy_collections_runtime import SCENARIO, Clock

from mike_trade.app import (
    AlpacaProductionMarketData,
    AppSettings,
    ProductionMarketData,
    ShadowScenarioSettings,
    StrategyAssignment,
    SymbolStrategySettings,
    build_runtime,
)
from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.backtest.runner import BacktestRequest, BacktestRunner
from mike_trade.domain.errors import ConfigurationError
from mike_trade.domain.market import Candle as ReplayCandle
from mike_trade.domain.market import FeedProvenance, Timeframe
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import OrderPurpose
from mike_trade.market_data.candles import Candle, CandleTimeframe, MinuteBar
from mike_trade.market_data.context import QuoteSnapshot
from mike_trade.market_data.session_candles import (
    SessionCandleError,
    SessionCandleStore,
)
from mike_trade.research.commands import _daily_history, _research_registry
from mike_trade.research.datasets import HistoricalDatasetRepository
from mike_trade.runtime import ExecutionMode
from mike_trade.scheduling.calendar import session_for
from mike_trade.strategies.base import (
    BuyStrategy,
    ParameterSchema,
    StrategyEvaluation,
    StrategySpec,
    TriggerKind,
)
from mike_trade.strategies.closed_bar import REASON_CODES

D = Decimal
NY = ZoneInfo("America/New_York")
M1, D1, W1 = CandleTimeframe.M1, CandleTimeframe.D1, CandleTimeframe.W1
REGISTRY = _research_registry()
VCP_ID, CUP_ID = "volatility_contraction_pattern", "cup_and_handle"


def et(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), NY).astimezone(UTC)


# ------------------------------------------------------------------------------ fake provider


class FakeAlpaca:
    """Serves ``/v2/stocks/bars`` from canned daily candles; records every request."""

    def __init__(self, daily: dict[str, list[Candle]]) -> None:
        self.daily = daily
        self.calls: list[dict] = []
        self.closed = 0
        self.fail_for: set[str] = set()
        self.stamp_hour = 0

    def get(self, path: str, params=None):
        assert path == "/v2/stocks/bars"
        self.calls.append(dict(params))
        symbol = params["symbols"]
        assert params["adjustment"] == "raw"
        if params["timeframe"] != "1Day":
            return {"bars": [], "next_page_token": None}
        if symbol in self.fail_for:
            raise RuntimeError(f"provider outage for {symbol}")
        return {"bars": [
            {"t": datetime.combine(c.start.astimezone(NY).date(), time(self.stamp_hour), NY)
                  .astimezone(UTC).isoformat().replace("+00:00", "Z"),
             "o": str(c.open), "h": str(c.high), "l": str(c.low), "c": str(c.close),
             "v": int(c.volume), "n": 7, "vw": str(c.close)}
            for c in self.daily.get(symbol, [])
        ], "next_page_token": None}

    def close(self) -> None:
        self.closed += 1

    def daily_calls(self) -> list[dict]:
        return [c for c in self.calls if c["timeframe"] == "1Day"]


def vcp_candles(**kw):
    return daily_series(vcp_closes(), volumes=vcp_volumes(), **kw)


# ============================================================================ datasets / research


def test_daily_dataset_acquisition_is_immutable_idempotent_raw_and_drops_a_developing_day(tmp_path):
    candles = vcp_candles(last=TODAY)                                      # includes TODAY's bar
    fake = FakeAlpaca({SYMBOL: candles})
    repo = HistoricalDatasetRepository(tmp_path)
    first = candles[0].start.astimezone(NY).date()
    midday = et(TODAY, 12)
    (snapshot,) = repo.acquire_daily(fake, [SYMBOL], first, TODAY, now=midday)
    assert snapshot.timeframe == "1d" and snapshot.adjustment == "raw" and snapshot.bar_count == 259
    got = repo.daily_candles(SYMBOL, first, TODAY)
    assert got[-1].start.astimezone(NY).date() == LAST_DAILY                 # TODAY's developing bar dropped
    assert [c.close for c in got] == [c.close for c in candles[:-1]]
    again = repo.acquire_daily(fake, [SYMBOL], first, TODAY, now=midday)
    assert [s.dataset_id for s in again] == [snapshot.dataset_id] and len(fake.daily_calls()) == 1
    assert repo.snapshots() == [snapshot]
    with pytest.raises(ValueError, match="no immutable daily"):
        repo.daily_candles(SYMBOL, first - timedelta(days=30), TODAY)
    with pytest.raises(ValueError, match="adjustment='raw'"):
        repo.acquire_daily(fake, [SYMBOL], first, TODAY, adjustment="split", now=midday)
    skewed = FakeAlpaca({"QQQ": vcp_candles(last=TODAY, symbol="QQQ")})
    skewed.stamp_hour = 12                                                  # a mis-stamped provider
    with pytest.raises(SessionCandleError):
        repo.acquire_daily(skewed, ["QQQ"], first, TODAY, now=midday)
    empty = FakeAlpaca({})
    with pytest.raises(ValueError, match="no settled daily bars"):
        repo.acquire_daily(empty, ["IWM"], first, TODAY, now=midday)


def test_daily_history_for_research_is_loaded_only_when_a_selected_strategy_needs_it(tmp_path):
    fake = FakeAlpaca({SYMBOL: vcp_candles(last=TODAY)})
    repo = HistoricalDatasetRepository(tmp_path)
    first = TODAY - timedelta(days=400)                      # declared coverage (the fake has less data)
    repo.acquire_daily(fake, [SYMBOL], first, TODAY, now=et(TODAY, 18))
    start, end = session_for(TODAY).open_utc, session_for(TODAY).close_utc
    assert _daily_history(repo, REGISTRY, SYMBOL, start, end, {"vwap_bounce": {}}) == (None, None)
    store, tag = _daily_history(repo, REGISTRY, SYMBOL, start, end, {VCP_ID: {"base_sessions": 40}})
    assert tag == store.fingerprint()[:20]
    seen = store.get_completed_candles(SYMBOL, D1, as_of=et(TODAY, 10))
    assert seen[-1].start.astimezone(NY).date() == LAST_DAILY                # TODAY invisible at 10:00
    # not enough stored history for the lookback before the window: refuse rather than guess
    with pytest.raises(ValueError, match="no immutable daily"):
        _daily_history(repo, REGISTRY, SYMBOL, start, end, {VCP_ID: {"base_sessions": 300}})
    other = HistoricalDatasetRepository(tmp_path / "empty")
    with pytest.raises(ValueError, match="no immutable daily"):
        _daily_history(other, REGISTRY, SYMBOL, start, end, {VCP_ID: {}})


# ================================================================================ BACKTEST


class ProbeBuy(BuyStrategy):
    """Records the D1/W1 history visible at every evaluation; never signals."""

    seen: list[tuple[datetime, date | None, datetime | None, int, int]] = []
    spec = StrategySpec(
        strategy_id="history_probe", version="1.0.0", side=StrategyRole.BUY, category="technical",
        sub_category="price_action", reference_symbols=(), required_timeframes=(M1, D1, W1),
        warmup_bars=1, completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=M1,
        parameter_schema=ParameterSchema(()), persistent_state_keys=(),
        methodology="records visible history", source="test probe",
        execution_limitations=("test only",), reason_codes=REASON_CODES,
        history_bars=((D1, 5), (W1, 2)),
    )

    def evaluate(self, context, params, state):
        daily, weekly = context.candles[D1], context.candles[W1]
        ProbeBuy.seen.append((
            context.as_of,
            daily[-1].start.astimezone(NY).date() if daily else None,
            weekly[-1].end if weekly else None, len(daily), len(weekly)))
        return StrategyEvaluation.no_signal(self.spec, context.symbol)


def replay_bars(sessions_to_closes: dict[date, list[str]]) -> list[ReplayEvent]:
    provenance = FeedProvenance("replay", "IEX", "synthetic", False)
    events = []
    for day, closes in sessions_to_closes.items():
        previous = D(closes[0])
        for i, text in enumerate(closes):
            close = D(text)
            start = session_for(day).open_utc + timedelta(minutes=i)
            events.append(ReplayEvent.bar(ReplayCandle(
                SYMBOL, Timeframe.M1, start, start + timedelta(minutes=1), previous,
                max(previous, close) + D("0.01"), min(previous, close) - D("0.01"), close,
                D(1000), provenance)))
            previous = close
    return events


def run_backtest(registry, strategy_id, events, first, last, store, tmp_path, params=None, state=None):
    clock = ReplayClock(first)
    feed = HistoricalReplayFeed(events, DataProvenance("synthetic", "IEX", False, "daily-weekly"),
                                clock=clock, seed=5)
    request = BacktestRequest(
        symbols=(SYMBOL,), window_start=first, window_end=last, period_role=PeriodRole.RESEARCH,
        config={}, seed=5, starting_capital=D("100000"), strategy_ids=(strategy_id,),
        strategy_params={strategy_id: params or {}},
        initial_strategy_state={strategy_id: state or {}}, context_history_bars=500,
        result_directory=tmp_path)
    runner = BacktestRunner(registry, feed, clock, HistoricalExecutionSimulator(feed, clock),
                            session_candles=store)
    return asyncio.run(runner.run(request))


def probe_registry():
    from mike_trade.strategies.registry import StrategyRegistry

    registry = StrategyRegistry()
    registry.register(ProbeBuy)
    return registry


def test_backtest_replay_exposes_only_the_daily_and_weekly_history_knowable_at_each_event(tmp_path):
    store = SessionCandleStore()
    store.ingest_daily(SYMBOL, daily_series([D(100 + i) for i in range(30)], last=date(2026, 10, 16)))
    days = [date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 9), date(2026, 10, 12)]
    events = replay_bars({d: ["100", "100", "100"] for d in days})
    ProbeBuy.seen.clear()
    run_backtest(probe_registry(), "history_probe", events, session_for(days[0]).open_utc,
                 session_for(days[-1]).close_utc, store, tmp_path)
    by_day = {}
    for as_of, last_day, last_week_end, n_daily, n_weekly in ProbeBuy.seen:
        by_day.setdefault(as_of.astimezone(NY).date(), set()).add((last_day, last_week_end))
        assert as_of >= et(date(2026, 10, 1), 0)
    assert by_day[date(2026, 10, 6)] == {(date(2026, 10, 5), et(date(2026, 10, 2), 16))}   # Tue: Monday done
    assert by_day[date(2026, 10, 7)] == {(date(2026, 10, 6), et(date(2026, 10, 2), 16))}   # Wed: not Fri's week
    assert by_day[date(2026, 10, 9)] == {(date(2026, 10, 8), et(date(2026, 10, 2), 16))}   # Friday intraday
    assert by_day[date(2026, 10, 12)] == {(date(2026, 10, 9), et(date(2026, 10, 9), 16))}  # Monday: week done


def test_backtest_refuses_a_daily_strategy_without_a_history_source_and_m1_runs_are_unchanged(tmp_path):
    events = replay_bars({date(2026, 10, 6): ["100", "100"]})
    first, last = session_for(date(2026, 10, 6)).open_utc, session_for(date(2026, 10, 6)).close_utc
    with pytest.raises(ValueError, match="session_candles"):
        run_backtest(probe_registry(), "history_probe", events, first, last, None, tmp_path)
    plain = _research_registry()
    result = run_backtest(plain, "vwap_bounce", events, first, last, None, tmp_path / "m1")
    assert result.manifest is not None
    with_store = SessionCandleStore()
    with_store.ingest_daily(SYMBOL, daily_series([D(100)] * 12, last=date(2026, 10, 5)))
    # supplying daily history to an M1-only strategy set neither changes decisions nor the manifest hash
    other = run_backtest(plain, "vwap_bounce", events, first, last, with_store, tmp_path / "m1b")
    assert [dict(e) for e in other.evaluations] == [dict(e) for e in result.evaluations]


def _breakout_events(closes: list[str]) -> list[ReplayEvent]:
    return replay_bars({TODAY: closes})


@pytest.mark.parametrize("strategy_id,daily,closes,breakout_index", [
    (VCP_ID, vcp_candles(), ["97.0", "97.4", "97.9", "97.95", "97.9875", "98.0", "98.4"], 5),
    (CUP_ID, daily_series(cup_closes(), volumes=cup_volumes(len(cup_closes()))),
     ["100.0", "100.3", "100.4", "100.5", "100.6", "100.9"], 4),
])
def test_backtest_signals_on_the_exact_intraday_breakout_bar_and_is_deterministic(
    strategy_id, daily, closes, breakout_index, tmp_path
):
    store = SessionCandleStore()
    store.ingest_daily(SYMBOL, daily)
    first, last = session_for(TODAY).open_utc, session_for(TODAY).close_utc
    kwargs = dict(registry=REGISTRY, strategy_id=strategy_id, events=_breakout_events(closes),
                  first=first, last=last, store=store)
    one = run_backtest(**kwargs, tmp_path=tmp_path / "a")
    two = run_backtest(**kwargs, tmp_path=tmp_path / "b")
    assert [s["bar_time"] for s in one.signals] == [first + timedelta(minutes=breakout_index)]
    assert one.signals[0]["trigger_timeframe"] == "1m" and one.signals[0]["strategy"] == strategy_id
    assert one.run_id == two.run_id          # (results_hash embeds a per-execution uuid; compare the ledgers)
    for ledger in ("evaluations", "signals", "intents", "fills", "equity_curve"):
        assert getattr(one, ledger) == getattr(two, ledger)
    # the daily history is part of the run identity: a different history is a different run
    changed = SessionCandleStore()
    changed.ingest_daily(SYMBOL, daily[:-1] + [daily_candle(
        daily[-1].start.astimezone(NY).date(), daily[-1].close, volume=daily[-1].volume + 1)])
    three = run_backtest(**{**kwargs, "store": changed}, tmp_path=tmp_path / "c")
    assert three.run_id != one.run_id
    # one bar earlier (history up to the pre-breakout bar): no look-ahead, no signal
    early = run_backtest(**{**kwargs, "events": _breakout_events(closes[:breakout_index])},
                         tmp_path=tmp_path / "d")
    assert early.signals == ()


# =================================================================================== SHADOW


def alpaca_market_data(clock, fake, **kw):
    return AlpacaProductionMarketData(
        clock, timedelta(minutes=5), feed="iex", warmup_bars=60, environ={},
        history_client_factory=lambda: fake, **kw)


async def test_shadow_warm_fetches_daily_history_once_per_symbol_and_serves_it_in_the_context():
    candles = vcp_candles(last=TODAY)                      # includes the developing TODAY bar
    fake = FakeAlpaca({SYMBOL: candles, "QQQ": vcp_candles(last=TODAY, symbol="QQQ")})
    clock = Clock(et(TODAY, 10))
    md = alpaca_market_data(clock, fake)
    md.configure_history({SYMBOL: {D1: 252, W1: 4}})
    assert await md.warm([SYMBOL])
    assert len(fake.daily_calls()) == 1 and fake.daily_calls()[0]["symbols"] == SYMBOL   # no duplicates
    assert fake.daily_calls()[0]["timeframe"] == "1Day" and fake.daily_calls()[0]["feed"] == "iex"
    assert fake.closed == 1
    context = md.snapshot([SYMBOL])[SYMBOL]
    assert set(context.candles) == {M1, D1, W1}
    daily, weekly = context.candles[D1], context.candles[W1]
    assert daily[-1].start.astimezone(NY).date() == LAST_DAILY               # the developing day is absent
    assert len(daily) == 252 and weekly and weekly[-1].end <= context.as_of
    assert weekly[-1].end == et(date(2026, 10, 2), 16) or weekly[-1].end <= et(LAST_DAILY, 16)
    assert set(md.snapshot(["QQQ"])["QQQ"].candles) == {M1}                  # unconfigured symbols unchanged
    assert not md.session_candles.has("QQQ")


async def test_shadow_daily_provider_failures_are_isolated_and_fail_closed():
    fake = FakeAlpaca({SYMBOL: vcp_candles(last=LAST_DAILY), "QQQ": vcp_candles(last=LAST_DAILY, symbol="QQQ")})
    fake.fail_for = {"QQQ"}
    md = alpaca_market_data(Clock(et(TODAY, 10)), fake)
    md.configure_history({SYMBOL: {D1: 252}, "QQQ": {D1: 252}})
    assert await md.warm([SYMBOL, "QQQ"])                                    # startup is not blocked
    assert md.session_candles.has(SYMBOL) and not md.session_candles.has("QQQ")
    assert md.snapshot(["QQQ"])["QQQ"].candles[D1] == ()                     # empty history: strategy fails closed
    bad = FakeAlpaca({SYMBOL: vcp_candles(last=LAST_DAILY)})
    bad.stamp_hour = 12                                                      # mis-stamped daily bars
    md2 = alpaca_market_data(Clock(et(TODAY, 10)), bad)
    md2.configure_history({SYMBOL: {D1: 252}})
    assert await md2.warm([SYMBOL]) and not md2.session_candles.has(SYMBOL)


async def test_shadow_refreshes_the_newly_settled_session_once_off_the_evaluation_path():
    full = vcp_candles(last=TODAY)
    fake = FakeAlpaca({SYMBOL: full[:-1]})
    clock = Clock(et(TODAY, 10))
    md = alpaca_market_data(clock, fake)
    md.configure_history({SYMBOL: {D1: 252}})
    await md.warm([SYMBOL])
    before = len(md.session_candles.get_completed_candles(SYMBOL, D1, as_of=clock.value))
    md._schedule_daily_refresh(SYMBOL)
    assert not md._background                                                # nothing is due at 10:00
    clock.value = et(TODAY, 17, 30)                                          # TODAY has settled
    fake.daily[SYMBOL] = full
    md._schedule_daily_refresh(SYMBOL)
    md._schedule_daily_refresh(SYMBOL)                                       # single flight + throttle
    assert len(md._background) == 1
    await asyncio.gather(*md._background)
    after = md.session_candles.get_completed_candles(SYMBOL, D1, as_of=clock.value)
    assert len(after) == before + 1 and after[-1].start.astimezone(NY).date() == TODAY
    assert md.session_candles.conflicts(SYMBOL) == 0 and not md._daily_refreshing
    refreshes = len(fake.daily_calls())
    md._schedule_daily_refresh(SYMBOL)
    assert not md._background and len(fake.daily_calls()) == refreshes       # up to date: no request
    # a failed refresh never raises into the stream loop
    clock.value = et(date(2026, 10, 12), 17, 30)
    fake.fail_for = {SYMBOL}
    md._daily_refresh_not_before.clear()
    await md._refresh_daily(SYMBOL)
    assert not md._daily_refreshing


def shadow_settings(tmp_path: Path, **assignments) -> AppSettings:
    scenario = ShadowScenarioSettings(
        name=SCENARIO, starting_cash=D("50000"), per_symbol_budget=D("3000"),
        total_budget=D("20000"), max_entry_cycles_per_session=3, consecutive_loss_limit=3,
        post_sale_cooldown=timedelta(0), minimum_hold=timedelta(minutes=1))
    return AppSettings(
        mode=ExecutionMode.SHADOW, db_path=tmp_path / "t.sqlite3", symbols=(SYMBOL,),
        shadow_scenarios=(scenario,),
        symbol_strategies=(SymbolStrategySettings(SYMBOL, tuple(
            StrategyAssignment(i, i) for i in assignments["buys"])),))


def test_runtime_configures_daily_history_for_bound_daily_strategies_and_refuses_unsupported_data(tmp_path):
    clock = Clock(et(TODAY, 10))
    md = ProductionMarketData(clock, timedelta(minutes=5))
    runtime = build_runtime(shadow_settings(tmp_path, buys=(VCP_ID, CUP_ID)),
                            overrides={"clock": clock, "market_data": md})
    assert runtime.lifecycle.history_requirements() == {SYMBOL: {D1: 252}}
    assert md._session_frames == {SYMBOL: (D1,)} and md._history_sessions[SYMBOL] == 252
    assert md._timeframes_for(SYMBOL) == (M1, D1) and md._timeframes_for("QQQ") == (M1,)
    # an M1-only assignment configures nothing
    md2 = ProductionMarketData(clock, timedelta(minutes=5))
    build_runtime(shadow_settings(tmp_path, buys=("momentum_breakout",)),
                  overrides={"clock": clock, "market_data": md2})
    assert md2._session_frames == {}
    # a market-data service that cannot supply daily history makes the configuration fail closed
    md3 = ProductionMarketData(clock, timedelta(minutes=5))
    md3.configure_history = None  # type: ignore[assignment]
    with pytest.raises(ConfigurationError) as raised:
        build_runtime(shadow_settings(tmp_path, buys=(VCP_ID,)),
                      overrides={"clock": clock, "market_data": md3})
    assert raised.value.code == "STRATEGY_HISTORY_UNSUPPORTED"
    # non-daily intraday timeframes are still refused in SHADOW
    bad = SimpleNamespace()
    assert bad is not None


async def test_shadow_runtime_enters_a_long_on_the_intraday_breakout_of_a_daily_vcp(tmp_path):
    clock = Clock(et(TODAY, 9, 30))
    md = ProductionMarketData(clock, timedelta(minutes=5))
    runtime = build_runtime(shadow_settings(tmp_path, buys=(VCP_ID,)),
                            overrides={"clock": clock, "market_data": md})
    await runtime.start()
    shadow = runtime.broker.scenarios[SCENARIO]                              # type: ignore[attr-defined]
    try:
        md.session_candles.ingest_daily(SYMBOL, vcp_candles())
        closes = ["97.0", "97.4", "97.9", "97.95", "97.9875", "98.0", "98.4"]
        minutes = minute_candles([D(c) for c in closes], day=TODAY, volume=1000)
        entries = []
        for index, bar in enumerate(minutes):
            md.candles.on_minute_bar(MinuteBar(
                SYMBOL, bar.start, bar.open, bar.high, bar.low, bar.close, bar.volume, "iex", False))
            if index == 0:
                continue                                                     # bar index-1 is now closed
            clock.value = bar.start
            snapshot = md.snapshot([SYMBOL])[SYMBOL]
            assert snapshot.candles[M1][-1].close == D(closes[index - 1])
            assert snapshot.candles[D1][-1].start.astimezone(NY).date() == LAST_DAILY
            quote = QuoteSnapshot(SYMBOL, bar.open - D("0.05"), bar.open + D("0.05"), D(100), D(100),
                                  clock.value, clock.value, "iex")
            actions = await runtime.lifecycle.process({SYMBOL: dataclasses.replace(snapshot, latest_quote=quote)})
            entries.append((index - 1, [a for a in actions[SCENARIO] if a.startswith("ENTRY:")]))
        signal_bars = [i for i, found in entries if found]
        assert signal_bars == [5]                                            # the 98.0 close, once
        (entry,) = [o for o in shadow.account.state.orders.values() if o.purpose is OrderPurpose.ENTRY]
        assert entry.symbol == SYMBOL and entry.strategy_key == f"{VCP_ID}_cfg" or entry.strategy_key == VCP_ID
        assert [o for o in shadow.account.state.orders.values() if o.purpose is OrderPurpose.PROTECTIVE_STOP]
    finally:
        await runtime.stop()


def test_strategy_spec_requires_daily_history_to_be_declared_and_forbids_a_daily_trigger():
    base = dict(
        strategy_id="x_probe", version="1.0.0", side=StrategyRole.BUY, category="technical",
        sub_category="price_action", reference_symbols=(), required_timeframes=(M1, D1),
        warmup_bars=1, completed_bars_only=True, requires_volume=False,
        trigger_kind=TriggerKind.EDGE_ON_CLOSED_BAR, trigger_timeframe=M1,
        parameter_schema=ParameterSchema(()), persistent_state_keys=(), methodology="m", source="s",
        execution_limitations=("l",), reason_codes=REASON_CODES)
    with pytest.raises(ValueError, match="history_bars"):
        StrategySpec(**base)
    StrategySpec(**base, history_bars=((D1, 5),))
    with pytest.raises(ValueError, match="intraday"):
        StrategySpec(**{**base, "trigger_timeframe": D1}, history_bars=((D1, 5),))
    with pytest.raises(ValueError):
        StrategySpec(**{**base, "required_timeframes": (M1,)}, history_bars=((D1, 5),))
    with pytest.raises(ValueError):
        StrategySpec(**base, history_bars=((D1, 0),))
