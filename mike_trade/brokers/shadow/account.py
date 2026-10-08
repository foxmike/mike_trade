"""Durable local SHADOW account implementing the broker port.

SHADOW mode never touches Alpaca (or any other) order endpoint: every order,
fill, cash adjustment and eligibility change is an estimated fact appended to a
:class:`~mike_trade.brokers.shadow.ledger.ShadowLedger`.  Account state is only
ever produced by replaying those events, so a restart from the same database
reproduces cash, positions, P&L and counters exactly and cannot reset them.

Design choices:
1. One reducer.  :meth:`ShadowAccountState.validate` and ``apply`` are pure over
   (state, event); live operations and restart replay run the same pair, so the
   rules below also guard a tampered ledger.
2. Rejections reuse the domain errors with stable ``code`` values:
   ``BrokerRejectedError`` (averaging_down, pyramiding, partial_exit, oversell,
   no_position, starting_cash_already_set, ...), ``EntryBlockedError`` (the
   ``EntryBlockReason`` values, per_symbol_budget, total_budget, entry_kill_switch)
   and ``InsufficientBuyingPowerError`` (cash_negative).
3. A cycle is one long entry (possibly several partial fills of one order) and
   one complete exit.  Sells must equal the held quantity at submission; broker
   partial fills of that order accumulate.  Realized P&L per cycle is proceeds
   minus cost minus every fee (matching ``PositionCycle.realized_pnl``); interim
   partial-exit P&L uses proportional cost, and the closing fill re-derives the
   cycle total from exact sums so no rounding residue survives.
4. Positions without a supplied quote are valued at cost basis, so equity always
   equals starting cash + adjustments + realized + unrealized P&L.
5. Entry-eligibility settings reuse ``EntryEligibilityPolicy`` and
   ``EntryBlockReason``; the session date is the America/New_York exchange
   session (non-trading instants belong to the previous session), as in
   ``risk.entry_eligibility``.
6. Notional orders are unsupported; quantities may be fractional only when the
   account is created with ``allow_fractional=True``.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from mike_trade.brokers.shadow.ledger import (
    ACCOUNT_CREATED,
    CASH_ADJUSTED,
    FILL_RECORDED,
    LOCKOUT_CLEARED,
    ORDER_CANCELED,
    ORDER_REPLACED,
    ORDER_SUBMITTED,
    SIMULATOR_VERSION,
    ShadowLedger,
    ShadowLedgerEvent,
)
from mike_trade.domain.accounts import (
    AccountEnvironment,
    AccountScope,
    AccountSnapshot,
    BrokerCapabilities,
)
from mike_trade.domain.errors import (
    BrokerRejectedError,
    EntryBlockedError,
    InsufficientBuyingPowerError,
    MikeTradeError,
    ModeIsolationError,
    PersistenceError,
    UnsupportedOrderCapabilityError,
)
from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    EntryEligibilityPolicy,
    Fill,
    ManagementStatus,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    Position,
    TimeInForce,
)
from mike_trade.persistence.schema import decimal_from_db, from_db_timestamp
from mike_trade.risk.entry_eligibility import EntryBlockReason
from mike_trade.scheduling.calendar import NEW_YORK, is_trading_day, previous_session

__all__ = [
    "ShadowAccount",
    "ShadowAccountState",
    "ShadowPosition",
]

_ZERO = Decimal("0")
_ONE_MICROSECOND = timedelta(microseconds=1)
_OPEN_STATUSES = frozenset({BrokerOrderStatus.ACCEPTED, BrokerOrderStatus.PARTIALLY_FILLED})
_SOURCE = "shadow:simulated;estimated=True;broker_confirmed=False"


# --------------------------------------------------------------------------- helpers


def _reject(code: str, message: str) -> BrokerRejectedError:
    return BrokerRejectedError(message, code=code)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _decimal(
    value: object, name: str, *, minimum: Decimal = _ZERO, inclusive: bool = True
) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError(f"{name} must be Decimal; got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if value < minimum or (not inclusive and value == minimum):
        raise ValueError(f"{name} must be {'>=' if inclusive else '>'} {minimum}")
    return value


def _positive(value: object, name: str) -> Decimal:
    return _decimal(value, name, inclusive=False)


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value


def _session_date(at: datetime) -> date:
    """NY exchange session an instant belongs to (previous session if market closed all day)."""
    local_day = at.astimezone(NEW_YORK).date()
    if is_trading_day(local_day):
        return local_day
    return previous_session(local_day).session_date


def _d(data: Mapping[str, object], key: str) -> Decimal:
    return decimal_from_db(str(data[key]))


def _opt_d(data: Mapping[str, object], key: str) -> Decimal | None:
    value = data.get(key)
    return None if value is None else decimal_from_db(str(value))


def _ts(data: Mapping[str, object], key: str) -> datetime:
    return from_db_timestamp(str(data[key]))


def _is_fractional(quantity: Decimal) -> bool:
    return quantity != quantity.to_integral_value()


def _policy_to_data(policy: EntryEligibilityPolicy) -> dict[str, object]:
    return {
        "max_filled_entry_cycles_per_symbol_per_session": (
            policy.max_filled_entry_cycles_per_symbol_per_session
        ),
        "consecutive_loss_lockout_threshold": policy.consecutive_loss_lockout_threshold,
        "lockout_scope_by_strategy": policy.lockout_scope_by_strategy,
        "post_sale_cooldown_us": policy.post_sale_cooldown // _ONE_MICROSECOND,
        "min_hold_for_strategy_exit_us": policy.min_hold_for_strategy_exit // _ONE_MICROSECOND,
        "entry_kill_switch": policy.entry_kill_switch,
    }


def _policy_from_data(data: Mapping[str, object]) -> EntryEligibilityPolicy:
    threshold = data["consecutive_loss_lockout_threshold"]
    return EntryEligibilityPolicy(
        max_filled_entry_cycles_per_symbol_per_session=int(
            data["max_filled_entry_cycles_per_symbol_per_session"]  # type: ignore[call-overload]
        ),
        consecutive_loss_lockout_threshold=None if threshold is None else int(threshold),  # type: ignore[call-overload]
        lockout_scope_by_strategy=bool(data["lockout_scope_by_strategy"]),
        post_sale_cooldown=timedelta(microseconds=int(data["post_sale_cooldown_us"])),  # type: ignore[call-overload]
        min_hold_for_strategy_exit=timedelta(
            microseconds=int(data["min_hold_for_strategy_exit_us"])  # type: ignore[call-overload]
        ),
        entry_kill_switch=bool(data["entry_kill_switch"]),
    )


# --------------------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class ShadowPosition:
    """An open long position; all derived values are estimates, never broker-confirmed."""

    symbol: str
    quantity: Decimal
    cost_basis: Decimal
    entry_fees: Decimal
    strategy_key: str | None
    cycle_id: str
    entry_order_id: str
    opened_at: datetime
    entry_cost_total: Decimal
    entry_fees_total: Decimal
    realized_pnl: Decimal = _ZERO
    exit_proceeds: Decimal = _ZERO
    exit_fees: Decimal = _ZERO
    estimated: bool = True
    broker_confirmed: bool = False

    def __post_init__(self) -> None:
        _text(self.symbol, "symbol")
        _positive(self.quantity, "quantity")
        _positive(self.cost_basis, "cost_basis")
        _decimal(self.entry_fees, "entry_fees")
        if not isinstance(self.realized_pnl, Decimal):
            raise ValueError("realized_pnl must be Decimal")
        _utc(self.opened_at, "opened_at")
        if self.estimated is not True or self.broker_confirmed is not False:
            raise ValueError("shadow positions are always estimated and never broker-confirmed")

    @property
    def avg_cost(self) -> Decimal:
        return self.cost_basis / self.quantity


@dataclass(slots=True)
class _Order:
    broker_order_id: str
    client_order_id: str
    intent_id: str
    cycle_id: str
    symbol: str
    side: OrderSide
    purpose: OrderPurpose
    order_type: OrderType
    time_in_force: TimeInForce
    quantity: Decimal
    submitted_at: datetime
    updated_at: datetime
    reference_price: Decimal | None
    strategy_key: str | None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trail_price: Decimal | None = None
    trail_percent: Decimal | None = None
    filled_quantity: Decimal = _ZERO
    fill_value: Decimal = _ZERO
    status: BrokerOrderStatus = BrokerOrderStatus.ACCEPTED

    @property
    def is_open(self) -> bool:
        return self.status in _OPEN_STATUSES

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled_quantity

    def to_broker_order(self, account_scope_id: str) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id=self.broker_order_id,
            client_order_id=self.client_order_id,
            account_scope_id=account_scope_id,
            symbol=self.symbol,
            side=self.side,
            order_type=self.order_type,
            status=self.status,
            quantity=self.quantity,
            filled_quantity=self.filled_quantity,
            submitted_at=self.submitted_at,
            updated_at=max(self.submitted_at, self.updated_at),
            filled_avg_price=(
                self.fill_value / self.filled_quantity if self.filled_quantity > _ZERO else None
            ),
            limit_price=self.limit_price,
            stop_price=self.stop_price,
            trail_price=self.trail_price,
            trail_percent=self.trail_percent,
        )


# --------------------------------------------------------------------------- state


@dataclass(slots=True)
class ShadowAccountState:
    """Everything the account knows, rebuilt only by replaying ledger events."""

    scope: AccountScope
    created_at: datetime | None = None
    starting_cash: Decimal = _ZERO
    per_symbol_budget: Decimal = _ZERO
    total_budget: Decimal = _ZERO
    margin_enabled: bool = False
    allow_fractional: bool = False
    policy: EntryEligibilityPolicy | None = None
    simulator_version: str = SIMULATOR_VERSION
    assumptions: Mapping[str, str] = field(default_factory=dict)
    feed: str | None = None
    cash: Decimal = _ZERO
    adjustments: list[tuple[str, Decimal, str, str, datetime]] = field(default_factory=list)
    positions: dict[str, ShadowPosition] = field(default_factory=dict)
    orders: dict[str, _Order] = field(default_factory=dict)
    client_order_ids: dict[str, str] = field(default_factory=dict)
    fills: dict[str, Fill] = field(default_factory=dict)
    fill_slippage: dict[str, Decimal] = field(default_factory=dict)
    realized_pnl: Decimal = _ZERO
    closed_cycles: int = 0
    entry_cycles: dict[tuple[str, date], int] = field(default_factory=dict)
    consecutive_losses: dict[tuple[str, str], int] = field(default_factory=dict)
    cooldown_until: dict[str, datetime] = field(default_factory=dict)

    @property
    def estimated(self) -> bool:
        return True

    @property
    def broker_confirmed(self) -> bool:
        return False

    @property
    def is_created(self) -> bool:
        return self.created_at is not None

    # ---- counters (read-only views)

    def filled_entry_cycles(self, symbol: str, session: date) -> int:
        return self.entry_cycles.get((symbol.strip().upper(), session), 0)

    def losing_streak(self, symbol: str, strategy_key: str | None = None) -> int:
        return self.consecutive_losses.get((symbol.strip().upper(), strategy_key or ""), 0)

    def cooldown_expiry(self, symbol: str) -> datetime | None:
        return self.cooldown_until.get(symbol.strip().upper())

    def open_orders(self) -> list[_Order]:
        return [order for order in self.orders.values() if order.is_open]

    def reserved_buy_notional(self) -> Decimal:
        return sum(
            (
                order.remaining * order.reference_price
                for order in self.open_orders()
                if order.side is OrderSide.BUY and order.reference_price is not None
            ),
            start=_ZERO,
        )

    def entry_block(
        self, symbol: str, strategy_key: str | None, at: datetime
    ) -> tuple[str, str] | None:
        """Return ``(code, message)`` if a new entry for ``symbol`` is not eligible at ``at``."""
        policy = self.policy
        assert policy is not None
        if policy.entry_kill_switch:
            return "entry_kill_switch", "entry kill switch is on"
        session = _session_date(at)
        if (
            self.filled_entry_cycles(symbol, session)
            >= policy.max_filled_entry_cycles_per_symbol_per_session
        ):
            return (
                EntryBlockReason.MAX_ENTRY_CYCLES_REACHED.value,
                f"{symbol} reached its filled entry cycle limit for session {session}",
            )
        threshold = policy.consecutive_loss_lockout_threshold
        if threshold is not None:
            scope_key = strategy_key if policy.lockout_scope_by_strategy else None
            if self.losing_streak(symbol, scope_key) >= threshold:
                return (
                    EntryBlockReason.LOSING_STREAK_LOCKOUT.value,
                    f"{symbol} is locked out after {threshold} consecutive losing trades",
                )
        cooldown = self.cooldown_expiry(symbol)
        if cooldown is not None and at < cooldown:
            return (
                EntryBlockReason.POST_SALE_COOLDOWN.value,
                f"{symbol} is in post-sale cooldown until {cooldown.isoformat()}",
            )
        return None

    # ---- validation

    def validate(self, event: ShadowLedgerEvent) -> None:
        """Raise a domain error if ``event`` may not be applied to this state."""
        if event.kind == ACCOUNT_CREATED:
            self._validate_created(event)
            return
        if not self.is_created:
            raise PersistenceError(
                "shadow ledger has no account-created event", code="ledger_empty"
            )
        handler = {
            CASH_ADJUSTED: self._validate_adjustment,
            ORDER_SUBMITTED: self._validate_submit,
            ORDER_REPLACED: self._validate_replace,
            ORDER_CANCELED: self._validate_cancel,
            FILL_RECORDED: self._validate_fill,
            LOCKOUT_CLEARED: self._validate_lockout_clear,
        }[event.kind]
        handler(event)

    def _validate_created(self, event: ShadowLedgerEvent) -> None:
        if self.is_created:
            raise _reject(
                "starting_cash_already_set", "starting cash is set once, at account creation"
            )
        data = event.data
        _decimal(_d(data, "starting_cash"), "starting_cash")
        per_symbol = _decimal(_d(data, "per_symbol_budget"), "per_symbol_budget")
        total = _decimal(_d(data, "total_budget"), "total_budget")
        if total < per_symbol:
            raise ValueError("total_budget must be >= per_symbol_budget")

    def _validate_adjustment(self, event: ShadowLedgerEvent) -> None:
        data = event.data
        _text(data["adjustment_id"], "adjustment_id")
        _text(data["reason"], "reason")
        _text(data["actor"], "actor")
        amount = _d(data, "amount")
        if amount == _ZERO:
            raise ValueError("cash adjustment amount must be non-zero")
        if any(item[0] == data["adjustment_id"] for item in self.adjustments):
            raise _reject("duplicate_adjustment", "adjustment_id was already applied")
        if self.cash + amount < _ZERO and not self.margin_enabled:
            raise InsufficientBuyingPowerError(
                "cash adjustment would make cash negative", code="cash_negative"
            )

    def _check_quantity(self, quantity: Decimal) -> None:
        if _is_fractional(quantity) and not self.allow_fractional:
            raise UnsupportedOrderCapabilityError(
                "fractional quantities are not enabled for this shadow account",
                code="fractional_unsupported",
            )

    def _validate_submit(self, event: ShadowLedgerEvent) -> None:
        data = event.data
        symbol = str(data["symbol"])
        side = OrderSide(str(data["side"]))
        purpose = OrderPurpose(str(data["purpose"]))
        quantity = _positive(_d(data, "quantity"), "quantity")
        self._check_quantity(quantity)
        if str(data["client_order_id"]) in self.client_order_ids:
            raise _reject("duplicate_client_order_id", "client_order_id was already submitted")
        position = self.positions.get(symbol)
        if side is OrderSide.SELL:
            if position is None:
                raise _reject("no_position", f"cannot sell {symbol}: no position held (no shorts)")
            if quantity > position.quantity:
                raise _reject(
                    "oversell", f"sell {quantity} exceeds held {position.quantity} of {symbol}"
                )
            if quantity < position.quantity:
                raise _reject(
                    "partial_exit",
                    f"planned partial exit of {symbol} is not allowed; sell all "
                    f"{position.quantity}",
                )
            return
        if purpose is not OrderPurpose.ENTRY:
            raise _reject("buy_must_be_entry", "BUY orders must have ENTRY purpose")
        reference = _positive(_d(data, "reference_price"), "reference_price")
        strategy_key = data.get("strategy_key")
        assert self.policy is not None
        if position is not None:
            code = "averaging_down" if reference < position.avg_cost else "pyramiding"
            raise _reject(code, f"{symbol} already has an open position; additional entry refused")
        if any(o.symbol == symbol and o.side is OrderSide.BUY for o in self.open_orders()):
            raise _reject("pyramiding", f"{symbol} already has an open entry order")
        if self.policy.lockout_scope_by_strategy and strategy_key is None:
            raise _reject(
                "strategy_key_required", "entry requires a strategy_key for lockout scope"
            )
        blocked = self.entry_block(
            symbol, None if strategy_key is None else str(strategy_key), event.occurred_at
        )
        if blocked is not None:
            raise EntryBlockedError(blocked[1], code=blocked[0])
        notional = quantity * reference
        if notional > self.per_symbol_budget:
            raise EntryBlockedError(
                f"entry {notional} exceeds per-symbol budget {self.per_symbol_budget}",
                code="per_symbol_budget",
            )
        reserved = self.reserved_buy_notional()
        exposure = sum((p.cost_basis for p in self.positions.values()), start=_ZERO) + reserved
        if exposure + notional > self.total_budget:
            raise EntryBlockedError(
                f"entry {notional} exceeds total budget {self.total_budget} (committed {exposure})",
                code="total_budget",
            )
        if not self.margin_enabled and notional > self.cash - reserved:
            raise InsufficientBuyingPowerError(
                f"entry {notional} exceeds available cash {self.cash - reserved}",
                code="cash_negative",
            )

    def _open_order(self, data: Mapping[str, object]) -> _Order:
        order = self.orders.get(str(data["broker_order_id"]))
        if order is None:
            raise _reject("unknown_order", f"unknown order {data['broker_order_id']!r}")
        if not order.is_open:
            raise _reject(
                "order_not_open", f"order {order.broker_order_id} is {order.status.value}"
            )
        return order

    def _validate_replace(self, event: ShadowLedgerEvent) -> None:
        data = event.data
        order = self._open_order(data)
        if data.get("quantity") is not None:
            quantity = _positive(_d(data, "quantity"), "quantity")
            self._check_quantity(quantity)
            if order.side is OrderSide.BUY:
                if quantity > order.quantity or quantity <= order.filled_quantity:
                    raise _reject(
                        "invalid_replace_quantity",
                        "entry replacement may only reduce quantity above the filled amount",
                    )
            else:
                position = self.positions.get(order.symbol)
                held = _ZERO if position is None else position.quantity
                if quantity - order.filled_quantity != held:
                    raise _reject(
                        "partial_exit",
                        "replacement exit must still close the entire held position",
                    )
        if data.get("stop_price") is not None:
            _positive(_d(data, "stop_price"), "stop_price")
            if order.order_type not in {OrderType.STOP, OrderType.STOP_LIMIT}:
                raise _reject("invalid_replace", "stop_price requires a STOP or STOP_LIMIT order")
        if data.get("trail_price") is not None or data.get("trail_percent") is not None:
            if order.order_type is not OrderType.TRAILING_STOP:
                raise _reject("invalid_replace", "trail requires a TRAILING_STOP order")
            trail_key = "trail_price" if data.get("trail_price") else "trail_percent"
            _positive(_d(data, trail_key), "trail")

    def _validate_cancel(self, event: ShadowLedgerEvent) -> None:
        self._open_order(event.data)

    def _validate_fill(self, event: ShadowLedgerEvent) -> None:
        data = event.data
        order = self._open_order(data)
        fill_id = str(data["fill_id"])
        if fill_id in self.fills:
            raise _reject("duplicate_fill", f"fill {fill_id!r} was already applied")
        symbol = str(data["symbol"])
        side = OrderSide(str(data["side"]))
        if symbol != order.symbol or side is not order.side:
            raise _reject("fill_order_mismatch", "fill symbol/side differ from its order")
        quantity = _positive(_d(data, "quantity"), "quantity")
        price = _positive(_d(data, "price"), "price")
        fee = _decimal(_d(data, "fee"), "fee")
        _decimal(_d(data, "slippage"), "slippage")
        self._check_quantity(quantity)
        if quantity > order.remaining:
            raise _reject("overfill", f"fill {quantity} exceeds order remaining {order.remaining}")
        position = self.positions.get(symbol)
        if side is OrderSide.SELL:
            if position is None:
                raise _reject("no_position", f"cannot sell {symbol}: no position held (no shorts)")
            if quantity > position.quantity:
                raise _reject(
                    "oversell", f"sell {quantity} exceeds held {position.quantity} of {symbol}"
                )
            return
        if position is not None and position.entry_order_id != order.broker_order_id:
            raise _reject("pyramiding", f"{symbol} already has a position from another order")
        if not self.margin_enabled and self.cash - (quantity * price + fee) < _ZERO:
            raise InsufficientBuyingPowerError(
                f"fill cost {quantity * price + fee} would make cash negative", code="cash_negative"
            )

    def _validate_lockout_clear(self, event: ShadowLedgerEvent) -> None:
        _text(event.data["symbol"], "symbol")
        _text(event.data["reason"], "reason")
        _text(event.data["actor"], "actor")

    # ---- reduction

    def apply(self, event: ShadowLedgerEvent) -> None:
        """Fold an already-validated event into the state."""
        data = event.data
        if event.kind == ACCOUNT_CREATED:
            self.created_at = event.occurred_at
            self.starting_cash = _d(data, "starting_cash")
            self.cash = self.starting_cash
            self.per_symbol_budget = _d(data, "per_symbol_budget")
            self.total_budget = _d(data, "total_budget")
            self.margin_enabled = bool(data["margin_enabled"])
            self.allow_fractional = bool(data["allow_fractional"])
            self.policy = _policy_from_data(data["policy"])
            self.simulator_version = event.simulator_version
            self.assumptions = dict(event.assumptions)
            self.feed = event.feed
        elif event.kind == CASH_ADJUSTED:
            amount = _d(data, "amount")
            self.cash += amount
            self.adjustments.append(
                (
                    str(data["adjustment_id"]),
                    amount,
                    str(data["reason"]),
                    str(data["actor"]),
                    event.occurred_at,
                )
            )
        elif event.kind == ORDER_SUBMITTED:
            self._apply_submit(event)
        elif event.kind == ORDER_REPLACED:
            order = self.orders[str(data["broker_order_id"])]
            if data.get("quantity") is not None:
                order.quantity = _d(data, "quantity")
            if data.get("stop_price") is not None:
                order.stop_price = _d(data, "stop_price")
            if data.get("trail_price") is not None:
                order.trail_price = _d(data, "trail_price")
            if data.get("trail_percent") is not None:
                order.trail_percent = _d(data, "trail_percent")
            order.updated_at = event.occurred_at
        elif event.kind == ORDER_CANCELED:
            order = self.orders[str(data["broker_order_id"])]
            order.status = BrokerOrderStatus.CANCELED
            order.updated_at = event.occurred_at
        elif event.kind == FILL_RECORDED:
            self._apply_fill(event)
        elif event.kind == LOCKOUT_CLEARED:
            strategy_key = data.get("strategy_key")
            key = (str(data["symbol"]), "" if strategy_key is None else str(strategy_key))
            self.consecutive_losses[key] = 0

    def _apply_submit(self, event: ShadowLedgerEvent) -> None:
        data = event.data
        broker_order_id = str(data["broker_order_id"])
        strategy_key = data.get("strategy_key")
        self.orders[broker_order_id] = _Order(
            broker_order_id=broker_order_id,
            client_order_id=str(data["client_order_id"]),
            intent_id=str(data["intent_id"]),
            cycle_id=str(data["cycle_id"]),
            symbol=str(data["symbol"]),
            side=OrderSide(str(data["side"])),
            purpose=OrderPurpose(str(data["purpose"])),
            order_type=OrderType(str(data["order_type"])),
            time_in_force=TimeInForce(str(data["time_in_force"])),
            quantity=_d(data, "quantity"),
            submitted_at=event.occurred_at,
            updated_at=event.occurred_at,
            reference_price=_opt_d(data, "reference_price"),
            strategy_key=None if strategy_key is None else str(strategy_key),
            limit_price=_opt_d(data, "limit_price"),
            stop_price=_opt_d(data, "stop_price"),
            trail_price=_opt_d(data, "trail_price"),
            trail_percent=_opt_d(data, "trail_percent"),
        )
        self.client_order_ids[str(data["client_order_id"])] = broker_order_id

    def _apply_fill(self, event: ShadowLedgerEvent) -> None:
        data = event.data
        order = self.orders[str(data["broker_order_id"])]
        symbol = order.symbol
        quantity, price, fee = _d(data, "quantity"), _d(data, "price"), _d(data, "fee")
        filled_at = _ts(data, "filled_at")
        fill = Fill(
            fill_id=str(data["fill_id"]),
            broker_order_id=order.broker_order_id,
            account_scope_id=self.scope.scope_id,
            symbol=symbol,
            side=order.side,
            quantity=quantity,
            price=price,
            fee=fee,
            filled_at=filled_at,
        )
        self.fills[fill.fill_id] = fill
        self.fill_slippage[fill.fill_id] = _d(data, "slippage")
        order.filled_quantity += quantity
        order.fill_value += quantity * price
        order.updated_at = filled_at
        order.status = (
            BrokerOrderStatus.FILLED
            if order.filled_quantity == order.quantity
            else BrokerOrderStatus.PARTIALLY_FILLED
        )
        if order.side is OrderSide.BUY:
            self.cash -= quantity * price + fee
            position = self.positions.get(symbol)
            if position is None:
                self.positions[symbol] = ShadowPosition(
                    symbol=symbol,
                    quantity=quantity,
                    cost_basis=quantity * price,
                    entry_fees=fee,
                    strategy_key=order.strategy_key,
                    cycle_id=order.cycle_id,
                    entry_order_id=order.broker_order_id,
                    opened_at=filled_at,
                    entry_cost_total=quantity * price,
                    entry_fees_total=fee,
                )
                key = (symbol, _session_date(filled_at))
                self.entry_cycles[key] = self.entry_cycles.get(key, 0) + 1
            else:
                self.positions[symbol] = replace(
                    position,
                    quantity=position.quantity + quantity,
                    cost_basis=position.cost_basis + quantity * price,
                    entry_fees=position.entry_fees + fee,
                    entry_cost_total=position.entry_cost_total + quantity * price,
                    entry_fees_total=position.entry_fees_total + fee,
                )
            return
        self.cash += quantity * price - fee
        position = self.positions[symbol]
        exit_proceeds = position.exit_proceeds + quantity * price
        exit_fees = position.exit_fees + fee
        if quantity == position.quantity:
            # Closing fill: the cycle result comes from exact sums, never from divided parts.
            cycle_pnl = (
                exit_proceeds - position.entry_cost_total - position.entry_fees_total - exit_fees
            )
            self.realized_pnl += cycle_pnl - position.realized_pnl
        else:
            cost = position.cost_basis * quantity / position.quantity
            entry_fees = position.entry_fees * quantity / position.quantity
            pnl = quantity * price - fee - cost - entry_fees
            self.realized_pnl += pnl
            self.positions[symbol] = replace(
                position,
                quantity=position.quantity - quantity,
                cost_basis=position.cost_basis - cost,
                entry_fees=position.entry_fees - entry_fees,
                realized_pnl=position.realized_pnl + pnl,
                exit_proceeds=exit_proceeds,
                exit_fees=exit_fees,
            )
            return
        del self.positions[symbol]
        self.closed_cycles += 1
        streak_keys = [(symbol, "")]
        if position.strategy_key is not None:
            streak_keys.append((symbol, position.strategy_key))
        for streak_key in streak_keys:
            self.consecutive_losses[streak_key] = (
                self.consecutive_losses.get(streak_key, 0) + 1 if cycle_pnl < _ZERO else 0
            )
        assert self.policy is not None
        if self.policy.post_sale_cooldown > timedelta(0):
            self.cooldown_until[symbol] = filled_at + self.policy.post_sale_cooldown


# --------------------------------------------------------------------------- account


class ShadowAccount:
    """SHADOW implementation of ``BrokerPort``; never contacts any broker endpoint."""

    environment = AccountEnvironment.SHADOW
    estimated = True
    broker_confirmed = False

    def __init__(self, ledger: ShadowLedger, state: ShadowAccountState) -> None:
        self._ledger = ledger
        self._state = state
        self._quotes: dict[str, Decimal] = {}

    # ---- construction

    @classmethod
    def create(
        cls,
        ledger: ShadowLedger,
        *,
        starting_cash: Decimal,
        per_symbol_budget: Decimal,
        total_budget: Decimal,
        eligibility: EntryEligibilityPolicy,
        margin_enabled: bool = False,
        allow_fractional: bool = False,
        simulator_version: str = SIMULATOR_VERSION,
        assumptions: Mapping[str, str] | None = None,
        feed: str | None = None,
        at: datetime | None = None,
    ) -> ShadowAccount:
        """Create a new account; refuses if this scenario already has any ledger history."""
        if ledger.read_events():
            raise _reject(
                "starting_cash_already_set",
                "this scenario already exists; starting cash cannot be set again",
            )
        _decimal(starting_cash, "starting_cash")
        _decimal(per_symbol_budget, "per_symbol_budget")
        _decimal(total_budget, "total_budget")
        if not isinstance(eligibility, EntryEligibilityPolicy):
            raise TypeError("eligibility must be an EntryEligibilityPolicy")
        if not isinstance(margin_enabled, bool) or not isinstance(allow_fractional, bool):
            raise TypeError("margin_enabled and allow_fractional must be bool")
        state = ShadowAccountState(scope=ledger.scope)
        account = cls(ledger, state)
        event = ShadowLedgerEvent(
            event_id=f"{ledger.scope.scope_id}:created",
            kind=ACCOUNT_CREATED,
            scope=ledger.scope,
            occurred_at=_utc(at or ledger.now(), "at"),
            data={
                "starting_cash": starting_cash,
                "per_symbol_budget": per_symbol_budget,
                "total_budget": total_budget,
                "margin_enabled": margin_enabled,
                "allow_fractional": allow_fractional,
                "policy": _policy_to_data(eligibility),
            },
            simulator_version=simulator_version,
            assumptions=dict(assumptions or {}),
            feed=feed,
        )
        account._commit(event)
        return account

    @classmethod
    def open(cls, ledger: ShadowLedger) -> ShadowAccount:
        """Rebuild an existing account by replaying its ledger; never resets anything."""
        state = ShadowAccountState(scope=ledger.scope)
        for event in ledger.read_events():
            try:
                state.validate(event)
            except MikeTradeError as error:
                raise PersistenceError(
                    f"shadow ledger replay rejected event {event.event_id!r}: {error}",
                    code="ledger_replay_invalid",
                ) from error
            state.apply(event)
        if not state.is_created:
            raise PersistenceError(
                f"no shadow account exists for {ledger.scope.scope_id!r}", code="ledger_empty"
            )
        return cls(ledger, state)

    def set_starting_cash(self, amount: Decimal) -> None:
        """Always rejected: starting cash is set once, at creation; use ``adjust_cash``."""
        raise _reject(
            "starting_cash_already_set",
            "starting cash is set once, at creation; use an audited cash adjustment",
        )

    # ---- identity / state

    @property
    def scope(self) -> AccountScope:
        return self._state.scope

    @property
    def account_id(self) -> str:
        return self._state.scope.account_id

    @property
    def scenario_id(self) -> str:
        scenario = self._state.scope.scenario
        assert scenario is not None
        return scenario

    @property
    def mode(self) -> AccountEnvironment:
        return AccountEnvironment.SHADOW

    @property
    def state(self) -> ShadowAccountState:
        """A deep copy; mutating it never affects the account."""
        return copy.deepcopy(self._state)

    def _commit(self, event: ShadowLedgerEvent) -> bool:
        """Validate, append and apply ``event``; False if it was an identical duplicate."""
        if self._ledger.contains(event.event_id):
            self._ledger.append(event)  # identical content is a no-op; different raises
            return False
        self._state.validate(event)
        self._ledger.append(event)
        self._state.apply(event)
        return True

    def _event(
        self,
        kind: str,
        suffix: str,
        at: datetime,
        data: Mapping[str, object],
        *,
        feed: str | None = None,
    ) -> ShadowLedgerEvent:
        state = self._state
        return ShadowLedgerEvent(
            event_id=f"{state.scope.scope_id}:{suffix}",
            kind=kind,
            scope=state.scope,
            occurred_at=_utc(at, "at"),
            data=data,
            simulator_version=state.simulator_version,
            assumptions=state.assumptions,
            feed=feed if feed is not None else state.feed,
        )

    def _now(self, at: datetime | None) -> datetime:
        return _utc(at, "at") if at is not None else self._ledger.now()

    # ---- cash, lockouts, quotes

    def adjust_cash(
        self,
        *,
        adjustment_id: str,
        amount: Decimal,
        reason: str,
        actor: str,
        at: datetime | None = None,
    ) -> bool:
        """Apply an audited cash change; returns False for an identical retry."""
        adjustment_id = _text(adjustment_id, "adjustment_id")
        if not isinstance(amount, Decimal):
            raise ValueError(f"amount must be Decimal; got {type(amount).__name__}")
        event = self._event(
            CASH_ADJUSTED,
            f"cash:{adjustment_id}",
            self._now(at),
            {
                "adjustment_id": adjustment_id,
                "amount": amount,
                "reason": _text(reason, "reason"),
                "actor": _text(actor, "actor"),
            },
        )
        return self._commit(event)

    def clear_lockout(
        self,
        symbol: str,
        *,
        reason: str,
        actor: str,
        strategy_key: str | None = None,
        at: datetime | None = None,
    ) -> None:
        """Operator reset of a consecutive-loss streak (audited)."""
        occurred_at = self._now(at)
        event = ShadowLedgerEvent(
            event_id=f"{self.scope.scope_id}:lockout-clear:{uuid4().hex}",
            kind=LOCKOUT_CLEARED,
            scope=self.scope,
            occurred_at=occurred_at,
            data={
                "symbol": _text(symbol, "symbol").upper(),
                "strategy_key": (
                    None if strategy_key is None else _text(strategy_key, "strategy_key")
                ),
                "reason": _text(reason, "reason"),
                "actor": _text(actor, "actor"),
            },
            simulator_version=self._state.simulator_version,
            assumptions=self._state.assumptions,
            feed=self._state.feed,
        )
        self._commit(event)

    def update_quote(self, symbol: str, price: Decimal) -> None:
        """Supply the quote used to mark ``symbol``; marks are inputs, not ledger facts."""
        self._quotes[_text(symbol, "symbol").upper()] = _positive(price, "price")

    def _marks(self, quotes: Mapping[str, Decimal] | None) -> dict[str, Decimal]:
        marks = dict(self._quotes)
        for symbol, price in (quotes or {}).items():
            marks[symbol.strip().upper()] = _positive(price, "quote")
        return marks

    def unrealized_pnl(self, quotes: Mapping[str, Decimal] | None = None) -> Decimal:
        """Estimated P&L of open positions, net of their entry fees; unmarked = cost basis."""
        marks = self._marks(quotes)
        total = _ZERO
        for symbol, position in self._state.positions.items():
            mark = marks.get(symbol)
            value = position.cost_basis if mark is None else position.quantity * mark
            total += value - position.cost_basis - position.entry_fees
        return total

    def equity(self, quotes: Mapping[str, Decimal] | None = None) -> Decimal:
        """Estimated equity: cash plus open positions marked to the supplied quotes."""
        marks = self._marks(quotes)
        total = self._state.cash
        for symbol, position in self._state.positions.items():
            mark = marks.get(symbol)
            total += position.cost_basis if mark is None else position.quantity * mark
        return total

    # ---- BrokerPort

    def capabilities(self) -> BrokerCapabilities:
        fractional = self._state.allow_fractional
        order_types = frozenset(item.value.lower() for item in OrderType)
        return BrokerCapabilities(
            provider="shadow",
            environment=AccountEnvironment.SHADOW,
            supported_order_types=order_types,
            supports_fractional=fractional,
            supports_notional=False,
            supports_native_trailing_stop=False,
            supports_extended_hours=False,
            supports_short=False,
            paper_verified_combinations=(
                frozenset((name, True) for name in order_types) if fractional else frozenset()
            ),
        )

    async def get_account(self) -> AccountSnapshot:
        state = self._state
        reserved = state.reserved_buy_notional()
        unmarked = sorted(set(state.positions) - set(self._quotes))
        source = _SOURCE + (f";valued_at_cost={','.join(unmarked)}" if unmarked else "")
        equity = self.equity()
        return AccountSnapshot(
            scope=state.scope,
            as_of=self._ledger.now(),
            cash=state.cash,
            equity=equity,
            buying_power=state.cash - reserved,
            portfolio_value=equity,
            shorting_enabled=False,
            source=source,
        )

    async def list_positions(self) -> Sequence[Position]:
        now = self._ledger.now()
        positions = []
        for symbol in sorted(self._state.positions):
            item = self._state.positions[symbol]
            mark = self._quotes.get(symbol)
            positions.append(
                Position(
                    account_scope_id=self.scope.scope_id,
                    symbol=symbol,
                    quantity=item.quantity,
                    avg_entry_price=item.avg_cost,
                    cost_basis=item.cost_basis,
                    as_of=now,
                    market_value=None if mark is None else item.quantity * mark,
                    management=ManagementStatus.MANAGED,
                    cycle_id=item.cycle_id,
                )
            )
        return positions

    async def list_open_orders(self) -> Sequence[BrokerOrder]:
        return [o.to_broker_order(self.scope.scope_id) for o in self._state.open_orders()]

    async def get_order(self, broker_order_id: str) -> BrokerOrder | None:
        order = self._state.orders.get(broker_order_id)
        return None if order is None else order.to_broker_order(self.scope.scope_id)

    async def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        broker_order_id = self._state.client_order_ids.get(client_order_id)
        return None if broker_order_id is None else await self.get_order(broker_order_id)

    async def submit_order(
        self, intent: OrderIntent, *, strategy_key: str | None = None
    ) -> BrokerOrder:
        """Record a simulated order. Idempotent by ``client_order_id``; no broker is called."""
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be an OrderIntent")
        if intent.account_scope_id != self.scope.scope_id:
            raise ModeIsolationError(
                f"intent scope {intent.account_scope_id!r} differs from account scope "
                f"{self.scope.scope_id!r}",
                code="intent_scope_mismatch",
            )
        existing = self._state.client_order_ids.get(intent.client_order_id)
        if existing is not None:
            return self._state.orders[existing].to_broker_order(self.scope.scope_id)
        if intent.quantity is None:
            raise UnsupportedOrderCapabilityError(
                "shadow accounts do not support notional orders", code="notional_unsupported"
            )
        reference: Decimal | None = None
        if intent.side is OrderSide.BUY:
            reference = intent.limit_price or intent.stop_price or self._quotes.get(intent.symbol)
            if reference is None:
                raise _reject(
                    "reference_price_required",
                    f"no limit, stop or supplied quote to size entry for {intent.symbol}",
                )
        broker_order_id = f"shadow-{intent.client_order_id}"
        event = self._event(
            ORDER_SUBMITTED,
            f"order:{intent.client_order_id}",
            intent.created_at,
            {
                "broker_order_id": broker_order_id,
                "client_order_id": intent.client_order_id,
                "intent_id": intent.intent_id,
                "cycle_id": intent.cycle_id,
                "symbol": intent.symbol,
                "side": intent.side,
                "purpose": intent.purpose,
                "order_type": intent.order_type,
                "time_in_force": intent.time_in_force,
                "quantity": intent.quantity,
                "limit_price": intent.limit_price,
                "stop_price": intent.stop_price,
                "trail_price": intent.trail_price,
                "trail_percent": intent.trail_percent,
                "reference_price": reference,
                "strategy_key": (
                    None if strategy_key is None else _text(strategy_key, "strategy_key")
                ),
                "signal_id": intent.signal_id,
            },
        )
        self._commit(event)
        return self._state.orders[broker_order_id].to_broker_order(self.scope.scope_id)

    async def replace_order(
        self,
        broker_order_id: str,
        *,
        stop_price: Decimal | None = None,
        trail: Decimal | None = None,
        quantity: Decimal | None = None,
        at: datetime | None = None,
    ) -> BrokerOrder:
        order = self._state.orders.get(broker_order_id)
        if order is None:
            raise _reject("unknown_order", f"unknown order {broker_order_id!r}")
        if stop_price is None and trail is None and quantity is None:
            raise ValueError("replace_order requires stop_price, trail or quantity")
        data: dict[str, object] = {"broker_order_id": broker_order_id}
        if stop_price is not None:
            data["stop_price"] = _positive(stop_price, "stop_price")
        if trail is not None:
            field_name = "trail_percent" if order.trail_percent is not None else "trail_price"
            data[field_name] = _positive(trail, "trail")
        if quantity is not None:
            data["quantity"] = _positive(quantity, "quantity")
        event = ShadowLedgerEvent(
            event_id=f"{self.scope.scope_id}:replace:{uuid4().hex}",
            kind=ORDER_REPLACED,
            scope=self.scope,
            occurred_at=self._now(at),
            data=data,
            simulator_version=self._state.simulator_version,
            assumptions=self._state.assumptions,
            feed=self._state.feed,
        )
        self._commit(event)
        return order.to_broker_order(self.scope.scope_id)

    async def cancel_order(self, broker_order_id: str, *, at: datetime | None = None) -> None:
        order = self._state.orders.get(broker_order_id)
        if order is None:
            raise _reject("unknown_order", f"unknown order {broker_order_id!r}")
        if not order.is_open:
            return
        self._commit(
            self._event(
                ORDER_CANCELED,
                f"cancel:{broker_order_id}",
                self._now(at),
                {"broker_order_id": broker_order_id},
            )
        )

    async def list_fills(self, since: datetime) -> Sequence[Fill]:
        _utc(since, "since")
        return sorted(
            (fill for fill in self._state.fills.values() if fill.filled_at >= since),
            key=lambda fill: (fill.filled_at, fill.fill_id),
        )

    # ---- simulated executions

    def apply_fill(
        self, fill: Fill, *, slippage: Decimal = _ZERO, feed: str | None = None
    ) -> BrokerOrder:
        """Record a simulated execution of an open order; idempotent by ``fill_id``.

        ``fill.price`` and ``fill.fee`` are the simulator's estimates and
        ``slippage`` (per share, already reflected in the price) is recorded
        alongside; none of it is ever broker-confirmed.
        """
        if not isinstance(fill, Fill):
            raise TypeError("fill must be a Fill")
        if fill.account_scope_id != self.scope.scope_id:
            raise ModeIsolationError(
                f"fill scope {fill.account_scope_id!r} differs from account scope "
                f"{self.scope.scope_id!r}",
                code="fill_scope_mismatch",
            )
        _decimal(slippage, "slippage")
        known = self._state.fills.get(fill.fill_id)
        order = self._state.orders.get(fill.broker_order_id)
        if known is not None:
            if known != fill or order is None:
                raise _reject(
                    "duplicate_fill", f"fill {fill.fill_id!r} conflicts with a prior fill"
                )
            return order.to_broker_order(self.scope.scope_id)
        if order is None:
            raise _reject("unknown_order", f"unknown order {fill.broker_order_id!r}")
        event = self._event(
            FILL_RECORDED,
            f"fill:{fill.fill_id}",
            fill.filled_at,
            {
                "fill_id": fill.fill_id,
                "broker_order_id": fill.broker_order_id,
                "symbol": fill.symbol,
                "side": fill.side,
                "quantity": fill.quantity,
                "price": fill.price,
                "fee": fill.fee,
                "slippage": slippage,
                "filled_at": fill.filled_at,
            },
            feed=feed,
        )
        self._commit(event)
        return order.to_broker_order(self.scope.scope_id)
