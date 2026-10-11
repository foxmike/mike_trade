"""Quote-based SHADOW order matching: estimated fills, never broker fills.

The simulator turns an open SHADOW MARKET or LIMIT order into an *estimated* fill
using contemporaneous quotes from the shared market-data snapshot
(:class:`~mike_trade.market_data.context.QuoteSnapshot`).  It never calls a broker,
never reads candles, and never invents a price: with no usable quote the answer is
``NO_FILL`` or an explicit ``UNCERTAIN`` outcome carrying a reason code.

Design choices:
1. Quote evidence only.  Buys fill against the ask and sell against the bid; a bar
   (or its close) is not accepted as input, so "the bar crossed" can never fill.
2. ``simulate`` is pure over (order, quotes, ``as_of``).  Recording is separate:
   :meth:`ShadowFillSimulator.apply_fill_to_account` hands the result to
   ``ShadowAccount.apply_fill``, which stays the authority for cash, no-negative-cash
   and no-short rules (its domain errors propagate; nothing is swallowed).
3. Quotes are walked oldest first.  A quote that cannot be used because of *when* it
   is (before latency elapsed, outside the regular NY session, DAY order expired,
   limit not crossed, our own liquidity already consumed) is skipped.  A quote that
   cannot be trusted (crossed/locked, stale at ``as_of``, missing or zero size on
   the side we need) stops the walk with ``UNCERTAIN`` rather than guessing.
4. Slippage (bps plus ticks) is never negative and always moves the price against
   the trader; the final price is rounded against the trader, and a limit order is
   capped at its limit, never better than the quote.
5. Participation: a fill is at most ``participation`` x displayed size of the side
   hit.  Liquidity that this simulator instance already handed out from one quote is
   not handed out twice.  Whole-share orders floor to whole shares; fractional
   orders are allowed only when configured, for configured order types, and DAY.
6. Every result records the simulator version, assumptions, the quote used and its
   feed provenance (for example ``iex``, not consolidated), and is labeled
   ``estimated=True`` / ``broker_confirmed=False``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any

from mike_trade.brokers.shadow.account import ShadowAccount
from mike_trade.brokers.shadow.ledger import SIMULATOR_VERSION
from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    Fill,
    OrderSide,
    OrderType,
    TimeInForce,
)
from mike_trade.market_data.context import MarketContext, QuoteSnapshot
from mike_trade.scheduling.calendar import (
    NEW_YORK,
    SessionStatus,
    is_trading_day,
    next_session,
    session_for,
    session_status,
)

__all__ = [
    "SIMULATOR_VERSION",
    "FillAssumptions",
    "FillOutcome",
    "ShadowFillSimulator",
    "SimulatedFill",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BPS = Decimal("10000")
_OPEN_STATUSES = frozenset({BrokerOrderStatus.ACCEPTED, BrokerOrderStatus.PARTIALLY_FILLED})
_SUPPORTED_TYPES = frozenset({OrderType.MARKET, OrderType.LIMIT})
_IEX = "iex"

# Reason codes (stable strings; part of the result record).
REASON_FILLED = "filled_against_quote"
REASON_PARTIAL = "partial_fill_participation_cap"
REASON_ORDER_NOT_OPEN = "order_not_open"
REASON_ORDER_TYPE_UNSUPPORTED = "order_type_unsupported"
REASON_LIMIT_PRICE_MISSING = "limit_price_missing"
REASON_FRACTIONAL_UNSUPPORTED = "fractional_unsupported"
REASON_FRACTIONAL_ORDER_TYPE = "fractional_order_type_unsupported"
REASON_FRACTIONAL_TIF = "fractional_time_in_force_unsupported"
REASON_QUOTE_MISSING = "quote_missing"
REASON_AWAITING_QUOTE = "awaiting_quote_after_latency"
REASON_OUTSIDE_SESSION = "outside_regular_session"
REASON_DAY_EXPIRED = "day_order_expired"
REASON_CALENDAR = "calendar_unavailable"
REASON_LIMIT_NOT_CROSSED = "limit_not_crossed"
REASON_QUOTE_CROSSED = "quote_crossed_or_locked"
REASON_QUOTE_STALE = "quote_stale"
REASON_SIZE_MISSING = "quote_size_unavailable"
REASON_SIZE_ZERO = "quote_size_zero"
REASON_LIQUIDITY_EXHAUSTED = "liquidity_exhausted"
REASON_BELOW_LOT = "participation_cap_below_minimum_lot"
REASON_PRICE_NON_POSITIVE = "price_non_positive_after_slippage"


class FillOutcome(Enum):
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    NO_FILL = "NO_FILL"
    UNCERTAIN = "UNCERTAIN"
    UNSUPPORTED = "UNSUPPORTED"


# --------------------------------------------------------------------------- helpers


def _decimal(
    value: object, name: str, *, minimum: Decimal = _ZERO, inclusive: bool = True
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise ValueError(f"{name} must be Decimal; got {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if value < minimum or (not inclusive and value == minimum):
        raise ValueError(f"{name} must be {'>=' if inclusive else '>'} {minimum}")
    return value


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")
    return value


def _duration(value: object, name: str) -> timedelta:
    if not isinstance(value, timedelta) or value < timedelta(0):
        raise ValueError(f"{name} must be a non-negative timedelta")
    return value


def _is_fractional(quantity: Decimal) -> bool:
    return quantity != quantity.to_integral_value()


def _consolidated(feed_id: str) -> bool:
    return feed_id != _IEX


# --------------------------------------------------------------------------- assumptions


@dataclass(frozen=True, slots=True)
class FillAssumptions:
    """Every tunable of the simulation; recorded verbatim on each result."""

    latency: timedelta = timedelta(0)
    max_quote_age: timedelta = timedelta(seconds=5)
    slippage_bps: Decimal = _ZERO
    slippage_ticks: Decimal = _ZERO
    tick_size: Decimal = Decimal("0.01")
    price_quantum: Decimal = Decimal("0.0001")
    participation: Decimal = _ONE
    quote_size_multiplier: Decimal = _ONE
    fee_per_fill: Decimal = _ZERO
    fee_per_share: Decimal = _ZERO
    fee_bps: Decimal = _ZERO
    fee_minimum: Decimal = _ZERO
    fee_quantum: Decimal = Decimal("0.01")
    allow_fractional: bool = False
    fractional_order_types: frozenset[OrderType] = frozenset(_SUPPORTED_TYPES)
    quantity_quantum: Decimal = Decimal("0.000000001")

    def __post_init__(self) -> None:
        _duration(self.latency, "latency")
        if _duration(self.max_quote_age, "max_quote_age") == timedelta(0):
            raise ValueError("max_quote_age must be positive")
        for name in (
            "slippage_bps",
            "slippage_ticks",
            "fee_per_fill",
            "fee_per_share",
            "fee_bps",
            "fee_minimum",
        ):
            _decimal(getattr(self, name), name)
        for name in (
            "tick_size",
            "price_quantum",
            "quote_size_multiplier",
            "fee_quantum",
            "quantity_quantum",
        ):
            _decimal(getattr(self, name), name, inclusive=False)
        participation = _decimal(self.participation, "participation", inclusive=False)
        if participation > _ONE:
            raise ValueError("participation must be <= 1")
        if not isinstance(self.allow_fractional, bool):
            raise TypeError("allow_fractional must be bool")
        types = frozenset(self.fractional_order_types)
        if not types <= _SUPPORTED_TYPES or not all(isinstance(t, OrderType) for t in types):
            raise ValueError("fractional_order_types may only contain MARKET and LIMIT")
        object.__setattr__(self, "fractional_order_types", types)

    def as_mapping(self) -> Mapping[str, str]:
        """String form recorded on results (and usable as ledger ``assumptions``)."""
        return MappingProxyType(
            {
                "latency_us": str(self.latency // timedelta(microseconds=1)),
                "max_quote_age_us": str(self.max_quote_age // timedelta(microseconds=1)),
                "slippage_bps": str(self.slippage_bps),
                "slippage_ticks": str(self.slippage_ticks),
                "tick_size": str(self.tick_size),
                "price_quantum": str(self.price_quantum),
                "participation": str(self.participation),
                "quote_size_multiplier": str(self.quote_size_multiplier),
                "fee_per_fill": str(self.fee_per_fill),
                "fee_per_share": str(self.fee_per_share),
                "fee_bps": str(self.fee_bps),
                "fee_minimum": str(self.fee_minimum),
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
class SimulatedFill:
    """Outcome of one matching attempt, with the evidence and assumptions behind it."""

    outcome: FillOutcome
    reason: str
    symbol: str
    side: OrderSide
    broker_order_id: str
    requested_quantity: Decimal
    simulator_version: str
    assumptions: Mapping[str, str]
    quantity: Decimal = _ZERO
    price: Decimal | None = None
    fee: Decimal = _ZERO
    slippage: Decimal = _ZERO
    filled_at: datetime | None = None
    fill_id: str | None = None
    quote_provider_ts: datetime | None = None
    quote_receive_ts: datetime | None = None
    quote_bid: Decimal | None = None
    quote_ask: Decimal | None = None
    quote_bid_size: Decimal | None = None
    quote_ask_size: Decimal | None = None
    quote_price: Decimal | None = None
    feed_id: str | None = None
    consolidated: bool | None = None
    estimated: bool = field(default=True, init=False)
    broker_confirmed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, FillOutcome):
            raise TypeError("outcome must be a FillOutcome")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("reason must be a non-empty string")
        object.__setattr__(self, "assumptions", MappingProxyType(dict(self.assumptions)))
        for name in ("requested_quantity", "quantity", "fee", "slippage"):
            _decimal(getattr(self, name), name)
        for name in ("price", "quote_bid", "quote_ask", "quote_price"):
            value = getattr(self, name)
            if value is not None:
                _decimal(value, name, inclusive=False)
        for name in ("filled_at", "quote_provider_ts", "quote_receive_ts"):
            value = getattr(self, name)
            if value is not None:
                _utc(value, name)
        if self.is_fill:
            required = (
                self.price,
                self.filled_at,
                self.fill_id,
                self.quote_provider_ts,
                self.quote_receive_ts,
                self.quote_price,
                self.feed_id,
                self.consolidated,
            )
            if self.quantity <= _ZERO or any(item is None for item in required):
                raise ValueError("a fill requires quantity, price, time and full quote evidence")
            if self.quantity > self.requested_quantity:
                raise ValueError("fill quantity exceeds the requested quantity")
        elif self.quantity != _ZERO or self.price is not None or self.fee != _ZERO:
            raise ValueError("a non-fill outcome carries no quantity, price or fee")

    @property
    def is_fill(self) -> bool:
        return self.outcome in (FillOutcome.FILLED, FillOutcome.PARTIALLY_FILLED)

    @property
    def remaining_quantity(self) -> Decimal:
        return self.requested_quantity - self.quantity

    @property
    def feed_label(self) -> str | None:
        """Provenance label for the ledger, e.g. ``iex:not_consolidated``."""
        if self.feed_id is None:
            return None
        return f"{self.feed_id}:{'consolidated' if self.consolidated else 'not_consolidated'}"

    def to_fill(self, account_scope_id: str) -> Fill:
        """The domain :class:`Fill` for a fill outcome (still an estimate)."""
        if not self.is_fill:
            raise ValueError(f"{self.outcome.value} outcome has no fill")
        assert self.price is not None and self.filled_at is not None and self.fill_id is not None
        return Fill(
            fill_id=self.fill_id,
            broker_order_id=self.broker_order_id,
            account_scope_id=account_scope_id,
            symbol=self.symbol,
            side=self.side,
            quantity=self.quantity,
            price=self.price,
            fee=self.fee,
            filled_at=self.filled_at,
        )


# --------------------------------------------------------------------------- simulator


class ShadowFillSimulator:
    """Match open SHADOW orders against quotes; never contacts a broker."""

    def __init__(self, assumptions: FillAssumptions | None = None) -> None:
        if assumptions is None:
            assumptions = FillAssumptions()
        if not isinstance(assumptions, FillAssumptions):
            raise TypeError("assumptions must be FillAssumptions")
        self._assumptions = assumptions
        self._consumed: dict[tuple[str, OrderSide, datetime, datetime, Decimal], Decimal] = {}

    @property
    def assumptions(self) -> FillAssumptions:
        return self._assumptions

    @property
    def version(self) -> str:
        return SIMULATOR_VERSION

    # ---- public API

    def simulate(
        self,
        order: BrokerOrder,
        quotes: Iterable[QuoteSnapshot] | MarketContext,
        *,
        as_of: datetime,
        time_in_force: TimeInForce = TimeInForce.DAY,
        existing_fill_ids: Collection[str] = (),
    ) -> SimulatedFill:
        """Estimate the next fill of ``order`` from quotes known at ``as_of``.

        Pure: nothing is recorded or consumed.  ``existing_fill_ids`` lists fills
        already recorded so a quote that already filled this order is not reused.
        """
        if not isinstance(order, BrokerOrder):
            raise TypeError("order must be a BrokerOrder")
        _utc(as_of, "as_of")
        _utc(order.submitted_at, "order.submitted_at")
        remaining = order.quantity - order.filled_quantity

        def result(
            outcome: FillOutcome, reason: str, quote: QuoteSnapshot | None = None
        ) -> SimulatedFill:
            return self._none(order, remaining, outcome, reason, quote)

        if order.status not in _OPEN_STATUSES or remaining <= _ZERO:
            return result(FillOutcome.NO_FILL, REASON_ORDER_NOT_OPEN)
        unsupported = self._unsupported_reason(order, time_in_force)
        if unsupported is not None:
            return result(FillOutcome.UNSUPPORTED, unsupported)

        candidates = self._candidates(order, quotes)
        if not candidates:
            return result(FillOutcome.UNCERTAIN, REASON_QUOTE_MISSING)
        a = self._assumptions
        earliest = order.submitted_at + a.latency
        known = [q for q in candidates if q.provider_ts <= as_of and q.receive_ts <= as_of]
        eligible = [q for q in known if q.provider_ts >= earliest]
        if not eligible:
            return result(FillOutcome.NO_FILL, REASON_AWAITING_QUOTE)

        fallback = REASON_AWAITING_QUOTE
        for quote in eligible:
            if self._fill_id(order, quote) in existing_fill_ids:
                continue
            skip = self._timing_skip(order, quote, time_in_force)
            if skip is not None:
                fallback = skip
                continue
            if quote.bid >= quote.ask:
                return result(FillOutcome.UNCERTAIN, REASON_QUOTE_CROSSED, quote)
            buy = order.side.broker_side == "buy"
            touch = quote.ask if buy else quote.bid
            if order.order_type is OrderType.LIMIT:
                assert order.limit_price is not None
                crossed = touch <= order.limit_price if buy else touch >= order.limit_price
                if not crossed:
                    fallback = REASON_LIMIT_NOT_CROSSED
                    continue
            if as_of - quote.provider_ts > a.max_quote_age:
                return result(FillOutcome.UNCERTAIN, REASON_QUOTE_STALE, quote)
            size = quote.ask_size if buy else quote.bid_size
            if size is None:
                return result(FillOutcome.UNCERTAIN, REASON_SIZE_MISSING, quote)
            if size <= _ZERO:
                return result(FillOutcome.UNCERTAIN, REASON_SIZE_ZERO, quote)
            available = size * a.quote_size_multiplier * a.participation - self._consumed.get(
                self._consumption_key(order, quote), _ZERO
            )
            if available <= _ZERO:
                fallback = REASON_LIQUIDITY_EXHAUSTED
                continue
            quantity = self._quantize_quantity(min(remaining, available), order.quantity)
            if quantity <= _ZERO:
                fallback = REASON_BELOW_LOT
                continue
            price = self._price(order, touch)
            if price is None:
                return result(FillOutcome.UNCERTAIN, REASON_PRICE_NON_POSITIVE, quote)
            partial = quantity < remaining
            return SimulatedFill(
                outcome=FillOutcome.PARTIALLY_FILLED if partial else FillOutcome.FILLED,
                reason=REASON_PARTIAL if partial else REASON_FILLED,
                symbol=order.symbol,
                side=order.side,
                broker_order_id=order.broker_order_id,
                requested_quantity=remaining,
                simulator_version=SIMULATOR_VERSION,
                assumptions=a.as_mapping(),
                quantity=quantity,
                price=price,
                fee=self._fee(quantity, price),
                slippage=abs(price - touch),
                filled_at=quote.provider_ts,
                fill_id=self._fill_id(order, quote),
                **self._evidence(quote, touch),
            )
        return result(FillOutcome.NO_FILL, fallback)

    def apply_fill_to_account(
        self,
        account: ShadowAccount,
        broker_order_id: str,
        quotes: Iterable[QuoteSnapshot] | MarketContext,
        *,
        as_of: datetime,
    ) -> SimulatedFill:
        """Simulate the next fill of an open account order and record it on the account.

        ``ShadowAccount`` keeps authority over cash, no-negative-cash, no-short and
        overfill checks; its domain errors propagate and nothing is consumed then.
        """
        if not isinstance(account, ShadowAccount):
            raise TypeError("account must be a ShadowAccount")
        state = account.state
        stored = state.orders.get(broker_order_id)
        if stored is None:
            raise ValueError(f"unknown order {broker_order_id!r}")
        scope_id = account.scope.scope_id
        order = stored.to_broker_order(scope_id)
        fractional = _is_fractional(order.quantity)
        if not account.capabilities().supports_order(order.order_type.value.lower(), fractional):
            return self._none(
                order,
                order.quantity - order.filled_quantity,
                FillOutcome.UNSUPPORTED,
                REASON_FRACTIONAL_UNSUPPORTED if fractional else REASON_ORDER_TYPE_UNSUPPORTED,
            )
        simulated = self.simulate(
            order,
            quotes,
            as_of=as_of,
            time_in_force=stored.time_in_force,
            existing_fill_ids=frozenset(state.fills),
        )
        if not simulated.is_fill:
            return simulated
        account.apply_fill(
            simulated.to_fill(scope_id), slippage=simulated.slippage, feed=simulated.feed_label
        )
        key = self._consumption_key_for(simulated)
        self._consumed[key] = self._consumed.get(key, _ZERO) + simulated.quantity
        return simulated

    # ---- evaluation steps

    def _unsupported_reason(self, order: BrokerOrder, time_in_force: TimeInForce) -> str | None:
        if order.order_type not in _SUPPORTED_TYPES:
            return REASON_ORDER_TYPE_UNSUPPORTED
        if order.order_type is OrderType.LIMIT and (
            order.limit_price is None or order.limit_price <= _ZERO
        ):
            return REASON_LIMIT_PRICE_MISSING
        if _is_fractional(order.quantity):
            if not self._assumptions.allow_fractional:
                return REASON_FRACTIONAL_UNSUPPORTED
            if order.order_type not in self._assumptions.fractional_order_types:
                return REASON_FRACTIONAL_ORDER_TYPE
            if time_in_force is not TimeInForce.DAY:
                return REASON_FRACTIONAL_TIF
        return None

    @staticmethod
    def _candidates(
        order: BrokerOrder, quotes: Iterable[QuoteSnapshot] | MarketContext
    ) -> list[QuoteSnapshot]:
        if isinstance(quotes, MarketContext):
            items: Iterable[object] = () if quotes.latest_quote is None else (quotes.latest_quote,)
        else:
            items = quotes
        selected: list[QuoteSnapshot] = []
        for item in items:
            if not isinstance(item, QuoteSnapshot):
                raise TypeError("quotes must be QuoteSnapshot values (bars are not fill evidence)")
            if item.symbol == order.symbol:
                selected.append(item)
        return sorted(selected, key=lambda q: (q.provider_ts, q.receive_ts))

    @staticmethod
    def _timing_skip(
        order: BrokerOrder, quote: QuoteSnapshot, time_in_force: TimeInForce
    ) -> str | None:
        """Reason a quote is not usable *because of when it is*, else ``None``."""
        status = session_status(quote.provider_ts)
        if status is SessionStatus.UNKNOWN:
            return REASON_CALENDAR
        if status is not SessionStatus.REGULAR:
            return REASON_OUTSIDE_SESSION
        if time_in_force is TimeInForce.DAY:
            try:
                good_for = _day_session_date(order.submitted_at)
            except ValueError:
                return REASON_CALENDAR
            if quote.provider_ts.astimezone(NEW_YORK).date() != good_for:
                return REASON_DAY_EXPIRED
        return None

    def _price(self, order: BrokerOrder, touch: Decimal) -> Decimal | None:
        """Quote plus adverse slippage, rounded against the trader, capped at the limit."""
        a = self._assumptions
        slip = touch * a.slippage_bps / _BPS + a.slippage_ticks * a.tick_size
        if order.side.broker_side == "buy":
            price = (touch + slip).quantize(a.price_quantum, rounding=ROUND_CEILING)
            if order.order_type is OrderType.LIMIT:
                assert order.limit_price is not None
                price = min(price, order.limit_price)
            return max(price, touch)
        price = (touch - slip).quantize(a.price_quantum, rounding=ROUND_FLOOR)
        if order.order_type is OrderType.LIMIT:
            assert order.limit_price is not None
            price = max(price, order.limit_price)
        price = min(price, touch)
        return price if price > _ZERO else None

    def _fee(self, quantity: Decimal, price: Decimal) -> Decimal:
        a = self._assumptions
        fee = a.fee_per_fill + a.fee_per_share * quantity + quantity * price * a.fee_bps / _BPS
        return max(fee, a.fee_minimum).quantize(a.fee_quantum, rounding=ROUND_CEILING)

    def _quantize_quantity(self, quantity: Decimal, order_quantity: Decimal) -> Decimal:
        """Whole orders fill whole shares; fractional orders fill to the configured lot."""
        if not _is_fractional(order_quantity):
            return quantity.to_integral_value(rounding=ROUND_FLOOR)
        return quantity.quantize(self._assumptions.quantity_quantum, rounding=ROUND_DOWN)

    # ---- identity and records

    @staticmethod
    def _fill_id(order: BrokerOrder, quote: QuoteSnapshot) -> str:
        return (
            f"shadow-fill:{order.broker_order_id}:"
            f"{quote.provider_ts.isoformat()}:{quote.receive_ts.isoformat()}"
        )

    @staticmethod
    def _consumption_key(
        order: BrokerOrder, quote: QuoteSnapshot
    ) -> tuple[str, OrderSide, datetime, datetime, Decimal]:
        touch = quote.ask if order.side.broker_side == "buy" else quote.bid
        return (quote.symbol, order.side, quote.provider_ts, quote.receive_ts, touch)

    @staticmethod
    def _consumption_key_for(
        fill: SimulatedFill,
    ) -> tuple[str, OrderSide, datetime, datetime, Decimal]:
        assert fill.quote_provider_ts and fill.quote_receive_ts and fill.quote_price
        return (
            fill.symbol,
            fill.side,
            fill.quote_provider_ts,
            fill.quote_receive_ts,
            fill.quote_price,
        )

    @staticmethod
    def _evidence(quote: QuoteSnapshot, touch: Decimal) -> dict[str, Any]:
        return {
            "quote_provider_ts": quote.provider_ts,
            "quote_receive_ts": quote.receive_ts,
            "quote_bid": quote.bid,
            "quote_ask": quote.ask,
            "quote_bid_size": quote.bid_size,
            "quote_ask_size": quote.ask_size,
            "quote_price": touch,
            "feed_id": quote.feed_id,
            "consolidated": _consolidated(quote.feed_id),
        }

    def _none(
        self,
        order: BrokerOrder,
        remaining: Decimal,
        outcome: FillOutcome,
        reason: str,
        quote: QuoteSnapshot | None = None,
    ) -> SimulatedFill:
        evidence: dict[str, Any] = {}
        if quote is not None:
            touch = quote.ask if order.side.broker_side == "buy" else quote.bid
            evidence = self._evidence(quote, touch)
        return SimulatedFill(
            outcome=outcome,
            reason=reason,
            symbol=order.symbol,
            side=order.side,
            broker_order_id=order.broker_order_id,
            requested_quantity=max(remaining, _ZERO),
            simulator_version=SIMULATOR_VERSION,
            assumptions=self._assumptions.as_mapping(),
            **evidence,
        )


def _day_session_date(submitted_at: datetime) -> date:
    """NY session date a DAY order placed at ``submitted_at`` is good for."""
    local_day = submitted_at.astimezone(NEW_YORK).date()
    if is_trading_day(local_day) and submitted_at < session_for(local_day).close_utc:
        return local_day
    return next_session(local_day).session_date
