from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.trading import OrderSide, OrderType, TimeInForce
from mike_trade.execution.intents import IntentStore, OrderIntent, OrderPurpose, persist_intent
from mike_trade.execution.order_fsm import (
    TERMINAL_STATUSES,
    IllegalOrderTransition,
    OrderEvent,
    OrderStatus,
    apply_transition,
)
from mike_trade.persistence.journal import DomainEventJournal
from mike_trade.persistence.repositories import AccountScopeRepository
from mike_trade.persistence.schema import initialize_database
from mike_trade.persistence.transaction import WriterSession

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
D = Decimal

LEGAL_EDGES = {
    OrderStatus.INTENT_PERSISTED: {OrderStatus.SUBMITTING},
    OrderStatus.SUBMITTING: {
        OrderStatus.ACCEPTED,
        OrderStatus.PENDING,
        OrderStatus.REJECTED,
        OrderStatus.UNKNOWN,
    },
    OrderStatus.ACCEPTED: {
        OrderStatus.PENDING,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REPLACED,
        OrderStatus.SUSPENDED,
    },
    OrderStatus.PENDING: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REPLACED,
        OrderStatus.SUSPENDED,
    },
    OrderStatus.PARTIALLY_FILLED: {
        OrderStatus.FILLED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REPLACED,
        OrderStatus.SUSPENDED,
    },
    OrderStatus.CANCEL_PENDING: {
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REPLACED,
        OrderStatus.SUSPENDED,
    },
    OrderStatus.SUSPENDED: {
        OrderStatus.ACCEPTED,
        OrderStatus.PENDING,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REPLACED,
    },
    OrderStatus.UNKNOWN: {
        OrderStatus.ACCEPTED,
        OrderStatus.PENDING,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.REJECTED,
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REPLACED,
        OrderStatus.SUSPENDED,
    },
}


def event_for(current: OrderStatus, target: OrderStatus) -> OrderEvent:
    filled = D("1") if target in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED} else None
    return OrderEvent(
        target,
        filled_qty=filled,
        broker_lookup=current is OrderStatus.UNKNOWN,
    )


def make_intent(**overrides: object) -> OrderIntent:
    values: dict[str, object] = {
        "intent_id": "intent-1",
        "execution_mode": AccountEnvironment.PAPER,
        "account_key": "account-1",
        "symbol": "aapl",
        "side": OrderSide.BUY,
        "purpose": OrderPurpose.ENTRY_LONG,
        "position_cycle_id": "cycle-1",
        "originating_strategy_id": "strategy-1",
        "qty": D("2.5"),
        "notional": None,
        "order_type": OrderType.LIMIT,
        "limit_price": D("190.25"),
        "stop_price": None,
        "trail_price": None,
        "trail_percent": None,
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


def test_order_status_has_exact_contract_members() -> None:
    assert [status.name for status in OrderStatus] == [
        "INTENT_PERSISTED",
        "SUBMITTING",
        "ACCEPTED",
        "PENDING",
        "PARTIALLY_FILLED",
        "FILLED",
        "REJECTED",
        "CANCEL_PENDING",
        "CANCELED",
        "EXPIRED",
        "REPLACED",
        "SUSPENDED",
        "UNKNOWN",
    ]


@pytest.mark.parametrize(
    ("current", "target"),
    [(current, target) for current, targets in LEGAL_EDGES.items() for target in targets],
)
def test_every_legal_edge_is_accepted(current: OrderStatus, target: OrderStatus) -> None:
    result = apply_transition(current, event_for(current, target))

    assert result.accepted
    assert not result.ignored
    assert not result.illegal
    assert result.previous_status is current
    assert result.status is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OrderStatus.INTENT_PERSISTED, OrderStatus.ACCEPTED),
        (OrderStatus.INTENT_PERSISTED, OrderStatus.FILLED),
        (OrderStatus.SUBMITTING, OrderStatus.CANCEL_PENDING),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.REJECTED),
    ],
)
def test_representative_illegal_edges_return_typed_error(
    current: OrderStatus, target: OrderStatus
) -> None:
    result = apply_transition(current, event_for(current, target))

    assert result.illegal
    assert result.status is current
    assert isinstance(result.error, IllegalOrderTransition)
    with pytest.raises(IllegalOrderTransition):
        result.raise_for_illegal()


@pytest.mark.parametrize("terminal", TERMINAL_STATUSES)
def test_terminal_states_are_immutable_but_same_state_is_idempotent(
    terminal: OrderStatus,
) -> None:
    same = apply_transition(terminal, OrderEvent(terminal))
    assert same.ignored
    assert same.status is terminal

    other = OrderStatus.REJECTED if terminal is not OrderStatus.REJECTED else OrderStatus.CANCELED
    changed = apply_transition(terminal, event_for(terminal, other))
    assert changed.illegal
    assert changed.status is terminal


@pytest.mark.parametrize(
    ("current", "stale"),
    [
        (OrderStatus.PENDING, OrderStatus.ACCEPTED),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING),
        (OrderStatus.CANCEL_PENDING, OrderStatus.PARTIALLY_FILLED),
        (OrderStatus.CANCEL_PENDING, OrderStatus.ACCEPTED),
    ],
)
def test_stale_lower_rank_updates_are_ignored(
    current: OrderStatus, stale: OrderStatus
) -> None:
    # CANCEL_PENDING -> PARTIALLY_FILLED is a legal race only when it carries a fill.
    result = apply_transition(current, OrderEvent(stale))
    assert result.ignored
    assert result.status is current


@pytest.mark.parametrize("fill_status", [OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED])
def test_cancel_pending_can_lose_race_to_fill(fill_status: OrderStatus) -> None:
    result = apply_transition(
        OrderStatus.CANCEL_PENDING,
        OrderEvent(fill_status, filled_qty=D("0.25")),
    )
    assert result.accepted
    assert result.status is fill_status
    assert result.filled_qty == D("0.25")


def test_unknown_requires_broker_lookup_to_resolve() -> None:
    unverified = apply_transition(OrderStatus.UNKNOWN, OrderEvent(OrderStatus.ACCEPTED))
    assert unverified.illegal

    verified = apply_transition(
        OrderStatus.UNKNOWN,
        OrderEvent(OrderStatus.ACCEPTED, broker_lookup=True),
    )
    assert verified.accepted


def test_status_only_updates_never_carry_filled_quantity() -> None:
    accepted = apply_transition(OrderStatus.SUBMITTING, OrderEvent(OrderStatus.ACCEPTED))
    pending = apply_transition(OrderStatus.ACCEPTED, OrderEvent(OrderStatus.PENDING))
    assert accepted.filled_qty is None
    assert pending.filled_qty is None
    with pytest.raises(ValueError, match="only through fill-bearing"):
        OrderEvent(OrderStatus.ACCEPTED, filled_qty=D("1"))
    status_only_fill = apply_transition(OrderStatus.PENDING, OrderEvent(OrderStatus.FILLED))
    assert status_only_fill.accepted
    assert status_only_fill.filled_qty is None


def test_cancel_and_replace_require_final_broker_confirmation() -> None:
    requested = apply_transition(OrderStatus.ACCEPTED, OrderEvent(OrderStatus.CANCEL_PENDING))
    assert requested.accepted
    assert requested.status is OrderStatus.CANCEL_PENDING
    assert requested.status is not OrderStatus.CANCELED

    canceled = apply_transition(
        OrderStatus.CANCEL_PENDING,
        OrderEvent(OrderStatus.CANCELED, broker_confirmed=False),
    )
    replaced = apply_transition(
        OrderStatus.PENDING,
        OrderEvent(OrderStatus.REPLACED, broker_confirmed=False),
    )
    assert canceled.illegal
    assert replaced.illegal


def test_intent_is_immutable_normalized_and_can_derive_stable_client_id() -> None:
    intent = make_intent()
    assert intent.symbol == "AAPL"
    assert intent.account_scope_id == "paper:account-1"
    with pytest.raises((AttributeError, TypeError)):
        intent.symbol = "MSFT"  # type: ignore[misc]

    values = {field: getattr(intent, field) for field in intent.__dataclass_fields__}
    values.pop("client_order_id")
    first = OrderIntent.create(**values)
    second = OrderIntent.create(**values)
    assert first.client_order_id == second.client_order_id


@pytest.mark.parametrize(
    "overrides",
    [
        {"qty": D("0")},
        {"qty": D("-1")},
        {"qty": None, "notional": None},
        {"qty": D("1"), "notional": D("100")},
        {"side": OrderSide.SELL, "purpose": OrderPurpose.ENTRY_LONG},
        {"side": OrderSide.BUY, "purpose": OrderPurpose.EXIT_LONG},
    ],
)
def test_intent_rejects_invalid_sizing_and_non_long_semantics(
    overrides: dict[str, object]
) -> None:
    with pytest.raises((TypeError, ValueError)):
        make_intent(**overrides)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("qty", 1.0),
        ("notional", 100.0),
        ("limit_price", 190.25),
        ("stop_price", 180.0),
        ("trail_price", 2.0),
        ("trail_percent", 1.5),
    ],
)
def test_intent_rejects_float_sizing_and_prices(field: str, value: float) -> None:
    overrides: dict[str, object] = {field: value}
    if field == "notional":
        overrides["qty"] = None
    elif field == "stop_price":
        overrides.update(order_type=OrderType.STOP, limit_price=None)
    elif field in {"trail_price", "trail_percent"}:
        overrides.update(order_type=OrderType.TRAILING_STOP, limit_price=None)
    with pytest.raises(TypeError, match="Decimal"):
        make_intent(**overrides)


@pytest.mark.parametrize(
    "field",
    ["signal_bar_time", "signal_expires_at", "quote_reference_time", "created_at"],
)
def test_intent_requires_utc_timestamps(field: str) -> None:
    with pytest.raises(ValueError, match="UTC"):
        make_intent(**{field: NOW.replace(tzinfo=None)})


def test_persist_happens_before_submission_and_duplicate_client_id_is_idempotent(
    tmp_path,
) -> None:
    path = tmp_path / "paper.sqlite3"
    initialize_database(path, AccountEnvironment.PAPER, now=NOW).close()

    with WriterSession(path) as session:
        writer = session.bind_scope("paper:account-1")
        AccountScopeRepository(writer, lambda: NOW).register_scope(
            AccountScope(AccountEnvironment.PAPER, "account-1")
        )
        journal = DomainEventJournal(writer.connection, clock=lambda: NOW)
        store = IntentStore(journal)
        intent = make_intent()
        hook_observations: list[tuple[int, str, bool]] = []

        def submit_hook(persisted: OrderIntent) -> None:
            events = journal.read_stream(
                persisted.account_scope_id, "order_intent", persisted.intent_id
            )
            hook_observations.append(
                (len(events), events[0].payload["status"], writer.connection.in_transaction)
            )

        first = persist_intent(store, intent, submission_hook=submit_hook)
        duplicate_input = replace(intent, intent_id="intent-2")
        duplicate = persist_intent(
            store,
            duplicate_input,
            submission_hook=lambda _: pytest.fail("duplicate must not submit again"),
        )

        assert first == intent
        assert duplicate == intent
        assert hook_observations == [(1, "INTENT_PERSISTED", False)]
        events = journal.read_stream(intent.account_scope_id, "order_intent", intent.intent_id)
        assert len(events) == 1
        assert events[0].payload["trade_affecting"] is True
