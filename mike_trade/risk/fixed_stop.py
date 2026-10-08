"""Fixed protective stops and protective-order coverage reconciliation.

Pure, provider-neutral functions for a long-only position cycle.  Nothing here
touches a broker, clock, or credential: every function maps explicit inputs to
a value, and :func:`reconcile_after_fill` returns a *plan* (orders to cancel,
an order intent to place) that a coordinator executes.

Safety rules enforced here:

* Stop prices are floored to the injected tick grid, so rounding never raises a
  long stop above the computed level.
* A stop must be strictly positive and strictly below its reference price.
* A long stop never moves downward: replacements keep ``max(previous, new)`` and
  an explicit lower value is rejected.
* A replacement is planned as cancel, then (after the cancel is confirmed in the
  broker order snapshot) place.  A new order is only ever planned when no
  effective protective order remains, so protective sells never overlap.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from mike_trade.domain.trading import (
    TERMINAL_ORDER_STATUSES,
    BrokerOrderStatus,
    Fill,
    OrderPurpose,
    OrderSide,
    OrderType,
    TimeInForce,
)

from .stop_policy import ProtectionMode, ProtectionPolicy, _order_semantics, _tif

__all__ = [
    "compute_fixed_stop_price",
    "build_fixed_stop_order",
    "check_coverage",
    "CoverageResult",
    "reconcile_after_fill",
    "CoverageState",
    "RecommendedAction",
    "InvalidStopPrice",
    "TickRules",
    "ALPACA_EQUITY_TICK_RULES",
    "ProtectiveOrderIntent",
    "ProtectiveOrderView",
    "FixedStopCycleState",
    "ReconcileResult",
]

_ZERO = Decimal("0")
_HUNDRED = Decimal("100")
_STOP_LIMIT = "STOP_LIMIT"
_CONFIRMED_FILL_PRICE = "CONFIRMED_FILL_PRICE"

STOP_LIMIT_RISK_NOTE = "A stop-limit order may not fill if price gaps through the limit."


class InvalidStopPrice(ValueError):
    """A stop could not be computed or accepted; ``reason_code`` says why."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(f"{reason_code}: {message}")
        self.reason_code = reason_code


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite() or value < _ZERO or (positive and value == _ZERO):
        relation = "> 0" if positive else ">= 0"
        raise ValueError(f"{name} must be a finite Decimal {relation}")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


# --------------------------------------------------------------------------- ticks


@dataclass(frozen=True, slots=True)
class TickRules:
    """Price grid: ``increment_below`` under ``threshold``, else ``increment_at_or_above``."""

    threshold: Decimal
    increment_at_or_above: Decimal
    increment_below: Decimal

    def __post_init__(self) -> None:
        for name in ("threshold", "increment_at_or_above", "increment_below"):
            _decimal(getattr(self, name), name, positive=True)

    def increment_for(self, price: Decimal) -> Decimal:
        return self.increment_at_or_above if price >= self.threshold else self.increment_below

    def floor(self, price: Decimal) -> Decimal:
        """Round ``price`` down (never up) to the grid.  May return zero."""

        increment = self.increment_for(_decimal(price, "price"))
        return (price / increment).to_integral_value(rounding=ROUND_FLOOR) * increment


# Alpaca equity convention: $0.01 at or above $1.00, $0.0001 below.
ALPACA_EQUITY_TICK_RULES = TickRules(
    threshold=Decimal("1.00"),
    increment_at_or_above=Decimal("0.01"),
    increment_below=Decimal("0.0001"),
)


# ------------------------------------------------------------------- stop price


def compute_fixed_stop_price(
    policy: ProtectionPolicy,
    reference_price: Decimal,
    atr: Decimal | None = None,
    support: Decimal | None = None,
    *,
    tick_rules: TickRules,
    support_buffer: Decimal = _ZERO,
) -> Decimal:
    """Return the tick-floored long stop for a FIXED ``policy``.

    ``reference_price`` is normally the confirmed average fill price.  Exactly
    one distance applies (the policy guarantees it): percent, USD, ATR multiple,
    or support.  For a support policy ``support`` is the current support level
    (falling back to ``policy.support_level``) and ``support_buffer`` is
    subtracted from it.
    """

    if not isinstance(policy, ProtectionPolicy):
        raise TypeError("policy must be ProtectionPolicy")
    if policy.mode is not ProtectionMode.FIXED:
        raise ValueError("compute_fixed_stop_price requires a FIXED policy")
    if not isinstance(tick_rules, TickRules):
        raise TypeError("tick_rules must be TickRules")
    reference = _decimal(reference_price, "reference_price", positive=True)
    buffer = _decimal(support_buffer, "support_buffer")

    if policy.distance_percent is not None:
        raw = reference * (_HUNDRED - policy.distance_percent) / _HUNDRED
    elif policy.distance_usd is not None:
        raw = reference - policy.distance_usd
    elif policy.atr_multiple is not None:
        if atr is None:
            raise InvalidStopPrice("STOP_INPUT_MISSING", "ATR is required for an ATR-multiple stop")
        raw = reference - policy.atr_multiple * _decimal(atr, "atr", positive=True)
    else:
        level = policy.support_level if support is None else support
        if level is None:
            raise InvalidStopPrice("STOP_INPUT_MISSING", "support level is required")
        raw = _decimal(level, "support", positive=True) - buffer

    if raw <= _ZERO:
        raise InvalidStopPrice("STOP_NOT_POSITIVE", f"computed stop {raw} is not positive")
    stop = tick_rules.floor(raw)
    if stop <= _ZERO:
        raise InvalidStopPrice("STOP_NOT_POSITIVE", f"stop {raw} floors to zero on the tick grid")
    if stop >= reference:
        raise InvalidStopPrice(
            "STOP_NOT_BELOW_REFERENCE", f"stop {stop} is not below reference {reference}"
        )
    return stop


# ------------------------------------------------------------------ order intent


@dataclass(frozen=True, slots=True)
class ProtectiveOrderIntent:
    """Provider-neutral SELL stop / stop-limit intent for a long position."""

    client_order_id: str
    cycle_id: str
    symbol: str
    revision: int
    quantity: Decimal
    order_type: OrderType
    stop_price: Decimal
    limit_price: Decimal | None
    time_in_force: TimeInForce
    side: OrderSide = OrderSide.SELL
    purpose: OrderPurpose = OrderPurpose.PROTECTIVE_STOP
    risk_notes: tuple[str, ...] = ()


def _client_order_id(cycle_id: str, revision: int) -> str:
    digest = hashlib.sha256(f"{len(cycle_id)}:{cycle_id}:{revision}".encode()).hexdigest()[:24]
    return f"mt-fs-r{revision}-{digest}"


def build_fixed_stop_order(
    cycle_id: str,
    symbol: str,
    covered_qty: Decimal,
    stop_price: Decimal,
    semantics: str | OrderType,
    tif: TimeInForce | str,
    *,
    revision: int = 0,
    limit_offset: Decimal | None = None,
    tick_rules: TickRules | None = None,
    previous_stop_price: Decimal | None = None,
) -> ProtectiveOrderIntent:
    """Build the SELL protective intent; the client order id is ``f(cycle_id, revision)``.

    STOP_LIMIT needs ``limit_offset`` and ``tick_rules``; the limit is
    ``stop - offset`` floored to the grid (so ``limit <= stop``).  If
    ``previous_stop_price`` is given, a lower ``stop_price`` is rejected.
    """

    cycle = _text(cycle_id, "cycle_id")
    sym = _text(symbol, "symbol").upper()
    quantity = _decimal(covered_qty, "covered_qty", positive=True)
    stop = _decimal(stop_price, "stop_price", positive=True)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("revision must be an int >= 0")
    kind = _order_semantics(semantics)
    time_in_force = _tif(tif)
    if previous_stop_price is not None and stop < _decimal(
        previous_stop_price, "previous_stop_price", positive=True
    ):
        raise InvalidStopPrice(
            "STOP_DOWNWARD_MOVE", f"stop {stop} is below previous stop {previous_stop_price}"
        )

    limit: Decimal | None = None
    notes: tuple[str, ...] = ()
    order_type = OrderType.STOP
    if kind == _STOP_LIMIT:
        if limit_offset is None or tick_rules is None:
            raise ValueError("STOP_LIMIT requires limit_offset and tick_rules")
        offset = _decimal(limit_offset, "limit_offset", positive=True)
        limit = tick_rules.floor(stop - offset)
        if limit <= _ZERO:
            raise InvalidStopPrice("LIMIT_NOT_POSITIVE", f"limit {stop - offset} is not positive")
        order_type = OrderType.STOP_LIMIT
        notes = (STOP_LIMIT_RISK_NOTE,)
    elif limit_offset is not None:
        raise ValueError("STOP_MARKET does not accept limit_offset")

    return ProtectiveOrderIntent(
        client_order_id=_client_order_id(cycle, revision),
        cycle_id=cycle,
        symbol=sym,
        revision=revision,
        quantity=quantity,
        order_type=order_type,
        stop_price=stop,
        limit_price=limit,
        time_in_force=time_in_force,
        risk_notes=notes,
    )


# ---------------------------------------------------------------------- coverage


class CoverageState(StrEnum):
    COVERED = "COVERED"
    UNDER_COVERED = "UNDER_COVERED"
    OVER_COVERED = "OVER_COVERED"
    MULTIPLE_EFFECTIVE = "MULTIPLE_EFFECTIVE"
    NONE = "NONE"


class RecommendedAction(StrEnum):
    NO_OP = "NO_OP"
    REPLACE_TO_QTY = "REPLACE_TO_QTY"
    CANCEL_EXCESS = "CANCEL_EXCESS"
    PLACE = "PLACE"


@dataclass(frozen=True, slots=True)
class ProtectiveOrderView:
    """Minimal broker-order snapshot used for coverage.

    Field names match :class:`mike_trade.domain.trading.BrokerOrder`, which can
    be passed to :func:`check_coverage` directly.
    """

    client_order_id: str
    quantity: Decimal
    filled_quantity: Decimal
    status: BrokerOrderStatus
    stop_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CoverageResult:
    state: CoverageState
    action: RecommendedAction
    remaining_qty: Decimal
    open_qty: Decimal
    effective_order_ids: tuple[str, ...]
    cancel_order_ids: tuple[str, ...] = ()
    target_qty: Decimal = _ZERO


def _open_qty(order: object) -> Decimal:
    quantity = _decimal(order.quantity, "order quantity")
    filled = _decimal(order.filled_quantity, "order filled_quantity")
    if filled > quantity:
        raise ValueError("order filled_quantity must not exceed quantity")
    return quantity - filled


def _is_effective(order: object) -> bool:
    # Anything not known terminal (including PENDING_CANCEL and UNKNOWN) may still
    # execute, so it counts toward open sell quantity.
    status = order.status
    if not isinstance(status, BrokerOrderStatus):
        raise TypeError("order status must be BrokerOrderStatus")
    return status not in TERMINAL_ORDER_STATUSES


def check_coverage(
    confirmed_remaining_qty: Decimal, protective_orders: Sequence[object]
) -> CoverageResult:
    """Classify protective coverage of a confirmed remaining long quantity.

    Precedence: nothing effective is NONE; zero remaining or open quantity above
    remaining is OVER_COVERED (oversell risk); several effective orders are
    MULTIPLE_EFFECTIVE; one order below remaining is UNDER_COVERED.  Excess
    cancellation keeps at most one order: the largest whose open quantity does
    not exceed the remaining quantity (ties: higher stop, then id).
    """

    remaining = _decimal(confirmed_remaining_qty, "confirmed_remaining_qty")
    effective = [order for order in protective_orders if _is_effective(order)]
    ids = tuple(str(order.client_order_id) for order in effective)  # type: ignore[attr-defined]
    open_qty = sum((_open_qty(order) for order in effective), _ZERO)

    def result(state: CoverageState, action: RecommendedAction, cancel=()) -> CoverageResult:
        return CoverageResult(state, action, remaining, open_qty, ids, tuple(cancel), remaining)

    if not effective:
        if remaining == _ZERO:
            return result(CoverageState.NONE, RecommendedAction.NO_OP)
        return result(CoverageState.NONE, RecommendedAction.PLACE)

    if remaining == _ZERO or open_qty > remaining or len(effective) > 1:
        state = (
            CoverageState.OVER_COVERED
            if remaining == _ZERO or open_qty > remaining
            else CoverageState.MULTIPLE_EFFECTIVE
        )
        keepers = [
            order for order in effective if remaining > _ZERO and _open_qty(order) <= remaining
        ]
        keeper = min(
            keepers,
            key=lambda o: (
                -_open_qty(o),
                -(getattr(o, "stop_price", None) or _ZERO),
                str(o.client_order_id),  # type: ignore[attr-defined]
            ),
            default=None,
        )
        cancel = [i for o, i in zip(effective, ids, strict=True) if o is not keeper]
        return result(state, RecommendedAction.CANCEL_EXCESS, cancel)

    if open_qty < remaining:
        return result(CoverageState.UNDER_COVERED, RecommendedAction.REPLACE_TO_QTY, ids)
    return result(CoverageState.COVERED, RecommendedAction.NO_OP)


# -------------------------------------------------------------------- reconcile


@dataclass(frozen=True, slots=True)
class FixedStopCycleState:
    """Immutable fixed-stop view of one long cycle (cumulative confirmed fills)."""

    cycle_id: str
    symbol: str
    policy: ProtectionPolicy
    tick_rules: TickRules
    entry_qty: Decimal = _ZERO
    entry_cost: Decimal = _ZERO
    exit_qty: Decimal = _ZERO
    applied_fill_ids: tuple[str, ...] = ()
    protective_orders: tuple[object, ...] = ()
    last_stop_price: Decimal | None = None
    revision: int = 0
    atr: Decimal | None = None
    support: Decimal | None = None
    reference_price: Decimal | None = None
    support_buffer: Decimal = _ZERO

    @property
    def remaining_qty(self) -> Decimal:
        return self.entry_qty - self.exit_qty

    @property
    def avg_fill_price(self) -> Decimal | None:
        return None if self.entry_qty == _ZERO else self.entry_cost / self.entry_qty


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Plan after a fill: cancel ``cancel_order_ids``, then place ``place`` if set.

    ``awaiting_cancel`` means a cancel is already in flight: do nothing until the
    order snapshot shows it terminal, then call again.  ``reason_code`` is set
    when a needed stop could not be built; existing protection is left alone.
    """

    coverage: CoverageResult
    state: FixedStopCycleState
    cancel_order_ids: tuple[str, ...] = ()
    place: ProtectiveOrderIntent | None = None
    awaiting_cancel: bool = False
    reason_code: str | None = None


def _apply_fill(state: FixedStopCycleState, fill: Fill) -> FixedStopCycleState:
    if not isinstance(fill, Fill):
        raise TypeError("fill_event must be a Fill")
    if fill.symbol != state.symbol.strip().upper():
        raise ValueError("fill symbol does not match the cycle")
    if fill.fill_id in state.applied_fill_ids:
        return state
    ids = state.applied_fill_ids + (fill.fill_id,)
    if fill.side is OrderSide.BUY:
        return replace(
            state,
            entry_qty=state.entry_qty + fill.quantity,
            entry_cost=state.entry_cost + fill.quantity * fill.price,
            applied_fill_ids=ids,
        )
    if state.exit_qty + fill.quantity > state.entry_qty:
        raise ValueError("cumulative exit quantity must not exceed entry quantity")
    return replace(state, exit_qty=state.exit_qty + fill.quantity, applied_fill_ids=ids)


def _planned_stop(state: FixedStopCycleState) -> Decimal:
    policy = state.policy
    reference = state.reference_price
    if reference is None:
        if policy.fixed_reference != _CONFIRMED_FILL_PRICE:
            raise InvalidStopPrice(
                "STOP_INPUT_MISSING", f"reference {policy.fixed_reference} was not resolved"
            )
        reference = state.avg_fill_price
    if reference is None:
        raise InvalidStopPrice("STOP_INPUT_MISSING", "no confirmed fill price")
    stop = compute_fixed_stop_price(
        policy,
        reference,
        state.atr,
        state.support,
        tick_rules=state.tick_rules,
        support_buffer=state.support_buffer,
    )
    floor = [p for p in (state.last_stop_price,) if p is not None]
    floor += [
        o.stop_price  # type: ignore[attr-defined]
        for o in state.protective_orders
        if _is_effective(o) and getattr(o, "stop_price", None) is not None
    ]
    stop = max([stop, *floor])
    if stop >= reference:
        # A ratcheted stop can sit at/above a lower post-fill reference; do not place it.
        raise InvalidStopPrice(
            "STOP_NOT_BELOW_REFERENCE", f"ratcheted stop {stop} is not below reference {reference}"
        )
    return stop


def _intent(state: FixedStopCycleState, stop: Decimal, qty: Decimal) -> ProtectiveOrderIntent:
    policy = state.policy
    return build_fixed_stop_order(
        state.cycle_id,
        state.symbol,
        qty,
        stop,
        policy.order_semantics,
        policy.time_in_force,
        revision=state.revision + 1,
        limit_offset=policy.limit_offset,
        tick_rules=state.tick_rules,
        previous_stop_price=state.last_stop_price,
    )


def reconcile_after_fill(
    cycle_state: FixedStopCycleState, fill_event: Fill | None = None
) -> ReconcileResult:
    """Apply an entry (BUY) or exit (SELL) fill and plan protective coverage.

    Idempotent per ``fill_id``.  Call again with ``fill_event=None`` and an
    updated ``protective_orders`` snapshot once a planned cancel is confirmed.
    """

    if not isinstance(cycle_state, FixedStopCycleState):
        raise TypeError("cycle_state must be FixedStopCycleState")
    state = cycle_state if fill_event is None else _apply_fill(cycle_state, fill_event)
    coverage = check_coverage(state.remaining_qty, state.protective_orders)

    def done(**kw: object) -> ReconcileResult:
        return ReconcileResult(coverage=coverage, state=state, **kw)  # type: ignore[arg-type]

    if coverage.action is RecommendedAction.NO_OP:
        return done()

    cancelling = any(
        o.status is BrokerOrderStatus.PENDING_CANCEL
        for o in state.protective_orders
        if o.client_order_id in coverage.effective_order_ids  # type: ignore[attr-defined]
    )
    if cancelling:
        return done(awaiting_cancel=True)

    if coverage.action is RecommendedAction.CANCEL_EXCESS:
        # Oversell safety comes first: cancel regardless of whether a new stop is valid.
        return done(cancel_order_ids=coverage.cancel_order_ids)

    try:
        stop = _planned_stop(state)
        intent = _intent(state, stop, state.remaining_qty)
    except InvalidStopPrice as exc:
        return done(reason_code=exc.reason_code)

    if coverage.action is RecommendedAction.REPLACE_TO_QTY:
        return done(cancel_order_ids=coverage.cancel_order_ids)

    placed = replace(state, revision=intent.revision, last_stop_price=stop)
    return ReconcileResult(coverage=coverage, state=placed, place=intent)
