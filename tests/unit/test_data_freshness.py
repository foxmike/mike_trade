from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from mike_trade.market_data.quality import FeedQualityTracker, HealthState


class Clock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value


@dataclass
class Event:
    symbol: str
    provider_source_ts: datetime
    receive_ts: datetime
    feed_id: str = "sip"
    price: Decimal = Decimal("10")
    size: Decimal = Decimal("2")
    sequence_id: int | None = None
    kind: str = "trade"


def tracker(clock, **kwargs):
    configured_feed_id = kwargs.pop("configured_feed_id", "sip")
    return FeedQualityTracker(
        clock=clock,
        staleness_seconds={"trades": 5, "bars": 90},
        session_calendar=lambda _now: True,
        configured_feed_id=configured_feed_id,
        **kwargs,
    )


def test_age_and_transport_latency_use_distinct_timestamps():
    now = datetime(2026, 1, 5, 15, 0, 10, tzinfo=timezone.utc)
    t = tracker(Clock(now))
    assert t.ingest(Event("AAPL", now - timedelta(seconds=8), now - timedelta(seconds=3)))
    fresh = t.symbol_health("AAPL").channels["trades"]
    assert fresh.age_seconds == 8
    assert fresh.transport_latency_seconds == 5


def test_regular_session_staleness_degrades_and_blocks_entry():
    now = datetime(2026, 1, 5, 15, 0, 0, tzinfo=timezone.utc)
    t = tracker(Clock(now), stream_activity_gap_seconds=120)
    t.ingest(Event("AAPL", now - timedelta(seconds=6), now - timedelta(seconds=5)))
    assert t.symbol_health("AAPL").state is HealthState.DEGRADED
    decision = t.entries_allowed("AAPL")
    assert not decision.allowed
    assert decision.reason_code == "STALE_DATA"


def test_unknown_freshness_fails_closed():
    decision = tracker(Clock(datetime(2026, 1, 5, tzinfo=timezone.utc))).entries_allowed("MSFT")
    assert not decision.allowed
    assert decision.reason_code == "UNKNOWN_FRESHNESS"


def test_duplicates_by_sequence_and_fallback_are_counted_without_refresh():
    now = datetime(2026, 1, 5, 15, tzinfo=timezone.utc)
    t = tracker(Clock(now))
    event = Event("AAPL", now - timedelta(seconds=1), now - timedelta(seconds=1), sequence_id=77)
    assert t.ingest(event)
    assert not t.ingest(event)
    # A later receive time for the same provider sequence remains a duplicate.
    duplicate = Event("AAPL", event.provider_source_ts, now, sequence_id=77)
    assert not t.ingest(duplicate)
    key_event = Event("MSFT", event.provider_source_ts, event.receive_ts, sequence_id=None)
    assert t.ingest(key_event)
    assert not t.ingest(key_event)
    assert t.symbol_health("AAPL").channels["trades"].receive_ts == event.receive_ts
    assert t.symbol_health("AAPL").channels["trades"].duplicate_count == 2
    assert t.symbol_health("MSFT").channels["trades"].duplicate_count == 1


def test_out_of_order_event_is_counted_without_replacing_latest_source_time():
    now = datetime(2026, 1, 5, 15, tzinfo=timezone.utc)
    t = tracker(Clock(now), stream_activity_gap_seconds=120)
    latest = Event("AAPL", now - timedelta(seconds=1), now, sequence_id=2)
    older = Event("AAPL", now - timedelta(seconds=3), now, sequence_id=1)
    t.ingest(latest)
    t.ingest(older)
    fresh = t.symbol_health("AAPL").channels["trades"]
    assert fresh.provider_source_ts == latest.provider_source_ts
    assert fresh.out_of_order_count == 1


def test_fresh_polling_fallback_allows_entries_when_stream_disconnects():
    now = datetime(2026, 1, 5, 15, tzinfo=timezone.utc)
    t = tracker(Clock(now))
    t.set_stream_connected(False)
    t.ingest(Event("AAPL", now, now), source="poll")
    assert t.symbol_health("AAPL").state is HealthState.HEALTHY
    assert t.entries_allowed("AAPL").allowed


def test_missing_completed_minute_bar_blocks_until_recovered():
    now = datetime(2026, 1, 5, 15, 5, tzinfo=timezone.utc)
    t = tracker(Clock(now), stream_activity_gap_seconds=300)
    first = now - timedelta(minutes=3)
    t.ingest(Event("AAPL", first, first + timedelta(seconds=1), kind="bar"))
    t.ingest(Event("AAPL", first + timedelta(minutes=2), first + timedelta(minutes=2, seconds=1), kind="bar"))
    assert t.symbol_health("AAPL").unresolved_gaps == (first + timedelta(minutes=1),)
    assert t.entries_allowed("AAPL").reason_code == "UNRESOLVED_GAP"
    t.mark_gap_recovered("AAPL", first + timedelta(minutes=1))
    assert t.entries_allowed("AAPL").allowed


def test_feed_provenance_mismatch_and_iex_consolidation():
    now = datetime(2026, 1, 5, 15, tzinfo=timezone.utc)
    t = tracker(Clock(now), configured_feed_id="sip")
    t.ingest(Event("AAPL", now, now, feed_id="iex"))
    health = t.symbol_health("AAPL")
    assert health.feed.feed_id == "iex"
    assert health.feed.consolidated is False
    assert health.feed.entitlement_mismatch
    assert t.entries_allowed("AAPL").reason_code == "ENTITLEMENT_MISMATCH"


def test_unrecognized_feed_id_fails_closed():
    now = datetime(2026, 1, 5, 15, tzinfo=timezone.utc)
    t = tracker(Clock(now))
    t.ingest(Event("AAPL", now, now, feed_id="mystery"))
    assert t.entries_allowed("AAPL").reason_code == "UNKNOWN_FEED_ID"


def test_rate_limit_counters_and_backoff_are_projected():
    now = datetime(2026, 1, 5, 15, tzinfo=timezone.utc)
    t = tracker(Clock(now))
    t.record_request(status_code=200)
    t.record_request(status_code=429, backoff_seconds=12)
    snapshot = t.service_health()
    assert snapshot.rate_limit.requests_used == 2
    assert snapshot.rate_limit.responses_429 == 1
    assert snapshot.rate_limit.backoff_seconds == 12
    assert snapshot.state is HealthState.DEGRADED


def test_health_controls_entries_only():
    assert "fills" in FeedQualityTracker.__doc__
    assert not any(name in FeedQualityTracker.__dict__ for name in (
        "block_fill", "block_order_update", "block_stop", "block_reconciliation", "block_manual_close"
    ))
