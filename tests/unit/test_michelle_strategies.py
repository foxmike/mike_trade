"""Focused rule tests for Michelle's four independent strategies."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal as D

import pytest

from mike_trade.app import (
    AlpacaProductionMarketData,
    AppSettings,
    StrategyAssignment,
    SymbolStrategySettings,
    build_runtime,
)
from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import HistoricalExecutionSimulator
from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.backtest.runner import BacktestRequest, BacktestRunner
from mike_trade.domain.market import Candle as ReplayCandle
from mike_trade.domain.market import FeedProvenance, Timeframe
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import (
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    TimeInForce,
)
from mike_trade.market_data.alpaca_stream import StreamTrade
from mike_trade.market_data.candles import (
    Candle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, MarketContext, TradeSnapshot
from mike_trade.runtime import ExecutionMode
from mike_trade.scheduling.calendar import SessionStatus, is_trading_day, session_for
from mike_trade.strategies.base import ParameterValidationError, ReasonCode
from mike_trade.strategies.buy.five_minute_orb_michelle import FiveMinuteOrbMichelleBuy
from mike_trade.strategies.buy.vwap_bounce_break_michelle import (
    VwapBounceBreakMichelleBuy,
    _base,
    _engulfing,
    _scan_cycle,
)
from mike_trade.strategies.michelle_common import entry_attempt_allowed
from mike_trade.strategies.registry import StrategyRegistry
from mike_trade.strategies.sell.five_minute_orb_michelle import FiveMinuteOrbMichelleSell
from mike_trade.strategies.sell.vwap_bounce_break_michelle import VwapBounceBreakMichelleSell

DAY = date(2026, 10, 8)
OPEN = session_for(DAY).open_utc


def bar(
    minute: int,
    open_: str,
    high: str,
    low: str,
    close: str,
    *,
    status: CandleStatus = CandleStatus.CLOSED,
) -> Candle:
    start = OPEN + timedelta(minutes=minute)
    return Candle(
        "ABC",
        CandleTimeframe.M1,
        start,
        start + timedelta(minutes=1),
        D(open_),
        D(high),
        D(low),
        D(close),
        D("100"),
        "sip",
        True,
        CandleOrigin.REPLAY if status is CandleStatus.CLOSED else CandleOrigin.STREAM_TRADE,
        PriceBasis.RAW,
        status,
    )


def daily_history() -> tuple[Candle, ...]:
    days: list[date] = []
    candidate = DAY - timedelta(days=1)
    while len(days) < 15:
        if is_trading_day(candidate):
            days.append(candidate)
        candidate -= timedelta(days=1)
    result = []
    for index, day in enumerate(reversed(days)):
        session = session_for(day)
        close = D("99") + D(index) / D("10")
        result.append(
            Candle(
                "ABC",
                CandleTimeframe.D1,
                session.open_utc,
                session.close_utc,
                close - D("0.2"),
                close + D("0.7"),
                close - D("0.5"),
                close,
                D("100000"),
                "sip",
                True,
                CandleOrigin.REPLAY,
                PriceBasis.RAW,
                CandleStatus.CLOSED,
            )
        )
    return tuple(result)


def context(
    bars: list[Candle],
    *,
    daily: tuple[Candle, ...] = (),
    at: datetime | None = None,
    developing: Candle | None = None,
    trade_price: str | None = None,
    indicators: dict[str, D | None] | None = None,
) -> MarketContext:
    as_of = at or bars[-1].end
    trade = (
        None
        if trade_price is None
        else TradeSnapshot("ABC", D(trade_price), D("1"), as_of, as_of, "sip")
    )
    candles = {CandleTimeframe.M1: tuple(bars)}
    if daily:
        candles[CandleTimeframe.D1] = daily
    return MarketContext(
        symbol="ABC",
        as_of=as_of,
        version=max(1, len(bars)),
        feed_id="sip",
        consolidated=True,
        price_basis=PriceBasis.RAW,
        session_status=SessionStatus.REGULAR,
        session_id=session_for(DAY).session_id,
        freshness=ContextFreshness(as_of, timedelta(0), timedelta(minutes=2), False, None),
        candles=candles,
        indicators=indicators or {},
        latest_trade=trade,
        developing_candles=({CandleTimeframe.M1: developing} if developing is not None else None),
        completed_bars_only=developing is None,
    )


def orb_bars(*, high: str = "100.60", green: bool = True) -> list[Candle]:
    top = D(high)
    close = top - D("0.10") if top > D("101") else min(D("100.20"), top - D("0.02"))
    first_open = D("100.00") if green else D("100.25")
    return [
        bar(0, str(first_open), str(max(first_open, D("100.10"))), "100.00", "100.05"),
        bar(1, "100.05", str(min(top, D("100.15"))), "100.04", "100.10"),
        bar(2, "100.10", str(min(top, D("100.20"))), "100.08", "100.15"),
        bar(3, "100.15", str(min(top, D("100.25"))), "100.12", "100.18"),
        bar(4, "100.18", high, "100.16", str(close)),
        bar(5, str(close), str(top + D("0.08")), str(close - D("0.01")), str(top + D("0.05"))),
    ]


def vwap_cycle(*, intervening_red: bool = True) -> list[Candle]:
    closes = [D("100")] * 20 + [
        D("99.50"),
        D("99.55"),
        D("99.70"),
        D("99.90"),
        D("100.05"),
    ]
    if intervening_red:
        closes.extend((D("99.98"), D("100.10")))
    else:
        closes.append(D("100.10"))
    result: list[Candle] = []
    for index, close in enumerate(closes):
        open_ = close if index < 20 else close - D("0.05")
        low = close - D("0.05")
        high = close + D("0.05")
        if index == 20:  # deterministic below-VWAP hammer
            open_, low, high = D("99.60"), D("99.20"), D("99.65")
        if index == 24:  # first green crosses and closes over VWAP
            open_, low = D("99.92"), D("99.90")
        if intervening_red and index == 25:
            open_, high, low = D("100.06"), D("100.08"), D("99.96")
        result.append(bar(index, str(open_), str(high), str(low), str(close)))
    return result


def watching() -> dict[str, object]:
    return {"eligibility": "WATCHING_BUY"}


def test_orb_buy_historical_signal_and_live_tick_identity() -> None:
    bars = orb_bars()
    strategy = FiveMinuteOrbMichelleBuy()
    historical = strategy.evaluate(context(bars, daily=daily_history()), {}, watching())
    assert historical.is_signal
    assert historical.strategy_id == "five_minute_orb_michelle_buy"
    assert historical.reference_price == D("100.65")
    assert historical.state_updates["entry_orb_high"] == "100.60"

    closed = bars[:5]
    developing = bar(5, "100.40", "100.61", "100.40", "100.61", status=CandleStatus.DEVELOPING)
    live_context = context(
        closed,
        daily=daily_history(),
        at=OPEN + timedelta(minutes=5, seconds=10),
        developing=developing,
        trade_price="100.61",
    )
    first = strategy.evaluate(live_context, {}, watching())
    repeated_state = {"eligibility": "WATCHING_BUY", **first.state_updates}
    repeated = strategy.evaluate(live_context, {}, repeated_state)
    assert first.is_signal and not repeated.is_signal


@pytest.mark.parametrize(
    ("high", "expected"),
    [("100.30", True), ("101.80", True), ("100.29", False), ("101.81", False)],
)
def test_orb_range_boundaries(high: str, expected: bool) -> None:
    bars = orb_bars(high=high)
    result = FiveMinuteOrbMichelleBuy().evaluate(
        context(bars, daily=daily_history()), {}, watching()
    )
    assert result.is_signal is expected


def test_orb_rejects_red_range_breach_falling_vwap_and_cutoff() -> None:
    strategy = FiveMinuteOrbMichelleBuy()
    red = strategy.evaluate(context(orb_bars(green=False), daily=daily_history()), {}, watching())
    assert not red.is_signal
    breached = orb_bars()
    breached.insert(5, bar(5, "100.4", "100.5", "99.90", "100.45"))
    breached[-1] = bar(6, "100.45", "100.7", "100.4", "100.65")
    assert not strategy.evaluate(context(breached, daily=daily_history()), {}, watching()).is_signal
    late = [*orb_bars()[:5], bar(120, "100.4", "100.7", "100.3", "100.65")]
    assert (
        ReasonCode.NOT_ELIGIBLE
        in strategy.evaluate(context(late, daily=daily_history()), {}, watching()).reason_codes
    )


def test_orb_optional_spy_filter_and_parameter_validation() -> None:
    strategy = FiveMinuteOrbMichelleBuy()
    bars = orb_bars()
    missing = strategy.evaluate(
        context(bars, daily=daily_history()),
        {"spy_confirmation_enabled": True},
        watching(),
    )
    assert ReasonCode.INSUFFICIENT_DATA in missing.reason_codes
    rising = strategy.evaluate(
        context(
            bars,
            daily=daily_history(),
            indicators={"spy_close": D("500.1"), "spy_close_prev": D("500")},
        ),
        {"spy_confirmation_enabled": True},
        watching(),
    )
    assert rising.is_signal
    with pytest.raises(ParameterValidationError):
        strategy.evaluate(context(bars, daily=daily_history()), {"unknown": True}, watching())


def test_shared_attempt_limit_and_nonconsecutive_rule() -> None:
    at = OPEN + timedelta(minutes=10)
    assert entry_attempt_allowed((), at)
    assert not entry_attempt_allowed((at - timedelta(minutes=1),), at)
    assert not entry_attempt_allowed((at - timedelta(minutes=9), at - timedelta(minutes=2)), at)
    assert entry_attempt_allowed((at - timedelta(minutes=10),), at)


def test_all_four_strategies_register_independently() -> None:
    registry = StrategyRegistry()
    for cls in (
        FiveMinuteOrbMichelleBuy,
        FiveMinuteOrbMichelleSell,
        VwapBounceBreakMichelleBuy,
        VwapBounceBreakMichelleSell,
    ):
        registry.register(cls)
    expected = {
        "five_minute_orb_michelle_buy",
        "five_minute_orb_michelle_sell",
        "vwap_bounce_break_michelle_buy",
        "vwap_bounce_break_michelle_sell",
    }
    assert expected == {item.spec.strategy_id for item in registry.list_strategies()}


def test_runtime_registry_binds_independent_buy_sell_collections(tmp_path) -> None:
    class Clock:
        def now(self) -> datetime:
            return OPEN + timedelta(minutes=30)

    def assignment(strategy_id: str) -> StrategyAssignment:
        return StrategyAssignment(f"{strategy_id}_configured", strategy_id, {})

    runtime = build_runtime(
        AppSettings(
            mode=ExecutionMode.SHADOW,
            db_path=tmp_path / "michelle-registry.sqlite3",
            symbols=("ABC",),
            symbol_strategies=(
                SymbolStrategySettings(
                    "ABC",
                    (
                        assignment("five_minute_orb_michelle_buy"),
                        assignment("vwap_bounce_break_michelle_buy"),
                    ),
                    (
                        assignment("five_minute_orb_michelle_sell"),
                        assignment("vwap_bounce_break_michelle_sell"),
                    ),
                ),
            ),
        ),
        overrides={"clock": Clock()},
    )
    collections = runtime.lifecycle.collections("ABC")
    assert {binding.strategy_id for binding in collections.buys} == {
        "five_minute_orb_michelle_buy",
        "vwap_bounce_break_michelle_buy",
    }
    assert {binding.strategy_id for binding in collections.sells} == {
        "five_minute_orb_michelle_sell",
        "vwap_bounce_break_michelle_sell",
    }


def test_historical_vwap_entry_fills_next_bar_and_uses_protective_exit(tmp_path) -> None:
    result, strategy_bars = _run_vwap_replay(
        tmp_path,
        (
            bar(27, "100.20", "100.30", "100.00", "100.15"),
            bar(28, "100.10", "100.15", "99.50", "99.60"),
        ),
    )
    entry = next(item for item in result.fills if item["side"] == "BUY")
    exit_fill = next(item for item in result.fills if item["side"] == "SELL")
    assert entry["price"] == D("100.20")
    assert entry["time"] == strategy_bars[27].start
    assert exit_fill["exit_reason"] == "PROTECTIVE_STOP"
    assert result.cycles[0]["protection_mode"] == "FIXED_THEN_TRAILING"


def test_entry_bar_initial_stop_is_tested_before_trailing_advances(tmp_path) -> None:
    # The entry bar both spikes (which would activate the trail) and trades through the
    # initial stop; the stop is tested first and the trail must not have advanced.
    result, strategy_bars = _run_vwap_replay(
        tmp_path,
        (
            bar(27, "100.20", "103.00", "99.00", "100.00"),
            bar(28, "100.00", "100.10", "99.90", "100.00"),
        ),
    )
    exits = [item for item in result.fills if item["side"] == "SELL"]
    assert len(exits) == 1
    assert exits[0]["exit_reason"] == "PROTECTIVE_STOP"
    assert exits[0]["time"] <= strategy_bars[27].end
    cycle = result.cycles[0]
    assert cycle["final_stop_price"] == cycle["initial_stop_price"]


def _run_vwap_replay(tmp_path, tail):  # noqa: ANN001, ANN202
    strategy_bars = vwap_cycle(intervening_red=True)
    strategy_bars.extend(tail)
    provenance = FeedProvenance("replay", "IEX", "synthetic", False)
    events = [
        ReplayEvent.bar(
            ReplayCandle(
                item.symbol,
                Timeframe.M1,
                item.start,
                item.end,
                item.open,
                item.high,
                item.low,
                item.close,
                item.volume,
                provenance,
            )
        )
        for item in strategy_bars
    ]
    clock = ReplayClock(OPEN)
    feed = HistoricalReplayFeed(
        events,
        DataProvenance("synthetic", "IEX", False, "michelle-v1"),
        clock=clock,
        seed=17,
    )
    execution = HistoricalExecutionSimulator(feed, clock)
    registry = StrategyRegistry()
    registry.register(VwapBounceBreakMichelleBuy)
    registry.register(VwapBounceBreakMichelleSell)

    def intent_factory(evaluation, market_context, _request):  # noqa: ANN001
        buy = evaluation.side is StrategyRole.BUY
        return OrderIntent(
            intent_id=f"intent-{evaluation.signal_id}",
            client_order_id=f"client-{evaluation.signal_id}",
            account_scope_id="backtest:michelle",
            cycle_id=f"cycle-{evaluation.symbol}",
            symbol=evaluation.symbol,
            side=OrderSide.BUY if buy else OrderSide.SELL,
            purpose=OrderPurpose.ENTRY if buy else OrderPurpose.STRATEGY_EXIT,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            created_at=market_context.as_of,
            quantity=D("10") if buy else execution.held_quantity(evaluation.symbol),
        )

    request = BacktestRequest(
        symbols=("ABC",),
        window_start=OPEN,
        window_end=strategy_bars[-1].end,
        period_role=PeriodRole.RESEARCH,
        config={},
        seed=17,
        starting_capital=D("10000"),
        strategy_ids=(
            "vwap_bounce_break_michelle_buy",
            "vwap_bounce_break_michelle_sell",
        ),
        context_history_bars=100,
        result_directory=tmp_path,
    )
    result = asyncio.run(
        BacktestRunner(registry, feed, clock, execution, intent_factory=intent_factory).run(request)
    )
    return result, strategy_bars


def test_live_trade_event_builds_developing_context_and_invokes_lifecycle() -> None:
    at = OPEN + timedelta(minutes=5, seconds=7)

    class Clock:
        def now(self) -> datetime:
            return at

    class Scheduler:
        def on_market_data_arrival(self, symbol, version, due_at):  # noqa: ANN001
            assert (symbol, due_at) == ("ABC", at)
            assert version > 0
            return [object()]

    class Lifecycle:
        contexts: list[MarketContext] = []

        async def process(self, values):  # noqa: ANN001
            self.contexts.append(values["ABC"])
            return {"test": []}

    service = AlpacaProductionMarketData(
        Clock(), timedelta(minutes=1), feed="sip", warmup_bars=20, environ={}
    )
    lifecycle = Lifecycle()
    service._scheduler = Scheduler()
    service._lifecycle = lifecycle
    service._trading_symbols = ("ABC",)
    event = StreamTrade("ABC", at, at, "sip", D("100.61"), D("2"))
    service.cache.store_event(event, source="stream")
    asyncio.run(service._on_trade(event))
    (built,) = lifecycle.contexts
    assert built.completed_bars_only is False
    assert built.developing_candles[CandleTimeframe.M1].close == D("100.61")
    assert built.latest_trade is not None and built.latest_trade.price == D("100.61")


def test_vwap_patterns_and_historical_second_green_with_red_between() -> None:
    bars = vwap_cycle(intervening_red=True)
    assert _base(
        [
            bar(0, "99.9", "100.0", "99.86", "99.9"),
            bar(1, "99.91", "99.99", "99.87", "99.90"),
            bar(2, "99.90", "99.98", "99.86", "99.91"),
        ],
        [D("100.1"), D("100.1"), D("100.1")],
        2,
    )
    engulf = [
        bar(0, "100", "100.02", "99.88", "99.90"),
        bar(1, "99.88", "100.04", "99.86", "100.02"),
    ]
    assert _engulfing(engulf, [D("100.1"), D("100.1")], 1)
    cycle, replay = _scan_cycle(bars, None)
    assert cycle is not None and cycle["bottom"] == "hammer"
    assert cycle["first_time"] == bars[24].start
    assert replay is not None and replay[0] is bars[-1]
    result = VwapBounceBreakMichelleBuy().evaluate(context(bars), {}, watching())
    assert result.is_signal
    assert result.state_updates["signal_cycle_id"] == cycle["id"]


def test_vwap_cycle_invalidates_deep_dip_expiry_extension_and_consumption() -> None:
    bars = vwap_cycle()
    deep = list(bars)
    deep[20] = bar(20, "99.60", "99.65", "97.80", "99.50")
    assert _scan_cycle(deep, None)[1] is None
    extended = list(bars)
    extended[-1] = bar(len(extended) - 1, "100.05", "100.60", "100.04", "100.55")
    assert _scan_cycle(extended, None)[1] is None
    consumed_at = bars[-1].start
    assert _scan_cycle(bars, consumed_at)[1] is None

    expired = vwap_cycle()[:21]
    for minute in range(21, 53):
        expired.append(bar(minute, "100", "100.05", "99.95", "100"))
    assert _scan_cycle(expired, None)[1] is None


@pytest.mark.parametrize(
    ("strategy", "key"),
    [
        (FiveMinuteOrbMichelleSell(), "position_orb_high"),
        (VwapBounceBreakMichelleSell(), "position_vwap_at_entry"),
    ],
)
def test_michelle_sell_fixed_to_trailing_is_monotonic_and_full_exit_advisory(
    strategy: object, key: str
) -> None:
    base_state = {
        "position_cycle_id": "cycle-1",
        "position_fill_price": "100.10",
        key: "100.00",
    }
    quiet = context([bar(10, "100.1", "100.70", "99.80", "100.2")])
    first = strategy.evaluate(quiet, {}, base_state)  # type: ignore[attr-defined]
    assert not first.is_signal
    assert first.state_updates["active_stop_mode"] == "FIXED"
    assert first.state_updates["active_stop_price"] == "99.75"

    activation = context([bar(11, "100.3", "100.76", "100.30", "100.5")])
    second_state = {**base_state, **first.state_updates}
    second = strategy.evaluate(activation, {}, second_state)  # type: ignore[attr-defined]
    assert second.state_updates["active_stop_mode"] == "TRAILING"
    assert D(str(second.state_updates["active_stop_price"])) == D("100.25620")

    pullback = context([bar(12, "100.5", "100.60", "100.30", "100.4")])
    third_state = {**second_state, **second.state_updates}
    third = strategy.evaluate(pullback, {}, third_state)  # type: ignore[attr-defined]
    assert third.state_updates["active_stop_price"] == second.state_updates["active_stop_price"]

    trigger = context([bar(13, "100.3", "100.40", "100.20", "100.25")])
    final = strategy.evaluate(
        trigger,
        {},
        {**third_state, **third.state_updates},  # type: ignore[attr-defined]
    )
    assert final.is_signal
    assert final.strategy_id.endswith("_michelle_sell")
