"""Offline contracts for Alpaca activities and trade_updates normalization."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from mike_trade.brokers.alpaca.activities import (
    AlpacaActivityService,
    CorporateActivity,
    FeeActivity,
    FillActivity,
    UnknownActivity,
    activity_idempotency_key,
    dedupe_activities,
)
from mike_trade.brokers.alpaca.auth import (
    AlpacaCredentials,
    AlpacaEnvironment,
    EnvironmentMismatchError,
)
from mike_trade.brokers.alpaca.updates import (
    ReconnectPolicy,
    StreamAuthError,
    StreamReconnected,
    TradeUpdate,
    TradeUpdateEvent,
    TradeUpdateStream,
)

FP = "fixture-paper-account"


def order_fixture(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": "order-1",
        "client_order_id": "client-1",
        "symbol": "AAPL",
        "status": "accepted",
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "order_class": "simple",
        "qty": "3.5",
        "notional": None,
        "filled_qty": "0",
        "filled_avg_price": None,
        "limit_price": "190.25",
        "stop_price": "180.50",
        "trail_price": "2.50",
        "trail_percent": "1.25",
        "hwm": "195.75",
        "legs": [],
        "created_at": "2026-10-08T13:00:00-04:00",
        "updated_at": "2026-10-08T17:00:01Z",
        "submitted_at": "2026-10-08T17:00:00Z",
        "filled_at": None,
        "expired_at": None,
        "canceled_at": None,
        "failed_at": None,
        "replaced_at": None,
    }
    result.update(overrides)
    return result


def update_fixture(event: str, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "event": event,
        "order": order_fixture(status=event),
        "timestamp": "2026-10-08T17:00:02.123456Z",
        "execution_id": None,
        "price": None,
        "qty": None,
        "position_qty": None,
    }
    data.update(overrides)
    return {"stream": "trade_updates", "data": data}


@pytest.mark.parametrize(
    ("raw_event", "expected"),
    [
        ("new", TradeUpdateEvent.NEW),
        ("accepted", TradeUpdateEvent.ACCEPTED),
        ("pending_new", TradeUpdateEvent.PENDING_NEW),
        ("partial_fill", TradeUpdateEvent.PARTIAL_FILL),
        ("fill", TradeUpdateEvent.FILL),
        ("rejected", TradeUpdateEvent.REJECTED),
        ("canceled", TradeUpdateEvent.CANCELED),
        ("expired", TradeUpdateEvent.EXPIRED),
        ("replaced", TradeUpdateEvent.REPLACED),
        ("pending_cancel", TradeUpdateEvent.PENDING_CANCEL),
        ("pending_replace", TradeUpdateEvent.PENDING_REPLACE),
        ("done_for_day", TradeUpdateEvent.DONE_FOR_DAY),
        ("stopped", TradeUpdateEvent.STOPPED),
        ("suspended", TradeUpdateEvent.SUSPENDED),
        ("calculated", TradeUpdateEvent.CALCULATED),
    ],
)
def test_each_trade_update_event_is_normalized(
    raw_event: str, expected: TradeUpdateEvent
) -> None:
    update = TradeUpdate.from_message(
        update_fixture(raw_event),
        environment=AlpacaEnvironment.PAPER,
        fingerprint=FP,
    )
    assert update.event is expected
    assert update.raw_event == raw_event
    assert update.timestamp.tzinfo is UTC
    assert update.environment is AlpacaEnvironment.PAPER
    assert update.fingerprint == FP
    for field in (
        "qty",
        "filled_qty",
        "filled_avg_price",
        "limit_price",
        "stop_price",
        "trail_price",
        "trail_percent",
        "hwm",
    ):
        value = getattr(update.order, field)
        assert value is None or isinstance(value, Decimal), field


def test_partial_fill_then_fill_has_incremental_cumulative_and_position_qty() -> None:
    partial = TradeUpdate.from_message(
        update_fixture(
            "partial_fill",
            execution_id="exec-1",
            price="190.10",
            qty="1.25",
            position_qty="1.25",
            order=order_fixture(
                status="partially_filled",
                filled_qty="1.25",
                filled_avg_price="190.10",
            ),
        ),
        environment=AlpacaEnvironment.PAPER,
        fingerprint=FP,
    )
    fill = TradeUpdate.from_message(
        update_fixture(
            "fill",
            execution_id="exec-2",
            price="190.20",
            qty="2.25",
            position_qty="3.50",
            order=order_fixture(
                status="filled",
                filled_qty="3.50",
                filled_avg_price="190.1642857143",
                filled_at="2026-10-08T17:00:03Z",
            ),
        ),
        environment=AlpacaEnvironment.PAPER,
        fingerprint=FP,
    )
    assert partial.incremental_qty == Decimal("1.25")
    assert partial.cumulative_qty == Decimal("1.25")
    assert partial.position_qty == Decimal("1.25")
    assert fill.incremental_qty == Decimal("2.25")
    assert fill.cumulative_qty == Decimal("3.50")
    assert fill.position_qty == Decimal("3.50")
    assert fill.price == Decimal("190.20")


def test_unknown_event_is_preserved_and_environment_mismatch_is_rejected() -> None:
    unknown = TradeUpdate.from_message(
        update_fixture("future_event"),
        environment=AlpacaEnvironment.PAPER,
        fingerprint=FP,
    )
    assert unknown.event is TradeUpdateEvent.UNKNOWN
    assert unknown.raw_event == "future_event"
    assert unknown.is_unknown

    mismatch = update_fixture("accepted", environment="LIVE")
    with pytest.raises(EnvironmentMismatchError):
        TradeUpdate.from_message(
            mismatch,
            environment=AlpacaEnvironment.PAPER,
            fingerprint=FP,
        )


class FakeActivityClient:
    environment = AlpacaEnvironment.PAPER
    fingerprint = FP

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        assert params is not None
        self.calls.append((path, dict(params)))
        return self.pages[len(self.calls) - 1]


def fill_activity(activity_id: str, *, fill_type: str = "fill") -> dict[str, Any]:
    return {
        "activity_type": "FILL",
        "cum_qty": "3.50",
        "id": activity_id,
        "leaves_qty": "0",
        "price": "190.20",
        "qty": "2.25",
        "side": "buy",
        "symbol": "AAPL",
        "transaction_time": "2026-10-08T13:00:03-04:00",
        "order_id": "order-1",
        "type": fill_type,
    }


def test_activity_pagination_is_capped_and_passes_filters_and_tokens() -> None:
    client = FakeActivityClient(
        [
            [fill_activity("activity-1")],
            [fill_activity("activity-2")],
            [fill_activity("activity-3")],
        ]
    )
    service = AlpacaActivityService(client, page_size=1, max_pages=2)
    result = service.list_activities(
        activity_types=["FILL", "FEE"],
        after="2026-10-01T00:00:00Z",
        until="2026-10-09",
    )
    assert [item.id for item in result] == ["activity-1", "activity-2"]
    assert len(client.calls) == 2
    assert client.calls[0] == (
        "/v2/account/activities",
        {
            "page_size": 1,
            "activity_types": "FILL,FEE",
            "after": "2026-10-01T00:00:00Z",
            "until": "2026-10-09",
        },
    )
    assert client.calls[1][1]["page_token"] == "activity-1"


def test_fill_fee_dividend_corporate_unknown_and_dedupe() -> None:
    service = AlpacaActivityService(FakeActivityClient([]))
    fill = service.normalize_activity(fill_activity("fill-1", fill_type="partial_fill"))
    fee = service.normalize_activity(
        {
            "activity_type": "FEE",
            "activity_sub_type": "TAF",
            "id": "fee-1",
            "date": "2026-10-08",
            "net_amount": "-0.07",
            "symbol": "AAPL",
            "qty": "3.50",
        }
    )
    dividend = service.normalize_activity(
        {
            "activity_type": "DIV",
            "id": "div-1",
            "date": "2026-10-08",
            "net_amount": "1.02",
            "symbol": "T",
            "qty": "2",
            "per_share_amount": "0.51",
        }
    )
    split = service.normalize_activity(
        {
            "activity_type": "SPLIT",
            "activity_sub_type": "FSPLIT",
            "id": "split-1",
            "date": "2026-10-08",
            "symbol": "XYZ",
            "qty": "20",
            "net_amount": "0",
        }
    )
    unknown = service.normalize_activity(
        {
            "activity_type": "FUTURE_KIND",
            "id": "future-1",
            "date": "2026-10-08",
            "net_amount": "7.25",
        }
    )
    assert isinstance(fill, FillActivity)
    assert fill.type == "partial_fill"
    assert fill.qty == Decimal("2.25")
    assert fill.transaction_time.tzinfo is UTC
    assert isinstance(fee, FeeActivity)
    assert fee.amount == Decimal("-0.07")
    assert fee.qty == Decimal("3.50")
    assert isinstance(dividend, CorporateActivity) and dividend.is_dividend
    assert dividend.amount == Decimal("1.02")
    assert dividend.per_share_amount == Decimal("0.51")
    assert isinstance(split, CorporateActivity) and not split.is_dividend
    assert split.qty == Decimal("20")
    assert isinstance(unknown, UnknownActivity)
    assert unknown.raw["net_amount"] == "7.25"
    assert fill.idempotency_key == activity_idempotency_key(
        "fill-1", AlpacaEnvironment.PAPER, FP
    )
    assert dedupe_activities([fill, fee, fill, dividend, fee]) == [fill, fee, dividend]


AUTHORIZED = {
    "stream": "authorization",
    "data": {"status": "authorized", "action": "authenticate"},
}
LISTENING = {"stream": "listening", "data": {"streams": ["trade_updates"]}}


class FakeConnection:
    def __init__(self, incoming: list[Any]) -> None:
        self.incoming = incoming
        self.sent: list[Mapping[str, Any]] = []
        self.closed = 0

    async def send_json(self, payload: Mapping[str, Any]) -> None:
        self.sent.append(dict(payload))

    async def receive_json(self) -> Any:
        if not self.incoming:
            raise EOFError("fixture exhausted")
        value = self.incoming.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    async def close(self) -> None:
        self.closed += 1


class FakeTransport:
    def __init__(self, connections: list[FakeConnection]) -> None:
        self.connections = connections
        self.urls: list[str] = []

    async def connect(self, url: str) -> FakeConnection:
        self.urls.append(url)
        if not self.connections:
            raise ConnectionError("no scripted connection")
        return self.connections.pop(0)


def stream(transport: FakeTransport, **overrides: Any) -> TradeUpdateStream:
    return TradeUpdateStream(
        AlpacaCredentials("fixture-key", "fixture-secret", AlpacaEnvironment.PAPER),
        transport,
        fingerprint=FP,
        rng=lambda: 0.5,
        **overrides,
    )


@pytest.mark.asyncio
async def test_stream_auth_failure_is_typed() -> None:
    connection = FakeConnection(
        [
            {
                "stream": "authorization",
                "data": {"status": "unauthorized", "action": "authenticate"},
            }
        ]
    )
    events = stream(FakeTransport([connection])).events()
    with pytest.raises(StreamAuthError):
        await anext(events)
    assert connection.sent[0] == {
        "action": "auth",
        "key": "fixture-key",
        "secret": "fixture-secret",
    }
    assert all(payload.get("action") != "listen" for payload in connection.sent)


@pytest.mark.asyncio
async def test_disconnect_emits_gap_resubscribes_and_uses_bounded_sleep() -> None:
    first_update = update_fixture(
        "partial_fill",
        qty="1",
        position_qty="1",
        order=order_fixture(status="partially_filled", filled_qty="1"),
    )
    second_update = update_fixture(
        "fill",
        qty="2.5",
        position_qty="3.5",
        order=order_fixture(status="filled", filled_qty="3.5"),
    )
    first = FakeConnection([AUTHORIZED, LISTENING, first_update, ConnectionError("drop")])
    second = FakeConnection([AUTHORIZED, LISTENING, second_update])
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    policy = ReconnectPolicy(
        max_attempts=2,
        base_delay_seconds=4,
        max_delay_seconds=5,
        jitter_ratio=1,
    )
    source = stream(
        FakeTransport([first, second]),
        reconnect_policy=policy,
        sleep=fake_sleep,
        clock=lambda: datetime(2026, 10, 8, 17, 0, 4, tzinfo=UTC),
    ).events()
    partial = await anext(source)
    gap = await anext(source)
    fill = await anext(source)
    await source.aclose()

    assert isinstance(partial, TradeUpdate)
    assert isinstance(gap, StreamReconnected)
    assert gap.last_seen_timestamp == partial.timestamp
    assert gap.requires_reconciliation
    assert isinstance(fill, TradeUpdate) and fill.event is TradeUpdateEvent.FILL
    assert sleeps == [4.0]
    assert max(sleeps) <= policy.max_delay_seconds
    for connection in (first, second):
        assert connection.sent[1] == {
            "action": "listen",
            "data": {"streams": ["trade_updates"]},
        }


def test_stream_constructor_rejects_environment_mismatch() -> None:
    credentials = AlpacaCredentials(
        "fixture-key", "fixture-secret", AlpacaEnvironment.PAPER
    )
    with pytest.raises(EnvironmentMismatchError):
        TradeUpdateStream(
            credentials,
            FakeTransport([]),
            environment=AlpacaEnvironment.LIVE,
        )
