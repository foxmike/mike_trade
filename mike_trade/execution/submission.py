"""Exactly-once order submission with unknown-outcome recovery.

``OrderSubmitter`` turns an authorized f.2 command and a persisted f.1 intent
into at most one broker submission per ``client_order_id``:

* ``INTENT_PERSISTED -> SUBMITTING`` is persisted *before* the broker is called,
  and every later transition is persisted before the next step acts on it.
* Staleness is re-checked immediately before the network call; a stale intent is
  rejected locally and the broker is never contacted.
* Any submit failure other than an explicit broker rejection is an *unknown
  outcome*: the intent moves to ``UNKNOWN`` and is resolved only by looking the
  order up by its original ``client_order_id``.  The submitter never resubmits
  blindly and never mints a new ``client_order_id`` or attempt number.
* A fill is recorded only when the broker reports fill-bearing status together
  with a filled quantity; acceptance never implies a fill.

The FSM has no ``UNKNOWN -> SUBMITTING`` edge, so a confirmed not-found retry is
an explicit ledger *re-arm* (``resolve_not_found``) that restores the same intent
to ``INTENT_PERSISTED`` under the same ``client_order_id``.

Durability is injected through ``SubmissionLedger``: the f.1 ``IntentStore`` owns
only the initial intent record, so transition, annotation, and unresolved-marker
persistence is adapted here rather than added to f.1.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal, Protocol, TypeVar

from mike_trade.brokers.ports import BrokerPort
from mike_trade.domain.accounts import AccountEnvironment
from mike_trade.domain.errors import (
    BrokerError,
    BrokerRejectedError,
    LiveTradingNotAuthorizedError,
    UnsupportedOrderCapabilityError,
)
from mike_trade.domain.trading import BrokerOrder, BrokerOrderStatus
from mike_trade.domain.trading import OrderIntent as BrokerOrderIntent
from mike_trade.domain.trading import OrderPurpose as BrokerOrderPurpose

from .coordinator import CommandDecision, ExecutionKey, OrderCommand
from .intents import OrderIntent, OrderPurpose
from .order_fsm import (
    TERMINAL_STATUSES,
    OrderEvent,
    OrderStatus,
    TransitionResult,
    apply_transition,
)

__all__ = [
    "OrderSubmitter",
    "SubmissionResult",
    "OrderOutcomeUnresolved",
    "StaleSubmission",
    "UnresolvedOrderMarker",
]

_T = TypeVar("_T")

_INFLIGHT = frozenset(
    {OrderStatus.INTENT_PERSISTED, OrderStatus.SUBMITTING, OrderStatus.UNKNOWN}
)
_CANCELABLE = frozenset(
    {
        OrderStatus.ACCEPTED,
        OrderStatus.PENDING,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.SUSPENDED,
    }
)
_REPLACEABLE = frozenset({OrderStatus.ACCEPTED, OrderStatus.PENDING})
_FILL_BEARING = frozenset({OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED})

# BrokerOrderStatus.UNKNOWN is deliberately absent: it establishes nothing.
_BROKER_STATUS = {
    BrokerOrderStatus.NEW: OrderStatus.PENDING,
    BrokerOrderStatus.PENDING_NEW: OrderStatus.PENDING,
    BrokerOrderStatus.ACCEPTED: OrderStatus.ACCEPTED,
    BrokerOrderStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
    BrokerOrderStatus.FILLED: OrderStatus.FILLED,
    BrokerOrderStatus.PENDING_CANCEL: OrderStatus.CANCEL_PENDING,
    BrokerOrderStatus.CANCELED: OrderStatus.CANCELED,
    BrokerOrderStatus.EXPIRED: OrderStatus.EXPIRED,
    BrokerOrderStatus.REJECTED: OrderStatus.REJECTED,
    BrokerOrderStatus.REPLACED: OrderStatus.REPLACED,
}

_BROKER_PURPOSE = {
    OrderPurpose.ENTRY_LONG: BrokerOrderPurpose.ENTRY,
    OrderPurpose.EXIT_LONG: BrokerOrderPurpose.STRATEGY_EXIT,
    OrderPurpose.ENTRY_SHORT: BrokerOrderPurpose.ENTRY,
    OrderPurpose.EXIT_SHORT: BrokerOrderPurpose.STRATEGY_EXIT,
    OrderPurpose.PROTECTIVE_STOP: BrokerOrderPurpose.PROTECTIVE_STOP,
    OrderPurpose.TRAILING_STOP: BrokerOrderPurpose.PROTECTIVE_STOP,
    OrderPurpose.MANUAL_CLOSE: BrokerOrderPurpose.MANUAL_CLOSE,
    OrderPurpose.SAFETY_EXIT: BrokerOrderPurpose.SAFETY_EXIT,
}

Outcome = Literal[
    "submitted",
    "rejected",
    "rejected_local",
    "resolved",
    "not_found",
    "rearmed",
    "needs_decision",
    "blocked",
    "refused",
    "unconfirmed",
    "cancel_requested",
    "replace_requested",
    "replace_unconfirmed",
]


class StaleSubmission(Enum):
    """Typed reason an intent was rejected locally without contacting the broker."""

    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
    QUOTE_STALE = "QUOTE_STALE"


@dataclass(frozen=True, slots=True)
class UnresolvedOrderMarker:
    """Durable evidence that an order may exist at the broker; blocks new entries."""

    account_key: str
    symbol: str
    client_order_id: str
    intent_id: str
    reason: str
    recorded_at: datetime


class OrderOutcomeUnresolved(BrokerError):
    """A submission may exist, but lookup could not establish its outcome."""

    def __init__(
        self,
        client_order_id: str,
        lookup_error: BaseException,
        marker: UnresolvedOrderMarker | None = None,
    ) -> None:
        self.client_order_id = client_order_id
        self.lookup_error = lookup_error
        self.marker = marker
        super().__init__(
            f"order outcome remains unresolved for client_order_id {client_order_id!r}",
            code="order_outcome_unresolved",
            context={"client_order_id": client_order_id},
        )


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    """Disposition of one submitter operation; ``status`` is the persisted state."""

    outcome: Outcome
    client_order_id: str
    status: OrderStatus | None
    broker_order_id: str | None = None
    filled_qty: Decimal | None = None
    stale: StaleSubmission | None = None
    marker: UnresolvedOrderMarker | None = None
    detail: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class _Clock(Protocol):
    def now(self) -> datetime: ...


class SubmissionLedger(Protocol):
    """Append-only persistence the submitter requires; each call must be durable."""

    def status(self, client_order_id: str) -> OrderStatus | None: ...

    def record_transition(
        self,
        intent: OrderIntent,
        result: TransitionResult,
        *,
        reason: str,
        broker_order_id: str | None,
    ) -> None: ...

    def record_rearm(self, intent: OrderIntent, *, reason: str) -> None:
        """Append a record restoring ``INTENT_PERSISTED`` for the same client_order_id."""
        ...

    def annotate(self, intent: OrderIntent, kind: str, detail: str) -> None: ...

    def broker_order_id(self, client_order_id: str) -> str | None: ...

    def inflight(self, account_key: str) -> Sequence[OrderIntent]:
        """Intents currently INTENT_PERSISTED, SUBMITTING, or UNKNOWN."""
        ...

    def put_marker(self, marker: UnresolvedOrderMarker) -> None: ...

    def clear_marker(self, client_order_id: str) -> None: ...

    def markers(self, account_key: str) -> Sequence[UnresolvedOrderMarker]: ...


async def _resolve(value: _T | Awaitable[_T]) -> _T:
    if inspect.isawaitable(value):
        return await value
    return value


def _to_broker_intent(intent: OrderIntent) -> BrokerOrderIntent:
    return BrokerOrderIntent(
        intent_id=intent.intent_id,
        client_order_id=intent.client_order_id,
        account_scope_id=intent.account_scope_id,
        cycle_id=intent.position_cycle_id,
        symbol=intent.symbol,
        side=intent.side,
        purpose=_BROKER_PURPOSE[intent.purpose],
        order_type=intent.order_type,
        time_in_force=intent.time_in_force,
        created_at=intent.created_at,
        quantity=intent.qty,
        notional=intent.notional,
        limit_price=intent.limit_price,
        stop_price=intent.stop_price,
        trail_price=intent.trail_price,
        trail_percent=intent.trail_percent,
    )


def _event_for(order: BrokerOrder, *, lookup: bool) -> OrderEvent | None:
    """Translate a broker snapshot; quantity is carried only for fill-bearing status."""
    status = _BROKER_STATUS.get(order.status)
    if status is None:
        return None
    filled = (
        order.filled_quantity
        if status in _FILL_BEARING and order.filled_quantity > 0
        else None
    )
    return OrderEvent(status, filled_qty=filled, broker_lookup=lookup)


class OrderSubmitter:
    """Single-shot, persist-first submitter with fail-closed unknown handling."""

    def __init__(
        self,
        *,
        broker: BrokerPort,
        ledger: SubmissionLedger,
        clock: _Clock,
        quote_checker: Callable[[OrderIntent, datetime], bool | Awaitable[bool]],
        signal_checker: Callable[[OrderIntent, datetime], bool | Awaitable[bool]] | None = None,
        replace_capability: Callable[[str], bool] | None = None,
        live_authorized: bool = False,
        short_authorized: bool = False,
        short_entry_checker: Callable[[OrderIntent], bool | Awaitable[bool]] | None = None,
        call_timeout: float | None = None,
    ) -> None:
        if not isinstance(live_authorized, bool):
            raise TypeError("live_authorized must be bool")
        if not isinstance(short_authorized, bool):
            raise TypeError("short_authorized must be bool")
        self._broker = broker
        self._ledger = ledger
        self._clock = clock
        self._quote_checker = quote_checker
        self._signal_checker = signal_checker or (
            lambda intent, now: now < intent.signal_expires_at
        )
        self._replace_capability = replace_capability
        self._live_authorized = live_authorized
        self._short_authorized = short_authorized
        self._short_entry_checker = short_entry_checker
        self._call_timeout = call_timeout

    # ------------------------------------------------------------------ submit

    async def submit(
        self, command: OrderCommand, decision: CommandDecision, intent: OrderIntent
    ) -> SubmissionResult:
        """Submit one persisted intent at most once.

        Raises ``OrderOutcomeUnresolved`` when an unknown outcome cannot be
        resolved by lookup; the intent then stays UNKNOWN with a blocking marker.
        """
        if not isinstance(command, OrderCommand):
            raise TypeError("command must be OrderCommand")
        if not isinstance(decision, CommandDecision):
            raise TypeError("decision must be CommandDecision")
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be OrderIntent")
        self._require_live_authorization(intent)
        await self._require_short_authorization(intent)
        if command.intent != intent or decision.key != ExecutionKey.from_intent(intent):
            raise ValueError("command, decision, and intent must describe the same order")
        if not decision.authorized:
            raise ValueError(f"command is not authorized: {decision.reason.value}")

        cid = intent.client_order_id
        status = self._ledger.status(cid)
        if status is not OrderStatus.INTENT_PERSISTED:
            return self._result("refused", intent, status, detail="intent is not INTENT_PERSISTED")
        if intent.purpose in {
            OrderPurpose.ENTRY_LONG,
            OrderPurpose.ENTRY_SHORT,
        } and self._blocking_marker(intent):
            return self._result(
                "blocked", intent, status, detail="unresolved order blocks new entries"
            )

        broker_intent = _to_broker_intent(intent)
        # Persist-before-send: if this append fails, the broker is never called.
        status = self._apply(
            intent, status, OrderEvent(OrderStatus.SUBMITTING), reason="submit:persist_before_send"
        )

        stale = await self._stale_reason(intent)
        if stale is not None:
            status = self._apply(
                intent,
                status,
                OrderEvent(OrderStatus.REJECTED),
                reason=f"local_reject:{stale.value}",
            )
            return self._result("rejected_local", intent, status, stale=stale)

        try:
            order = await self._call(self._broker.submit_order(broker_intent))
        except BrokerRejectedError as exc:
            status = self._apply(
                intent, status, OrderEvent(OrderStatus.REJECTED), reason="broker_rejected"
            )
            return self._result("rejected", intent, status, detail=str(exc))
        except asyncio.CancelledError:
            raise  # stays SUBMITTING; recover_inflight resolves it by lookup
        except Exception as exc:
            return await self._unknown_outcome(intent, exc)

        event = (
            _event_for(order, lookup=False) if order.client_order_id == cid else None
        )
        if event is None:
            return await self._unknown_outcome(
                intent, ValueError("submit response did not establish this order's status")
            )
        status = self._apply(
            intent, status, event, reason="submit_response", broker_order_id=order.broker_order_id
        )
        return self._result(
            "submitted",
            intent,
            status,
            broker_order_id=order.broker_order_id,
            filled_qty=event.filled_qty,
        )

    async def _unknown_outcome(self, intent: OrderIntent, error: BaseException) -> SubmissionResult:
        self._apply(
            intent,
            OrderStatus.SUBMITTING,
            OrderEvent(OrderStatus.UNKNOWN),
            reason=f"unknown_outcome:{type(error).__name__}",
        )
        try:
            return await self._resolve_by_lookup(intent, OrderStatus.UNKNOWN, source="submit")
        except OrderOutcomeUnresolved as unresolved:
            unresolved.add_note(f"submission error: {error!r}")
            raise

    # --------------------------------------------------------- unknown handling

    async def resolve_unknown(self, intent: OrderIntent) -> SubmissionResult:
        """Re-run lookup for an UNKNOWN intent; raises if still unresolved."""
        status = self._ledger.status(intent.client_order_id)
        if status is not OrderStatus.UNKNOWN:
            return self._result("refused", intent, status, detail="intent is not UNKNOWN")
        return await self._resolve_by_lookup(intent, status, source="resolve_unknown")

    async def resolve_not_found(self, intent: OrderIntent, *, retry: bool) -> SubmissionResult:
        """Explicit operator/engine decision for an UNKNOWN intent absent at the broker.

        A fresh lookup is made first; the order is acted on if it now exists.
        ``retry=True`` re-arms the *same* intent and client_order_id for a normal
        ``submit``; ``retry=False`` abandons it as a terminal rejection.
        """
        cid = intent.client_order_id
        status = self._ledger.status(cid)
        if status is not OrderStatus.UNKNOWN:
            return self._result("refused", intent, status, detail="intent is not UNKNOWN")
        result = await self._resolve_by_lookup(intent, status, source="resolve_not_found")
        if result.outcome != "not_found":
            return result
        if retry:
            self._ledger.record_rearm(intent, reason="not_found_confirmed:explicit_retry")
            self._ledger.clear_marker(cid)
            return self._result("rearmed", intent, OrderStatus.INTENT_PERSISTED)
        status = self._apply(
            intent,
            status,
            OrderEvent(OrderStatus.REJECTED, broker_lookup=True),
            reason="not_found_confirmed:abandoned",
        )
        self._ledger.clear_marker(cid)
        return self._result("rejected_local", intent, status, detail="abandoned after not-found")

    async def _resolve_by_lookup(
        self, intent: OrderIntent, current: OrderStatus, *, source: str
    ) -> SubmissionResult:
        try:
            order = await self._lookup(intent)
        except Exception as exc:
            raise self._unresolved(intent, f"lookup_failed:{source}", exc) from exc
        if order is None:
            self._ledger.annotate(intent, "NOT_FOUND_CONFIRMED", source)
            marker = self._mark_unresolved(intent, "not_found_awaiting_resolution")
            return self._result(
                "not_found", intent, current, marker=marker,
                detail="broker has no such order; retry only via resolve_not_found",
            )
        event = (
            _event_for(order, lookup=True)
            if order.client_order_id == intent.client_order_id
            else None
        )
        if event is None:
            error = ValueError("lookup did not establish this order's status")
            raise self._unresolved(intent, f"lookup_inconclusive:{source}", error)
        status = self._apply(
            intent, current, event, reason=f"lookup:{source}", broker_order_id=order.broker_order_id
        )
        self._ledger.clear_marker(intent.client_order_id)
        return self._result(
            "resolved",
            intent,
            status,
            broker_order_id=order.broker_order_id,
            filled_qty=event.filled_qty,
        )

    # ------------------------------------------------------------ crash recovery

    async def recover_inflight(self, account_key: str) -> list[SubmissionResult]:
        """Resolve INTENT_PERSISTED/SUBMITTING/UNKNOWN intents by client_order_id lookup.

        Never submits.  INTENT_PERSISTED intents with no broker record are expired
        locally when their signal is stale, otherwise flagged for an explicit
        decision.  Per-intent problems are reported, not raised.
        """
        results: list[SubmissionResult] = []
        for intent in self._ledger.inflight(account_key):
            status = self._ledger.status(intent.client_order_id)
            if status not in _INFLIGHT:
                continue
            try:
                results.append(await self._recover_one(intent, status))
            except OrderOutcomeUnresolved as unresolved:
                results.append(
                    self._result(
                        "unconfirmed", intent, self._ledger.status(intent.client_order_id),
                        marker=unresolved.marker, detail=str(unresolved.lookup_error),
                    )
                )
        return results

    async def _recover_one(self, intent: OrderIntent, status: OrderStatus) -> SubmissionResult:
        if status is OrderStatus.SUBMITTING:
            status = self._apply(
                intent, status, OrderEvent(OrderStatus.UNKNOWN), reason="crash_recovery"
            )
        if status is OrderStatus.UNKNOWN:
            return await self._resolve_by_lookup(intent, status, source="recovery")

        # INTENT_PERSISTED: SUBMITTING was never persisted, so nothing was sent by us.
        try:
            order = await self._lookup(intent)
        except Exception as exc:
            raise self._unresolved(intent, "lookup_failed:recovery", exc) from exc
        if order is not None:
            event = (
                _event_for(order, lookup=True)
                if order.client_order_id == intent.client_order_id
                else None
            )
            if event is None:
                error = ValueError("lookup did not establish this order's status")
                raise self._unresolved(intent, "lookup_inconclusive:recovery", error)
            status = self._apply(
                intent,
                status,
                event,
                reason="lookup:recovery",
                broker_order_id=order.broker_order_id,
            )
            self._ledger.clear_marker(intent.client_order_id)
            return self._result(
                "resolved", intent, status,
                broker_order_id=order.broker_order_id, filled_qty=event.filled_qty,
            )
        if await self._signal_stale(intent):
            status = self._apply(
                intent,
                status,
                OrderEvent(OrderStatus.REJECTED),
                reason=f"local_reject:{StaleSubmission.SIGNAL_EXPIRED.value}",
            )
            return self._result(
                "rejected_local", intent, status, stale=StaleSubmission.SIGNAL_EXPIRED
            )
        self._ledger.annotate(intent, "NEEDS_DECISION", "no broker record; not auto-submitted")
        return self._result(
            "needs_decision", intent, status, detail="no broker record; explicit decision required"
        )

    # ---------------------------------------------------------- cancel / replace

    async def request_cancel(self, intent: OrderIntent) -> SubmissionResult:
        """Persist CANCEL_PENDING, then ask the broker; CANCELED needs confirmation."""
        self._require_live_authorization(intent)
        cid = intent.client_order_id
        status = self._ledger.status(cid)
        broker_order_id = self._ledger.broker_order_id(cid)
        if status not in _CANCELABLE or broker_order_id is None:
            return self._result(
                "refused", intent, status, broker_order_id=broker_order_id,
                detail="cancel needs a live order with a recorded broker order id",
            )
        status = self._apply(
            intent,
            status,
            OrderEvent(OrderStatus.CANCEL_PENDING, broker_confirmed=False),
            reason="cancel_requested",
            broker_order_id=broker_order_id,
        )
        detail = ""
        try:
            await self._call(self._broker.cancel_order(broker_order_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = f"cancel call failed: {exc!r}"
            self._ledger.annotate(intent, "CANCEL_REQUEST_ERROR", detail)
        return self._result(
            "cancel_requested", intent, status, broker_order_id=broker_order_id, detail=detail
        )

    async def request_replace(
        self,
        intent: OrderIntent,
        *,
        stop_price: Decimal | None = None,
        trail: Decimal | None = None,
        quantity: Decimal | None = None,
    ) -> SubmissionResult:
        """Replace only under a proven capability; REPLACED needs broker confirmation."""
        self._require_live_authorization(intent)
        requested = {
            "replace_stop_price": stop_price,
            "replace_trail_price": trail,
            "replace_quantity": quantity,
        }
        names = [name for name, value in requested.items() if value is not None]
        if not names:
            raise ValueError("a replacement field is required")
        cid = intent.client_order_id
        status = self._ledger.status(cid)
        broker_order_id = self._ledger.broker_order_id(cid)
        unproven = [name for name in names if not self._capability_proven(name)]
        if unproven:
            return self._result(
                "refused", intent, status, broker_order_id=broker_order_id,
                detail=f"replace capability not proven: {', '.join(unproven)}",
            )
        if status not in _REPLACEABLE or broker_order_id is None:
            return self._result(
                "refused", intent, status, broker_order_id=broker_order_id,
                detail="replace needs an open order with a recorded broker order id",
            )
        self._ledger.annotate(intent, "REPLACE_REQUESTED", repr(sorted(names)))
        try:
            replacement = await self._call(
                self._broker.replace_order(
                    broker_order_id, stop_price=stop_price, trail=trail, quantity=quantity
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = f"replace call failed: {exc!r}"
            self._ledger.annotate(intent, "REPLACE_ERROR", detail)
            return self._result(
                "replace_unconfirmed",
                intent,
                status,
                broker_order_id=broker_order_id,
                detail=detail,
            )
        self._ledger.annotate(
            intent,
            "REPLACE_RESPONSE",
            f"broker_order_id={replacement.broker_order_id} status={replacement.status.value}",
        )
        # The response is evidence only; status changes need a confirmed lookup.
        return self._result(
            "replace_requested", intent, status, broker_order_id=broker_order_id
        )

    async def confirm_from_broker(self, intent: OrderIntent) -> SubmissionResult:
        """Apply broker-confirmed state (cancel/replace/fill updates) via lookup.

        A failed or empty lookup leaves the persisted status untouched.
        """
        cid = intent.client_order_id
        status = self._ledger.status(cid)
        if status is None or status in TERMINAL_STATUSES:
            return self._result("refused", intent, status, detail="nothing to confirm")
        if status is OrderStatus.UNKNOWN:
            return await self._resolve_by_lookup(intent, status, source="confirm")
        try:
            order = await self._lookup(intent)
        except Exception as exc:
            return self._result("unconfirmed", intent, status, detail=f"lookup failed: {exc!r}")
        if order is None or order.client_order_id != cid:
            return self._result("unconfirmed", intent, status, detail="broker has no such order")
        event = _event_for(order, lookup=True)
        if event is None:
            return self._result("unconfirmed", intent, status, detail="broker status unknown")
        status = self._apply(
            intent, status, event, reason="confirm", broker_order_id=order.broker_order_id
        )
        return self._result(
            "resolved", intent, status,
            broker_order_id=order.broker_order_id, filled_qty=event.filled_qty,
        )

    # ------------------------------------------------------------------ helpers

    def entries_blocked(self, account_key: str, symbol: str) -> bool:
        """True while an unresolved-order marker exists for this account and symbol."""
        wanted = symbol.strip().upper()
        return any(m.symbol == wanted for m in self._ledger.markers(account_key))

    def _blocking_marker(self, intent: OrderIntent) -> bool:
        return self.entries_blocked(intent.account_key, intent.symbol)

    def _require_live_authorization(self, intent: OrderIntent) -> None:
        live = (
            intent.execution_mode is AccountEnvironment.LIVE
            or intent.account_scope_id.startswith("live:")
            or getattr(self._broker, "environment", None) is AccountEnvironment.LIVE
        )
        if live and not self._live_authorized:
            raise LiveTradingNotAuthorizedError(
                "LIVE submit, cancel, and replace require explicit live authorization",
                code="live_not_authorized",
                context={"client_order_id": intent.client_order_id},
            )

    async def _require_short_authorization(self, intent: OrderIntent) -> None:
        """Fail closed before persistence/submission of a short opening order.

        The explicit switch prevents a configuration or strategy merge from
        enabling shorting.  Broker/account flags are necessary but not
        sufficient: the injected checker must also prove symbol borrowability,
        buying power/margin and an available broker-held protection mechanism.
        """
        if intent.purpose is not OrderPurpose.ENTRY_SHORT:
            return
        capabilities = self._broker.capabilities()
        account = await self._call(self._broker.get_account())
        checker = self._short_entry_checker
        eligible = False
        if checker is not None:
            try:
                eligible = await _resolve(checker(intent)) is True
            except Exception:
                eligible = False
        if not (
            self._short_authorized
            and capabilities.supports_short
            and account.shorting_enabled
            and account.buying_power > Decimal("0")
            and eligible
        ):
            raise UnsupportedOrderCapabilityError(
                "short entry requires explicit authorization, broker/account "
                "eligibility, buying power, symbol borrowability, and protection",
                code="short_entry_not_authorized",
                context={"client_order_id": intent.client_order_id},
            )

    def _capability_proven(self, name: str) -> bool:
        if self._replace_capability is None:
            return False
        try:
            return self._replace_capability(name) is True
        except Exception:
            return False

    async def _stale_reason(self, intent: OrderIntent) -> StaleSubmission | None:
        if await self._signal_stale(intent):
            return StaleSubmission.SIGNAL_EXPIRED
        try:
            fresh = await _resolve(self._quote_checker(intent, self._clock.now()))
        except Exception:
            fresh = False
        return None if fresh is True else StaleSubmission.QUOTE_STALE

    async def _signal_stale(self, intent: OrderIntent) -> bool:
        try:
            fresh = await _resolve(self._signal_checker(intent, self._clock.now()))
        except Exception:
            fresh = False
        return fresh is not True

    async def _lookup(self, intent: OrderIntent) -> BrokerOrder | None:
        return await self._call(self._broker.get_order_by_client_id(intent.client_order_id))

    async def _call(self, awaitable: Awaitable[_T]) -> _T:
        if self._call_timeout is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, self._call_timeout)

    def _apply(
        self,
        intent: OrderIntent,
        current: OrderStatus,
        event: OrderEvent,
        *,
        reason: str,
        broker_order_id: str | None = None,
    ) -> OrderStatus:
        """Validate through the f.1 FSM and persist the transition before returning."""
        if current is OrderStatus.INTENT_PERSISTED and event.status is not OrderStatus.SUBMITTING:
            current = self._apply(
                intent, current, OrderEvent(OrderStatus.SUBMITTING), reason=f"{reason}:submitting"
            )
        result = apply_transition(current, event)
        if result.illegal and current is OrderStatus.SUBMITTING:
            # SUBMITTING reaches non-direct states only through ACCEPTED (response)
            # or UNKNOWN (lookup).
            bridge = OrderStatus.UNKNOWN if event.broker_lookup else OrderStatus.ACCEPTED
            if bridge is not event.status:
                current = self._apply(
                    intent,
                    current,
                    OrderEvent(bridge, broker_lookup=event.broker_lookup),
                    reason=f"{reason}:bridge",
                    broker_order_id=broker_order_id,
                )
                result = apply_transition(current, event)
        result.raise_for_illegal()
        if result.accepted:
            self._ledger.record_transition(
                intent, result, reason=reason, broker_order_id=broker_order_id
            )
        return result.status

    def _mark_unresolved(self, intent: OrderIntent, reason: str) -> UnresolvedOrderMarker:
        marker = UnresolvedOrderMarker(
            account_key=intent.account_key,
            symbol=intent.symbol,
            client_order_id=intent.client_order_id,
            intent_id=intent.intent_id,
            reason=reason,
            recorded_at=self._clock.now(),
        )
        self._ledger.put_marker(marker)
        return marker

    def _unresolved(
        self, intent: OrderIntent, reason: str, error: BaseException
    ) -> OrderOutcomeUnresolved:
        marker = self._mark_unresolved(intent, reason)
        return OrderOutcomeUnresolved(intent.client_order_id, error, marker)

    @staticmethod
    def _result(
        outcome: Outcome,
        intent: OrderIntent,
        status: OrderStatus | None,
        **fields: object,
    ) -> SubmissionResult:
        return SubmissionResult(outcome, intent.client_order_id, status, **fields)  # type: ignore[arg-type]
