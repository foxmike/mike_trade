"""Credentials-free production vwap_reference product-cycle coverage."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from mike_trade.app import (
    AppSettings,
    ShadowScenarioSettings,
    build_runtime,
)
from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.brokers.shadow.fills import FillAssumptions
from mike_trade.domain.errors import LiveTradingNotAuthorizedError
from mike_trade.domain.market import (
    Candle as ReplayCandle,
)
from mike_trade.domain.market import (
    FeedProvenance,
    Timeframe,
)
from mike_trade.domain.trading import OrderPurpose
from mike_trade.execution.coordinator import OrderCommand
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, MarketContext, QuoteSnapshot
from mike_trade.reporting.accounting import ReportPeriod, build_account_ledger_report
from mike_trade.runtime import ExecutionMode
from mike_trade.scheduling.calendar import SessionStatus

D = Decimal
START = datetime(2026, 10, 8, 14, 32, tzinfo=UTC)
DEFAULT_EXIT_BID = D("100")


class Clock:
    def __init__(self, value: datetime = START) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def context(
    symbol: str,
    at: datetime,
    closes: tuple[Decimal, Decimal],
    vwaps: tuple[Decimal, Decimal],
    *,
    bid: Decimal,
    ask: Decimal,
    version: int,
) -> MarketContext:
    bars: list[Candle] = []
    for index, close in enumerate(closes):
        start = at - timedelta(minutes=2 - index)
        bars.append(
            Candle(
                symbol,
                CandleTimeframe.M1,
                start,
                start + timedelta(minutes=1),
                close,
                close + D("0.20"),
                close - D("0.20"),
                close,
                D("100"),
                "iex",
                False,
                CandleOrigin.REPLAY,
                PriceBasis.RAW,
                CandleStatus.CLOSED,
            )
        )
    quote = QuoteSnapshot(symbol, bid, ask, D("100"), D("100"), at, at, "iex")
    return MarketContext(
        symbol=symbol,
        as_of=at,
        version=version,
        feed_id="iex",
        consolidated=False,
        price_basis=PriceBasis.RAW,
        session_status=SessionStatus.REGULAR,
        session_id=f"XNYS-{at.date().isoformat()}",
        freshness=ContextFreshness(
            at, timedelta(0), timedelta(seconds=90), False, None
        ),
        candles={CandleTimeframe.M1: tuple(bars)},
        indicators={"vwap_prev": vwaps[0], "vwap": vwaps[1]},
        latest_quote=quote,
    )


def upward(symbol: str, at: datetime, version: int = 1) -> MarketContext:
    return context(
        symbol,
        at,
        (D("99"), D("101")),
        (D("100"), D("100")),
        bid=D("101"),
        ask=D("101.1"),
        version=version,
    )


def downward(
    symbol: str, at: datetime, *, bid: Decimal = DEFAULT_EXIT_BID, version: int = 2
) -> MarketContext:
    return context(
        symbol,
        at,
        (D("101"), D("100")),
        (D("100.5"), D("100.5")),
        bid=bid,
        ask=bid + D("0.1"),
        version=version,
    )


def scenario(**changes: object) -> ShadowScenarioSettings:
    values: dict[str, object] = {
        "name": "vwap_reference_shadow",
        "starting_cash": D("25000.00"),
        "per_symbol_budget": D("3000.00"),
        "total_budget": D("10000.00"),
        "max_entry_cycles_per_session": 3,
        "consecutive_loss_limit": 3,
        "post_sale_cooldown": timedelta(minutes=5),
        "minimum_hold": timedelta(minutes=1),
    }
    values.update(changes)
    return ShadowScenarioSettings(**values)  # type: ignore[arg-type]


async def start_runtime(
    path: Path,
    clock: Clock,
    *,
    scenarios: tuple[ShadowScenarioSettings, ...] | None = None,
    symbols: tuple[str, ...] = ("SPY",),
):
    runtime = build_runtime(
        AppSettings(
            mode=ExecutionMode.SHADOW,
            db_path=path,
            symbols=symbols,
            shadow_scenarios=scenarios or (scenario(),),
        ),
        overrides={"clock": clock},
    )
    await runtime.start()
    return runtime


async def test_mode_composition_isolated_and_live_requires_authorization(
    tmp_path: Path,
) -> None:
    backtest = build_runtime(
        AppSettings(mode="BACKTEST", data_dir=tmp_path, symbols=("SPY",))
    )
    shadow = build_runtime(
        AppSettings(mode="SHADOW", data_dir=tmp_path, symbols=("SPY",))
    )
    paper = build_runtime(
        AppSettings(mode="PAPER", data_dir=tmp_path, symbols=("SPY",))
    )
    with pytest.raises(LiveTradingNotAuthorizedError):
        build_runtime(AppSettings(mode="LIVE", data_dir=tmp_path))
    live = build_runtime(
        AppSettings(
            mode="LIVE",
            enable_live_trading=True,
            data_dir=tmp_path,
            symbols=("SPY",),
        )
    )

    assert len(
        {
            backtest.persistence.path,  # type: ignore[attr-defined]
            shadow.persistence.path,  # type: ignore[attr-defined]
            paper.persistence.path,  # type: ignore[attr-defined]
            live.persistence.path,  # type: ignore[attr-defined]
        }
    ) == 4
    assert backtest.broker.simulator.__class__.__name__ == (  # type: ignore[attr-defined]
        "HistoricalExecutionSimulator"
    )
    assert "Alpaca" not in backtest.broker.__class__.__name__
    assert "Alpaca" not in shadow.broker.__class__.__name__
    assert paper.broker.__class__.__name__ == "AlpacaBrokerAdapter"
    assert live.broker.__class__.__name__ == "AlpacaBrokerAdapter"
    assert paper.broker._client is None and live.broker._client is None  # type: ignore[attr-defined]

    status = await backtest.start()
    assert status.entries_allowed is True
    await backtest.stop()


@pytest.mark.parametrize("exit_path", ["strategy", "stop"])
async def test_reference_pair_full_cycle_exact_once_and_protected(
    tmp_path: Path, exit_path: str
) -> None:
    clock = Clock()
    runtime = await start_runtime(tmp_path / f"{exit_path}.sqlite3", clock)
    try:
        evaluator = runtime.lifecycle
        assert evaluator is not None
        acquired = runtime.market_data.snapshot(("SPY",))  # type: ignore[attr-defined]
        assert set(acquired) == {"SPY"}
        assert runtime.market_data.acquisitions == {"SPY": 1}  # type: ignore[attr-defined]
        first = await evaluator.process({"SPY": upward("SPY", clock.value)})
        duplicate = await evaluator.process({"SPY": upward("SPY", clock.value)})
        shadow = runtime.broker.scenarios["vwap_reference_shadow"]  # type: ignore[attr-defined]

        entry_orders = [
            order
            for order in shadow.account.state.orders.values()
            if order.purpose is OrderPurpose.ENTRY
        ]
        protective = [
            order
            for order in shadow.account.state.orders.values()
            if order.purpose is OrderPurpose.PROTECTIVE_STOP
        ]
        assert len(entry_orders) == 1
        assert sum(action.startswith("ENTRY:") for action in first["vwap_reference_shadow"]) == 1
        assert not any(action.startswith("ENTRY:") for action in duplicate["vwap_reference_shadow"])
        assert len(protective) == 1
        assert protective[0].quantity == D("29")
        assert shadow.account.state.cash == D("22068.1000")
        assert evaluator.buy_parameters == {}
        assert evaluator.sell_parameters == {}
        assert evaluator.buy_parameters is not evaluator.sell_parameters

        clock.value += timedelta(minutes=2)
        if exit_path == "strategy":
            actions = await evaluator.process(
                {"SPY": downward("SPY", clock.value, bid=D("100"))}
            )
            assert any(action.startswith("EXIT:") for action in actions["vwap_reference_shadow"])
            assert shadow.account.state.realized_pnl == D("-31.9000")
            assert shadow.account.state.cash == D("24968.1000")
            assert shadow.stops.state(entry_orders[0].cycle_id).status == "CANCELED"
        else:
            actions = await evaluator.process(
                {"SPY": downward("SPY", clock.value, bid=D("99"))}
            )
            assert "STOP_EXIT" in actions["vwap_reference_shadow"]
            assert shadow.account.state.realized_pnl == D("-60.9000")
            assert shadow.account.state.cash == D("24939.1000")
        assert shadow.account.state.positions == {}
        report = build_account_ledger_report(
            shadow.account.scope,
            ReportPeriod.LIFETIME,
            clock.value,
            shadow_ledger=shadow.ledger,
        )
        assert report.ending_cash == shadow.account.state.cash
        assert report.net_realized_pnl == shadow.account.state.realized_pnl
        assert report.residual == D("0") and report.reconciled is True

        blocked = shadow.account.state.entry_block("SPY", "vwap_bounce", clock.value)
        assert blocked is not None and blocked[0] == "POST_SALE_COOLDOWN"
        clock.value += timedelta(minutes=6)
        assert shadow.account.state.entry_block("SPY", "vwap_bounce", clock.value) is None
        # The engine clears a BUY strategy's eligibility while a position is held (its indicator
        # state keeps advancing but it can never signal) and re-arms it on the next FLAT
        # evaluation; a neutral bar (no bounce) proves the re-arm without opening a position.
        assert "eligibility" not in shadow.states[("buy", "vwap_bounce", "SPY")]
        await evaluator.process({"SPY": downward("SPY", clock.value, bid=D("100.5"), version=9)})
        assert shadow.states[("buy", "vwap_bounce", "SPY")]["eligibility"] == "WATCHING_BUY"
    finally:
        await runtime.stop()


async def test_partial_fill_replay_never_submits_second_entry(tmp_path: Path) -> None:
    assumptions = FillAssumptions(participation=D("0.1"))
    clock = Clock()
    runtime = await start_runtime(
        tmp_path / "partial.sqlite3",
        clock,
        scenarios=(scenario(fill_assumptions=assumptions),),
    )
    try:
        evaluator = runtime.lifecycle
        shadow = runtime.broker.scenarios["vwap_reference_shadow"]  # type: ignore[attr-defined]
        same = upward("SPY", clock.value)
        await evaluator.process({"SPY": same})
        # Replaying the identical quote cannot manufacture another fill. New quote
        # evidence may advance the partial fill, while the strategy path still must
        # not submit another entry.
        await evaluator.process({"SPY": same})
        for version in range(2, 5):
            clock.value += timedelta(minutes=1)
            await evaluator.process(
                {"SPY": upward("SPY", clock.value, version=version)}
            )
        entries = [
            order
            for order in shadow.account.state.orders.values()
            if order.purpose is OrderPurpose.ENTRY
        ]
        assert len(entries) == 1
        assert entries[0].filled_quantity == entries[0].quantity
        assert shadow.account.state.positions["SPY"].quantity == D("29")
        assert shadow.stops.state(entries[0].cycle_id).covered_quantity == D("29")
    finally:
        await runtime.stop()


async def test_shared_snapshot_fanout_one_and_ten_symbols_and_scenario_isolation(
    tmp_path: Path,
) -> None:
    symbols = tuple(f"S{index}" for index in range(10))
    clock = Clock()
    scenarios = (
        scenario(name="conservative", per_symbol_budget=D("1000"), total_budget=D("10000")),
        scenario(name="aggressive", per_symbol_budget=D("1500"), total_budget=D("15000")),
    )
    runtime = await start_runtime(
        tmp_path / "fanout.sqlite3", clock, scenarios=scenarios, symbols=symbols
    )
    try:
        snapshot = {
            symbol: upward(symbol, clock.value, version=index + 1)
            for index, symbol in enumerate(symbols)
        }
        acquired = runtime.market_data.snapshot(symbols)  # type: ignore[attr-defined]
        assert set(acquired) == set(symbols)
        assert runtime.market_data.acquisitions == {  # type: ignore[attr-defined]
            symbol: 1 for symbol in symbols
        }
        result = await runtime.lifecycle.process(snapshot)
        assert set(result) == {"conservative", "aggressive"}
        for configured in scenarios:
            account = runtime.broker.scenarios[configured.name].account  # type: ignore[attr-defined]
            assert account.scope.scenario == configured.name
            assert len(account.state.positions) == 10
        conservative = runtime.broker.scenarios["conservative"].account  # type: ignore[attr-defined]
        aggressive = runtime.broker.scenarios["aggressive"].account  # type: ignore[attr-defined]
        assert conservative.state.cash != aggressive.state.cash
        assert conservative.scope.scope_id != aggressive.scope.scope_id
        # One immutable mapping supplied all ten symbols to both scenarios: acquisition
        # is per symbol, not per strategy or scenario.
        assert len(snapshot) == 10
        one = await runtime.lifecycle.process({symbols[0]: snapshot[symbols[0]]})
        assert set(one) == {"conservative", "aggressive"}
    finally:
        await runtime.stop()


async def test_limits_lockouts_budget_race_and_session_reset(tmp_path: Path) -> None:
    clock = Clock()
    configured = scenario(
        max_entry_cycles_per_session=1,
        consecutive_loss_limit=1,
        post_sale_cooldown=timedelta(0),
    )
    runtime = await start_runtime(
        tmp_path / "limits.sqlite3", clock, scenarios=(configured,),
        symbols=("SPY", "QQQ"),
    )
    try:
        lifecycle = runtime.lifecycle
        await lifecycle.process({"SPY": upward("SPY", clock.value)})
        clock.value += timedelta(minutes=2)
        await lifecycle.process({"SPY": downward("SPY", clock.value)})
        account = runtime.broker.scenarios[configured.name].account  # type: ignore[attr-defined]
        block = account.state.entry_block("SPY", "vwap_bounce", clock.value)
        assert block is not None and block[0] == "MAX_ENTRY_CYCLES_REACHED"
        next_session = datetime(2026, 10, 9, 14, 32, tzinfo=UTC)
        assert account.state.filled_entry_cycles("SPY", clock.value.date()) == 1
        assert account.state.filled_entry_cycles("SPY", next_session.date()) == 0
        # The daily counter reset is independent from the durable losing-streak lockout.
        block = account.state.entry_block("SPY", "vwap_bounce", next_session)
        assert block is not None and block[0] == "LOSING_STREAK_LOCKOUT"

        scenario_runtime = runtime.broker.scenarios[configured.name]  # type: ignore[attr-defined]
        now = clock.value

        def command(symbol: str) -> OrderCommand:
            from mike_trade.app import _CommandIntent

            intent = _CommandIntent(
                "SHADOW", account.scope.scope_id, symbol, "ENTRY_LONG",
                now + timedelta(minutes=1), now, D("1"), None, D("15000"),
            )
            return OrderCommand(intent, reservation_amount=D("15000"))

        decisions = await asyncio.gather(
            scenario_runtime.coordinator.authorize(command("SPY")),
            scenario_runtime.coordinator.authorize(command("QQQ")),
        )
        assert sum(item.authorized for item in decisions) == 1
    finally:
        await runtime.stop()

    strategy_scoped = scenario(
        name="strategy-scoped",
        max_entry_cycles_per_session=3,
        consecutive_loss_limit=1,
        lockout_scope_by_strategy=True,
        post_sale_cooldown=timedelta(0),
    )
    clock.value = START
    scoped_runtime = await start_runtime(
        tmp_path / "strategy-lockout.sqlite3",
        clock,
        scenarios=(strategy_scoped,),
    )
    try:
        await scoped_runtime.lifecycle.process({"SPY": upward("SPY", clock.value)})
        clock.value += timedelta(minutes=2)
        await scoped_runtime.lifecycle.process({"SPY": downward("SPY", clock.value)})
        scoped = scoped_runtime.broker.scenarios[strategy_scoped.name].account.state  # type: ignore[attr-defined]
        assert scoped.losing_streak("SPY", "vwap_bounce") == 1
        assert scoped.losing_streak("SPY", "other_buy") == 0
        assert scoped.entry_block("SPY", "vwap_bounce", clock.value)[0] == (  # type: ignore[index]
            "LOSING_STREAK_LOCKOUT"
        )
        assert scoped.entry_block("SPY", "other_buy", clock.value) is None
    finally:
        await scoped_runtime.stop()


async def test_early_close_cutoff_default_no_eod_and_fractional_replay(
    tmp_path: Path,
) -> None:
    # 2026-11-27 is the Friday after Thanksgiving: 13:00 ET close, 12:30 cutoff.
    clock = Clock(datetime(2026, 11, 27, 17, 31, tzinfo=UTC))
    fractional = scenario(
        allow_fractional=True,
        fill_assumptions=FillAssumptions(allow_fractional=True),
        post_sale_cooldown=timedelta(0),
    )
    runtime = await start_runtime(
        tmp_path / "fractional.sqlite3", clock, scenarios=(fractional,)
    )
    try:
        actions = await runtime.lifecycle.process({"SPY": upward("SPY", clock.value)})
        assert actions[fractional.name] == []
        assert runtime.mode is ExecutionMode.SHADOW
        # No close/flatten/EOD call is scheduled by default.
        assert all(
            "EOD" not in registration.strategy_id.upper()
            for registration in runtime.scheduler.registrations  # type: ignore[attr-defined]
        )
    finally:
        await runtime.stop()

    # Before an ordinary close, fractional fills reconcile exactly through replay.
    clock.value = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
    path = tmp_path / "fractional-replay.sqlite3"
    runtime = await start_runtime(path, clock, scenarios=(fractional,))
    await runtime.lifecycle.process({"SPY": upward("SPY", clock.value)})
    account = runtime.broker.scenarios[fractional.name].account  # type: ignore[attr-defined]
    quantity = account.state.positions["SPY"].quantity
    cash = account.state.cash
    assert quantity != quantity.to_integral_value()
    await runtime.stop()

    restarted = await start_runtime(path, clock, scenarios=(fractional,))
    try:
        restored = restarted.broker.scenarios[fractional.name].account  # type: ignore[attr-defined]
        assert restored.state.positions["SPY"].quantity == quantity
        assert restored.state.cash == cash
        assert restored.state.realized_pnl == D("0")
        report = build_account_ledger_report(
            restored.scope,
            ReportPeriod.LIFETIME,
            clock.value,
            shadow_ledger=restarted.broker.scenarios[fractional.name].ledger,  # type: ignore[attr-defined]
            marks={"SPY": D("101.1")},
        )
        assert report.ending_cash == cash
        assert report.residual == D("0") and report.reconciled is True
    finally:
        await restarted.stop()


def test_deterministic_replay_hides_future_bars_and_context_rejects_them() -> None:
    at = START
    replay_candle = ReplayCandle(
        "SPY",
        Timeframe.M1,
        at,
        at + timedelta(minutes=1),
        D("100"),
        D("101"),
        D("99"),
        D("100.5"),
        D("1000"),
        FeedProvenance("replay", "IEX", "offline-fixture", False),
    )
    event = ReplayEvent.bar(replay_candle)
    provenance = DataProvenance("offline-fixture", "IEX", False, "v1")
    visible_at = replay_candle.end + timedelta(seconds=30)
    observed: list[tuple[ReplayEvent, ...]] = []
    for _ in range(2):
        replay_clock = ReplayClock(at)
        feed = HistoricalReplayFeed(
            (event,),
            provenance,
            availability_latency=timedelta(seconds=30),
            clock=replay_clock,
            seed=17,
        )
        assert feed.as_of(replay_clock.now()) == ()
        with pytest.raises(Exception, match="ahead of the replay clock"):
            feed.as_of(visible_at)
        replay_clock.advance_to(visible_at)
        observed.append(feed.as_of(replay_clock.now()))
    assert observed[0] == observed[1]

    valid = upward("SPY", at)
    with pytest.raises(Exception, match="closes after as_of"):
        MarketContext(
            symbol="SPY",
            as_of=at - timedelta(minutes=1),
            version=1,
            feed_id="iex",
            consolidated=False,
            price_basis=PriceBasis.RAW,
            session_status=SessionStatus.REGULAR,
            session_id="XNYS-2026-10-08",
            freshness=valid.freshness,
            candles=valid.candles,
            indicators=valid.indicators,
            latest_quote=valid.latest_quote,
        )
