from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mike_trade.domain.protection import ProtectionStatus
from mike_trade.domain.trading import OrderSide
from mike_trade.execution.order_fsm import OrderStatus
from mike_trade.portfolio.cycles import (
    CycleEvent,
    CycleState,
    CycleTransitionError,
    PositionCycle,
    apply_cycle_event,
    deserialize_cycle,
    serialize_cycle,
)

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
D = Decimal


def event(event_type: str, **values: object) -> CycleEvent:
    return CycleEvent(event_type=event_type, occurred_at=NOW, **values)


def fresh() -> PositionCycle:
    return PositionCycle(
        account_or_scenario_id="paper:account-1",
        symbol="aapl",
        cycle_id="cycle-1",
    )


def accepted(cycle: PositionCycle, **values: object) -> PositionCycle:
    updated, audits = apply_cycle_event(
        cycle,
        event(
            CycleEvent.BUY_SIGNAL_ACCEPTED,
            strategy_id="buy-vwap",
            entry_intent_id="intent-1",
            target_entry_qty=D("10"),
            **values,
        ),
    )
    assert len(audits) == 1
    return updated


def pending_buy() -> PositionCycle:
    cycle = accepted(fresh())
    cycle, _ = apply_cycle_event(cycle, event(CycleEvent.BUY_SUBMITTED, order_id="entry-order-1"))
    cycle, _ = apply_cycle_event(cycle, event(CycleEvent.BUY_ACKNOWLEDGED))
    return cycle


def buy_fill(
    cycle: PositionCycle,
    fill_id: str,
    quantity: str,
    *,
    price: str = "100.125",
) -> PositionCycle:
    updated, _ = apply_cycle_event(
        cycle,
        event(
            CycleEvent.FILL_APPLIED,
            fill_id=fill_id,
            order_id="entry-order-1",
            side=OrderSide.BUY,
            quantity=D(quantity),
            price=D(price),
        ),
    )
    return updated


def protected_position(quantity: str = "10") -> PositionCycle:
    cycle = buy_fill(pending_buy(), "buy-fill-1", quantity)
    cycle, _ = apply_cycle_event(
        cycle,
        event(
            CycleEvent.PROTECTION_CONFIRMED,
            order_id="protection-1",
            protection_qty=D(quantity),
        ),
    )
    return cycle


def test_cycle_state_is_the_exact_composite_state_set() -> None:
    assert [state.name for state in CycleState] == [
        "DISABLED",
        "WATCHING_BUY",
        "ENTRY_RESERVED",
        "BUY_SUBMITTING",
        "BUY_PENDING",
        "PARTIALLY_OWNED",
        "PROTECTIVE_PENDING",
        "WATCHING_SELL",
        "SELL_SUBMITTING",
        "SELL_PENDING",
        "CLOSING",
        "RECONCILING",
        "PAUSED",
        "ERROR",
        "UNMANAGED",
        "CLOSED",
    ]


def test_decimal_and_utc_boundaries_are_strict() -> None:
    with pytest.raises(TypeError, match="filled_entry_qty must be Decimal"):
        PositionCycle("scope", "AAPL", "cycle", filled_entry_qty=1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        PositionCycle(
            "scope",
            "AAPL",
            "cycle",
            state=CycleState.PROTECTIVE_PENDING,
            filled_entry_qty=D("1"),
            remaining_qty=D("1"),
            average_entry_price=D("100"),
            first_positive_fill_at=datetime(2026, 10, 8, 14, 30),
            entry_slot_consumed=True,
        )
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        CycleEvent(CycleEvent.OPERATOR_PAUSE, datetime(2026, 10, 8, 14, 30))
    with pytest.raises(ValueError, match="must be UTC"):
        CycleEvent(
            CycleEvent.OPERATOR_PAUSE,
            datetime(2026, 10, 8, 10, 30, tzinfo=timezone(timedelta(hours=-4))),
        )
    with pytest.raises(TypeError, match="quantity must be Decimal"):
        event(
            CycleEvent.FILL_APPLIED,
            fill_id="fill",
            side=OrderSide.BUY,
            quantity=1.0,
            price=D("100"),
        )


def test_second_buy_is_ignored_and_audited_in_every_pending_or_owned_state() -> None:
    pending = pending_buy()
    partial = replace(buy_fill(pending, "fill-1", "2"), state=CycleState.PARTIALLY_OWNED)
    protective = replace(partial, state=CycleState.PROTECTIVE_PENDING)
    watching_sell, _ = apply_cycle_event(
        protective,
        event(
            CycleEvent.PROTECTION_CONFIRMED,
            order_id="protection-1",
            protection_qty=D("2"),
        ),
    )

    for cycle in (pending, partial, protective, watching_sell):
        updated, audits = apply_cycle_event(
            cycle,
            event(CycleEvent.BUY_SIGNAL_ACCEPTED, strategy_id="another-strategy"),
        )
        assert updated is cycle
        assert audits[0]["event_type"] == "BuySignalIgnored"
        assert audits[0]["outcome"] == "ignored"
        assert updated.entry_intent_id == "intent-1"


def test_partial_buy_consumes_one_slot_once_and_requires_protection() -> None:
    first = buy_fill(pending_buy(), "fill-1", "2.50", price="100.10")
    assert first.state is CycleState.PROTECTIVE_PENDING
    assert first.entry_slot_consumed is True
    assert first.first_positive_fill_at == NOW
    assert first.filled_entry_qty == D("2.50")
    assert first.remaining_qty == D("2.50")
    assert first.protection_status is ProtectionStatus.PENDING

    second = buy_fill(first, "fill-2", "1.25", price="100.30")
    assert second.entry_slot_consumed is True
    assert second.first_positive_fill_at == first.first_positive_fill_at
    assert second.filled_entry_qty == D("3.75")
    assert second.version == first.version + 1
    assert second.average_entry_price == (D("2.50") * D("100.10") + D("1.25") * D("100.30")) / D(
        "3.75"
    )


def test_partially_filled_then_canceled_keeps_owned_shares_without_rebuy() -> None:
    partial = buy_fill(pending_buy(), "fill-1", "3")
    canceled, audits = apply_cycle_event(
        partial, event(CycleEvent.ENTRY_CANCELED, reason="broker canceled remainder")
    )
    assert canceled.state is CycleState.PROTECTIVE_PENDING
    assert canceled.entry_order_status is OrderStatus.CANCELED
    assert canceled.filled_entry_qty == canceled.remaining_qty == D("3")
    assert canceled.target_entry_qty == D("10")
    assert len(audits) == 1

    ignored, ignored_audits = apply_cycle_event(
        canceled,
        event(CycleEvent.BUY_SIGNAL_ACCEPTED, strategy_id="buy-vwap"),
    )
    assert ignored is canceled
    assert ignored_audits[0]["outcome"] == "ignored"


def test_duplicate_fill_is_an_identity_noop_without_audit() -> None:
    cycle = pending_buy()
    applied = buy_fill(cycle, "same-fill", "2")
    duplicate, audits = apply_cycle_event(
        applied,
        event(
            CycleEvent.FILL_APPLIED,
            fill_id="same-fill",
            order_id="entry-order-1",
            side=OrderSide.BUY,
            quantity=D("2"),
            price=D("100.125"),
        ),
    )
    assert duplicate is applied
    assert duplicate.remaining_qty == D("2")
    assert duplicate.version == applied.version
    assert audits == ()


def test_oversell_is_rejected_without_mutating_cycle() -> None:
    cycle = protected_position("4")
    with pytest.raises(CycleTransitionError, match="shorting is forbidden"):
        apply_cycle_event(
            cycle,
            event(
                CycleEvent.FILL_APPLIED,
                fill_id="sell-too-much",
                side=OrderSide.SELL,
                quantity=D("4.0001"),
                price=D("105"),
            ),
        )
    assert cycle.remaining_qty == D("4")
    assert cycle.filled_exit_qty == D("0")


def test_partial_exit_reduces_remaining_and_requires_protection_resize() -> None:
    cycle = protected_position("10")
    cycle, _ = apply_cycle_event(cycle, event(CycleEvent.SELL_SIGNAL_ACCEPTED))
    cycle, _ = apply_cycle_event(cycle, event(CycleEvent.SELL_SUBMITTED, order_id="sell-1"))
    partial, _ = apply_cycle_event(
        cycle,
        event(
            CycleEvent.FILL_APPLIED,
            fill_id="sell-fill-1",
            order_id="sell-1",
            side=OrderSide.SELL,
            quantity=D("4"),
            price=D("110"),
        ),
    )
    assert partial.remaining_qty == D("6")
    assert partial.filled_exit_qty == D("4")
    assert partial.state is CycleState.PROTECTIVE_PENDING
    assert partial.protection_status is ProtectionStatus.PENDING
    assert partial.protection_qty == D("10")  # stale coverage until resize is confirmed

    resized, _ = apply_cycle_event(
        partial,
        event(
            CycleEvent.PROTECTION_CONFIRMED,
            order_id="protection-2",
            protection_qty=D("6"),
        ),
    )
    assert resized.protection_qty == D("6")
    assert resized.state is CycleState.SELL_PENDING


def test_illegal_transition_raises_typed_error_and_leaves_state_unchanged() -> None:
    cycle = fresh()
    with pytest.raises(CycleTransitionError) as caught:
        apply_cycle_event(cycle, event(CycleEvent.SELL_SUBMITTED, order_id="sell-1"))
    assert caught.value.cycle is cycle
    assert caught.value.current_state is CycleState.WATCHING_BUY
    assert cycle.version == 0


def test_closed_requires_flat_and_no_open_linked_sell_or_protection() -> None:
    owned = protected_position("10")
    with pytest.raises(CycleTransitionError, match="remaining_qty"):
        apply_cycle_event(owned, event(CycleEvent.CLOSE_CONFIRMED_FLAT))

    selling, _ = apply_cycle_event(owned, event(CycleEvent.SELL_SIGNAL_ACCEPTED))
    selling, _ = apply_cycle_event(selling, event(CycleEvent.SELL_SUBMITTED, order_id="sell-1"))
    flat, _ = apply_cycle_event(
        selling,
        event(
            CycleEvent.FILL_APPLIED,
            fill_id="sell-fill-1",
            order_id="sell-1",
            side=OrderSide.SELL,
            quantity=D("10"),
            price=D("110"),
        ),
    )
    assert flat.state is CycleState.CLOSING
    assert not flat.has_open_sell_order
    assert flat.has_open_protective_order
    with pytest.raises(CycleTransitionError, match="protective order"):
        apply_cycle_event(flat, event(CycleEvent.CLOSE_CONFIRMED_FLAT))

    protection_canceled, _ = apply_cycle_event(
        flat,
        event(
            CycleEvent.PROTECTION_CONFIRMED,
            order_id="protection-1",
            protection_status=ProtectionStatus.CANCELED,
            protection_qty=D("0"),
        ),
    )
    closed, _ = apply_cycle_event(
        protection_canceled,
        event(
            CycleEvent.CLOSE_CONFIRMED_FLAT,
            cooldown_until=NOW + timedelta(minutes=5),
        ),
    )
    assert closed.state is CycleState.CLOSED

    stale_sell = replace(
        protection_canceled,
        sell_order_status=OrderStatus.PENDING,
    )
    with pytest.raises(CycleTransitionError, match="sell order"):
        apply_cycle_event(stale_sell, event(CycleEvent.CLOSE_CONFIRMED_FLAT))


def test_closed_rearms_only_explicitly_after_cooldown() -> None:
    closed = PositionCycle(
        account_or_scenario_id="paper:account-1",
        symbol="AAPL",
        cycle_id="cycle-1",
        state=CycleState.CLOSED,
        cooldown_until=NOW + timedelta(minutes=1),
    )
    with pytest.raises(CycleTransitionError, match="cooldown"):
        apply_cycle_event(closed, event(CycleEvent.REARM))
    rearmed, _ = apply_cycle_event(
        closed,
        CycleEvent(
            CycleEvent.REARM,
            NOW + timedelta(minutes=1),
            new_cycle_id="cycle-2",
        ),
    )
    assert rearmed.state is CycleState.WATCHING_BUY
    assert rearmed.cycle_id == "cycle-2"


def test_pause_blocks_entry_but_processes_fill_sell_protection_and_reconciliation() -> None:
    pending = pending_buy()
    paused, _ = apply_cycle_event(pending, event(CycleEvent.OPERATOR_PAUSE))
    assert paused.state is CycleState.PAUSED
    assert paused.paused_state is CycleState.BUY_PENDING
    for kind in (
        CycleEvent.BUY_SIGNAL_ACCEPTED,
        CycleEvent.ENTRY_RESERVED,
        CycleEvent.BUY_SUBMITTED,
        CycleEvent.BUY_ACKNOWLEDGED,
    ):
        with pytest.raises(CycleTransitionError, match="blocked while paused"):
            apply_cycle_event(paused, event(kind))

    filled, _ = apply_cycle_event(
        paused,
        event(
            CycleEvent.FILL_APPLIED,
            fill_id="fill-while-paused",
            order_id="entry-order-1",
            side=OrderSide.BUY,
            quantity=D("10"),
            price=D("100"),
        ),
    )
    assert filled.state is CycleState.PAUSED
    assert filled.paused_state is CycleState.PROTECTIVE_PENDING
    assert filled.remaining_qty == D("10")

    protected, _ = apply_cycle_event(
        filled,
        event(
            CycleEvent.PROTECTION_CONFIRMED,
            order_id="protection-1",
            protection_qty=D("10"),
        ),
    )
    assert protected.paused_state is CycleState.WATCHING_SELL
    selling, _ = apply_cycle_event(protected, event(CycleEvent.SELL_SIGNAL_ACCEPTED))
    assert selling.paused_state is CycleState.SELL_SUBMITTING

    reconciling, _ = apply_cycle_event(selling, event(CycleEvent.RECONCILIATION_REQUIRED))
    assert reconciling.state is CycleState.PAUSED
    assert reconciling.paused_state is CycleState.RECONCILING
    resolved, _ = apply_cycle_event(reconciling, event(CycleEvent.RECONCILIATION_RESOLVED))
    assert resolved.state is CycleState.PAUSED
    assert resolved.paused_state is CycleState.WATCHING_SELL
    resumed, _ = apply_cycle_event(resolved, event(CycleEvent.OPERATOR_RESUME))
    assert resumed.state is CycleState.WATCHING_SELL
    assert resumed.paused_state is None


def test_snapshot_round_trip_is_equal_and_decimal_exact() -> None:
    cycle = protected_position("3.750")
    paused, _ = apply_cycle_event(cycle, event(CycleEvent.OPERATOR_PAUSE))
    payload = serialize_cycle(paused)
    assert payload["filled_entry_qty"] == "3.750"
    assert payload["average_entry_price"] == "100.125"

    restored = deserialize_cycle(json.loads(json.dumps(payload)))
    assert restored == paused
    assert isinstance(restored.remaining_qty, Decimal)
    assert restored.remaining_qty.as_tuple() == D("3.750").as_tuple()
    assert deserialize_cycle(json.dumps(payload)) == paused
