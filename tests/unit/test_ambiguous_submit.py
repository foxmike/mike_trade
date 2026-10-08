from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mike_trade.domain.accounts import AccountEnvironment
from mike_trade.domain.errors import LiveTradingNotAuthorizedError, PersistenceError
from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    OrderSide,
    OrderType,
    TimeInForce,
)
from mike_trade.domain.trading import OrderIntent as BrokerOrderIntent
from mike_trade.execution.coordinator import (
    CommandDecision,
    DecisionReason,
    ExecutionKey,
    OrderCommand,
)
from mike_trade.execution.intents import OrderIntent, OrderPurpose
from mike_trade.execution.order_fsm import OrderStatus, TransitionResult
from mike_trade.execution.submission import (
    OrderOutcomeUnresolved,
    OrderSubmitter,
    StaleSubmission,
    UnresolvedOrderMarker,
)

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
D = Decimal
S = OrderStatus


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


def make_intent(**overrides: object) -> OrderIntent:
    values: dict[str, object] = {
        "intent_id": "intent-1",
        "execution_mode": AccountEnvironment.PAPER,
        "account_key": "account-1",
        "symbol": "AAPL",
        "side": OrderSide.BUY,
        "purpose": OrderPurpose.ENTRY_LONG,
        "position_cycle_id": "cycle-1",
        "qty": D("2"),
        "order_type": OrderType.LIMIT,
        "limit_price": D("190.25"),
        "time_in_force": TimeInForce.DAY,
        "signal_bar_time": NOW - timedelta(minutes=1),
        "signal_expires_at": NOW + timedelta(minutes=4),
        "quote_reference_time": NOW - timedelta(seconds=2),
        "client_order_id": "stable-client-id",
        "attempt_number": 0,
        "created_at": NOW,
    }
    values.update(overrides)
    return OrderIntent(**values)  # type: ignore[arg-type]


def authorize(intent: OrderIntent) -> tuple[OrderCommand, CommandDecision]:
    command = OrderCommand(intent)
    decision = CommandDecision(
        DecisionReason.AUTHORIZED, ExecutionKey.from_intent(intent), command.purpose
    )
    return command, decision


def broker_order(
    client_order_id: str,
    status: BrokerOrderStatus,
    *,
    filled: str = "0",
    broker_order_id: str = "broker-1",
) -> BrokerOrder:
    filled_qty = D(filled)
    return BrokerOrder(
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
        account_scope_id="paper:account-1",
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        status=status,
        quantity=D("2"),
        filled_quantity=filled_qty,
        submitted_at=NOW,
        updated_at=NOW,
        filled_avg_price=D("190") if filled_qty > 0 else None,
        limit_price=D("190.25"),
    )


@dataclass
class Record:
    kind: str
    status: OrderStatus | None = None
    reason: str = ""
    filled_qty: Decimal | None = None
    broker_order_id: str | None = None


@dataclass
class FakeLedger:
    """In-memory append-only intent store implementing SubmissionLedger."""

    intents: dict[str, OrderIntent] = field(default_factory=dict)
    statuses: dict[str, OrderStatus] = field(default_factory=dict)
    records: list[tuple[str, Record]] = field(default_factory=list)
    broker_ids: dict[str, str] = field(default_factory=dict)
    marker_map: dict[str, UnresolvedOrderMarker] = field(default_factory=dict)
    fail_on: set[OrderStatus] = field(default_factory=set)

    def register(self, intent: OrderIntent, status: OrderStatus = S.INTENT_PERSISTED) -> None:
        self.intents[intent.client_order_id] = intent
        self.statuses[intent.client_order_id] = status

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
        if result.status in self.fail_on:
            raise PersistenceError("simulated persist failure")
        cid = intent.client_order_id
        assert self.statuses[cid] is result.previous_status
        self.statuses[cid] = result.status
        if broker_order_id is not None:
            self.broker_ids[cid] = broker_order_id
        self.records.append(
            (
                cid,
                Record(
                    "transition", result.status, reason, result.filled_qty, broker_order_id
                ),
            )
        )

    def record_rearm(self, intent: OrderIntent, *, reason: str) -> None:
        self.statuses[intent.client_order_id] = S.INTENT_PERSISTED
        self.records.append((intent.client_order_id, Record("rearm", S.INTENT_PERSISTED, reason)))

    def annotate(self, intent: OrderIntent, kind: str, detail: str) -> None:
        self.records.append((intent.client_order_id, Record(kind, reason=detail)))

    def broker_order_id(self, client_order_id: str) -> str | None:
        return self.broker_ids.get(client_order_id)

    def inflight(self, account_key: str) -> list[OrderIntent]:
        return [
            intent
            for cid, intent in self.intents.items()
            if intent.account_key == account_key
            and self.statuses[cid] in {S.INTENT_PERSISTED, S.SUBMITTING, S.UNKNOWN}
        ]

    def put_marker(self, marker: UnresolvedOrderMarker) -> None:
        self.marker_map[marker.client_order_id] = marker

    def clear_marker(self, client_order_id: str) -> None:
        self.marker_map.pop(client_order_id, None)

    def markers(self, account_key: str) -> list[UnresolvedOrderMarker]:
        return [m for m in self.marker_map.values() if m.account_key == account_key]

    def kinds(self) -> list[str]:
        return [record.kind for _, record in self.records]

    def transitions(self) -> list[OrderStatus]:
        return [r.status for _, r in self.records if r.kind == "transition" and r.status]


class FakeBroker:
    """Scriptable in-memory BrokerPort subset; never touches a network."""

    def __init__(self, ledger: FakeLedger | None = None) -> None:
        self.environment = AccountEnvironment.PAPER
        self.ledger = ledger
        self.orders: dict[str, BrokerOrder] = {}
        self.submit_calls: list[BrokerOrderIntent] = []
        self.lookup_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.replace_calls: list[tuple[str, dict[str, Decimal | None]]] = []
        self.submit_error: BaseException | None = None
        self.store_on_submit_error = False
        self.submit_status = BrokerOrderStatus.ACCEPTED
        self.lookup_error: BaseException | None = None
        self.status_at_submit: OrderStatus | None = None

    async def submit_order(self, intent: BrokerOrderIntent) -> BrokerOrder:
        if self.ledger is not None:
            self.status_at_submit = self.ledger.status(intent.client_order_id)
        self.submit_calls.append(intent)
        order = broker_order(intent.client_order_id, self.submit_status)
        if self.submit_error is not None:
            if self.store_on_submit_error:
                self.orders[intent.client_order_id] = order
            raise self.submit_error
        self.orders[intent.client_order_id] = order
        return order

    async def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        self.lookup_calls.append(client_order_id)
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.orders.get(client_order_id)

    async def cancel_order(self, broker_order_id: str) -> None:
        self.cancel_calls.append(broker_order_id)

    async def replace_order(
        self,
        broker_order_id: str,
        *,
        stop_price: Decimal | None = None,
        trail: Decimal | None = None,
        quantity: Decimal | None = None,
    ) -> BrokerOrder:
        self.replace_calls.append(
            (broker_order_id, {"stop_price": stop_price, "trail": trail, "quantity": quantity})
        )
        return broker_order(
            "replacement-client-id", BrokerOrderStatus.ACCEPTED, broker_order_id="broker-2"
        )

    def set_status(self, client_order_id: str, status: BrokerOrderStatus, **kw: str) -> None:
        self.orders[client_order_id] = broker_order(client_order_id, status, **kw)


@dataclass
class Rig:
    intent: OrderIntent
    ledger: FakeLedger
    broker: FakeBroker
    clock: FakeClock
    make: Callable[..., OrderSubmitter]

    @property
    def submitter(self) -> OrderSubmitter:
        return self.make()

    @property
    def cid(self) -> str:
        return self.intent.client_order_id


def build_rig(
    intent: OrderIntent | None = None,
    *,
    register: bool = True,
    quote_fresh: bool = True,
    replace_proven: bool = False,
    live_authorized: bool = False,
) -> Rig:
    intent = intent or make_intent()
    ledger = FakeLedger()
    if register:
        ledger.register(intent)
    broker = FakeBroker(ledger)
    clock = FakeClock()
    quote = {"fresh": quote_fresh}

    def make(**overrides: object) -> OrderSubmitter:
        params: dict[str, object] = {
            "broker": broker,
            "ledger": ledger,
            "clock": clock,
            "quote_checker": lambda _intent, _now: quote["fresh"],
            "replace_capability": lambda _name: replace_proven,
            "live_authorized": live_authorized,
        }
        params.update(overrides)
        return OrderSubmitter(**params)  # type: ignore[arg-type]

    rig = Rig(intent, ledger, broker, clock, make)
    rig.quote = quote  # type: ignore[attr-defined]
    return rig


# --------------------------------------------------------------- happy path / ordering


async def test_persists_submitting_before_broker_call_and_applies_response() -> None:
    rig = build_rig()
    command, decision = authorize(rig.intent)

    result = await rig.submitter.submit(command, decision, rig.intent)

    assert rig.broker.status_at_submit is S.SUBMITTING
    assert [c.client_order_id for c in rig.broker.submit_calls] == ["stable-client-id"]
    assert rig.ledger.transitions() == [S.SUBMITTING, S.ACCEPTED]
    assert (result.outcome, result.status, result.filled_qty) == ("submitted", S.ACCEPTED, None)
    assert rig.ledger.broker_ids[rig.cid] == "broker-1"


async def test_no_broker_call_when_submitting_persist_fails() -> None:
    rig = build_rig()
    rig.ledger.fail_on.add(S.SUBMITTING)
    command, decision = authorize(rig.intent)

    with pytest.raises(PersistenceError):
        await rig.submitter.submit(command, decision, rig.intent)

    assert rig.broker.submit_calls == []
    assert rig.broker.lookup_calls == []
    assert rig.ledger.status(rig.cid) is S.INTENT_PERSISTED


async def test_refuses_intent_not_in_intent_persisted() -> None:
    for status in (None, S.SUBMITTING, S.UNKNOWN, S.ACCEPTED):
        rig = build_rig(register=False)
        if status is not None:
            rig.ledger.register(rig.intent, status)
        command, decision = authorize(rig.intent)

        result = await rig.submitter.submit(command, decision, rig.intent)

        assert result.outcome == "refused"
        assert rig.broker.submit_calls == []


async def test_second_submit_of_same_intent_does_not_reach_broker() -> None:
    rig = build_rig()
    command, decision = authorize(rig.intent)

    await rig.submitter.submit(command, decision, rig.intent)
    again = await rig.submitter.submit(command, decision, rig.intent)

    assert again.outcome == "refused"
    assert len(rig.broker.submit_calls) == 1


async def test_unauthorized_decision_is_rejected_before_any_persist() -> None:
    rig = build_rig()
    command, _ = authorize(rig.intent)
    denied = CommandDecision(
        DecisionReason.REJECTED_OPEN_ORDER, ExecutionKey.from_intent(rig.intent), command.purpose
    )

    with pytest.raises(ValueError):
        await rig.submitter.submit(command, denied, rig.intent)

    assert rig.ledger.records == []
    assert rig.broker.submit_calls == []


# ----------------------------------------------------------------------- staleness


async def test_stale_signal_blocks_submit_with_typed_local_rejection() -> None:
    rig = build_rig()
    rig.clock.current = rig.intent.signal_expires_at
    command, decision = authorize(rig.intent)

    result = await rig.submitter.submit(command, decision, rig.intent)

    assert result.outcome == "rejected_local"
    assert result.stale is StaleSubmission.SIGNAL_EXPIRED
    assert result.status is S.REJECTED
    assert rig.broker.submit_calls == []
    assert rig.ledger.transitions() == [S.SUBMITTING, S.REJECTED]


async def test_stale_quote_blocks_submit_with_typed_local_rejection() -> None:
    rig = build_rig(quote_fresh=False)
    command, decision = authorize(rig.intent)

    result = await rig.submitter.submit(command, decision, rig.intent)

    assert (result.outcome, result.stale) == ("rejected_local", StaleSubmission.QUOTE_STALE)
    assert rig.broker.submit_calls == []
    assert rig.ledger.status(rig.cid) is S.REJECTED


async def test_quote_checker_failure_fails_closed() -> None:
    rig = build_rig()

    def boom(_intent: OrderIntent, _now: datetime) -> bool:
        raise RuntimeError("quote feed down")

    command, decision = authorize(rig.intent)
    result = await rig.make(quote_checker=boom).submit(command, decision, rig.intent)

    assert result.stale is StaleSubmission.QUOTE_STALE
    assert rig.broker.submit_calls == []


# ------------------------------------------------------------------ unknown outcome


async def test_timeout_then_lookup_found_accepted_one_submit_no_fill() -> None:
    rig = build_rig()
    rig.broker.submit_error = TimeoutError("response lost")
    rig.broker.store_on_submit_error = True
    command, decision = authorize(rig.intent)

    result = await rig.submitter.submit(command, decision, rig.intent)

    assert len(rig.broker.submit_calls) == 1
    assert rig.broker.lookup_calls == [rig.cid]
    assert rig.ledger.transitions() == [S.SUBMITTING, S.UNKNOWN, S.ACCEPTED]
    assert (result.outcome, result.status, result.filled_qty) == ("resolved", S.ACCEPTED, None)
    assert all(r.filled_qty is None for _, r in rig.ledger.records)
    assert rig.ledger.marker_map == {}


async def test_timeout_then_lookup_reports_fill_only_from_broker_data() -> None:
    rig = build_rig()
    rig.broker.submit_error = ConnectionError("reset")
    command, decision = authorize(rig.intent)
    rig.broker.set_status(rig.cid, BrokerOrderStatus.FILLED, filled="2")

    result = await rig.submitter.submit(command, decision, rig.intent)

    assert result.status is S.FILLED
    assert result.filled_qty == D("2")
    assert rig.ledger.transitions() == [S.SUBMITTING, S.UNKNOWN, S.FILLED]


async def test_timeout_then_not_found_retries_only_same_client_order_id() -> None:
    rig = build_rig()
    rig.broker.submit_error = TimeoutError("never arrived")
    command, decision = authorize(rig.intent)

    first = await rig.submitter.submit(command, decision, rig.intent)

    assert first.outcome == "not_found"
    assert rig.ledger.status(rig.cid) is S.UNKNOWN
    assert rig.submitter.entries_blocked("account-1", "AAPL")
    # Not-found alone never re-enables submit.
    blind = await rig.submitter.submit(command, decision, rig.intent)
    assert blind.outcome == "refused"
    assert len(rig.broker.submit_calls) == 1

    rearmed = await rig.submitter.resolve_not_found(rig.intent, retry=True)
    assert rearmed.outcome == "rearmed"
    assert rig.ledger.status(rig.cid) is S.INTENT_PERSISTED
    assert not rig.submitter.entries_blocked("account-1", "AAPL")

    rig.broker.submit_error = None
    retried = await rig.submitter.submit(command, decision, rig.intent)

    assert retried.status is S.ACCEPTED
    assert [c.client_order_id for c in rig.broker.submit_calls] == [rig.cid, rig.cid]
    assert {c.client_order_id for c in rig.broker.submit_calls} == {"stable-client-id"}


async def test_resolve_not_found_uses_fresh_lookup_and_adopts_late_order() -> None:
    rig = build_rig()
    rig.broker.submit_error = TimeoutError("slow")
    command, decision = authorize(rig.intent)
    await rig.submitter.submit(command, decision, rig.intent)
    rig.broker.set_status(rig.cid, BrokerOrderStatus.ACCEPTED)  # appears late

    result = await rig.submitter.resolve_not_found(rig.intent, retry=True)

    assert (result.outcome, result.status) == ("resolved", S.ACCEPTED)
    assert len(rig.broker.submit_calls) == 1


async def test_resolve_not_found_without_retry_abandons_terminally() -> None:
    rig = build_rig()
    rig.broker.submit_error = TimeoutError("never arrived")
    command, decision = authorize(rig.intent)
    await rig.submitter.submit(command, decision, rig.intent)

    result = await rig.submitter.resolve_not_found(rig.intent, retry=False)

    assert result.status is S.REJECTED
    assert not rig.submitter.entries_blocked("account-1", "AAPL")


async def test_timeout_then_lookup_failure_stays_unknown_and_blocks_entries() -> None:
    rig = build_rig()
    rig.broker.submit_error = TimeoutError("lost")
    rig.broker.lookup_error = ConnectionError("lookup down")
    command, decision = authorize(rig.intent)

    with pytest.raises(OrderOutcomeUnresolved) as raised:
        await rig.submitter.submit(command, decision, rig.intent)

    assert raised.value.client_order_id == rig.cid
    assert raised.value.marker is not None
    assert rig.ledger.status(rig.cid) is S.UNKNOWN
    assert rig.submitter.entries_blocked("account-1", "AAPL")
    assert not rig.submitter.entries_blocked("account-1", "MSFT")
    assert not rig.submitter.entries_blocked("account-2", "AAPL")

    # A new entry intent for the same account+symbol is blocked; no extra submit.
    other = make_intent(intent_id="intent-2", client_order_id="other-client-id", attempt_number=1)
    rig.ledger.register(other)
    other_cmd, other_decision = authorize(other)
    blocked = await rig.submitter.submit(other_cmd, other_decision, other)
    again = await rig.submitter.submit(command, decision, rig.intent)

    assert blocked.outcome == "blocked"
    assert again.outcome == "refused"
    assert len(rig.broker.submit_calls) == 1
    assert {c.client_order_id for c in rig.broker.submit_calls} == {"stable-client-id"}
    assert rig.ledger.status(other.client_order_id) is S.INTENT_PERSISTED


async def test_explicit_rejection_is_terminal_without_lookup() -> None:
    from mike_trade.domain.errors import BrokerRejectedError

    rig = build_rig()
    rig.broker.submit_error = BrokerRejectedError("insufficient")
    command, decision = authorize(rig.intent)

    result = await rig.submitter.submit(command, decision, rig.intent)

    assert (result.outcome, result.status) == ("rejected", S.REJECTED)
    assert rig.broker.lookup_calls == []


# ------------------------------------------------------------------ crash recovery


async def test_recover_submitting_resolves_by_client_order_id_without_resubmit() -> None:
    rig = build_rig(register=False)
    rig.ledger.register(rig.intent, S.SUBMITTING)
    rig.broker.set_status(rig.cid, BrokerOrderStatus.ACCEPTED)

    results = await rig.submitter.recover_inflight("account-1")

    assert [(r.outcome, r.status) for r in results] == [("resolved", S.ACCEPTED)]
    assert rig.broker.lookup_calls == [rig.cid]
    assert rig.broker.submit_calls == []


async def test_recover_submitting_not_found_stays_unknown_and_blocks() -> None:
    rig = build_rig(register=False)
    rig.ledger.register(rig.intent, S.SUBMITTING)

    results = await rig.submitter.recover_inflight("account-1")

    assert [(r.outcome, r.status) for r in results] == [("not_found", S.UNKNOWN)]
    assert rig.submitter.entries_blocked("account-1", "AAPL")
    assert rig.broker.submit_calls == []


async def test_recover_unknown_with_lookup_failure_reports_unresolved() -> None:
    rig = build_rig(register=False)
    rig.ledger.register(rig.intent, S.UNKNOWN)
    rig.broker.lookup_error = TimeoutError("down")

    results = await rig.submitter.recover_inflight("account-1")

    assert [(r.outcome, r.status) for r in results] == [("unconfirmed", S.UNKNOWN)]
    assert rig.submitter.entries_blocked("account-1", "AAPL")
    assert rig.broker.submit_calls == []


async def test_recover_intent_persisted_is_never_auto_submitted() -> None:
    rig = build_rig()

    results = await rig.submitter.recover_inflight("account-1")

    assert [(r.outcome, r.status) for r in results] == [("needs_decision", S.INTENT_PERSISTED)]
    assert "NEEDS_DECISION" in rig.ledger.kinds()
    assert rig.broker.submit_calls == []
    assert rig.ledger.status(rig.cid) is S.INTENT_PERSISTED


async def test_recover_intent_persisted_expires_when_signal_stale() -> None:
    rig = build_rig()
    rig.clock.current = rig.intent.signal_expires_at + timedelta(seconds=1)

    results = await rig.submitter.recover_inflight("account-1")

    assert [(r.outcome, r.stale) for r in results] == [
        ("rejected_local", StaleSubmission.SIGNAL_EXPIRED)
    ]
    assert rig.ledger.status(rig.cid) is S.REJECTED
    assert rig.broker.submit_calls == []


async def test_recover_intent_persisted_found_at_broker_adopts_its_state() -> None:
    rig = build_rig()
    rig.broker.set_status(rig.cid, BrokerOrderStatus.ACCEPTED)

    results = await rig.submitter.recover_inflight("account-1")

    assert [(r.outcome, r.status) for r in results] == [("resolved", S.ACCEPTED)]
    assert rig.ledger.transitions() == [S.SUBMITTING, S.ACCEPTED]
    assert rig.broker.submit_calls == []


# ------------------------------------------------------------------ cancel / replace


async def _accepted_rig(**kwargs: object) -> Rig:
    rig = build_rig(**kwargs)  # type: ignore[arg-type]
    command, decision = authorize(rig.intent)
    await rig.submitter.submit(command, decision, rig.intent)
    return rig


async def test_cancel_request_stays_cancel_pending_until_broker_confirms() -> None:
    rig = await _accepted_rig()

    result = await rig.submitter.request_cancel(rig.intent)

    assert (result.outcome, result.status) == ("cancel_requested", S.CANCEL_PENDING)
    assert rig.broker.cancel_calls == ["broker-1"]
    assert rig.ledger.status(rig.cid) is S.CANCEL_PENDING

    # Broker still reports an open order: nothing is inferred.
    still_open = await rig.submitter.confirm_from_broker(rig.intent)
    assert still_open.status is S.CANCEL_PENDING

    rig.broker.set_status(rig.cid, BrokerOrderStatus.CANCELED)
    confirmed = await rig.submitter.confirm_from_broker(rig.intent)
    assert confirmed.status is S.CANCELED
    assert rig.ledger.status(rig.cid) is S.CANCELED


async def test_cancel_call_failure_keeps_cancel_pending() -> None:
    rig = await _accepted_rig()

    async def failing(_broker_order_id: str) -> None:
        raise TimeoutError("cancel lost")

    rig.broker.cancel_order = failing  # type: ignore[method-assign]
    result = await rig.submitter.request_cancel(rig.intent)

    assert result.status is S.CANCEL_PENDING
    assert "CANCEL_REQUEST_ERROR" in rig.ledger.kinds()


async def test_cancel_refused_without_open_order() -> None:
    rig = build_rig()

    result = await rig.submitter.request_cancel(rig.intent)

    assert result.outcome == "refused"
    assert rig.broker.cancel_calls == []


async def test_replace_refused_when_capability_unproven() -> None:
    rig = await _accepted_rig(replace_proven=False)

    result = await rig.submitter.request_replace(rig.intent, stop_price=D("180"))

    assert result.outcome == "refused"
    assert rig.broker.replace_calls == []
    assert rig.ledger.status(rig.cid) is S.ACCEPTED


async def test_replace_fails_closed_when_capability_check_raises_or_missing() -> None:
    rig = await _accepted_rig()

    def raising(_name: str) -> bool:
        raise RuntimeError("matrix unreadable")

    for submitter in (rig.make(replace_capability=raising), rig.make(replace_capability=None)):
        result = await submitter.request_replace(rig.intent, stop_price=D("180"))
        assert result.outcome == "refused"
    assert rig.broker.replace_calls == []


async def test_replace_applies_replaced_only_after_confirmed_lookup() -> None:
    rig = await _accepted_rig(replace_proven=True)

    result = await rig.submitter.request_replace(rig.intent, stop_price=D("180"))

    assert result.outcome == "replace_requested"
    assert rig.broker.replace_calls == [
        ("broker-1", {"stop_price": D("180"), "trail": None, "quantity": None})
    ]
    assert rig.ledger.status(rig.cid) is S.ACCEPTED
    assert "REPLACE_RESPONSE" in rig.ledger.kinds()

    unconfirmed = await rig.submitter.confirm_from_broker(rig.intent)
    assert unconfirmed.status is S.ACCEPTED

    rig.broker.set_status(rig.cid, BrokerOrderStatus.REPLACED)
    confirmed = await rig.submitter.confirm_from_broker(rig.intent)
    assert confirmed.status is S.REPLACED
    assert rig.ledger.status(rig.cid) is S.REPLACED


# --------------------------------------------------------------------- live safety


async def test_live_submit_refused_without_authorization() -> None:
    intent = make_intent(execution_mode=AccountEnvironment.LIVE)
    rig = build_rig(intent)
    command, decision = authorize(intent)

    with pytest.raises(LiveTradingNotAuthorizedError):
        await rig.submitter.submit(command, decision, intent)

    assert rig.broker.submit_calls == []
    assert rig.ledger.records == []
    assert rig.ledger.status(rig.cid) is S.INTENT_PERSISTED


async def test_live_cancel_and_replace_refused_without_authorization() -> None:
    intent = make_intent(execution_mode=AccountEnvironment.LIVE)
    rig = build_rig(intent, replace_proven=True)

    with pytest.raises(LiveTradingNotAuthorizedError):
        await rig.submitter.request_cancel(intent)
    with pytest.raises(LiveTradingNotAuthorizedError):
        await rig.submitter.request_replace(intent, stop_price=D("1"))

    assert rig.broker.cancel_calls == [] and rig.broker.replace_calls == []


async def test_live_broker_environment_alone_requires_authorization() -> None:
    rig = build_rig()
    rig.broker.environment = AccountEnvironment.LIVE
    command, decision = authorize(rig.intent)

    with pytest.raises(LiveTradingNotAuthorizedError):
        await rig.submitter.submit(command, decision, rig.intent)

    assert rig.broker.submit_calls == []


async def test_live_submit_allowed_with_explicit_authorization() -> None:
    intent = make_intent(execution_mode=AccountEnvironment.LIVE)
    rig = build_rig(intent, live_authorized=True)
    command, decision = authorize(intent)

    result = await rig.submitter.submit(command, decision, intent)

    assert result.status is S.ACCEPTED
    assert len(rig.broker.submit_calls) == 1
