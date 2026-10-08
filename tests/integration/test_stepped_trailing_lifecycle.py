"""Phase-O stepped-trailing lifecycle and fault regression tests.

End-to-end-in-process wiring of:

* o.1 ratchet (``mike_trade.risk.trailing``),
* o.2 replacement coordinator with an in-memory fake port
  (``mike_trade.execution.replace``),
* o.3 close coordinator (``mike_trade.execution.close_coordinator``),
* o.4 trailing state store on temporary SQLite
  (``mike_trade.risk.trailing_state``).

No production code is modified here; all brokers are in-memory fakes.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from typing import Any

import pytest

from mike_trade.domain.trading import TimeInForce
from mike_trade.execution.close_coordinator import (
    CloseCoordinator,
    SellIntent,
    detect_orphan_orders,
)
from mike_trade.execution.replace import (
    ReplacementOutcome,
    ReplacementStatus,
    StopReplacementCoordinator,
)
from mike_trade.risk.trailing import (
    DistanceKind,
    SteppedTrailingPolicy,
    SteppedTrailingState,
    advance,
    initial_state,
    should_replace,
)
from mike_trade.risk.trailing_state import (
    TrailingStateRecord,
    TrailingStateStore,
    TrailingStatus,
)

D = Decimal
NOW = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)
TICK = D("0.01")


def floor_cent(price: Decimal) -> Decimal:
    return (price / TICK).to_integral_value(rounding=ROUND_FLOOR) * TICK


def make_policy() -> SteppedTrailingPolicy:
    return SteppedTrailingPolicy(
        distance_kind=DistanceKind.USD,
        distance_value=D("2"),
        step_kind=DistanceKind.USD,
        step_value=D("1"),
        min_replace_interval=timedelta(0),
        time_in_force=TimeInForce.GTC,
        policy_revision="phase-o-rev1",
    )


def confirmed_outcome(
    order_id: str, broker_id: str, price: Decimal, qty: Decimal
) -> ReplacementOutcome:
    return ReplacementOutcome(
        ReplacementStatus.CONFIRMED,
        order_id,
        broker_order_id=broker_id,
        stop_price=price,
        quantity=qty,
        observed_at=NOW,
        detail="test confirmation",
    )


class InMemoryReplacePort:
    """Synchronous in-memory fake of the StopReplacementPort."""

    def __init__(self) -> None:
        self.increment = TICK
        self.replace_calls: list[Decimal] = []
        self.replace_qtys: list[Decimal] = []
        self.mode = "confirm"
        self.lookup_script: list[ReplacementOutcome] = []
        self._seq = 0

    def supports_standalone_stop(self, *, qty: Decimal, tif: TimeInForce) -> bool:
        return qty == qty.to_integral_value()

    def supports_replace(self, *, qty: Decimal, tif: TimeInForce) -> bool:
        return qty == qty.to_integral_value()

    def price_increment(self) -> Decimal:
        return self.increment

    def replace_stop(
        self,
        order_id: str,
        new_stop_price: Decimal,
        qty: Decimal,
        tif: TimeInForce,
        client_order_id: str,
    ) -> ReplacementOutcome:
        self.replace_calls.append(new_stop_price)
        self.replace_qtys.append(qty)
        self._seq += 1
        if self.mode == "confirm":
            return ReplacementOutcome(
                ReplacementStatus.CONFIRMED,
                order_id,
                broker_order_id=f"{order_id}-r{self._seq}",
                stop_price=new_stop_price,
                quantity=qty,
                observed_at=NOW,
                detail="fake confirm",
            )
        if self.mode == "reject":
            return ReplacementOutcome(
                ReplacementStatus.REJECTED, order_id, observed_at=NOW, detail="fake reject"
            )
        if self.mode == "unknown":
            return ReplacementOutcome(
                ReplacementStatus.UNKNOWN, order_id, observed_at=NOW, detail="fake unknown"
            )
        raise RuntimeError("fake port boom")

    def cancel_stop(self, order_id: str) -> ReplacementOutcome:
        return ReplacementOutcome(
            ReplacementStatus.UNKNOWN, order_id, observed_at=NOW, detail="fake cancel unknown"
        )

    def lookup_order(self, order_id: str) -> ReplacementOutcome:
        if self.lookup_script:
            return self.lookup_script.pop(0)
        return ReplacementOutcome(
            ReplacementStatus.UNKNOWN, order_id, observed_at=NOW, detail="fake lookup unknown"
        )


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value


class Events:
    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []

    def emit(self, event: Mapping[str, object]) -> None:
        self.items.append(dict(event))


class FakeCloseBroker:
    """Minimal async close-coordinator broker with a no-short guard."""

    def __init__(self, position_qty: Decimal = D("10")) -> None:
        self.positions = {"position-1": position_qty}
        self.orders: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.next_exit_status = "ACCEPTED"
        self.seq = 0

    def add_stop(
        self, order_id: str = "stop-1", quantity: Decimal = D("10"), stop_price: Decimal = D("98")
    ) -> None:
        self.orders[order_id] = {
            "broker_order_id": order_id,
            "client_order_id": f"client-{order_id}",
            "position_id": "position-1",
            "symbol": "AAPL",
            "side": "SELL",
            "order_type": "STOP",
            "status": "ACCEPTED",
            "quantity": quantity,
            "filled_quantity": D("0"),
            "stop_price": stop_price,
        }

    async def confirmed_position_quantity(self, position_id: str, symbol: str) -> Decimal:
        return self.positions.get(position_id, D("0"))

    async def list_open_orders(self) -> list[dict[str, Any]]:
        return list(self.orders.values())

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        self.calls.append(("cancel", order_id))
        order = self.orders[order_id]
        order["status"] = "CANCELED"
        return dict(order)

    async def get_order(self, order_id: str) -> dict[str, Any] | None:
        order = self.orders.get(order_id)
        return None if order is None else dict(order)

    async def submit_exit(self, intent: SellIntent, quantity: Decimal) -> dict[str, Any]:
        self.calls.append(("exit", quantity))
        assert quantity <= self.positions[intent.position_id], "fake broker would go short"
        assert quantity > D("0"), "exit quantity must be positive"
        self.seq += 1
        order_id = f"exit-{self.seq}"
        order = {
            "broker_order_id": order_id,
            "client_order_id": intent.client_order_id,
            "position_id": intent.position_id,
            "symbol": intent.symbol,
            "side": "SELL",
            "order_type": str(intent.order_type),
            "status": self.next_exit_status,
            "quantity": quantity,
            "filled_quantity": quantity if self.next_exit_status == "FILLED" else D("0"),
            "stop_price": None,
        }
        self.orders[order_id] = order
        return dict(order)

    async def submit_protective_stop(
        self,
        position_id: str,
        symbol: str,
        quantity: Decimal,
        stop_price: Decimal,
        client_order_id: str,
    ) -> dict[str, Any]:
        self.calls.append(("protect", quantity))
        self.seq += 1
        order_id = f"stop-reprotected-{self.seq}"
        order = {
            "broker_order_id": order_id,
            "client_order_id": client_order_id,
            "position_id": position_id,
            "symbol": symbol,
            "side": "SELL",
            "order_type": "STOP",
            "status": "ACCEPTED",
            "quantity": quantity,
            "filled_quantity": D("0"),
            "stop_price": stop_price,
        }
        self.orders[order_id] = order
        return dict(order)

    async def replace_protective_stop(
        self,
        order_id: str,
        position_id: str,
        symbol: str,
        quantity: Decimal,
        stop_price: Decimal,
        client_order_id: str,
    ) -> dict[str, Any]:
        self.calls.append(("replace", order_id))
        self.orders[order_id]["status"] = "REPLACED"
        self.seq += 1
        new_id = f"stop-replaced-{self.seq}"
        order = {
            "broker_order_id": new_id,
            "client_order_id": client_order_id,
            "position_id": position_id,
            "symbol": symbol,
            "side": "SELL",
            "order_type": "STOP",
            "status": "ACCEPTED",
            "quantity": quantity,
            "filled_quantity": D("0"),
            "stop_price": stop_price,
        }
        self.orders[new_id] = order
        return dict(order)


class TrailHarness:
    """Wire ratchet + replacement coordinator + close coordinator + SQLite store."""

    def __init__(self, tmp_path, cycle_id: str = "cycle-1") -> None:
        self.policy = make_policy()
        self.cycle_id = cycle_id
        self.position_id = "position-1"
        self.symbol = "AAPL"
        self.qty = D("10")
        self.db_path = tmp_path / f"{cycle_id}.db"
        self.store = TrailingStateStore(self.db_path)
        self.port = InMemoryReplacePort()
        self.intents: list[dict[str, object]] = []
        self.clock = Clock()
        self.close_events = Events()
        self.close_broker = FakeCloseBroker(position_qty=self.qty)
        self.close = CloseCoordinator(self.close_broker, Clock(), self.close_events)
        self.ratchet: SteppedTrailingState | None = None
        self.confirmed_stop: Decimal | None = None
        self.protected_order_id = "stop-1"
        self.fill_confirmed = False
        self._client_seq = 0

        def record_intent(intent: Mapping[str, object]) -> None:
            self.intents.append(dict(intent))
            self.store.record_replacement_intent(
                self.cycle_id,
                intent_id=str(intent["client_order_id"]),
                candidate_stop=Decimal(str(intent["new_stop_price"])),
                idempotency_key=f"intent-{intent['client_order_id']}",
            )

        self.coord = StopReplacementCoordinator(
            self.port, record_intent, min_replace_interval=timedelta(0), clock=self.clock
        )

    def confirm_entry_fill(self, entry: Decimal, qty: Decimal) -> None:
        if qty <= D("0"):
            raise ValueError("entry fill quantity must be positive")
        if entry <= D("0"):
            raise ValueError("entry price must be positive")
        self.ratchet = initial_state(entry, self.policy)
        self.confirmed_stop = self.ratchet.candidate_stop
        self.qty = qty
        self.fill_confirmed = True
        record = TrailingStateRecord(
            cycle_id=self.cycle_id,
            entry_reference=self.ratchet.entry_reference,
            high_watermark=self.ratchet.high_watermark,
            ratchet_anchor=self.ratchet.ratchet_anchor,
            candidate_stop=self.ratchet.candidate_stop,
            last_confirmed_stop=self.confirmed_stop,
            protected_quantity=qty,
            broker_order_id=self.protected_order_id,
            broker_status="ACCEPTED",
            policy_revision=self.policy.policy_revision,
            time_in_force=self.policy.time_in_force,
        )
        self.store.create(record, idempotency_key="create", occurred_at=NOW)
        self.close_broker.positions[self.position_id] = qty
        self.close_broker.orders.clear()
        self.close_broker.add_stop(
            self.protected_order_id, quantity=qty, stop_price=self.confirmed_stop
        )
        self.close.register_protective_stop(
            position_id=self.position_id,
            symbol=self.symbol,
            order_id=self.protected_order_id,
            stop_price=self.confirmed_stop,
        )

    def on_high(self, high: Decimal) -> ReplacementOutcome | None:
        """Fold one high through ratchet -> store -> replacement coordinator."""
        if not self.fill_confirmed or self.ratchet is None or self.confirmed_stop is None:
            return None
        rec = self.store.load(self.cycle_id)
        assert rec is not None
        if rec.status in (TrailingStatus.TRAIL_STALE, TrailingStatus.CLOSED):
            return None
        if rec.requires_reconciliation:
            raise RuntimeError("outstanding replacement requires reconciliation")
        self.ratchet = advance(self.ratchet, high, self.policy)
        self._client_seq += 1
        self.store.record_candidate(
            self.cycle_id,
            high_watermark=self.ratchet.high_watermark,
            ratchet_anchor=self.ratchet.ratchet_anchor,
            candidate_stop=self.ratchet.candidate_stop,
            policy_revision=self.policy.policy_revision,
            idempotency_key=f"ratchet-{self._client_seq}-{high}",
        )
        if not should_replace(self.ratchet.candidate_stop, self.confirmed_stop, floor_cent):
            return None
        client_id = f"replace-{self._client_seq}"
        result = self.coord.replace_stop(
            protected_order_id=self.protected_order_id,
            candidate_stop=self.ratchet.candidate_stop,
            last_confirmed_stop=self.confirmed_stop,
            qty=self.qty,
            tif=self.policy.time_in_force,
            client_order_id=client_id,
            position_id=self.position_id,
        )
        if result.status is ReplacementStatus.CONFIRMED:
            assert result.stop_price is not None and result.broker_order_id is not None
            self.store.confirm_replacement(
                self.cycle_id,
                price=result.stop_price,
                broker_order_id=result.broker_order_id,
                broker_status="REPLACED",
                idempotency_key=f"confirm-{client_id}",
            )
            old = self.protected_order_id
            self.confirmed_stop = result.stop_price
            self.protected_order_id = result.broker_order_id
            self.close_broker.orders.pop(old, None)
            self.close_broker.add_stop(
                self.protected_order_id, quantity=self.qty, stop_price=self.confirmed_stop
            )
            self.close.register_protective_stop(
                position_id=self.position_id,
                symbol=self.symbol,
                order_id=self.protected_order_id,
                stop_price=self.confirmed_stop,
            )
        elif result.status is ReplacementStatus.REJECTED:
            self.store.reject_replacement(
                self.cycle_id, broker_status="REJECTED", idempotency_key=f"reject-{client_id}"
            )
        elif result.status is ReplacementStatus.UNKNOWN:
            self.store.mark_replacement_unknown(
                self.cycle_id, idempotency_key=f"unknown-{client_id}"
            )
        elif result.status is ReplacementStatus.STOP_FILLED_DURING_REPLACE:
            self.store.record_stop_filled(
                self.cycle_id,
                broker_order_id=result.broker_order_id or self.protected_order_id,
                idempotency_key=f"fill-{client_id}",
            )
        return result

    def close_store(self) -> None:
        self.store.close()


def test_canonical_lifecycle_exact_stops_and_replace_counts(tmp_path) -> None:
    h = TrailHarness(tmp_path)
    h.confirm_entry_fill(D("100"), D("10"))
    assert h.confirmed_stop == D("98")

    assert h.on_high(D("100.80")) is None
    assert h.port.replace_calls == []
    assert h.confirmed_stop == D("98")

    out1 = h.on_high(D("101"))
    assert out1 is not None and out1.status is ReplacementStatus.CONFIRMED
    assert out1.stop_price == D("99")
    assert h.confirmed_stop == D("99")

    out2 = h.on_high(D("103.20"))
    assert out2 is not None and out2.status is ReplacementStatus.CONFIRMED
    assert out2.stop_price == D("101")
    assert h.confirmed_stop == D("101")

    assert h.on_high(D("102")) is None
    assert h.confirmed_stop == D("101")

    assert h.port.replace_calls == [D("99"), D("101")]
    rec = h.store.load(h.cycle_id)
    assert rec is not None and rec.last_confirmed_stop == D("101")
    # close coordinator still tracks the live protective stop
    assert h.close_broker.orders[h.protected_order_id]["stop_price"] == D("101")
    h.close_store()


def test_protection_requires_confirmed_positive_fill(tmp_path) -> None:
    h = TrailHarness(tmp_path)
    # No fill yet: highs are ignored, nothing persisted, no broker call.
    assert h.on_high(D("101")) is None
    assert h.port.replace_calls == []
    assert h.store.load(h.cycle_id) is None
    with pytest.raises(ValueError):
        h.confirm_entry_fill(D("100"), D("0"))
    with pytest.raises(ValueError):
        h.confirm_entry_fill(D("100"), D("-1"))
    h.confirm_entry_fill(D("100"), D("10"))
    rec = h.store.load(h.cycle_id)
    assert rec is not None
    assert rec.last_confirmed_stop == D("98")
    assert rec.protected_quantity == D("10")
    h.close_store()


def test_stop_never_moves_down_seeded_path(tmp_path) -> None:
    rng = random.Random(20261008)
    h = TrailHarness(tmp_path)
    h.confirm_entry_fill(D("100"), D("10"))
    confirmed_series = [h.confirmed_stop]
    prior = h.confirmed_stop
    assert prior is not None
    price = D("100")
    for _ in range(80):
        move = D(str(round(rng.uniform(-1.5, 2.5), 2)))
        price = max(D("90"), price + move)
        result = h.on_high(price)
        assert h.confirmed_stop is not None and h.confirmed_stop >= prior
        if result is not None and result.status is ReplacementStatus.CONFIRMED:
            assert result.stop_price is not None and result.stop_price >= prior
        prior = h.confirmed_stop
        confirmed_series.append(h.confirmed_stop)
    # every replace call strictly exceeds the confirmed stop that preceded it
    seen_confirmed: list[Decimal] = [D("98")]
    call_idx = 0
    # re-derive ordering: replace calls must be strictly increasing
    for call in h.port.replace_calls:
        assert call > seen_confirmed[-1], f"replace {call} not above prior {seen_confirmed[-1]}"
        # rounded call equals the submitted price (already tick-aligned here)
        assert floor_cent(call) > seen_confirmed[-1]
        seen_confirmed.append(call)
        call_idx += 1
    assert confirmed_series == sorted(confirmed_series, key=Decimal)
    h.close_store()


def test_parity_pure_ratchet_vs_confirmed_sequence(tmp_path) -> None:
    path = [D("100.80"), D("101"), D("101.50"), D("103.20"), D("102"), D("104.10")]
    policy = make_policy()
    pure = initial_state(D("100"), policy)
    pure_candidates: list[Decimal] = []
    for high in path:
        pure = advance(pure, high, policy)
        pure_candidates.append(pure.candidate_stop)

    h = TrailHarness(tmp_path)
    h.confirm_entry_fill(D("100"), D("10"))
    harness_candidates: list[Decimal] = []
    harness_confirmed: list[Decimal] = []
    for high in path:
        h.on_high(high)
        rec = h.store.load(h.cycle_id)
        assert rec is not None
        harness_candidates.append(rec.candidate_stop)
        assert h.confirmed_stop is not None
        harness_confirmed.append(h.confirmed_stop)
    # persisted candidates always agree with the pure ratchet
    assert harness_candidates == pure_candidates
    # whenever all replacements confirm, the confirmed stop tracks the candidate
    # at each step where the candidate advanced above the prior confirmed stop.
    assert harness_confirmed == [D("98"), D("99"), D("99"), D("101"), D("101"), D("102")]
    assert pure_candidates == [D("98"), D("99"), D("99"), D("101"), D("101"), D("102")]
    h.close_store()


def test_rejected_replace_retains_stop_blocks_entries(tmp_path) -> None:
    h = TrailHarness(tmp_path)
    h.confirm_entry_fill(D("100"), D("10"))
    h.port.mode = "reject"
    out = h.on_high(D("101"))
    assert out is not None and out.status is ReplacementStatus.REJECTED
    assert out.authoritative_stop_price == D("98")
    assert out.authoritative_order_id == "stop-1"
    assert out.event == "TRAIL_UPDATE_UNCONFIRMED"
    assert out.entries_blocked and out.reconciliation_required
    assert h.confirmed_stop == D("98")
    assert h.port.replace_calls == [D("99")]
    h.close_store()


def test_unknown_replace_lookup_reconciliation_resumes(tmp_path) -> None:
    h = TrailHarness(tmp_path)
    h.confirm_entry_fill(D("100"), D("10"))
    h.port.mode = "unknown"
    out = h.on_high(D("101"))
    assert out is not None and out.status is ReplacementStatus.UNKNOWN
    assert out.authoritative_stop_price == D("98")
    assert out.entries_blocked and out.reconciliation_required
    rec = h.store.load(h.cycle_id)
    assert rec is not None and rec.requires_reconciliation
    # advancement is blocked while the intent is outstanding
    with pytest.raises(RuntimeError, match="reconciliation"):
        h.on_high(D("102"))
    # lookup-based reconciliation: broker confirms the pending replace at 99
    lookup_confirm = confirmed_outcome("stop-1", "stop-1-r9", D("99"), D("10"))
    resolved = h.coord.confirm("stop-1", observation=lookup_confirm, position_id="position-1")
    assert resolved.status is ReplacementStatus.CONFIRMED
    h.store.confirm_replacement(
        h.cycle_id, price=D("99"), broker_order_id="stop-1-r9", idempotency_key="reconcile-99"
    )
    h.confirmed_stop = D("99")
    h.protected_order_id = "stop-1-r9"
    h.port.mode = "confirm"
    out2 = h.on_high(D("103.20"))
    assert out2 is not None and out2.status is ReplacementStatus.CONFIRMED
    assert h.confirmed_stop == D("101")
    h.close_store()


def test_stop_fill_during_replace_closes_cycle(tmp_path) -> None:
    h = TrailHarness(tmp_path)
    h.confirm_entry_fill(D("100"), D("10"))
    h.port.lookup_script = [
        ReplacementOutcome(
            ReplacementStatus.STOP_FILLED_DURING_REPLACE,
            "stop-1",
            broker_order_id="stop-1",
            filled_quantity=D("10"),
            remaining_quantity=D("0"),
            observed_at=NOW,
            detail="old stop filled",
        )
    ]
    out = h.on_high(D("101"))
    assert out is not None and out.status is ReplacementStatus.STOP_FILLED_DURING_REPLACE
    assert out.filled_quantity == D("10")
    assert out.remaining_quantity == D("0")
    assert h.port.replace_calls == []
    assert h.confirmed_stop == D("98")  # last verified stop retained
    h.close_store()


def test_partial_stop_fill_during_replace_protects_remainder(tmp_path) -> None:
    h = TrailHarness(tmp_path)
    h.confirm_entry_fill(D("100"), D("10"))
    h.port.lookup_script = [
        ReplacementOutcome(
            ReplacementStatus.STOP_FILLED_DURING_REPLACE,
            "stop-1",
            broker_order_id="stop-1",
            filled_quantity=D("3.5"),
            remaining_quantity=D("6.5"),
            observed_at=NOW,
            detail="partial fill",
        )
    ]
    out = h.on_high(D("101"))
    assert out is not None and out.status is ReplacementStatus.STOP_FILLED_DURING_REPLACE
    assert out.remaining_quantity == D("6.5")
    assert h.port.replace_calls == []
    # remaining quantity is exactly the unfilled residual
    assert out.remaining_quantity == D("10") - out.filled_quantity
    h.close_store()


async def test_discretionary_close_races_pending_replace_no_short(tmp_path) -> None:
    h = TrailHarness(tmp_path)
    h.confirm_entry_fill(D("100"), D("10"))
    h.port.mode = "unknown"
    out = h.on_high(D("101"))
    assert out is not None and out.status is ReplacementStatus.UNKNOWN
    assert h.coord.pending("position-1") or h.coord.pending("stop-1")
    intent = SellIntent(
        position_id="position-1",
        symbol="AAPL",
        quantity=D("10"),
        kind="MANUAL_CLOSE",
        client_order_id="exit-manual-1",
    )
    outcome = await h.close.close(intent)
    assert outcome.status in ("SUBMITTED", "REPROTECTED", "FLAT")
    exits = [call for call in h.close_broker.calls if call[0] == "exit"]
    assert len(exits) == 1
    assert exits[0][1] <= D("10")
    assert h.close_broker.positions["position-1"] >= D("0")
    h.close_store()


def test_no_duplicate_pending_under_rapid_highs(tmp_path) -> None:
    h = TrailHarness(tmp_path)
    h.confirm_entry_fill(D("100"), D("10"))
    h.port.mode = "unknown"
    first = h.coord.replace_stop(
        protected_order_id="stop-1",
        candidate_stop=D("99"),
        last_confirmed_stop=D("98"),
        qty=D("10"),
        tif=TimeInForce.GTC,
        client_order_id="replace-1",
        position_id="position-1",
    )
    second = h.coord.replace_stop(
        protected_order_id="stop-1",
        candidate_stop=D("101"),
        last_confirmed_stop=D("98"),
        qty=D("10"),
        tif=TimeInForce.GTC,
        client_order_id="replace-2",
        position_id="position-1",
    )
    assert first.status is ReplacementStatus.UNKNOWN
    assert second.status is ReplacementStatus.UNKNOWN
    assert second.coalesced is True
    assert second.coalesced_stop_price == D("101")
    assert len(h.port.replace_calls) == 1
    h.close_store()


async def test_orphan_stop_after_flat_detected(tmp_path) -> None:
    h = TrailHarness(tmp_path)
    h.confirm_entry_fill(D("100"), D("10"))
    # position goes flat but the broker stop is still working
    h.close_broker.positions["position-1"] = D("0")
    orphans = await h.close.detect_orphans(
        local_intents=[],
        local_positions=[{"symbol": "AAPL", "quantity": D("0")}],
    )
    by_id = {o.broker_order_id: o.reason for o in orphans}
    assert by_id.get("stop-1") == "ORPHAN_STOP_FLAT_POSITION"
    # pure-function form agrees and detection adopts/cancels nothing
    calls_before = list(h.close_broker.calls)
    found = detect_orphan_orders(
        h.close_broker.orders.values(),
        [],
        [{"symbol": "AAPL", "quantity": D("0")}],
    )
    assert {o.broker_order_id for o in found} == {"stop-1"}
    assert h.close_broker.calls == calls_before
    h.close_store()


def test_restart_blocks_until_reconciliation_then_resumes(tmp_path) -> None:
    h = TrailHarness(tmp_path)
    h.confirm_entry_fill(D("100"), D("10"))
    assert h.on_high(D("101")).status is ReplacementStatus.CONFIRMED  # confirmed 99
    h.port.mode = "unknown"
    out = h.on_high(D("103.20"))  # candidate 101, intent outstanding
    assert out is not None and out.status is ReplacementStatus.UNKNOWN
    watermark = h.ratchet.high_watermark if h.ratchet is not None else D("103.20")
    anchor = h.ratchet.ratchet_anchor if h.ratchet is not None else D("103")
    h.close_store()

    reopened = TrailingStateStore(h.db_path)
    rec = reopened.load(h.cycle_id)
    assert rec is not None
    assert rec.requires_reconciliation
    assert rec.high_watermark == watermark
    assert rec.ratchet_anchor == anchor
    with pytest.raises(RuntimeError, match="reconciliation"):
        reopened.record_candidate(
            h.cycle_id,
            high_watermark=D("104"),
            ratchet_anchor=D("104"),
            candidate_stop=D("102"),
            policy_revision="phase-o-rev1",
            idempotency_key="blocked-after-restart",
        )
    reopened.reconcile(
        h.cycle_id, idempotency_key="restart-reconcile", broker_order_id="stop-1-r1"
    )
    assert not reopened.load(h.cycle_id).requires_reconciliation
    # resume from the persisted watermark/anchor
    resumed = SteppedTrailingState(
        entry_reference=rec.entry_reference,
        high_watermark=rec.high_watermark,
        ratchet_anchor=rec.ratchet_anchor,
        candidate_stop=rec.candidate_stop,
    )
    assert resumed.high_watermark == D("103.20")
    assert resumed.ratchet_anchor == D("103")
    nxt = advance(resumed, D("104.10"), make_policy())
    assert nxt.candidate_stop == D("102")
    reopened.record_candidate(
        h.cycle_id,
        high_watermark=nxt.high_watermark,
        ratchet_anchor=nxt.ratchet_anchor,
        candidate_stop=nxt.candidate_stop,
        policy_revision="phase-o-rev1",
        idempotency_key="resumed-ratchet",
    )
    assert reopened.load(h.cycle_id).candidate_stop == D("102")
    reopened.close()


def test_engine_offline_stale_retains_stop_and_blocks_advance(tmp_path) -> None:
    h = TrailHarness(tmp_path)
    h.confirm_entry_fill(D("100"), D("10"))
    assert h.on_high(D("101")).status is ReplacementStatus.CONFIRMED
    assert h.confirmed_stop == D("99")
    stale = h.store.mark_stale(h.cycle_id, "engine heartbeat lost", idempotency_key="offline-1")
    assert stale.status is TrailingStatus.TRAIL_STALE
    assert stale.last_confirmed_stop == D("99")
    calls_before = list(h.port.replace_calls)
    intents_before = list(h.intents)
    # new highs while stale neither advance the ratchet nor submit replacements
    assert h.on_high(D("110")) is None
    assert h.port.replace_calls == calls_before
    assert h.intents == intents_before
    rec = h.store.load(h.cycle_id)
    assert rec is not None
    assert rec.status is TrailingStatus.TRAIL_STALE
    assert rec.last_confirmed_stop == D("99")
    assert rec.high_watermark == D("101")
    h.close_store()


def test_rounding_before_compare_never_submits_equal_or_lower(tmp_path) -> None:
    h = TrailHarness(tmp_path)
    h.confirm_entry_fill(D("100"), D("10"))
    # 98.019 floors to 98.01 which is not above 98... but ratchet never
    # produces it; assert the gate directly plus a coordinator-level check.
    assert should_replace(D("98.019"), D("98.01"), floor_cent) is False
    assert should_replace(D("98.021"), D("98.01"), floor_cent) is True
    before = list(h.port.replace_calls)
    out = h.coord.replace_stop(
        protected_order_id="stop-1",
        candidate_stop=D("98.019"),
        last_confirmed_stop=D("98.01"),
        qty=D("10"),
        tif=TimeInForce.GTC,
        client_order_id="round-check",
        position_id="position-1",
    )
    assert out.status is ReplacementStatus.REJECTED
    assert h.port.replace_calls == before
    h.close_store()


def test_fail_closed_capability_without_verified_replace(tmp_path) -> None:
    h = TrailHarness(tmp_path)
    h.confirm_entry_fill(D("100"), D("10"))
    h.port.supports_replace = lambda *, qty, tif: False  # type: ignore[method-assign]
    out = h.coord.replace_stop(
        protected_order_id="stop-1",
        candidate_stop=D("99"),
        last_confirmed_stop=D("98"),
        qty=D("10"),
        tif=TimeInForce.GTC,
        client_order_id="cap-check",
        position_id="position-1",
    )
    assert out.status is ReplacementStatus.UNSUPPORTED
    assert h.port.replace_calls == []
    h.close_store()
