from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mike_trade.risk.stop_state import (
    InMemoryProtectionStateStorage,
    ProtectionCondition,
    ProtectionStateRecord,
    ProtectionStateRepository,
    apply_event,
    entry_block_reasons,
    to_dashboard_view,
)


NOW = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)


def record(**changes):
    values = dict(
        cycle_id="cycle-1",
        account_id="account-1",
        scenario_id=None,
        symbol="AAPL",
        mode="FIXED",
        execution_mode="PAPER",
        protected_qty=Decimal("10"),
        remaining_position_qty=Decimal("10"),
        stop_price=Decimal("95"),
        condition=ProtectionCondition.PENDING,
    )
    values.update(changes)
    return ProtectionStateRecord(**values)


def event(kind, **values):
    return {"type": kind, "occurred_at": NOW, **values}


def test_all_conditions_reachable_through_legal_events():
    pending = record()
    pending, _ = apply_event(pending, event("placement_requested", event_id="place"))
    assert pending.condition is ProtectionCondition.PENDING
    active, _ = apply_event(pending, event("broker_ack", broker_order_id="b1", broker_update_id="ack"))
    assert active.condition is ProtectionCondition.ACTIVE
    updating, _ = apply_event(active, event("replace_requested", stop_price=Decimal("96")))
    assert updating.condition is ProtectionCondition.UPDATING
    unconfirmed, _ = apply_event(updating, event("broker_unreadable"))
    assert unconfirmed.condition is ProtectionCondition.UNCONFIRMED
    active_again, _ = apply_event(unconfirmed, event("replace_confirmed"))
    assert active_again.condition is ProtectionCondition.ACTIVE
    triggered, _ = apply_event(active_again, event("trigger", filled_qty=Decimal("10")))
    assert triggered.condition is ProtectionCondition.TRIGGERED

    expired, audit = apply_event(record(tif="DAY"), event("expiry"))
    assert expired.condition is ProtectionCondition.EXPIRED
    assert expired.reason == "PROTECTION_EXPIRED/OVERNIGHT"
    assert audit[0].reason == "PROTECTION_EXPIRED/OVERNIGHT"
    failed, _ = apply_event(record(), event("placement_failed"))
    assert failed.condition is ProtectionCondition.UNPROTECTED
    offline, _ = apply_event(record(), event("engine_offline"))
    assert offline.condition is ProtectionCondition.OFFLINE


def test_illegal_terminal_reactivation_and_long_stop_decrease_rejected():
    active, _ = apply_event(record(), event("broker_ack"))
    triggered, _ = apply_event(active, event("trigger"))
    with pytest.raises(ValueError, match="illegal transition"):
        apply_event(triggered, event("broker_ack", broker_update_id="illegal-reactivation"))
    active, _ = apply_event(record(), event("broker_ack"))
    with pytest.raises(ValueError, match="may not decrease"):
        apply_event(active, event("replace_requested", stop_price=Decimal("94")))


def test_done_for_day_expiry_and_unprotected_entry_blocks():
    expired, _ = apply_event(record(), event("done_for_day"))
    assert expired.condition is ProtectionCondition.EXPIRED
    assert expired.reason == "PROTECTION_EXPIRED/OVERNIGHT"
    unprotected, _ = apply_event(record(), event("coverage_failed"))
    assert unprotected.reason == "UNPROTECTED_POSITION"
    assert entry_block_reasons([unprotected]) == ["UNPROTECTED_POSITION"]
    assert entry_block_reasons([expired]) == ["PROTECTION_EXPIRED/OVERNIGHT"]


def test_pending_blocks_only_after_grace_period_and_unconfirmed_blocks():
    pending, _ = apply_event(record(), event("placement_requested"))
    later = NOW + timedelta(seconds=31)
    assert entry_block_reasons([pending], now=later, pending_grace_period=timedelta(seconds=30)) == ["PROTECTION_PENDING"]
    assert entry_block_reasons([pending], now=NOW + timedelta(seconds=10), pending_grace_period=timedelta(seconds=30)) == []
    unconfirmed, _ = apply_event(record(), event("broker_unreadable"))
    assert entry_block_reasons([unconfirmed]) == ["PROTECTION_UNCONFIRMED"]


def test_duplicate_broker_update_is_idempotent_and_audit_is_append_only():
    storage = InMemoryProtectionStateStorage()
    repo = ProtectionStateRepository(storage)
    initial = record()
    update = event("broker_ack", broker_order_id="broker-1", broker_update_id="update-1")
    first = repo.save_event(initial, update)
    audit_count = len(storage.audit_events)
    second = repo.save_event(initial, update)
    assert second is first
    assert second.revision == first.revision
    assert len(storage.audit_events) == audit_count == 1


def test_paper_and_shadow_records_with_same_cycle_are_isolated():
    storage = InMemoryProtectionStateStorage()
    repo = ProtectionStateRepository(storage)
    paper = record(execution_mode="PAPER")
    shadow = record(execution_mode="SHADOW", scenario_id="scenario-1")
    paper_active = repo.save_event(paper, event("broker_ack", broker_order_id="paper-order", broker_update_id="same"))
    shadow_failed = repo.save_event(shadow, event("coverage_failed", broker_update_id="same"))
    assert repo.load(mode="PAPER", account_id="account-1", scenario_id=None, cycle_id="cycle-1") == paper_active
    assert repo.load(mode="SHADOW", account_id="account-1", scenario_id="scenario-1", cycle_id="cycle-1") == shadow_failed
    assert paper_active.condition is ProtectionCondition.ACTIVE
    assert shadow_failed.condition is ProtectionCondition.UNPROTECTED
    assert len(repo.list_records(mode="PAPER")) == 1
    assert len(repo.list_records(mode="SHADOW")) == 1


def test_dashboard_projection_marks_values_stale_and_offline():
    active, _ = apply_event(record(), event("broker_ack", broker_order_id="b-1"))
    view = to_dashboard_view(active, NOW, timedelta(seconds=61), offline_after=timedelta(seconds=60))
    assert view["condition"] is ProtectionCondition.OFFLINE
    assert view["stale"] is True
    assert view["broker_order_id"] == "b-1"
    assert view["stop_price"] == Decimal("95")
