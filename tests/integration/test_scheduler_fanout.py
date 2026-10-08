"""Shared-feed fan-out proof: 10 watched symbols share one broker-data feed.

Phase-I scheduler verification wiring the real phase-i components through
their public APIs only:

* shared market-data service + cache (``MarketDataService``/``MarketDataCache``)
* i.1 scheduler (``StrategyScheduler`` + declarative triggers)
* i.2 queueing (``EventBacklog``) and ``EvaluationPool``
* i.3 ``DecisionRouter``
* i.4 serialized decision consumer (the single-consumer contract the engine
  owns; there is no ``mike_trade/engine.py`` or ``observability/health.py``
  module in this repo, so the consumer is exercised directly)

A deterministic, in-process fake market-data provider implements the provider
side of the shared-service contract. It counts subscribe/stream-open calls
and replays scripted trades, quotes and 1-minute bars for 10 symbols over a
fixed simulated session window with a fake clock. No network, no credentials
and no Alpaca endpoints are used anywhere in this file.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.market_data.alpaca_stream import (
    Channel,
    StreamBar,
    StreamQuote,
    StreamTrade,
)
from mike_trade.market_data.cache import MarketDataCache
from mike_trade.market_data.service import MarketDataService
from mike_trade.scheduling.queueing import EventBacklog
from mike_trade.scheduling.scheduler import (
    DueEvaluation,
    MarketDataArrivalEvent,
    StrategyScheduler,
)
from mike_trade.scheduling.triggers import CANDLE_CLOSED, QUOTE_CHANGE, TriggerKind
from mike_trade.scheduling.workers import EvaluationOutcome, EvaluationPool
from mike_trade.signals.router import DecisionRouter

SYMBOLS = (
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "AMD",
    "JPM",
    "SPY",
)
CHANNELS = (Channel.TRADES, Channel.QUOTES, Channel.BARS)

T0 = datetime(2026, 3, 2, 13, 30, tzinfo=UTC)
SESSION_END = T0 + timedelta(minutes=6)
BAR_ENDS_1M = (T0 + timedelta(minutes=1), T0 + timedelta(minutes=2), T0 + timedelta(minutes=3))
BAR_END_5M = T0 + timedelta(minutes=5)

MAX_WORKERS = 4
N_DUES_EXPECTED_1M = len(SYMBOLS) * len(BAR_ENDS_1M)  # 30
N_DUES_EXPECTED_5M = len(SYMBOLS)  # 10


# ------------------------------------------------------------------ fakes


class FakeClock:
    """Fixed simulated clock shared by every component in one run."""

    def __init__(self, now: datetime = SESSION_END) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


class FakeMarketDataStream:
    """Deterministic in-process provider stream for the shared service.

    Exposes the ``subscribe``/``unsubscribe``/``events``/``close`` surface the
    shared ``MarketDataService`` requires, counts every stream-open
    (subscribe) call, and replays scripted events from an in-process queue.
    There are no sockets, no URLs and no credentials.
    """

    def __init__(self) -> None:
        self.subscribe_calls: list[frozenset[tuple[str, Channel]]] = []
        self.unsubscribe_calls: list[frozenset[tuple[str, Channel]]] = []
        self.close_calls = 0
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._observer: Any = None
        self._closed = False

    def set_state_observer(self, observer: Any) -> None:
        self._observer = observer

    async def subscribe(self, pairs: Any) -> None:
        self.subscribe_calls.append(frozenset(pairs))

    async def unsubscribe(self, pairs: Any) -> None:
        self.unsubscribe_calls.append(frozenset(pairs))

    async def events(self) -> Any:
        while not self._closed:
            yield await self._queue.get()

    async def emit(self, event: Any) -> None:
        await self._queue.put(event)

    async def close(self) -> None:
        self.close_calls += 1
        self._closed = True


class FakePoller:
    """Fallback poller stub; the scripted session never needs fallback data."""

    poll_interval_seconds = 0.05

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def poll_cycle(self, symbols: tuple[str, ...]) -> tuple[()]:
        self.calls.append(tuple(symbols))
        return ()


class FakeFreshness:
    """Router freshness view pinned to the fake clock (never stale)."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock

    def latest_snapshot_version(self, symbol: str) -> int | None:
        return None

    def newest_data_ts(self, symbol: str) -> datetime | None:
        return self._clock.now()


@dataclass(frozen=True)
class FakeSignal:
    """Minimal actionable signal payload (expiry + bar identity)."""

    expires_at: datetime | None = None
    bar_time: datetime | None = None


@dataclass(frozen=True)
class FillEvent:
    """Scripted critical fill event (name drives CRITICAL classification)."""

    symbol: str = "AAPL"
    event_time: datetime | None = None
    snapshot_version: int | None = None
    strategy_id: str = ""


@dataclass(frozen=True)
class StopEvent:
    """Scripted critical stop event (name drives CRITICAL classification)."""

    symbol: str = "AAPL"
    event_time: datetime | None = None
    snapshot_version: int | None = None
    strategy_id: str = ""


class SerializedConsumer:
    """The single serialized decision consumer; records re-entrancy."""

    def __init__(self) -> None:
        self.delivered: list[Any] = []
        self.active = 0
        self.peak = 0

    async def __call__(self, decision: Any) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)
        assert self.active == 1, "decision consumer ran re-entrantly"
        await asyncio.sleep(0)
        self.delivered.append(decision)
        self.active -= 1


# ------------------------------------------------- test-local strategies


class _StrategyBase:
    strategy_id = "base"

    def __init__(self, calls: list[tuple[str, str, str]]) -> None:
        self._calls = calls

    def _note(self, ctx: dict[str, Any]) -> None:
        self._calls.append((self.strategy_id, ctx["symbol"], str(ctx["identity"])))


class MomentumContinuation(_StrategyBase):
    """BUY, CANDLE_CLOSED(1m): signals only on up bars; reads only ctx."""

    strategy_id = "momentum_continuation"

    def evaluate(self, ctx: dict[str, Any]) -> FakeSignal | None:
        self._note(ctx)
        time.sleep(0.003)
        if ctx["close"] > ctx["open"]:
            return FakeSignal(expires_at=ctx["expires_at"], bar_time=ctx["bar_time"])
        return None


class PullbackBid(_StrategyBase):
    """BUY, QUOTE_CHANGE: signals on every second quote change; reads only ctx."""

    strategy_id = "pullback_bid"

    def evaluate(self, ctx: dict[str, Any]) -> FakeSignal | None:
        self._note(ctx)
        time.sleep(0.003)
        if ctx["tick_index"] % 2 == 0:
            return FakeSignal(expires_at=ctx["expires_at"], bar_time=ctx["bar_time"])
        return None


class BreakdownExit(_StrategyBase):
    """SELL, CANDLE_CLOSED(5m): signals only on down bars; reads only ctx."""

    strategy_id = "breakdown_exit"

    def evaluate(self, ctx: dict[str, Any]) -> FakeSignal | None:
        self._note(ctx)
        time.sleep(0.003)
        if ctx["close"] < ctx["open"]:
            return FakeSignal(expires_at=ctx["expires_at"], bar_time=ctx["bar_time"])
        return None


class FaultyProbe(_StrategyBase):
    """BUY, QUOTE_CHANGE: always raises; must surface as ERROR, never BUY."""

    strategy_id = "faulty_probe"

    def evaluate(self, ctx: dict[str, Any]) -> FakeSignal | None:
        self._note(ctx)
        raise RuntimeError("scripted strategy failure")


class StalledProbe(_StrategyBase):
    """SELL, QUOTE_CHANGE: always exceeds the timeout; must surface as TIMEOUT."""

    strategy_id = "stalled_probe"

    def evaluate(self, ctx: dict[str, Any]) -> FakeSignal | None:
        self._note(ctx)
        time.sleep(0.6)
        return None


class ConcurrencyProbe:
    """Neutral probe used only to measure the pool's thread high-water mark."""

    def evaluate(self, ctx: dict[str, Any]) -> None:
        time.sleep(0.03)
        return None


# ------------------------------------------------------- scripted session


def _base_price(symbol: str) -> Decimal:
    return Decimal(100 + 5 * SYMBOLS.index(symbol))


def _script_1m_bars() -> dict[tuple[str, datetime], tuple[Decimal, Decimal]]:
    """(symbol, bar_end) -> (open, close); bars 1 and 3 up, bar 2 down."""
    bars: dict[tuple[str, datetime], tuple[Decimal, Decimal]] = {}
    for symbol in SYMBOLS:
        base = _base_price(symbol)
        for index, end in enumerate(BAR_ENDS_1M, start=1):
            close = base + Decimal(1) if index % 2 == 1 else base - Decimal(1)
            bars[(symbol, end)] = (base, close)
    return bars


def _script_5m_bar() -> dict[str, tuple[Decimal, Decimal]]:
    """symbol -> (open, close); every 5m bar is down so SELL signals fire."""
    return {symbol: (_base_price(symbol) + Decimal(2), _base_price(symbol) - Decimal(2))
            for symbol in SYMBOLS}


def _quote(symbol: str, version: int) -> tuple[Decimal, Decimal, datetime]:
    base = _base_price(symbol)
    step = Decimal("0.05") * version
    return (base - Decimal("0.10") + step, base + Decimal("0.10") + step,
            T0 + timedelta(seconds=10 * version))


def _script_critical_events() -> list[Any]:
    return [
        FillEvent("AAPL", T0 + timedelta(seconds=30)),
        FillEvent("MSFT", T0 + timedelta(seconds=35)),
        FillEvent("NVDA", T0 + timedelta(seconds=40)),
        StopEvent("TSLA", T0 + timedelta(seconds=60)),
        StopEvent("AMD", T0 + timedelta(seconds=65)),
    ]


# ---------------------------------------------------------------- scenario


async def run_scenario() -> dict[str, Any]:
    """Execute the full fan-out scenario once; return every observable count."""
    clock = FakeClock()
    stream = FakeMarketDataStream()
    poller = FakePoller()
    cache = MarketDataCache()
    service = MarketDataService(stream, poller, cache, clock)
    calls: list[tuple[str, str, str]] = []

    strategies: dict[tuple[str, str, StrategyRole], _StrategyBase] = {}
    momentum = MomentumContinuation(calls)
    pullback = PullbackBid(calls)
    breakdown = BreakdownExit(calls)
    faulty = FaultyProbe(calls)
    stalled = StalledProbe(calls)

    # --- shared market-data service: one engine consumer + one audit observer
    all_pairs = frozenset((symbol, channel) for symbol in SYMBOLS for channel in CHANNELS)
    engine_feed = await service.register("engine", all_pairs)
    audit_feed = await service.register("audit", all_pairs)
    stream_open_count = len(stream.subscribe_calls)
    subscribed_symbols = {symbol for symbol, _ in stream.subscribe_calls[0]}
    subscribed_pairs = set(stream.subscribe_calls[0])

    # --- prove the live stream path, then replay the scripted session
    await service.start()
    try:
        await stream.emit(StreamTrade(
            symbol="AAPL", provider_source_ts=T0,
            receive_ts=T0 + timedelta(milliseconds=1), feed_id="sip",
            price=_base_price("AAPL"), size=Decimal(2)))
        first_engine = await asyncio.wait_for(engine_feed.get(), timeout=5)
        first_audit = await asyncio.wait_for(audit_feed.get(), timeout=5)
        assert first_engine is first_audit  # one stream fans out identically
        assert first_engine.symbol == "AAPL" and first_engine.kind == "trade"

        for symbol in SYMBOLS:
            base = _base_price(symbol)
            service.ingest(StreamTrade(
                symbol=symbol, provider_source_ts=T0,
                receive_ts=T0 + timedelta(milliseconds=2), feed_id="sip",
                price=base, size=Decimal(1)), source="stream")
            bid, ask, ts = _quote(symbol, 2)
            service.ingest(StreamQuote(
                symbol=symbol, provider_source_ts=ts,
                receive_ts=ts + timedelta(milliseconds=1), feed_id="sip",
                bid_price=bid, bid_size=Decimal(10),
                ask_price=ask, ask_size=Decimal(10)), source="stream")
            bars_1m = _script_1m_bars()
            for end in BAR_ENDS_1M:
                o, c = bars_1m[(symbol, end)]
                service.ingest(StreamBar(
                    symbol=symbol, provider_source_ts=end - timedelta(minutes=1),
                    receive_ts=end, feed_id="sip", open=o,
                    high=max(o, c) + Decimal("0.5"), low=min(o, c) - Decimal("0.5"),
                    close=c, volume=Decimal(1000)), source="stream")
        engine_depth = engine_feed.status.queue_size
        audit_depth = audit_feed.status.queue_size
        cache_ok = all(service.cache_snapshot(s).freshness is not None for s in SYMBOLS)
    finally:
        await service.close()

    # --- i.1 scheduler: 3 strategies per symbol across BUY/SELL + fault probes
    scheduler = StrategyScheduler(clock, watched_symbols=SYMBOLS)
    for symbol in SYMBOLS:
        scheduler.register(symbol=symbol, strategy_id=momentum.strategy_id,
                           side=StrategyRole.BUY, triggers=[CANDLE_CLOSED("1m")])
        strategies[(symbol, momentum.strategy_id, StrategyRole.BUY)] = momentum
        scheduler.register(symbol=symbol, strategy_id=pullback.strategy_id,
                           side=StrategyRole.BUY, triggers=[QUOTE_CHANGE])
        strategies[(symbol, pullback.strategy_id, StrategyRole.BUY)] = pullback
        scheduler.register(symbol=symbol, strategy_id=breakdown.strategy_id,
                           side=StrategyRole.SELL, triggers=[CANDLE_CLOSED("5m")])
        strategies[(symbol, breakdown.strategy_id, StrategyRole.SELL)] = breakdown
    scheduler.register(symbol="AAPL", strategy_id=faulty.strategy_id,
                       side=StrategyRole.BUY, triggers=[QUOTE_CHANGE])
    strategies[("AAPL", faulty.strategy_id, StrategyRole.BUY)] = faulty
    scheduler.register(symbol="AAPL", strategy_id=stalled.strategy_id,
                       side=StrategyRole.SELL, triggers=[QUOTE_CHANGE])
    strategies[("AAPL", stalled.strategy_id, StrategyRole.SELL)] = stalled
    registrations = len(scheduler.registrations)

    # --- drive the scripted session through the scheduler
    bars_1m = _script_1m_bars()
    bars_5m = _script_5m_bar()
    candle_1m_dues: list[DueEvaluation] = []
    for end in BAR_ENDS_1M:
        for symbol in SYMBOLS:
            candle_1m_dues.extend(scheduler.on_candle_closed(symbol, "1m", end))
    repeat_candle: list[DueEvaluation] = []
    for end in BAR_ENDS_1M:
        for symbol in SYMBOLS:
            repeat_candle.extend(scheduler.on_candle_closed(symbol, "1m", end))
    candle_5m_dues: list[DueEvaluation] = []
    for symbol in SYMBOLS:
        candle_5m_dues.extend(scheduler.on_candle_closed(symbol, "5m", BAR_END_5M))
    repeat_5m: list[DueEvaluation] = []
    for symbol in SYMBOLS:
        repeat_5m.extend(scheduler.on_candle_closed(symbol, "5m", BAR_END_5M))

    quote_dues: list[DueEvaluation] = []
    for version in (1, 2):
        for symbol in SYMBOLS:
            bid, ask, _ = _quote(symbol, version)
            quote_dues.extend(scheduler.on_quote_change(symbol, bid, ask, version))
    duplicate_quote: list[DueEvaluation] = []
    for symbol in SYMBOLS:
        bid, ask, _ = _quote(symbol, 2)
        duplicate_quote.extend(scheduler.on_quote_change(symbol, bid, ask, 2))
    control_dues: list[DueEvaluation] = []
    for _ in range(5):
        control_dues.extend(scheduler.on_control_tick())

    # --- i.2 EvaluationPool: concurrency probe, then sequential evaluation
    pool = EvaluationPool(max_workers=MAX_WORKERS, max_in_flight=MAX_WORKERS,
                          evaluation_timeout=0.15)
    try:
        probe = ConcurrencyProbe()
        probe_dues = [DueEvaluation("AAPL", f"probe_{i % 6}", StrategyRole.BUY,
                                    TriggerKind.QUOTE_CHANGE, None, None, T0)
                      for i in range(24)]
        probe_results = await asyncio.gather(
            *(pool.submit(due, probe, {}) for due in probe_dues))
        high_water = pool.in_flight_high_water

        expires_at = clock.now() + timedelta(hours=1)

        def context_for(due: DueEvaluation) -> dict[str, Any]:
            if due.bar_key is not None and str(due.bar_key.timeframe) == "CandleTimeframe.M1":
                o, c = bars_1m[(due.symbol, due.bar_key.end_utc)]
                return {"symbol": due.symbol, "open": o, "close": c,
                        "bar_time": due.bar_key.end_utc, "expires_at": expires_at,
                        "identity": due.bar_key.end_utc.isoformat()}
            if due.bar_key is not None:
                o, c = bars_5m[due.symbol]
                return {"symbol": due.symbol, "open": o, "close": c,
                        "bar_time": due.bar_key.end_utc, "expires_at": expires_at,
                        "identity": due.bar_key.end_utc.isoformat()}
            bid, ask, ts = _quote(due.symbol, due.snapshot_version or 1)
            return {"symbol": due.symbol, "bid": bid, "ask": ask,
                    "tick_index": due.snapshot_version or 1, "bar_time": ts,
                    "expires_at": expires_at,
                    "identity": f"quote-{due.snapshot_version}"}

        ordered_dues = sorted(
            candle_1m_dues + candle_5m_dues + quote_dues,
            key=lambda d: (d.symbol, d.strategy_id, d.side.value,
                           d.bar_key.end_utc.isoformat() if d.bar_key else "",
                           d.snapshot_version or 0))
        results: list[tuple[DueEvaluation, Any]] = []
        for due in ordered_dues:
            strategy = strategies[(due.symbol, due.strategy_id, due.side)]
            results.append((due, await pool.submit(due, strategy, context_for(due))))

        # Obsolete snapshots must never become actionable signals.
        pool.update_watermark("NVDA", "stale_probe", "BUY", 10)
        stale_due = DueEvaluation("NVDA", "stale_probe", StrategyRole.BUY,
                                  TriggerKind.QUOTE_CHANGE, 3, None, T0)
        stale_result = await pool.submit(stale_due, probe, {})
    finally:
        await pool.shutdown()

    by_outcome: dict[str, int] = {}
    for _, result in results:
        by_outcome[result.outcome.value] = by_outcome.get(result.outcome.value, 0) + 1

    # --- i.2 backlog: burst coalescing + critical-event delivery
    backlog = EventBacklog(critical_maxsize=64, coalescible_maxsize=64)
    criticals = _script_critical_events()
    # One burst: arrival versions 1..10 with the 5 critical events injected
    # mid-burst (coalescible puts) in arrival order.
    burst: list[Any] = [
        MarketDataArrivalEvent("NVDA", 1),
        MarketDataArrivalEvent("NVDA", 2),
        criticals[0],
        MarketDataArrivalEvent("NVDA", 3),
        MarketDataArrivalEvent("NVDA", 4),
        criticals[1],
        MarketDataArrivalEvent("NVDA", 5),
        MarketDataArrivalEvent("NVDA", 6),
        criticals[2],
        MarketDataArrivalEvent("NVDA", 7),
        MarketDataArrivalEvent("NVDA", 8),
        criticals[3],
        MarketDataArrivalEvent("NVDA", 9),
        MarketDataArrivalEvent("NVDA", 10),
        criticals[4],
    ]
    for item in burst:
        await backlog.put(item)
    backlog_stats_after_burst = backlog.stats
    drained: list[Any] = []
    while not backlog.empty():
        drained.append(await backlog.get())
        backlog.task_done()
    drained_critical = [item for item in drained
                        if isinstance(item, (FillEvent, StopEvent))]
    drained_coalescible = [item for item in drained
                           if isinstance(item, MarketDataArrivalEvent)]
    stale_put_accepted = await backlog.put(MarketDataArrivalEvent("NVDA", 3))
    backlog_stats_final = backlog.stats
    # The surviving latest snapshot produces no spurious scheduler output:
    # no strategy in this scenario triggers on bare market-data arrival.
    latest_version = max(item.snapshot_version for item in drained_coalescible)
    arrival_dues = scheduler.on_market_data_arrival("NVDA", latest_version)

    # --- i.3 router + i.4 serialized consumer
    consumer = SerializedConsumer()
    router = DecisionRouter(consumer=consumer, clock=clock,
                            freshness=FakeFreshness(clock),
                            max_data_age=timedelta(hours=1))
    for due, result in results:
        router.submit_evaluation(result, bar_key=due.bar_key)
    for event in criticals:
        router.submit_event(event)
    await asyncio.gather(router.drain(), router.drain(), router.drain())
    await router.drain()

    records = router.records
    decision_log = [(r.kind.value, r.priority.value, r.symbol, r.strategy_id,
                     r.status.value, r.reason) for r in records]
    delivered_ids = []
    for decision in consumer.delivered:
        payload = decision.payload
        if isinstance(payload, (FillEvent, StopEvent)):
            delivered_ids.append((payload.symbol, type(payload).__name__, "CRITICAL"))
        else:
            delivered_ids.append((payload.symbol, payload.strategy_id,
                                  payload.side.value))

    return {
        "stream_open_count": stream_open_count,
        "subscribed_symbols": subscribed_symbols,
        "subscribed_pair_count": len(subscribed_pairs),
        "engine_depth": engine_depth,
        "audit_depth": audit_depth,
        "cache_ok": cache_ok,
        "registrations": registrations,
        "n_1m_dues": len(candle_1m_dues),
        "n_5m_dues": len(candle_5m_dues),
        "n_quote_dues": len(quote_dues),
        "repeat_candle": len(repeat_candle),
        "repeat_5m": len(repeat_5m),
        "duplicate_quote": len(duplicate_quote),
        "control_dues": len(control_dues),
        "high_water": high_water,
        "probe_outcomes": sorted(r.outcome.value for r in probe_results),
        "by_outcome": by_outcome,
        "stale_outcome": stale_result.outcome.value,
        "coalesced": backlog_stats_after_burst.coalesced,
        "discarded_final": backlog_stats_final.discarded,
        "stale_put_accepted": stale_put_accepted,
        "n_drained_critical": len(drained_critical),
        "drained_critical_order": [(type(i).__name__, i.symbol) for i in drained_critical],
        "drained_versions": [i.snapshot_version for i in drained_coalescible],
        "arrival_dues": len(arrival_dues),
        "consumer_peak": consumer.peak,
        "n_delivered": len(consumer.delivered),
        "delivered_head": delivered_ids[:5],
        "decision_log": decision_log,
        "delivered_ids": delivered_ids,
        "strategy_calls": calls,
        "error_details": [r.error_detail for _, r in results
                          if r.outcome is EvaluationOutcome.ERROR],
        "records": [(r.symbol, r.strategy_id, r.status.value, r.reason) for r in records],
    }


async def test_ten_symbols_share_one_broker_data_feed() -> None:
    first = await run_scenario()
    second = await run_scenario()

    for run in (first, second):
        # One connection for all 10 symbols: a single stream-open whose
        # subscriptions cover every watched symbol on every channel.
        assert run["stream_open_count"] == 1
        assert run["subscribed_symbols"] == set(SYMBOLS)
        assert run["subscribed_pair_count"] == len(SYMBOLS) * len(CHANNELS)
        # Fan-out: the engine consumer and the audit observer each received
        # the 1 live-stream trade (consumed above as the stream-path proof)
        # plus all 50 replayed updates (1 trade + 1 quote + 3 bars per symbol).
        assert run["engine_depth"] == 50
        assert run["audit_depth"] == 50
        assert run["cache_ok"] is True
        # 3 strategies per symbol (BUY + SELL, mixed triggers) + 2 probes.
        assert run["registrations"] == len(SYMBOLS) * 3 + 2

        # Closed-bar strategies evaluate exactly once per closed bar per
        # (symbol, strategy, side), despite repeated identical control ticks.
        assert run["n_1m_dues"] == N_DUES_EXPECTED_1M
        assert run["n_5m_dues"] == N_DUES_EXPECTED_5M
        assert run["repeat_candle"] == 0
        assert run["repeat_5m"] == 0
        assert run["duplicate_quote"] == 0
        assert run["control_dues"] == 0
        # 9 symbols x 2 quote versions x 1 strategy + AAPL x 2 x 3 strategies.
        assert run["n_quote_dues"] == 9 * 2 + 2 * 3
        once_keys = [key for key in run["strategy_calls"]
                     if key[0] in ("momentum_continuation", "breakdown_exit")]
        assert len(once_keys) == len({*once_keys}) == N_DUES_EXPECTED_1M + N_DUES_EXPECTED_5M

        # Pool concurrency never exceeds its bound and is independent of the
        # 10 symbols x 32 strategy registrations.
        assert run["high_water"] <= MAX_WORKERS
        assert run["high_water"] < len(SYMBOLS) * 32
        assert set(run["probe_outcomes"]) == {"NO_SIGNAL"}

        # Burst coalescing: versions 1..10 collapse to the latest; the stale
        # re-put is refused; the obsolete snapshot never becomes a signal.
        assert run["drained_versions"] == [10]
        assert run["coalesced"] == 9
        assert run["stale_put_accepted"] is False
        assert run["discarded_final"] == 1
        assert run["arrival_dues"] == 0
        assert run["stale_outcome"] == "CANCELLED"

        # Every scripted fill/stop reaches the serialized consumer exactly
        # once, in FIFO order within the critical classes, with no overlap.
        assert run["n_drained_critical"] == 5
        assert run["drained_critical_order"] == [
            ("FillEvent", "AAPL"), ("FillEvent", "MSFT"), ("FillEvent", "NVDA"),
            ("StopEvent", "TSLA"), ("StopEvent", "AMD")]
        assert run["consumer_peak"] == 1
        assert run["delivered_head"] == [
            ("AAPL", "FillEvent", "CRITICAL"), ("MSFT", "FillEvent", "CRITICAL"),
            ("NVDA", "FillEvent", "CRITICAL"), ("TSLA", "StopEvent", "CRITICAL"),
            ("AMD", "StopEvent", "CRITICAL")]
        # 40 scripted signals + 5 critical events delivered; nothing else.
        assert run["n_delivered"] == 45
        assert run["by_outcome"] == {"SIGNAL": 40, "NO_SIGNAL": 20,
                                     "ERROR": 2, "TIMEOUT": 2}

        # Raising/slow strategies yield ERROR/TIMEOUT and never a BUY.
        assert len(run["error_details"]) == 2
        assert all("scripted strategy failure" in (detail or "") for detail in
                   run["error_details"])
        faulty_records = [r for r in run["records"] if r[1] == "faulty_probe"]
        stalled_records = [r for r in run["records"] if r[1] == "stalled_probe"]
        assert faulty_records and all(r[2] == "NON_ACTIONABLE" and r[3] == "ERROR"
                                      for r in faulty_records)
        assert stalled_records and all(r[2] == "NON_ACTIONABLE" and r[3] == "TIMEOUT"
                                       for r in stalled_records)
        assert not any(sid in ("faulty_probe", "stalled_probe")
                       for _, sid, _ in run["delivered_ids"])

    # Determinism: two runs produce an identical ordered decision log.
    assert first["decision_log"] == second["decision_log"]
    assert first["delivered_ids"] == second["delivered_ids"]
    assert len(first["decision_log"]) == 45 + 20 + 4  # forwarded + non-actionable
