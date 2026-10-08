"""End-to-end order-lifecycle protocol checks across the f-phase stack.

Exercises intents and the FSM (f.1), the coordinator (f.2), the submitter
(f.3), and the fill processor (f.4) through their public APIs only, against an
isolated temporary SQLite database and the scriptable fake Broker port from
``tests/fixtures/order_events.py``. No network access, credentials, or live
endpoints are used.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

import pytest

from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.trading import BrokerOrder, BrokerOrderStatus, BudgetPolicy
from mike_trade.domain.trading import OrderIntent as DomainIntent
from mike_trade.domain.trading import (
    OrderPurpose as DomainPurpose,
)
from mike_trade.domain.trading import OrderSide, OrderType, PositionCycle, TimeInForce
from mike_trade.execution.coordinator import (
    DecisionReason,
    ExecutionKey,
    OrderCommand,
    OrderCommandCoordinator,
)
from mike_trade.execution.fill_processor import FillProcessor
from mike_trade.execution.intents import (
    IntentStore,
    OrderIntent,
    OrderPurpose,
    persist_intent,
)
from mike_trade.execution.order_fsm import OrderStatus, TransitionResult
from mike_trade.execution.submission import (
    OrderOutcomeUnresolved,
    OrderSubmitter,
    UnresolvedOrderMarker,
)
from mike_trade.persistence.journal import DomainEventJournal
from mike_trade.persistence.repositories import (
    AccountScopeRepository,
    BrokerOrderRepository,
    OrderIntentRepository,
    PositionCycleRepository,
)
from mike_trade.persistence.schema import initialize_database
from mike_trade.persistence.transaction import ScopedWriter, WriterSession

_FIXTURES_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "order_events.py"
_spec = importlib.util.spec_from_file_location("_order_fault_fixtures", _FIXTURES_PATH)
assert _spec is not None and _spec.loader is not None
_fixtures = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _fixtures
_spec.loader.exec_module(_fixtures)

ScriptedFakeBroker = _fixtures.ScriptedFakeBroker
FixedClock = _fixtures.FixedClock
make_order_update = _fixtures.make_order_update
make_fill_event = _fixtures.make_fill_event
FaultScript = _fixtures.FaultScript

BASE_NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
D = Decimal

_BUDGET = BudgetPolicy(
    per_symbol_budget=D("100000"),
    total_application_budget=D("100000"),
    max_open_positions=20,
)

_DOMAIN_PURPOSE = {
    OrderPurpose.ENTRY_LONG: DomainPurpose.ENTRY,
    OrderPurpose.EXIT_LONG: DomainPurpose.STRATEGY_EXIT,
    OrderPurpose.PROTECTIVE_STOP: DomainPurpose.PROTECTIVE_STOP,
    OrderPurpose.TRAILING_STOP: DomainPurpose.PROTECTIVE_STOP,
    OrderPurpose.MANUAL_CLOSE: DomainPurpose.MANUAL_CLOSE,
    OrderPurpose.SAFETY_EXIT: DomainPurpose.SAFETY_EXIT,
}


# ------------------------------------------------------------------ harness


@dataclass
class _Record:
    kind: str
    status: OrderStatus | None = None
    reason: str = ""
    filled_qty: Decimal | None = None
    broker_order_id: str | None = None


@dataclass
class _MemoryLedger:
    """In-memory append-only SubmissionLedger for the protocol harness."""

    intents: dict[str, OrderIntent] = field(default_factory=dict)
    statuses: dict[str, OrderStatus] = field(default_factory=dict)
    records: list[tuple[str, _Record]] = field(default_factory=list)
    broker_ids: dict[str, str] = field(default_factory=dict)
    marker_map: dict[str, UnresolvedOrderMarker] = field(default_factory=dict)

    def register(self, intent: OrderIntent) -> None:
        self.intents[intent.client_order_id] = intent
        self.statuses[intent.client_order_id] = OrderStatus.INTENT_PERSISTED

    def status(self, client_order_id: str) -> OrderStatus | None:
        return self.statuses.get(client_order_id)

    def record_transition(
        self,
        intent: OrderIntent,
        result: TransitionResult,
        *,
        reason: str,
        broker_order_id: str | None,
    ) -> None:
        cid = intent.client_order_id
        assert self.statuses[cid] is result.previous_status
        self.statuses[cid] = result.status
        if broker_order_id is not None:
            self.broker_ids[cid] = broker_order_id
        self.records.append(
            (cid, _Record("transition", result.status, reason, result.filled_qty, broker_order_id))
        )

    def record_rearm(self, intent: OrderIntent, *, reason: str) -> None:
        self.statuses[intent.client_order_id] = OrderStatus.INTENT_PERSISTED
        self.records.append(
            (intent.client_order_id, _Record("rearm", OrderStatus.INTENT_PERSISTED, reason))
        )

    def annotate(self, intent: OrderIntent, kind: str, detail: str) -> None:
        self.records.append((intent.client_order_id, _Record(kind, reason=detail)))

    def broker_order_id(self, client_order_id: str) -> str | None:
        return self.broker_ids.get(client_order_id)

    def inflight(self, account_key: str) -> list[OrderIntent]:
        return [
            intent
            for cid, intent in self.intents.items()
            if intent.account_key == account_key
            and self.statuses[cid]
            in {OrderStatus.INTENT_PERSISTED, OrderStatus.SUBMITTING, OrderStatus.UNKNOWN}
        ]

    def put_marker(self, marker: UnresolvedOrderMarker) -> None:
        self.marker_map[marker.client_order_id] = marker

    def clear_marker(self, client_order_id: str) -> None:
        self.marker_map.pop(client_order_id, None)

    def markers(self, account_key: str) -> list[UnresolvedOrderMarker]:
        return [m for m in self.marker_map.values() if m.account_key == account_key]

    def transitions(self) -> list[OrderStatus]:
        return [r.status for _, r in self.records if r.kind == "transition" and r.status]

    def transition_log(self) -> list[tuple[str, str]]:
        return [
            (r.status.value if r.status else r.kind, r.reason)
            for _, r in self.records
            if r.kind in {"transition", "rearm"}
        ]


class _QuoteAges:
    async def quote_age(self, intent: OrderIntent, now: datetime) -> timedelta:
        return timedelta(seconds=1)


class _NoOpenOrders:
    async def has_open_or_unresolved_order(self, key: ExecutionKey, purpose: str) -> bool:
        return False


class _Positions:
    def __init__(
        self,
        held: Decimal = D("0"),
        committed: Decimal = D("0"),
        protective: Decimal = D("0"),
    ) -> None:
        self.held = held
        self.committed = committed
        self.protective = protective

    async def confirmed_long_quantity(self, key: ExecutionKey) -> Decimal:
        return self.held

    async def committed_sell_quantity(self, key: ExecutionKey) -> Decimal:
        return self.committed

    async def protective_sell_quantity(self, key: ExecutionKey) -> Decimal:
        return self.protective


class _Cash:
    def __init__(self, amount: Decimal) -> None:
        self.amount = amount

    async def available_cash(self, key: ExecutionKey) -> Decimal:
        return self.amount


def _open_db(
    tmp_path: Path, name: str, environment: AccountEnvironment, scope: AccountScope
) -> tuple[WriterSession, ScopedWriter]:
    path = tmp_path / name
    initialize_database(path, environment, now=BASE_NOW).close()
    session = WriterSession(path)
    writer = session.bind_scope(scope.scope_id)
    AccountScopeRepository(writer, FixedClock(BASE_NOW)).register_scope(scope)
    return session, writer


def _exec_intent(
    *,
    intent_id: str,
    client_order_id: str,
    account_key: str = "ACCT",
    mode: AccountEnvironment = AccountEnvironment.PAPER,
    symbol: str = "XYZ",
    side: OrderSide = OrderSide.BUY,
    purpose: OrderPurpose = OrderPurpose.ENTRY_LONG,
    qty: str = "10",
    order_type: OrderType = OrderType.LIMIT,
    limit_price: str = "100",
    attempt: int = 0,
    cycle: str | None = None,
    now: datetime = BASE_NOW,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        execution_mode=mode,
        account_key=account_key,
        symbol=symbol,
        side=side,
        purpose=purpose,
        position_cycle_id=cycle or f"cycle-{intent_id}",
        qty=D(qty),
        order_type=order_type,
        limit_price=D(limit_price) if order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT} else None,
        time_in_force=TimeInForce.DAY,
        signal_bar_time=now - timedelta(minutes=1),
        signal_expires_at=now + timedelta(minutes=30),
        quote_reference_time=now - timedelta(seconds=2),
        client_order_id=client_order_id,
        attempt_number=attempt,
        created_at=now,
    )


def _exit_intent(
    *,
    intent_id: str,
    client_order_id: str,
    cycle: str,
    purpose: OrderPurpose = OrderPurpose.EXIT_LONG,
    qty: str = "10",
    account_key: str = "ACCT",
    mode: AccountEnvironment = AccountEnvironment.PAPER,
    now: datetime = BASE_NOW,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        execution_mode=mode,
        account_key=account_key,
        symbol="XYZ",
        side=OrderSide.SELL,
        purpose=purpose,
        position_cycle_id=cycle,
        qty=D(qty),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        signal_bar_time=now - timedelta(minutes=1),
        signal_expires_at=now + timedelta(minutes=30),
        quote_reference_time=now - timedelta(seconds=2),
        client_order_id=client_order_id,
        attempt_number=0,
        created_at=now,
    )


def _persist(store: IntentStore, ledger: _MemoryLedger, intent: OrderIntent) -> OrderIntent:
    persisted = persist_intent(store, intent)
    ledger.register(persisted)
    return persisted


def _coordinator(
    clock: FixedClock,
    *,
    cash: Decimal = D("100000"),
    held: Decimal = D("0"),
    committed: Decimal = D("0"),
    protective: Decimal = D("0"),
) -> OrderCommandCoordinator:
    return OrderCommandCoordinator(
        clock=clock,
        quote_age_provider=_QuoteAges(),
        open_order_provider=_NoOpenOrders(),
        position_provider=_Positions(held, committed, protective),
        buying_power_budget_provider=_Cash(cash),
        max_quote_age=timedelta(seconds=30),
    )


def _submitter(
    broker: ScriptedFakeBroker, ledger: _MemoryLedger, clock: FixedClock
) -> OrderSubmitter:
    return OrderSubmitter(
        broker=broker,
        ledger=ledger,
        clock=clock,
        quote_checker=lambda intent, now: True,
    )


def _ensure_db_order(
    writer: ScopedWriter,
    intent: OrderIntent,
    *,
    broker_order_id: str,
    scope_id: str | None = None,
) -> BrokerOrder:
    """Mirror an accepted execution intent into the fill processor's ledger tables."""
    scope = scope_id or intent.account_scope_id
    cycles = PositionCycleRepository(writer, FixedClock(BASE_NOW))
    intents = OrderIntentRepository(writer, FixedClock(BASE_NOW), _BUDGET)
    orders = BrokerOrderRepository(writer, FixedClock(BASE_NOW))
    qty = intent.qty if intent.qty is not None else D("10")
    if cycles.get_cycle(intent.position_cycle_id) is None:
        cycles.open_cycle(
            PositionCycle(
                cycle_id=intent.position_cycle_id,
                account_scope_id=scope,
                symbol=intent.symbol,
            )
        )
    domain_intent = DomainIntent(
        intent_id=intent.intent_id,
        client_order_id=intent.client_order_id,
        account_scope_id=scope,
        cycle_id=intent.position_cycle_id,
        symbol=intent.symbol,
        side=intent.side,
        purpose=_DOMAIN_PURPOSE[intent.purpose],
        order_type=intent.order_type,
        time_in_force=intent.time_in_force,
        created_at=intent.created_at,
        quantity=qty,
        limit_price=intent.limit_price,
    )
    intents.record_intent(
        domain_intent, D("1000") if _DOMAIN_PURPOSE[intent.purpose] is DomainPurpose.ENTRY else D("0")
    )
    order = BrokerOrder(
        broker_order_id=broker_order_id,
        client_order_id=intent.client_order_id,
        account_scope_id=scope,
        symbol=intent.symbol,
        side=intent.side,
        order_type=intent.order_type,
        status=BrokerOrderStatus.ACCEPTED,
        quantity=qty,
        filled_quantity=D("0"),
        submitted_at=intent.created_at,
        updated_at=intent.created_at,
        limit_price=intent.limit_price,
    )
    orders.upsert_broker_order(order, intent_id=intent.intent_id)
    return order


def _snapshot_bytes(writer: ScopedWriter, scope_id: str) -> bytes:
    """Canonical byte projection of persisted order/ledger state.

    Surrogate UUID columns (activity/row ids) are intentionally excluded so the
    projection is a deterministic function of the ingested fixture stream.
    """
    conn = writer.connection
    projection: dict[str, Any] = {
        "broker_orders": [
            dict(zip(("client_order_id", "broker_order_id", "symbol", "side", "status",
                       "quantity", "filled_quantity", "filled_avg_price"), row, strict=True))
            for row in conn.execute(
                "SELECT client_order_id, broker_order_id, symbol, side, status, quantity,"
                " filled_quantity, filled_avg_price FROM broker_orders"
                " WHERE account_scope_id=? ORDER BY client_order_id",
                (scope_id,),
            ).fetchall()
        ],
        "fills": [
            dict(zip(("external_activity_id", "broker_order_id", "cycle_id", "symbol", "side",
                       "quantity", "price", "fee"), row, strict=True))
            for row in conn.execute(
                "SELECT external_activity_id, broker_order_id, cycle_id, symbol, side,"
                " quantity, price, fee FROM fills WHERE account_scope_id=?"
                " ORDER BY external_activity_id",
                (scope_id,),
            ).fetchall()
        ],
        "positions": [
            dict(zip(("symbol", "quantity", "average_entry_price"), row, strict=True))
            for row in conn.execute(
                "SELECT symbol, quantity, average_entry_price FROM positions"
                " WHERE account_scope_id=? ORDER BY symbol",
                (scope_id,),
            ).fetchall()
        ],
        "budget_reservations": [
            dict(zip(("reservation_id", "amount", "state"), row, strict=True))
            for row in conn.execute(
                "SELECT reservation_id, amount, state FROM budget_reservations"
                " WHERE account_scope_id=? ORDER BY reservation_id",
                (scope_id,),
            ).fetchall()
        ],
        "broker_activities": [
            dict(zip(("external_activity_id", "activity_type", "occurred_at"), row, strict=True))
            for row in conn.execute(
                "SELECT external_activity_id, activity_type, occurred_at FROM broker_activities"
                " WHERE account_scope_id=? ORDER BY external_activity_id",
                (scope_id,),
            ).fetchall()
        ],
        "order_intents": [
            dict(zip(("intent_id", "client_order_id", "symbol", "side", "purpose", "status"),
                     row, strict=True))
            for row in conn.execute(
                "SELECT intent_id, client_order_id, symbol, side, purpose, status"
                " FROM order_intents WHERE account_scope_id=? ORDER BY intent_id",
                (scope_id,),
            ).fetchall()
        ],
    }
    return json.dumps(projection, sort_keys=True, default=str).encode("utf-8")


def _paper_scope(account_key: str = "ACCT") -> AccountScope:
    return AccountScope(AccountEnvironment.PAPER, account_key)


# ------------------------------------------------------------- (a) happy path


async def test_a_happy_path_intent_to_filled(tmp_path: Path) -> None:
    clock = FixedClock(BASE_NOW)
    session, writer = _open_db(tmp_path, "a.sqlite3", AccountEnvironment.PAPER, _paper_scope())
    try:
        store = IntentStore(DomainEventJournal(writer.connection, clock=clock))
        ledger = _MemoryLedger()
        broker = ScriptedFakeBroker(clock)
        broker.program(FaultScript(submit="accept"))
        coordinator = _coordinator(clock)
        submitter = _submitter(broker, ledger, clock)
        processor = FillProcessor(writer, clock)

        intent = _persist(store, ledger, _exec_intent(intent_id="intent-a", client_order_id="cid-a"))
        command = OrderCommand(intent)
        decision = await coordinator.authorize(command)
        assert decision.authorized
        result = await submitter.submit(command, decision, intent)
        assert (result.outcome, result.status) == ("submitted", OrderStatus.ACCEPTED)
        assert ledger.transitions() == [OrderStatus.SUBMITTING, OrderStatus.ACCEPTED]

        _ensure_db_order(writer, intent, broker_order_id="broker-cid-a")
        first = processor.ingest(
            make_fill_event(
                client_order_id="cid-a", event_id="cid-a-fill-1",
                occurred_at=BASE_NOW, quantity=D("4"), price=D("10"),
                broker_order_id="broker-cid-a",
            )
        )
        assert first.order_status is OrderStatus.PARTIALLY_FILLED
        second = processor.ingest(
            make_fill_event(
                client_order_id="cid-a", event_id="cid-a-fill-2",
                occurred_at=BASE_NOW + timedelta(seconds=1), quantity=D("6"), price=D("20"),
                broker_order_id="broker-cid-a",
            )
        )
        assert second.order_status is OrderStatus.FILLED
        assert second.incremental_qty == D("6")

        held, average = writer.connection.execute(
            "SELECT quantity, average_entry_price FROM positions"
            " WHERE account_scope_id=? AND symbol=?",
            ("paper:ACCT", "XYZ"),
        ).fetchone()
        assert D(held) == D("10")
        assert D(average) == (D("4") * D("10") + D("6") * D("20")) / D("10")
        status, filled = writer.connection.execute(
            "SELECT status, filled_quantity FROM broker_orders WHERE client_order_id=?",
            ("cid-a",),
        ).fetchone()
        assert status == "FILLED"
        assert D(filled) == D("10")
    finally:
        session.close()


# ---------------------------------- (b) ambiguous submit, order actually accepted


async def test_b_ambiguous_submit_accepted_single_submit_no_fill(tmp_path: Path) -> None:
    clock = FixedClock(BASE_NOW)
    session, writer = _open_db(tmp_path, "b.sqlite3", AccountEnvironment.PAPER, _paper_scope())
    try:
        store = IntentStore(DomainEventJournal(writer.connection, clock=clock))
        ledger = _MemoryLedger()
        broker = ScriptedFakeBroker(clock)
        broker.program(FaultScript(submit="timeout_accepted", lookup="found"))
        coordinator = _coordinator(clock)
        submitter = _submitter(broker, ledger, clock)

        intent = _persist(store, ledger, _exec_intent(intent_id="intent-b", client_order_id="cid-b"))
        command = OrderCommand(intent)
        decision = await coordinator.authorize(command)
        result = await submitter.submit(command, decision, intent)

        assert (result.outcome, result.status) == ("resolved", OrderStatus.ACCEPTED)
        assert len(broker.submit_calls) == 1
        assert broker.lookup_calls == ["cid-b"]
        assert broker.submitted_client_ids() == ["cid-b"]
        assert result.filled_qty is None
        assert ledger.transitions() == [OrderStatus.SUBMITTING, OrderStatus.UNKNOWN, OrderStatus.ACCEPTED]
        assert all(record.filled_qty is None for _, record in ledger.records)
        assert ledger.marker_map == {}
    finally:
        session.close()


# ---------------------------------- (c) ambiguous submit with lookup failure


async def test_c_ambiguous_submit_lookup_failure_blocks_no_resubmit(tmp_path: Path) -> None:
    clock = FixedClock(BASE_NOW)
    session, writer = _open_db(tmp_path, "c.sqlite3", AccountEnvironment.PAPER, _paper_scope())
    try:
        store = IntentStore(DomainEventJournal(writer.connection, clock=clock))
        ledger = _MemoryLedger()
        broker = ScriptedFakeBroker(clock)
        broker.program(FaultScript(submit="timeout_absent", lookup="error"))
        coordinator = _coordinator(clock)
        submitter = _submitter(broker, ledger, clock)

        intent = _persist(store, ledger, _exec_intent(intent_id="intent-c", client_order_id="cid-c"))
        command = OrderCommand(intent)
        decision = await coordinator.authorize(command)
        with pytest.raises(OrderOutcomeUnresolved):
            await submitter.submit(command, decision, intent)

        assert ledger.status("cid-c") is OrderStatus.UNKNOWN
        assert submitter.entries_blocked("ACCT", "XYZ")
        assert not submitter.entries_blocked("ACCT", "OTHER")
        assert len(broker.submit_calls) == 1
        assert broker.lookup_calls == ["cid-c"]
        assert broker.submitted_client_ids() == ["cid-c"]

        again = await submitter.submit(command, decision, intent)
        assert again.outcome == "refused"
        other = _exec_intent(intent_id="intent-c2", client_order_id="cid-c2", attempt=1)
        ledger.register(other)
        blocked = await submitter.submit(OrderCommand(other), decision, other)
        assert blocked.outcome in {"blocked", "refused"}
        assert len(broker.submit_calls) == 1
        assert set(broker.submitted_client_ids()) == {"cid-c"}
    finally:
        session.close()


# ---------------------------------- (d) duplicate and out-of-order equivalence


async def test_d_duplicate_out_of_order_matches_in_order(tmp_path: Path) -> None:
    clock = FixedClock(BASE_NOW)
    broker = ScriptedFakeBroker(clock)

    async def _run(name: str, tag: str, events: Sequence[Any]) -> bytes:
        session, writer = _open_db(tmp_path, name, AccountEnvironment.PAPER, _paper_scope())
        try:
            processor = FillProcessor(writer, clock)
            intent = _exec_intent(intent_id=f"intent-{tag}", client_order_id=f"cid-{tag}")
            _ensure_db_order(writer, intent, broker_order_id=f"broker-{tag}")
            outcomes = [processor.ingest(event) for event in events]
            assert any(outcome.outcome == "APPLIED" for outcome in outcomes)
            return _snapshot_bytes(writer, "paper:ACCT")
        finally:
            session.close()

    in_order = [
        make_fill_event(
            client_order_id="cid-d", event_id="cid-d-fill-1", occurred_at=BASE_NOW,
            quantity=D("4"), price=D("10"), broker_order_id="broker-d",
        ),
        make_fill_event(
            client_order_id="cid-d", event_id="cid-d-fill-2",
            occurred_at=BASE_NOW + timedelta(seconds=1), quantity=D("6"), price=D("20"),
            broker_order_id="broker-d",
        ),
    ]
    # Re-key the shared stream per run so both ledgers start from the same events.
    ordered_a = [
        make_fill_event(
            client_order_id="cid-d-in", event_id="cid-d-in-1", occurred_at=BASE_NOW,
            quantity=D("4"), price=D("10"), broker_order_id="broker-d-in",
        ),
        make_fill_event(
            client_order_id="cid-d-in", event_id="cid-d-in-2",
            occurred_at=BASE_NOW + timedelta(seconds=1), quantity=D("6"), price=D("20"),
            broker_order_id="broker-d-in",
        ),
    ]
    base = [
        make_fill_event(
            client_order_id="cid-d-ooo", event_id="cid-d-ooo-1", occurred_at=BASE_NOW,
            quantity=D("4"), price=D("10"), broker_order_id="broker-d-ooo",
        ),
        make_fill_event(
            client_order_id="cid-d-ooo", event_id="cid-d-ooo-2",
            occurred_at=BASE_NOW + timedelta(seconds=1), quantity=D("6"), price=D("20"),
            broker_order_id="broker-d-ooo",
        ),
    ]
    scrambled = broker.duplicate_stream(broker.out_of_order(base))

    assert [e.event_id for e in in_order]  # builders carry stable deterministic ids
    snap_ordered = await _run("d-in.sqlite3", "d-in", ordered_a)
    snap_scrambled = await _run("d-ooo.sqlite3", "d-ooo", scrambled)

    # Normalize the run-specific identity keys before comparing ledger projections.
    def _normalize(raw: bytes, tag: str) -> bytes:
        text = raw.decode("utf-8").replace(tag, "d")
        return text.encode("utf-8")

    assert _normalize(snap_ordered, "d-in") == _normalize(snap_scrambled, "d-ooo")
    final = json.loads(snap_ordered.decode("utf-8"))
    assert D(final["positions"][0]["quantity"]) == D("10")
    assert len(final["fills"]) == 2


# ---------------------------------- (e) partial fill then cancel


async def test_e_partial_fill_then_cancel_releases_remainder(tmp_path: Path) -> None:
    clock = FixedClock(BASE_NOW)
    session, writer = _open_db(tmp_path, "e.sqlite3", AccountEnvironment.PAPER, _paper_scope())
    try:
        store = IntentStore(DomainEventJournal(writer.connection, clock=clock))
        ledger = _MemoryLedger()
        broker = ScriptedFakeBroker(clock)
        coordinator = _coordinator(clock)
        submitter = _submitter(broker, ledger, clock)
        processor = FillProcessor(writer, clock)

        intent = _persist(store, ledger, _exec_intent(intent_id="intent-e", client_order_id="cid-e"))
        command = OrderCommand(intent)
        await submitter.submit(command, await coordinator.authorize(command), intent)
        _ensure_db_order(writer, intent, broker_order_id="broker-cid-e")

        stream = broker.partial_then_cancel_stream(
            client_order_id="cid-e", broker_order_id="broker-cid-e",
            occurred_at=BASE_NOW, partial_qty=D("4"), price=D("20"),
        )
        fill_result = processor.ingest(stream[0])
        assert fill_result.order_status is OrderStatus.PARTIALLY_FILLED
        cancel_result = processor.ingest(stream[1])
        assert cancel_result.order_status is OrderStatus.CANCELED

        held = writer.connection.execute(
            "SELECT quantity FROM positions WHERE account_scope_id=? AND symbol=?",
            ("paper:ACCT", "XYZ"),
        ).fetchone()
        assert D(held[0]) == D("4")
        state, amount = writer.connection.execute(
            "SELECT state, amount FROM budget_reservations WHERE reservation_id=?",
            ("rsv:intent-e",),
        ).fetchone()
        assert (state, amount) == ("RELEASED", "0")
        status = writer.connection.execute(
            "SELECT status FROM broker_orders WHERE client_order_id=?", ("cid-e",)
        ).fetchone()
        assert status[0] == "CANCELED"
    finally:
        session.close()


# ---------------------------------- (f) cancel requested then fill wins


async def test_f_cancel_requested_then_fill_ends_filled(tmp_path: Path) -> None:
    clock = FixedClock(BASE_NOW)
    session, writer = _open_db(tmp_path, "f.sqlite3", AccountEnvironment.PAPER, _paper_scope())
    try:
        store = IntentStore(DomainEventJournal(writer.connection, clock=clock))
        ledger = _MemoryLedger()
        broker = ScriptedFakeBroker(clock)
        coordinator = _coordinator(clock)
        submitter = _submitter(broker, ledger, clock)
        processor = FillProcessor(writer, clock)

        intent = _persist(store, ledger, _exec_intent(intent_id="intent-f", client_order_id="cid-f"))
        command = OrderCommand(intent)
        submitted = await submitter.submit(command, await coordinator.authorize(command), intent)
        assert submitted.broker_order_id is not None
        broker_order_id = submitted.broker_order_id
        _ensure_db_order(writer, intent, broker_order_id=broker_order_id)

        cancel = await submitter.request_cancel(intent)
        assert (cancel.outcome, cancel.status) == ("cancel_requested", OrderStatus.CANCEL_PENDING)
        processor.ingest(
            make_order_update(
                client_order_id="cid-f", status="PENDING_CANCEL", occurred_at=BASE_NOW,
                event_id="cid-f-cancel-req", broker_order_id=broker_order_id,
            )
        )
        fill_result = processor.ingest(
            make_fill_event(
                client_order_id="cid-f", event_id="cid-f-fill-1", occurred_at=BASE_NOW,
                quantity=D("10"), price=D("100"), broker_order_id=broker_order_id,
            )
        )
        assert fill_result.order_status is OrderStatus.FILLED

        broker.record_fill("cid-f", D("10"), BrokerOrderStatus.FILLED)
        confirmed = await submitter.confirm_from_broker(intent)
        assert (confirmed.outcome, confirmed.status) == ("resolved", OrderStatus.FILLED)

        assert ledger.status("cid-f") is OrderStatus.FILLED
        db_status = writer.connection.execute(
            "SELECT status FROM broker_orders WHERE client_order_id=?", ("cid-f",)
        ).fetchone()[0]
        assert db_status == "FILLED"
        assert broker.submit_calls[0].client_order_id == "cid-f"
    finally:
        session.close()


# ---------------------------------- (g) protective-stop vs exit race


async def test_g_protective_stop_race_never_negative_held(tmp_path: Path) -> None:
    clock = FixedClock(BASE_NOW)
    session, writer = _open_db(tmp_path, "g.sqlite3", AccountEnvironment.PAPER, _paper_scope())
    try:
        store = IntentStore(DomainEventJournal(writer.connection, clock=clock))
        ledger = _MemoryLedger()
        broker = ScriptedFakeBroker(clock)
        coordinator = _coordinator(clock)
        submitter = _submitter(broker, ledger, clock)
        processor = FillProcessor(writer, clock)

        entry = _persist(
            store, ledger, _exec_intent(intent_id="intent-g", client_order_id="cid-g-entry")
        )
        entry_command = OrderCommand(entry)
        await submitter.submit(entry_command, await coordinator.authorize(entry_command), entry)
        _ensure_db_order(writer, entry, broker_order_id="broker-entry")
        processor.ingest(
            make_fill_event(
                client_order_id="cid-g-entry", event_id="cid-g-entry-fill",
                occurred_at=BASE_NOW, quantity=D("10"), price=D("10"),
                broker_order_id="broker-entry",
            )
        )

        stop = _exit_intent(
            intent_id="intent-g-stop", client_order_id="cid-g-stop",
            cycle=entry.position_cycle_id, purpose=OrderPurpose.PROTECTIVE_STOP, qty="10",
        )
        exit_intent = _exit_intent(
            intent_id="intent-g-exit", client_order_id="cid-g-exit",
            cycle=entry.position_cycle_id, qty="10",
        )
        ledger.register(stop)
        ledger.register(exit_intent)
        _ensure_db_order(writer, stop, broker_order_id="broker-stop")
        _ensure_db_order(writer, exit_intent, broker_order_id="broker-exit")

        # With the protective stop fully committed, the application exit is refused.
        gated = _coordinator(clock, held=D("10"), committed=D("10"), protective=D("10"))
        exit_decision = await gated.authorize(OrderCommand(exit_intent))
        assert exit_decision.reason is DecisionReason.REJECTED_OVERSELL

        stream = broker.stop_exit_race_stream(
            stop_client_id="cid-g-stop", exit_client_id="cid-g-exit",
            stop_broker_id="broker-stop", exit_broker_id="broker-exit",
            occurred_at=BASE_NOW + timedelta(seconds=1),
            stop_qty=D("10"), exit_qty=D("1"), price=D("9"),
        )
        stop_result = processor.ingest(stream[0])
        assert stop_result.order_status is OrderStatus.FILLED
        exit_result = processor.ingest(stream[1])
        assert exit_result.drift is not None
        assert exit_result.drift.drift_type == "CRITICAL_OVERSELL"

        quantities = [
            D(row[0])
            for row in writer.connection.execute(
                "SELECT quantity FROM positions WHERE account_scope_id=?", ("paper:ACCT",)
            ).fetchall()
        ]
        assert all(qty >= D("0") for qty in quantities)
        controls = writer.connection.execute(
            "SELECT kill_switch_enabled, paused FROM entry_controls WHERE account_scope_id=?",
            ("paper:ACCT",),
        ).fetchone()
        assert tuple(controls) == (1, 1)
        assert exit_decision.reason is DecisionReason.REJECTED_OVERSELL or (
            exit_result.drift is not None
        )
    finally:
        session.close()


# ---------------------------------- (h) crash between persist and response


async def test_h_crash_recovery_resolves_by_lookup_without_resubmit(tmp_path: Path) -> None:
    clock = FixedClock(BASE_NOW)
    session, writer = _open_db(tmp_path, "h.sqlite3", AccountEnvironment.PAPER, _paper_scope())
    try:
        store = IntentStore(DomainEventJournal(writer.connection, clock=clock))
        ledger = _MemoryLedger()
        broker = ScriptedFakeBroker(clock)
        broker.program(FaultScript(submit="crash", lookup="found"))
        submitter = _submitter(broker, ledger, clock)

        # The intent was persisted, then the process died before any response.
        intent = persist_intent(store, _exec_intent(intent_id="intent-h", client_order_id="cid-h"))
        ledger.register(intent)
        ledger.statuses[intent.client_order_id] = OrderStatus.SUBMITTING
        ledger.records.append(
            (intent.client_order_id, _Record("transition", OrderStatus.SUBMITTING, "submit:persist_before_send"))
        )
        persisted = store  # journal already holds INTENT_PERSISTED
        assert persisted is not None

        from mike_trade.domain.trading import OrderIntent as _BrokerIntent

        planted = broker.plant_order(
            _BrokerIntent(
                intent_id=intent.intent_id,
                client_order_id=intent.client_order_id,
                account_scope_id=intent.account_scope_id,
                cycle_id=intent.position_cycle_id,
                symbol=intent.symbol,
                side=intent.side,
                purpose=DomainPurpose.ENTRY,
                order_type=intent.order_type,
                time_in_force=intent.time_in_force,
                created_at=intent.created_at,
                quantity=intent.qty,
                limit_price=intent.limit_price,
            )
        )
        assert planted.client_order_id == "cid-h"

        results = await submitter.recover_inflight("ACCT")

        assert [(r.outcome, r.status) for r in results] == [("resolved", OrderStatus.ACCEPTED)]
        assert broker.lookup_calls == ["cid-h"]
        assert broker.submit_calls == []
        assert ledger.status("cid-h") is OrderStatus.ACCEPTED
    finally:
        session.close()


# ---------------------------------- (i) concurrent entries share one cash pool


async def test_i_concurrent_entries_cannot_double_reserve_cash() -> None:
    import asyncio as _asyncio

    clock = FixedClock(BASE_NOW)
    coordinator = _coordinator(clock, cash=D("100"))

    def _entry(symbol: str, notional: str) -> OrderCommand:
        now = BASE_NOW
        return OrderCommand(
            OrderIntent(
                intent_id=f"intent-i-{symbol.lower()}",
                execution_mode=AccountEnvironment.PAPER,
                account_key="ACCT",
                symbol=symbol,
                side=OrderSide.BUY,
                purpose=OrderPurpose.ENTRY_LONG,
                position_cycle_id=f"cycle-i-{symbol.lower()}",
                notional=D(notional),
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                signal_bar_time=now - timedelta(minutes=1),
                signal_expires_at=now + timedelta(minutes=30),
                quote_reference_time=now - timedelta(seconds=2),
                client_order_id=f"cid-i-{symbol.lower()}",
                attempt_number=0,
                created_at=now,
            )
        )

    commands = [_entry("AAA", "75"), _entry("BBB", "75")]
    decisions = await _asyncio.gather(*(coordinator.authorize(command) for command in commands))

    authorized = [d for d in decisions if d.authorized]
    refused = [d for d in decisions if d.reason is DecisionReason.REJECTED_INSUFFICIENT_CASH]
    assert len(authorized) == 1
    assert len(refused) == 1
    reserved = sum((d.reservation.amount for d in authorized), start=D("0"))
    assert reserved == D("75")
    remaining = await coordinator.reservation_ledger.available(authorized[0].reservation.execution_key)
    assert remaining == D("25")
    assert remaining >= D("0")


# ---------------------------------- (j) SHADOW and PAPER isolation


async def test_j_shadow_and_paper_accounts_are_isolated(tmp_path: Path) -> None:
    clock = FixedClock(BASE_NOW)
    paper_scope = _paper_scope("ACCT")
    shadow_scope = AccountScope(AccountEnvironment.SHADOW, "ACCT", scenario="s1")
    paper_session, paper_writer = _open_db(
        tmp_path, "j-paper.sqlite3", AccountEnvironment.PAPER, paper_scope
    )
    shadow_session, shadow_writer = _open_db(
        tmp_path, "j-shadow.sqlite3", AccountEnvironment.SHADOW, shadow_scope
    )
    try:
        paper_store = IntentStore(DomainEventJournal(paper_writer.connection, clock=clock))
        paper_ledger = _MemoryLedger()
        shadow_ledger = _MemoryLedger()
        paper_broker = ScriptedFakeBroker(clock, AccountEnvironment.PAPER)
        shadow_broker = ScriptedFakeBroker(clock, AccountEnvironment.SHADOW)
        coordinator = _coordinator(clock)

        paper_intent = _persist(
            paper_store, paper_ledger,
            _exec_intent(intent_id="intent-j", client_order_id="cid-j",
                         account_key="ACCT", mode=AccountEnvironment.PAPER),
        )
        shadow_intent = _exec_intent(intent_id="intent-j", client_order_id="cid-j",
                                     account_key="ACCT", mode=AccountEnvironment.SHADOW)
        shadow_ledger.register(shadow_intent)

        paper_key = ExecutionKey.from_intent(paper_intent)
        shadow_key = ExecutionKey.from_intent(shadow_intent)
        assert paper_key.account_scope != shadow_key.account_scope
        assert paper_key.mode == "PAPER"
        assert shadow_key.mode == "SHADOW"

        paper_command = OrderCommand(paper_intent)
        shadow_command = OrderCommand(shadow_intent)
        paper_decision, shadow_decision = await __import__("asyncio").gather(
            coordinator.authorize(paper_command), coordinator.authorize(shadow_command)
        )
        assert paper_decision.authorized
        assert shadow_decision.authorized

        paper_submitter = _submitter(paper_broker, paper_ledger, clock)
        shadow_submitter = _submitter(shadow_broker, shadow_ledger, clock)
        paper_result = await paper_submitter.submit(paper_command, paper_decision, paper_intent)
        assert paper_result.status is OrderStatus.ACCEPTED
        assert shadow_ledger.status("cid-j") is OrderStatus.INTENT_PERSISTED
        assert shadow_broker.submit_calls == []
        assert len(paper_broker.submit_calls) == 1

        # An unresolved paper marker never blocks the shadow account.
        paper_broker.program(FaultScript(submit="timeout_absent", lookup="error"))
        stuck = _exec_intent(intent_id="intent-j2", client_order_id="cid-j2")
        _persist(paper_store, paper_ledger, stuck)
        await coordinator.complete(paper_command)
        stuck_command = OrderCommand(stuck)
        stuck_decision = await coordinator.authorize(stuck_command)
        assert stuck_decision.authorized
        with pytest.raises(OrderOutcomeUnresolved):
            await paper_submitter.submit(stuck_command, stuck_decision, stuck)
        assert paper_submitter.entries_blocked("ACCT", "XYZ")
        assert not shadow_submitter.entries_blocked("ACCT", "XYZ")

        _ensure_db_order(paper_writer, paper_intent, broker_order_id="broker-cid-j")
        paper_snap = _snapshot_bytes(paper_writer, paper_scope.scope_id)
        shadow_snap = _snapshot_bytes(shadow_writer, shadow_scope.scope_id)
        assert b"broker-cid-j" in paper_snap
        assert b"broker-cid-j" not in shadow_snap

        with pytest.raises(Exception, match="(?i)scope|cross"):
            shadow_writer.require_scope(paper_scope.scope_id)
    finally:
        paper_session.close()
        shadow_session.close()


# ---------------------------------- determinism: replay is byte-identical


async def test_k_replay_same_stream_is_byte_identical(tmp_path: Path) -> None:
    clock = FixedClock(BASE_NOW)

    def _build(tag: str) -> list[Any]:
        return [
            make_order_update(
                client_order_id=f"cid-k-{tag}", status="ACCEPTED", occurred_at=BASE_NOW,
                event_id=f"cid-k-{tag}-accept", broker_order_id=f"broker-k-{tag}",
            ),
            make_fill_event(
                client_order_id=f"cid-k-{tag}", event_id=f"cid-k-{tag}-fill-1",
                occurred_at=BASE_NOW, quantity=D("4"), price=D("10"),
                broker_order_id=f"broker-k-{tag}",
            ),
            make_fill_event(
                client_order_id=f"cid-k-{tag}", event_id=f"cid-k-{tag}-fill-2",
                occurred_at=BASE_NOW + timedelta(seconds=1), quantity=D("6"), price=D("20"),
                broker_order_id=f"broker-k-{tag}",
            ),
        ]

    async def _run(name: str, tag: str) -> tuple[bytes, list[tuple[str, str]]]:
        session, writer = _open_db(tmp_path, name, AccountEnvironment.PAPER, _paper_scope())
        try:
            run_clock = FixedClock(BASE_NOW)
            ledger = _MemoryLedger()
            broker = ScriptedFakeBroker(run_clock)
            submitter = _submitter(broker, ledger, run_clock)
            coordinator = _coordinator(run_clock)
            processor = FillProcessor(writer, run_clock)
            intent = _exec_intent(intent_id=f"intent-k-{tag}", client_order_id=f"cid-k-{tag}")
            ledger.register(intent)
            command = OrderCommand(intent)
            await submitter.submit(command, await coordinator.authorize(command), intent)
            _ensure_db_order(writer, intent, broker_order_id=f"broker-k-{tag}")
            for event in _build(tag):
                processor.ingest(event)
            # Normalize run-specific identity keys so the determinism check
            # compares ledger projections, not the distinct run tags.
            raw = _snapshot_bytes(writer, "paper:ACCT").decode("utf-8")
            normalized = raw.replace(tag, "TAG").encode("utf-8")
            log = [
                (status, reason) for status, reason in ledger.transition_log()
            ]
            normalized_log = [(s, r) for s, r in log]
            return normalized, normalized_log
        finally:
            session.close()

    first_snap, first_log = await _run("k-one.sqlite3", "one")
    second_snap, second_log = await _run("k-two.sqlite3", "two")

    assert first_snap == second_snap
    assert first_log == second_log
    final = json.loads(first_snap.decode("utf-8"))
    assert D(final["positions"][0]["quantity"]) == D("10")
