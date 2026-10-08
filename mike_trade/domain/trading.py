"""Immutable trading lifecycle, fill, position, and budget contracts.

The module deliberately depends only on the Python standard library.

Design choices (3 of the task's 4-decision budget):
1. Symbols are stripped and uppercased at construction, while opaque IDs are
   stripped but otherwise preserved.
2. A cycle's realized P&L is the literal exit proceeds minus total entry cost
   minus every recorded entry and exit fee; planned partial exits are outside
   this contract.
3. ``entry_blocked`` returns stored reasons and adds the stable reason
   ``"locked_until"`` only while the state's UTC lock deadline is in the future.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum

__all__ = [
    "OrderSide",
    "PositionDirection",
    "OrderType",
    "TimeInForce",
    "OrderPurpose",
    "BrokerOrderStatus",
    "TERMINAL_ORDER_STATUSES",
    "ManagementStatus",
    "PositionCycleState",
    "OrderIntent",
    "BrokerOrder",
    "Fill",
    "PositionCycle",
    "Position",
    "BudgetPolicy",
    "EntryEligibilityPolicy",
    "EntryEligibilityState",
]

_ZERO = Decimal("0")


def _require_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty str")
    return value.strip()


def _require_symbol(value: object) -> str:
    symbol = _require_text(value, field="symbol").upper()
    if any(character.isspace() for character in symbol):
        raise ValueError("symbol must not contain whitespace")
    return symbol


def _require_decimal(
    value: object,
    *,
    field: str,
    minimum: Decimal = _ZERO,
    inclusive: bool = True,
) -> Decimal:
    if not isinstance(value, Decimal):
        raise ValueError(f"{field} must be Decimal; got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{field} must be finite")
    if value < minimum or (not inclusive and value == minimum):
        operator = ">=" if inclusive else ">"
        raise ValueError(f"{field} must be {operator} {minimum}")
    return value


def _optional_decimal(
    value: object,
    *,
    field: str,
    minimum: Decimal = _ZERO,
    inclusive: bool = True,
) -> Decimal | None:
    if value is None:
        return None
    return _require_decimal(value, field=field, minimum=minimum, inclusive=inclusive)


def _require_utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC")
    return value.astimezone(UTC)


def _optional_utc(value: object, *, field: str) -> datetime | None:
    if value is None:
        return None
    return _require_utc(value, field=field)


def _require_enum(value: object, enum_type: type[Enum], *, field: str) -> None:
    if not isinstance(value, enum_type):
        raise ValueError(f"{field} must be {enum_type.__name__}")


def _require_int(value: object, *, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be int")
    if value < minimum:
        raise ValueError(f"{field} must be >= {minimum}")
    return value


def _require_duration(value: object, *, field: str) -> timedelta:
    if not isinstance(value, timedelta):
        raise ValueError(f"{field} must be timedelta")
    if value < timedelta(0):
        raise ValueError(f"{field} must be >= 0")
    return value


def _validate_order_prices(
    order_type: OrderType,
    limit_price: Decimal | None,
    stop_price: Decimal | None,
    trail_price: Decimal | None,
    trail_percent: Decimal | None,
) -> None:
    if order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT}:
        if limit_price is None:
            raise ValueError(f"{order_type.value} orders require limit_price")
    elif limit_price is not None:
        raise ValueError(f"{order_type.value} orders must not specify limit_price")

    if order_type in {OrderType.STOP, OrderType.STOP_LIMIT}:
        if stop_price is None:
            raise ValueError(f"{order_type.value} orders require stop_price")
    elif stop_price is not None:
        raise ValueError(f"{order_type.value} orders must not specify stop_price")

    trail_count = int(trail_price is not None) + int(trail_percent is not None)
    if order_type is OrderType.TRAILING_STOP:
        if trail_count != 1:
            raise ValueError("TRAILING_STOP orders require exactly one trail value")
    elif trail_count:
        raise ValueError(f"{order_type.value} orders must not specify a trail value")


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionDirection(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    TRAILING_STOP = "TRAILING_STOP"


class TimeInForce(Enum):
    DAY = "DAY"
    GTC = "GTC"


class OrderPurpose(Enum):
    ENTRY = "ENTRY"
    STRATEGY_EXIT = "STRATEGY_EXIT"
    PROTECTIVE_STOP = "PROTECTIVE_STOP"
    MANUAL_CLOSE = "MANUAL_CLOSE"
    SAFETY_EXIT = "SAFETY_EXIT"
    EOD_CLOSE = "EOD_CLOSE"


class BrokerOrderStatus(Enum):
    NEW = "NEW"
    PENDING_NEW = "PENDING_NEW"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    REPLACED = "REPLACED"
    UNKNOWN = "UNKNOWN"


TERMINAL_ORDER_STATUSES = frozenset(
    {
        BrokerOrderStatus.FILLED,
        BrokerOrderStatus.CANCELED,
        BrokerOrderStatus.EXPIRED,
        BrokerOrderStatus.REJECTED,
        BrokerOrderStatus.REPLACED,
    }
)


class ManagementStatus(Enum):
    MANAGED = "MANAGED"
    UNMANAGED = "UNMANAGED"
    ADOPTED = "ADOPTED"


class PositionCycleState(Enum):
    PENDING_ENTRY = "PENDING_ENTRY"
    OPEN = "OPEN"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    ABANDONED = "ABANDONED"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """An idempotent, broker-neutral instruction to submit one order."""

    intent_id: str
    client_order_id: str
    account_scope_id: str
    cycle_id: str
    symbol: str
    side: OrderSide
    purpose: OrderPurpose
    order_type: OrderType
    time_in_force: TimeInForce
    created_at: datetime
    quantity: Decimal | None = None
    notional: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trail_price: Decimal | None = None
    trail_percent: Decimal | None = None
    signal_id: str | None = None

    def __post_init__(self) -> None:
        for field in ("intent_id", "client_order_id", "account_scope_id", "cycle_id"):
            object.__setattr__(self, field, _require_text(getattr(self, field), field=field))
        object.__setattr__(self, "symbol", _require_symbol(self.symbol))
        _require_enum(self.side, OrderSide, field="side")
        _require_enum(self.purpose, OrderPurpose, field="purpose")
        _require_enum(self.order_type, OrderType, field="order_type")
        _require_enum(self.time_in_force, TimeInForce, field="time_in_force")
        object.__setattr__(self, "created_at", _require_utc(self.created_at, field="created_at"))

        quantity = _optional_decimal(
            self.quantity, field="quantity", minimum=_ZERO, inclusive=False
        )
        notional = _optional_decimal(
            self.notional, field="notional", minimum=_ZERO, inclusive=False
        )
        if (quantity is None) == (notional is None):
            raise ValueError("exactly one of quantity or notional is required")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "notional", notional)

        for field in ("limit_price", "stop_price", "trail_price", "trail_percent"):
            object.__setattr__(
                self,
                field,
                _optional_decimal(
                    getattr(self, field), field=field, minimum=_ZERO, inclusive=False
                ),
            )
        _validate_order_prices(
            self.order_type,
            self.limit_price,
            self.stop_price,
            self.trail_price,
            self.trail_percent,
        )
        if self.signal_id is not None:
            object.__setattr__(
                self, "signal_id", _require_text(self.signal_id, field="signal_id")
            )
        if self.side is OrderSide.SELL and self.purpose is OrderPurpose.ENTRY:
            raise ValueError("short entry intents are not supported")


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    """A normalized broker order snapshot."""

    broker_order_id: str
    client_order_id: str
    account_scope_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    status: BrokerOrderStatus
    quantity: Decimal
    filled_quantity: Decimal
    submitted_at: datetime
    updated_at: datetime
    filled_avg_price: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trail_price: Decimal | None = None
    trail_percent: Decimal | None = None
    replaced_by: str | None = None

    def __post_init__(self) -> None:
        for field in ("broker_order_id", "client_order_id", "account_scope_id"):
            object.__setattr__(self, field, _require_text(getattr(self, field), field=field))
        object.__setattr__(self, "symbol", _require_symbol(self.symbol))
        _require_enum(self.side, OrderSide, field="side")
        _require_enum(self.order_type, OrderType, field="order_type")
        _require_enum(self.status, BrokerOrderStatus, field="status")
        quantity = _require_decimal(
            self.quantity, field="quantity", minimum=_ZERO, inclusive=False
        )
        filled = _require_decimal(self.filled_quantity, field="filled_quantity")
        if filled > quantity:
            raise ValueError("filled_quantity must not exceed quantity")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "filled_quantity", filled)
        average = _optional_decimal(
            self.filled_avg_price,
            field="filled_avg_price",
            minimum=_ZERO,
            inclusive=False,
        )
        if filled > _ZERO and average is None:
            raise ValueError("filled_avg_price is required when filled_quantity is positive")
        object.__setattr__(self, "filled_avg_price", average)
        for field in ("limit_price", "stop_price", "trail_price", "trail_percent"):
            object.__setattr__(
                self,
                field,
                _optional_decimal(
                    getattr(self, field), field=field, minimum=_ZERO, inclusive=False
                ),
            )
        _validate_order_prices(
            self.order_type,
            self.limit_price,
            self.stop_price,
            self.trail_price,
            self.trail_percent,
        )
        submitted = _require_utc(self.submitted_at, field="submitted_at")
        updated = _require_utc(self.updated_at, field="updated_at")
        if updated < submitted:
            raise ValueError("updated_at must not be before submitted_at")
        object.__setattr__(self, "submitted_at", submitted)
        object.__setattr__(self, "updated_at", updated)
        if self.replaced_by is not None:
            object.__setattr__(
                self, "replaced_by", _require_text(self.replaced_by, field="replaced_by")
            )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_ORDER_STATUSES


@dataclass(frozen=True, slots=True)
class Fill:
    """One broker execution, keyed idempotently by ``fill_id``."""

    fill_id: str
    broker_order_id: str
    account_scope_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    fee: Decimal
    filled_at: datetime

    def __post_init__(self) -> None:
        for field in ("fill_id", "broker_order_id", "account_scope_id"):
            object.__setattr__(self, field, _require_text(getattr(self, field), field=field))
        object.__setattr__(self, "symbol", _require_symbol(self.symbol))
        _require_enum(self.side, OrderSide, field="side")
        object.__setattr__(
            self,
            "quantity",
            _require_decimal(self.quantity, field="quantity", minimum=_ZERO, inclusive=False),
        )
        object.__setattr__(
            self,
            "price",
            _require_decimal(self.price, field="price", minimum=_ZERO, inclusive=False),
        )
        object.__setattr__(self, "fee", _require_decimal(self.fee, field="fee"))
        object.__setattr__(self, "filled_at", _require_utc(self.filled_at, field="filled_at"))


@dataclass(frozen=True, slots=True)
class PositionCycle:
    """A single long entry followed by a single complete discretionary exit."""

    cycle_id: str
    account_scope_id: str
    symbol: str
    direction: PositionDirection = PositionDirection.LONG
    state: PositionCycleState = PositionCycleState.PENDING_ENTRY
    entry_strategy_key: str | None = None
    entry_fills: tuple[Fill, ...] = ()
    exit_fills: tuple[Fill, ...] = ()
    opened_at: datetime | None = None
    closed_at: datetime | None = None

    def __post_init__(self) -> None:
        for field in ("cycle_id", "account_scope_id"):
            object.__setattr__(self, field, _require_text(getattr(self, field), field=field))
        object.__setattr__(self, "symbol", _require_symbol(self.symbol))
        _require_enum(self.direction, PositionDirection, field="direction")
        if self.direction is not PositionDirection.LONG:
            raise ValueError("SHORT position cycles are dormant")
        _require_enum(self.state, PositionCycleState, field="state")
        if self.entry_strategy_key is not None:
            object.__setattr__(
                self,
                "entry_strategy_key",
                _require_text(self.entry_strategy_key, field="entry_strategy_key"),
            )
        if not isinstance(self.entry_fills, tuple) or not isinstance(self.exit_fills, tuple):
            raise ValueError("entry_fills and exit_fills must be tuples")

        all_fills = self.entry_fills + self.exit_fills
        if any(not isinstance(fill, Fill) for fill in all_fills):
            raise ValueError("cycle fills must be Fill instances")
        fill_ids = [fill.fill_id for fill in all_fills]
        if len(fill_ids) != len(set(fill_ids)):
            raise ValueError("fill_id values must be unique within a cycle")
        for fill in all_fills:
            if fill.account_scope_id != self.account_scope_id or fill.symbol != self.symbol:
                raise ValueError("fill account_scope_id and symbol must match the cycle")
        if any(fill.side is not OrderSide.BUY for fill in self.entry_fills):
            raise ValueError("entry fills must have BUY side")
        if any(fill.side is not OrderSide.SELL for fill in self.exit_fills):
            raise ValueError("exit fills must have SELL side")
        if self.exit_quantity > self.entry_quantity:
            raise ValueError("cumulative exit quantity must not exceed entry quantity")

        opened_at = _optional_utc(self.opened_at, field="opened_at")
        closed_at = _optional_utc(self.closed_at, field="closed_at")
        object.__setattr__(self, "opened_at", opened_at)
        object.__setattr__(self, "closed_at", closed_at)
        if opened_at is not None and closed_at is not None and closed_at < opened_at:
            raise ValueError("closed_at must not be before opened_at")

        if self.state is PositionCycleState.PENDING_ENTRY:
            if all_fills or opened_at is not None or closed_at is not None:
                raise ValueError("PENDING_ENTRY cycles cannot contain fills or lifecycle times")
        elif self.state is PositionCycleState.OPEN:
            if (
                not self.entry_fills
                or self.exit_fills
                or opened_at is None
                or closed_at is not None
            ):
                raise ValueError("OPEN cycles require entry fills only and opened_at")
        elif self.state is PositionCycleState.EXITING:
            if not self.entry_fills or self.remaining_quantity <= _ZERO:
                raise ValueError("EXITING cycles require a positive remaining quantity")
            if opened_at is None or closed_at is not None:
                raise ValueError("EXITING cycles require opened_at and no closed_at")
        elif self.state is PositionCycleState.CLOSED:
            if not self.entry_fills or self.remaining_quantity != _ZERO:
                raise ValueError("CLOSED cycles require a fully exited entry")
            if opened_at is None or closed_at is None:
                raise ValueError("CLOSED cycles require opened_at and closed_at")
        elif closed_at is not None and opened_at is None:
            raise ValueError("an ABANDONED cycle cannot close before it opens")

    def apply_entry_fill(self, fill: Fill) -> PositionCycle:
        if not isinstance(fill, Fill):
            raise ValueError("fill must be Fill")
        if self.state not in {PositionCycleState.PENDING_ENTRY, PositionCycleState.OPEN}:
            raise ValueError("entry fills are allowed only before exit begins")
        if self.exit_fills:
            raise ValueError("entry fills are not allowed after an exit fill")
        if fill.side is not OrderSide.BUY:
            raise ValueError("entry fill side must be BUY")
        if fill.fill_id in {item.fill_id for item in self.entry_fills}:
            raise ValueError("fill_id already exists in cycle")
        opened_at = self.opened_at if self.opened_at is not None else fill.filled_at
        return replace(
            self,
            state=PositionCycleState.OPEN,
            entry_fills=self.entry_fills + (fill,),
            opened_at=opened_at,
        )

    def begin_exit(self) -> PositionCycle:
        if self.state is not PositionCycleState.OPEN or self.remaining_quantity <= _ZERO:
            raise ValueError("exit can begin only for an OPEN cycle with remaining quantity")
        return replace(self, state=PositionCycleState.EXITING)

    def apply_exit_fill(self, fill: Fill) -> PositionCycle:
        if not isinstance(fill, Fill):
            raise ValueError("fill must be Fill")
        if self.state is not PositionCycleState.EXITING:
            raise ValueError("exit fills require an EXITING cycle")
        if fill.side is not OrderSide.SELL:
            raise ValueError("exit fill side must be SELL")
        if fill.fill_id in {item.fill_id for item in self.entry_fills + self.exit_fills}:
            raise ValueError("fill_id already exists in cycle")
        if fill.quantity > self.remaining_quantity:
            raise ValueError("cumulative exit quantity must not exceed entry quantity")
        fills = self.exit_fills + (fill,)
        if fill.quantity == self.remaining_quantity:
            return replace(
                self,
                state=PositionCycleState.CLOSED,
                exit_fills=fills,
                closed_at=fill.filled_at,
            )
        return replace(self, exit_fills=fills)

    @property
    def entry_quantity(self) -> Decimal:
        return sum((fill.quantity for fill in self.entry_fills), start=_ZERO)

    @property
    def exit_quantity(self) -> Decimal:
        return sum((fill.quantity for fill in self.exit_fills), start=_ZERO)

    @property
    def remaining_quantity(self) -> Decimal:
        return self.entry_quantity - self.exit_quantity

    @property
    def avg_entry_price(self) -> Decimal | None:
        if self.entry_quantity == _ZERO:
            return None
        cost = sum((fill.quantity * fill.price for fill in self.entry_fills), start=_ZERO)
        return cost / self.entry_quantity

    @property
    def avg_exit_price(self) -> Decimal | None:
        if self.exit_quantity == _ZERO:
            return None
        proceeds = sum((fill.quantity * fill.price for fill in self.exit_fills), start=_ZERO)
        return proceeds / self.exit_quantity

    @property
    def realized_pnl(self) -> Decimal:
        proceeds = sum((fill.quantity * fill.price for fill in self.exit_fills), start=_ZERO)
        entry_cost = sum(
            (fill.quantity * fill.price for fill in self.entry_fills), start=_ZERO
        )
        fees = sum((fill.fee for fill in self.entry_fills + self.exit_fills), start=_ZERO)
        return proceeds - entry_cost - fees


@dataclass(frozen=True, slots=True)
class Position:
    """A positive long position observed from a broker or maintained ledger."""

    account_scope_id: str
    symbol: str
    quantity: Decimal
    avg_entry_price: Decimal
    cost_basis: Decimal
    as_of: datetime
    market_value: Decimal | None = None
    management: ManagementStatus = ManagementStatus.UNMANAGED
    cycle_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "account_scope_id",
            _require_text(self.account_scope_id, field="account_scope_id"),
        )
        object.__setattr__(self, "symbol", _require_symbol(self.symbol))
        object.__setattr__(
            self,
            "quantity",
            _require_decimal(self.quantity, field="quantity", minimum=_ZERO, inclusive=False),
        )
        object.__setattr__(
            self,
            "avg_entry_price",
            _require_decimal(
                self.avg_entry_price,
                field="avg_entry_price",
                minimum=_ZERO,
                inclusive=False,
            ),
        )
        object.__setattr__(
            self, "cost_basis", _require_decimal(self.cost_basis, field="cost_basis")
        )
        object.__setattr__(
            self,
            "market_value",
            _optional_decimal(self.market_value, field="market_value"),
        )
        _require_enum(self.management, ManagementStatus, field="management")
        if self.cycle_id is not None:
            object.__setattr__(
                self, "cycle_id", _require_text(self.cycle_id, field="cycle_id")
            )
        if self.management is ManagementStatus.MANAGED and self.cycle_id is None:
            raise ValueError("MANAGED positions require cycle_id")
        object.__setattr__(self, "as_of", _require_utc(self.as_of, field="as_of"))


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    per_symbol_budget: Decimal
    total_application_budget: Decimal
    max_open_positions: int
    planned_loss_per_trade: Decimal | None = None
    daily_loss_limit: Decimal | None = None
    require_broker_buying_power: bool = True

    def __post_init__(self) -> None:
        per_symbol = _require_decimal(self.per_symbol_budget, field="per_symbol_budget")
        total = _require_decimal(
            self.total_application_budget, field="total_application_budget"
        )
        if total < per_symbol:
            raise ValueError("total_application_budget must be >= per_symbol_budget")
        object.__setattr__(self, "per_symbol_budget", per_symbol)
        object.__setattr__(self, "total_application_budget", total)
        _require_int(self.max_open_positions, field="max_open_positions", minimum=1)
        for field in ("planned_loss_per_trade", "daily_loss_limit"):
            object.__setattr__(
                self, field, _optional_decimal(getattr(self, field), field=field)
            )
        if not isinstance(self.require_broker_buying_power, bool):
            raise ValueError("require_broker_buying_power must be bool")


@dataclass(frozen=True, slots=True)
class EntryEligibilityPolicy:
    max_filled_entry_cycles_per_symbol_per_session: int
    consecutive_loss_lockout_threshold: int | None = None
    lockout_scope_by_strategy: bool = False
    post_sale_cooldown: timedelta = timedelta(0)
    min_hold_for_strategy_exit: timedelta = timedelta(0)
    entry_kill_switch: bool = False

    def __post_init__(self) -> None:
        _require_int(
            self.max_filled_entry_cycles_per_symbol_per_session,
            field="max_filled_entry_cycles_per_symbol_per_session",
            minimum=1,
        )
        if self.consecutive_loss_lockout_threshold is not None:
            _require_int(
                self.consecutive_loss_lockout_threshold,
                field="consecutive_loss_lockout_threshold",
                minimum=1,
            )
        if not isinstance(self.lockout_scope_by_strategy, bool):
            raise ValueError("lockout_scope_by_strategy must be bool")
        if not isinstance(self.entry_kill_switch, bool):
            raise ValueError("entry_kill_switch must be bool")
        _require_duration(self.post_sale_cooldown, field="post_sale_cooldown")
        _require_duration(
            self.min_hold_for_strategy_exit, field="min_hold_for_strategy_exit"
        )

    def exit_allowed(self, purpose: OrderPurpose, held_for: timedelta) -> bool:
        """Return exit eligibility; entry-only restrictions are intentionally ignored."""

        _require_enum(purpose, OrderPurpose, field="purpose")
        duration = _require_duration(held_for, field="held_for")
        if purpose in {
            OrderPurpose.PROTECTIVE_STOP,
            OrderPurpose.MANUAL_CLOSE,
            OrderPurpose.SAFETY_EXIT,
            OrderPurpose.EOD_CLOSE,
        }:
            return True
        if purpose is OrderPurpose.STRATEGY_EXIT:
            return duration >= self.min_hold_for_strategy_exit
        return False


@dataclass(frozen=True, slots=True)
class EntryEligibilityState:
    account_scope_id: str
    symbol: str
    session_date: date
    filled_entry_cycles: int
    consecutive_losses: int
    last_exit_at: datetime | None = None
    locked_until: datetime | None = None
    block_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "account_scope_id",
            _require_text(self.account_scope_id, field="account_scope_id"),
        )
        object.__setattr__(self, "symbol", _require_symbol(self.symbol))
        if type(self.session_date) is not date:
            raise ValueError("session_date must be date")
        _require_int(self.filled_entry_cycles, field="filled_entry_cycles", minimum=0)
        _require_int(self.consecutive_losses, field="consecutive_losses", minimum=0)
        object.__setattr__(
            self, "last_exit_at", _optional_utc(self.last_exit_at, field="last_exit_at")
        )
        object.__setattr__(
            self, "locked_until", _optional_utc(self.locked_until, field="locked_until")
        )
        if not isinstance(self.block_reasons, tuple):
            raise ValueError("block_reasons must be a tuple")
        reasons = tuple(
            _require_text(reason, field="block_reasons item") for reason in self.block_reasons
        )
        object.__setattr__(self, "block_reasons", reasons)

    def entry_blocked(self, now: datetime) -> tuple[str, ...]:
        current = _require_utc(now, field="now")
        reasons = list(self.block_reasons)
        if self.locked_until is not None and current < self.locked_until:
            if "locked_until" not in reasons:
                reasons.append("locked_until")
        return tuple(reasons)
