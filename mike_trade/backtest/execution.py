"""Historical (replay-only) execution simulator with explicit costs and fill policies.

``HistoricalExecutionSimulator`` is a ``mike_trade.brokers.ports.BrokerPort``-shaped
adapter (``environment`` is ``BACKTEST``).  It accepts the same
``domain.trading.OrderIntent`` values the coordinator produces in shadow/live modes
and turns them into *estimated* fills from the data served by a
``HistoricalReplayFeed`` and a ``ReplayClock``.  It never imports or calls a broker
client, endpoint or credential, never reads the wall clock and uses no randomness:
equal events, assumptions and seed always give an identical fill sequence.

Design choices (10):
1. Reuse over re-implementation.  Order lifecycle goes through
   ``execution.order_fsm`` (``apply_transition``); stop state is a
   ``domain.protection.TrailingStopState`` (its ``ratchet`` is the never-move-down
   rule); broker-native trailing stops use ``domain.protection.start_protection``
   with the tick floor of ``risk.fixed_stop``; capability checks use the
   broker-neutral ``BrokerCapabilities.supports_order`` (fractional is refused unless
   explicitly configured, and only for configured order types, DAY only), exactly as
   the SHADOW account does.  With quote evidence, MARKET/LIMIT matching is delegated
   to ``brokers.shadow.fills.ShadowFillSimulator.simulate`` (session, DAY expiry,
   limit crossing, staleness, size and adverse slippage); this module then applies
   its own fees, shared-liquidity cap and held-quantity cap.
2. Evidence.  Quote fidelity comes from the feed.  Not UNAVAILABLE: matching is
   driven by quotes (buys at the ask, sells at the bid, plus adverse slippage; a stop
   triggers on ``bid <= stop`` and fills at the bid, so a gap fills where the market
   is).  UNAVAILABLE: bars only.  ``BarFillPolicy.NEXT_BAR_OPEN`` fills at the open of
   the first completed bar whose start is at or after ``submitted_at + submission
   latency`` (never the close of the signal bar); ``NO_BAR_FILL`` refuses bar-only
   fills.  Every bar-only fill carries ``fidelity_limitation == "BAR_ONLY"``.
3. Bar sequence: (a) at the open: market orders, limits that are marketable at the
   open, and stops the open gapped through (filled at the open, not the stop price);
   (b) intrabar: stops with ``low <= stop`` at the stop price, then limits that the
   bar trades strictly through, at their limit.  Intrabar fills are stamped with the
   bar end.  Protection created from a bar-only entry fill is the sole exception to
   normal activation timing: its initial stop covers the remainder of that entry bar.
   Other orders and stop replacements act only on bars starting at or after activation.
4. Intrabar ambiguity.  A stop touched intrabar in a bar that also reaches a
   favorable level (a SELL limit, or a high that would ratchet the same native
   trailing stop) is ambiguous.  The candle's high/low never determine which came
   first; ``AmbiguityPolicy.STOP_FIRST`` (default) executes the stop first and lets a
   favorable order take only what is left, ``STOP_ONLY`` additionally withholds the
   favorable SELL limits for that bar.  Fills of that bar carry
   ``AMBIGUOUS_INTRABAR_ORDER``.
5. Trailing stops first test the stop active at the start of a bar, then ratchet from
   that completed bar's favorable extreme for later bars only, and never move backward.
   This includes the entry bar after its initial stop has survived.  Fixed-stop
   replacements are applied only after
   ``stop_replace_latency``; the previous stop stays active meanwhile; a lower
   stop is rejected (never-move-down).  Stepped trailing is computed by the
   coordinator and reaches this simulator as a stop replacement.
6. Quantity safety.  SELLs need a held position and a quantity within it; protective
   stops need a confirmed positive filled position, and their open quantity is
   clamped to the held quantity after every change.  Overlapping SELLs are clamped at
   fill time to the held quantity, so the simulator never goes short, and every open
   SELL is canceled once the position is flat.
7. Liquidity.  A fill takes at most ``participation`` x bar volume (or displayed
   quote size) in total across all orders of that bar/quote; leftovers stay pending.
   Whole-share orders floor to whole shares.
8. Session.  Only regular-session bars/quotes fill (early closes honoured through
   the NYSE calendar); an unknown calendar date, a trading halt (until its resume,
   else to the session close) or a zero-volume bar produces no fill.  A split while
   orders or a position are open raises ``NotImplementedError`` (fail closed).
9. Costs are Decimal.  Slippage = bps of the touch + ticks x tick size, always
   adverse, rounded against the trader.  Fee = per-order (first fill only) +
   per-share x quantity + bps of notional, rounded up to ``fee_quantum``.
10. Every ``SimulatedExecution`` records the simulator version, assumptions, data
    provenance, ``estimated=True`` / ``broker_confirmed=False`` and any flags.
    Non-fill decisions are kept as ``diagnostics`` (time, order id, reason).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal
from enum import StrEnum
from types import MappingProxyType

from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.feed import HistoricalReplayFeed, QuoteFidelity, ReplayEvent
from mike_trade.brokers.shadow.fills import FillAssumptions, ShadowFillSimulator
from mike_trade.domain.accounts import (
    AccountEnvironment,
    AccountSnapshot,
    BrokerCapabilities,
)
from mike_trade.domain.errors import (
    BrokerRejectedError,
    MikeTradeError,
    UnsupportedOrderCapabilityError,
)
from mike_trade.domain.market import Candle, MarketEventKind
from mike_trade.domain.protection import (
    ProtectionKind,
    ProtectionPolicy,
    ProtectionStatus,
    TrailingStopState,
    start_protection,
)
from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    Fill,
    ManagementStatus,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    Position,
    PositionDirection,
    TimeInForce,
)
from mike_trade.execution.order_fsm import (
    TERMINAL_STATUSES,
    OrderEvent,
    OrderStatus,
    apply_transition,
)
from mike_trade.market_data.context import QuoteSnapshot
from mike_trade.risk.fixed_stop import ALPACA_EQUITY_TICK_RULES
from mike_trade.scheduling.calendar import (
    NEW_YORK,
    SessionStatus,
    is_trading_day,
    next_session,
    session_for,
    session_status,
)

__all__ = [
    "AMBIGUOUS_INTRABAR_ORDER",
    "EXECUTION_SIMULATOR_VERSION",
    "AmbiguityPolicy",
    "BarFillPolicy",
    "ExecutionAssumptions",
    "HistoricalExecutionSimulator",
    "SimulatedExecution",
]

EXECUTION_SIMULATOR_VERSION = "backtest-exec/2"
AMBIGUOUS_INTRABAR_ORDER = "AMBIGUOUS_INTRABAR_ORDER"

_DATA_GAP_BEFORE_FILL = "DATA_GAP_BEFORE_FILL"
_BAR_ONLY = "BAR_ONLY"
_ZERO = Decimal("0")
_BPS = Decimal("10000")
_STOP_TYPES = frozenset({OrderType.STOP, OrderType.TRAILING_STOP})
_SUPPORTED_TYPES = frozenset(
    {OrderType.MARKET, OrderType.LIMIT, OrderType.STOP, OrderType.TRAILING_STOP}
)
_BROKER_STATUS = {
    OrderStatus.ACCEPTED: BrokerOrderStatus.ACCEPTED,
    OrderStatus.PARTIALLY_FILLED: BrokerOrderStatus.PARTIALLY_FILLED,
    OrderStatus.FILLED: BrokerOrderStatus.FILLED,
    OrderStatus.CANCELED: BrokerOrderStatus.CANCELED,
    OrderStatus.EXPIRED: BrokerOrderStatus.EXPIRED,
    OrderStatus.REJECTED: BrokerOrderStatus.REJECTED,
}


class BarFillPolicy(StrEnum):
    """How bar-only evidence (no quotes) may produce a fill."""

    NEXT_BAR_OPEN = "NEXT_BAR_OPEN"
    NO_BAR_FILL = "NO_BAR_FILL"


class AmbiguityPolicy(StrEnum):
    """Resolution of a bar that touches a stop and also reaches a favorable level.

    Both members are pessimistic; no member lets the favorable order go first.
    """

    STOP_FIRST = "STOP_FIRST"
    STOP_ONLY = "STOP_ONLY"


# --------------------------------------------------------------------------- helpers


def _decimal(value: object, name: str, *, inclusive: bool = True) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise ValueError(f"{name} must be Decimal; got {type(value).__name__}")
    if not value.is_finite() or value < _ZERO or (not inclusive and value == _ZERO):
        raise ValueError(f"{name} must be a finite Decimal {'>=' if inclusive else '>'} 0")
    return value


def _duration(value: object, name: str) -> timedelta:
    if not isinstance(value, timedelta) or value < timedelta(0):
        raise ValueError(f"{name} must be a non-negative timedelta")
    return value


def _buys(side: OrderSide) -> bool:
    """True for the broker-side buy variants (BUY, BUY_TO_COVER): they pay the ask."""
    return side.broker_side == "buy"


def _is_fractional(quantity: Decimal) -> bool:
    return quantity != quantity.to_integral_value()


def _micros(value: timedelta) -> str:
    return str(value // timedelta(microseconds=1))


# --------------------------------------------------------------------------- assumptions


@dataclass(frozen=True, slots=True)
class ExecutionAssumptions:
    """Every tunable of the simulation; recorded verbatim on each execution."""

    bar_fill_policy: BarFillPolicy = BarFillPolicy.NEXT_BAR_OPEN
    ambiguity_policy: AmbiguityPolicy = AmbiguityPolicy.STOP_FIRST
    submission_latency: timedelta = timedelta(0)
    stop_replace_latency: timedelta = timedelta(0)
    max_quote_age: timedelta = timedelta(seconds=5)
    slippage_bps: Decimal = _ZERO
    slippage_ticks: Decimal = _ZERO
    tick_size: Decimal = Decimal("0.01")
    price_quantum: Decimal = Decimal("0.0001")
    participation: Decimal = Decimal("1")
    quote_size_multiplier: Decimal = Decimal("1")
    fee_per_order: Decimal = _ZERO
    fee_per_share: Decimal = _ZERO
    fee_bps: Decimal = _ZERO
    fee_quantum: Decimal = Decimal("0.01")
    allow_fractional: bool = False
    fractional_order_types: frozenset[OrderType] = frozenset({OrderType.MARKET, OrderType.LIMIT})
    quantity_quantum: Decimal = Decimal("0.000000001")

    def __post_init__(self) -> None:
        if not isinstance(self.bar_fill_policy, BarFillPolicy):
            raise ValueError("bar_fill_policy must be a BarFillPolicy")
        if not isinstance(self.ambiguity_policy, AmbiguityPolicy):
            raise ValueError("ambiguity_policy must be an AmbiguityPolicy")
        _duration(self.submission_latency, "submission_latency")
        _duration(self.stop_replace_latency, "stop_replace_latency")
        for name in ("fee_per_order", "fee_per_share", "fee_bps"):
            _decimal(getattr(self, name), name)
        _decimal(self.fee_quantum, "fee_quantum", inclusive=False)
        # Shared knobs (and the fractional fail-closed rules) are validated by the
        # shadow assumptions the quote path is delegated to.
        self.to_fill_assumptions()

    def to_fill_assumptions(self) -> FillAssumptions:
        """The shadow-simulator view; fees stay here so they are applied exactly once."""
        return FillAssumptions(
            latency=self.submission_latency,
            max_quote_age=self.max_quote_age,
            slippage_bps=self.slippage_bps,
            slippage_ticks=self.slippage_ticks,
            tick_size=self.tick_size,
            price_quantum=self.price_quantum,
            participation=self.participation,
            quote_size_multiplier=self.quote_size_multiplier,
            allow_fractional=self.allow_fractional,
            fractional_order_types=self.fractional_order_types,
            quantity_quantum=self.quantity_quantum,
        )

    def as_mapping(self) -> Mapping[str, str]:
        """String form recorded on every execution."""
        return MappingProxyType(
            {
                "bar_fill_policy": self.bar_fill_policy.value,
                "ambiguity_policy": self.ambiguity_policy.value,
                "submission_latency_us": _micros(self.submission_latency),
                "stop_replace_latency_us": _micros(self.stop_replace_latency),
                "max_quote_age_us": _micros(self.max_quote_age),
                "slippage_bps": str(self.slippage_bps),
                "slippage_ticks": str(self.slippage_ticks),
                "tick_size": str(self.tick_size),
                "price_quantum": str(self.price_quantum),
                "participation": str(self.participation),
                "quote_size_multiplier": str(self.quote_size_multiplier),
                "fee_per_order": str(self.fee_per_order),
                "fee_per_share": str(self.fee_per_share),
                "fee_bps": str(self.fee_bps),
                "fee_quantum": str(self.fee_quantum),
                "allow_fractional": str(self.allow_fractional),
                "fractional_order_types": ",".join(
                    sorted(t.value for t in self.fractional_order_types)
                ),
                "quantity_quantum": str(self.quantity_quantum),
                "session": "regular_only;America/New_York",
            }
        )


# --------------------------------------------------------------------------- result


@dataclass(frozen=True, slots=True)
class SimulatedExecution:
    """One estimated fill with the evidence, costs and assumptions behind it."""

    fill: Fill
    purpose: OrderPurpose
    order_type: OrderType
    sequence: int
    evidence: str
    evidence_time: datetime
    reference_price: Decimal
    slippage: Decimal
    slippage_cost: Decimal
    order_remaining: Decimal
    position_after: Decimal
    fidelity_limitation: str | None
    flags: tuple[str, ...]
    simulator_version: str
    assumptions: Mapping[str, str]
    provenance: Mapping[str, str]
    seed: int
    estimated: bool = field(default=True, init=False)
    broker_confirmed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        for name in ("reference_price", "slippage", "slippage_cost", "order_remaining",
                     "position_after"):
            _decimal(getattr(self, name), name)
        object.__setattr__(self, "flags", tuple(sorted(set(self.flags))))
        object.__setattr__(self, "assumptions", MappingProxyType(dict(self.assumptions)))
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))

    @property
    def symbol(self) -> str:
        return self.fill.symbol

    @property
    def side(self) -> OrderSide:
        return self.fill.side

    @property
    def quantity(self) -> Decimal:
        return self.fill.quantity

    @property
    def price(self) -> Decimal:
        return self.fill.price

    @property
    def fee(self) -> Decimal:
        return self.fill.fee


# --------------------------------------------------------------------------- internals


@dataclass(frozen=True, slots=True)
class _Replacement:
    effective_at: datetime
    stop_price: Decimal | None
    quantity: Decimal | None


@dataclass(slots=True)
class _Order:
    seq: int
    broker_order_id: str
    intent: OrderIntent
    status: OrderStatus
    quantity: Decimal
    submitted_at: datetime
    eligible_at: datetime
    expires_at: datetime | None
    updated_at: datetime
    filled: Decimal = _ZERO
    notional: Decimal = _ZERO
    fills: int = 0
    prot: TrailingStopState | None = None
    policy: ProtectionPolicy | None = None
    pending: list[_Replacement] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status not in TERMINAL_STATUSES

    @property
    def side(self) -> OrderSide:
        return self.intent.side

    @property
    def order_type(self) -> OrderType:
        return self.intent.order_type

    @property
    def limit_price(self) -> Decimal | None:
        return self.intent.limit_price

    @property
    def remaining(self) -> Decimal:
        return self.quantity - self.filled


@dataclass(slots=True)
class _Held:
    account_scope_id: str
    cycle_id: str
    quantity: Decimal
    cost: Decimal
    direction: PositionDirection


@dataclass(frozen=True, slots=True)
class _CycleView:
    """The structural surface ``start_protection`` reads from a position cycle."""

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


@dataclass(frozen=True, slots=True)
class _Evidence:
    kind: str
    time: datetime
    detail: Mapping[str, str]
    limitation: str | None


# --------------------------------------------------------------------------- simulator


class HistoricalExecutionSimulator:
    """Replay-only, BrokerPort-shaped matching engine; never contacts a broker."""

    environment = AccountEnvironment.BACKTEST

    def __init__(
        self,
        feed: HistoricalReplayFeed,
        clock: ReplayClock,
        assumptions: ExecutionAssumptions | None = None,
    ) -> None:
        if not isinstance(feed, HistoricalReplayFeed):
            raise TypeError("feed must be a HistoricalReplayFeed")
        if not isinstance(clock, ReplayClock):
            raise TypeError("clock must be a ReplayClock")
        if assumptions is None:
            assumptions = ExecutionAssumptions()
        if not isinstance(assumptions, ExecutionAssumptions):
            raise TypeError("assumptions must be ExecutionAssumptions")
        self._feed = feed
        self._clock = clock
        self._a = assumptions
        self._assumption_map = assumptions.as_mapping()
        self._shadow = ShadowFillSimulator(assumptions.to_fill_assumptions())
        self._orders: dict[str, _Order] = {}
        self._by_client: dict[str, str] = {}
        self._held: dict[str, _Held] = {}
        self._executions: list[SimulatedExecution] = []
        self._diagnostics: list[tuple[datetime, str, str]] = []
        self._halts: dict[str, list[tuple[datetime, datetime]]] = {}
        self._last_bar_end: dict[str, datetime] = {}
        self._last_price: dict[str, Decimal] = {}
        self._used: dict[tuple[object, ...], Decimal] = {}
        self._shadow_fill_ids: set[str] = set()
        self._entry_protection_stops: dict[str, Decimal] = {}
        self._entry_protection_rejections: list[Mapping[str, object]] = []
        self._cursor: datetime | None = None
        self._order_seq = 0
        self._exec_seq = 0

    # ---- introspection

    @property
    def version(self) -> str:
        return EXECUTION_SIMULATOR_VERSION

    @property
    def assumptions(self) -> ExecutionAssumptions:
        return self._a

    @property
    def executions(self) -> tuple[SimulatedExecution, ...]:
        return tuple(self._executions)

    @property
    def diagnostics(self) -> tuple[tuple[datetime, str, str], ...]:
        """Non-fill decisions as ``(time, broker_order_id or "", reason)``."""
        return tuple(self._diagnostics)

    @property
    def entry_protection_rejections(self) -> tuple[Mapping[str, object], ...]:
        """Entries rejected atomically because their required stop was already breached."""
        return tuple(self._entry_protection_rejections)

    def require_entry_protection_below(
        self, broker_order_id: str, stop_price: Decimal
    ) -> None:
        """Require a long entry fill to remain strictly above its prescribed stop.

        The check occurs against the simulator's adverse-slippage fill price immediately
        before committing the fill.  This models fail-closed entry admission without looking
        ahead to a later bar and without ever creating an unprotected simulated position.
        """
        order = self._orders.get(broker_order_id)
        if order is None:
            raise BrokerRejectedError(f"unknown order {broker_order_id!r}", code="unknown_order")
        if order.intent.purpose is not OrderPurpose.ENTRY or order.side is not OrderSide.BUY:
            raise ValueError("entry protection floor requires a long ENTRY order")
        self._entry_protection_stops[broker_order_id] = _decimal(
            stop_price, "stop_price", inclusive=False
        )

    def held_quantity(self, symbol: str) -> Decimal:
        held = self._held.get(symbol.upper())
        return _ZERO if held is None else held.quantity

    def position_direction(self, symbol: str) -> PositionDirection | None:
        held = self._held.get(symbol.upper())
        return None if held is None else held.direction

    def protective_coverage(self, symbol: str) -> Decimal:
        """Open quantity of protective stops on ``symbol`` (never above the held quantity)."""
        return sum(
            (o.remaining for o in self._open(symbol.upper()) if o.prot is not None), _ZERO
        )

    def protection_state(self, broker_order_id: str) -> TrailingStopState | None:
        order = self._orders.get(broker_order_id)
        return None if order is None else order.prot

    def pending_replacements(self, broker_order_id: str) -> int:
        order = self._orders.get(broker_order_id)
        return 0 if order is None else len(order.pending)

    # ---- BrokerPort

    def capabilities(self) -> BrokerCapabilities:
        a = self._a
        return BrokerCapabilities(
            provider="backtest",
            environment=AccountEnvironment.BACKTEST,
            supported_order_types=frozenset(t.value.lower() for t in _SUPPORTED_TYPES),
            supports_fractional=a.allow_fractional,
            supports_notional=False,
            supports_native_trailing_stop=True,
            supports_extended_hours=False,
            supports_short=True,
            paper_verified_combinations=(
                frozenset((t.value.lower(), True) for t in a.fractional_order_types)
                if a.allow_fractional
                else frozenset()
            ),
        )

    async def get_account(self) -> AccountSnapshot:
        raise NotImplementedError("the historical simulator does not model cash or equity")

    async def list_positions(self) -> Sequence[Position]:
        now = self._clock.now()
        return tuple(
            Position(
                account_scope_id=h.account_scope_id,
                symbol=symbol,
                quantity=h.quantity,
                avg_entry_price=h.cost / h.quantity,
                cost_basis=h.cost,
                as_of=now,
                management=ManagementStatus.MANAGED,
                cycle_id=h.cycle_id,
                direction=h.direction,
            )
            for symbol, h in sorted(self._held.items())
        )

    async def list_open_orders(self) -> Sequence[BrokerOrder]:
        return tuple(self._snapshot(o) for o in self._orders.values() if o.is_open)

    async def get_order(self, broker_order_id: str) -> BrokerOrder | None:
        order = self._orders.get(broker_order_id)
        return None if order is None else self._snapshot(order)

    async def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        order_id = self._by_client.get(client_order_id)
        return None if order_id is None else self._snapshot(self._orders[order_id])

    async def list_fills(self, since: datetime) -> Sequence[Fill]:
        return tuple(e.fill for e in self._executions if e.fill.filled_at >= since)

    async def submit_order(
        self,
        intent: OrderIntent,
        *,
        protection_reference: Decimal | None = None,
    ) -> BrokerOrder:
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be a domain OrderIntent")
        existing = self._by_client.get(intent.client_order_id)
        if existing is not None:
            return self._snapshot(self._orders[existing])
        quantity = self._check_capability(intent)
        if protection_reference is not None:
            _decimal(protection_reference, "protection_reference", inclusive=False)
            if intent.order_type not in _STOP_TYPES:
                raise ValueError("protection_reference is valid only for protective stops")
        symbol = intent.symbol
        held = self._held.get(symbol)
        if intent.side is OrderSide.SELL:
            if held is None or held.direction is not PositionDirection.LONG:
                raise BrokerRejectedError(
                    f"{symbol}: SELL without a held position would create a short",
                    code="would_short",
                )
            if quantity > held.quantity:
                raise BrokerRejectedError(
                    f"{symbol}: SELL quantity {quantity} exceeds held {held.quantity}",
                    code="exceeds_held_quantity",
                )
        elif intent.side is OrderSide.BUY_TO_COVER:
            if held is None or held.direction is not PositionDirection.SHORT:
                raise BrokerRejectedError(
                    f"{symbol}: BUY_TO_COVER requires an existing short",
                    code="no_short_position",
                )
            if quantity > held.quantity:
                raise BrokerRejectedError(
                    f"{symbol}: cover quantity {quantity} exceeds short {held.quantity}",
                    code="exceeds_held_quantity",
                )
        elif held is not None and held.direction is not intent.side.direction:
            # Never a simultaneous long and short; pyramiding is the lifecycle layer's rule.
            raise BrokerRejectedError(
                f"{symbol}: an open {held.direction.value} position blocks an opposite entry",
                code="opposite_position_open",
            )
        now = self._clock.now()
        self._order_seq += 1
        order = _Order(
            seq=self._order_seq,
            broker_order_id=f"bt-order-{self._order_seq:06d}",
            intent=intent,
            status=OrderStatus.INTENT_PERSISTED,
            quantity=quantity,
            submitted_at=now,
            eligible_at=now + self._a.submission_latency,
            expires_at=(
                _day_expiry(now) if intent.time_in_force is TimeInForce.DAY else None
            ),
            updated_at=now,
        )
        if intent.order_type in _STOP_TYPES:
            assert held is not None
            self._arm_protection(order, held, reference=protection_reference)
        self._transition(order, OrderStatus.SUBMITTING)
        self._transition(order, OrderStatus.ACCEPTED)
        self._orders[order.broker_order_id] = order
        self._by_client[intent.client_order_id] = order.broker_order_id
        return self._snapshot(order)

    async def replace_order(
        self,
        broker_order_id: str,
        *,
        stop_price: Decimal | None = None,
        trail: Decimal | None = None,
        quantity: Decimal | None = None,
    ) -> BrokerOrder:
        order = self._orders.get(broker_order_id)
        if order is None or not order.is_open:
            raise BrokerRejectedError(f"order {broker_order_id!r} is not open", code="not_open")
        if trail is not None:
            raise UnsupportedOrderCapabilityError("changing a trail value is not supported")
        if order.prot is None:
            raise UnsupportedOrderCapabilityError("only protective stop orders can be replaced")
        if stop_price is None and quantity is None:
            raise ValueError("replace_order needs a stop_price or a quantity")
        if stop_price is not None:
            if order.order_type is not OrderType.STOP:
                raise UnsupportedOrderCapabilityError(
                    "only fixed STOP orders accept a replacement stop price"
                )
            _decimal(stop_price, "stop_price", inclusive=False)
            pending_stops = [r.stop_price for r in order.pending if r.stop_price is not None]
            loosens = (
                stop_price < max([order.prot.stop_price, *pending_stops])
                if order.prot.direction is PositionDirection.LONG
                else stop_price > min([order.prot.stop_price, *pending_stops])
            )
            if loosens:
                raise ValueError(
                    "a long protective stop must never move downward"
                    if order.prot.direction is PositionDirection.LONG
                    else "a short protective stop must never move upward"
                )
        if quantity is not None:
            _decimal(quantity, "quantity", inclusive=False)
            remaining = quantity - order.filled
            if remaining <= _ZERO:
                raise ValueError("replacement quantity must exceed the filled quantity")
            if remaining > self.held_quantity(order.intent.symbol):
                raise BrokerRejectedError(
                    "protective coverage would exceed the confirmed filled quantity",
                    code="exceeds_held_quantity",
                )
        now = self._clock.now()
        order.pending.append(
            _Replacement(now + self._a.stop_replace_latency, stop_price, quantity)
        )
        self._apply_replacements(order.intent.symbol, now)
        return self._snapshot(order)

    async def cancel_order(self, broker_order_id: str) -> None:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise BrokerRejectedError(f"unknown order {broker_order_id!r}", code="unknown_order")
        if order.is_open:
            self._cancel(order, self._clock.now(), "canceled")

    # ---- replay driving

    def advance_to(self, t: datetime) -> tuple[SimulatedExecution, ...]:
        """Advance the clock to ``t`` and process every feed event available by then."""
        self._clock.advance_to(t)
        events = self._feed.as_of(t, since=self._cursor)
        self._cursor = t
        out: list[SimulatedExecution] = []
        for event in events:
            out.extend(self.process_event(event))
        self._expire(t)
        return tuple(out)

    def process_event(self, event: ReplayEvent) -> tuple[SimulatedExecution, ...]:
        """Apply one replay event (in availability order) and return resulting fills."""
        if event.kind == ReplayEvent.KIND_BAR:
            assert event.candle is not None
            return tuple(self._on_bar(event.candle))
        if event.kind == ReplayEvent.KIND_QUOTE:
            return tuple(self._on_quote(event))
        if event.kind == ReplayEvent.KIND_DATA_QUALITY:
            if event.reason == ReplayEvent.REASON_HALT:
                self._halts.setdefault(event.symbol, []).append(
                    (event.event_time, event.window_end or _session_end(event.event_time))
                )
            return ()
        if self._open(event.symbol) or event.symbol in self._held:
            raise NotImplementedError("splits with open orders or positions are not simulated")
        return ()

    def process_entry_bar_protection(
        self,
        broker_order_id: str,
        candle: Candle,
        *,
        favorable_extreme_reached: bool = False,
        allow_trailing_ratchet: bool = False,
    ) -> tuple[SimulatedExecution, ...]:
        """Apply newly armed protection to the remainder of its entry bar.

        Bar-only market entries fill at the next bar's open, but the lifecycle can only
        submit their protective order when that completed bar becomes available.  This
        method closes that representation gap: the initial stop is tested against the
        entry bar before any favorable extreme is allowed to ratchet trailing protection.

        ``favorable_extreme_reached`` is supplied for coordinator-managed stepped
        trailing stops.  ``allow_trailing_ratchet`` is true only when the entry filled
        at the open; an intrabar fill cannot safely consume a high/low that may have
        preceded it.  Native trailing orders derive their favorable move from policy.
        An entry bar touching both relevant extremes is resolved stop-first.
        """
        if not isinstance(favorable_extreme_reached, bool):
            raise TypeError("favorable_extreme_reached must be bool")
        if not isinstance(allow_trailing_ratchet, bool):
            raise TypeError("allow_trailing_ratchet must be bool")
        order = self._orders.get(broker_order_id)
        if order is None:
            raise BrokerRejectedError(
                f"unknown order {broker_order_id!r}", code="unknown_order"
            )
        if candle.symbol != order.intent.symbol:
            raise ValueError("entry candle symbol must match the protective order")
        if order.prot is None:
            raise ValueError("entry-bar protection requires a protective stop order")
        if (
            not order.is_open
            or self._feed.quote_fidelity(candle.symbol) is not QuoteFidelity.UNAVAILABLE
        ):
            return ()
        if self._last_bar_end.get(candle.symbol) != candle.end:
            raise ValueError("entry candle must be the simulator's most recently processed bar")
        if self._bar_block_reason(candle, _bar_in_regular_session(candle)) is not None:
            return ()

        if order.prot.status is ProtectionStatus.PENDING:
            order.prot = replace(order.prot, status=ProtectionStatus.ACTIVE)
        native_ratchet = (
            allow_trailing_ratchet
            and order.order_type is OrderType.TRAILING_STOP
            and self._candidate_stop(order, candle.high, candle.low) != order.prot.stop_price
        )
        hit = self._stop_crossed(order, candle.low, candle.high)
        ambiguous = hit and (favorable_extreme_reached or native_ratchet)
        if ambiguous:
            self._note(candle.start, broker_order_id, AMBIGUOUS_INTRABAR_ORDER)

        out: list[SimulatedExecution] = []
        if hit:
            evidence = self._bar_evidence(candle)
            stop = order.prot.stop_price
            execution = self._try_fill(
                order,
                stop,
                self._price(order, stop, capped=False),
                candle.end,
                (candle.symbol, "BAR", candle.start),
                candle.volume * self._a.participation,
                evidence,
                (AMBIGUOUS_INTRABAR_ORDER,) if ambiguous else (),
            )
            if execution is not None:
                out.append(execution)

        # The initial stop was tested first.  Only a surviving native trailing order may
        # consume the completed entry bar's favorable extreme for subsequent bars.
        if (
            allow_trailing_ratchet
            and order.is_open
            and order.order_type is OrderType.TRAILING_STOP
        ):
            self._ratchet_order(order, candle.high, candle.low)
        return tuple(out)

    # ---- admission

    def _check_capability(self, intent: OrderIntent) -> Decimal:
        if intent.quantity is None:
            raise UnsupportedOrderCapabilityError("notional orders are not supported")
        quantity = intent.quantity
        fractional = _is_fractional(quantity)
        if not self.capabilities().supports_order(intent.order_type.value.lower(), fractional):
            raise UnsupportedOrderCapabilityError(
                f"{intent.order_type.value} (fractional={fractional}) is not supported"
            )
        if fractional and intent.time_in_force is not TimeInForce.DAY:
            raise UnsupportedOrderCapabilityError("fractional orders must be DAY")
        if intent.order_type in _STOP_TYPES and intent.side not in {
            OrderSide.SELL,
            OrderSide.BUY_TO_COVER,
        }:
            raise UnsupportedOrderCapabilityError("stop orders require a closing side")
        if intent.purpose is OrderPurpose.PROTECTIVE_STOP and intent.order_type not in _STOP_TYPES:
            raise UnsupportedOrderCapabilityError("a protective stop must be a stop order")
        return quantity

    def _arm_protection(
        self, order: _Order, held: _Held, *, reference: Decimal | None = None
    ) -> None:
        intent = order.intent
        avg = held.cost / held.quantity
        if intent.order_type is OrderType.STOP:
            assert intent.stop_price is not None
            order.prot = TrailingStopState(
                cycle_id=intent.cycle_id,
                account_scope_id=intent.account_scope_id,
                symbol=intent.symbol,
                kind=ProtectionKind.FIXED_STOP,
                status=ProtectionStatus.PENDING,
                covered_quantity=order.quantity,
                stop_price=intent.stop_price,
                high_water_mark=(
                    max(intent.stop_price, avg)
                    if held.direction is PositionDirection.LONG
                    else min(intent.stop_price, avg)
                ),
                direction=held.direction,
            )
            return
        order.policy = ProtectionPolicy(
            kind=ProtectionKind.BROKER_TRAILING_STOP,
            trail_percent=intent.trail_percent,
            trail_amount=intent.trail_price,
        )
        view = _CycleView(
            intent.cycle_id,
            intent.account_scope_id,
            intent.symbol,
            order.quantity,
            held.direction,
        )
        # ``start_protection`` reads only the structural cycle surface that ``_CycleView`` has.
        state = start_protection(
            order.policy,
            view,
            reference if reference is not None else self._last_price.get(intent.symbol, avg),
        )  # type: ignore[arg-type]
        rounded = (
            ALPACA_EQUITY_TICK_RULES.floor(state.stop_price)
            if held.direction is PositionDirection.LONG
            else ALPACA_EQUITY_TICK_RULES.ceil(state.stop_price)
        )
        if rounded <= _ZERO:
            raise ValueError("trailing distance leaves no positive stop")
        order.prot = replace(state, stop_price=rounded)

    # ---- bars

    def _on_bar(self, c: Candle) -> list[SimulatedExecution]:
        symbol = c.symbol
        self._expire(c.start)
        previous_end = self._last_bar_end.get(symbol)
        if previous_end is not None and c.start < previous_end:
            self._note(c.start, "", "bar_not_after_previous_bar")
            return []
        gap_from = previous_end if previous_end is not None and c.start > previous_end else None
        self._last_bar_end[symbol] = c.end
        regular = _bar_in_regular_session(c)
        out: list[SimulatedExecution] = []
        if self._feed.quote_fidelity(symbol) is QuoteFidelity.UNAVAILABLE:
            out = self._match_bar(c, regular, gap_from)
        if regular:
            # Completed-bar information only reaches orders from the next bar on.
            self._last_price[symbol] = c.close
            self._ratchet(symbol, c.high, c.low, c.start)
        return out

    def _match_bar(
        self, c: Candle, regular: bool, gap_from: datetime | None
    ) -> list[SimulatedExecution]:
        symbol, a = c.symbol, self._a
        reason = self._bar_block_reason(c, regular)
        if reason is not None:
            if self._open(symbol):
                self._note(c.start, "", reason)
            return []
        self._apply_replacements(symbol, c.start)
        self._sync(symbol, c.start)
        orders = sorted(
            (o for o in self._open(symbol) if o.eligible_at <= c.start),
            key=lambda o: (o.eligible_at, o.seq),
        )
        if not orders:
            return []
        for o in orders:
            if o.prot is not None and o.prot.status is ProtectionStatus.PENDING:
                o.prot = replace(o.prot, status=ProtectionStatus.ACTIVE)
        evidence = self._bar_evidence(c)

        key = (c.symbol, "BAR", c.start)
        cap = c.volume * a.participation
        out: list[SimulatedExecution] = []

        def flags_for(o: _Order, ambiguous: bool) -> tuple[str, ...]:
            found = [AMBIGUOUS_INTRABAR_ORDER] if ambiguous else []
            if gap_from is not None and o.eligible_at <= gap_from:
                found.append(_DATA_GAP_BEFORE_FILL)
            return tuple(found)

        def fill(o: _Order, touch: Decimal, price: Decimal | None, at: datetime,
                 ambiguous: bool) -> None:
            ex = self._try_fill(o, touch, price, at, key, cap, evidence, flags_for(o, ambiguous))
            if ex is not None:
                out.append(ex)

        # (a) the open: market, marketable limits, and stops the open gapped through
        for o in orders:
            if not o.is_open:
                continue
            if o.order_type is OrderType.MARKET:
                fill(o, c.open, self._price(o, c.open, capped=False), c.start, False)
            elif o.order_type is OrderType.LIMIT:
                assert o.limit_price is not None
                marketable = (
                    c.open <= o.limit_price if _buys(o.side) else c.open >= o.limit_price
                )
                if marketable:
                    fill(o, c.open, self._price(o, c.open, capped=True), c.start, False)
            elif o.prot is not None and self._stop_crossed(o, c.open, c.open):
                fill(o, c.open, self._price(o, c.open, capped=False), c.start, False)

        # (b) intrabar: stops first, then limits traded strictly through
        live = [o for o in orders if o.is_open]
        hit = [o for o in live if o.prot is not None and self._stop_crossed(o, c.low, c.high)]
        favorable = [
            o for o in live
            if o.order_type is OrderType.LIMIT and o.limit_price is not None
            and (
                (o.side is OrderSide.SELL and c.high >= o.limit_price)
                or (o.side is OrderSide.BUY_TO_COVER and c.low <= o.limit_price)
            )
        ]
        ratchets = any(
            o.order_type is OrderType.TRAILING_STOP
            and o.prot is not None
            and self._candidate_stop(o, c.high, c.low) != o.prot.stop_price
            for o in hit
        )
        ambiguous = bool(hit) and self.held_quantity(symbol) > _ZERO and bool(favorable or ratchets)
        if ambiguous:
            self._note(c.start, "", AMBIGUOUS_INTRABAR_ORDER)
        for o in hit:
            if o.is_open and o.prot is not None:
                stop = o.prot.stop_price
                fill(o, stop, self._price(o, stop, capped=False), c.end, ambiguous)
        withhold = ambiguous and a.ambiguity_policy is AmbiguityPolicy.STOP_ONLY
        for o in live:
            if not o.is_open or o.order_type is not OrderType.LIMIT or o.limit_price is None:
                continue
            through = c.low < o.limit_price if _buys(o.side) else c.high > o.limit_price
            if through and not (withhold and o.side.is_exit):
                fill(o, o.limit_price, o.limit_price, c.end, ambiguous)
        return out

    def _bar_evidence(self, c: Candle) -> _Evidence:
        return _Evidence(
            "BAR",
            c.start,
            {
                "bar_start": c.start.isoformat(),
                "bar_end": c.end.isoformat(),
                "open": str(c.open),
                "high": str(c.high),
                "low": str(c.low),
                "close": str(c.close),
                "volume": str(c.volume),
                "bar_fill_policy": self._a.bar_fill_policy.value,
            },
            _BAR_ONLY,
        )

    def _bar_block_reason(self, c: Candle, regular: bool) -> str | None:
        if self._a.bar_fill_policy is BarFillPolicy.NO_BAR_FILL:
            return "bar_only_fills_disabled"
        if not regular:
            return "outside_regular_session"
        if self._halted(c.symbol, c.start, c.end):
            return "trading_halt"
        if c.volume <= _ZERO:
            return "no_bar_volume"
        return None

    # ---- quotes

    def _on_quote(self, event: ReplayEvent) -> list[SimulatedExecution]:
        q = event.quote
        assert q is not None and q.kind is MarketEventKind.QUOTE and event.available_at is not None
        try:
            assert q.bid is not None and q.ask is not None
            snap = QuoteSnapshot(
                symbol=q.symbol, bid=q.bid, ask=q.ask, bid_size=q.bid_size, ask_size=q.ask_size,
                provider_ts=q.event_time, receive_ts=event.available_at, feed_id=q.provenance.feed,
            )
        except (ValueError, MikeTradeError):
            self._note(q.event_time, "", "quote_invalid")
            return []
        symbol, at = snap.symbol, snap.provider_ts
        self._expire(at)
        if session_status(at) is not SessionStatus.REGULAR or self._halted(symbol, at, at):
            if self._open(symbol):
                self._note(at, "", "outside_regular_session_or_halt")
            return []
        self._apply_replacements(symbol, at)
        self._sync(symbol, at)
        evidence_detail = {
            "quote_provider_ts": at.isoformat(),
            "quote_receive_ts": snap.receive_ts.isoformat(),
            "bid": str(snap.bid),
            "ask": str(snap.ask),
            "bid_size": str(snap.bid_size),
            "ask_size": str(snap.ask_size),
            "quote_feed": snap.feed_id,
        }
        evidence = _Evidence("QUOTE", at, evidence_detail, None)
        out: list[SimulatedExecution] = []
        for o in sorted(self._open(symbol), key=lambda o: (o.eligible_at, o.seq)):
            if not o.is_open or o.eligible_at > at:
                continue
            ex = (
                self._quote_stop_fill(o, snap, evidence)
                if o.prot is not None
                else self._quote_shadow_fill(o, snap, evidence)
            )
            if ex is not None:
                out.append(ex)
        held = self._held.get(symbol)
        short = held is not None and held.direction is PositionDirection.SHORT
        self._last_price[symbol] = snap.ask if short else snap.bid
        self._ratchet(symbol, snap.bid, snap.ask, at)
        return out

    def _quote_shadow_fill(
        self, o: _Order, snap: QuoteSnapshot, evidence: _Evidence
    ) -> SimulatedExecution | None:
        want = o.remaining
        if o.side.is_exit:
            want = min(want, self._held_for(o.intent.symbol, o.side.direction))
        if want <= _ZERO:
            return None
        avg = o.notional / o.filled if o.filled > _ZERO else None
        view = BrokerOrder(
            broker_order_id=o.broker_order_id,
            client_order_id=o.intent.client_order_id,
            account_scope_id=o.intent.account_scope_id,
            symbol=o.intent.symbol,
            side=o.side,
            order_type=o.order_type,
            status=BrokerOrderStatus.PARTIALLY_FILLED if o.filled > _ZERO
            else BrokerOrderStatus.ACCEPTED,
            quantity=o.filled + want,
            filled_quantity=o.filled,
            submitted_at=o.submitted_at,
            updated_at=o.updated_at,
            filled_avg_price=avg,
            limit_price=o.limit_price,
        )
        result = self._shadow.simulate(
            view,
            [snap],
            as_of=snap.receive_ts,
            time_in_force=o.intent.time_in_force,
            existing_fill_ids=self._shadow_fill_ids,
        )
        if not result.is_fill:
            self._note(snap.provider_ts, o.broker_order_id, result.reason)
            return None
        assert result.price is not None and result.quote_price is not None
        assert result.fill_id is not None
        self._shadow_fill_ids.add(result.fill_id)
        size = snap.ask_size if _buys(o.side) else snap.bid_size
        assert size is not None
        cap = size * self._a.quote_size_multiplier * self._a.participation
        key = (snap.symbol, "QUOTE", o.side, snap.provider_ts, snap.receive_ts)
        return self._try_fill(
            o, result.quote_price, result.price, snap.provider_ts, key, cap, evidence, (),
            want=result.quantity,
        )

    def _quote_stop_fill(
        self, o: _Order, snap: QuoteSnapshot, evidence: _Evidence
    ) -> SimulatedExecution | None:
        assert o.prot is not None
        if o.prot.status is ProtectionStatus.PENDING:
            o.prot = replace(o.prot, status=ProtectionStatus.ACTIVE)
        short = o.prot.direction is PositionDirection.SHORT
        # A long stop sells at the bid once the bid falls to it; a short stop covers at the
        # ask once the ask rises to it.
        touch = snap.ask if short else snap.bid
        if (touch < o.prot.stop_price) if short else (touch > o.prot.stop_price):
            return None
        size = snap.ask_size if short else snap.bid_size
        reason = None
        if snap.bid >= snap.ask:
            reason = "quote_crossed_or_locked"
        elif snap.receive_ts - snap.provider_ts > self._a.max_quote_age:
            reason = "quote_stale"
        elif size is None or size <= _ZERO:
            reason = "quote_size_unavailable"
        if reason is not None:
            self._note(snap.provider_ts, o.broker_order_id, reason)
            return None
        assert size is not None
        cap = size * self._a.quote_size_multiplier * self._a.participation
        key = (snap.symbol, "QUOTE", o.side, snap.provider_ts, snap.receive_ts)
        return self._try_fill(
            o, touch, self._price(o, touch, capped=False), snap.provider_ts, key, cap,
            evidence, (),
        )

    # ---- shared fill machinery

    def _price(self, o: _Order, touch: Decimal, *, capped: bool) -> Decimal | None:
        """Touch plus adverse slippage, rounded against the trader (limit never exceeded)."""
        a = self._a
        slip = touch * a.slippage_bps / _BPS + a.slippage_ticks * a.tick_size
        if _buys(o.side):
            price = (touch + slip).quantize(a.price_quantum, rounding=ROUND_CEILING)
            if capped and o.limit_price is not None:
                price = min(price, o.limit_price)
            return price
        price = (touch - slip).quantize(a.price_quantum, rounding=ROUND_FLOOR)
        if capped and o.limit_price is not None:
            price = max(price, o.limit_price)
        price = min(price, touch)
        return price if price > _ZERO else None

    def _fee(self, first: bool, quantity: Decimal, price: Decimal) -> Decimal:
        a = self._a
        fee = (
            (a.fee_per_order if first else _ZERO)
            + a.fee_per_share * quantity
            + quantity * price * a.fee_bps / _BPS
        )
        return fee.quantize(a.fee_quantum, rounding=ROUND_CEILING)

    def _take(
        self, key: tuple[object, ...], cap: Decimal, want: Decimal, order_quantity: Decimal
    ) -> Decimal:
        """Reserve up to ``want`` from the shared liquidity ``cap`` of ``key``."""
        quantity = min(want, cap - self._used.get(key, _ZERO))
        if quantity <= _ZERO:
            return _ZERO
        if _is_fractional(order_quantity):
            quantity = quantity.quantize(self._a.quantity_quantum, rounding=ROUND_DOWN)
        else:
            quantity = quantity.to_integral_value(rounding=ROUND_FLOOR)
        if quantity > _ZERO:
            self._used[key] = self._used.get(key, _ZERO) + quantity
        return quantity

    def _try_fill(
        self,
        o: _Order,
        touch: Decimal,
        price: Decimal | None,
        at: datetime,
        key: tuple[object, ...],
        cap: Decimal,
        evidence: _Evidence,
        flags: tuple[str, ...],
        *,
        want: Decimal | None = None,
    ) -> SimulatedExecution | None:
        if price is None:
            self._note(at, o.broker_order_id, "price_non_positive_after_slippage")
            return None
        required_stop = self._entry_protection_stops.get(o.broker_order_id)
        if required_stop is not None and o.side is OrderSide.BUY and price <= required_stop:
            reason = "ENTRY_REJECTED_PROTECTIVE_STOP_NOT_BELOW_FILL"
            self._transition(o, OrderStatus.REJECTED)
            o.updated_at = at
            self._note(at, o.broker_order_id, reason)
            self._entry_protection_rejections.append(
                {
                    "time": at,
                    "order_id": o.broker_order_id,
                    "symbol": o.intent.symbol,
                    "entry_price": price,
                    "initial_stop_price": required_stop,
                    "reason": reason,
                }
            )
            return None
        wanted = o.remaining if want is None else want
        if o.side.is_exit:
            wanted = min(wanted, self._held_for(o.intent.symbol, o.side.direction))
        else:
            held = self._held.get(o.intent.symbol)
            if held is not None and held.direction is not o.side.direction:
                self._note(at, o.broker_order_id, "opposite_position_blocks_entry")
                return None
        quantity = self._take(key, cap, wanted, o.quantity) if wanted > _ZERO else _ZERO
        if quantity <= _ZERO:
            self._note(at, o.broker_order_id, "no_liquidity_or_below_lot")
            return None
        return self._commit(o, quantity, price, touch, at, evidence, flags)

    def _commit(
        self,
        o: _Order,
        quantity: Decimal,
        price: Decimal,
        touch: Decimal,
        at: datetime,
        evidence: _Evidence,
        flags: tuple[str, ...],
    ) -> SimulatedExecution:
        intent = o.intent
        fill = Fill(
            fill_id=f"bt-fill:{o.broker_order_id}:{o.fills + 1}",
            broker_order_id=o.broker_order_id,
            account_scope_id=intent.account_scope_id,
            symbol=intent.symbol,
            side=o.side,
            quantity=quantity,
            price=price,
            fee=self._fee(o.fills == 0, quantity, price),
            filled_at=at,
        )
        o.filled += quantity
        o.notional += quantity * price
        o.fills += 1
        o.updated_at = at
        self._transition(
            o,
            OrderStatus.FILLED if o.filled == o.quantity else OrderStatus.PARTIALLY_FILLED,
            filled_qty=quantity,
        )
        if o.prot is not None:
            if o.remaining > _ZERO:
                o.prot = replace(o.prot, covered_quantity=o.remaining)
            else:
                o.prot = replace(o.prot, status=ProtectionStatus.TRIGGERED)
        self._apply_to_position(o, quantity, price)
        self._sync(intent.symbol, at)
        self._exec_seq += 1
        execution = SimulatedExecution(
            fill=fill,
            purpose=intent.purpose,
            order_type=o.order_type,
            sequence=self._exec_seq,
            evidence=evidence.kind,
            evidence_time=evidence.time,
            reference_price=touch,
            slippage=abs(price - touch),
            slippage_cost=abs(price - touch) * quantity,
            order_remaining=o.remaining if o.is_open else _ZERO,
            position_after=self.held_quantity(intent.symbol),
            fidelity_limitation=evidence.limitation,
            flags=flags,
            simulator_version=EXECUTION_SIMULATOR_VERSION,
            assumptions=self._assumption_map,
            provenance=self._provenance(intent.symbol, evidence),
            seed=self._feed.seed,
        )
        self._executions.append(execution)
        return execution

    def _provenance(self, symbol: str, evidence: _Evidence) -> dict[str, str]:
        p = self._feed.provenance
        return {
            **evidence.detail,
            "evidence": evidence.kind,
            "source": p.source,
            "feed": p.feed,
            "is_consolidated": str(p.is_consolidated),
            "revision": p.revision,
            "adjustment": p.adjustment,
            "point_in_time": str(p.point_in_time),
            "dataset_hash": str(p.dataset_hash),
            "quote_fidelity": self._feed.quote_fidelity(symbol).value,
        }

    # ---- positions, coverage, order state

    def _apply_to_position(self, o: _Order, quantity: Decimal, price: Decimal) -> None:
        symbol = o.intent.symbol
        held = self._held.get(symbol)
        if o.side.is_entry:
            if held is None:
                held = self._held[symbol] = _Held(
                    o.intent.account_scope_id, o.intent.cycle_id, _ZERO, _ZERO, o.side.direction
                )
            assert held.direction is o.side.direction
            held.quantity += quantity
            held.cost += quantity * price
            return
        assert held is not None and held.direction is o.side.direction
        assert quantity <= held.quantity
        held.cost -= held.cost / held.quantity * quantity
        held.quantity -= quantity
        if held.quantity == _ZERO:
            del self._held[symbol]

    def _sync(self, symbol: str, at: datetime) -> None:
        """Keep protective coverage within the held quantity; cancel exits once flat."""
        for o in self._open(symbol):
            if not o.side.is_exit:
                continue
            held = self._held_for(symbol, o.side.direction)
            if held <= _ZERO:
                self._cancel(o, at, "position_closed")
            elif o.prot is not None and o.remaining > held:
                o.quantity = o.filled + held
                o.prot = replace(o.prot, covered_quantity=held)

    def _apply_replacements(self, symbol: str, at: datetime) -> None:
        for o in self._open(symbol):
            due = sorted((r for r in o.pending if r.effective_at <= at),
                         key=lambda r: r.effective_at)
            if not due or o.prot is None:
                continue
            o.pending = [r for r in o.pending if r.effective_at > at]
            for r in due:
                if r.stop_price is not None:
                    extreme = (
                        max(o.prot.high_water_mark, r.stop_price)
                        if o.prot.direction is PositionDirection.LONG
                        else min(o.prot.high_water_mark, r.stop_price)
                    )
                    o.prot = o.prot.ratchet(r.stop_price, extreme)
                if r.quantity is not None:
                    remaining = min(
                        r.quantity - o.filled,
                        self._held_for(symbol, o.prot.direction),
                    )
                    if remaining > _ZERO:
                        o.quantity = o.filled + remaining
                        o.prot = replace(o.prot, covered_quantity=remaining)
                o.updated_at = r.effective_at

    def _candidate_stop(self, o: _Order, high: Decimal, low: Decimal) -> Decimal:
        """Stop a native trailing order would have after folding in the favorable extreme.

        A long trails the highest price and never moves down; a short trails the lowest
        price and never moves up.
        """
        assert o.prot is not None and o.policy is not None
        intent = o.intent
        direction = o.prot.direction
        view = _CycleView(
            intent.cycle_id, intent.account_scope_id, intent.symbol, o.remaining, direction
        )
        if direction is PositionDirection.LONG:
            reference = max(o.prot.high_water_mark, high)
            moved = start_protection(o.policy, view, reference).stop_price  # type: ignore[arg-type]
            return max(o.prot.stop_price, ALPACA_EQUITY_TICK_RULES.floor(moved))
        reference = min(o.prot.high_water_mark, low)
        moved = start_protection(o.policy, view, reference).stop_price  # type: ignore[arg-type]
        return min(o.prot.stop_price, ALPACA_EQUITY_TICK_RULES.ceil(moved))

    def _ratchet(self, symbol: str, high: Decimal, low: Decimal, at: datetime) -> None:
        for o in self._open(symbol):
            if (
                o.order_type is not OrderType.TRAILING_STOP
                or o.prot is None
                or o.eligible_at > at
                or o.remaining <= _ZERO
            ):
                continue
            self._ratchet_order(o, high, low)

    def _ratchet_order(self, order: _Order, high: Decimal, low: Decimal) -> None:
        """Apply one completed bar's favorable extreme to one native trailing order."""
        assert order.prot is not None
        stop = self._candidate_stop(order, high, low)
        extreme = (
            max(order.prot.high_water_mark, high)
            if order.prot.direction is PositionDirection.LONG
            else min(order.prot.high_water_mark, low)
        )
        order.prot = order.prot.ratchet(stop, extreme)

    def _held_for(self, symbol: str, direction: PositionDirection) -> Decimal:
        """Held quantity of ``symbol`` only when the position has the given direction."""
        held = self._held.get(symbol)
        return held.quantity if held is not None and held.direction is direction else _ZERO

    @staticmethod
    def _stop_crossed(o: _Order, low: Decimal, high: Decimal) -> bool:
        """True when the price range reaches the order's protective stop.

        A long (SELL) stop sits below the market and fires when the low reaches it; a short
        (BUY_TO_COVER) stop sits above the market and fires when the high reaches it.
        """
        assert o.prot is not None
        if o.prot.direction is PositionDirection.LONG:
            return low <= o.prot.stop_price
        return high >= o.prot.stop_price

    def _transition(
        self, o: _Order, target: OrderStatus, *, filled_qty: Decimal | None = None
    ) -> None:
        result = apply_transition(o.status, OrderEvent(target, filled_qty, broker_confirmed=True))
        result.raise_for_illegal()
        o.status = result.status

    def _cancel(self, o: _Order, at: datetime, reason: str) -> None:
        self._transition(o, OrderStatus.CANCELED)
        o.pending.clear()
        o.updated_at = at
        if o.prot is not None:
            o.prot = replace(o.prot, status=ProtectionStatus.CANCELED)
        self._note(at, o.broker_order_id, reason)

    def _expire(self, at: datetime) -> None:
        for o in list(self._orders.values()):
            if o.is_open and o.expires_at is not None and at >= o.expires_at:
                self._transition(o, OrderStatus.EXPIRED)
                o.updated_at = o.expires_at
                self._note(o.expires_at, o.broker_order_id, "day_order_expired")

    def _open(self, symbol: str) -> list[_Order]:
        return [o for o in self._orders.values() if o.is_open and o.intent.symbol == symbol]

    def _halted(self, symbol: str, start: datetime, end: datetime) -> bool:
        """True when ``[start, end)`` (or the instant ``start`` when equal to ``end``) is halted."""
        if start == end:
            return any(h_start <= start < h_end for h_start, h_end in self._halts.get(symbol, ()))
        return any(
            h_start < end and start < h_end for h_start, h_end in self._halts.get(symbol, ())
        )

    def _note(self, at: datetime, order_id: str, reason: str) -> None:
        self._diagnostics.append((at, order_id, reason))

    def _snapshot(self, o: _Order) -> BrokerOrder:
        intent = o.intent
        return BrokerOrder(
            broker_order_id=o.broker_order_id,
            client_order_id=intent.client_order_id,
            account_scope_id=intent.account_scope_id,
            symbol=intent.symbol,
            side=o.side,
            order_type=o.order_type,
            status=_BROKER_STATUS[o.status],
            quantity=o.quantity,
            filled_quantity=o.filled,
            submitted_at=o.submitted_at,
            updated_at=o.updated_at,
            filled_avg_price=o.notional / o.filled if o.filled > _ZERO else None,
            limit_price=intent.limit_price,
            stop_price=(
                o.prot.stop_price
                if o.prot is not None and o.order_type is OrderType.STOP
                else None
            ),
            trail_price=intent.trail_price,
            trail_percent=intent.trail_percent,
        )


# --------------------------------------------------------------------------- calendar


def _bar_in_regular_session(c: Candle) -> bool:
    if session_status(c.start) is not SessionStatus.REGULAR:
        return False
    return c.end <= session_for(c.start.astimezone(NEW_YORK).date()).close_utc


def _session_end(at: datetime) -> datetime:
    """Close of the session containing ``at`` (else a day later when the calendar is unknown)."""
    try:
        return session_for(at.astimezone(NEW_YORK).date()).close_utc
    except ValueError:
        return at + timedelta(days=1)


def _day_expiry(submitted_at: datetime) -> datetime | None:
    """Close of the session a DAY order submitted at ``submitted_at`` is good for."""
    day = submitted_at.astimezone(NEW_YORK).date()
    try:
        if is_trading_day(day) and submitted_at < session_for(day).close_utc:
            return session_for(day).close_utc
        return next_session(day).close_utc
    except ValueError:
        return None
