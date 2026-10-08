"""Central coordination of broker-held protection and discretionary exits.

The coordinator is deliberately independent of strategy scheduling and control
state.  Its only inputs are confirmed broker quantities/order updates, policy,
capabilities, a broker port, and a UTC clock.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from mike_trade.runtime import ClockPort, ExecutionMode

from .stop_policy import (
    BrokerProtectionCapabilities,
    ProtectionEligibility,
    ProtectionPolicy,
    evaluate_policy,
)

__all__ = ["ProtectionCoordinator"]

_ZERO = Decimal("0")
_TERMINAL = frozenset({"CANCELED", "CANCELLED", "FILLED", "REJECTED", "EXPIRED"})


class _ProtectionBrokerPort(Protocol):
    """Minimal provider-neutral surface used by this coordinator.

    A concrete adapter may implement ``submit_protective_order`` and
    ``submit_exit_order`` directly, or accept both request values through its
    generic ``submit_order`` method.
    """

    def submit_protective_order(self, request: object) -> object: ...

    def submit_exit_order(self, request: object) -> object: ...

    def cancel_order(self, order_id: str) -> object: ...


@dataclass(frozen=True, slots=True)
class _ProtectiveOrderRequest:
    cycle_id: str
    quantity: Decimal
    confirmed_avg_fill_price: Decimal
    policy: ProtectionPolicy
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _ExitOrderRequest:
    cycle_id: str
    quantity: Decimal
    reason: str
    created_at: datetime


@dataclass(slots=True)
class _WorkingExit:
    order_id: str
    quantity: Decimal
    cumulative_filled: Decimal = _ZERO

    @property
    def working_quantity(self) -> Decimal:
        return self.quantity - self.cumulative_filled


@dataclass(slots=True)
class _CycleState:
    cycle_id: str
    confirmed_entry_quantity: Decimal = _ZERO
    confirmed_exit_quantity: Decimal = _ZERO
    avg_fill_price: Decimal | None = None
    status: str = "AWAITING_FILL"
    protective_order_id: str | None = None
    protective_order_quantity: Decimal = _ZERO
    protective_cumulative_filled: Decimal = _ZERO
    pending_replacement: bool = False
    pending_exit_quantity: Decimal = _ZERO
    pending_exit_reason: str | None = None
    working_exit: _WorkingExit | None = None
    updated_at: datetime | None = None
    last_error: str | None = None

    @property
    def remaining_quantity(self) -> Decimal:
        remaining = (
            self.confirmed_entry_quantity
            - self.confirmed_exit_quantity
            - self.protective_cumulative_filled
        )
        return max(_ZERO, remaining)

    @property
    def working_protective_quantity(self) -> Decimal:
        if self.protective_order_id is None:
            return _ZERO
        return self.protective_order_quantity - self.protective_cumulative_filled

    @property
    def working_exit_quantity(self) -> Decimal:
        return _ZERO if self.working_exit is None else self.working_exit.working_quantity


@dataclass(frozen=True, slots=True)
class _ProtectionSnapshot:
    cycle_id: str
    status: str
    confirmed_remaining_quantity: Decimal
    protective_order_id: str | None
    protective_working_quantity: Decimal
    exit_order_id: str | None
    exit_working_quantity: Decimal
    pending_replacement: bool
    pending_exit_quantity: Decimal
    updated_at: datetime | None
    last_error: str | None

    @property
    def working_sell_quantity(self) -> Decimal:
        return self.protective_working_quantity + self.exit_working_quantity


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite() or value < _ZERO or (positive and value == _ZERO):
        relation = "> 0" if positive else ">= 0"
        raise ValueError(f"{name} must be a finite Decimal {relation}")
    return value


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC (zero offset)")
    return value.astimezone(UTC)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _status(value: object) -> str:
    raw = getattr(value, "value", value)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("status must be a non-empty string or status enum")
    return raw.strip().upper()


def _order_id(value: object) -> str:
    if isinstance(value, str):
        return _text(value, "broker order id")
    if isinstance(value, dict):
        for name in ("broker_order_id", "order_id", "id"):
            if name in value:
                return _text(value[name], "broker order id")
    for name in ("broker_order_id", "order_id", "id"):
        candidate = getattr(value, name, None)
        if candidate is not None:
            return _text(candidate, "broker order id")
    raise ValueError("broker acknowledgement did not contain an order id")


async def _await_if_needed(value: object) -> object:
    return await value if inspect.isawaitable(value) else value


class ProtectionCoordinator:
    """Serialize protection and exit orders for each PositionCycle id."""

    def __init__(
        self,
        broker: _ProtectionBrokerPort,
        clock: ClockPort,
        policy: ProtectionPolicy,
    ) -> None:
        if broker is None:
            raise TypeError("broker is required")
        if not callable(getattr(clock, "now", None)):
            raise TypeError("clock must provide now()")
        if not isinstance(policy, ProtectionPolicy):
            raise TypeError("policy must be ProtectionPolicy")
        self._broker = broker
        self._clock = clock
        self._policy = policy
        self._cycles: dict[str, _CycleState] = {}
        self._unprotected_cycles: set[str] = set()

    @property
    def entry_block_reasons(self) -> tuple[str, ...]:
        """Fresh-entry blocks raised by any currently unprotected filled cycle."""

        if not self._unprotected_cycles:
            return ()
        return ("UNPROTECTED_POSITION",)

    def check_entry_eligibility(
        self,
        policy: ProtectionPolicy,
        capabilities: BrokerProtectionCapabilities,
        mode: ExecutionMode | object,
        intended_qty: Decimal,
    ) -> ProtectionEligibility:
        """Evaluate protection before entry submission.

        ``entry_allowed`` on the result is false precisely when required
        protection is ineligible.  No strategy pause or holding-period input is
        accepted by this safety boundary.
        """

        return evaluate_policy(policy, capabilities, mode, intended_qty)

    async def on_confirmed_fill(
        self,
        cycle_id: str,
        cumulative_filled_qty: Decimal,
        avg_fill_price: Decimal,
        *,
        confirmed: bool = True,
    ) -> _ProtectionSnapshot | None:
        """Cover a positive, broker-confirmed cumulative entry fill."""

        cycle = _text(cycle_id, "cycle_id")
        quantity = _decimal(cumulative_filled_qty, "cumulative_filled_qty")
        price = _decimal(avg_fill_price, "avg_fill_price", positive=True)
        if not isinstance(confirmed, bool):
            raise TypeError("confirmed must be bool")
        if not confirmed or quantity == _ZERO:
            return self.current_protection(cycle)

        state = self._cycles.setdefault(cycle, _CycleState(cycle_id=cycle))
        if quantity < state.confirmed_entry_quantity:
            raise ValueError("cumulative_filled_qty must not decrease")
        state.confirmed_entry_quantity = quantity
        state.avg_fill_price = price
        state.updated_at = self._now()

        if state.working_exit is not None or state.pending_exit_quantity > _ZERO:
            return self._snapshot(state)
        if state.protective_order_id is None:
            await self._place_protection(state)
        elif state.working_protective_quantity != state.remaining_quantity:
            await self._request_replacement(state)
        return self._snapshot(state)

    async def on_exit_requested(
        self, cycle_id: str, qty: Decimal, reason: str
    ) -> _ProtectionSnapshot:
        """Queue or submit an exit without overlapping an effective stop."""

        state = self._require_cycle(cycle_id)
        quantity = _decimal(qty, "qty", positive=True)
        exit_reason = _text(reason, "reason")
        if state.remaining_quantity == _ZERO:
            raise ValueError("cannot exit a cycle with no confirmed remaining position")
        if state.working_exit is not None:
            raise RuntimeError("an exit order is already working for this cycle")
        requested = state.pending_exit_quantity + quantity
        if requested > state.remaining_quantity:
            raise ValueError("requested exit quantity exceeds confirmed remaining quantity")

        state.pending_exit_quantity = requested
        state.pending_exit_reason = exit_reason
        state.updated_at = self._now()
        if state.protective_order_id is not None:
            if not state.status.startswith("CANCEL_PENDING"):
                state.status = "CANCEL_PENDING_FOR_EXIT"
                await self._cancel_effective_protection(state)
            else:
                state.status = "CANCEL_PENDING_FOR_EXIT"
        else:
            await self._release_pending_exit(state)
        self._assert_sell_invariant(state)
        return self._snapshot(state)

    async def on_broker_order_update(
        self,
        cycle_id: str,
        order_id: str | object,
        status: object | None = None,
        *,
        filled_qty: Decimal | None = None,
        avg_fill_price: Decimal | None = None,
        occurred_at: datetime | None = None,
    ) -> _ProtectionSnapshot:
        """Apply a normalized cumulative broker-order update.

        ``order_id`` may also be an existing broker-neutral order snapshot with
        ``broker_order_id``, ``status``, ``filled_quantity``, and ``updated_at``
        attributes.
        """

        state = self._require_cycle(cycle_id)
        update = order_id
        if isinstance(update, str):
            broker_order_id = _text(update, "order_id")
        else:
            broker_order_id = _order_id(update)
            if status is None:
                status = getattr(update, "status", None)
            if filled_qty is None:
                filled_qty = getattr(
                    update, "filled_quantity", getattr(update, "filled_qty", None)
                )
            if avg_fill_price is None:
                avg_fill_price = getattr(update, "filled_avg_price", None)
            if occurred_at is None:
                occurred_at = getattr(update, "updated_at", None)
        if status is None:
            raise ValueError("status is required")
        normalized = _status(status)
        cumulative = _decimal(_ZERO if filled_qty is None else filled_qty, "filled_qty")
        if avg_fill_price is not None:
            _decimal(avg_fill_price, "avg_fill_price", positive=True)
        state.updated_at = self._now() if occurred_at is None else _utc(occurred_at, "occurred_at")

        if broker_order_id == state.protective_order_id:
            await self._apply_protective_update(state, normalized, cumulative)
        elif state.working_exit is not None and broker_order_id == state.working_exit.order_id:
            await self._apply_exit_update(state, normalized, cumulative)
        else:
            raise ValueError("order update does not belong to an effective order for this cycle")
        self._assert_sell_invariant(state)
        return self._snapshot(state)

    def current_protection(self, cycle_id: str) -> _ProtectionSnapshot | None:
        state = self._cycles.get(_text(cycle_id, "cycle_id"))
        return None if state is None else self._snapshot(state)

    def _now(self) -> datetime:
        return _utc(self._clock.now(), "clock.now()")

    def _require_cycle(self, cycle_id: str) -> _CycleState:
        cycle = _text(cycle_id, "cycle_id")
        try:
            return self._cycles[cycle]
        except KeyError:
            raise KeyError(f"unknown PositionCycle id: {cycle}") from None

    async def _broker_call(self, preferred: str, fallback: str, request: object) -> object:
        method = getattr(self._broker, preferred, None)
        if not callable(method):
            method = getattr(self._broker, fallback, None)
        if not callable(method):
            raise TypeError(f"broker must provide {preferred}() or {fallback}()")
        return await _await_if_needed(method(request))

    async def _place_protection(self, state: _CycleState) -> None:
        if state.remaining_quantity <= _ZERO:
            return
        if state.protective_order_id is not None:
            raise RuntimeError("cannot place a second effective protective order")
        if state.avg_fill_price is None:
            raise RuntimeError("confirmed average fill price is required for protection")
        request = _ProtectiveOrderRequest(
            cycle_id=state.cycle_id,
            quantity=state.remaining_quantity,
            confirmed_avg_fill_price=state.avg_fill_price,
            policy=self._policy,
            created_at=self._now(),
        )
        try:
            acknowledgement = await self._broker_call(
                "submit_protective_order", "submit_order", request
            )
            state.protective_order_id = _order_id(acknowledgement)
            state.protective_order_quantity = state.remaining_quantity
            state.protective_cumulative_filled = _ZERO
            state.pending_replacement = False
            state.status = "PROTECTION_ACTIVE"
            state.last_error = None
            self._unprotected_cycles.discard(state.cycle_id)
        except Exception as exc:
            self._mark_unprotected(state, f"protective placement failed: {exc}")
            raise RuntimeError(state.last_error) from exc
        self._assert_sell_invariant(state)

    async def _request_replacement(self, state: _CycleState) -> None:
        if state.protective_order_id is None:
            await self._place_protection(state)
            return
        if state.status == "CANCEL_PENDING_FOR_REPLACEMENT":
            return
        state.pending_replacement = True
        state.status = "CANCEL_PENDING_FOR_REPLACEMENT"
        await self._cancel_effective_protection(state)

    async def _cancel_effective_protection(self, state: _CycleState) -> None:
        order_id = state.protective_order_id
        if order_id is None:
            return
        cancel = getattr(self._broker, "cancel_order", None)
        if not callable(cancel):
            self._mark_unprotected(state, "broker must provide cancel_order()")
            raise RuntimeError(state.last_error)
        try:
            await _await_if_needed(cancel(order_id))
        except Exception as exc:
            self._mark_unprotected(state, f"protective cancellation failed: {exc}")
            raise RuntimeError(state.last_error) from exc

    async def _release_pending_exit(self, state: _CycleState) -> None:
        quantity = state.pending_exit_quantity
        if quantity == _ZERO:
            return
        if state.protective_order_id is not None:
            raise RuntimeError("cannot release exit while protection remains effective")
        if quantity > state.remaining_quantity:
            raise RuntimeError("pending exit exceeds confirmed remaining position")
        reason = state.pending_exit_reason
        assert reason is not None
        request = _ExitOrderRequest(state.cycle_id, quantity, reason, self._now())
        try:
            acknowledgement = await self._broker_call("submit_exit_order", "submit_order", request)
            state.working_exit = _WorkingExit(_order_id(acknowledgement), quantity)
            state.pending_exit_quantity = _ZERO
            state.pending_exit_reason = None
            state.status = "EXIT_ACTIVE"
        except Exception as exc:
            self._mark_unprotected(state, f"exit placement failed after protection cancel: {exc}")
            raise RuntimeError(state.last_error) from exc

    async def _apply_protective_update(
        self, state: _CycleState, status: str, cumulative_filled: Decimal
    ) -> None:
        if cumulative_filled < state.protective_cumulative_filled:
            raise ValueError("protective cumulative filled quantity must not decrease")
        if cumulative_filled > state.protective_order_quantity:
            raise ValueError("protective filled quantity exceeds its order quantity")
        state.protective_cumulative_filled = cumulative_filled

        if status in {"PARTIALLY_FILLED", "PARTIAL_FILL"}:
            state.status = "PROTECTION_PARTIALLY_FILLED"
            return
        if status == "FILLED":
            state.confirmed_exit_quantity += state.protective_cumulative_filled
            state.protective_order_id = None
            state.protective_order_quantity = _ZERO
            state.protective_cumulative_filled = _ZERO
            state.pending_replacement = False
            if state.remaining_quantity == _ZERO:
                state.pending_exit_quantity = _ZERO
                state.pending_exit_reason = None
            state.status = (
                "POSITION_CLOSED"
                if state.remaining_quantity == _ZERO
                else "UNPROTECTED_POSITION"
            )
            if state.remaining_quantity > _ZERO:
                self._mark_unprotected(state, "protective order filled without closing position")
            else:
                self._unprotected_cycles.discard(state.cycle_id)
            return
        if status in {"CANCELED", "CANCELLED"}:
            replace_after_cancel = state.pending_replacement
            state.confirmed_exit_quantity += state.protective_cumulative_filled
            state.protective_order_id = None
            state.protective_order_quantity = _ZERO
            state.protective_cumulative_filled = _ZERO
            state.pending_replacement = False
            if state.pending_exit_quantity > _ZERO:
                await self._release_pending_exit(state)
            elif replace_after_cancel and state.remaining_quantity > _ZERO:
                await self._place_protection(state)
            elif state.remaining_quantity > _ZERO:
                self._mark_unprotected(state, "protective order canceled without replacement")
            return
        if status in {"REJECTED", "EXPIRED"}:
            state.confirmed_exit_quantity += state.protective_cumulative_filled
            state.protective_order_id = None
            state.protective_order_quantity = _ZERO
            state.protective_cumulative_filled = _ZERO
            state.pending_replacement = False
            self._mark_unprotected(state, f"protective order became {status}")
            return
        if status not in _TERMINAL:
            # ACCEPTED/PENDING/NEW updates preserve the currently effective order.
            return

    async def _apply_exit_update(
        self, state: _CycleState, status: str, cumulative_filled: Decimal
    ) -> None:
        working = state.working_exit
        assert working is not None
        if cumulative_filled < working.cumulative_filled:
            raise ValueError("exit cumulative filled quantity must not decrease")
        if cumulative_filled > working.quantity:
            raise ValueError("exit filled quantity exceeds its order quantity")
        delta = cumulative_filled - working.cumulative_filled
        working.cumulative_filled = cumulative_filled
        state.confirmed_exit_quantity += delta

        if status in {"PARTIALLY_FILLED", "PARTIAL_FILL"}:
            state.status = "EXIT_PARTIALLY_FILLED"
            return
        if status in _TERMINAL:
            state.working_exit = None
            if state.remaining_quantity == _ZERO:
                state.status = "POSITION_CLOSED"
                self._unprotected_cycles.discard(state.cycle_id)
            else:
                await self._place_protection(state)

    def _mark_unprotected(self, state: _CycleState, message: str) -> None:
        state.status = "UNPROTECTED_POSITION"
        state.last_error = message
        state.updated_at = self._now()
        self._unprotected_cycles.add(state.cycle_id)

    def _assert_sell_invariant(self, state: _CycleState) -> None:
        working = state.working_protective_quantity + state.working_exit_quantity
        if working > state.remaining_quantity:
            raise RuntimeError(
                "working protective plus exit quantity exceeds confirmed remaining long quantity"
            )
        if state.protective_order_id is not None and state.working_exit is not None:
            raise RuntimeError("protective and discretionary sell orders must not overlap")

    @staticmethod
    def _snapshot(state: _CycleState) -> _ProtectionSnapshot:
        return _ProtectionSnapshot(
            cycle_id=state.cycle_id,
            status=state.status,
            confirmed_remaining_quantity=state.remaining_quantity,
            protective_order_id=state.protective_order_id,
            protective_working_quantity=state.working_protective_quantity,
            exit_order_id=None if state.working_exit is None else state.working_exit.order_id,
            exit_working_quantity=state.working_exit_quantity,
            pending_replacement=state.pending_replacement,
            pending_exit_quantity=state.pending_exit_quantity,
            updated_at=state.updated_at,
            last_error=state.last_error,
        )
