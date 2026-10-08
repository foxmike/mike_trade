from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from mike_trade.domain.protection import ProtectionStatus
from mike_trade.portfolio.cycles import CycleState, PositionCycle
from mike_trade.portfolio.status import (
    build_account_status,
    build_symbol_status,
    phase_label,
)

NOW = datetime(2026, 10, 8, 14, 30, tzinfo=UTC)
D = Decimal


def cycle(scope: str = "paper:acct", symbol: str = "AAPL", **kwargs) -> PositionCycle:
    return PositionCycle(scope, symbol, f"cycle-{scope}-{symbol}", **kwargs)


def owned_cycle(**kwargs) -> PositionCycle:
    return cycle(
        state=CycleState.PROTECTIVE_PENDING,
        filled_entry_qty=D("3.25"),
        remaining_qty=D("3.25"),
        average_entry_price=D("100.1250"),
        first_positive_fill_at=NOW - timedelta(minutes=5),
        entry_slot_consumed=True,
        **kwargs,
    )


def test_account_aggregator_filters_account_and_mode() -> None:
    cycles = [
        cycle("shadow:acct:scenario-a", "AAPL"),
        cycle("shadow:acct:scenario-b", "MSFT"),
        cycle("paper:acct", "NVDA"),
        cycle("live:acct", "TSLA"),
    ]
    rows = build_account_status(
        cycles,
        account_or_scenario_id="shadow:acct:scenario-a",
        mode="SHADOW",
    )
    assert [(row.account_or_scenario_id, row.mode, row.symbol) for row in rows] == [
        ("shadow:acct:scenario-a", "SHADOW", "AAPL")
    ]


def test_every_cycle_state_has_label_and_raw_enum_is_preserved() -> None:
    for state in CycleState:
        label = phase_label(state)
        assert label
        extra = {}
        if state is CycleState.PAUSED:
            extra["paused_state"] = CycleState.WATCHING_BUY
        elif state is CycleState.RECONCILING:
            extra["reconciliation_state"] = CycleState.WATCHING_BUY
        status = build_symbol_status(cycle(state=state, **extra), mode="PAPER", now=NOW)
        assert status.phase_label == label
        assert status.cycle_state is state
    assert phase_label(CycleState.WATCHING_BUY) == "watching for entry"
    assert phase_label(CycleState.PROTECTIVE_PENDING) == "awaiting protection"


def test_entries_block_for_paused_reconciling_unknown_protection_and_cooldown() -> None:
    paused = cycle(
        state=CycleState.PAUSED,
        paused_state=CycleState.WATCHING_BUY,
    )
    reconciling = cycle(
        state=CycleState.RECONCILING,
        reconciliation_state=CycleState.WATCHING_BUY,
    )
    unknown = owned_cycle(protection_status=ProtectionStatus.UNKNOWN)
    cooling = cycle(cooldown_until=NOW + timedelta(minutes=2))
    assert build_symbol_status(paused, mode="PAPER", now=NOW).block_reasons == ("paused",)
    assert build_symbol_status(reconciling, mode="PAPER", now=NOW).block_reasons == (
        "reconciling",
    )
    assert build_symbol_status(unknown, mode="PAPER", now=NOW).block_reasons == (
        "unknown_stop",
    )
    assert build_symbol_status(cooling, mode="PAPER", now=NOW).block_reasons == ("cooldown",)
    allowed = build_symbol_status(cycle(), mode="PAPER", now=NOW)
    assert allowed.entries_blocked is False
    assert allowed.block_reasons == ()


def test_unmanaged_holding_is_not_counted_as_managed_quantity() -> None:
    unmanaged = cycle(
        state=CycleState.UNMANAGED,
        filled_entry_qty=D("7"),
        remaining_qty=D("7"),
        average_entry_price=D("25"),
        first_positive_fill_at=NOW,
        entry_slot_consumed=True,
    )
    status = build_symbol_status(unmanaged, mode="PAPER", now=NOW)
    assert status.unmanaged is True
    assert status.held_quantity == D("7")
    assert status.remaining_managed_quantity == D("0")


def test_stale_heartbeat_preserves_last_known_position_values() -> None:
    position = owned_cycle(last_transition_at=NOW - timedelta(minutes=2))
    status = build_symbol_status(
        position,
        mode="PAPER",
        heartbeat_at=NOW - timedelta(minutes=3),
        stale_after=timedelta(minutes=1),
        now=NOW,
    )
    assert status.stale is True
    assert status.held_quantity == D("3.25")
    assert status.remaining_managed_quantity == D("3.25")
    assert status.average_entry_price == D("100.1250")


def test_json_serialization_uses_exact_decimal_strings_and_utc_iso_timestamps() -> None:
    status = build_symbol_status(
        owned_cycle(last_transition_at=NOW - timedelta(minutes=5)), mode="PAPER", now=NOW
    )
    payload = status.to_json_dict()
    assert payload["held_quantity"] == "3.25"
    assert payload["average_entry_price"] == "100.1250"
    assert payload["updated_at"] == (NOW - timedelta(minutes=5)).isoformat(timespec="microseconds")
    assert '"held_quantity": "3.25"' in status.to_json()
    assert '"updated_at": "2026-10-08T14:25:00.000000+00:00"' in status.to_json()
