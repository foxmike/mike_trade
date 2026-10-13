"""Sell-side order I/O for PAPER/LIVE: protective stops, replacements, closes.

``LiveExecutor`` is the broker port behind the existing
:class:`~mike_trade.execution.close_coordinator.CloseCoordinator`.  The coordinator already
solves the dangerous part (serialising every sell-side mutation of a position, cancelling and
*confirming* the protective stop before an exit is sent, re-protecting when the exit fails or
only partly fills, freezing on an unknown outcome); this module supplies the Alpaca/ledger
side of that contract and the protection policy mechanics on top:

* ``establish`` places the initial broker-held stop for the quantity actually filled;
* ``ratchet`` tightens it (Michelle fixed-to-trailing, stepped trailing) with a PATCH that is
  only believed once the broker shows the old order ``replaced`` - until then the previous
  stop remains the authoritative protection, so no gap is created and no atomic replace is
  claimed (Alpaca's replace is asynchronous: the old order keeps working until it is
  replaced);
* ``resize`` / ``restore`` keep the stop equal to the managed quantity after a partial
  fill, an external sale, or a human cancelling the stop;
* every order is recorded as an intent *before* the POST and linked to its broker order after.

A trailing stop only ever tightens: candidates are floored to the tick grid and compared with
the last broker-confirmed stop.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from mike_trade.domain.errors import BrokerError, BrokerRejectedError
from mike_trade.domain.protection import (
    ProtectionKind,
    ProtectionPolicy,
    ProtectionStatus,
    TrailingStopState,
    start_protection,
)
from mike_trade.domain.trading import (
    BrokerOrder,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    PositionCycle,
    TimeInForce,
)
from mike_trade.execution.close_coordinator import CloseCoordinator, CloseOutcome, SellIntent
from mike_trade.live.gateway import (
    LiveGateway,
    OrderNotSubmitted,
    UnknownOrderOutcome,
)
from mike_trade.live.ledger import LiveLedger
from mike_trade.risk.trailing import (
    DistanceKind,
    SteppedTrailingPolicy,
    advance,
    initial_state,
)

__all__ = [
    "LiveCloseCoordinator",
    "LiveExecutor",
    "ProtectionFailure",
    "StopRuntime",
    "ceil_to_tick",
    "floor_to_tick",
    "policy_from_dict",
    "policy_to_dict",
    "ratchet_candidate",
]

ZERO = Decimal("0")
_EXIT_PURPOSE = {
    "STRATEGY": OrderPurpose.STRATEGY_EXIT,
    "STRATEGY_EXIT": OrderPurpose.STRATEGY_EXIT,
    "EXIT_LONG": OrderPurpose.STRATEGY_EXIT,
    "MANUAL": OrderPurpose.MANUAL_CLOSE,
    "MANUAL_CLOSE": OrderPurpose.MANUAL_CLOSE,
    "CLOSE_ALL": OrderPurpose.MANUAL_CLOSE,
    "FLATTEN": OrderPurpose.MANUAL_CLOSE,
    "END_OF_DAY": OrderPurpose.EOD_CLOSE,
}


class ProtectionFailure(RuntimeError):
    """The engine could not establish or keep broker-held protection for a position."""


class LiveCloseCoordinator(CloseCoordinator):
    """``CloseCoordinator`` plus the two reconciliation hooks it deliberately lacks."""

    def unfreeze(self, position_id: str) -> None:
        """Clear a freeze once broker reconciliation has re-established the facts."""
        self._frozen.discard(position_id)

    def forget(self, position_id: str) -> None:
        self._frozen.discard(position_id)
        self._protections.pop(position_id, None)

    def protection(self, position_id: str) -> tuple[str | None, Decimal | None]:
        known = self._protections.get(position_id)
        return (None, None) if known is None else (known.order_id, known.stop_price)


@dataclass(slots=True)
class StopRuntime:
    """Live view of one cycle's protective stop (persisted as ``trailing_stop_states``)."""

    cycle_id: str
    symbol: str
    policy: ProtectionPolicy
    entry_reference: Decimal
    order_id: str | None
    stop_price: Decimal
    high: Decimal
    covered: Decimal
    status: ProtectionStatus = ProtectionStatus.ACTIVE
    last_replace_at: datetime | None = None

    @property
    def native(self) -> bool:
        return self.policy.kind is ProtectionKind.BROKER_TRAILING_STOP


def policy_to_dict(policy: ProtectionPolicy) -> dict[str, object]:
    """JSON-safe form of a protection policy (persisted with the entry facts)."""
    values: dict[str, object] = {"kind": policy.kind.value, "require_broker_held": policy.require_broker_held}
    for name in (
        "stop_offset_percent", "stop_offset_amount", "trail_percent", "trail_amount",
        "step_size", "activation_gain", "initial_stop_price", "activation_reference",
    ):
        value = getattr(policy, name)
        values[name] = None if value is None else str(value)
    return values


def policy_from_dict(data: Mapping[str, object]) -> ProtectionPolicy:
    fields = {
        name: (None if data.get(name) is None else Decimal(str(data[name])))
        for name in (
            "stop_offset_percent", "stop_offset_amount", "trail_percent", "trail_amount",
            "step_size", "activation_gain", "initial_stop_price", "activation_reference",
        )
    }
    return ProtectionPolicy(
        kind=ProtectionKind(str(data["kind"])), require_broker_held=bool(data.get("require_broker_held", True)),
        **fields,
    )


def ceil_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    """Round a long stop UP onto the broker's price grid (used only for Michelle's initial stop,
    so rounding can shrink, never enlarge, the $0.25 initial distance)."""
    return (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def floor_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    """Round a long stop DOWN onto the broker's price grid (never tighter than designed)."""
    return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _step_policy(policy: ProtectionPolicy) -> SteppedTrailingPolicy:
    kind = DistanceKind.PERCENT if policy.trail_percent is not None else DistanceKind.USD
    distance = policy.trail_percent if policy.trail_percent is not None else policy.trail_amount
    assert distance is not None and policy.step_size is not None
    return SteppedTrailingPolicy(
        kind, distance, DistanceKind.USD, policy.step_size, timedelta(0), TimeInForce.GTC,
        "live-shared-policy",
    )


def ratchet_candidate(
    policy: ProtectionPolicy, entry_reference: Decimal, high: Decimal
) -> Decimal | None:
    """The stop a trailing policy asks for given the highest traded price, or ``None``.

    Pure and restart-safe: every candidate is a function of ``(entry_reference, high)`` alone,
    so the persisted high-water mark reconstructs it exactly.
    """
    if policy.kind is ProtectionKind.FIXED_THEN_TRAILING_STOP:
        assert policy.trail_percent is not None and policy.activation_reference is not None
        candidate = high * (Decimal(1) - policy.trail_percent / Decimal(100))
        # Michelle: the raw candidate must exceed the initial stop; the caller additionally
        # requires the tick-adjusted candidate to strictly exceed it (see ``ratchet``).
        return candidate if candidate > policy.activation_reference else None
    if policy.kind is ProtectionKind.STEPPED_TRAILING_STOP:
        activation = policy.activation_gain or ZERO
        if high < entry_reference + activation:
            return None
        step_policy = _step_policy(policy)
        state = advance(initial_state(entry_reference, step_policy), high, step_policy)
        return state.candidate_stop
    return None


@dataclass(slots=True)
class LiveExecutor:
    """Alpaca/ledger implementation of the close-coordinator port plus stop management."""

    gateway: LiveGateway
    ledger: LiveLedger
    clock: Any
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep
    cancel_polls: int = 6
    cancel_poll_interval: float = 0.4
    min_replace_interval: timedelta = timedelta(seconds=1)
    stops: dict[str, StopRuntime] = field(default_factory=dict)
    events: deque[Mapping[str, object]] = field(default_factory=lambda: deque(maxlen=500))
    coordinator: LiveCloseCoordinator = field(init=False)

    def __post_init__(self) -> None:
        self.coordinator = LiveCloseCoordinator(self, self.clock, self._sink)

    # ================================================================== events

    def _sink(self, event: Mapping[str, object]) -> None:
        self.events.append(event)
        try:
            self.ledger.event(
                "protection",
                str(event.get("position_id") or event.get("symbol") or "engine"),
                str(event["event_type"]),
                {k: v for k, v in event.items() if k != "event_type"},
            )
        except Exception:  # journaling must never break an order path
            pass

    def emit(self, event_type: str, **payload: object) -> None:
        self._sink({"event_type": event_type, "occurred_at": self.clock.now(), **payload})

    # ===================================================== port: reads (broker)

    def confirmed_position_quantity(self, position_id: str, symbol: str) -> Decimal:
        position = self.gateway.position(symbol)
        return ZERO if position is None or position.side != "long" else position.quantity

    def list_open_orders(self) -> tuple[BrokerOrder, ...]:
        return self.gateway.snapshot().open_orders

    def get_order(self, order_id: str) -> BrokerOrder | None:
        order = self.gateway.get_order(order_id)
        if order is not None:
            self.ledger.upsert_order(order)
        return order

    async def cancel_order(self, order_id: str) -> BrokerOrder | None:
        """Request cancellation and wait (bounded) for a TERMINAL status.

        Alpaca reports ``pending_cancel`` before ``canceled``; a stop is only treated as gone
        once the broker says so, never on the request's acknowledgement.
        """
        order = self.gateway.cancel_order(order_id)
        for _ in range(self.cancel_polls):
            if order is None or order.is_terminal:
                break
            await self.sleep(self.cancel_poll_interval)
            order = self.gateway.get_order(order_id)
        if order is not None:
            self.ledger.upsert_order(order)
        return order

    # ================================================== port: order placement

    def _intent(
        self, cycle_id: str, symbol: str, purpose: OrderPurpose, quantity: Decimal, *,
        order_type: OrderType, tif: TimeInForce, attempt: int, client_order_id: str,
        stop_price: Decimal | None = None, trail_percent: Decimal | None = None,
        signal_id: str | None = None, side: OrderSide = OrderSide.SELL,
    ) -> OrderIntent:
        return OrderIntent(
            intent_id=f"{cycle_id}:{purpose.value}:{attempt}",
            client_order_id=client_order_id,
            account_scope_id=self.ledger.scope_id,
            cycle_id=cycle_id,
            symbol=symbol,
            side=side,
            purpose=purpose,
            order_type=order_type,
            time_in_force=tif,
            created_at=self.clock.now(),
            quantity=quantity,
            stop_price=stop_price,
            trail_percent=trail_percent,
            # ``order_intents.signal_id`` is a foreign key into ``signals`` (not populated by the
            # engine); the signal id is already part of the cycle id, so it is not duplicated.
            signal_id=None,
        )

    def _place(
        self, intent: OrderIntent, attempt: int, send: Callable[[], BrokerOrder],
        reservation: Decimal = ZERO,
    ) -> BrokerOrder:
        """Record the intent durably, send once, link the broker order to the intent."""
        receipt = self.ledger.record_intent(intent, reservation)
        holder: list[BrokerOrder] = []

        def submit(_receipt: object) -> str | None:
            order = send()
            holder.append(order)
            return order.broker_order_id

        try:
            self.ledger.submit_recorded(receipt, submit)
        except OrderNotSubmitted:
            self.ledger.mark_not_submitted(intent.intent_id, "broker confirms the order is absent")
            raise
        except UnknownOrderOutcome:
            raise
        except BrokerError as exc:
            # A 4xx refusal (insufficient qty, invalid stop, capability) created no order.
            status = getattr(exc, "status_code", None)
            if isinstance(status, int) and 400 <= status < 500 and status not in (408, 429):
                self.ledger.mark_not_submitted(intent.intent_id, f"refused: {exc}")
            elif getattr(exc, "code", "") in {"capability_unavailable", "oversell_rejected"}:
                self.ledger.mark_not_submitted(intent.intent_id, f"refused: {exc}")
            raise
        order = holder[0]
        self.ledger.upsert_order(order, intent_id=intent.intent_id)
        return order

    def submit_entry(
        self, cycle_id: str, symbol: str, quantity: Decimal, reservation: Decimal, signal_id: str
    ) -> BrokerOrder:
        """Budget-reserve and market-BUY ``quantity`` shares for a new cycle."""
        attempt = self.ledger.next_attempt(cycle_id, OrderPurpose.ENTRY)
        client_id = self.gateway.client_order_id(cycle_id, OrderPurpose.ENTRY, attempt)
        domain = self._intent(
            cycle_id, symbol, OrderPurpose.ENTRY, quantity, order_type=OrderType.MARKET,
            tif=TimeInForce.DAY, attempt=attempt, client_order_id=client_id,
            signal_id=signal_id, side=OrderSide.BUY,
        )
        return self._place(
            domain, attempt, lambda: self.gateway.submit_market(domain, attempt), reservation
        )

    def submit_exit(self, intent: SellIntent, quantity: Decimal) -> BrokerOrder:
        """Market-sell ``quantity`` (CloseCoordinator calls this after the stop is confirmed gone)."""
        purpose = _EXIT_PURPOSE.get(str(intent.kind), OrderPurpose.SAFETY_EXIT)
        attempt = self.ledger.next_attempt(intent.position_id, purpose)
        client_id = self.gateway.client_order_id(intent.position_id, purpose, attempt)
        domain = self._intent(
            intent.position_id, intent.symbol, purpose, quantity, order_type=OrderType.MARKET,
            tif=TimeInForce.DAY, attempt=attempt, client_order_id=client_id,
        )
        return self._place(domain, attempt, lambda: self.gateway.submit_market(domain, attempt))

    def _place_stop(self, cycle_id: str, symbol: str, quantity: Decimal, stop_price: Decimal) -> BrokerOrder:
        attempt = self.ledger.next_attempt(cycle_id, OrderPurpose.PROTECTIVE_STOP)
        client_id = self.gateway.client_order_id(cycle_id, OrderPurpose.PROTECTIVE_STOP, attempt)
        domain = self._intent(
            cycle_id, symbol, OrderPurpose.PROTECTIVE_STOP, quantity, order_type=OrderType.STOP,
            tif=TimeInForce.GTC, attempt=attempt, client_order_id=client_id, stop_price=stop_price,
        )
        return self._place(
            domain, attempt, lambda: self.gateway.submit_stop(cycle_id, symbol, quantity, stop_price, attempt)
        )

    def _place_trailing(self, cycle_id: str, symbol: str, quantity: Decimal, percent: Decimal) -> BrokerOrder:
        attempt = self.ledger.next_attempt(cycle_id, OrderPurpose.PROTECTIVE_STOP)
        # The service derives the TRAILING_STOP lineage id; the ledger intent must carry it.
        client_id = self.gateway.trailing_client_order_id(cycle_id, attempt)  
        domain = self._intent(
            cycle_id, symbol, OrderPurpose.PROTECTIVE_STOP, quantity,
            order_type=OrderType.TRAILING_STOP, tif=TimeInForce.GTC, attempt=attempt,
            client_order_id=client_id, trail_percent=percent,
        )
        return self._place(
            domain, attempt,
            lambda: self.gateway.submit_trailing_stop(cycle_id, symbol, quantity, percent, attempt),
        )

    def submit_protective_stop(
        self, position_id: str, symbol: str, quantity: Decimal, stop_price: Decimal,
        client_order_id: str, **_direction: object,
    ) -> BrokerOrder:
        """Re-protect after a failed/partial exit (CloseCoordinator contract)."""
        runtime = self.stops.get(position_id)
        if runtime is not None and runtime.native:
            assert runtime.policy.trail_percent is not None
            order = self._place_trailing(position_id, symbol, quantity, runtime.policy.trail_percent)
        else:
            order = self._place_stop(position_id, symbol, quantity, stop_price)
        if runtime is not None:
            runtime.order_id, runtime.covered, runtime.status = (
                order.broker_order_id, quantity, ProtectionStatus.ACTIVE,
            )
            self._persist(runtime)
        return order

    def replace_protective_stop(
        self, order_id: str, position_id: str, symbol: str, quantity: Decimal,
        stop_price: Decimal, client_order_id: str, **_direction: object,
    ) -> BrokerOrder:
        """PATCH a working stop; the old order is authoritative until the broker confirms.

        Returns the REPLACEMENT order when the broker confirms it, otherwise the unchanged OLD
        order (still working, still protecting) - never an UNKNOWN that would freeze the
        position over a routine ratchet.
        """
        old = self.gateway.get_order(order_id)
        if old is None or old.is_terminal:
            raise BrokerRejectedError(
                f"stop {order_id} is no longer working", code="stop_not_working"
            )
        attempt = self.ledger.next_attempt(position_id, OrderPurpose.PROTECTIVE_STOP)
        client_id = self.gateway.client_order_id(position_id, OrderPurpose.PROTECTIVE_STOP, attempt)
        domain = self._intent(
            position_id, symbol, OrderPurpose.PROTECTIVE_STOP, quantity, order_type=OrderType.STOP,
            tif=TimeInForce.GTC, attempt=attempt, client_order_id=client_id, stop_price=stop_price,
        )
        receipt = self.ledger.record_intent(domain)
        holder: list[BrokerOrder] = []

        def submit(_receipt: object) -> str | None:
            replacement = self.gateway.replace_stop(order_id, stop_price, quantity, client_id)
            if replacement is None:
                raise _Unconfirmed(order_id)
            holder.append(replacement)
            return replacement.broker_order_id

        try:
            self.ledger.submit_recorded(receipt, submit)
        except _Unconfirmed:
            # The old order is still the broker-confirmed protection; resolve the intent later
            # from a client-order-id lookup (reconciler) in case the PATCH did land.
            self.emit("STOP_REPLACE_UNCONFIRMED", position_id=position_id, symbol=symbol,
                      order_id=order_id, requested_stop=stop_price)
            return old
        replacement = holder[0]
        self.ledger.upsert_order(replacement, intent_id=domain.intent_id)
        refreshed_old = self.gateway.get_order(order_id)
        if refreshed_old is not None:
            self.ledger.upsert_order(refreshed_old)
        return replacement

    # ============================================================ stop lifecycle

    def _runtime_from_ledger(self, cycle: PositionCycle, policy: ProtectionPolicy) -> StopRuntime | None:
        state = self.ledger.load_stop(cycle.cycle_id)
        if state is None or state.status in (ProtectionStatus.CANCELED, ProtectionStatus.TRIGGERED):
            return None
        reference = cycle.avg_entry_price
        assert reference is not None
        return StopRuntime(
            cycle.cycle_id, cycle.symbol, policy, reference, state.broker_order_id,
            state.stop_price, state.high_water_mark, state.covered_quantity, state.status,
        )

    def runtime(self, cycle: PositionCycle, policy: ProtectionPolicy) -> StopRuntime | None:
        known = self.stops.get(cycle.cycle_id)
        if known is not None:
            return known
        restored = self._runtime_from_ledger(cycle, policy)
        if restored is not None:
            self.stops[cycle.cycle_id] = restored
            if restored.order_id:
                self.coordinator.register_protective_stop(
                    position_id=cycle.cycle_id, symbol=cycle.symbol,
                    order_id=restored.order_id, stop_price=restored.stop_price,
                )
        return restored

    def _persist(self, runtime: StopRuntime) -> None:
        state = TrailingStopState(
            cycle_id=runtime.cycle_id,
            account_scope_id=self.ledger.scope_id,
            symbol=runtime.symbol,
            kind=runtime.policy.kind,
            status=runtime.status,
            covered_quantity=runtime.covered,
            stop_price=runtime.stop_price,
            high_water_mark=max(runtime.high, runtime.stop_price),
            broker_order_id=runtime.order_id,
            last_confirmed_at=self.clock.now(),
        )
        saved = self.ledger.save_stop(state)
        runtime.stop_price = saved.stop_price
        runtime.high = saved.high_water_mark
        if runtime.order_id:
            self.coordinator.register_protective_stop(
                position_id=runtime.cycle_id, symbol=runtime.symbol,
                order_id=runtime.order_id, stop_price=runtime.stop_price,
            )

    def establish(
        self, cycle: PositionCycle, quantity: Decimal, entry_price: Decimal, policy: ProtectionPolicy
    ) -> StopRuntime:
        """Place the initial broker-held stop for ``quantity`` actually-filled shares."""
        if quantity <= ZERO:
            raise ProtectionFailure("nothing to protect")
        tick = self.gateway.tick_size(cycle.symbol)
        proposed = start_protection(policy, _live_cycle(cycle, self.ledger.scope_id, quantity), entry_price)
        if policy.kind is ProtectionKind.FIXED_THEN_TRAILING_STOP:
            stop = ceil_to_tick(proposed.stop_price, tick)
        else:
            stop = floor_to_tick(proposed.stop_price, tick)
        if stop <= ZERO or stop >= entry_price:
            raise ProtectionFailure(
                f"initial stop {stop} is not below the entry fill {entry_price}"
            )
        try:
            if policy.kind is ProtectionKind.BROKER_TRAILING_STOP:
                assert policy.trail_percent is not None
                order = self._place_trailing(cycle.cycle_id, cycle.symbol, quantity, policy.trail_percent)
            else:
                order = self._place_stop(cycle.cycle_id, cycle.symbol, quantity, stop)
        except (BrokerError, OrderNotSubmitted, UnknownOrderOutcome) as exc:
            raise ProtectionFailure(f"protective stop was not established: {exc}") from exc
        runtime = StopRuntime(
            cycle.cycle_id, cycle.symbol, policy, entry_price, order.broker_order_id,
            stop, max(entry_price, stop), quantity, ProtectionStatus.ACTIVE,
        )
        self.stops[cycle.cycle_id] = runtime
        self._persist(runtime)
        self.ledger.stop_event(
            cycle.cycle_id, "PROTECTION_ESTABLISHED", new=stop,
            payload={"order_id": order.broker_order_id, "quantity": quantity, "kind": policy.kind},
        )
        return runtime

    def restore(self, runtime: StopRuntime, quantity: Decimal, reason: str) -> StopRuntime:
        """Re-place a stop that vanished (e.g. a human cancelled it), never below the last stop."""
        if quantity <= ZERO:
            raise ProtectionFailure("nothing to protect")
        try:
            if runtime.native:
                assert runtime.policy.trail_percent is not None
                order = self._place_trailing(runtime.cycle_id, runtime.symbol, quantity, runtime.policy.trail_percent)
            else:
                order = self._place_stop(runtime.cycle_id, runtime.symbol, quantity, runtime.stop_price)
        except (BrokerError, OrderNotSubmitted, UnknownOrderOutcome) as exc:
            raise ProtectionFailure(f"protective stop could not be restored: {exc}") from exc
        runtime.order_id, runtime.covered, runtime.status = (
            order.broker_order_id, quantity, ProtectionStatus.ACTIVE,
        )
        self._persist(runtime)
        self.ledger.stop_event(
            runtime.cycle_id, "PROTECTION_RESTORED", new=runtime.stop_price,
            payload={"order_id": order.broker_order_id, "quantity": quantity, "reason": reason},
        )
        return runtime

    async def resize(self, runtime: StopRuntime, quantity: Decimal, reason: str) -> StopRuntime:
        """Make the stop cover exactly ``quantity`` shares (same stop price)."""
        if quantity == runtime.covered or runtime.order_id is None:
            return runtime
        if runtime.native:
            # Alpaca trailing-stop quantity replacement is not a proven capability: cancel the
            # (confirmed) stop and place a fresh one for the new quantity immediately.
            assert runtime.policy.trail_percent is not None
            old = runtime.order_id
            self.coordinator.forget(runtime.cycle_id)
            cancelled = await self.cancel_order(old)
            if cancelled is not None and not cancelled.is_terminal:
                raise ProtectionFailure("trailing stop cancel is unconfirmed; not resizing")
            order = self._place_trailing(runtime.cycle_id, runtime.symbol, quantity, runtime.policy.trail_percent)
            runtime.order_id, runtime.covered = order.broker_order_id, quantity
            self._persist(runtime)
            self.ledger.stop_event(runtime.cycle_id, "COVERAGE_RESIZED", new=runtime.stop_price,
                                   payload={"quantity": quantity, "reason": reason, "method": "cancel_resubmit"})
            return runtime
        outcome = await self.coordinator.replace_protective_stop(
            position_id=runtime.cycle_id, symbol=runtime.symbol, order_id=runtime.order_id,
            stop_price=runtime.stop_price,
            client_order_id=f"{runtime.cycle_id}-resize", quantity=quantity,
        )
        self._apply_replace(runtime, outcome, quantity=quantity, reason=reason)
        return runtime

    async def reassert_price(self, runtime: StopRuntime, quantity: Decimal) -> None:
        """The broker's stop is looser than the last confirmed one: tighten it back."""
        if runtime.order_id is None:
            return
        outcome = await self.coordinator.replace_protective_stop(
            position_id=runtime.cycle_id, symbol=runtime.symbol, order_id=runtime.order_id,
            stop_price=runtime.stop_price, client_order_id=f"{runtime.cycle_id}-reassert",
            quantity=quantity,
        )
        self._apply_replace(runtime, outcome, quantity=quantity, reason="reassert last stop")

    def _apply_replace(
        self, runtime: StopRuntime, outcome: CloseOutcome, *, quantity: Decimal, reason: str
    ) -> bool:
        """Adopt a replacement only when the broker produced a different, working order."""
        if outcome.status == "REPLACED" and outcome.protective_order_id:
            replaced = outcome.protective_order_id != runtime.order_id
            if replaced:
                runtime.order_id = outcome.protective_order_id
                runtime.covered = quantity
                if outcome.stop_price is not None:
                    runtime.stop_price = max(runtime.stop_price, outcome.stop_price)
                runtime.status = ProtectionStatus.ACTIVE
                runtime.last_replace_at = self.clock.now()
                self._persist(runtime)
                self.ledger.stop_event(
                    runtime.cycle_id, "STOP_REPLACED", new=runtime.stop_price,
                    payload={"order_id": runtime.order_id, "quantity": quantity, "reason": reason},
                )
            return replaced
        if outcome.status in {"FROZEN", "UNPROTECTED"}:
            runtime.status = ProtectionStatus.UNKNOWN
            self._persist(runtime)
        return False

    async def ratchet(self, runtime: StopRuntime, observed_high: Decimal) -> bool:
        """Raise the high-water mark and tighten the broker stop if the policy asks for it."""
        if observed_high > runtime.high:
            runtime.high = observed_high
        candidate = ratchet_candidate(runtime.policy, runtime.entry_reference, runtime.high)
        if candidate is None or runtime.order_id is None or runtime.native:
            self._persist_quietly(runtime)
            return False
        tick = self.gateway.tick_size(runtime.symbol)
        target = floor_to_tick(candidate, tick)
        if (
            runtime.policy.kind is ProtectionKind.FIXED_THEN_TRAILING_STOP
            and runtime.policy.activation_reference is not None
            and target <= runtime.policy.activation_reference
        ):
            self._persist_quietly(runtime)  # tick-adjusted candidate has not passed the initial stop
            return False
        if target <= runtime.stop_price:
            self._persist_quietly(runtime)
            return False
        now = self.clock.now()
        if runtime.last_replace_at is not None and now - runtime.last_replace_at < self.min_replace_interval:
            return False
        previous = runtime.stop_price
        outcome = await self.coordinator.replace_protective_stop(
            position_id=runtime.cycle_id, symbol=runtime.symbol, order_id=runtime.order_id,
            stop_price=target, client_order_id=f"{runtime.cycle_id}-ratchet", quantity=runtime.covered,
        )
        runtime.last_replace_at = now
        changed = self._apply_replace(runtime, outcome, quantity=runtime.covered, reason="ratchet")
        if changed:
            self.ledger.stop_event(runtime.cycle_id, "TRAIL_TIGHTENED", previous=previous,
                                   new=runtime.stop_price, payload={"high": runtime.high})
        return changed

    def _persist_quietly(self, runtime: StopRuntime) -> None:
        try:
            self._persist(runtime)
        except ValueError:
            pass

    # ================================================================== closes

    async def close(
        self, cycle: PositionCycle, quantity: Decimal, kind: str, *, signal_id: str,
        runtime: StopRuntime | None,
    ) -> CloseOutcome:
        """Cancel-confirm the stop, sell ``quantity`` at market, re-protect on failure."""
        entry_fill = cycle.entry_fills[0].filled_at if cycle.entry_fills else None
        return await self.coordinator.close(
            SellIntent(
                position_id=cycle.cycle_id,
                symbol=cycle.symbol,
                quantity=quantity,
                kind=kind,
                client_order_id=f"{cycle.cycle_id}-{signal_id}",
                first_confirmed_entry_fill_at=entry_fill,
                protective_order_id=None if runtime is None else runtime.order_id,
                last_confirmed_stop_price=None if runtime is None else runtime.stop_price,
            )
        )

    def retire(self, cycle_id: str, status: ProtectionStatus = ProtectionStatus.CANCELED) -> None:
        """Drop in-memory protection state for a cycle that is closed at the broker."""
        runtime = self.stops.pop(cycle_id, None)
        self.coordinator.forget(cycle_id)
        if runtime is not None:
            runtime.status = status


class _Unconfirmed(Exception):
    def __init__(self, order_id: str) -> None:
        super().__init__(order_id)
        self.order_id = order_id


def _live_cycle(cycle: PositionCycle, scope_id: str, quantity: Decimal) -> Any:
    from mike_trade.domain.trading import PositionDirection

    class _Like:
        cycle_id = cycle.cycle_id
        account_scope_id = scope_id
        symbol = cycle.symbol
        entry_quantity = quantity
        remaining_quantity = quantity
        direction = PositionDirection.LONG

    return _Like()

