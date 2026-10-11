"""Account-scoped write repositories over the single-writer transaction layer.

Every repository takes a ``ScopedWriter`` and an injected clock, converts values
with ``schema.decimal_to_db`` / ``schema.to_db_timestamp``, and appends a journal
event in the same unit of work as every trade-affecting mutation.  Order intent
is committed (with its budget reservation) before any broker submit callable
runs; fills are applied exactly once per ``(account_scope_id, external_activity_id)``.

Design choices (5, the task's decision budget):
1. Fill identity and ledger.  ``Fill.fill_id`` *is* the broker's
   ``external_activity_id`` (``fills.fill_id`` is the surrogate
   ``"<scope>:<activity id>"``), and a fill's cycle is resolved through its
   broker order's intent.  ``broker_orders.filled_quantity`` is derived from the
   fills ledger and never decreases; broker snapshots older than the stored
   ``updated_at`` are ignored.
2. Reservation lifecycle.  Only ENTRY intents are budget-checked and reserve
   (``rsv:<intent_id>``); other purposes must pass a zero amount and never
   block.  An ACTIVE reservation's ``amount`` is the unconsumed remainder:
   entry fills consume it in proportion to the fill's share of the intent's
   remaining quantity (or notional), CONSUMED once the intent completes.  It is
   RELEASED when its order ends unfilled-in-part, its cycle closes, or a submit
   definitively fails.  Exposure is ACTIVE reservations plus open-position cost.
3. Submit outcomes.  ``BrokerRejectedError`` marks SUBMIT_FAILED (reservation
   released); any other exception marks UNKNOWN (reservation kept, since the
   order may exist).  The original exception is re-raised after the durable
   mark, success marks SUBMITTED, and only a RECORDED intent with no earlier
   submit-request event may be submitted.
4. Derived rows.  ``positions`` holds a row only for an open cycle (deleted on
   close) priced at the cycle's average entry.  Exit fills shrink active
   trailing coverage to the remaining quantity; entry fills never grow it.
   Closing marks the trailing state TRIGGERED if the filling order is its
   broker order, else CANCELED.
5. Entry controls.  Counters are keyed ``(symbol, session_date, strategy or '')``
   and carry ``consecutive_losses`` forward from the latest earlier row.  A
   loss is ``realized_pnl < 0``; anything else resets the streak.  Reaching the
   threshold inserts one indefinite lockout (``locked_until`` NULL) per
   ``(symbol, strategy scope)`` until cleared.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol
from uuid import uuid4

from mike_trade.domain.accounts import (
    AccountEnvironment,
    AccountScope,
    AccountSnapshot,
    ControlCommand,
    HealthSnapshot,
    HealthStatus,
)
from mike_trade.domain.errors import (
    BrokerRejectedError,
    EntryBlockedError,
    ModeIsolationError,
    PersistenceError,
)
from mike_trade.domain.protection import (
    ProtectionKind,
    ProtectionStatus,
    TrailingStopState,
)
from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    BudgetPolicy,
    EntryEligibilityPolicy,
    EntryEligibilityState,
    Fill,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    PositionCycle,
    PositionCycleState,
    PositionDirection,
    TimeInForce,
)
from mike_trade.persistence.journal import (
    DomainEventJournal,
    NewEvent,
    RecordedEvent,
    canonical_payload_json,
    commit_then_effect,
)
from mike_trade.persistence.schema import (
    decimal_from_db,
    decimal_to_db,
    from_db_timestamp,
    to_db_timestamp,
)
from mike_trade.persistence.transaction import ScopedWriter

__all__ = [
    "AccountScopeRepository",
    "RunConfigRepository",
    "OrderIntentRepository",
    "IntentReceipt",
    "BrokerOrderRepository",
    "FillRepository",
    "FillApplyResult",
    "PositionCycleRepository",
    "ProtectionRepository",
    "EntryControlRepository",
    "OperatorCommandRepository",
    "ReconciliationRepository",
    "StrategyStateRepository",
    "CashEquityRepository",
    "ServiceHealthRepository",
    "BudgetExceededError",
]

_ZERO = Decimal("0")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NON_TERMINAL_PROTECTION = ("PENDING", "ACTIVE", "REPLACING", "UNKNOWN")
_UNFILLED_END_STATUSES = frozenset(
    {
        BrokerOrderStatus.CANCELED,
        BrokerOrderStatus.EXPIRED,
        BrokerOrderStatus.REJECTED,
        BrokerOrderStatus.REPLACED,
    }
)


class BudgetExceededError(EntryBlockedError):
    """A new entry reservation would exceed the total or per-symbol budget."""


class _Clock(Protocol):
    def now(self) -> datetime: ...


Clock = _Clock | Callable[[], datetime]


def _read_clock(clock: Clock) -> datetime:
    now_method = getattr(clock, "now", None)
    value = now_method() if callable(now_method) else clock()  # type: ignore[operator]
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PersistenceError("clock must return an aware datetime")
    return value.astimezone(UTC)


def _opt_dec(text: str | None) -> Decimal | None:
    return None if text is None else decimal_from_db(text)


def _opt_dec_to_db(value: Decimal | None) -> str | None:
    return None if value is None else decimal_to_db(value)


def _symbol(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("symbol must be a non-empty str")
    return value.strip().upper()


def _reservation_id(intent_id: str) -> str:
    return f"rsv:{intent_id}"


class _Repository:
    """Shared plumbing: scope, clock, and same-transaction journal appends."""

    def __init__(self, writer: ScopedWriter, clock: Clock) -> None:
        self._w = writer
        self._clock = clock
        self._journal = DomainEventJournal(writer.connection, clock=clock)

    @property
    def _scope(self) -> str:
        return self._w.account_scope_id

    def _now(self) -> datetime:
        return _read_clock(self._clock)

    def _emit(
        self,
        event_type: str,
        stream_type: str,
        stream_id: str,
        payload: Mapping[str, Any],
        *,
        occurred_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> RecordedEvent:
        result = self._journal.append_in_transaction(
            [
                NewEvent(
                    account_scope_id=self._scope,
                    stream_type=stream_type,
                    stream_id=stream_id,
                    event_type=event_type,
                    payload=payload,
                    occurred_at=occurred_at if occurred_at is not None else self._now(),
                    idempotency_key=idempotency_key,
                )
            ]
        )
        return result.recorded_events[0]


# --------------------------------------------------------------------------- scopes


class AccountScopeRepository(_Repository):
    def register_scope(
        self, scope: AccountScope, *, broker: str | None = None, run_id: str | None = None
    ) -> AccountScope:
        """Idempotently register this writer's account scope."""
        self._w.require_scope(scope.scope_id)
        if scope.environment is not self._w.environment:
            raise ModeIsolationError(
                f"scope environment {scope.environment.value!r} differs from database "
                f"environment {self._w.environment.value!r}"
            )
        backtest_run = run_id
        if backtest_run is None and scope.environment is AccountEnvironment.BACKTEST:
            backtest_run = scope.scenario
        with self._w.unit_of_work() as conn:
            exists = conn.execute(
                "SELECT 1 FROM account_scopes WHERE account_scope_id = ?", (self._scope,)
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO account_scopes(account_scope_id, environment, broker, "
                    "broker_account_ref, scenario_name, run_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._scope,
                        scope.environment.value,
                        broker,
                        scope.account_id,
                        scope.scenario,
                        backtest_run,
                        to_db_timestamp(self._now()),
                    ),
                )
        return scope

    def get_scope(self) -> AccountScope | None:
        row = self._w.connection.execute(
            "SELECT environment, broker_account_ref, scenario_name FROM account_scopes "
            "WHERE account_scope_id = ?",
            (self._scope,),
        ).fetchone()
        if row is None:
            return None
        return AccountScope(AccountEnvironment(row[0]), row[1], row[2])


class RunConfigRepository(_Repository):
    def record_run_config(
        self, config: Mapping[str, Any], *, fingerprint_sha256: str | None = None
    ) -> str:
        """Store a run config once per fingerprint; returns the fingerprint."""
        config_json = canonical_payload_json(config)
        fingerprint = fingerprint_sha256 or hashlib.sha256(config_json.encode("utf-8")).hexdigest()
        if not _SHA256_RE.fullmatch(fingerprint):
            raise ValueError("fingerprint_sha256 must be 64 lowercase hex characters")
        now = self._now()
        with self._w.unit_of_work() as conn:
            exists = conn.execute(
                "SELECT 1 FROM run_configs WHERE account_scope_id = ? "
                "AND config_fingerprint_sha256 = ?",
                (self._scope, fingerprint),
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO run_configs(run_config_id, account_scope_id, "
                    "config_fingerprint_sha256, config_json, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (uuid4().hex, self._scope, fingerprint, config_json, to_db_timestamp(now)),
                )
                self._emit(
                    "RunConfigRecorded",
                    "run_config",
                    fingerprint,
                    {"config_fingerprint_sha256": fingerprint},
                    occurred_at=now,
                    idempotency_key=f"run_config:{fingerprint}",
                )
        return fingerprint


# ----------------------------------------------------------------- shared row helpers


def _load_intent(conn: sqlite3.Connection, scope: str, intent_id: str) -> OrderIntent | None:
    row = conn.execute(
        "SELECT intent_id, client_order_id, account_scope_id, cycle_id, symbol, side, purpose, "
        "order_type, time_in_force, created_at, quantity, notional, limit_price, stop_price, "
        "trail_price, trail_percent, signal_id FROM order_intents "
        "WHERE account_scope_id = ? AND intent_id = ?",
        (scope, intent_id),
    ).fetchone()
    if row is None:
        return None
    return OrderIntent(
        intent_id=row[0],
        client_order_id=row[1],
        account_scope_id=row[2],
        cycle_id=row[3],
        symbol=row[4],
        side=OrderSide(row[5]),
        purpose=OrderPurpose(row[6]),
        order_type=OrderType(row[7]),
        time_in_force=TimeInForce(row[8]),
        created_at=from_db_timestamp(row[9]),
        quantity=_opt_dec(row[10]),
        notional=_opt_dec(row[11]),
        limit_price=_opt_dec(row[12]),
        stop_price=_opt_dec(row[13]),
        trail_price=_opt_dec(row[14]),
        trail_percent=_opt_dec(row[15]),
        signal_id=row[16],
    )


def _load_cycle(conn: sqlite3.Connection, scope: str, cycle_id: str) -> PositionCycle | None:
    row = conn.execute(
        "SELECT cycle_id, symbol, direction, state, entry_strategy_key, opened_at, closed_at "
        "FROM position_cycles WHERE account_scope_id = ? AND cycle_id = ?",
        (scope, cycle_id),
    ).fetchone()
    if row is None:
        return None
    entries: list[Fill] = []
    exits: list[Fill] = []
    fill_rows = conn.execute(
        "SELECT external_activity_id, broker_order_id, symbol, side, quantity, price, fee, "
        "filled_at FROM fills WHERE account_scope_id = ? AND cycle_id = ? "
        "ORDER BY filled_at, rowid",
        (scope, cycle_id),
    ).fetchall()
    for activity_id, order_id, symbol, side, quantity, price, fee, filled_at in fill_rows:
        fill = Fill(
            fill_id=activity_id,
            broker_order_id=order_id,
            account_scope_id=scope,
            symbol=symbol,
            side=OrderSide(side),
            quantity=decimal_from_db(quantity),
            price=decimal_from_db(price),
            fee=decimal_from_db(fee),
            filled_at=from_db_timestamp(filled_at),
        )
        (entries if fill.side.is_entry else exits).append(fill)
    return PositionCycle(
        cycle_id=row[0],
        account_scope_id=scope,
        symbol=row[1],
        direction=PositionDirection(row[2]),
        state=PositionCycleState(row[3]),
        entry_strategy_key=row[4],
        entry_fills=tuple(entries),
        exit_fills=tuple(exits),
        opened_at=None if row[5] is None else from_db_timestamp(row[5]),
        closed_at=None if row[6] is None else from_db_timestamp(row[6]),
    )


def _release_reservations(
    conn: sqlite3.Connection,
    scope: str,
    now: datetime,
    *,
    reservation_id: str | None = None,
    cycle_id: str | None = None,
) -> int:
    """Release ACTIVE reservations by id or by cycle; returns how many changed."""
    if reservation_id is not None:
        where, arg = "reservation_id = ?", reservation_id
    elif cycle_id is not None:
        where, arg = "cycle_id = ?", cycle_id
    else:
        raise ValueError("reservation_id or cycle_id is required")
    cursor = conn.execute(
        f"UPDATE budget_reservations SET state = 'RELEASED', resolved_at = ? "
        f"WHERE account_scope_id = ? AND state = 'ACTIVE' AND {where}",
        (to_db_timestamp(now), scope, arg),
    )
    return cursor.rowcount


# ------------------------------------------------------------------------- cycles


class PositionCycleRepository(_Repository):
    def open_cycle(self, cycle: PositionCycle) -> PositionCycle:
        """Persist a new PENDING_ENTRY cycle."""
        self._w.require_scope(cycle.account_scope_id)
        if cycle.state is not PositionCycleState.PENDING_ENTRY:
            raise ValueError("open_cycle requires a PENDING_ENTRY cycle")
        now = self._now()
        with self._w.unit_of_work() as conn:
            conn.execute(
                "INSERT INTO position_cycles(cycle_id, account_scope_id, symbol, direction, "
                "state, entry_strategy_key, opened_at, closed_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
                (
                    cycle.cycle_id,
                    self._scope,
                    cycle.symbol,
                    cycle.direction.value,
                    cycle.state.value,
                    cycle.entry_strategy_key,
                    to_db_timestamp(now),
                    to_db_timestamp(now),
                ),
            )
            self._emit(
                "PositionCycleOpened",
                "position_cycle",
                cycle.cycle_id,
                {
                    "cycle_id": cycle.cycle_id,
                    "symbol": cycle.symbol,
                    "direction": cycle.direction,
                    "entry_strategy_key": cycle.entry_strategy_key,
                },
                occurred_at=now,
            )
        return cycle

    def get_cycle(self, cycle_id: str) -> PositionCycle | None:
        return _load_cycle(self._w.connection, self._scope, cycle_id)

    def list_open(self, symbol: str | None = None) -> list[PositionCycle]:
        """Cycles holding a position (OPEN or EXITING), oldest first."""
        query = (
            "SELECT cycle_id FROM position_cycles WHERE account_scope_id = ? "
            "AND state IN ('OPEN', 'EXITING')"
        )
        args: list[str] = [self._scope]
        if symbol is not None:
            query += " AND symbol = ?"
            args.append(_symbol(symbol))
        rows = self._w.connection.execute(query + " ORDER BY opened_at, rowid", args).fetchall()
        cycles = (self.get_cycle(row[0]) for row in rows)
        return [cycle for cycle in cycles if cycle is not None]


# ------------------------------------------------------------------------- intents


@dataclass(frozen=True, slots=True)
class IntentReceipt:
    """Proof that an intent (and its reservation and event) is durably committed."""

    intent: OrderIntent
    reservation_id: str | None
    event_seq: int
    recorded_at: datetime
    duplicate: bool = False


class OrderIntentRepository(_Repository):
    def __init__(
        self, writer: ScopedWriter, clock: Clock, budget_policy: BudgetPolicy
    ) -> None:
        super().__init__(writer, clock)
        self._budget = budget_policy

    def record_intent(self, intent: OrderIntent, reservation_amount: Decimal) -> IntentReceipt:
        """Atomically budget-check and persist intent, reservation, and journal event."""
        self._w.require_scope(intent.account_scope_id)
        amount = reservation_amount
        if not isinstance(amount, Decimal) or not amount.is_finite() or amount < _ZERO:
            raise ValueError("reservation_amount must be a finite non-negative Decimal")
        is_entry = intent.purpose is OrderPurpose.ENTRY
        if is_entry and amount <= _ZERO:
            raise ValueError("ENTRY intents require a positive reservation_amount")
        if not is_entry and amount != _ZERO:
            raise ValueError("only ENTRY intents may reserve budget")
        now = self._now()
        reservation_id = _reservation_id(intent.intent_id) if is_entry else None
        with self._w.unit_of_work() as conn:
            existing = conn.execute(
                "SELECT intent_id FROM order_intents "
                "WHERE account_scope_id = ? AND client_order_id = ?",
                (self._scope, intent.client_order_id),
            ).fetchone()
            if existing is not None:
                if existing[0] != intent.intent_id:
                    raise PersistenceError(
                        f"client_order_id {intent.client_order_id!r} belongs to another intent"
                    )
                event = self._journal.get_by_idempotency_key(self._scope, intent.client_order_id)
                return IntentReceipt(
                    intent, reservation_id, 0 if event is None else event.event_seq, now, True
                )
            cycle = conn.execute(
                "SELECT symbol, state FROM position_cycles "
                "WHERE account_scope_id = ? AND cycle_id = ?",
                (self._scope, intent.cycle_id),
            ).fetchone()
            if cycle is None:
                raise PersistenceError(f"unknown cycle {intent.cycle_id!r}")
            if cycle[0] != intent.symbol:
                raise ValueError("intent symbol must match its cycle")
            if is_entry and cycle[1] not in ("PENDING_ENTRY", "OPEN"):
                raise ValueError(f"entry intents are not allowed for a {cycle[1]} cycle")
            if is_entry:
                self._check_budget(conn, intent.symbol, amount)
                conn.execute(
                    "INSERT INTO budget_reservations(reservation_id, account_scope_id, cycle_id, "
                    "amount, currency, state, created_at, resolved_at) "
                    "VALUES (?, ?, ?, ?, 'USD', 'ACTIVE', ?, NULL)",
                    (
                        reservation_id,
                        self._scope,
                        intent.cycle_id,
                        decimal_to_db(amount),
                        to_db_timestamp(now),
                    ),
                )
            conn.execute(
                "INSERT INTO order_intents(intent_id, account_scope_id, cycle_id, signal_id, "
                "client_order_id, symbol, side, purpose, order_type, time_in_force, quantity, "
                "notional, limit_price, stop_price, trail_price, trail_percent, status, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RECORDED', ?, ?)",
                (
                    intent.intent_id,
                    self._scope,
                    intent.cycle_id,
                    intent.signal_id,
                    intent.client_order_id,
                    intent.symbol,
                    intent.side.value,
                    intent.purpose.value,
                    intent.order_type.value,
                    intent.time_in_force.value,
                    _opt_dec_to_db(intent.quantity),
                    _opt_dec_to_db(intent.notional),
                    _opt_dec_to_db(intent.limit_price),
                    _opt_dec_to_db(intent.stop_price),
                    _opt_dec_to_db(intent.trail_price),
                    _opt_dec_to_db(intent.trail_percent),
                    to_db_timestamp(intent.created_at),
                    to_db_timestamp(now),
                ),
            )
            event = self._emit(
                "OrderIntentRecorded",
                "order_intent",
                intent.intent_id,
                {
                    "intent_id": intent.intent_id,
                    "client_order_id": intent.client_order_id,
                    "cycle_id": intent.cycle_id,
                    "symbol": intent.symbol,
                    "side": intent.side,
                    "purpose": intent.purpose,
                    "order_type": intent.order_type,
                    "time_in_force": intent.time_in_force,
                    "quantity": intent.quantity,
                    "notional": intent.notional,
                    "limit_price": intent.limit_price,
                    "stop_price": intent.stop_price,
                    "trail_price": intent.trail_price,
                    "trail_percent": intent.trail_percent,
                    "signal_id": intent.signal_id,
                    "reservation_id": reservation_id,
                    "reservation_amount": amount,
                },
                occurred_at=intent.created_at,
                idempotency_key=intent.client_order_id,
            )
        return IntentReceipt(intent, reservation_id, event.event_seq, now)

    def _check_budget(self, conn: sqlite3.Connection, symbol: str, amount: Decimal) -> None:
        """Reservations plus open-position cost must stay within the account budgets."""
        total = _ZERO
        by_symbol = _ZERO
        reserved = conn.execute(
            "SELECT c.symbol, r.amount FROM budget_reservations r "
            "LEFT JOIN position_cycles c ON c.account_scope_id = r.account_scope_id "
            "AND c.cycle_id = r.cycle_id WHERE r.account_scope_id = ? AND r.state = 'ACTIVE'",
            (self._scope,),
        ).fetchall()
        for row_symbol, row_amount in reserved:
            value = decimal_from_db(row_amount)
            total += value
            if row_symbol == symbol:
                by_symbol += value
        held = conn.execute(
            "SELECT symbol, quantity, average_entry_price FROM positions "
            "WHERE account_scope_id = ?",
            (self._scope,),
        ).fetchall()
        for row_symbol, quantity, average in held:
            cost = decimal_from_db(quantity) * decimal_from_db(average)
            total += cost
            if row_symbol == symbol:
                by_symbol += cost
        if total + amount > self._budget.total_application_budget:
            raise BudgetExceededError(
                "reservation would exceed total_application_budget",
                code="budget_exceeded",
                context={"limit": "total", "committed": str(total), "requested": str(amount)},
            )
        if by_symbol + amount > self._budget.per_symbol_budget:
            raise BudgetExceededError(
                f"reservation would exceed per_symbol_budget for {symbol}",
                code="budget_exceeded",
                context={
                    "limit": "per_symbol",
                    "symbol": symbol,
                    "committed": str(by_symbol),
                    "requested": str(amount),
                },
            )

    def submit_recorded_intent(
        self, receipt: IntentReceipt, submit: Callable[[IntentReceipt], str | None]
    ) -> str | None:
        """Journal the submit request, then call ``submit`` only for a durable intent.

        ``submit`` may return the broker order id.  On failure the intent is
        marked SUBMIT_FAILED or UNKNOWN (never deleted) and the error re-raised.
        """
        intent = receipt.intent
        self._w.require_scope(intent.account_scope_id)
        row = self._w.connection.execute(
            "SELECT status FROM order_intents "
            "WHERE account_scope_id = ? AND intent_id = ? AND client_order_id = ?",
            (self._scope, intent.intent_id, intent.client_order_id),
        ).fetchone()
        if row is None:
            raise PersistenceError("intent is not durably recorded; refusing to submit")
        if row[0] != "RECORDED":
            raise PersistenceError(f"intent is {row[0]}, only a RECORDED intent may be submitted")
        request_key = f"submit:{intent.client_order_id}"
        if self._journal.get_by_idempotency_key(self._scope, request_key) is not None:
            self._mark_failure(receipt, "UNKNOWN", "earlier submit request has no outcome")
            raise PersistenceError("an earlier submit attempt has an unknown outcome")

        outcome: list[str | None] = []

        def effect(_events: tuple[RecordedEvent, ...]) -> None:
            try:
                broker_order_id = submit(receipt)
            except BaseException as exc:
                status = "SUBMIT_FAILED" if isinstance(exc, BrokerRejectedError) else "UNKNOWN"
                try:
                    self._mark_failure(receipt, status, repr(exc))
                except Exception as mark_error:
                    exc.add_note(f"could not persist submit outcome: {mark_error!r}")
                raise
            self.mark_submitted(receipt, broker_order_id=broker_order_id)
            outcome.append(broker_order_id)

        commit_then_effect(
            self._journal,
            [
                NewEvent(
                    account_scope_id=self._scope,
                    stream_type="order_intent",
                    stream_id=intent.intent_id,
                    event_type="OrderSubmitRequested",
                    payload={
                        "intent_id": intent.intent_id,
                        "client_order_id": intent.client_order_id,
                    },
                    occurred_at=self._now(),
                    idempotency_key=request_key,
                )
            ],
            effect,
        )
        return outcome[0]

    def mark_submitted(self, receipt: IntentReceipt, broker_order_id: str | None = None) -> None:
        intent = receipt.intent
        self._w.require_scope(intent.account_scope_id)
        now = self._now()
        with self._w.unit_of_work() as conn:
            cursor = conn.execute(
                "UPDATE order_intents SET status = 'SUBMITTED', updated_at = ? "
                "WHERE account_scope_id = ? AND intent_id = ? "
                "AND status IN ('RECORDED', 'UNKNOWN')",
                (to_db_timestamp(now), self._scope, intent.intent_id),
            )
            if cursor.rowcount == 0:
                row = conn.execute(
                    "SELECT status FROM order_intents WHERE account_scope_id = ? "
                    "AND intent_id = ?",
                    (self._scope, intent.intent_id),
                ).fetchone()
                if row is not None and row[0] == "SUBMITTED":
                    return
                raise PersistenceError(f"intent cannot be marked submitted from {row}")
            self._emit(
                "OrderIntentSubmitted",
                "order_intent",
                intent.intent_id,
                {
                    "intent_id": intent.intent_id,
                    "client_order_id": intent.client_order_id,
                    "broker_order_id": broker_order_id,
                },
                occurred_at=now,
            )

    def _mark_failure(self, receipt: IntentReceipt, status: str, detail: str) -> None:
        intent = receipt.intent
        now = self._now()
        with self._w.unit_of_work() as conn:
            cursor = conn.execute(
                "UPDATE order_intents SET status = ?, updated_at = ? "
                "WHERE account_scope_id = ? AND intent_id = ? AND status = 'RECORDED'",
                (status, to_db_timestamp(now), self._scope, intent.intent_id),
            )
            if cursor.rowcount == 0:
                return
            released = 0
            if status == "SUBMIT_FAILED":
                released = _release_reservations(
                    conn, self._scope, now, reservation_id=_reservation_id(intent.intent_id)
                )
            self._emit(
                "OrderIntentSubmitFailed" if status == "SUBMIT_FAILED" else "OrderIntentUnknown",
                "order_intent",
                intent.intent_id,
                {
                    "intent_id": intent.intent_id,
                    "client_order_id": intent.client_order_id,
                    "status": status,
                    "detail": detail,
                    "reservation_released": bool(released),
                },
                occurred_at=now,
            )


# ------------------------------------------------------------------- broker orders


class BrokerOrderRepository(_Repository):
    def upsert_broker_order(
        self,
        order: BrokerOrder,
        *,
        intent_id: str | None = None,
        raw: Mapping[str, Any] | None = None,
    ) -> bool:
        """Insert or refresh an order snapshot; returns False for a stale snapshot."""
        self._w.require_scope(order.account_scope_id)
        now = self._now()
        raw_json = None if raw is None else canonical_payload_json(raw)
        with self._w.unit_of_work() as conn:
            if intent_id is None:
                found = conn.execute(
                    "SELECT intent_id FROM order_intents "
                    "WHERE account_scope_id = ? AND client_order_id = ?",
                    (self._scope, order.client_order_id),
                ).fetchone()
                intent_id = None if found is None else found[0]
            existing = conn.execute(
                "SELECT status, filled_quantity, filled_avg_price, updated_at, intent_id "
                "FROM broker_orders WHERE account_scope_id = ? AND broker_order_id = ?",
                (self._scope, order.broker_order_id),
            ).fetchone()
            values = (
                order.status.value,
                decimal_to_db(order.quantity),
                _opt_dec_to_db(order.limit_price),
                _opt_dec_to_db(order.stop_price),
                _opt_dec_to_db(order.trail_price),
                _opt_dec_to_db(order.trail_percent),
            )
            if existing is None:
                conn.execute(
                    "INSERT INTO broker_orders(account_scope_id, broker_order_id, intent_id, "
                    "client_order_id, symbol, side, order_type, status, quantity, "
                    "filled_quantity, filled_avg_price, limit_price, stop_price, trail_price, "
                    "trail_percent, submitted_at, updated_at, raw_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._scope,
                        order.broker_order_id,
                        intent_id,
                        order.client_order_id,
                        order.symbol,
                        order.side.value,
                        order.order_type.value,
                        values[0],
                        values[1],
                        decimal_to_db(order.filled_quantity),
                        _opt_dec_to_db(order.filled_avg_price),
                        values[2],
                        values[3],
                        values[4],
                        values[5],
                        to_db_timestamp(order.submitted_at),
                        to_db_timestamp(order.updated_at),
                        raw_json,
                    ),
                )
                event_type: str | None = "CREATED"
            else:
                if order.updated_at < from_db_timestamp(existing[3]):
                    return False
                stored_filled = decimal_from_db(existing[1])
                advanced = order.filled_quantity >= stored_filled
                filled = order.filled_quantity if advanced else stored_filled
                average = order.filled_avg_price if advanced else _opt_dec(existing[2])
                conn.execute(
                    "UPDATE broker_orders SET status = ?, quantity = ?, limit_price = ?, "
                    "stop_price = ?, trail_price = ?, trail_percent = ?, filled_quantity = ?, "
                    "filled_avg_price = ?, updated_at = ?, raw_json = ?, "
                    "intent_id = COALESCE(intent_id, ?) "
                    "WHERE account_scope_id = ? AND broker_order_id = ?",
                    (
                        *values,
                        decimal_to_db(filled),
                        _opt_dec_to_db(average),
                        to_db_timestamp(order.updated_at),
                        raw_json,
                        intent_id,
                        self._scope,
                        order.broker_order_id,
                    ),
                )
                event_type = "STATUS_CHANGED" if existing[0] != order.status.value else None
                intent_id = existing[4] if existing[4] is not None else intent_id
            if event_type is not None:
                snapshot = {
                    "broker_order_id": order.broker_order_id,
                    "client_order_id": order.client_order_id,
                    "status": order.status,
                    "previous_status": None if existing is None else existing[0],
                    "quantity": order.quantity,
                    "filled_quantity": order.filled_quantity,
                    "replaced_by": order.replaced_by,
                }
                conn.execute(
                    "INSERT INTO broker_order_events(broker_order_event_id, account_scope_id, "
                    "broker_order_id, event_type, status, payload_json, occurred_at, "
                    "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        uuid4().hex,
                        self._scope,
                        order.broker_order_id,
                        event_type,
                        order.status.value,
                        canonical_payload_json(snapshot),
                        to_db_timestamp(order.updated_at),
                        to_db_timestamp(now),
                    ),
                )
                released = False
                if intent_id is not None and order.status in _UNFILLED_END_STATUSES:
                    released = bool(
                        _release_reservations(
                            conn, self._scope, now, reservation_id=_reservation_id(intent_id)
                        )
                    )
                self._emit(
                    "BrokerOrderObserved",
                    "broker_order",
                    order.broker_order_id,
                    {**snapshot, "intent_id": intent_id, "reservation_released": released},
                    occurred_at=order.updated_at,
                )
        return True

    def record_activity(
        self,
        external_activity_id: str,
        activity_type: str,
        occurred_at: datetime,
        payload: Mapping[str, Any] | None = None,
    ) -> bool:
        """Append a raw broker activity once; returns False if already recorded."""
        now = self._now()
        payload_json = canonical_payload_json(payload or {})
        with self._w.unit_of_work() as conn:
            exists = conn.execute(
                "SELECT 1 FROM broker_activities WHERE account_scope_id = ? "
                "AND external_activity_id = ?",
                (self._scope, external_activity_id),
            ).fetchone()
            if exists is not None:
                return False
            conn.execute(
                "INSERT INTO broker_activities(broker_activity_id, account_scope_id, "
                "external_activity_id, activity_type, payload_json, occurred_at, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid4().hex,
                    self._scope,
                    external_activity_id,
                    activity_type,
                    payload_json,
                    to_db_timestamp(occurred_at),
                    to_db_timestamp(now),
                ),
            )
        return True


# ---------------------------------------------------------------------------- fills


@dataclass(frozen=True, slots=True)
class FillApplyResult:
    """Outcome of ``apply_fill``: ``applied`` or ``duplicate`` (nothing changed)."""

    outcome: Literal["applied", "duplicate"]
    external_activity_id: str
    cycle: PositionCycle | None = None
    event_seq: int | None = None

    @property
    def applied(self) -> bool:
        return self.outcome == "applied"

    @property
    def duplicate(self) -> bool:
        return self.outcome == "duplicate"


class FillRepository(_Repository):
    def apply_fill(self, fill: Fill) -> FillApplyResult:
        """Apply a broker fill exactly once, atomically across all derived state."""
        self._w.require_scope(fill.account_scope_id)
        now = self._now()
        scope = self._scope
        with self._w.unit_of_work() as conn:
            if conn.execute(
                "SELECT 1 FROM fills WHERE account_scope_id = ? AND external_activity_id = ?",
                (scope, fill.fill_id),
            ).fetchone():
                return FillApplyResult("duplicate", fill.fill_id)

            order = conn.execute(
                "SELECT intent_id, symbol, side, quantity, filled_quantity, filled_avg_price "
                "FROM broker_orders WHERE account_scope_id = ? AND broker_order_id = ?",
                (scope, fill.broker_order_id),
            ).fetchone()
            if order is None:
                raise PersistenceError(
                    f"fill references unknown broker order {fill.broker_order_id!r}"
                )
            if order[0] is None:
                raise PersistenceError("fill's broker order is not linked to an order intent")
            if order[1] != fill.symbol or order[2] != fill.side.value:
                raise ValueError("fill symbol and side must match its broker order")
            intent = _load_intent(conn, scope, order[0])
            if intent is None:
                raise PersistenceError(f"unknown order intent {order[0]!r}")
            cycle = _load_cycle(conn, scope, intent.cycle_id)
            if cycle is None:
                raise PersistenceError(f"unknown cycle {intent.cycle_id!r}")

            order_qty, order_cost = self._ledger(conn, "b.broker_order_id", fill.broker_order_id)
            if order_qty + fill.quantity > decimal_from_db(order[3]):
                raise ValueError("cumulative fill quantity must not exceed broker order quantity")
            intent_qty, intent_cost = self._ledger(conn, "b.intent_id", intent.intent_id)

            # Domain transition first: a violation raises before any row is written.
            if fill.side.is_entry:
                updated = cycle.apply_entry_fill(fill)
            else:
                if cycle.state is PositionCycleState.OPEN:
                    cycle = cycle.begin_exit()
                updated = cycle.apply_exit_fill(fill)

            cost = fill.quantity * fill.price
            conn.execute(
                "INSERT INTO fills(fill_id, account_scope_id, external_activity_id, "
                "broker_order_id, cycle_id, symbol, side, quantity, price, fee, filled_at, "
                "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{scope}:{fill.fill_id}",
                    scope,
                    fill.fill_id,
                    fill.broker_order_id,
                    cycle.cycle_id,
                    fill.symbol,
                    fill.side.value,
                    decimal_to_db(fill.quantity),
                    decimal_to_db(fill.price),
                    decimal_to_db(fill.fee),
                    to_db_timestamp(fill.filled_at),
                    to_db_timestamp(now),
                ),
            )
            self._update_order_from_ledger(
                conn, fill, order, order_qty + fill.quantity, order_cost + cost
            )
            conn.execute(
                "UPDATE position_cycles SET state = ?, opened_at = ?, closed_at = ?, "
                "updated_at = ? WHERE account_scope_id = ? AND cycle_id = ?",
                (
                    updated.state.value,
                    None if updated.opened_at is None else to_db_timestamp(updated.opened_at),
                    None if updated.closed_at is None else to_db_timestamp(updated.closed_at),
                    to_db_timestamp(now),
                    scope,
                    cycle.cycle_id,
                ),
            )
            reservation_state = self._settle_reservation(
                conn, intent, updated, fill, intent_qty, intent_cost, now
            )
            self._upsert_position(conn, updated, fill)
            trailing = self._resize_trailing(conn, updated, fill, now)
            event = self._emit(
                "FillApplied",
                "position_cycle",
                cycle.cycle_id,
                {
                    "external_activity_id": fill.fill_id,
                    "broker_order_id": fill.broker_order_id,
                    "intent_id": intent.intent_id,
                    "cycle_id": cycle.cycle_id,
                    "symbol": fill.symbol,
                    "side": fill.side,
                    "quantity": fill.quantity,
                    "price": fill.price,
                    "fee": fill.fee,
                    "filled_at": fill.filled_at,
                    "cycle_state": updated.state,
                    "remaining_quantity": updated.remaining_quantity,
                    "realized_pnl": (
                        updated.realized_pnl if updated.state is PositionCycleState.CLOSED else None
                    ),
                    "reservation_state": reservation_state,
                    "trailing": trailing,
                },
                occurred_at=fill.filled_at,
                idempotency_key=f"fill:{fill.fill_id}",
            )
        return FillApplyResult("applied", fill.fill_id, updated, event.event_seq)

    def _ledger(
        self, conn: sqlite3.Connection, column: str, value: str
    ) -> tuple[Decimal, Decimal]:
        """Sum already-applied fill quantity and cost for a broker order or intent."""
        rows = conn.execute(
            "SELECT f.quantity, f.price FROM fills f JOIN broker_orders b "
            "ON b.account_scope_id = f.account_scope_id AND b.broker_order_id = f.broker_order_id "
            f"WHERE f.account_scope_id = ? AND {column} = ?",
            (self._scope, value),
        ).fetchall()
        quantity = _ZERO
        cost = _ZERO
        for raw_quantity, raw_price in rows:
            part = decimal_from_db(raw_quantity)
            quantity += part
            cost += part * decimal_from_db(raw_price)
        return quantity, cost

    def _update_order_from_ledger(
        self,
        conn: sqlite3.Connection,
        fill: Fill,
        order: tuple[Any, ...],
        ledger_qty: Decimal,
        ledger_cost: Decimal,
    ) -> None:
        stored = decimal_from_db(order[4])
        if ledger_qty >= stored:
            filled, average = ledger_qty, ledger_cost / ledger_qty
        else:
            filled, average = stored, _opt_dec(order[5])
        conn.execute(
            "UPDATE broker_orders SET filled_quantity = ?, filled_avg_price = ? "
            "WHERE account_scope_id = ? AND broker_order_id = ?",
            (
                decimal_to_db(filled),
                _opt_dec_to_db(average),
                self._scope,
                fill.broker_order_id,
            ),
        )

    def _settle_reservation(
        self,
        conn: sqlite3.Connection,
        intent: OrderIntent,
        cycle: PositionCycle,
        fill: Fill,
        prior_qty: Decimal,
        prior_cost: Decimal,
        now: datetime,
    ) -> str | None:
        """Consume the entry reservation in proportion; release it when the cycle closes."""
        scope = self._scope
        if fill.side.is_exit:  # SELL or BUY_TO_COVER
            if cycle.state is PositionCycleState.CLOSED:
                _release_reservations(conn, scope, now, cycle_id=cycle.cycle_id)
                return "RELEASED"
            return None
        row = conn.execute(
            "SELECT amount, state FROM budget_reservations "
            "WHERE account_scope_id = ? AND reservation_id = ?",
            (scope, _reservation_id(intent.intent_id)),
        ).fetchone()
        if row is None or row[1] != "ACTIVE":
            return None if row is None else row[1]
        reserved = decimal_from_db(row[0])
        if intent.quantity is not None:
            remaining, filled_part = intent.quantity - prior_qty, fill.quantity
        else:
            assert intent.notional is not None
            remaining, filled_part = intent.notional - prior_cost, fill.quantity * fill.price
        if remaining <= filled_part:
            conn.execute(
                "UPDATE budget_reservations SET state = 'CONSUMED', resolved_at = ? "
                "WHERE account_scope_id = ? AND reservation_id = ?",
                (to_db_timestamp(now), scope, _reservation_id(intent.intent_id)),
            )
            return "CONSUMED"
        conn.execute(
            "UPDATE budget_reservations SET amount = ? "
            "WHERE account_scope_id = ? AND reservation_id = ?",
            (
                decimal_to_db(reserved - reserved * filled_part / remaining),
                scope,
                _reservation_id(intent.intent_id),
            ),
        )
        return "ACTIVE"

    def _upsert_position(
        self, conn: sqlite3.Connection, cycle: PositionCycle, fill: Fill
    ) -> None:
        scope = self._scope
        if cycle.state is PositionCycleState.CLOSED:
            conn.execute(
                "DELETE FROM positions WHERE account_scope_id = ? AND symbol = ? "
                "AND cycle_id = ?",
                (scope, cycle.symbol, cycle.cycle_id),
            )
            return
        average = cycle.avg_entry_price
        assert average is not None
        conn.execute(
            "INSERT INTO positions(account_scope_id, symbol, cycle_id, management, direction, "
            "quantity, average_entry_price, market_price, market_value, unrealized_pl, as_of) "
            "VALUES (?, ?, ?, 'MANAGED', ?, ?, ?, NULL, NULL, NULL, ?) "
            "ON CONFLICT(account_scope_id, symbol) DO UPDATE SET cycle_id = excluded.cycle_id, "
            "management = 'MANAGED', direction = excluded.direction, "
            "quantity = excluded.quantity, average_entry_price = excluded.average_entry_price, "
            "market_price = NULL, market_value = NULL, unrealized_pl = NULL, "
            "as_of = excluded.as_of",
            (
                scope,
                cycle.symbol,
                cycle.cycle_id,
                cycle.direction.value,
                decimal_to_db(cycle.remaining_quantity),
                decimal_to_db(average),
                to_db_timestamp(fill.filled_at),
            ),
        )

    def _resize_trailing(
        self, conn: sqlite3.Connection, cycle: PositionCycle, fill: Fill, now: datetime
    ) -> str | None:
        """Shrink active trailing coverage after an exit; entry fills never grow it."""
        if not fill.side.is_exit:
            return None
        scope = self._scope
        row = conn.execute(
            "SELECT trailing_stop_state_id, covered_quantity, stop_price, broker_order_id "
            "FROM trailing_stop_states WHERE account_scope_id = ? AND cycle_id = ? "
            f"AND status IN {_NON_TERMINAL_PROTECTION!r}",
            (scope, cycle.cycle_id),
        ).fetchone()
        if row is None:
            return None
        covered = decimal_from_db(row[1])
        if cycle.state is PositionCycleState.CLOSED:
            status = "TRIGGERED" if row[3] == fill.broker_order_id else "CANCELED"
            conn.execute(
                "UPDATE trailing_stop_states SET status = ?, updated_at = ? "
                "WHERE trailing_stop_state_id = ?",
                (status, to_db_timestamp(now), row[0]),
            )
            event_type = "PROTECTION_CLOSED"
        elif cycle.remaining_quantity < covered:
            conn.execute(
                "UPDATE trailing_stop_states SET covered_quantity = ?, updated_at = ? "
                "WHERE trailing_stop_state_id = ?",
                (decimal_to_db(cycle.remaining_quantity), to_db_timestamp(now), row[0]),
            )
            status, event_type = "RESIZED", "COVERAGE_RESIZED"
        else:
            return None
        conn.execute(
            "INSERT INTO trailing_stop_events(trailing_stop_event_id, account_scope_id, "
            "cycle_id, event_type, previous_stop_price, new_stop_price, payload_json, "
            "occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid4().hex,
                scope,
                cycle.cycle_id,
                event_type,
                row[2],
                row[2],
                canonical_payload_json(
                    {
                        "previous_covered_quantity": covered,
                        "remaining_quantity": cycle.remaining_quantity,
                        "external_activity_id": fill.fill_id,
                    }
                ),
                to_db_timestamp(fill.filled_at),
            ),
        )
        return status


# -------------------------------------------------------------------- protection


class ProtectionRepository(_Repository):
    def record_protection_order(
        self,
        protection_order_id: str,
        cycle_id: str,
        kind: ProtectionKind,
        status: ProtectionStatus,
        covered_quantity: Decimal,
        *,
        stop_price: Decimal | None = None,
        broker_order_id: str | None = None,
    ) -> None:
        now = self._now()
        with self._w.unit_of_work() as conn:
            conn.execute(
                "INSERT INTO protection_orders(protection_order_id, account_scope_id, cycle_id, "
                "broker_order_id, kind, status, covered_quantity, stop_price, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    protection_order_id,
                    self._scope,
                    cycle_id,
                    broker_order_id,
                    kind.value,
                    status.value,
                    decimal_to_db(covered_quantity),
                    _opt_dec_to_db(stop_price),
                    to_db_timestamp(now),
                    to_db_timestamp(now),
                ),
            )
            self._emit(
                "ProtectionOrderRecorded",
                "position_cycle",
                cycle_id,
                {
                    "protection_order_id": protection_order_id,
                    "kind": kind,
                    "status": status,
                    "covered_quantity": covered_quantity,
                    "stop_price": stop_price,
                    "broker_order_id": broker_order_id,
                },
                occurred_at=now,
            )

    def load_trailing_state(self, cycle_id: str) -> TrailingStopState | None:
        return self._load_trailing(self._w.connection, cycle_id)

    def _load_trailing(self, conn: sqlite3.Connection, cycle_id: str) -> TrailingStopState | None:
        row = conn.execute(
            "SELECT symbol, kind, status, covered_quantity, stop_price, high_water_mark, "
            "broker_order_id, last_confirmed_at FROM trailing_stop_states "
            "WHERE account_scope_id = ? AND cycle_id = ?",
            (self._scope, cycle_id),
        ).fetchone()
        if row is None:
            return None
        return TrailingStopState(
            cycle_id=cycle_id,
            account_scope_id=self._scope,
            symbol=row[0],
            kind=ProtectionKind(row[1]),
            status=ProtectionStatus(row[2]),
            covered_quantity=decimal_from_db(row[3]),
            stop_price=decimal_from_db(row[4]),
            high_water_mark=decimal_from_db(row[5]),
            broker_order_id=row[6],
            last_confirmed_at=None if row[7] is None else from_db_timestamp(row[7]),
        )

    def save_trailing_state(self, state: TrailingStopState) -> TrailingStopState:
        """Upsert a cycle's trailing state; a downward long stop raises ``ValueError``."""
        self._w.require_scope(state.account_scope_id)
        now = self._now()
        with self._w.unit_of_work() as conn:
            existing = self._load_trailing(conn, state.cycle_id)
            if existing is not None:
                ratcheted = existing.ratchet(state.stop_price, state.high_water_mark)
                state = replace(
                    state,
                    stop_price=ratcheted.stop_price,
                    high_water_mark=ratcheted.high_water_mark,
                )
            conn.execute(
                "INSERT INTO trailing_stop_states(trailing_stop_state_id, account_scope_id, "
                "cycle_id, symbol, kind, status, covered_quantity, stop_price, high_water_mark, "
                "broker_order_id, last_confirmed_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(account_scope_id, cycle_id) DO UPDATE SET kind = excluded.kind, "
                "status = excluded.status, covered_quantity = excluded.covered_quantity, "
                "stop_price = excluded.stop_price, high_water_mark = excluded.high_water_mark, "
                "broker_order_id = excluded.broker_order_id, "
                "last_confirmed_at = excluded.last_confirmed_at, updated_at = excluded.updated_at",
                (
                    uuid4().hex,
                    self._scope,
                    state.cycle_id,
                    state.symbol,
                    state.kind.value,
                    state.status.value,
                    decimal_to_db(state.covered_quantity),
                    decimal_to_db(state.stop_price),
                    decimal_to_db(state.high_water_mark),
                    state.broker_order_id,
                    None
                    if state.last_confirmed_at is None
                    else to_db_timestamp(state.last_confirmed_at),
                    to_db_timestamp(now),
                ),
            )
            self._emit(
                "TrailingStopSaved",
                "position_cycle",
                state.cycle_id,
                {
                    "kind": state.kind,
                    "status": state.status,
                    "covered_quantity": state.covered_quantity,
                    "stop_price": state.stop_price,
                    "high_water_mark": state.high_water_mark,
                    "broker_order_id": state.broker_order_id,
                },
                occurred_at=now,
            )
        return state

    def append_trailing_event(
        self,
        cycle_id: str,
        event_type: str,
        *,
        previous_stop_price: Decimal | None = None,
        new_stop_price: Decimal | None = None,
        payload: Mapping[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> str:
        """Append an immutable trailing-stop audit row; returns its id."""
        event_id = uuid4().hex
        with self._w.unit_of_work() as conn:
            conn.execute(
                "INSERT INTO trailing_stop_events(trailing_stop_event_id, account_scope_id, "
                "cycle_id, event_type, previous_stop_price, new_stop_price, payload_json, "
                "occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    self._scope,
                    cycle_id,
                    event_type,
                    _opt_dec_to_db(previous_stop_price),
                    _opt_dec_to_db(new_stop_price),
                    canonical_payload_json(payload or {}),
                    to_db_timestamp(occurred_at if occurred_at is not None else self._now()),
                ),
            )
        return event_id

    def mark_status(self, protection_order_id: str, status: ProtectionStatus) -> None:
        """Set a protection order's status (including UNKNOWN)."""
        now = self._now()
        with self._w.unit_of_work() as conn:
            row = conn.execute(
                "SELECT cycle_id FROM protection_orders "
                "WHERE account_scope_id = ? AND protection_order_id = ?",
                (self._scope, protection_order_id),
            ).fetchone()
            if row is None:
                raise PersistenceError(f"unknown protection order {protection_order_id!r}")
            conn.execute(
                "UPDATE protection_orders SET status = ?, updated_at = ? "
                "WHERE account_scope_id = ? AND protection_order_id = ?",
                (status.value, to_db_timestamp(now), self._scope, protection_order_id),
            )
            self._emit(
                "ProtectionStatusChanged",
                "position_cycle",
                row[0],
                {"protection_order_id": protection_order_id, "status": status},
                occurred_at=now,
            )


# ---------------------------------------------------------------- entry controls


class EntryControlRepository(_Repository):
    """Entry-side controls only; nothing here can block an exit."""

    def _ensure_counter(
        self,
        conn: sqlite3.Connection,
        symbol: str,
        session_date: date,
        key: str,
        now: datetime,
    ) -> None:
        prior = conn.execute(
            "SELECT consecutive_losses FROM entry_eligibility_counters "
            "WHERE account_scope_id = ? AND symbol = ? AND originating_buy_strategy = ? "
            "AND session_date < ? ORDER BY session_date DESC LIMIT 1",
            (self._scope, symbol, key, session_date.isoformat()),
        ).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO entry_eligibility_counters(account_scope_id, symbol, "
            "session_date, originating_buy_strategy, filled_entry_cycles, consecutive_losses, "
            "updated_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
            (
                self._scope,
                symbol,
                session_date.isoformat(),
                key,
                0 if prior is None else prior[0],
                to_db_timestamp(now),
            ),
        )

    def increment_filled_entry_cycles(
        self, symbol: str, session_date: date, originating_buy_strategy: str | None = None
    ) -> int:
        symbol = _symbol(symbol)
        key = originating_buy_strategy or ""
        now = self._now()
        with self._w.unit_of_work() as conn:
            self._ensure_counter(conn, symbol, session_date, key, now)
            conn.execute(
                "UPDATE entry_eligibility_counters SET filled_entry_cycles = "
                "filled_entry_cycles + 1, updated_at = ? WHERE account_scope_id = ? "
                "AND symbol = ? AND session_date = ? AND originating_buy_strategy = ?",
                (to_db_timestamp(now), self._scope, symbol, session_date.isoformat(), key),
            )
            count = conn.execute(
                "SELECT filled_entry_cycles FROM entry_eligibility_counters "
                "WHERE account_scope_id = ? AND symbol = ? AND session_date = ? "
                "AND originating_buy_strategy = ?",
                (self._scope, symbol, session_date.isoformat(), key),
            ).fetchone()[0]
            self._emit(
                "FilledEntryCycleIncremented",
                "entry_eligibility",
                f"{symbol}:{session_date.isoformat()}",
                {
                    "symbol": symbol,
                    "session_date": session_date.isoformat(),
                    "originating_buy_strategy": originating_buy_strategy,
                    "filled_entry_cycles": count,
                },
                occurred_at=now,
            )
        return int(count)

    def record_trade_result(
        self,
        symbol: str,
        session_date: date,
        realized_pnl: Decimal,
        policy: EntryEligibilityPolicy,
        originating_buy_strategy: str | None = None,
    ) -> int:
        """Update the loss streak; lock entries once the policy threshold is reached.

        Returns the new consecutive-loss count.  Pass a strategy only when
        ``policy.lockout_scope_by_strategy`` is enabled; otherwise the streak is
        symbol-wide.
        """
        symbol = _symbol(symbol)
        if not isinstance(realized_pnl, Decimal) or not realized_pnl.is_finite():
            raise ValueError("realized_pnl must be a finite Decimal")
        scoped_strategy = originating_buy_strategy if policy.lockout_scope_by_strategy else None
        key = scoped_strategy or ""
        now = self._now()
        with self._w.unit_of_work() as conn:
            self._ensure_counter(conn, symbol, session_date, key, now)
            row = conn.execute(
                "SELECT consecutive_losses FROM entry_eligibility_counters "
                "WHERE account_scope_id = ? AND symbol = ? AND session_date = ? "
                "AND originating_buy_strategy = ?",
                (self._scope, symbol, session_date.isoformat(), key),
            ).fetchone()
            losses = row[0] + 1 if realized_pnl < _ZERO else 0
            conn.execute(
                "UPDATE entry_eligibility_counters SET consecutive_losses = ?, updated_at = ? "
                "WHERE account_scope_id = ? AND symbol = ? AND session_date = ? "
                "AND originating_buy_strategy = ?",
                (losses, to_db_timestamp(now), self._scope, symbol, session_date.isoformat(), key),
            )
            threshold = policy.consecutive_loss_lockout_threshold
            locked = False
            if threshold is not None and losses >= threshold:
                active = conn.execute(
                    "SELECT 1 FROM entry_lockouts WHERE account_scope_id = ? AND symbol = ? "
                    "AND originating_buy_strategy IS ? AND cleared_at IS NULL",
                    (self._scope, symbol, scoped_strategy),
                ).fetchone()
                if active is None:
                    conn.execute(
                        "INSERT INTO entry_lockouts(entry_lockout_id, account_scope_id, symbol, "
                        "originating_buy_strategy, reason, locked_at, locked_until, cleared_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
                        (
                            uuid4().hex,
                            self._scope,
                            symbol,
                            scoped_strategy,
                            f"consecutive_losses:{losses}",
                            to_db_timestamp(now),
                        ),
                    )
                    locked = True
            self._emit(
                "TradeResultRecorded",
                "entry_eligibility",
                f"{symbol}:{session_date.isoformat()}",
                {
                    "symbol": symbol,
                    "session_date": session_date.isoformat(),
                    "originating_buy_strategy": scoped_strategy,
                    "realized_pnl": realized_pnl,
                    "consecutive_losses": losses,
                    "lockout_set": locked,
                },
                occurred_at=now,
            )
        return int(losses)

    def set_cooldown(
        self,
        symbol: str,
        until: datetime,
        reason: str,
        originating_buy_strategy: str | None = None,
    ) -> str:
        symbol = _symbol(symbol)
        now = self._now()
        cooldown_id = uuid4().hex
        with self._w.unit_of_work() as conn:
            conn.execute(
                "INSERT INTO entry_cooldowns(entry_cooldown_id, account_scope_id, symbol, "
                "originating_buy_strategy, reason, started_at, cooldown_until, cleared_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    cooldown_id,
                    self._scope,
                    symbol,
                    originating_buy_strategy,
                    reason,
                    to_db_timestamp(now),
                    to_db_timestamp(until),
                ),
            )
            self._emit(
                "EntryCooldownSet",
                "entry_eligibility",
                symbol,
                {
                    "symbol": symbol,
                    "originating_buy_strategy": originating_buy_strategy,
                    "reason": reason,
                    "cooldown_until": until,
                },
                occurred_at=now,
            )
        return cooldown_id

    def set_kill_switch(self, enabled: bool, *, updated_by: str, reason: str = "") -> None:
        self._set_flag("kill_switch_enabled", enabled, updated_by, reason, "EntryKillSwitchSet")

    def set_paused(self, paused: bool, *, updated_by: str, reason: str = "") -> None:
        self._set_flag("paused", paused, updated_by, reason, "EntryPausedSet")

    def _set_flag(
        self, column: str, value: bool, updated_by: str, reason: str, event_type: str
    ) -> None:
        if not isinstance(value, bool):
            raise ValueError(f"{column} must be bool")
        if not isinstance(updated_by, str) or not updated_by.strip():
            raise ValueError("updated_by must be a non-empty str")
        now = self._now()
        with self._w.unit_of_work() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO entry_controls(account_scope_id, kill_switch_enabled, "
                "paused, reason, updated_by, updated_at) VALUES (?, 0, 0, '', ?, ?)",
                (self._scope, updated_by, to_db_timestamp(now)),
            )
            conn.execute(
                f"UPDATE entry_controls SET {column} = ?, reason = ?, updated_by = ?, "
                "updated_at = ? WHERE account_scope_id = ?",
                (int(value), reason, updated_by, to_db_timestamp(now), self._scope),
            )
            self._emit(
                event_type,
                "entry_controls",
                self._scope,
                {"enabled": value, "updated_by": updated_by, "reason": reason},
                occurred_at=now,
            )

    def load_eligibility_state(
        self, symbol: str, session_date: date, originating_buy_strategy: str | None = None
    ) -> EntryEligibilityState:
        symbol = _symbol(symbol)
        conn = self._w.connection
        now = self._now()
        stamp = to_db_timestamp(now)
        filled = conn.execute(
            "SELECT COALESCE(SUM(filled_entry_cycles), 0) FROM entry_eligibility_counters "
            "WHERE account_scope_id = ? AND symbol = ? AND session_date = ?",
            (self._scope, symbol, session_date.isoformat()),
        ).fetchone()[0]
        streak = conn.execute(
            "SELECT consecutive_losses FROM entry_eligibility_counters "
            "WHERE account_scope_id = ? AND symbol = ? AND originating_buy_strategy = ? "
            "AND session_date <= ? ORDER BY session_date DESC LIMIT 1",
            (self._scope, symbol, originating_buy_strategy or "", session_date.isoformat()),
        ).fetchone()
        reasons: list[str] = []
        controls = conn.execute(
            "SELECT kill_switch_enabled, paused FROM entry_controls WHERE account_scope_id = ?",
            (self._scope,),
        ).fetchone()
        if controls is not None and controls[0]:
            reasons.append("entry_kill_switch")
        if controls is not None and controls[1]:
            reasons.append("paused")
        locked = conn.execute(
            "SELECT 1 FROM entry_lockouts WHERE account_scope_id = ? AND symbol = ? "
            "AND (originating_buy_strategy IS NULL OR originating_buy_strategy = ?) "
            "AND cleared_at IS NULL AND (locked_until IS NULL OR locked_until > ?)",
            (self._scope, symbol, originating_buy_strategy, stamp),
        ).fetchone()
        if locked is not None:
            reasons.append("consecutive_loss_lockout")
        cooldown = conn.execute(
            "SELECT MAX(cooldown_until) FROM entry_cooldowns WHERE account_scope_id = ? "
            "AND symbol = ? AND (originating_buy_strategy IS NULL OR originating_buy_strategy = ?) "
            "AND cleared_at IS NULL AND cooldown_until > ?",
            (self._scope, symbol, originating_buy_strategy, stamp),
        ).fetchone()[0]
        last_exit = conn.execute(
            "SELECT MAX(closed_at) FROM position_cycles WHERE account_scope_id = ? "
            "AND symbol = ? AND state = 'CLOSED'",
            (self._scope, symbol),
        ).fetchone()[0]
        return EntryEligibilityState(
            account_scope_id=self._scope,
            symbol=symbol,
            session_date=session_date,
            filled_entry_cycles=int(filled),
            consecutive_losses=0 if streak is None else int(streak[0]),
            last_exit_at=None if last_exit is None else from_db_timestamp(last_exit),
            locked_until=None if cooldown is None else from_db_timestamp(cooldown),
            block_reasons=tuple(reasons),
        )


# ----------------------------------------------------------------- operator / recon


class OperatorCommandRepository(_Repository):
    def record_command(
        self,
        command: ControlCommand,
        *,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> bool:
        """Append an operator command; returns False if it was already recorded."""
        self._w.require_scope(command.scope.scope_id)
        key = idempotency_key or command.command_id
        now = self._now()
        with self._w.unit_of_work() as conn:
            exists = conn.execute(
                "SELECT 1 FROM operator_commands WHERE account_scope_id = ? "
                "AND (command_id = ? OR idempotency_key = ?)",
                (self._scope, command.command_id, key),
            ).fetchone()
            if exists is not None:
                return False
            conn.execute(
                "INSERT INTO operator_commands(command_id, account_scope_id, command_kind, "
                "symbol, reason, issued_by, live_confirmed, payload_json, issued_at, "
                "processed_at, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    command.command_id,
                    self._scope,
                    command.kind.value,
                    None if command.symbol is None else _symbol(command.symbol),
                    command.reason,
                    command.issued_by,
                    int(command.live_confirmed),
                    canonical_payload_json(payload or {}),
                    to_db_timestamp(command.issued_at),
                    key,
                ),
            )
            self._emit(
                "OperatorCommandRecorded",
                "operator_command",
                command.command_id,
                {
                    "command_id": command.command_id,
                    "kind": command.kind.value,
                    "symbol": command.symbol,
                    "issued_by": command.issued_by,
                    "live_confirmed": command.live_confirmed,
                },
                occurred_at=now,
                idempotency_key=f"operator_command:{command.command_id}",
            )
        return True


class ReconciliationRepository(_Repository):
    def start_run(self) -> str:
        run_id = uuid4().hex
        now = self._now()
        with self._w.unit_of_work() as conn:
            conn.execute(
                "INSERT INTO reconciliation_runs(reconciliation_run_id, account_scope_id, "
                "status, started_at, completed_at, summary_json) "
                "VALUES (?, ?, 'STARTED', ?, NULL, '{}')",
                (run_id, self._scope, to_db_timestamp(now)),
            )
            self._emit(
                "ReconciliationStarted",
                "reconciliation_run",
                run_id,
                {"reconciliation_run_id": run_id},
                occurred_at=now,
            )
        return run_id

    def complete_run(
        self,
        run_id: str,
        status: Literal["CLEAN", "DRIFTED", "ERROR"],
        summary: Mapping[str, Any] | None = None,
    ) -> None:
        now = self._now()
        with self._w.unit_of_work() as conn:
            cursor = conn.execute(
                "UPDATE reconciliation_runs SET status = ?, completed_at = ?, summary_json = ? "
                "WHERE account_scope_id = ? AND reconciliation_run_id = ? "
                "AND status = 'STARTED'",
                (
                    status,
                    to_db_timestamp(now),
                    canonical_payload_json(summary or {}),
                    self._scope,
                    run_id,
                ),
            )
            if cursor.rowcount == 0:
                raise PersistenceError(f"reconciliation run {run_id!r} is not in progress")
            self._emit(
                "ReconciliationCompleted",
                "reconciliation_run",
                run_id,
                {"reconciliation_run_id": run_id, "status": status},
                occurred_at=now,
            )

    def record_drift(
        self,
        run_id: str,
        drift_type: str,
        *,
        entity_ref: str | None = None,
        expected: Mapping[str, Any] | None = None,
        actual: Mapping[str, Any] | None = None,
    ) -> str:
        """Append a drift observation; fills and orders are never modified."""
        drift_id = uuid4().hex
        now = self._now()
        with self._w.unit_of_work() as conn:
            conn.execute(
                "INSERT INTO reconciliation_drifts(drift_id, account_scope_id, "
                "reconciliation_run_id, drift_type, entity_ref, expected_json, actual_json, "
                "detected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    drift_id,
                    self._scope,
                    run_id,
                    drift_type,
                    entity_ref,
                    None if expected is None else canonical_payload_json(expected),
                    None if actual is None else canonical_payload_json(actual),
                    to_db_timestamp(now),
                ),
            )
            self._emit(
                "ReconciliationDriftRecorded",
                "reconciliation_run",
                run_id,
                {"drift_id": drift_id, "drift_type": drift_type, "entity_ref": entity_ref},
                occurred_at=now,
            )
        return drift_id

    def resolve_drift(
        self,
        drift_id: str,
        resolution_kind: str,
        resolved_by: str,
        details: Mapping[str, Any] | None = None,
    ) -> str:
        resolution_id = uuid4().hex
        now = self._now()
        with self._w.unit_of_work() as conn:
            conn.execute(
                "INSERT INTO reconciliation_resolutions(resolution_id, account_scope_id, "
                "drift_id, resolution_kind, details_json, resolved_by, resolved_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    resolution_id,
                    self._scope,
                    drift_id,
                    resolution_kind,
                    canonical_payload_json(details or {}),
                    resolved_by,
                    to_db_timestamp(now),
                ),
            )
            self._emit(
                "ReconciliationDriftResolved",
                "reconciliation_drift",
                drift_id,
                {
                    "drift_id": drift_id,
                    "resolution_id": resolution_id,
                    "resolution_kind": resolution_kind,
                    "resolved_by": resolved_by,
                },
                occurred_at=now,
            )
        return resolution_id

    def has_unresolved_drift(self) -> bool:
        row = self._w.connection.execute(
            "SELECT EXISTS(SELECT 1 FROM reconciliation_drifts d "
            "WHERE d.account_scope_id = ? AND NOT EXISTS ("
            "SELECT 1 FROM reconciliation_resolutions r "
            "WHERE r.account_scope_id = d.account_scope_id AND r.drift_id = d.drift_id))",
            (self._scope,),
        ).fetchone()
        return bool(row[0])


# --------------------------------------------------------- strategy / cash / health


class StrategyStateRepository(_Repository):
    def save_state(
        self,
        instance_id: str,
        symbol: str,
        state: Mapping[str, Any],
        *,
        armed: bool = True,
        rearm_required: bool = False,
    ) -> None:
        symbol = _symbol(symbol)
        now = self._now()
        state_json = canonical_payload_json(state)
        with self._w.unit_of_work() as conn:
            conn.execute(
                "INSERT INTO strategy_state(account_scope_id, instance_id, symbol, state_json, "
                "armed, rearm_required, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(account_scope_id, instance_id, symbol) DO UPDATE SET "
                "state_json = excluded.state_json, armed = excluded.armed, "
                "rearm_required = excluded.rearm_required, updated_at = excluded.updated_at",
                (
                    self._scope,
                    instance_id,
                    symbol,
                    state_json,
                    int(armed),
                    int(rearm_required),
                    to_db_timestamp(now),
                ),
            )
            self._emit(
                "StrategyStateSaved",
                "strategy_state",
                f"{instance_id}:{symbol}",
                {"armed": armed, "rearm_required": rearm_required, "state": state},
                occurred_at=now,
            )

    def load_state(self, instance_id: str, symbol: str) -> dict[str, Any] | None:
        row = self._w.connection.execute(
            "SELECT state_json, armed, rearm_required, updated_at FROM strategy_state "
            "WHERE account_scope_id = ? AND instance_id = ? AND symbol = ?",
            (self._scope, instance_id, _symbol(symbol)),
        ).fetchone()
        if row is None:
            return None
        return {
            "state": json.loads(row[0]),
            "armed": bool(row[1]),
            "rearm_required": bool(row[2]),
            "updated_at": from_db_timestamp(row[3]),
        }


class CashEquityRepository(_Repository):
    def append_cash_event(
        self,
        event_type: str,
        amount: Decimal,
        occurred_at: datetime,
        *,
        external_activity_id: str | None = None,
        description: str = "",
        currency: str = "USD",
    ) -> bool:
        """Append an account-level cash event; False if the activity was already recorded."""
        now = self._now()
        with self._w.unit_of_work() as conn:
            if external_activity_id is not None and conn.execute(
                "SELECT 1 FROM cash_events WHERE account_scope_id = ? "
                "AND external_activity_id = ?",
                (self._scope, external_activity_id),
            ).fetchone():
                return False
            conn.execute(
                "INSERT INTO cash_events(cash_event_id, account_scope_id, event_type, amount, "
                "currency, external_activity_id, description, occurred_at, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid4().hex,
                    self._scope,
                    event_type,
                    decimal_to_db(amount),
                    currency,
                    external_activity_id,
                    description,
                    to_db_timestamp(occurred_at),
                    to_db_timestamp(now),
                ),
            )
        return True

    def append_equity_snapshot(self, snapshot: AccountSnapshot) -> bool:
        """Append an account snapshot; False if (as_of, source) was already recorded."""
        self._w.require_scope(snapshot.scope.scope_id)
        now = self._now()
        with self._w.unit_of_work() as conn:
            if conn.execute(
                "SELECT 1 FROM equity_snapshots WHERE account_scope_id = ? AND as_of = ? "
                "AND source = ?",
                (self._scope, to_db_timestamp(snapshot.as_of), snapshot.source),
            ).fetchone():
                return False
            conn.execute(
                "INSERT INTO equity_snapshots(account_scope_id, cash, equity, buying_power, "
                "portfolio_value, source, as_of, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._scope,
                    decimal_to_db(snapshot.cash),
                    decimal_to_db(snapshot.equity),
                    decimal_to_db(snapshot.buying_power),
                    _opt_dec_to_db(snapshot.portfolio_value),
                    snapshot.source,
                    to_db_timestamp(snapshot.as_of),
                    to_db_timestamp(now),
                ),
            )
        return True


class ServiceHealthRepository(_Repository):
    def heartbeat(
        self,
        component: str,
        status: HealthStatus = HealthStatus.HEALTHY,
        details: Mapping[str, str] | None = None,
    ) -> HealthSnapshot:
        """Upsert a component's health with the clock's current time as the heartbeat."""
        now = self._now()
        snapshot = HealthSnapshot(
            component=component,
            status=status,
            observed_at=now,
            heartbeat_at=now,
            details=dict(details or {}),
        )
        with self._w.unit_of_work() as conn:
            conn.execute(
                "INSERT INTO service_health(account_scope_id, component, status, heartbeat_at, "
                "observed_at, details_json) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(account_scope_id, component) DO UPDATE SET status = excluded.status, "
                "heartbeat_at = excluded.heartbeat_at, observed_at = excluded.observed_at, "
                "details_json = excluded.details_json",
                (
                    self._scope,
                    snapshot.component,
                    snapshot.status.value,
                    to_db_timestamp(now),
                    to_db_timestamp(now),
                    canonical_payload_json(dict(snapshot.details)),
                ),
            )
        return snapshot
