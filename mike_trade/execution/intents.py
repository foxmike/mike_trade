"""Durable provider-neutral order intents above the broker port."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

from mike_trade.domain.accounts import AccountEnvironment
from mike_trade.domain.trading import OrderSide, OrderType, TimeInForce
from mike_trade.persistence.journal import DomainEventJournal, NewEvent, RecordedEvent

from .order_fsm import OrderStatus

__all__ = ["OrderIntent", "OrderPurpose", "IntentStore", "persist_intent"]

_ZERO = Decimal("0")
_INTENT_EVENT = "OrderIntentPersisted"
_IDEMPOTENCY_PREFIX = "order-intent:"


class OrderPurpose(Enum):
    """Enabled long-only reasons for placing an order.

    Short-entry and short-cover purposes are intentionally absent.  Adding enum
    names in the future does not enable those capabilities; validation and the
    broker capability boundary must opt in separately.
    """

    ENTRY_LONG = "ENTRY_LONG"
    EXIT_LONG = "EXIT_LONG"
    PROTECTIVE_STOP = "PROTECTIVE_STOP"
    TRAILING_STOP = "TRAILING_STOP"
    MANUAL_CLOSE = "MANUAL_CLOSE"
    SAFETY_EXIT = "SAFETY_EXIT"


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a UTC-aware datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must use UTC")
    return value.astimezone(UTC)


def _positive_decimal(value: object, field: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal):
        raise TypeError(f"{field} must be Decimal")
    if not value.is_finite() or value <= _ZERO:
        raise ValueError(f"{field} must be a finite positive Decimal")
    return value


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """Immutable instruction whose durable identity precedes broker submission."""

    intent_id: str
    execution_mode: AccountEnvironment
    account_key: str
    symbol: str
    side: OrderSide
    purpose: OrderPurpose
    position_cycle_id: str
    order_type: OrderType
    time_in_force: TimeInForce
    signal_bar_time: datetime
    signal_expires_at: datetime
    quote_reference_time: datetime
    client_order_id: str
    attempt_number: int
    created_at: datetime
    originating_strategy_id: str | None = None
    qty: Decimal | None = None
    notional: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    trail_price: Decimal | None = None
    trail_percent: Decimal | None = None

    def __post_init__(self) -> None:
        for field in ("intent_id", "account_key", "position_cycle_id", "client_order_id"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        symbol = _text(self.symbol, "symbol").upper()
        if any(character.isspace() for character in symbol):
            raise ValueError("symbol must not contain whitespace")
        object.__setattr__(self, "symbol", symbol)

        if not isinstance(self.execution_mode, AccountEnvironment):
            raise TypeError("execution_mode must be an AccountEnvironment")
        if self.execution_mode not in {
            AccountEnvironment.SHADOW,
            AccountEnvironment.PAPER,
            AccountEnvironment.LIVE,
        }:
            raise ValueError("execution_mode must be SHADOW, PAPER, or LIVE")
        if not isinstance(self.side, OrderSide):
            raise TypeError("side must be an OrderSide")
        if not isinstance(self.purpose, OrderPurpose):
            raise TypeError("purpose must be an OrderPurpose")
        if not isinstance(self.order_type, OrderType):
            raise TypeError("order_type must be an OrderType")
        if not isinstance(self.time_in_force, TimeInForce):
            raise TypeError("time_in_force must be a TimeInForce")
        if isinstance(self.attempt_number, bool) or not isinstance(self.attempt_number, int):
            raise TypeError("attempt_number must be int")
        if self.attempt_number < 0:
            raise ValueError("attempt_number must be >= 0")

        for field in (
            "signal_bar_time",
            "signal_expires_at",
            "quote_reference_time",
            "created_at",
        ):
            object.__setattr__(self, field, _utc(getattr(self, field), field))
        if self.signal_expires_at < self.signal_bar_time:
            raise ValueError("signal_expires_at must not precede signal_bar_time")

        if self.originating_strategy_id is not None:
            object.__setattr__(
                self,
                "originating_strategy_id",
                _text(self.originating_strategy_id, "originating_strategy_id"),
            )

        for field in (
            "qty",
            "notional",
            "limit_price",
            "stop_price",
            "trail_price",
            "trail_percent",
        ):
            object.__setattr__(self, field, _positive_decimal(getattr(self, field), field))
        if (self.qty is None) == (self.notional is None):
            raise ValueError("exactly one of qty or notional is required")
        self._validate_prices()

        required_side = OrderSide.BUY if self.purpose is OrderPurpose.ENTRY_LONG else OrderSide.SELL
        if self.side is not required_side:
            if self.purpose is OrderPurpose.ENTRY_LONG:
                raise ValueError("short-opening sides are disabled; ENTRY_LONG requires BUY")
            raise ValueError(f"{self.purpose.value} is a long exit and requires SELL")

    @property
    def account_scope_id(self) -> str:
        """Environment-qualified persistence identity."""
        return f"{self.execution_mode.value.lower()}:{self.account_key}"

    @classmethod
    def create(cls, **values: Any) -> OrderIntent:
        """Build an intent using the existing stable client-order-ID helper.

        Callers may still invoke the normal constructor when a provider-neutral
        externally allocated ID is already durable.
        """
        if "client_order_id" in values:
            raise TypeError("create() derives client_order_id; do not pass it")
        try:
            mode = values["execution_mode"]
            account_key = values["account_key"]
            cycle_id = values["position_cycle_id"]
            purpose = values["purpose"]
            attempt = values["attempt_number"]
        except KeyError as exc:
            raise TypeError(f"create() missing required argument: {exc.args[0]}") from None
        if not isinstance(purpose, OrderPurpose):
            raise TypeError("purpose must be an OrderPurpose")
        # This is intentionally lazy: the intent model and durable store stay
        # usable without importing a concrete broker adapter.
        from mike_trade.brokers.alpaca.orders import OrderPurpose as AlpacaOrderPurpose
        from mike_trade.brokers.alpaca.orders import make_client_order_id

        alpaca_purpose = {
            OrderPurpose.ENTRY_LONG: AlpacaOrderPurpose.ENTRY,
            OrderPurpose.EXIT_LONG: AlpacaOrderPurpose.EXIT,
            OrderPurpose.PROTECTIVE_STOP: AlpacaOrderPurpose.PROTECTIVE_STOP,
            OrderPurpose.TRAILING_STOP: AlpacaOrderPurpose.TRAILING_STOP,
            OrderPurpose.MANUAL_CLOSE: AlpacaOrderPurpose.MANUAL_CLOSE,
            OrderPurpose.SAFETY_EXIT: AlpacaOrderPurpose.SAFETY_EXIT,
        }[purpose]
        values["client_order_id"] = make_client_order_id(
            mode, account_key, cycle_id, alpaca_purpose, attempt
        )
        return cls(**values)

    def _validate_prices(self) -> None:
        if self.order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT}:
            if self.limit_price is None:
                raise ValueError(f"{self.order_type.value} requires limit_price")
        elif self.limit_price is not None:
            raise ValueError(f"{self.order_type.value} must not specify limit_price")

        if self.order_type in {OrderType.STOP, OrderType.STOP_LIMIT}:
            if self.stop_price is None:
                raise ValueError(f"{self.order_type.value} requires stop_price")
        elif self.stop_price is not None:
            raise ValueError(f"{self.order_type.value} must not specify stop_price")

        trail_count = int(self.trail_price is not None) + int(self.trail_percent is not None)
        if self.order_type is OrderType.TRAILING_STOP:
            if trail_count != 1:
                raise ValueError("TRAILING_STOP requires exactly one trail value")
        elif trail_count:
            raise ValueError(f"{self.order_type.value} must not specify a trail value")


def _payload(intent: OrderIntent) -> dict[str, Any]:
    return {
        "status": OrderStatus.INTENT_PERSISTED,
        "trade_affecting": True,
        "intent_id": intent.intent_id,
        "execution_mode": intent.execution_mode,
        "account_key": intent.account_key,
        "symbol": intent.symbol,
        "side": intent.side,
        "purpose": intent.purpose,
        "position_cycle_id": intent.position_cycle_id,
        "originating_strategy_id": intent.originating_strategy_id,
        "qty": intent.qty,
        "notional": intent.notional,
        "order_type": intent.order_type,
        "limit_price": intent.limit_price,
        "stop_price": intent.stop_price,
        "trail_price": intent.trail_price,
        "trail_percent": intent.trail_percent,
        "time_in_force": intent.time_in_force,
        "signal_bar_time": intent.signal_bar_time,
        "signal_expires_at": intent.signal_expires_at,
        "quote_reference_time": intent.quote_reference_time,
        "client_order_id": intent.client_order_id,
        "attempt_number": intent.attempt_number,
        "created_at": intent.created_at,
    }


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"persisted {field} must be an ISO timestamp")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    return _utc(parsed, field)


def _decimal_from_payload(payload: Mapping[str, Any], field: str) -> Decimal | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"persisted {field} must be a decimal string")
    return Decimal(value)


def _from_recorded(event: RecordedEvent) -> OrderIntent:
    if event.event_type != _INTENT_EVENT:
        raise ValueError("client_order_id belongs to a non-intent journal event")
    payload = event.payload
    if (
        not isinstance(payload, Mapping)
        or payload.get("status") != OrderStatus.INTENT_PERSISTED.value
    ):
        raise ValueError("persisted order intent payload has an invalid initial status")
    return OrderIntent(
        intent_id=payload["intent_id"],
        execution_mode=AccountEnvironment(payload["execution_mode"]),
        account_key=payload["account_key"],
        symbol=payload["symbol"],
        side=OrderSide(payload["side"]),
        purpose=OrderPurpose(payload["purpose"]),
        position_cycle_id=payload["position_cycle_id"],
        originating_strategy_id=payload.get("originating_strategy_id"),
        qty=_decimal_from_payload(payload, "qty"),
        notional=_decimal_from_payload(payload, "notional"),
        order_type=OrderType(payload["order_type"]),
        limit_price=_decimal_from_payload(payload, "limit_price"),
        stop_price=_decimal_from_payload(payload, "stop_price"),
        trail_price=_decimal_from_payload(payload, "trail_price"),
        trail_percent=_decimal_from_payload(payload, "trail_percent"),
        time_in_force=TimeInForce(payload["time_in_force"]),
        signal_bar_time=_parse_time(payload["signal_bar_time"], "signal_bar_time"),
        signal_expires_at=_parse_time(payload["signal_expires_at"], "signal_expires_at"),
        quote_reference_time=_parse_time(payload["quote_reference_time"], "quote_reference_time"),
        client_order_id=payload["client_order_id"],
        attempt_number=payload["attempt_number"],
        created_at=_parse_time(payload["created_at"], "created_at"),
    )


class IntentStore:
    """Append-only intent persistence with scoped client-ID idempotency."""

    def __init__(self, journal: DomainEventJournal) -> None:
        if not isinstance(journal, DomainEventJournal):
            raise TypeError("journal must be a DomainEventJournal")
        self._journal = journal

    def persist_intent(
        self,
        intent: OrderIntent,
        *,
        submission_hook: Callable[[OrderIntent], Any] | None = None,
    ) -> OrderIntent:
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be an OrderIntent")
        key = f"{_IDEMPOTENCY_PREFIX}{intent.client_order_id}"
        existing = self._journal.get_by_idempotency_key(intent.account_scope_id, key)
        if existing is not None:
            return _from_recorded(existing)

        result = self._journal.append(
            [
                NewEvent(
                    account_scope_id=intent.account_scope_id,
                    stream_type="order_intent",
                    stream_id=intent.intent_id,
                    event_type=_INTENT_EVENT,
                    payload=_payload(intent),
                    occurred_at=intent.created_at,
                    idempotency_key=key,
                )
            ]
        )
        persisted = _from_recorded(result.recorded_events[0])
        if submission_hook is not None:
            submission_hook(persisted)
        return persisted


def persist_intent(
    store: IntentStore,
    intent: OrderIntent,
    *,
    submission_hook: Callable[[OrderIntent], Any] | None = None,
) -> OrderIntent:
    """Persist an intent before invoking the optional submission boundary."""
    if not isinstance(store, IntentStore):
        raise TypeError("store must be an IntentStore")
    return store.persist_intent(intent, submission_hook=submission_hook)
