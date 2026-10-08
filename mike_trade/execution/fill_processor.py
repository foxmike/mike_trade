"""Exactly-once ingestion of normalized order updates and fills."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from mike_trade.domain.trading import OrderSide
from mike_trade.execution.order_fsm import OrderEvent, OrderStatus, apply_transition
from mike_trade.persistence.journal import DomainEventJournal, NewEvent, canonical_payload_json
from mike_trade.persistence.schema import decimal_from_db, decimal_to_db, to_db_timestamp
from mike_trade.persistence.transaction import ScopedWriter

__all__ = [
    "FillProcessor",
    "IngestResult",
    "FillEvent",
    "OrderUpdateEvent",
    "ReconciliationDrift",
    "PositionFillState",
]

ZERO = Decimal("0")


def _text(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _decimal(value: Decimal | None, name: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite() or value < ZERO:
        raise ValueError(f"{name} must be finite and non-negative")
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class FillEvent:
    client_order_id: str
    occurred_at: datetime
    event_id: str | None = None
    broker_order_id: str | None = None
    symbol: str | None = None
    side: OrderSide | str | None = None
    quantity: Decimal | None = None
    cumulative_qty: Decimal | None = None
    price: Decimal | None = None
    average_price: Decimal | None = None
    fee: Decimal = ZERO
    event_type: str = "FILL"

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_order_id", _text(self.client_order_id, "client_order_id"))
        object.__setattr__(self, "event_id", _text(self.event_id, "event_id", optional=True))
        object.__setattr__(
            self, "broker_order_id", _text(self.broker_order_id, "broker_order_id", optional=True)
        )
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol", optional=True))
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at))
        for name in ("quantity", "cumulative_qty", "price", "average_price", "fee"):
            object.__setattr__(
                self, name, _decimal(getattr(self, name), name, optional=name != "fee")
            )
        if self.side is not None and not isinstance(self.side, OrderSide):
            try:
                object.__setattr__(self, "side", OrderSide(str(self.side).upper()))
            except ValueError as exc:
                raise ValueError("side must be BUY or SELL") from exc


@dataclass(frozen=True, slots=True)
class OrderUpdateEvent:
    client_order_id: str
    status: str | OrderStatus
    occurred_at: datetime
    event_id: str | None = None
    broker_order_id: str | None = None
    cumulative_qty: Decimal | None = None
    average_price: Decimal | None = None
    broker_confirmed: bool = True
    broker_lookup: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_order_id", _text(self.client_order_id, "client_order_id"))
        object.__setattr__(self, "event_id", _text(self.event_id, "event_id", optional=True))
        object.__setattr__(
            self, "broker_order_id", _text(self.broker_order_id, "broker_order_id", optional=True)
        )
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at))
        object.__setattr__(
            self, "cumulative_qty", _decimal(self.cumulative_qty, "cumulative_qty", optional=True)
        )
        object.__setattr__(
            self, "average_price", _decimal(self.average_price, "average_price", optional=True)
        )


@dataclass(frozen=True, slots=True)
class PositionFillState:
    cycle_id: str
    symbol: str
    remaining_qty: Decimal
    cost_basis: Decimal
    average_price: Decimal | None


@dataclass(frozen=True, slots=True)
class ReconciliationDrift:
    drift_id: str
    drift_type: str
    client_order_id: str
    details: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class IngestResult:
    outcome: str
    dedupe_key: str
    incremental_qty: Decimal = ZERO
    order_status: OrderStatus | None = None
    position: PositionFillState | None = None
    drift: ReconciliationDrift | None = None

    @property
    def duplicate(self) -> bool:
        return self.outcome == "DUPLICATE"


class FillProcessor:
    """Apply normalized executions and lifecycle updates to a scoped ledger."""

    def __init__(
        self,
        writer: ScopedWriter,
        clock: Callable[[], datetime] | Any,
        notification_callback: Callable[[Mapping[str, Any]], None] | None = None,
    ):
        self._writer = writer
        self._clock = clock
        self._journal = DomainEventJournal(writer.connection, clock=clock)
        self._notify = notification_callback

    def ingest(self, event: FillEvent | OrderUpdateEvent) -> IngestResult:
        if not isinstance(event, (FillEvent, OrderUpdateEvent)):
            raise TypeError("event must be FillEvent or OrderUpdateEvent")
        scope = self._writer.account_scope_id
        key = self._dedupe_key(event)
        now_method = getattr(self._clock, "now", None)
        now = _utc(now_method() if callable(now_method) else self._clock())
        notification: Mapping[str, Any] | None = None
        with self._writer.unit_of_work() as conn:
            if conn.execute(
                "SELECT 1 FROM broker_activities WHERE account_scope_id=? "
                "AND external_activity_id=?",
                (scope, key),
            ).fetchone():
                return IngestResult("DUPLICATE", key)
            conn.execute(
                "INSERT INTO broker_activities(broker_activity_id,account_scope_id,"
                "external_activity_id,activity_type,payload_json,occurred_at,recorded_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    uuid4().hex,
                    scope,
                    key,
                    event.event_type if isinstance(event, FillEvent) else "ORDER_UPDATE",
                    canonical_payload_json(self._payload(event)),
                    to_db_timestamp(event.occurred_at),
                    to_db_timestamp(now),
                ),
            )
            order = conn.execute(
                "SELECT broker_order_id,intent_id,symbol,side,status,quantity,filled_quantity,"
                "filled_avg_price,client_order_id FROM broker_orders "
                "WHERE account_scope_id=? AND client_order_id=?",
                (scope, event.client_order_id),
            ).fetchone()
            if order is None:
                drift = self._drift(
                    conn, key, event, now, "UNKNOWN_CLIENT_ORDER", {}, self._payload(event)
                )
                self._emit(
                    "UnmanagedFillObserved"
                    if isinstance(event, FillEvent)
                    else "UnmanagedOrderUpdateObserved",
                    event.client_order_id,
                    self._payload(event),
                    event.occurred_at,
                    f"fill-ingest:{key}",
                )
                return IngestResult("UNMANAGED", key, drift=drift)
            (
                broker_id,
                intent_id,
                symbol,
                side_text,
                raw_status,
                raw_order_qty,
                raw_filled,
                raw_avg,
                _,
            ) = order
            if event.broker_order_id and event.broker_order_id != broker_id:
                drift = self._drift(
                    conn,
                    key,
                    event,
                    now,
                    "ORDER_ID_MISMATCH",
                    {"broker_order_id": broker_id},
                    self._payload(event),
                )
                self._emit(
                    "FillOrderMismatchObserved",
                    event.client_order_id,
                    self._payload(event),
                    event.occurred_at,
                    f"fill-ingest:{key}",
                )
                return IngestResult("DRIFT", key, drift=drift)
            current = self._fsm_status(raw_status)
            if isinstance(event, FillEvent):
                result, notification = self._apply_fill(conn, event, order, current, now, key)
            elif (
                self._target_status(event.status)
                in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}
                and event.cumulative_qty is not None
                and event.cumulative_qty > decimal_from_db(raw_filled)
            ):
                fill_event = FillEvent(
                    client_order_id=event.client_order_id,
                    event_id=event.event_id,
                    broker_order_id=event.broker_order_id,
                    occurred_at=event.occurred_at,
                    cumulative_qty=event.cumulative_qty,
                    average_price=event.average_price,
                )
                result, notification = self._apply_fill(conn, fill_event, order, current, now, key)
            else:
                result = self._apply_status(conn, event, order, current, now, key)
        if notification is not None and self._notify is not None:
            self._notify(notification)
        return result

    def _dedupe_key(self, event: FillEvent | OrderUpdateEvent) -> str:
        if event.event_id:
            return f"provider:{event.event_id}"
        payload = self._payload(event)
        raw = canonical_payload_json(payload)
        return "composite:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _payload(event: FillEvent | OrderUpdateEvent) -> dict[str, Any]:
        values = {name: getattr(event, name) for name in event.__dataclass_fields__}
        return values

    @staticmethod
    def _fsm_status(raw: str) -> OrderStatus:
        mapping = {
            "NEW": OrderStatus.PENDING,
            "PENDING_NEW": OrderStatus.PENDING,
            "PENDING_CANCEL": OrderStatus.CANCEL_PENDING,
        }
        if raw in mapping:
            return mapping[raw]
        try:
            return OrderStatus(raw)
        except ValueError:
            return OrderStatus.UNKNOWN

    @staticmethod
    def _broker_status(status: OrderStatus) -> str:
        return {
            OrderStatus.PENDING: "PENDING_NEW",
            OrderStatus.CANCEL_PENDING: "PENDING_CANCEL",
        }.get(status, status.value)

    def _apply_status(self, conn, event, order, current, now, key) -> IngestResult:
        status = self._target_status(event.status)
        transition = apply_transition(
            current,
            OrderEvent(
                status, broker_lookup=event.broker_lookup, broker_confirmed=event.broker_confirmed
            ),
        )
        if transition.accepted:
            conn.execute(
                "UPDATE broker_orders SET status=?,updated_at=? WHERE account_scope_id=? "
                "AND broker_order_id=?",
                (
                    self._broker_status(status),
                    to_db_timestamp(event.occurred_at),
                    self._writer.account_scope_id,
                    order[0],
                ),
            )
            if status in {
                OrderStatus.CANCELED,
                OrderStatus.EXPIRED,
                OrderStatus.REJECTED,
                OrderStatus.REPLACED,
            }:
                self._release_reservation(conn, order[1], now)
        self._emit(
            "OrderUpdateIngested",
            order[0],
            {
                "dedupe_key": key,
                "status": status.value,
                "applied": transition.accepted,
                "outcome": transition.outcome,
            },
            event.occurred_at,
            f"fill-ingest:{key}",
        )
        return IngestResult(
            "APPLIED" if transition.accepted else "RECORDED", key, order_status=transition.status
        )

    @staticmethod
    def _target_status(raw: str | OrderStatus) -> OrderStatus:
        if isinstance(raw, OrderStatus):
            return raw
        mapping = {
            "NEW": OrderStatus.PENDING,
            "PENDING_NEW": OrderStatus.PENDING,
            "PENDING_CANCEL": OrderStatus.CANCEL_PENDING,
        }
        value = str(raw).upper()
        if value in mapping:
            return mapping[value]
        return OrderStatus(value)

    def _apply_fill(self, conn, event, order, current, now, key):
        scope = self._writer.account_scope_id
        broker_id, intent_id, symbol, side_text, _, raw_order_qty, raw_filled, raw_avg, _ = order
        side = OrderSide(side_text)
        quantity = decimal_from_db(raw_filled)
        old_avg = ZERO if raw_avg is None else decimal_from_db(raw_avg)
        cumulative = event.cumulative_qty
        delta = (cumulative - quantity) if cumulative is not None else (event.quantity or ZERO)
        if delta < ZERO:
            delta = ZERO
        if delta == ZERO:
            self._emit(
                "FillUpdateIngested",
                broker_id,
                {"dedupe_key": key, "incremental_qty": ZERO, "cumulative_qty": cumulative},
                event.occurred_at,
                f"fill-ingest:{key}",
            )
            return IngestResult("RECORDED", key, order_status=current), None
        price = event.price if event.price is not None else event.average_price
        if price is None or price <= ZERO:
            raise ValueError("a positive fill requires a positive price")
        new_qty = quantity + delta
        if new_qty > decimal_from_db(raw_order_qty):
            drift = self._drift(
                conn,
                key,
                event,
                now,
                "FILL_EXCEEDS_ORDER_QTY",
                {"max_qty": raw_order_qty},
                {"new_qty": new_qty},
            )
            self._emit(
                "FillQuantityDriftObserved",
                broker_id,
                {"dedupe_key": key, "drift_id": drift.drift_id},
                event.occurred_at,
                f"fill-ingest:{key}",
            )
            return IngestResult("DRIFT", key, drift=drift), None
        if cumulative is not None and event.average_price is not None:
            new_cost = cumulative * event.average_price
            incremental_cost = new_cost - quantity * old_avg
            price = incremental_cost / delta if incremental_cost >= ZERO else event.average_price
        else:
            incremental_cost = delta * price
            new_cost = quantity * old_avg + incremental_cost
        target = (
            OrderStatus.FILLED
            if new_qty == decimal_from_db(raw_order_qty)
            else OrderStatus.PARTIALLY_FILLED
        )
        transitioned = apply_transition(current, OrderEvent(target, filled_qty=delta))
        next_status = transitioned.status if not transitioned.illegal else current
        cycle_id = None
        if intent_id is not None:
            row = conn.execute(
                "SELECT cycle_id FROM order_intents WHERE account_scope_id=? AND intent_id=?",
                (scope, intent_id),
            ).fetchone()
            cycle_id = row[0] if row else None
        position, drift = self._change_position(
            conn, event, order, cycle_id, side, delta, price, now, key
        )
        if drift is not None:
            return IngestResult("DRIFT", key, drift=drift), None
        conn.execute(
            "INSERT INTO fills(fill_id,account_scope_id,external_activity_id,broker_order_id,"
            "cycle_id,symbol,side,quantity,price,fee,filled_at,recorded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"{scope}:{key}",
                scope,
                key,
                broker_id,
                cycle_id,
                symbol,
                side.value,
                decimal_to_db(delta),
                decimal_to_db(price),
                decimal_to_db(event.fee),
                to_db_timestamp(event.occurred_at),
                to_db_timestamp(now),
            ),
        )
        conn.execute(
            "UPDATE broker_orders SET status=?,filled_quantity=?,filled_avg_price=?,updated_at=? "
            "WHERE account_scope_id=? AND broker_order_id=?",
            (
                self._broker_status(next_status),
                decimal_to_db(new_qty),
                decimal_to_db(new_cost / new_qty),
                to_db_timestamp(event.occurred_at),
                scope,
                broker_id,
            ),
        )
        self._settle_entry_reservation(
            conn, intent_id, delta, price, quantity, old_avg, new_qty, raw_order_qty, now
        )
        self._emit(
            "FillApplied",
            cycle_id or broker_id,
            {
                "dedupe_key": key,
                "broker_order_id": broker_id,
                "client_order_id": event.client_order_id,
                "symbol": symbol,
                "side": side,
                "incremental_qty": delta,
                "cumulative_qty": new_qty,
                "price": price,
                "remaining_quantity": position.remaining_qty if position else ZERO,
            },
            event.occurred_at,
            f"fill-ingest:{key}",
        )
        notice = {
            "type": "positive-fill-confirmed",
            "client_order_id": event.client_order_id,
            "broker_order_id": broker_id,
            "symbol": symbol,
            "side": side.value,
            "incremental_qty": delta,
            "remaining_qty": position.remaining_qty if position else ZERO,
            "cycle_id": cycle_id,
        }
        return IngestResult("APPLIED", key, delta, next_status, position), notice

    def _change_position(self, conn, event, order, cycle_id, side, delta, price, now, key):
        scope = self._writer.account_scope_id
        symbol = order[2]
        if event.symbol and event.symbol.upper() != symbol:
            raise ValueError("fill symbol does not match order")
        if event.side is not None and event.side is not side:
            raise ValueError("fill side does not match order")
        row = conn.execute(
            "SELECT quantity,average_entry_price FROM positions "
            "WHERE account_scope_id=? AND symbol=?",
            (scope, symbol),
        ).fetchone()
        held = decimal_from_db(row[0]) if row else ZERO
        avg = decimal_from_db(row[1]) if row else ZERO
        cost = held * avg
        if side is OrderSide.SELL and delta > held:
            drift = self._drift(
                conn,
                key,
                event,
                now,
                "CRITICAL_OVERSELL",
                {"held_qty": held},
                {"sell_qty": delta, "client_order_id": event.client_order_id},
            )
            if cycle_id:
                conn.execute(
                    "UPDATE position_cycles SET state='EXITING',updated_at=? "
                    "WHERE account_scope_id=? AND cycle_id=? AND state!='CLOSED'",
                    (to_db_timestamp(now), scope, cycle_id),
                )
            return None, drift
        new_held = held + delta if side is OrderSide.BUY else held - delta
        new_cost = cost + delta * price if side is OrderSide.BUY else cost - avg * delta
        if new_held == ZERO:
            if cycle_id:
                conn.execute(
                    "UPDATE position_cycles SET state='CLOSED',closed_at=?,updated_at=? "
                    "WHERE account_scope_id=? AND cycle_id=?",
                    (to_db_timestamp(event.occurred_at), to_db_timestamp(now), scope, cycle_id),
                )
            conn.execute(
                "DELETE FROM positions WHERE account_scope_id=? AND symbol=?", (scope, symbol)
            )
            return PositionFillState(cycle_id or "", symbol, ZERO, ZERO, None), None
        average = new_cost / new_held
        if cycle_id:
            state = "OPEN" if side is OrderSide.BUY else "EXITING"
            conn.execute(
                "UPDATE position_cycles SET state=?,opened_at=COALESCE(opened_at,?),updated_at=? "
                "WHERE account_scope_id=? AND cycle_id=?",
                (state, to_db_timestamp(event.occurred_at), to_db_timestamp(now), scope, cycle_id),
            )
        conn.execute(
            "INSERT INTO positions(account_scope_id,symbol,cycle_id,management,direction,"
            "quantity,average_entry_price,market_price,market_value,unrealized_pl,as_of) "
            "VALUES (?,?,?,'MANAGED','LONG',?,?,NULL,NULL,NULL,?) "
            "ON CONFLICT(account_scope_id,symbol) DO UPDATE SET "
            "cycle_id=excluded.cycle_id,management='MANAGED',direction='LONG',"
            "quantity=excluded.quantity,average_entry_price=excluded.average_entry_price,"
            "as_of=excluded.as_of",
            (
                scope,
                symbol,
                cycle_id,
                decimal_to_db(new_held),
                decimal_to_db(average),
                to_db_timestamp(event.occurred_at),
            ),
        )
        return PositionFillState(cycle_id or "", symbol, new_held, new_cost, average), None

    def _settle_entry_reservation(
        self, conn, intent_id, delta, price, prior_qty, prior_avg, new_qty, raw_order_qty, now
    ):
        if not intent_id:
            return
        scope = self._writer.account_scope_id
        intent = conn.execute(
            "SELECT side,quantity,notional FROM order_intents "
            "WHERE account_scope_id=? AND intent_id=?",
            (scope, intent_id),
        ).fetchone()
        if not intent or intent[0] != "BUY":
            return
        reservation_id = f"rsv:{intent_id}"
        row = conn.execute(
            "SELECT amount,state FROM budget_reservations "
            "WHERE account_scope_id=? AND reservation_id=?",
            (scope, reservation_id),
        ).fetchone()
        if not row or row[1] != "ACTIVE":
            return
        remainder = decimal_from_db(row[0])
        target = decimal_from_db(intent[1]) if intent[1] is not None else decimal_from_db(intent[2])
        share = (
            delta / (target - prior_qty)
            if intent[1] is not None and target > prior_qty
            else (delta * price) / (target - prior_qty * prior_avg)
            if target > prior_qty * prior_avg
            else Decimal("1")
        )
        if new_qty >= decimal_from_db(raw_order_qty):
            conn.execute(
                "UPDATE budget_reservations SET amount='0',state='CONSUMED',resolved_at=? "
                "WHERE account_scope_id=? AND reservation_id=?",
                (to_db_timestamp(now), scope, reservation_id),
            )
        else:
            consumed_fraction = min(Decimal("1"), max(ZERO, share))
            conn.execute(
                "UPDATE budget_reservations SET amount=? WHERE account_scope_id=? "
                "AND reservation_id=?",
                (
                    decimal_to_db(remainder * (Decimal("1") - consumed_fraction)),
                    scope,
                    reservation_id,
                ),
            )

    def _release_reservation(self, conn, intent_id, now):
        if not intent_id:
            return
        conn.execute(
            "UPDATE budget_reservations SET amount='0',state='RELEASED',resolved_at=? "
            "WHERE account_scope_id=? AND reservation_id=? AND state='ACTIVE'",
            (to_db_timestamp(now), self._writer.account_scope_id, f"rsv:{intent_id}"),
        )

    def _drift(self, conn, key, event, now, kind, expected, actual):
        scope = self._writer.account_scope_id
        run_id, drift_id = f"fill:{key}", uuid4().hex
        if not conn.execute(
            "SELECT 1 FROM reconciliation_runs WHERE account_scope_id=? "
            "AND reconciliation_run_id=?",
            (scope, run_id),
        ).fetchone():
            conn.execute(
                "INSERT INTO reconciliation_runs(reconciliation_run_id,account_scope_id,status,"
                "started_at,completed_at,summary_json) VALUES (?,?, 'DRIFTED',?,?,?)",
                (
                    run_id,
                    scope,
                    to_db_timestamp(now),
                    to_db_timestamp(now),
                    canonical_payload_json({"source": "fill_processor"}),
                ),
            )
        conn.execute(
            "INSERT INTO reconciliation_drifts(drift_id,account_scope_id,reconciliation_run_id,"
            "drift_type,entity_ref,expected_json,actual_json,detected_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                drift_id,
                scope,
                run_id,
                kind,
                event.client_order_id,
                canonical_payload_json(expected),
                canonical_payload_json(actual),
                to_db_timestamp(now),
            ),
        )
        conn.execute(
            "INSERT INTO entry_controls(account_scope_id,kill_switch_enabled,paused,reason,"
            "updated_by,updated_at) VALUES (?,1,1,?,?,?) "
            "ON CONFLICT(account_scope_id) DO UPDATE SET kill_switch_enabled=1,paused=1,"
            "reason=excluded.reason,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (scope, f"reconciliation drift: {kind}", "fill_processor", to_db_timestamp(now)),
        )
        self._emit(
            "ReconciliationDriftRecorded",
            run_id,
            {
                "drift_id": drift_id,
                "drift_type": kind,
                "client_order_id": event.client_order_id,
                "dedupe_key": key,
            },
            event.occurred_at,
            f"fill-drift:{key}",
        )
        return ReconciliationDrift(
            drift_id, kind, event.client_order_id, {"expected": expected, "actual": actual}
        )

    def _emit(self, event_type, stream_id, payload, occurred_at, idem):
        return self._journal.append_in_transaction(
            [
                NewEvent(
                    self._writer.account_scope_id,
                    "fill_processor",
                    str(stream_id),
                    event_type,
                    payload,
                    occurred_at,
                    idempotency_key=idem,
                )
            ]
        )
