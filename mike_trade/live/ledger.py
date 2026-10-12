"""Durable accounting for the PAPER/LIVE lifecycle, through the typed repositories.

Everything the engine believes about its own trades is written here: cycle, intent, broker
order, fill, protective-stop state.  It is the same ledger the dashboard, the account
reporter and :class:`mike_trade.execution.reconcile.AccountReconciler` read, so a PAPER/LIVE
trade appears exactly like any other.

Fills are keyed by Alpaca's *activity id* (the broker's own execution record), exactly once.
Executions of orders that carry no Mike_Trade intent (a human using the website, the mobile
app, DAS Trader on the same account, ...) are recorded as external: an exit-side execution
reduces the managed cycle it belongs to, anything else is journaled and never adopted.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from mike_trade.domain.accounts import AccountScope
from mike_trade.domain.protection import ProtectionStatus, TrailingStopState
from mike_trade.domain.trading import (
    BrokerOrder,
    BudgetPolicy,
    Fill,
    OrderIntent,
    OrderPurpose,
    PositionCycle,
    PositionDirection,
)
from mike_trade.persistence.journal import DomainEventJournal, NewEvent
from mike_trade.persistence.repositories import (
    BrokerOrderRepository,
    FillApplyResult,
    FillRepository,
    IntentReceipt,
    OrderIntentRepository,
    PositionCycleRepository,
    ProtectionRepository,
)
from mike_trade.persistence.schema import decimal_from_db, from_db_timestamp, to_db_timestamp
from mike_trade.persistence.transaction import ScopedWriter

__all__ = ["IntentRow", "LiveLedger", "OrderRow"]

ZERO = Decimal("0")
_OPEN_CYCLE_STATES = ("PENDING_ENTRY", "OPEN", "EXITING")


@dataclass(frozen=True, slots=True)
class IntentRow:
    intent_id: str
    client_order_id: str
    cycle_id: str
    symbol: str
    side: str
    purpose: str
    status: str
    quantity: Decimal | None
    stop_price: Decimal | None


@dataclass(frozen=True, slots=True)
class OrderRow:
    broker_order_id: str
    client_order_id: str
    intent_id: str | None
    symbol: str
    side: str
    order_type: str
    status: str
    quantity: Decimal
    filled_quantity: Decimal
    stop_price: Decimal | None
    cycle_id: str | None
    purpose: str | None


class LiveLedger:
    """One account scope's trade ledger."""

    def __init__(
        self,
        writer: ScopedWriter,
        clock: Any,
        scope: AccountScope,
        budget: BudgetPolicy,
    ) -> None:
        self.writer = writer
        self.clock = clock
        self.scope = scope
        self.scope_id = scope.scope_id
        self._cycles = PositionCycleRepository(writer, clock)
        self._intents = OrderIntentRepository(writer, clock, budget)
        self._orders = BrokerOrderRepository(writer, clock)
        self._fills = FillRepository(writer, clock)
        self._protection = ProtectionRepository(writer, clock)
        self._journal = DomainEventJournal(writer.connection, clock=clock)

    # ------------------------------------------------------------------ cycles

    def open_cycle(self, cycle_id: str, symbol: str, strategy_key: str | None) -> bool:
        """Create a PENDING_ENTRY cycle; idempotent (returns False if it already exists)."""
        if self._cycles.get_cycle(cycle_id) is not None:
            return False
        self._cycles.open_cycle(
            PositionCycle(
                cycle_id, self.scope_id, symbol, PositionDirection.LONG,
                entry_strategy_key=strategy_key,
            )
        )
        return True

    def cycle(self, cycle_id: str) -> PositionCycle | None:
        return self._cycles.get_cycle(cycle_id)

    def open_cycles(self) -> list[PositionCycle]:
        marks = ",".join("?" for _ in _OPEN_CYCLE_STATES)
        rows = self.writer.connection.execute(
            f"SELECT cycle_id FROM position_cycles WHERE account_scope_id = ? "
            f"AND state IN ({marks}) ORDER BY created_at, rowid",
            (self.scope_id, *_OPEN_CYCLE_STATES),
        ).fetchall()
        cycles = (self._cycles.get_cycle(row[0]) for row in rows)
        return [cycle for cycle in cycles if cycle is not None]

    def active_cycle(self, symbol: str) -> PositionCycle | None:
        """The symbol's single non-closed cycle (the engine never keeps two)."""
        found = [c for c in self.open_cycles() if c.symbol == symbol]
        return found[-1] if found else None

    def abandon_cycle(self, cycle_id: str, reason: str) -> None:
        """Close a cycle that never filled and has no working order (nothing was bought)."""
        cycle = self._cycles.get_cycle(cycle_id)
        if cycle is None or cycle.entry_fills:
            return
        now = self.clock.now()
        with self.writer.unit_of_work() as conn:
            conn.execute(
                "UPDATE position_cycles SET state = 'ABANDONED', updated_at = ? "
                "WHERE account_scope_id = ? AND cycle_id = ? AND state = 'PENDING_ENTRY'",
                (to_db_timestamp(now), self.scope_id, cycle_id),
            )
        self.event("position_cycle", cycle_id, "PositionCycleAbandoned", {"reason": reason})

    # ----------------------------------------------------------------- intents

    def next_attempt(self, cycle_id: str, purpose: OrderPurpose) -> int:
        """Monotonic attempt number: recorded intents for this lineage (restart-safe)."""
        row = self.writer.connection.execute(
            "SELECT COUNT(*) FROM order_intents WHERE account_scope_id = ? AND cycle_id = ? "
            "AND purpose = ?",
            (self.scope_id, cycle_id, purpose.value),
        ).fetchone()
        return int(row[0])

    def record_intent(self, intent: OrderIntent, reservation_amount: Decimal = ZERO) -> IntentReceipt:
        return self._intents.record_intent(intent, reservation_amount)

    def submit_recorded(
        self, receipt: IntentReceipt, submit: Callable[[IntentReceipt], str | None]
    ) -> str | None:
        return self._intents.submit_recorded_intent(receipt, submit)

    def mark_submitted(self, receipt: IntentReceipt, broker_order_id: str | None) -> None:
        self._intents.mark_submitted(receipt, broker_order_id=broker_order_id)

    def mark_submitted_by_id(self, intent_id: str, broker_order_id: str) -> bool:
        return self._intents.mark_submitted_by_id(intent_id, broker_order_id)

    def mark_not_submitted(self, intent_id: str, detail: str) -> bool:
        return self._intents.mark_not_submitted(intent_id, detail)

    def unresolved_intents(self) -> list[IntentRow]:
        """Intents whose broker outcome is not yet known (RECORDED / UNKNOWN)."""
        rows = self.writer.connection.execute(
            "SELECT intent_id, client_order_id, cycle_id, symbol, side, purpose, status, "
            "quantity, stop_price FROM order_intents WHERE account_scope_id = ? "
            "AND status IN ('RECORDED','UNKNOWN') ORDER BY created_at, rowid",
            (self.scope_id,),
        ).fetchall()
        return [self._intent_row(row) for row in rows]

    def intent_by_client(self, client_order_id: str) -> IntentRow | None:
        row = self.writer.connection.execute(
            "SELECT intent_id, client_order_id, cycle_id, symbol, side, purpose, status, "
            "quantity, stop_price FROM order_intents "
            "WHERE account_scope_id = ? AND client_order_id = ?",
            (self.scope_id, client_order_id),
        ).fetchone()
        return None if row is None else self._intent_row(row)

    def recorded_client_ids(self) -> frozenset[str]:
        rows = self.writer.connection.execute(
            "SELECT client_order_id FROM order_intents WHERE account_scope_id = ?",
            (self.scope_id,),
        ).fetchall()
        return frozenset(row[0] for row in rows)

    @staticmethod
    def _intent_row(row: Sequence[Any]) -> IntentRow:
        return IntentRow(
            row[0], row[1], row[2], row[3], row[4], row[5], row[6],
            None if row[7] is None else decimal_from_db(row[7]),
            None if row[8] is None else decimal_from_db(row[8]),
        )

    # ------------------------------------------------------------------ orders

    def upsert_order(self, order: BrokerOrder, intent_id: str | None = None) -> bool:
        return self._orders.upsert_broker_order(order, intent_id=intent_id)

    def order_row(self, broker_order_id: str) -> OrderRow | None:
        rows = self._order_rows("AND b.broker_order_id = ?", (broker_order_id,))
        return rows[0] if rows else None

    def order_rows_for_cycle(self, cycle_id: str) -> list[OrderRow]:
        return self._order_rows("AND i.cycle_id = ?", (cycle_id,))

    def working_order_rows(self, symbol: str | None = None) -> list[OrderRow]:
        """Ledger orders not known to be terminal."""
        extra = "AND b.status IN ('NEW','PENDING_NEW','ACCEPTED','PARTIALLY_FILLED','PENDING_CANCEL','UNKNOWN')"
        args: tuple[Any, ...] = ()
        if symbol is not None:
            extra += " AND b.symbol = ?"
            args = (symbol,)
        return self._order_rows(extra, args)

    def _order_rows(self, extra: str, args: tuple[Any, ...]) -> list[OrderRow]:
        rows = self.writer.connection.execute(
            "SELECT b.broker_order_id, b.client_order_id, b.intent_id, b.symbol, b.side, "
            "b.order_type, b.status, b.quantity, b.filled_quantity, b.stop_price, "
            "i.cycle_id, i.purpose FROM broker_orders b "
            "LEFT JOIN order_intents i ON i.account_scope_id = b.account_scope_id "
            "AND i.intent_id = b.intent_id WHERE b.account_scope_id = ? " + extra +
            " ORDER BY b.broker_order_row_id",
            (self.scope_id, *args),
        ).fetchall()
        return [
            OrderRow(
                r[0], r[1], r[2], r[3], r[4], r[5], r[6], decimal_from_db(r[7]),
                decimal_from_db(r[8]), None if r[9] is None else decimal_from_db(r[9]), r[10], r[11],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------- fills

    def has_activity(self, activity_id: str) -> bool:
        conn = self.writer.connection
        in_fills = conn.execute(
            "SELECT 1 FROM fills WHERE account_scope_id = ? AND external_activity_id = ?",
            (self.scope_id, activity_id),
        ).fetchone()
        if in_fills:
            return True
        return bool(
            conn.execute(
                "SELECT 1 FROM broker_activities WHERE account_scope_id = ? "
                "AND external_activity_id = ?",
                (self.scope_id, activity_id),
            ).fetchone()
        )

    def ledger_fill_quantity(self, broker_order_id: str) -> Decimal:
        rows = self.writer.connection.execute(
            "SELECT quantity FROM fills WHERE account_scope_id = ? AND broker_order_id = ?",
            (self.scope_id, broker_order_id),
        ).fetchall()
        return sum((decimal_from_db(row[0]) for row in rows), ZERO)

    def apply_fill(self, fill: Fill) -> FillApplyResult:
        return self._fills.apply_fill(fill)

    def apply_external_fill(self, fill: Fill, cycle_id: str, classification: str) -> FillApplyResult:
        return self._fills.apply_external_fill(fill, cycle_id, classification=classification)

    def record_activity(
        self, activity_id: str, activity_type: str, occurred_at: datetime,
        payload: Mapping[str, Any] | None = None,
    ) -> bool:
        return self._orders.record_activity(activity_id, activity_type, occurred_at, payload)

    def activity_cursor(self) -> datetime | None:
        """Latest fill / activity time on record (the REST read resumes just before it)."""
        row = self.writer.connection.execute(
            "SELECT MAX(t) FROM (SELECT occurred_at AS t FROM broker_activities "
            "WHERE account_scope_id = ? UNION ALL SELECT filled_at FROM fills "
            "WHERE account_scope_id = ?)",
            (self.scope_id, self.scope_id),
        ).fetchone()
        return None if row is None or row[0] is None else from_db_timestamp(row[0])

    # ------------------------------------------------------------- protection

    def load_stop(self, cycle_id: str) -> TrailingStopState | None:
        return self._protection.load_trailing_state(cycle_id)

    def save_stop(self, state: TrailingStopState) -> TrailingStopState:
        return self._protection.save_trailing_state(state)

    def set_stop_status(
        self, cycle_id: str, status: ProtectionStatus, *, broker_order_id: str | None = None,
        covered_quantity: Decimal | None = None,
    ) -> None:
        """Change a stored stop's status/identity without touching its ratcheted price."""
        existing = self.load_stop(cycle_id)
        if existing is None:
            return
        from dataclasses import replace

        quantity = existing.covered_quantity if covered_quantity is None else covered_quantity
        self._protection.save_trailing_state(
            replace(
                existing,
                status=status,
                covered_quantity=quantity if quantity > ZERO else existing.covered_quantity,
                broker_order_id=broker_order_id if broker_order_id is not None else existing.broker_order_id,
                last_confirmed_at=self.clock.now(),
            )
        )

    def stop_event(
        self, cycle_id: str, event_type: str, *, previous: Decimal | None = None,
        new: Decimal | None = None, payload: Mapping[str, Any] | None = None,
    ) -> None:
        self._protection.append_trailing_event(
            cycle_id, event_type, previous_stop_price=previous, new_stop_price=new,
            payload=payload, occurred_at=self.clock.now(),
        )

    # ----------------------------------------------------------------- journal

    def event(
        self, stream_type: str, stream_id: str, event_type: str, payload: Mapping[str, Any],
        *, idempotency_key: str | None = None,
    ) -> None:
        now = self.clock.now()
        self._journal.append(
            [
                NewEvent(
                    self.scope_id, stream_type, stream_id, event_type, dict(payload), now,
                    idempotency_key=idempotency_key,
                )
            ]
        )

    def once(self, key: str) -> bool:
        """True exactly once per ``key`` across restarts (journal idempotency key)."""
        result = self._journal.append(
            [
                NewEvent(
                    self.scope_id, "bookkeeping", key, "BookkeepingOnce", {"key": key},
                    self.clock.now(), idempotency_key=f"once:{key}",
                )
            ]
        )
        return bool(result.inserted_events)

    def verify_chain(self) -> list[str]:
        return self._journal.verify_chain(self.scope_id)
