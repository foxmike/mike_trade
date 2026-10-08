from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mike_trade.domain.trading import TimeInForce
from mike_trade.risk.trailing_state import (
    TrailingEventType,
    TrailingStateRecord,
    TrailingStateStore,
    TrailingStatus,
)

D = Decimal
UTC = timezone.utc


def _record(**changes):
    fields = dict(
        cycle_id="cycle-1",
        entry_reference=D("100.000"),
        high_watermark=D("103.250"),
        ratchet_anchor=D("103.00"),
        candidate_stop=D("101.00"),
        last_confirmed_stop=D("99.00"),
        protected_quantity=D("12.500"),
        broker_order_id="stop-1",
        broker_status="ACCEPTED",
        policy_revision="stepped-v3",
        time_in_force=TimeInForce.GTC,
        last_updated_at=datetime(2026, 4, 3, 14, 15, 16, 123456, tzinfo=UTC),
    )
    fields.update(changes)
    return TrailingStateRecord(**fields)


def _create(store, **changes):
    return store.create(_record(**changes), idempotency_key="created", occurred_at=_record().last_updated_at)


def test_candidate_and_intent_do_not_change_confirmed_stop(tmp_path):
    store = TrailingStateStore(tmp_path / "trailing.db")
    _create(store)
    before = store.load("cycle-1").last_confirmed_stop

    store.record_candidate("cycle-1", high_watermark=D("104"), ratchet_anchor=D("104"),
                           candidate_stop=D("102"), policy_revision="stepped-v3", idempotency_key="ratchet")
    store.record_replacement_intent("cycle-1", intent_id="intent-1", candidate_stop=D("102"), idempotency_key="intent-1")
    assert store.load("cycle-1").last_confirmed_stop == before

    event = store.confirm_replacement("cycle-1", price=D("102.00"), broker_order_id="stop-2",
                                      idempotency_key="broker-confirm-1")
    assert event.event_type is TrailingEventType.REPLACE_CONFIRMED
    assert store.load("cycle-1").last_confirmed_stop == D("102.00")
    store.close()


def test_lower_confirmation_is_drift_and_does_not_lower_stop(tmp_path):
    store = TrailingStateStore(tmp_path / "trailing.db")
    _create(store)
    event = store.confirm_replacement("cycle-1", price=D("98.99"), broker_order_id="stale-stop",
                                      idempotency_key="broker-stale-confirm")
    state = store.load("cycle-1")
    assert event.event_type is TrailingEventType.RECONCILED
    assert event.reconciliation_drift is True
    assert event.new_values["drift"] == "CONFIRMED_STOP_LOWER_THAN_CURRENT"
    assert state.last_confirmed_stop == D("99.00")
    assert state.broker_order_id == "stop-1"
    assert state.status is TrailingStatus.TRAIL_UPDATE_UNCONFIRMED
    with pytest.raises(RuntimeError, match="requires reconciliation"):
        store.record_candidate("cycle-1", high_watermark=D("104"), ratchet_anchor=D("104"),
                               candidate_stop=D("102"), policy_revision="stepped-v3", idempotency_key="blocked-by-drift")
    store.close()


def test_second_replacement_intent_returns_existing_intent(tmp_path):
    store = TrailingStateStore(tmp_path / "trailing.db")
    _create(store)
    first = store.record_replacement_intent("cycle-1", intent_id="intent-1", candidate_stop=D("100"), idempotency_key="intent-first")
    second = store.record_replacement_intent("cycle-1", intent_id="intent-2", candidate_stop=D("101"), idempotency_key="intent-second")
    assert first.event_id == second.event_id
    assert second.new_values["pending_intent_id"] == "intent-1"
    assert sum(e.event_type is TrailingEventType.REPLACE_INTENT_RECORDED for e in store.history("cycle-1")) == 1
    assert store.load("cycle-1").pending_intent_id == "intent-1"
    store.close()


def test_broker_update_replay_is_noop_for_history_and_state(tmp_path):
    store = TrailingStateStore(tmp_path / "trailing.db")
    _create(store)
    store.record_replacement_intent("cycle-1", intent_id="intent-1", candidate_stop=D("100"), idempotency_key="intent")
    when = datetime(2026, 4, 3, 15, tzinfo=UTC)
    store.confirm_replacement("cycle-1", price=D("100"), broker_order_id="stop-2", broker_status="NEW",
                              idempotency_key="update-123", occurred_at=when)
    count = len(store.history("cycle-1"))
    state = store.load("cycle-1")
    replay = store.confirm_replacement("cycle-1", price=D("100"), broker_order_id="stop-2", broker_status="NEW",
                                       idempotency_key="update-123", occurred_at=when)
    assert len(store.history("cycle-1")) == count
    assert replay.event_id == store.history("cycle-1")[-1].event_id
    assert store.load("cycle-1") == state
    store.close()


def test_restart_round_trips_decimal_and_utc_fields_and_pending_intent(tmp_path):
    path = tmp_path / "trailing.db"
    instant = datetime(2026, 4, 3, 11, 15, 16, 654321, tzinfo=UTC)
    store = TrailingStateStore(path)
    _create(store, last_updated_at=instant, pending_intent_id="replace-7",
            pending_since=instant - timedelta(seconds=3), status=TrailingStatus.PENDING_REPLACE)
    store.close()

    reopened = TrailingStateStore(path)
    restored = reopened.load("cycle-1")
    assert restored == _record(last_updated_at=instant, pending_intent_id="replace-7",
                               pending_since=instant - timedelta(seconds=3), status=TrailingStatus.PENDING_REPLACE)
    assert restored.entry_reference.as_tuple() == D("100.000").as_tuple()
    assert restored.last_updated_at == instant
    assert restored.last_updated_at.tzinfo is UTC
    assert restored.requires_reconciliation
    with pytest.raises(RuntimeError, match="requires reconciliation"):
        reopened.record_candidate("cycle-1", high_watermark=D("104"), ratchet_anchor=D("104"),
                                  candidate_stop=D("102"), policy_revision="stepped-v3", idempotency_key="blocked")
    reopened.reconcile("cycle-1", idempotency_key="reconciled", broker_order_id="stop-1", broker_status="NEW")
    assert not reopened.load("cycle-1").requires_reconciliation
    reopened.record_candidate("cycle-1", high_watermark=D("104"), ratchet_anchor=D("104"),
                              candidate_stop=D("102"), policy_revision="stepped-v3", idempotency_key="allowed")
    reopened.close()


def test_mark_stale_suspends_advancement_and_preserves_confirmed_stop(tmp_path):
    store = TrailingStateStore(tmp_path / "trailing.db")
    _create(store)
    stale = store.mark_stale("cycle-1", "heartbeat lost", idempotency_key="offline")
    assert stale.status is TrailingStatus.TRAIL_STALE
    assert stale.last_confirmed_stop == D("99.00")
    with pytest.raises(RuntimeError, match="suspended"):
        store.record_candidate("cycle-1", high_watermark=D("104"), ratchet_anchor=D("104"),
                              candidate_stop=D("102"), policy_revision="stepped-v3", idempotency_key="stale-ratchet")
    store.close()


def test_history_has_no_mutation_api_and_sqlite_enforces_append_only(tmp_path):
    store = TrailingStateStore(tmp_path / "trailing.db")
    _create(store)
    assert not hasattr(store, "update_history")
    assert not hasattr(store, "delete_history")
    store.record_candidate("cycle-1", high_watermark=D("104"), ratchet_anchor=D("104"),
                           candidate_stop=D("102"), policy_revision="stepped-v3", idempotency_key="ratchet")
    assert len(store.history("cycle-1")) == 2
    with pytest.raises(Exception, match="append-only"):
        store._conn.execute("UPDATE trailing_state_history SET event_type='RECONCILED'")
    with pytest.raises(Exception, match="append-only"):
        store._conn.execute("DELETE FROM trailing_state_history")
    store.close()


def test_store_uses_wal_mode(tmp_path):
    store = TrailingStateStore(tmp_path / "trailing.db")
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    store.close()


def test_stop_fill_is_audited_and_closes_cycle(tmp_path):
    store = TrailingStateStore(tmp_path / "trailing.db")
    _create(store)
    event = store.record_stop_filled("cycle-1", broker_order_id="stop-1", idempotency_key="fill-1")
    assert event.event_type is TrailingEventType.STOP_FILLED
    assert store.load("cycle-1").status is TrailingStatus.CLOSED
    assert store.load("cycle-1").last_confirmed_stop == D("99.00")
    store.close()
