"""Deterministic priority, tie-breaking and revalidation tests for the decision router."""

from __future__ import annotations

import asyncio
import itertools
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.scheduling.queueing import EventBacklog
from mike_trade.scheduling.scheduler import BarKey, DueEvaluation
from mike_trade.scheduling.triggers import TriggerKind
from mike_trade.scheduling.workers import EvaluationOutcome, EvaluationResult
from mike_trade.signals.router import (
    DecisionRouter,
    DecisionRouterClosedError,
    RejectReason,
    RoutePriority,
    RouteStatus,
)

T0 = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


class FakeFreshness:
    def __init__(self) -> None:
        self.versions: dict[str, int] = {}
        self.data_ts: dict[str, datetime] = {}

    def latest_snapshot_version(self, symbol: str) -> int | None:
        return self.versions.get(symbol)

    def newest_data_ts(self, symbol: str) -> datetime | None:
        return self.data_ts.get(symbol)


@dataclass(frozen=True)
class FillEvent:
    symbol: str = "AAPL"
    event_time: datetime | None = None
    snapshot_version: int | None = None
    strategy_id: str = ""
    expires_at: datetime | None = None  # must be ignored for critical events


@dataclass(frozen=True)
class BrokerOrderUpdate(FillEvent):
    pass


@dataclass(frozen=True)
class ReconciliationEvent(FillEvent):
    pass


@dataclass(frozen=True)
class StopEvent(FillEvent):
    pass


@dataclass(frozen=True)
class SafetyExitEvent(FillEvent):
    pass


@dataclass(frozen=True)
class ManualCloseEvent(FillEvent):
    pass


@dataclass(frozen=True)
class FakeSignal:
    expires_at: datetime | None = None
    bar_time: datetime | None = None


class Harness:
    def __init__(self, *, max_age: timedelta = timedelta(seconds=30)) -> None:
        self.clock = FakeClock()
        self.freshness = FakeFreshness()
        self.delivered: list[object] = []
        self.router = DecisionRouter(
            consumer=self._consume,
            clock=self.clock,
            freshness=self.freshness,
            max_data_age=max_age,
        )
        for symbol in ("AAPL", "MSFT", "NVDA"):
            self.fresh(symbol)

    def _consume(self, decision: object) -> None:
        self.delivered.append(decision)

    def fresh(self, symbol: str, version: int | None = None) -> None:
        self.freshness.data_ts[symbol] = self.clock.value - timedelta(seconds=1)
        if version is not None:
            self.freshness.versions[symbol] = version

    @property
    def payloads(self) -> list[object]:
        return [d.payload for d in self.delivered]  # type: ignore[attr-defined]


def result(
    *,
    strategy_id: str = "s1",
    side: StrategyRole = StrategyRole.BUY,
    symbol: str = "AAPL",
    version: int | None = 1,
    outcome: EvaluationOutcome = EvaluationOutcome.SIGNAL,
    signal: object | None = None,
    finished_at: datetime = T0,
) -> EvaluationResult:
    if outcome is EvaluationOutcome.SIGNAL and signal is None:
        signal = FakeSignal(bar_time=T0)
    return EvaluationResult(
        strategy_id=strategy_id,
        side=side,
        symbol=symbol,
        snapshot_version=version,
        started_at=T0 - timedelta(seconds=1),
        finished_at=finished_at,
        outcome=outcome,
        signal=signal if outcome is EvaluationOutcome.SIGNAL else None,
        error_detail="boom" if outcome is EvaluationOutcome.ERROR else None,
    )


# --------------------------------------------------------------------- priority


def six_priority_items() -> list[tuple[str, object]]:
    return [
        ("buy", result(side=StrategyRole.BUY, strategy_id="b")),
        ("sell", result(side=StrategyRole.SELL, strategy_id="x")),
        ("manual", ManualCloseEvent()),
        ("safety", SafetyExitEvent()),
        ("recon", ReconciliationEvent()),
        ("fill", FillEvent()),
    ]


def submit(router: DecisionRouter, item: object) -> int:
    if isinstance(item, EvaluationResult):
        return router.submit_evaluation(item)
    return router.submit_event(item)


async def test_priority_order_holds_for_every_arrival_permutation() -> None:
    expected = ["fill", "recon", "safety", "manual", "sell", "buy"]
    for perm in itertools.permutations(six_priority_items()):
        h = Harness()
        names = {id(obj): name for name, obj in perm}
        for _, obj in perm:
            submit(h.router, obj)
        await h.router.drain()
        assert [names[id(p)] for p in h.payloads] == expected


async def test_priority_classes_map_to_documented_levels() -> None:
    h = Harness()
    for event in (BrokerOrderUpdate(), StopEvent(), FillEvent(), ManualCloseEvent()):
        submit(h.router, event)
    await h.router.drain()
    assert [d.priority for d in h.delivered] == [  # type: ignore[attr-defined]
        RoutePriority.FILL_OR_BROKER_UPDATE,
        RoutePriority.FILL_OR_BROKER_UPDATE,
        RoutePriority.PROTECTIVE,
        RoutePriority.MANUAL_CLOSE,
    ]


async def test_late_critical_event_overtakes_queued_signals() -> None:
    h = Harness()
    submit(h.router, result(strategy_id="a"))
    submit(h.router, result(strategy_id="b"))
    submit(h.router, FillEvent())
    await h.router.drain()
    assert isinstance(h.payloads[0], FillEvent)


# ------------------------------------------------------------------ tie-breaking


async def test_tie_break_event_time_then_version_then_symbol_then_strategy_then_arrival() -> None:
    h = Harness()
    t = T0
    late, early = t + timedelta(seconds=5), t
    # Same priority (SELL); each pair differs only in the key under test.
    def sell(minute: int, **kwargs: object) -> EvaluationResult:
        # distinct bars so duplicate suppression is not what is being tested
        bar = FakeSignal(bar_time=T0 + timedelta(minutes=minute))
        return result(side=StrategyRole.SELL, signal=bar, **kwargs)  # type: ignore[arg-type]

    a_time_late = sell(1, strategy_id="a", finished_at=late, version=1)
    a_time_early = sell(2, strategy_id="z", finished_at=early, version=9)
    a_ver_hi = sell(3, strategy_id="a", finished_at=late, version=3)
    a_sym_z = sell(4, strategy_id="a", finished_at=late, version=3, symbol="NVDA")
    a_strat_b = sell(5, strategy_id="b", finished_at=late, version=3, symbol="NVDA")
    for obj in (a_strat_b, a_sym_z, a_ver_hi, a_time_late, a_time_early):
        h.router.submit_evaluation(obj)
    await h.router.drain()
    delivered = [id(p) for p in h.payloads]
    assert delivered == [
        id(a_time_early),  # earliest event time
        id(a_time_late),  # then lower snapshot_version (1 < 3)
        id(a_ver_hi),  # then symbol AAPL < NVDA
        id(a_sym_z),  # then strategy_id a < b
        id(a_strat_b),
    ]


async def test_arrival_sequence_is_final_tie_breaker() -> None:
    h = Harness()
    first = FillEvent(event_time=T0, snapshot_version=2)
    second = FillEvent(event_time=T0, snapshot_version=2)
    seq1 = h.router.submit_event(first)
    seq2 = h.router.submit_event(second)
    assert seq1 < seq2
    await h.router.drain()
    assert h.payloads[0] is first and h.payloads[1] is second


async def test_identical_inputs_give_identical_order_in_any_arrival_order() -> None:
    def items() -> list[object]:
        out: list[object] = []
        for i, sym in enumerate(("AAPL", "MSFT", "NVDA")):
            ts = T0 + timedelta(seconds=i % 2)
            out.append(FillEvent(symbol=sym, event_time=ts, snapshot_version=i))
            out.append(StopEvent(symbol=sym, event_time=ts, snapshot_version=i))
            out.append(result(side=StrategyRole.SELL, symbol=sym, strategy_id=f"s{i}",
                              finished_at=ts, version=i + 1))
            out.append(result(side=StrategyRole.BUY, symbol=sym, strategy_id=f"b{i}",
                              finished_at=ts, version=i + 1))
        return out

    def label(obj: object) -> tuple[str, str, str]:
        if isinstance(obj, EvaluationResult):
            return ("R", obj.symbol, obj.strategy_id)
        return (type(obj).__name__, obj.symbol, "")  # type: ignore[attr-defined]

    orders = []
    for seed in range(8):
        pool = items()
        random.Random(seed).shuffle(pool)
        h = Harness()
        for obj in pool:
            submit(h.router, obj)
        await h.router.drain()
        orders.append([label(p) for p in h.payloads])
    assert all(order == orders[0] for order in orders)
    assert len(orders[0]) == 12


# ------------------------------------------------------------------ revalidation


async def test_fresh_unexpired_current_signal_is_forwarded() -> None:
    h = Harness()
    h.fresh("AAPL", version=4)
    sig = FakeSignal(expires_at=T0 + timedelta(minutes=1), bar_time=T0)
    h.router.submit_evaluation(result(version=4, signal=sig))
    await h.router.drain()
    assert len(h.delivered) == 1
    assert h.router.records[0].status is RouteStatus.FORWARDED


async def test_expired_signal_is_rejected_and_not_forwarded() -> None:
    h = Harness()
    sig = FakeSignal(expires_at=T0 - timedelta(seconds=1), bar_time=T0)
    h.router.submit_evaluation(result(signal=sig))
    await h.router.drain()
    assert h.delivered == []
    rec = h.router.records[0]
    assert (rec.status, rec.reason) == (RouteStatus.REJECTED, RejectReason.EXPIRED.value)


async def test_signal_is_valid_exactly_at_expiry_instant() -> None:
    h = Harness()
    h.router.submit_evaluation(result(signal=FakeSignal(expires_at=T0, bar_time=T0)))
    await h.router.drain()
    assert len(h.delivered) == 1


async def test_superseded_snapshot_is_rejected() -> None:
    h = Harness()
    h.fresh("AAPL", version=5)
    h.router.submit_evaluation(result(version=4))
    await h.router.drain()
    assert h.delivered == []
    assert h.router.records[0].reason == RejectReason.SUPERSEDED.value


async def test_stale_market_data_is_rejected() -> None:
    h = Harness(max_age=timedelta(seconds=10))
    h.freshness.data_ts["AAPL"] = T0 - timedelta(seconds=11)
    h.router.submit_evaluation(result())
    await h.router.drain()
    assert h.delivered == []
    assert h.router.records[0].reason == RejectReason.STALE_DATA.value


async def test_data_exactly_at_max_age_is_fresh_and_missing_data_is_stale() -> None:
    h = Harness(max_age=timedelta(seconds=10))
    h.freshness.data_ts["AAPL"] = T0 - timedelta(seconds=10)
    del h.freshness.data_ts["MSFT"]
    h.router.submit_evaluation(result(symbol="AAPL"))
    h.router.submit_evaluation(result(symbol="MSFT"))
    await h.router.drain()
    assert [r.reason for r in h.router.records] == [None, RejectReason.STALE_DATA.value]


async def test_revalidation_uses_clock_at_delivery_time() -> None:
    h = Harness(max_age=timedelta(seconds=10))
    h.router.submit_evaluation(result())
    h.clock.value = T0 + timedelta(seconds=60)  # data is now 61s old
    await h.router.drain()
    assert h.delivered == []
    assert h.router.records[0].reason == RejectReason.STALE_DATA.value


async def test_expiry_is_checked_before_superseded_and_stale() -> None:
    h = Harness()
    h.fresh("AAPL", version=9)
    h.freshness.data_ts["AAPL"] = T0 - timedelta(hours=1)
    h.router.submit_evaluation(
        result(version=1, signal=FakeSignal(expires_at=T0 - timedelta(seconds=1), bar_time=T0))
    )
    await h.router.drain()
    assert h.router.records[0].reason == RejectReason.EXPIRED.value


# ------------------------------------------------------- critical events bypass


async def test_critical_events_bypass_expiry_staleness_and_supersession() -> None:
    h = Harness(max_age=timedelta(seconds=1))
    h.freshness.data_ts.clear()  # no market data at all
    h.freshness.versions["AAPL"] = 99
    expired = T0 - timedelta(days=1)
    events = [
        FillEvent(event_time=expired, snapshot_version=1, expires_at=expired),
        BrokerOrderUpdate(event_time=expired, snapshot_version=1, expires_at=expired),
        ReconciliationEvent(event_time=expired, expires_at=expired),
        StopEvent(event_time=expired, expires_at=expired),
        SafetyExitEvent(event_time=expired, expires_at=expired),
        ManualCloseEvent(event_time=expired, expires_at=expired),
    ]
    for event in events:
        h.router.submit_event(event)
    await h.router.drain()
    assert len(h.delivered) == len(events)
    assert all(r.status is RouteStatus.FORWARDED for r in h.router.records)


async def test_critical_event_without_timestamp_is_still_delivered() -> None:
    h = Harness()
    h.router.submit_event(FillEvent(event_time=None))
    await h.router.drain()
    assert len(h.delivered) == 1


async def test_non_critical_or_unknown_events_are_not_accepted() -> None:
    h = Harness()
    due = DueEvaluation("AAPL", "s1", StrategyRole.BUY, TriggerKind.MARKET_DATA_ARRIVAL, 1, None,
                        T0)
    with pytest.raises(TypeError):
        h.router.submit_event(due)
    with pytest.raises(TypeError):
        h.router.submit_event(object())
    with pytest.raises(TypeError):
        h.router.submit_evaluation(due)  # type: ignore[arg-type]
    assert h.router.pending == 0


# ------------------------------------------------------------ non-actionable


@pytest.mark.parametrize(
    "outcome",
    [
        EvaluationOutcome.ERROR,
        EvaluationOutcome.TIMEOUT,
        EvaluationOutcome.CANCELLED,
        EvaluationOutcome.STALE_DISCARDED,
        EvaluationOutcome.NO_SIGNAL,
    ],
)
@pytest.mark.parametrize("side", [StrategyRole.BUY, StrategyRole.SELL])
async def test_non_actionable_outcomes_are_recorded_and_never_routed(
    outcome: EvaluationOutcome, side: StrategyRole
) -> None:
    h = Harness()
    h.router.submit_evaluation(result(outcome=outcome, side=side))
    await h.router.drain()
    assert h.delivered == []
    rec = h.router.records[0]
    assert (rec.status, rec.reason) == (RouteStatus.NON_ACTIONABLE, outcome.value)
    assert rec.priority is RoutePriority.NON_ACTIONABLE


async def test_non_actionable_results_do_not_precede_real_work() -> None:
    h = Harness()
    h.router.submit_evaluation(result(outcome=EvaluationOutcome.ERROR, strategy_id="e"))
    h.router.submit_evaluation(result(strategy_id="b"))
    h.router.submit_event(FillEvent())
    await h.router.drain()
    assert [r.status for r in h.router.records] == [
        RouteStatus.FORWARDED,
        RouteStatus.FORWARDED,
        RouteStatus.NON_ACTIONABLE,
    ]


# --------------------------------------------------------- duplicate suppression


async def test_same_strategy_side_symbol_bar_is_forwarded_once() -> None:
    h = Harness()
    h.router.submit_evaluation(result(version=1))
    h.router.submit_evaluation(result(version=1))
    await h.router.drain()
    h.router.submit_evaluation(result(version=1))  # later redelivery
    await h.router.drain()
    assert len(h.delivered) == 1
    assert [r.status for r in h.router.records] == [
        RouteStatus.FORWARDED, RouteStatus.DUPLICATE, RouteStatus.DUPLICATE,
    ]


async def test_different_strategy_side_symbol_or_bar_are_not_duplicates() -> None:
    h = Harness()
    later = T0 + timedelta(minutes=1)
    variants = [
        result(),
        result(strategy_id="s2"),
        result(side=StrategyRole.SELL),
        result(symbol="MSFT"),
        result(signal=FakeSignal(bar_time=later)),
    ]
    for obj in variants:
        h.router.submit_evaluation(obj)
    await h.router.drain()
    assert len(h.delivered) == len(variants)


async def test_explicit_bar_key_from_due_evaluation_drives_suppression() -> None:
    h = Harness()
    bar1 = BarKey("AAPL", "1m", T0)
    bar2 = BarKey("AAPL", "1m", T0 + timedelta(minutes=1))
    nobar = FakeSignal()
    h.router.submit_evaluation(result(signal=nobar), bar_key=bar1)
    h.router.submit_evaluation(result(signal=nobar), bar_key=bar1)
    h.router.submit_evaluation(result(signal=nobar), bar_key=bar2)
    await h.router.drain()
    assert len(h.delivered) == 2


async def test_rejected_signal_does_not_consume_the_once_only_slot() -> None:
    h = Harness(max_age=timedelta(seconds=10))
    h.freshness.data_ts["AAPL"] = T0 - timedelta(minutes=5)
    h.router.submit_evaluation(result())
    await h.router.drain()
    h.fresh("AAPL")
    h.router.submit_evaluation(result())
    await h.router.drain()
    assert len(h.delivered) == 1
    assert [r.status for r in h.router.records] == [RouteStatus.REJECTED, RouteStatus.FORWARDED]


async def test_signal_without_any_bar_identity_is_not_suppressed() -> None:
    h = Harness()
    for _ in range(2):
        h.router.submit_evaluation(result(version=None, signal=FakeSignal()))
    await h.router.drain()
    assert len(h.delivered) == 2


# ----------------------------------------------------------------- serialization


async def test_consumer_is_never_entered_concurrently() -> None:
    active = 0
    peak = 0
    seen: list[int] = []

    async def consumer(decision: object) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        seen.append(decision.sequence)  # type: ignore[attr-defined]
        active -= 1

    clock, fresh = FakeClock(), FakeFreshness()
    fresh.data_ts["AAPL"] = T0
    router = DecisionRouter(consumer=consumer, clock=clock, freshness=fresh,
                            max_data_age=timedelta(seconds=30))
    for i in range(10):
        router.submit_event(FillEvent(event_time=T0 + timedelta(seconds=i)))
    runner = asyncio.create_task(router.run())
    await asyncio.gather(router.drain(), router.drain(), router.drain())
    router.stop()
    await runner
    assert peak == 1
    assert seen == sorted(seen)
    assert len(seen) == 10


async def test_items_submitted_during_delivery_are_ordered_by_priority() -> None:
    delivered: list[object] = []
    holder: dict[str, DecisionRouter] = {}

    async def consumer(decision: object) -> None:
        payload = decision.payload  # type: ignore[attr-defined]
        delivered.append(payload)
        if isinstance(payload, FillEvent) and not isinstance(payload, StopEvent):
            holder["r"].submit_event(StopEvent())
        await asyncio.sleep(0)

    fresh = FakeFreshness()
    fresh.data_ts["AAPL"] = T0
    router = DecisionRouter(consumer=consumer, clock=FakeClock(), freshness=fresh,
                            max_data_age=timedelta(seconds=30))
    holder["r"] = router
    router.submit_evaluation(result(side=StrategyRole.BUY))
    router.submit_event(FillEvent())
    await router.drain()
    assert [type(p).__name__ for p in delivered] == ["FillEvent", "StopEvent", "EvaluationResult"]


async def test_consumer_failure_propagates_and_keeps_remaining_items_queued() -> None:
    calls: list[object] = []

    def consumer(decision: object) -> None:
        calls.append(decision)
        if len(calls) == 1:
            raise RuntimeError("consumer down")

    fresh = FakeFreshness()
    router = DecisionRouter(consumer=consumer, clock=FakeClock(), freshness=fresh,
                            max_data_age=timedelta(seconds=30))
    router.submit_event(FillEvent(event_time=T0))
    router.submit_event(FillEvent(event_time=T0 + timedelta(seconds=1)))
    with pytest.raises(RuntimeError):
        await router.drain()
    assert router.pending == 1
    await router.drain()  # lock was released; the queued fill is still delivered
    assert len(calls) == 2


async def test_stop_refuses_new_items_but_drains_pending() -> None:
    h = Harness()
    h.router.submit_event(FillEvent())
    h.router.stop()
    with pytest.raises(DecisionRouterClosedError):
        h.router.submit_event(FillEvent())
    await asyncio.wait_for(h.router.run(), timeout=1)
    assert len(h.delivered) == 1


async def test_real_queueing_layer_critical_events_are_accepted() -> None:
    backlog = EventBacklog()
    await backlog.put(FillEvent())
    h = Harness()
    h.router.submit_event(await backlog.get())
    await h.router.drain()
    assert len(h.delivered) == 1
