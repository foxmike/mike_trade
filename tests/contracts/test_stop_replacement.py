"""Offline contract tests for protective-stop replacement and Alpaca mapping."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from mike_trade.brokers.alpaca.replacement import AlpacaStopReplacementAdapter
from mike_trade.domain.trading import TimeInForce
from mike_trade.execution.replace import (
    ReplacementOutcome,
    ReplacementStatus,
    StopReplacementCoordinator,
)

NOW = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)
OLD_ID = "stop-old"
NEW_ID = "stop-new"


def outcome(
    status: ReplacementStatus,
    *,
    order_id: str = OLD_ID,
    broker_order_id: str | None = None,
    price: str | None = None,
    qty: str | None = None,
    filled: str = "0",
    remaining: str | None = None,
    detail: str = "",
) -> ReplacementOutcome:
    return ReplacementOutcome(
        status,
        order_id,
        broker_order_id=broker_order_id,
        stop_price=None if price is None else Decimal(price),
        quantity=None if qty is None else Decimal(qty),
        filled_quantity=Decimal(filled),
        remaining_quantity=None if remaining is None else Decimal(remaining),
        observed_at=NOW,
        detail=detail,
    )


class FakePort:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.standalone = True
        self.replace_capability = True
        self.increment = Decimal("0.01")
        self.replace_result = outcome(ReplacementStatus.UNKNOWN)
        self.lookup_results: list[ReplacementOutcome] = [outcome(ReplacementStatus.UNKNOWN)]
        self.replace_calls = 0

    def supports_standalone_stop(self, *, qty: Decimal, tif: TimeInForce) -> bool:
        return self.standalone and qty == qty.to_integral_value()

    def supports_replace(self, *, qty: Decimal, tif: TimeInForce) -> bool:
        return self.replace_capability and qty == qty.to_integral_value()

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
        self.events.append("replace")
        self.replace_calls += 1
        return self.replace_result

    def cancel_stop(self, order_id: str) -> ReplacementOutcome:
        self.events.append("cancel")
        return outcome(ReplacementStatus.UNKNOWN)

    def lookup_order(self, order_id: str) -> ReplacementOutcome:
        self.events.append("lookup")
        if len(self.lookup_results) > 1:
            return self.lookup_results.pop(0)
        return self.lookup_results[0]


class FakeTransport:
    """Recorded HTTP transport: no credentials and no network implementation."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.get_results: list[object] = [alpaca_order(status="accepted")]
        self.patch_result: object = alpaca_order(
            order_id=NEW_ID, status="accepted", stop_price="101.23"
        )
        self.delete_result: object = None
        self.patch_calls = 0

    @staticmethod
    def _take(items: list[object]) -> object:
        result = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(result, BaseException):
            raise result
        return result

    def get(self, path: str, *, params: object = None) -> object:
        self.events.append("lookup")
        return self._take(self.get_results)

    def patch(
        self,
        path: str,
        *,
        json_body: Mapping[str, str],
        params: object = None,
    ) -> object:
        self.events.append("replace")
        self.patch_calls += 1
        assert path == f"/v2/orders/{OLD_ID}"
        assert json_body == {
            "stop_price": "101.23",
            "qty": "10",
            "time_in_force": "gtc",
            "client_order_id": "replace-1",
        }
        if isinstance(self.patch_result, BaseException):
            raise self.patch_result
        return self.patch_result

    def delete(self, path: str, *, params: object = None) -> object:
        self.events.append("cancel")
        return self.delete_result


def alpaca_order(
    *,
    order_id: str = OLD_ID,
    status: str,
    qty: str = "10",
    filled_qty: str = "0",
    stop_price: str = "100.00",
    replaced_by: str | None = None,
) -> dict[str, object]:
    return {
        "id": order_id,
        "status": status,
        "qty": qty,
        "filled_qty": filled_qty,
        "stop_price": stop_price,
        "replaced_by": replaced_by,
        "updated_at": NOW.isoformat(),
    }


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value


def build(kind: str, scenario: str) -> tuple[object, list[str], object]:
    events: list[str] = []
    if kind == "memory":
        port = FakePort(events)
        if scenario == "confirmed":
            port.replace_result = outcome(
                ReplacementStatus.CONFIRMED,
                broker_order_id=NEW_ID,
                price="101.23",
                qty="10",
            )
        elif scenario == "rejected":
            port.replace_result = outcome(ReplacementStatus.REJECTED, detail="no")
        elif scenario == "unknown":
            port.replace_result = outcome(ReplacementStatus.UNKNOWN)
            port.lookup_results = [
                outcome(ReplacementStatus.UNKNOWN),
                outcome(ReplacementStatus.UNKNOWN),
            ]
        elif scenario == "filled":
            port.lookup_results = [
                outcome(
                    ReplacementStatus.STOP_FILLED_DURING_REPLACE,
                    broker_order_id=OLD_ID,
                    filled="10",
                    remaining="0",
                )
            ]
        elif scenario == "partial":
            port.lookup_results = [
                outcome(
                    ReplacementStatus.STOP_FILLED_DURING_REPLACE,
                    broker_order_id=OLD_ID,
                    filled="3.5",
                    remaining="6.5",
                )
            ]
        return port, events, port

    transport = FakeTransport(events)
    if scenario == "confirmed":
        transport.patch_result = alpaca_order(
            order_id=NEW_ID, status="accepted", stop_price="101.23"
        )
        transport.get_results = [
            alpaca_order(status="accepted"),
            alpaca_order(status="replaced", replaced_by=NEW_ID),
        ]
    elif scenario == "rejected":
        transport.patch_result = alpaca_order(status="rejected")
    elif scenario == "unknown":
        transport.patch_result = alpaca_order(order_id=NEW_ID, status="accepted")
        transport.get_results = [
            alpaca_order(status="accepted"),
            alpaca_order(status="pending_replace"),
        ]
    elif scenario == "filled":
        transport.get_results = [alpaca_order(status="filled", filled_qty="10")]
    elif scenario == "partial":
        transport.get_results = [
            alpaca_order(status="partially_filled", filled_qty="3.5")
        ]
    adapter = AlpacaStopReplacementAdapter(transport, price_increment=Decimal("0.01"))
    return adapter, events, transport


def coordinator(
    port: object,
    events: list[str],
    *,
    clock: Clock | None = None,
    interval: timedelta = timedelta(0),
) -> StopReplacementCoordinator:
    def record(intent: Mapping[str, object]) -> None:
        assert intent["recorded_at"].tzinfo is not None  # type: ignore[union-attr]
        events.append("persist")

    return StopReplacementCoordinator(
        port, record, min_replace_interval=interval, clock=clock or Clock()
    )  # type: ignore[arg-type]


def request(workflow: StopReplacementCoordinator, **overrides: Any) -> ReplacementOutcome:
    kwargs: dict[str, object] = {
        "protected_order_id": OLD_ID,
        "candidate_stop": Decimal("101.239"),
        "last_confirmed_stop": Decimal("100.00"),
        "qty": Decimal("10"),
        "tif": TimeInForce.GTC,
        "client_order_id": "replace-1",
    }
    kwargs.update(overrides)
    return workflow.replace_stop(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["memory", "alpaca"])
def test_confirmed_replace_and_persist_before_broker(kind: str) -> None:
    port, events, _ = build(kind, "confirmed")
    result = request(coordinator(port, events))

    assert result.status is ReplacementStatus.CONFIRMED
    assert result.effective is True
    assert result.broker_order_id == NEW_ID
    assert result.stop_price == Decimal("101.23")
    assert result.quantity == Decimal("10")
    assert events.index("persist") < events.index("replace")


@pytest.mark.parametrize("kind", ["memory", "alpaca"])
def test_rejected_preserves_verified_stop_and_blocks(kind: str) -> None:
    port, events, _ = build(kind, "rejected")
    result = request(coordinator(port, events))

    assert result.status is ReplacementStatus.REJECTED
    assert result.authoritative_order_id == OLD_ID
    assert result.authoritative_stop_price == Decimal("100.00")
    assert result.trail_update_unconfirmed
    assert result.entries_blocked and result.reconciliation_required


@pytest.mark.parametrize("kind", ["memory", "alpaca"])
def test_acknowledged_but_unconfirmed_is_unknown(kind: str) -> None:
    port, events, _ = build(kind, "unknown")
    result = request(coordinator(port, events))

    assert result.status is ReplacementStatus.UNKNOWN
    assert result.effective is False
    assert result.authoritative_stop_price == Decimal("100.00")
    assert result.trail_update_unconfirmed
    assert result.entries_blocked and result.reconciliation_required


@pytest.mark.parametrize(
    ("kind", "scenario", "filled", "remaining"),
    [
        ("memory", "filled", "10", "0"),
        ("alpaca", "filled", "10", "0"),
        ("memory", "partial", "3.5", "6.5"),
        ("alpaca", "partial", "3.5", "6.5"),
    ],
)
def test_fill_race_reports_quantities_and_skips_replace(
    kind: str, scenario: str, filled: str, remaining: str
) -> None:
    port, events, transport = build(kind, scenario)
    result = request(coordinator(port, events))

    assert result.status is ReplacementStatus.STOP_FILLED_DURING_REPLACE
    assert result.filled_quantity == Decimal(filled)
    assert result.remaining_quantity == Decimal(remaining)
    assert "replace" not in events
    calls = transport.replace_calls if kind == "memory" else transport.patch_calls
    assert calls == 0


@pytest.mark.parametrize("kind", ["memory", "alpaca"])
def test_only_one_pending_replace_and_latest_candidate_is_coalesced(kind: str) -> None:
    port, events, transport = build(kind, "unknown")
    workflow = coordinator(port, events)
    first = request(workflow)
    second = request(
        workflow,
        candidate_stop=Decimal("102.119"),
        client_order_id="replace-2",
    )

    assert first.status is ReplacementStatus.UNKNOWN
    assert second.status is ReplacementStatus.UNKNOWN
    assert second.coalesced
    assert second.coalesced_stop_price == Decimal("102.11")
    calls = transport.replace_calls if kind == "memory" else transport.patch_calls
    assert calls == 1


@pytest.mark.parametrize("kind", ["memory", "alpaca"])
@pytest.mark.parametrize("candidate", ["100.00", "100.009", "99.99"])
def test_non_increasing_candidate_after_rounding_never_calls_broker(
    kind: str, candidate: str
) -> None:
    port, events, transport = build(kind, "confirmed")
    result = request(coordinator(port, events), candidate_stop=Decimal(candidate))

    assert result.status is ReplacementStatus.REJECTED
    assert "persist" not in events and "replace" not in events
    calls = transport.replace_calls if kind == "memory" else transport.patch_calls
    assert calls == 0


@pytest.mark.parametrize("kind", ["memory", "alpaca"])
def test_unverified_fractional_stop_fails_closed(kind: str) -> None:
    port, events, transport = build(kind, "confirmed")
    result = request(coordinator(port, events), qty=Decimal("10.5"))

    assert result.status is ReplacementStatus.UNSUPPORTED
    assert "persist" not in events and "replace" not in events
    calls = transport.replace_calls if kind == "memory" else transport.patch_calls
    assert calls == 0


def test_missing_provider_capability_fails_closed() -> None:
    port, events, _ = build("memory", "confirmed")
    port.replace_capability = False
    result = request(coordinator(port, events))
    assert result.status is ReplacementStatus.UNSUPPORTED
    assert port.replace_calls == 0


@pytest.mark.parametrize("kind", ["memory", "alpaca"])
def test_minimum_interval_is_enforced_with_injected_clock(kind: str) -> None:
    port, events, transport = build(kind, "confirmed")
    clock = Clock()
    workflow = coordinator(port, events, clock=clock, interval=timedelta(seconds=30))
    assert request(workflow).status is ReplacementStatus.CONFIRMED
    clock.value += timedelta(seconds=29)

    result = request(
        workflow,
        candidate_stop=Decimal("102.00"),
        last_confirmed_stop=Decimal("101.23"),
        client_order_id="replace-2",
    )
    assert result.status is ReplacementStatus.REJECTED
    assert "interval" in result.detail
    calls = transport.replace_calls if kind == "memory" else transport.patch_calls
    assert calls == 1


def test_alpaca_fractional_flag_must_be_explicitly_verified() -> None:
    transport = FakeTransport([])
    default = AlpacaStopReplacementAdapter(transport, price_increment=Decimal("0.01"))
    verified = AlpacaStopReplacementAdapter(
        transport,
        price_increment=Decimal("0.01"),
        fractional_stop_verified_in_paper=True,
    )
    assert not default.supports_replace(qty=Decimal("1.5"), tif=TimeInForce.DAY)
    assert verified.supports_replace(qty=Decimal("1.5"), tif=TimeInForce.DAY)


def test_alpaca_cancel_acknowledgement_requires_lookup_confirmation() -> None:
    events: list[str] = []
    transport = FakeTransport(events)
    transport.get_results = [
        alpaca_order(status="accepted"),
        alpaca_order(status="canceled"),
    ]
    adapter = AlpacaStopReplacementAdapter(transport, price_increment=Decimal("0.01"))
    workflow = coordinator(adapter, events)

    result = workflow.cancel_stop(
        protected_order_id=OLD_ID,
        last_confirmed_stop=Decimal("100.00"),
        qty=Decimal("10"),
        client_order_id="cancel-1",
    )

    assert result.status is ReplacementStatus.CONFIRMED
    assert result.authoritative_order_id is None
    assert result.authoritative_stop_price is None
    assert events.index("persist") < events.index("cancel")
