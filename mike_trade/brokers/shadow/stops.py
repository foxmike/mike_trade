"""Ledger-backed shadow simulation of long and short protective stops.

This module is deliberately broker-free.  Stop prices come from the shared
provider-neutral protection policies, while executions are ordinary shadow
account fills against quote evidence.

A long stop sits below the market, triggers when the bid falls to it and sells at the bid.
A short stop sits above the market, triggers when the ask rises to it and covers at the ask
(``BUY_TO_COVER``); a short trailing stop ratchets DOWN as the ask makes new lows and never
loosens upward.  ``trigger_source="BID"`` means the closing-side quote: bid for a long,
ask for a short.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from mike_trade.brokers.shadow.account import ShadowAccount
from mike_trade.brokers.shadow.ledger import ORDER_CANCELED, ORDER_REPLACED, SIMULATOR_VERSION, ShadowLedgerEvent
from mike_trade.domain.protection import ProtectionKind, ProtectionPolicy, start_protection
from mike_trade.domain.trading import (
    Fill, OrderIntent, OrderPurpose, OrderSide, OrderType, PositionDirection, TimeInForce,
)
from mike_trade.market_data.context import QuoteSnapshot, TradeSnapshot
from mike_trade.risk.trailing import (
    DistanceKind,
    SteppedTrailingPolicy,
    SteppedTrailingState,
    advance,
    initial_state,
)

_ZERO = Decimal("0")
_AUDIT_KEY = "shadow_stop_record"

__all__ = [
    "ShadowStopSimulator",
    "ShadowStopState",
    "StopTriggerAssumptions",
    "StopTriggerOutcome",
]


class StopTriggerOutcome(StrEnum):
    UNKNOWN = "UNKNOWN"
    NOT_TRIGGERED = "NOT_TRIGGERED"
    TRIGGERED = "TRIGGERED"
    CANCELED = "CANCELED"


@dataclass(frozen=True, slots=True)
class StopTriggerAssumptions:
    """Explicit inputs that define trigger eligibility and estimated execution."""

    trigger_source: str = "BID"
    max_quote_age: timedelta = timedelta(seconds=5)
    stop_slippage: Decimal = _ZERO
    simulator_version: str = SIMULATOR_VERSION

    def __post_init__(self) -> None:
        source = self.trigger_source.strip().upper() if isinstance(self.trigger_source, str) else ""
        if source not in {"BID", "LAST"}:
            raise ValueError("trigger_source must be BID or LAST")
        object.__setattr__(self, "trigger_source", source)
        if not isinstance(self.max_quote_age, timedelta) or self.max_quote_age <= timedelta(0):
            raise ValueError("max_quote_age must be a positive timedelta")
        if not isinstance(self.stop_slippage, Decimal) or not self.stop_slippage.is_finite() or self.stop_slippage < _ZERO:
            raise ValueError("stop_slippage must be a finite Decimal >= 0")
        if not isinstance(self.simulator_version, str) or not self.simulator_version.strip():
            raise ValueError("simulator_version must be non-empty")

    def as_mapping(self) -> dict[str, str]:
        return {
            "trigger_source": self.trigger_source,
            "max_quote_age_us": str(self.max_quote_age // timedelta(microseconds=1)),
            "stop_slippage": str(self.stop_slippage),
            "execution_price": "long:available_bid_minus_stop_slippage;short:available_ask_plus_stop_slippage",
            "estimated": "True",
            "broker_confirmed": "False",
        }


@dataclass(frozen=True, slots=True)
class ShadowStopState:
    cycle_id: str
    symbol: str
    policy: ProtectionPolicy
    status: str
    outcome: StopTriggerOutcome
    stop_price: Decimal
    high_water_mark: Decimal
    covered_quantity: Decimal
    remaining_quantity: Decimal
    entry_reference: Decimal
    order_id: str | None
    as_of: datetime
    reason: str | None = None
    stepped_state: SteppedTrailingState | None = None
    simulator_version: str = SIMULATOR_VERSION
    assumptions: tuple[tuple[str, str], ...] = ()
    provenance: str | None = None
    estimated: bool = True
    broker_confirmed: bool = False
    direction: PositionDirection = PositionDirection.LONG

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.as_of.utcoffset() != timedelta(0):
            raise ValueError("as_of must be timezone-aware UTC")
        for field in ("stop_price", "high_water_mark", "covered_quantity", "remaining_quantity", "entry_reference"):
            value = getattr(self, field)
            if not isinstance(value, Decimal) or not value.is_finite() or value < _ZERO:
                raise ValueError(f"{field} must be a finite Decimal >= 0")
        if self.estimated is not True or self.broker_confirmed is not False:
            raise ValueError("shadow stop records are estimated and not broker-confirmed")


class ShadowStopSimulator:
    """Simulate protective stops through one :class:`ShadowAccount`."""

    def __init__(self, account: ShadowAccount, assumptions: StopTriggerAssumptions | None = None) -> None:
        if not isinstance(account, ShadowAccount):
            raise TypeError("account must be a ShadowAccount")
        self.account = account
        self.assumptions = assumptions or StopTriggerAssumptions()
        if not isinstance(self.assumptions, StopTriggerAssumptions):
            raise TypeError("assumptions must be StopTriggerAssumptions")
        self._states: dict[str, ShadowStopState] = {}
        self._restore()

    @property
    def version(self) -> str:
        return self.assumptions.simulator_version

    def state(self, cycle_id: str) -> ShadowStopState | None:
        return self._states.get(cycle_id)

    async def arm(self, cycle_id: str, symbol: str, policy: ProtectionPolicy, *, at: datetime) -> ShadowStopState | None:
        """Create or resize protection from the position created by positive fills."""
        position = self.account.state.positions.get(symbol.upper())
        if position is None or position.quantity <= _ZERO:
            return None
        if position.cycle_id != cycle_id:
            raise ValueError("cycle_id does not identify the held shadow position")
        if not isinstance(policy, ProtectionPolicy):
            raise TypeError("policy must be ProtectionPolicy")
        _utc(at)
        previous = self._states.get(cycle_id)
        reference = position.avg_cost
        direction = position.direction
        long = direction is PositionDirection.LONG
        cycle = _PositionLike(cycle_id, self.account.scope.scope_id, position.symbol,
                              position.quantity, direction)
        proposed = start_protection(policy, cycle, reference)
        tighter = max if long else min  # a long stop only rises, a short stop only falls
        stop = proposed.stop_price if previous is None else tighter(previous.stop_price, proposed.stop_price)
        high = reference if previous is None else tighter(previous.high_water_mark, reference)
        step_state = previous.stepped_state if previous is not None else None
        if policy.kind is ProtectionKind.STEPPED_TRAILING_STOP and step_state is None:
            step_state = initial_state(reference, self._step_policy(policy), side=direction.value)
        order_id = previous.order_id if previous is not None else None
        state = self._new_state(cycle_id, position.symbol, policy, "ACTIVE", StopTriggerOutcome.NOT_TRIGGERED,
                                stop, high, position.quantity, position.quantity,
                                previous.entry_reference if previous is not None else reference, order_id, at,
                                stepped_state=step_state, provenance="shadow_fill",
                                direction=direction)
        previous_order = None if order_id is None else self.account.state.orders.get(order_id)
        if order_id is None or previous_order is None or not previous_order.is_open:
            # No resting stop (first arm, or the previous one was cancelled for an exit
            # that did not complete): restore coverage with a fresh stop order.
            order_id = await self._submit_stop(state, at)
            state = replace(state, order_id=order_id)
        else:
            order = self.account.state.orders[order_id]
            if order.is_open and (order.stop_price != stop or order.remaining != position.quantity):
                await self.account.replace_order(
                    order_id,
                    stop_price=stop,
                    quantity=order.filled_quantity + position.quantity,
                    at=at,
                )
        self._states[cycle_id] = state
        self._record(state, at, "ARMED_OR_RESIZED", None)
        return state

    async def evaluate(self, cycle_id: str, quote: QuoteSnapshot | None, *, at: datetime,
                       trade: TradeSnapshot | None = None) -> ShadowStopState:
        """Update trailing state, evaluate an explicit trigger, and execute if hit."""
        _utc(at)
        state = self._states[cycle_id]
        position = self.account.state.positions.get(state.symbol)
        if position is None:
            return await self._cancel_for_exit(state, at, reason="POSITION_CLOSED")
        state = replace(state, covered_quantity=position.quantity, remaining_quantity=position.quantity)
        if self._pending_exit(state.symbol):
            return await self._cancel_for_exit(state, at)
        quote_reason = self._quote_reason(quote, at, state.symbol)
        if quote_reason is not None:
            return self._save(replace(state, outcome=StopTriggerOutcome.UNKNOWN, status="UNKNOWN",
                                      as_of=at, reason=quote_reason), at, "EVALUATION_UNKNOWN", quote)
        assert quote is not None
        direction = position.direction
        long = direction is PositionDirection.LONG
        state = replace(state, direction=direction)
        # Michelle's transition policy explicitly follows traded prices. Existing
        # policies retain their quote-side high-water semantics.
        touch = quote.bid if long else quote.ask
        favorable = touch
        if state.policy.kind is ProtectionKind.FIXED_THEN_TRAILING_STOP:
            if (
                trade is not None
                and trade.symbol == state.symbol
                and trade.provider_ts <= at
                and trade.receive_ts <= at
                and at - trade.provider_ts <= self.assumptions.max_quote_age
            ):
                favorable = trade.price
        high = max(state.high_water_mark, favorable) if long else min(state.high_water_mark, favorable)
        stop = state.stop_price
        step_state = state.stepped_state
        tighter = max if long else min
        if state.policy.kind is ProtectionKind.BROKER_TRAILING_STOP:
            moved = start_protection(state.policy, _PositionLike(state.cycle_id, self.account.scope.scope_id,
                                      state.symbol, position.quantity, direction), high)
            stop = tighter(stop, moved.stop_price)
        elif state.policy.kind is ProtectionKind.STEPPED_TRAILING_STOP:
            assert step_state is not None
            activation = state.policy.activation_gain or _ZERO
            activated = (high >= state.entry_reference + activation if long
                         else high <= state.entry_reference - activation)
            if activated:
                step_state = advance(step_state, high, self._step_policy(state.policy))
                stop = tighter(stop, step_state.candidate_stop)
        elif state.policy.kind is ProtectionKind.FIXED_THEN_TRAILING_STOP:
            assert state.policy.trail_percent is not None
            assert state.policy.activation_reference is not None
            distance = state.policy.trail_percent / Decimal(100)
            candidate = high * (Decimal(1) - distance) if long else high * (Decimal(1) + distance)
            activated = (
                candidate > state.policy.activation_reference
                if long else candidate < state.policy.activation_reference
            )
            if activated:
                stop = tighter(stop, candidate)
        state = replace(state, high_water_mark=high, stop_price=stop, stepped_state=step_state,
                        as_of=at, status="ACTIVE", reason=None, provenance=self._provenance(quote))
        resting = self.account.state.orders[state.order_id].stop_price
        if (stop > (resting or _ZERO)) if long else (resting is None or stop < resting):
            await self.account.replace_order(state.order_id, stop_price=stop, at=at)
        trigger = self._trigger_value(quote, trade, at, direction)
        if trigger is None:
            state = replace(state, outcome=StopTriggerOutcome.UNKNOWN, status="UNKNOWN", reason="TRIGGER_INPUT_MISSING")
            return self._save(state, at, "EVALUATION_UNKNOWN", quote)
        if (trigger > stop) if long else (trigger < stop):
            state = replace(state, outcome=StopTriggerOutcome.NOT_TRIGGERED)
            return self._save(state, at, "EVALUATED_NOT_TRIGGERED", quote)
        # Adverse slippage: a long sells below the bid, a short covers above the ask.
        fill_price = touch - self.assumptions.stop_slippage if long else touch + self.assumptions.stop_slippage
        if fill_price <= _ZERO:
            state = replace(state, outcome=StopTriggerOutcome.UNKNOWN, status="UNKNOWN", reason="NON_POSITIVE_EXECUTION_PRICE")
            return self._save(state, at, "EVALUATION_UNKNOWN", quote)
        terminal_state = replace(state, outcome=StopTriggerOutcome.TRIGGERED, status="TRIGGERED",
                                 covered_quantity=_ZERO, remaining_quantity=_ZERO, reason="STOP_FILLED")
        self._record(terminal_state, at, "TRIGGERED", quote)
        order = self.account.state.orders[state.order_id]
        remaining = order.remaining
        fill = Fill(fill_id=f"shadow-stop:{cycle_id}:{quote.provider_ts.isoformat()}:{touch}",
                    broker_order_id=state.order_id, account_scope_id=self.account.scope.scope_id,
                    symbol=state.symbol, side=OrderSide.SELL if long else OrderSide.BUY_TO_COVER,
                    quantity=min(remaining, position.quantity),
                    price=fill_price, fee=_ZERO, filled_at=quote.provider_ts)
        self.account.apply_fill(fill, slippage=self.assumptions.stop_slippage, feed=self._provenance(quote))
        result = terminal_state
        self._states[cycle_id] = result
        return result

    async def prepare_discretionary_exit(self, cycle_id: str, *, at: datetime) -> None:
        """Cancel a pending protective sell before submitting an ordinary exit."""
        state = self._states.get(cycle_id)
        if state is not None and state.status not in {"CANCELED", "TRIGGERED"}:
            await self._cancel_for_exit(state, at)

    async def close_cycle(self, cycle_id: str, *, at: datetime) -> None:
        """Retire protection whose position has been closed by another path."""
        state = self._states.get(cycle_id)
        if state is not None and state.status not in {"CANCELED", "TRIGGERED"}:
            await self._cancel_for_exit(state, at, reason="POSITION_CLOSED")

    async def submit_discretionary_exit(self, intent: OrderIntent):
        """Cancel protection and submit the ordinary exit as one coordinated operation."""
        if not isinstance(intent, OrderIntent) or not intent.side.is_exit:
            raise TypeError("intent must be a SELL or BUY_TO_COVER OrderIntent")
        await self.prepare_discretionary_exit(intent.cycle_id, at=intent.created_at)
        return await self.account.submit_order(intent)

    def _pending_exit(self, symbol: str) -> bool:
        return any(order.symbol == symbol and order.side.is_exit
                   and order.purpose is not OrderPurpose.PROTECTIVE_STOP and order.is_open
                   for order in self.account.state.orders.values())

    async def _cancel_for_exit(self, state: ShadowStopState, at: datetime,
                               *, reason: str = "DISCRETIONARY_EXIT_PENDING") -> ShadowStopState:
        canceled = replace(state, status="CANCELED", outcome=StopTriggerOutcome.CANCELED,
                           covered_quantity=_ZERO, remaining_quantity=_ZERO, as_of=at, reason=reason)
        self._record(canceled, at, "CANCELED_FOR_DISCRETIONARY_EXIT", None)
        if state.order_id:
            order = self.account.state.orders.get(state.order_id)
            if order is not None and order.is_open:
                await self.account.cancel_order(state.order_id, at=at)
        self._states[state.cycle_id] = canceled
        return canceled

    async def _submit_stop(self, state: ShadowStopState, at: datetime) -> str:
        client_id = f"shadow-stop-{state.cycle_id}-{uuid4().hex}"
        intent = OrderIntent(
            intent_id=f"stop-intent-{uuid4().hex}", client_order_id=client_id,
            account_scope_id=self.account.scope.scope_id, cycle_id=state.cycle_id,
            symbol=state.symbol,
            side=OrderSide.SELL if state.direction is PositionDirection.LONG else OrderSide.BUY_TO_COVER,
            purpose=OrderPurpose.PROTECTIVE_STOP,
            order_type=OrderType.STOP, time_in_force=TimeInForce.GTC, created_at=at,
            quantity=state.covered_quantity, stop_price=state.stop_price)
        order = await self.account.submit_order(intent)
        return order.broker_order_id

    def _new_state(self, cycle: str, symbol: str, policy: ProtectionPolicy, status: str,
                   outcome: StopTriggerOutcome, stop: Decimal, high: Decimal, covered: Decimal,
                   remaining: Decimal, entry: Decimal, order_id: str | None, at: datetime,
                   *, stepped_state: SteppedTrailingState | None = None,
                   reason: str | None = None, provenance: str | None = None,
                   direction: PositionDirection = PositionDirection.LONG) -> ShadowStopState:
        return ShadowStopState(cycle, symbol, policy, status, outcome, stop, high, covered,
                               remaining, entry, order_id, at, reason, stepped_state, self.version,
                               tuple(sorted(self.assumptions.as_mapping().items())), provenance,
                               direction=direction)

    def _save(self, state: ShadowStopState, at: datetime, event: str,
              quote: QuoteSnapshot | None) -> ShadowStopState:
        self._states[state.cycle_id] = state
        self._record(state, at, event, quote)
        return state

    def _record(self, state: ShadowStopState, at: datetime, event: str,
                quote: QuoteSnapshot | None) -> None:
        provenance = self._provenance(quote)
        if provenance is None:
            provenance = "shadow_fill" if event == "ARMED_OR_RESIZED" else "no_quote_evidence"
        data = {"event": event, "cycle_id": state.cycle_id, "symbol": state.symbol,
                "status": state.status, "outcome": state.outcome.value,
                "stop_price": state.stop_price, "high_water_mark": state.high_water_mark,
                "covered_quantity": state.covered_quantity, "remaining_quantity": state.remaining_quantity,
                "entry_reference": state.entry_reference, "order_id": state.order_id,
                "reason": state.reason, "policy": _policy_data(state.policy),
                "stepped_state": _step_state_data(state.stepped_state),
                "assumptions": dict(state.assumptions), "provenance": provenance,
                "direction": state.direction.value,
                "estimated": True, "broker_confirmed": False}
        order_id = state.order_id
        if order_id is None:
            return
        order = self.account.state.orders.get(order_id)
        if order is None or not order.is_open:
            # Closed/canceled/fill events remain fully represented by the underlying ledger facts.
            return
        event_record = ShadowLedgerEvent(
            event_id=f"{self.account.scope.scope_id}:shadow-stop:{state.cycle_id}:{uuid4().hex}",
            kind=ORDER_REPLACED, scope=self.account.scope, occurred_at=at, data={
                "broker_order_id": order_id, "stop_price": state.stop_price, _AUDIT_KEY: data},
            simulator_version=self.version, assumptions=dict(state.assumptions),
            feed=self._provenance(quote))
        self.account._commit(event_record)

    def _restore(self) -> None:
        for event in self.account._ledger.read_events():
            record = event.data.get(_AUDIT_KEY)
            if not isinstance(record, Mapping):
                continue
            state = _state_from_record(dict(record), event.occurred_at, event.simulator_version)
            self._states[state.cycle_id] = state

    def _quote_reason(self, quote: QuoteSnapshot | None, at: datetime, symbol: str) -> str | None:
        if quote is None:
            return "QUOTE_MISSING"
        if quote.symbol != symbol:
            return "QUOTE_SYMBOL_MISMATCH"
        if quote.provider_ts > at or quote.receive_ts > at:
            return "QUOTE_FROM_FUTURE"
        if at - quote.provider_ts > self.assumptions.max_quote_age:
            return "QUOTE_STALE"
        return None

    def _trigger_value(self, quote: QuoteSnapshot, trade: TradeSnapshot | None,
                       at: datetime, direction: PositionDirection = PositionDirection.LONG) -> Decimal | None:
        if self.assumptions.trigger_source == "BID":
            return quote.bid if direction is PositionDirection.LONG else quote.ask
        if trade is None or trade.symbol != quote.symbol or trade.provider_ts > at or trade.receive_ts > at:
            return None
        if at - trade.provider_ts > self.assumptions.max_quote_age:
            return None
        return trade.price

    @staticmethod
    def _provenance(quote: QuoteSnapshot | None) -> str | None:
        if quote is None:
            return None
        return f"{quote.feed_id}:quote(provider_ts={quote.provider_ts.isoformat()},receive_ts={quote.receive_ts.isoformat()})"

    @staticmethod
    def _step_policy(policy: ProtectionPolicy) -> SteppedTrailingPolicy:
        kind = DistanceKind.PERCENT if policy.trail_percent is not None else DistanceKind.USD
        distance = policy.trail_percent if policy.trail_percent is not None else policy.trail_amount
        assert distance is not None and policy.step_size is not None
        return SteppedTrailingPolicy(kind, distance, DistanceKind.USD, policy.step_size,
                                     timedelta(0), TimeInForce.GTC, "shadow-shared-policy")


@dataclass(frozen=True)
class _PositionLike:
    cycle_id: str
    account_scope_id: str
    symbol: str
    quantity: Decimal
    direction: PositionDirection = PositionDirection.LONG

    @property
    def entry_quantity(self) -> Decimal:
        return self.quantity

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity


def _utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("times must be timezone-aware UTC")


def _policy_data(policy: ProtectionPolicy) -> dict[str, object]:
    values: dict[str, object] = {"kind": policy.kind.value, "require_broker_held": policy.require_broker_held}
    for name in (
        "stop_offset_percent", "stop_offset_amount", "trail_percent", "trail_amount",
        "step_size", "activation_gain", "initial_stop_price", "activation_reference",
    ):
        value = getattr(policy, name)
        values[name] = None if value is None else str(value)
    return values


def _step_state_data(state: SteppedTrailingState | None) -> dict[str, object] | None:
    if state is None:
        return None
    return {name: str(getattr(state, name)) for name in ("entry_reference", "high_watermark", "ratchet_anchor", "candidate_stop", "steps_advanced", "direction")}


def _state_from_record(data: dict[str, Any], at: datetime, version: str) -> ShadowStopState:
    raw = data["policy"]
    policy = ProtectionPolicy(kind=ProtectionKind(raw["kind"]), **{
        name: (None if raw[name] is None else Decimal(raw[name])) if name != "require_broker_held" else raw[name]
        for name in (
            "stop_offset_percent", "stop_offset_amount", "trail_percent", "trail_amount",
            "step_size", "activation_gain", "initial_stop_price", "activation_reference",
            "require_broker_held",
        )})
    raw_step = data.get("stepped_state")
    step = None if raw_step is None else SteppedTrailingState(
        Decimal(raw_step["entry_reference"]), Decimal(raw_step["high_watermark"]),
        Decimal(raw_step["ratchet_anchor"]), Decimal(raw_step["candidate_stop"]), int(raw_step["steps_advanced"]),
        str(raw_step.get("direction", "LONG")))
    return ShadowStopState(data["cycle_id"], data["symbol"], policy, data["status"],
        StopTriggerOutcome(data["outcome"]), Decimal(data["stop_price"]), Decimal(data["high_water_mark"]),
        Decimal(data["covered_quantity"]), Decimal(data["remaining_quantity"]), Decimal(data["entry_reference"]),
        data["order_id"], at, data.get("reason"), step, version,
        tuple(sorted(data["assumptions"].items())), data.get("provenance"),
        data["estimated"], data["broker_confirmed"],
        PositionDirection(data.get("direction", PositionDirection.LONG.value)))
