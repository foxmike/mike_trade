"""Engine runner heartbeat, graceful drain and health lifecycle tests.

All collaborators are fakes; no credentials, network, broker or provider
clients are used.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

import pytest

from mike_trade.engine import Engine, ReconciliationOutcome
from mike_trade.observability.health import (
    EngineMode,
    EngineState,
    HealthSnapshot,
    Liveness,
    classify_liveness,
)

T0 = datetime(2026, 5, 1, 14, 30, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value = self.value + delta


class FakeFeed:
    def __init__(self, freshness: str = "FRESH") -> None:
        self.freshness = freshness

    def is_fresh(self) -> bool:
        return self.freshness == "FRESH"


class FakeSink:
    def __init__(self) -> None:
        self.published: list[HealthSnapshot] = []
        self.flushes = 0

    def publish(self, snapshot: HealthSnapshot) -> None:
        self.published.append(snapshot)

    def flush(self) -> None:
        self.flushes += 1


class FakePool:
    def __init__(self) -> None:
        self.shutdown_calls = 0
        self.depth = 0
        self.backpressure_count = 0
        self.overflow_count = 0

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class FakeRouter:
    def __init__(self) -> None:
        self.signals: list[object] = []
        self.events: list[object] = []
        self.drains = 0
        self.stops = 0
        self.pending = 0

    def submit_evaluation(self, result: object) -> int:
        self.signals.append(result)
        return len(self.signals)

    def submit_event(self, event: object) -> int:
        self.events.append(event)
        return len(self.events)

    async def drain(self) -> int:
        self.drains += 1
        # Simulate delivering already-queued CRITICAL work.
        delivered, self.pending = self.pending, 0
        return delivered

    def stop(self) -> None:
        self.stops += 1


class FakeBroker:
    """Recording broker port the engine must never touch."""

    def __init__(self) -> None:
        self.submits = 0
        self.cancels: list[str] = []
        self.closes: list[str] = []
        self.flattens = 0
        self.stops = ["stop-1", "stop-2"]

    async def submit_order(self, intent: object) -> object:
        self.submits += 1
        return object()

    async def cancel_order(self, order_id: str) -> None:
        self.cancels.append(order_id)

    async def close_position(self, symbol: str) -> None:
        self.closes.append(symbol)

    async def flatten(self) -> None:
        self.flattens += 1


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


class Outcome(Enum):
    SIGNAL = "SIGNAL"
    ERROR = "ERROR"
    NO_SIGNAL = "NO_SIGNAL"


@dataclass(frozen=True)
class FakeResult:
    side: Side = Side.BUY
    outcome: Outcome = Outcome.SIGNAL
    strategy_id: str = "s1"
    actionable: bool = True
    error_detail: str | None = None


@dataclass(frozen=True)
class FillEvent:
    symbol: str = "AAPL"


@dataclass(frozen=True)
class StopEvent:
    symbol: str = "AAPL"


@dataclass(frozen=True)
class ManualCloseEvent:
    symbol: str = "AAPL"


def make_engine(**overrides: object) -> tuple[Engine, FakeClock, FakeSink, FakeRouter, FakePool]:
    clock = FakeClock()
    sink = FakeSink()
    router = FakeRouter()
    pool = FakePool()
    defaults: dict[str, object] = {
        "clock": clock,
        "market_data": FakeFeed("FRESH"),
        "router": router,
        "pool": pool,
        "backlog": None,
        "scheduler": None,
        "reconciliation": ReconciliationOutcome(clean=True, status="CLEAN"),
        "health_sink": sink,
        "mode": EngineMode.PAPER,
        "account_scope_id": "paper:test",
        "heartbeat_interval": 60.0,
        "drain_timeout": 2.0,
    }
    defaults.update(overrides)
    engine = Engine(**defaults)  # type: ignore[arg-type]
    return engine, clock, sink, router, pool


def buy() -> FakeResult:
    return FakeResult(side=Side.BUY, outcome=Outcome.SIGNAL)


def sell() -> FakeResult:
    return FakeResult(side=Side.SELL, outcome=Outcome.SIGNAL)


# ------------------------------------------------------------ startup order


async def test_startup_reconciling_blocked_then_running_allowed() -> None:
    seen: list[HealthSnapshot] = []

    class Step:
        async def reconcile(self) -> ReconciliationOutcome:
            # Startup must already be RECONCILING with entries blocked here.
            seen.append(engine.last_snapshot)  # type: ignore[name-defined]
            assert engine.state is EngineState.RECONCILING  # type: ignore[name-defined]
            assert engine.entries_allowed is False
            return ReconciliationOutcome(clean=True, status="CLEAN")

    engine, _, sink, _, _ = make_engine(reconciliation=Step())
    snapshot = await engine.start()
    try:
        assert seen and seen[0] is not None
        first = sink.published[0]
        assert first.state is EngineState.RECONCILING
        assert first.entries_allowed is False
        assert "STARTUP_RECONCILIATION" in first.entry_block_reasons
        assert snapshot.state is EngineState.RUNNING
        assert snapshot.entries_allowed is True
        assert snapshot.entry_block_reasons == ()
    finally:
        await engine.stop()


async def test_reconciliation_drift_stays_blocked_with_reason() -> None:
    engine, _, sink, _, _ = make_engine(
        reconciliation=ReconciliationOutcome(
            clean=False, reasons=("ORDER_QTY_MISMATCH",), status="DRIFTED"
        )
    )
    try:
        snapshot = await engine.start()
        assert snapshot.state in (EngineState.RECONCILING, EngineState.DEGRADED)
        assert snapshot.entries_allowed is False
        assert "ORDER_QTY_MISMATCH" in snapshot.entry_block_reasons
        assert engine.entries_allowed is False
        assert "ORDER_QTY_MISMATCH" in engine.entry_block_reasons
    finally:
        await engine.stop()


async def test_reconciliation_failure_stays_blocked() -> None:
    class Boom:
        async def reconcile(self) -> object:
            raise RuntimeError("broker unreachable")

    engine, _, _, _, _ = make_engine(reconciliation=Boom())
    try:
        snapshot = await engine.start()
        assert snapshot.entries_allowed is False
        assert snapshot.state in (EngineState.RECONCILING, EngineState.DEGRADED)
        assert snapshot.last_error is not None and "broker unreachable" in snapshot.last_error
    finally:
        await engine.stop()


# --------------------------------------------------------------- heartbeats


async def test_heartbeats_interval_monotonic_utc_with_fake_clock() -> None:
    engine, clock, sink, _, _ = make_engine()
    try:
        await engine.start()
        base = len(sink.published)
        for _ in range(3):
            clock.advance(timedelta(seconds=5))
            await engine.publish_heartbeat()
        snaps = sink.published[base:]
        sequences = [s.sequence for s in snaps]
        assert sequences == sorted(sequences) and len(set(sequences)) == 3
        for earlier, later in zip(snaps[:-1], snaps[1:], strict=True):
            assert later.sequence > earlier.sequence
            assert later.heartbeat_at > earlier.heartbeat_at
        for snap in snaps:
            assert snap.heartbeat_at.tzinfo is not None
            assert snap.heartbeat_at.utcoffset() == timedelta(0)
    finally:
        await engine.stop()


async def test_heartbeat_background_task_publishes_at_interval() -> None:
    engine, _, sink, _, _ = make_engine(heartbeat_interval=0.01)
    try:
        await engine.start()
        count_at_start = len(sink.published)
        await asyncio.sleep(0.08)
        assert len(sink.published) >= count_at_start + 2
    finally:
        await engine.stop()


# ---------------------------------------------------------------- liveness


def _snapshot_at(when: datetime) -> HealthSnapshot:
    return HealthSnapshot(
        state=EngineState.RUNNING,
        mode=EngineMode.PAPER,
        account_scope_id="paper:test",
        heartbeat_at=when,
        sequence=1,
        entries_allowed=True,
    )


def test_classify_liveness_thresholds() -> None:
    threshold = timedelta(seconds=30)
    assert classify_liveness(_snapshot_at(T0), T0, threshold) is Liveness.ONLINE
    at_edge = classify_liveness(_snapshot_at(T0), T0 + timedelta(seconds=30), threshold)
    assert at_edge is Liveness.ONLINE
    just_over = classify_liveness(_snapshot_at(T0), T0 + timedelta(seconds=31), threshold)
    assert just_over is Liveness.STALE
    at_double = classify_liveness(_snapshot_at(T0), T0 + timedelta(seconds=60), threshold)
    assert at_double is Liveness.STALE
    past_double = classify_liveness(_snapshot_at(T0), T0 + timedelta(seconds=61), threshold)
    assert past_double is Liveness.OFFLINE
    far = classify_liveness(_snapshot_at(T0), T0 + timedelta(hours=1), threshold)
    assert far is Liveness.OFFLINE
    assert classify_liveness(None, T0, threshold) is Liveness.OFFLINE


def test_classify_liveness_is_pure() -> None:
    threshold = timedelta(seconds=10)
    first = classify_liveness(_snapshot_at(T0), T0 + timedelta(seconds=5), threshold)
    second = classify_liveness(_snapshot_at(T0), T0 + timedelta(seconds=5), threshold)
    assert first is second is Liveness.ONLINE
    # Same inputs always give the same output; wall-clock changes do not leak in.
    snap = _snapshot_at(T0)
    assert classify_liveness(snap, T0 + timedelta(seconds=50), threshold) is Liveness.OFFLINE
    assert snap.heartbeat_at == T0  # input untouched
    with pytest.raises(ValueError):
        classify_liveness(snap, T0, timedelta(0))


# --------------------------------------------------- gating: pause / degrade


async def test_paused_blocks_buy_but_not_fill_stop_manual_close() -> None:
    engine, _, _, router, _ = make_engine()
    try:
        await engine.start()
        engine.pause()
        assert engine.entries_allowed is False
        assert engine.handle_evaluation_result(buy()) is False
        assert router.signals == []
        assert engine.handle_evaluation_result(sell()) is True
        assert len(router.signals) == 1
        assert engine.handle_critical_event(FillEvent()) is True
        assert engine.handle_critical_event(StopEvent()) is True
        assert engine.handle_critical_event(ManualCloseEvent()) is True
        assert len(router.events) == 3
        engine.resume()
        assert engine.entries_allowed is True
        assert engine.handle_evaluation_result(buy()) is True
    finally:
        await engine.stop()


async def test_degraded_stale_feed_blocks_buy_but_not_exits_or_critical() -> None:
    feed = FakeFeed("STALE")
    engine, _, _, router, _ = make_engine(market_data=feed)
    try:
        await engine.start()
        assert engine.state is EngineState.DEGRADED
        assert engine.entries_allowed is False
        assert engine.handle_evaluation_result(buy()) is False
        assert router.signals == []
        # Exits, stops and fills continue while degraded.
        assert engine.handle_evaluation_result(sell()) is True
        assert engine.handle_critical_event(FillEvent()) is True
        assert engine.handle_critical_event(StopEvent()) is True
        assert engine.handle_critical_event(ManualCloseEvent()) is True
        assert len(router.signals) == 1
        assert len(router.events) == 3
    finally:
        await engine.stop()


async def test_unknown_feed_freshness_degrades() -> None:
    engine, _, _, router, _ = make_engine(market_data=object())
    try:
        await engine.start()
        assert engine.state is EngineState.DEGRADED
        assert engine.handle_evaluation_result(buy()) is False
        assert router.signals == []
    finally:
        await engine.stop()


# ------------------------------------------------------------------- drain


async def test_stop_drains_critical_flushes_and_shuts_pool() -> None:
    persistence = FakeSink()
    engine, _, sink, router, pool = make_engine(persistence_sink=persistence)
    await engine.start()
    router.pending = 4
    assert engine.accepting_evaluations is True
    final = await asyncio.wait_for(engine.stop(), timeout=5)
    assert engine.accepting_evaluations is False
    assert router.drains >= 1
    assert router.pending == 0
    assert persistence.flushes >= 1
    assert pool.shutdown_calls == 1
    assert router.stops == 1
    states = [s.state for s in sink.published]
    assert EngineState.DRAINING in states
    assert states[-1] is EngineState.STOPPED
    assert final.state is EngineState.STOPPED
    assert final.entries_allowed is False
    assert "STOPPED" in final.entry_block_reasons


async def test_shutdown_makes_zero_broker_calls_and_keeps_stops() -> None:
    broker = FakeBroker()
    before = list(broker.stops)
    engine, _, _, router, _ = make_engine()
    await engine.start()
    await engine.stop()
    assert broker.submits == 0
    assert broker.cancels == []
    assert broker.closes == []
    assert broker.flattens == 0
    assert broker.stops == before  # protective stops untouched
    assert router.stops == 1


async def test_engine_source_constructs_no_broker_clients() -> None:
    import pathlib

    source = pathlib.Path("mike_trade/engine.py").read_text()
    lowered = source.lower()
    for token in (
        ".submit_order(",
        ".cancel_order(",
        ".close_position(",
        ".flatten(",
        "alpaca",
        "from mike_trade.brokers",
        "from mike_trade.market_data.alpaca",
        "BrokerPort(",
        "Client(",
    ):
        assert token not in source and token.lower() not in lowered, token
    # The engine holds no broker reference: no broker attribute is ever assigned.
    assert "self._broker" not in source
    assert "self.broker" not in source


# ------------------------------------------------------- strategy failures


async def test_strategy_exception_surfaces_degraded_never_buy() -> None:
    engine, _, _, router, _ = make_engine()
    try:
        await engine.start()
        bad = FakeResult(side=Side.BUY, outcome=Outcome.ERROR, error_detail="zero division")
        assert engine.handle_evaluation_result(bad) is False
        assert router.signals == []
        assert engine.state is EngineState.DEGRADED
        assert engine.entries_allowed is False
        snap = await engine.publish_heartbeat()
        assert snap.last_error is not None and "zero division" in snap.last_error
        assert all(getattr(s, "side", None) is not Side.BUY for s in router.signals)

        class Exploding:
            @property
            def outcome(self) -> object:
                raise RuntimeError("worker blew up")

        assert engine.handle_evaluation_result(Exploding()) is False
        assert router.signals == []
    finally:
        await engine.stop()


async def test_report_strategy_error_never_produces_buy() -> None:
    engine, _, _, router, _ = make_engine()
    try:
        await engine.start()
        engine.report_strategy_error("indicator NaN")
        assert engine.state is EngineState.DEGRADED
        assert engine.handle_evaluation_result(buy()) is False
        assert router.signals == []
        snap = await engine.publish_heartbeat()
        assert snap.last_error == "indicator NaN"
    finally:
        await engine.stop()
