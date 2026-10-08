"""Alpaca standalone native trailing-stop adapter.

Native trailing protection is continuous and broker managed.  Alpaca's ``hwm``
and ``stop_price`` fields are authoritative; this module never derives or
ratchets either value.  Protection therefore continues while the Alpaca order
remains active, including while the application engine is offline.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from mike_trade.risk.stop_policy import (
    BrokerProtectionCapabilities,
    ProtectionMode,
    ProtectionPolicy,
)
from mike_trade.runtime import ExecutionMode

__all__ = [
    "NativeTrailingState",
    "build_trailing_stop_request",
    "submit_trailing_stop",
    "replace_trailing_stop",
    "cancel_trailing_stop",
    "parse_trailing_order_state",
]

_ZERO = Decimal("0")
_TRAILING_STOP = "TRAILING_STOP"
_LINKED_ORDER_CLASSES = frozenset({"bracket", "oto", "oco"})


@dataclass(frozen=True, slots=True)
class NativeTrailingState:
    """One broker-confirmed snapshot of continuous native trailing protection."""

    order_id: str
    client_order_id: str
    status: str
    broker_status: str
    hwm: Decimal | None
    stop_price: Decimal | None
    trail_price: Decimal | None
    trail_percent: Decimal | None
    time_in_force: str
    qty: Decimal
    filled_qty: Decimal
    protected_quantity: Decimal
    updated_at: datetime
    behavior: str = field(default="continuous_broker_managed", init=False)

    @property
    def condition(self) -> str:
        """Provider-neutral protection condition (an alias for ``status``)."""

        return self.status

    @property
    def high_water_mark(self) -> Decimal | None:
        """Readable alias for Alpaca's ``hwm`` response field."""

        return self.hwm


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _decimal(
    value: Any,
    *,
    field: str,
    positive: bool = False,
    optional: bool = False,
    unparseable_is_none: bool = False,
) -> Decimal | None:
    if value is None:
        if optional:
            return None
        raise ValueError(f"{field} is required")
    try:
        if isinstance(value, bool) or isinstance(value, float):
            raise ValueError
        if isinstance(value, Decimal):
            parsed = value
        elif isinstance(value, int):
            parsed = Decimal(value)
        elif isinstance(value, str) and value.strip():
            parsed = Decimal(value.strip())
        else:
            raise ValueError
    except (InvalidOperation, ValueError):
        if unparseable_is_none:
            return None
        raise ValueError(f"{field} must be a valid Decimal") from None
    if not parsed.is_finite() or (positive and parsed <= _ZERO):
        if unparseable_is_none:
            return None
        requirement = "a finite positive Decimal" if positive else "a finite Decimal"
        raise ValueError(f"{field} must be {requirement}")
    return parsed


def _field(raw_order: Mapping[str, Any] | object, *names: str) -> Any:
    if isinstance(raw_order, Mapping):
        for name in names:
            if name in raw_order:
                return raw_order[name]
    else:
        for name in names:
            if hasattr(raw_order, name):
                return getattr(raw_order, name)
    return None


def _utc_timestamp(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"{field} is not a valid timestamp") from None
    else:
        raise ValueError(f"{field} must be a timezone-aware timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _mode(value: ExecutionMode | str) -> ExecutionMode:
    if isinstance(value, ExecutionMode):
        return value
    raw = getattr(value, "value", value)
    if isinstance(raw, str):
        try:
            return ExecutionMode(raw.strip().upper())
        except ValueError:
            pass
    raise ValueError("execution_mode must be BACKTEST, SHADOW, PAPER, or LIVE")


def _validate_fractional(
    qty: Decimal,
    policy: ProtectionPolicy,
    capabilities: BrokerProtectionCapabilities | None,
    execution_mode: ExecutionMode | str,
) -> None:
    if qty == qty.to_integral_value():
        return
    if not isinstance(capabilities, BrokerProtectionCapabilities):
        raise ValueError(
            "fractional native trailing quantity requires injected "
            "BrokerProtectionCapabilities"
        )
    if not policy.allow_fractional_protection:
        raise ValueError("fractional native trailing protection is not enabled by policy")
    if policy.documented_fractional_risk_ack is None:
        raise ValueError("fractional native trailing protection requires a risk acknowledgement")
    combination = (_TRAILING_STOP, policy.time_in_force)
    if combination not in capabilities.fractional_combinations:
        raise ValueError("fractional trailing/TIF combination is not supported")
    if (
        _mode(execution_mode) is ExecutionMode.LIVE
        and combination not in capabilities.paper_proven_fractional_combinations
    ):
        raise ValueError("fractional trailing/TIF combination is not paper_proven for LIVE")


def build_trailing_stop_request(
    symbol: str,
    qty: Decimal,
    policy: ProtectionPolicy,
    client_order_id: str,
    *,
    capabilities: BrokerProtectionCapabilities | None = None,
    execution_mode: ExecutionMode | str = ExecutionMode.PAPER,
    side: str = "sell",
    order_class: str = "simple",
) -> dict[str, str]:
    """Build a validated standalone Alpaca native trailing-stop SELL request."""

    cleaned_symbol = _text(symbol, field="symbol").upper()
    if any(character.isspace() for character in cleaned_symbol):
        raise ValueError("symbol must not contain whitespace")
    quantity = _decimal(qty, field="qty", positive=True)
    assert isinstance(quantity, Decimal)
    if not isinstance(policy, ProtectionPolicy):
        raise TypeError("policy must be ProtectionPolicy")
    if policy.mode is not ProtectionMode.TRAILING_NATIVE:
        raise ValueError("policy mode must be TRAILING_NATIVE")

    normalized_side = _text(getattr(side, "value", side), field="side").lower()
    if normalized_side != "sell":
        raise ValueError("native trailing protection is long-only and side must be SELL")
    normalized_class = _text(
        getattr(order_class, "value", order_class), field="order_class"
    ).lower()
    if policy.linked or normalized_class in _LINKED_ORDER_CLASSES:
        raise ValueError(
            "Alpaca trailing stops must be standalone; bracket, OTO, and OCO "
            "order classes are not allowed"
        )
    if normalized_class != "simple":
        raise ValueError("Alpaca trailing stops require order_class simple")

    trail_count = int(policy.trail_price is not None) + int(
        policy.trail_percent is not None
    )
    if trail_count != 1:
        raise ValueError("exactly one of trail_price or trail_percent is required")
    _validate_fractional(quantity, policy, capabilities, execution_mode)

    request = {
        "symbol": cleaned_symbol,
        "qty": str(quantity),
        "side": "sell",
        "type": "trailing_stop",
        "time_in_force": policy.time_in_force.value.lower(),
        "order_class": "simple",
        "client_order_id": _text(client_order_id, field="client_order_id"),
    }
    if policy.trail_price is not None:
        request["trail_price"] = str(policy.trail_price)
    else:
        assert policy.trail_percent is not None
        request["trail_percent"] = str(policy.trail_percent)
    return request


def _submit(client: object, request: Mapping[str, str]) -> Any:
    post = getattr(client, "post", None)
    if callable(post):
        return post("/v2/orders", json_body=dict(request), params=None)
    submit = getattr(client, "submit_order", None)
    if callable(submit):
        return submit(dict(request))
    if callable(client):
        return client(dict(request))
    raise TypeError("client must provide post(), submit_order(), or be callable")


def submit_trailing_stop(
    client: object,
    symbol: str,
    qty: Decimal,
    policy: ProtectionPolicy,
    client_order_id: str,
    *,
    capabilities: BrokerProtectionCapabilities | None = None,
    execution_mode: ExecutionMode | str = ExecutionMode.PAPER,
    side: str = "sell",
    order_class: str = "simple",
) -> NativeTrailingState:
    """Submit through an injected Alpaca HTTP client/port and parse its response."""

    request = build_trailing_stop_request(
        symbol,
        qty,
        policy,
        client_order_id,
        capabilities=capabilities,
        execution_mode=execution_mode,
        side=side,
        order_class=order_class,
    )
    return parse_trailing_order_state(_submit(client, request))


def replace_trailing_stop(
    client: object,
    order_id: str,
    *,
    qty: Decimal | None = None,
    trail_price: Decimal | None = None,
    trail_percent: Decimal | None = None,
    client_order_id: str | None = None,
) -> NativeTrailingState:
    """Replace mutable fields through an injected client; never changes order class."""

    cleaned_order_id = _text(order_id, field="order_id")
    if trail_price is not None and trail_percent is not None:
        raise ValueError("replace at most one of trail_price or trail_percent")
    body: dict[str, str] = {}
    if qty is not None:
        parsed_qty = _decimal(qty, field="qty", positive=True)
        assert isinstance(parsed_qty, Decimal)
        body["qty"] = str(parsed_qty)
    for field, value in (("trail_price", trail_price), ("trail_percent", trail_percent)):
        if value is not None:
            parsed = _decimal(value, field=field, positive=True)
            assert isinstance(parsed, Decimal)
            body[field] = str(parsed)
    if client_order_id is not None:
        body["client_order_id"] = _text(client_order_id, field="client_order_id")
    if not body:
        raise ValueError("a replacement qty, trail value, or client_order_id is required")

    patch = getattr(client, "patch", None)
    if callable(patch):
        raw = patch(f"/v2/orders/{cleaned_order_id}", json_body=body, params=None)
    else:
        replace = getattr(client, "replace_order", None)
        if not callable(replace):
            raise TypeError("client must provide patch() or replace_order()")
        raw = replace(cleaned_order_id, body)
    return parse_trailing_order_state(raw)


def cancel_trailing_stop(client: object, order_id: str) -> Any:
    """Cancel through an injected client/port without constructing credentials."""

    cleaned_order_id = _text(order_id, field="order_id")
    delete = getattr(client, "delete", None)
    if callable(delete):
        return delete(f"/v2/orders/{cleaned_order_id}", params=None)
    cancel = getattr(client, "cancel_order", None)
    if callable(cancel):
        return cancel(cleaned_order_id)
    raise TypeError("client must provide delete() or cancel_order()")


def _condition(broker_status: str, hwm: Decimal | None, stop_price: Decimal | None) -> str:
    if broker_status == "accepted":
        return "ACTIVE" if hwm is not None and stop_price is not None else "UNCONFIRMED"
    if broker_status in {"new", "pending_new"}:
        return "PENDING"
    if broker_status in {"replaced", "pending_replace"}:
        return "UPDATING"
    if broker_status in {"filled", "partially_filled"}:
        return "TRIGGERED"
    if broker_status in {"expired", "done_for_day"}:
        return "EXPIRED"
    if broker_status in {"canceled", "cancelled", "rejected"}:
        # A coordinator may supersede this only when it has separately recorded
        # an intentional coordinated exit.
        return "UNPROTECTED"
    return "UNCONFIRMED"


def parse_trailing_order_state(raw_order: Mapping[str, Any] | object) -> NativeTrailingState:
    """Parse an authoritative Alpaca native trailing-order snapshot.

    Missing or malformed broker ``hwm``/``stop_price`` fields are retained as
    ``None``.  An otherwise active accepted order is then ``UNCONFIRMED``;
    neither value is ever fabricated from market data or a prior snapshot.
    """

    if raw_order is None:
        raise TypeError("raw_order is required")
    order_id = _text(_field(raw_order, "id", "order_id"), field="order id")
    client_order_id = _text(
        _field(raw_order, "client_order_id"), field="client_order_id"
    )
    broker_status = _text(_field(raw_order, "status"), field="status").lower()
    time_in_force = _text(
        _field(raw_order, "time_in_force"), field="time_in_force"
    ).lower()
    qty = _decimal(_field(raw_order, "qty"), field="qty", positive=True)
    filled_qty = _decimal(_field(raw_order, "filled_qty"), field="filled_qty")
    assert isinstance(qty, Decimal) and isinstance(filled_qty, Decimal)
    if filled_qty < _ZERO:
        raise ValueError("filled_qty must be >= 0")
    if filled_qty > qty:
        raise ValueError("filled_qty must not exceed qty")

    hwm = _decimal(
        _field(raw_order, "hwm"),
        field="hwm",
        positive=True,
        optional=True,
        unparseable_is_none=True,
    )
    stop_price = _decimal(
        _field(raw_order, "stop_price"),
        field="stop_price",
        positive=True,
        optional=True,
        unparseable_is_none=True,
    )
    trail_price = _decimal(
        _field(raw_order, "trail_price"), field="trail_price", positive=True, optional=True
    )
    trail_percent = _decimal(
        _field(raw_order, "trail_percent"),
        field="trail_percent",
        positive=True,
        optional=True,
    )
    if (trail_price is None) == (trail_percent is None):
        raise ValueError("broker trailing order must contain exactly one trail value")
    updated_at = _utc_timestamp(
        _field(raw_order, "updated_at"), field="updated_at"
    )
    return NativeTrailingState(
        order_id=order_id,
        client_order_id=client_order_id,
        status=_condition(broker_status, hwm, stop_price),
        broker_status=broker_status,
        hwm=hwm,
        stop_price=stop_price,
        trail_price=trail_price,
        trail_percent=trail_percent,
        time_in_force=time_in_force,
        qty=qty,
        filled_qty=filled_qty,
        protected_quantity=qty - filled_qty,
        updated_at=updated_at,
    )
