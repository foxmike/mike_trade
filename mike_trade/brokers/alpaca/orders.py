"""Alpaca equity order submission, lookup, replacement, and cancellation.

Mutation requests are deliberately single-shot.  A submission intent is handed
to the injected persistence hook before POST, and an unknown POST outcome is
resolved by ``client_order_id`` lookup without issuing a second POST.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any

import httpx

from mike_trade.brokers.alpaca.assets import (
    AlpacaAsset,
    quantize_price,
    quantize_quantity,
)
from mike_trade.brokers.alpaca.auth import (
    AlpacaEnvironment,
    LiveAuthorizationRequired,
)
from mike_trade.brokers.alpaca.http import (
    AlpacaHttpClient,
    AlpacaNotFound,
    AlpacaTimeout,
    AlpacaTransportError,
    AlpacaUnknownOutcome,
    AlpacaUnprocessable,
)
from mike_trade.domain.errors import (
    BrokerError,
    BrokerRejectedError,
    UnsupportedOrderCapabilityError,
)

__all__ = [
    "AlpacaOrderService",
    "AlpacaOrder",
    "OrderPurpose",
    "make_client_order_id",
    "parse_client_order_id",
    "OrderSizing",
    "NotSubmitted",
    "OrderOutcomeUnresolved",
    "CapabilityUnavailable",
    "OversellRejected",
    "CancelResult",
]

_CLIENT_ORDER_ID_RE = re.compile(
    r"^mt1-(?P<purpose>[a-z]{1,3})-(?P<attempt>[0-9a-z]+)-(?P<digest>[0-9a-f]{32})$"
)
_TERMINAL_STATUSES = frozenset(
    {"filled", "canceled", "cancelled", "expired", "rejected", "replaced", "done_for_day"}
)
_UNKNOWN_OUTCOME_ERRORS = (
    AlpacaUnknownOutcome,
    AlpacaTimeout,
    AlpacaTransportError,
    httpx.TimeoutException,
    httpx.TransportError,
    TimeoutError,
    ConnectionError,
)


class OrderPurpose(Enum):
    """Stable lineage purposes encoded in an Alpaca ``client_order_id``."""

    ENTRY = "entry"
    EXIT = "exit"
    ENTRY_SHORT = "entry_short"
    EXIT_SHORT = "exit_short"
    PROTECTIVE_STOP = "protective_stop"
    TRAILING_STOP = "trailing_stop"
    MANUAL_CLOSE = "manual_close"
    SAFETY_EXIT = "safety_exit"
    EOD_CLOSE = "eod_close"


_PURPOSE_CODES = {
    OrderPurpose.ENTRY: "en",
    OrderPurpose.EXIT: "ex",
    OrderPurpose.ENTRY_SHORT: "es",
    OrderPurpose.EXIT_SHORT: "xs",
    OrderPurpose.PROTECTIVE_STOP: "ps",
    OrderPurpose.TRAILING_STOP: "ts",
    OrderPurpose.MANUAL_CLOSE: "mc",
    OrderPurpose.SAFETY_EXIT: "se",
    OrderPurpose.EOD_CLOSE: "ec",
}
_CODE_PURPOSES = {code: purpose for purpose, code in _PURPOSE_CODES.items()}


class OrderOutcomeUnresolved(BrokerError):
    """A submission may exist, but lookup could not establish its outcome."""

    def __init__(self, client_order_id: str, lookup_error: BaseException) -> None:
        self.client_order_id = client_order_id
        self.lookup_error = lookup_error
        super().__init__(
            f"order outcome remains unresolved for client_order_id {client_order_id!r}",
            code="order_outcome_unresolved",
            context={"client_order_id": client_order_id},
        )


class CapabilityUnavailable(UnsupportedOrderCapabilityError):
    """A mutation was refused because its exact capability is not proven."""

    def __init__(self, capability: str, message: str | None = None) -> None:
        self.capability = capability
        super().__init__(
            message or f"Alpaca capability {capability!r} is not proven",
            code="capability_unavailable",
            context={"capability": capability},
        )


class OversellRejected(BrokerRejectedError):
    """A sell quantity exceeds the caller's authoritative position quantity."""

    def __init__(self, qty: Decimal, known_position_qty: Decimal) -> None:
        self.qty = qty
        self.known_position_qty = known_position_qty
        super().__init__(
            f"sell quantity {qty} exceeds known position quantity {known_position_qty}",
            code="oversell_rejected",
            context={"qty": str(qty), "known_position_qty": str(known_position_qty)},
        )


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _decimal(value: Any, *, field: str, optional: bool = False) -> Decimal | None:
    if value is None:
        if optional:
            return None
        raise ValueError(f"{field} is required")
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field} must be Decimal, int, or str")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation:
            raise ValueError(f"{field} is not a valid decimal") from None
    else:
        raise ValueError(f"{field} must be Decimal, int, or str")
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _positive_decimal(value: Any, *, field: str) -> Decimal:
    result = _decimal(value, field=field)
    assert result is not None
    if result <= 0:
        raise ValueError(f"{field} must be > 0")
    return result


def _base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    encoded = ""
    while value:
        value, remainder = divmod(value, 36)
        encoded = alphabet[remainder] + encoded
    return encoded


def _purpose(value: OrderPurpose | str | Enum) -> OrderPurpose:
    if isinstance(value, OrderPurpose):
        return value
    raw = getattr(value, "value", value)
    if not isinstance(raw, str):
        raise ValueError("purpose must be an OrderPurpose or string")
    normalized = raw.strip().lower()
    aliases = {"strategy_exit": "exit"}
    normalized = aliases.get(normalized, normalized)
    try:
        return OrderPurpose(normalized)
    except ValueError:
        raise ValueError(f"unsupported order purpose: {raw!r}") from None


def make_client_order_id(
    mode: AlpacaEnvironment | str | Enum,
    account_fingerprint: str,
    cycle_id: str,
    purpose: OrderPurpose | str | Enum,
    attempt: int,
) -> str:
    """Return a deterministic, audit-inspectable ID no longer than 48 chars.

    The 128-bit truncated SHA-256 digest covers length-prefixed canonical input,
    while purpose and base-36 attempt remain directly inspectable.
    """

    raw_mode = getattr(mode, "value", mode)
    if not isinstance(raw_mode, str) or not raw_mode.strip():
        raise ValueError("mode must be a non-empty string or enum")
    fingerprint = _require_text(account_fingerprint, field="account_fingerprint")
    cycle = _require_text(cycle_id, field="cycle_id")
    resolved_purpose = _purpose(purpose)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise ValueError("attempt must be an int >= 0")
    components = (
        raw_mode.strip().upper(),
        fingerprint,
        cycle,
        resolved_purpose.value,
        str(attempt),
    )
    canonical = "".join(f"{len(item)}:{item}" for item in components)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    result = f"mt1-{_PURPOSE_CODES[resolved_purpose]}-{_base36(attempt)}-{digest}"
    if len(result) > 48:  # defensive: current format is at most 47 for bounded ints
        raise ValueError("attempt is too large for Alpaca's client_order_id limit")
    return result


def parse_client_order_id(client_order_id: str) -> dict[str, Any]:
    """Inspect an ID generated by :func:`make_client_order_id`.

    Hashed identity inputs cannot be reversed; the result exposes the format
    version, purpose, attempt, and digest for audit and comparison.
    """

    value = _require_text(client_order_id, field="client_order_id")
    match = _CLIENT_ORDER_ID_RE.fullmatch(value)
    if match is None or match.group("purpose") not in _CODE_PURPOSES:
        raise ValueError("not a mike_trade Alpaca client_order_id")
    return {
        "version": 1,
        "purpose": _CODE_PURPOSES[match.group("purpose")],
        "attempt": int(match.group("attempt"), 36),
        "digest": match.group("digest"),
        "client_order_id": value,
    }


@dataclass(frozen=True, slots=True)
class OrderSizing:
    """Explicit and mutually exclusive quantity-versus-notional selection."""

    qty: Decimal | int | str | None = None
    notional: Decimal | int | str | None = None

    def __post_init__(self) -> None:
        if (self.qty is None) == (self.notional is None):
            raise ValueError("exactly one of qty or notional is required")
        if self.qty is not None:
            object.__setattr__(self, "qty", _positive_decimal(self.qty, field="qty"))
        if self.notional is not None:
            object.__setattr__(
                self, "notional", _positive_decimal(self.notional, field="notional")
            )


@dataclass(frozen=True, slots=True)
class AlpacaOrder:
    """Normalized Alpaca order snapshot with lossless Decimal values."""

    environment: AlpacaEnvironment
    order_id: str
    client_order_id: str
    symbol: str
    status: str
    side: str
    type: str
    time_in_force: str
    order_class: str | None
    qty: Decimal | None
    notional: Decimal | None
    filled_qty: Decimal | None
    filled_avg_price: Decimal | None
    limit_price: Decimal | None
    stop_price: Decimal | None
    trail_price: Decimal | None
    trail_percent: Decimal | None
    hwm: Decimal | None
    legs: tuple[AlpacaOrder, ...]
    created_at: datetime | None
    updated_at: datetime | None
    submitted_at: datetime | None
    filled_at: datetime | None
    expired_at: datetime | None
    canceled_at: datetime | None
    failed_at: datetime | None
    replaced_at: datetime | None

    @property
    def id(self) -> str:
        return self.order_id

    @property
    def source_environment(self) -> AlpacaEnvironment:
        return self.environment

    @property
    def is_terminal(self) -> bool:
        return self.status.lower() in _TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class NotSubmitted:
    """Confirmed absence after an unknown POST outcome.

    Retrying the same logical attempt must reuse ``client_order_id``.
    """

    client_order_id: str
    retry_with_same_client_order_id: bool = True
    submitted: bool = False


@dataclass(frozen=True, slots=True)
class CancelResult:
    """Typed cancellation result, including idempotent terminal outcomes."""

    order_id: str
    cancel_requested: bool
    already_terminal: bool
    status: str | None = None


def _parse_timestamp(value: Any, *, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a timezone-aware timestamp or null")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field} is not a valid timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _normalize_order(
    data: Mapping[str, Any], *, environment: AlpacaEnvironment
) -> AlpacaOrder:
    raw_legs = data.get("legs")
    if raw_legs is None:
        legs: tuple[AlpacaOrder, ...] = ()
    elif isinstance(raw_legs, list):
        if not all(isinstance(item, Mapping) for item in raw_legs):
            raise ValueError("order legs must be JSON objects")
        legs = tuple(_normalize_order(item, environment=environment) for item in raw_legs)
    else:
        raise ValueError("order legs must be a JSON array or null")

    def optional_decimal(name: str) -> Decimal | None:
        return _decimal(data.get(name), field=name, optional=True)

    order_class = data.get("order_class")
    if order_class is not None and not isinstance(order_class, str):
        raise ValueError("order_class must be a string or null")
    return AlpacaOrder(
        environment=environment,
        order_id=_require_text(data.get("id"), field="id"),
        client_order_id=_require_text(data.get("client_order_id"), field="client_order_id"),
        symbol=_require_text(data.get("symbol"), field="symbol").upper(),
        status=_require_text(data.get("status"), field="status").lower(),
        side=_require_text(data.get("side"), field="side").lower(),
        type=_require_text(data.get("type", data.get("order_type")), field="type").lower(),
        time_in_force=_require_text(data.get("time_in_force"), field="time_in_force").lower(),
        order_class=None if order_class is None else order_class.lower(),
        qty=optional_decimal("qty"),
        notional=optional_decimal("notional"),
        filled_qty=optional_decimal("filled_qty"),
        filled_avg_price=optional_decimal("filled_avg_price"),
        limit_price=optional_decimal("limit_price"),
        stop_price=optional_decimal("stop_price"),
        trail_price=optional_decimal("trail_price"),
        trail_percent=optional_decimal("trail_percent"),
        hwm=optional_decimal("hwm"),
        legs=legs,
        created_at=_parse_timestamp(data.get("created_at"), field="created_at"),
        updated_at=_parse_timestamp(data.get("updated_at"), field="updated_at"),
        submitted_at=_parse_timestamp(data.get("submitted_at"), field="submitted_at"),
        filled_at=_parse_timestamp(data.get("filled_at"), field="filled_at"),
        expired_at=_parse_timestamp(data.get("expired_at"), field="expired_at"),
        canceled_at=_parse_timestamp(data.get("canceled_at"), field="canceled_at"),
        failed_at=_parse_timestamp(data.get("failed_at"), field="failed_at"),
        replaced_at=_parse_timestamp(data.get("replaced_at"), field="replaced_at"),
    )


def _enum_string(value: str | Enum, *, field: str, allowed: frozenset[str]) -> str:
    raw = getattr(value, "value", value)
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be a string or enum")
    normalized = raw.strip().lower()
    if normalized not in allowed:
        raise ValueError(f"unsupported {field}: {raw!r}")
    return normalized


class AlpacaOrderService:
    """Synchronous order boundary over an injected Alpaca HTTP client."""

    def __init__(
        self,
        client: Any,
        *,
        persist_before_send: Callable[..., Any] | Any | None = None,
        environment: AlpacaEnvironment | None = None,
        fingerprint: str | None = None,
    ) -> None:
        for method in ("get", "post", "patch", "delete"):
            if not callable(getattr(client, method, None)):
                raise TypeError(f"client must expose {method}()")
        resolved_environment = (
            environment
            if environment is not None
            else getattr(client, "environment", None)
        )
        if not isinstance(resolved_environment, AlpacaEnvironment):
            raise TypeError("environment must be supplied or exposed by the client")
        resolved_fingerprint = (
            fingerprint
            if fingerprint is not None
            else getattr(client, "fingerprint", None)
        )
        if not isinstance(resolved_fingerprint, str) or not resolved_fingerprint.strip():
            raise ValueError("fingerprint must be supplied or exposed by the client")
        if persist_before_send is not None:
            candidate = getattr(persist_before_send, "persist_before_send", persist_before_send)
            if not callable(candidate):
                raise TypeError(
                    "persist_before_send must be callable or expose persist_before_send()"
                )
        self._client = client
        self._environment = resolved_environment
        self._fingerprint = resolved_fingerprint.strip()
        self._persist_hook = persist_before_send
        self._outstanding: dict[tuple[str, str], tuple[int, str]] = {}

    @property
    def environment(self) -> AlpacaEnvironment:
        return self._environment

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def submit_order(
        self,
        *,
        asset: AlpacaAsset,
        side: str | Enum,
        order_type: str | Enum,
        sizing: OrderSizing | None = None,
        qty: Decimal | int | str | None = None,
        notional: Decimal | int | str | None = None,
        limit_price: Decimal | int | str | None = None,
        time_in_force: str | Enum = "day",
        cycle_id: str,
        purpose: OrderPurpose | str | Enum,
        attempt: int = 0,
        known_position_qty: Decimal | int | str | None = None,
    ) -> AlpacaOrder | NotSubmitted:
        """Submit a stock BUY/SELL market or limit order exactly once."""

        self._require_live_authorization()
        asset = self._validate_asset(asset)
        if sizing is not None and (qty is not None or notional is not None):
            raise ValueError("pass sizing or qty/notional, not both")
        resolved_sizing = sizing if sizing is not None else OrderSizing(qty=qty, notional=notional)
        if not isinstance(resolved_sizing, OrderSizing):
            raise TypeError("sizing must be an OrderSizing")
        resolved_side = _enum_string(side, field="side", allowed=frozenset({"buy", "sell"}))
        resolved_type = _enum_string(
            order_type, field="order_type", allowed=frozenset({"market", "limit"})
        )
        tif = _enum_string(
            time_in_force, field="time_in_force", allowed=frozenset({"day", "gtc"})
        )
        validated = self._validate_sizing(resolved_sizing, asset, tif=tif)
        if resolved_type == "limit":
            if limit_price is None:
                raise ValueError("limit orders require limit_price")
            price = quantize_price(limit_price, asset, strict=True)
        else:
            if limit_price is not None:
                raise ValueError("market orders must not specify limit_price")
            price = None
        validated_qty = validated.qty
        assert validated_qty is None or isinstance(validated_qty, Decimal)
        self._check_oversell(validated_qty, resolved_side, known_position_qty)
        body: dict[str, Any] = {
            "symbol": asset.symbol,
            "side": resolved_side,
            "type": resolved_type,
            "time_in_force": tif,
        }
        if validated.qty is not None:
            body["qty"] = str(validated.qty)
        else:
            body["notional"] = str(validated.notional)
        if price is not None:
            body["limit_price"] = str(price)
        return self._submit(body, cycle_id=cycle_id, purpose=purpose, attempt=attempt)

    def submit_stop_sell(
        self,
        *,
        asset: AlpacaAsset,
        qty: Decimal | int | str,
        stop_price: Decimal | int | str,
        known_position_qty: Decimal | int | str,
        capability_check: Callable[[str], bool] | None,
        time_in_force: str | Enum = "day",
        cycle_id: str,
        attempt: int = 0,
    ) -> AlpacaOrder | NotSubmitted:
        """Submit a capability-proven standalone protective stop SELL."""

        self._require_live_authorization()
        asset = self._validate_asset(asset)
        tif = _enum_string(
            time_in_force, field="time_in_force", allowed=frozenset({"day", "gtc"})
        )
        raw_qty = _positive_decimal(qty, field="qty")
        fractional = raw_qty != raw_qty.to_integral_value()
        capability = f"standalone_stop_sell:{'fractional' if fractional else 'whole'}:{tif}"
        self._require_capability(capability_check, capability)
        sizing = self._validate_sizing(OrderSizing(qty=raw_qty), asset, tif=tif)
        normalized_qty = sizing.qty
        assert isinstance(normalized_qty, Decimal)
        self._check_oversell(normalized_qty, "sell", known_position_qty)
        price = quantize_price(stop_price, asset, strict=True)
        body = {
            "symbol": asset.symbol,
            "qty": str(normalized_qty),
            "side": "sell",
            "type": "stop",
            "time_in_force": tif,
            "stop_price": str(price),
        }
        return self._submit(
            body,
            cycle_id=cycle_id,
            purpose=OrderPurpose.PROTECTIVE_STOP,
            attempt=attempt,
        )

    def submit_trailing_stop_sell(
        self,
        *,
        asset: AlpacaAsset,
        qty: Decimal | int | str,
        known_position_qty: Decimal | int | str,
        capability_check: Callable[[str], bool] | None,
        trail_price: Decimal | int | str | None = None,
        trail_percent: Decimal | int | str | None = None,
        time_in_force: str | Enum = "day",
        cycle_id: str,
        attempt: int = 0,
    ) -> AlpacaOrder | NotSubmitted:
        """Submit a standalone native trailing-stop SELL (never a linked leg)."""

        self._require_live_authorization()
        asset = self._validate_asset(asset)
        if (trail_price is None) == (trail_percent is None):
            raise ValueError("exactly one of trail_price or trail_percent is required")
        tif = _enum_string(
            time_in_force, field="time_in_force", allowed=frozenset({"day", "gtc"})
        )
        raw_qty = _positive_decimal(qty, field="qty")
        fractional = raw_qty != raw_qty.to_integral_value()
        quantity_kind = "fractional" if fractional else "whole"
        capability = f"standalone_trailing_stop_sell:{quantity_kind}:{tif}"
        self._require_capability(capability_check, capability)
        sizing = self._validate_sizing(OrderSizing(qty=raw_qty), asset, tif=tif)
        normalized_qty = sizing.qty
        assert isinstance(normalized_qty, Decimal)
        self._check_oversell(normalized_qty, "sell", known_position_qty)
        body: dict[str, Any] = {
            "symbol": asset.symbol,
            "qty": str(normalized_qty),
            "side": "sell",
            "type": "trailing_stop",
            "time_in_force": tif,
        }
        if trail_price is not None:
            body["trail_price"] = str(quantize_price(trail_price, asset, strict=True))
        else:
            body["trail_percent"] = str(
                _positive_decimal(trail_percent, field="trail_percent")
            )
        return self._submit(
            body,
            cycle_id=cycle_id,
            purpose=OrderPurpose.TRAILING_STOP,
            attempt=attempt,
        )

    def get_order(self, order_id: str) -> AlpacaOrder | None:
        cleaned = _require_text(order_id, field="order_id")
        try:
            data = self._client.get(f"/v2/orders/{cleaned}", params=None)
        except Exception as exc:  # duck-typed fakes preserve provider status_code
            if self._status_code(exc) == 404:
                return None
            raise
        return self._normalize_response(data, endpoint="GET /v2/orders/{id}")

    def get_order_by_client_order_id(self, client_order_id: str) -> AlpacaOrder | None:
        cleaned = _require_text(client_order_id, field="client_order_id")
        try:
            data = self._client.get(
                "/v2/orders:by_client_order_id",
                params={"client_order_id": cleaned},
            )
        except Exception as exc:  # duck-typed fakes preserve provider status_code
            if self._status_code(exc) == 404:
                return None
            raise
        return self._normalize_response(data, endpoint="GET /v2/orders:by_client_order_id")

    def list_open_orders(self, *, limit: int = 100, nested: bool = True) -> list[AlpacaOrder]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be an int between 1 and 500")
        if not isinstance(nested, bool):
            raise ValueError("nested must be bool")
        data = self._client.get(
            "/v2/orders",
            params={"status": "open", "limit": limit, "nested": nested},
        )
        if not isinstance(data, list):
            raise ValueError("GET /v2/orders must return a JSON array")
        return [
            self._normalize_response(item, endpoint="GET /v2/orders") for item in data
        ]

    def cancel_order(self, order_id: str) -> CancelResult:
        self._require_live_authorization()
        cleaned = _require_text(order_id, field="order_id")
        try:
            self._client.delete(f"/v2/orders/{cleaned}", params=None)
        except Exception as exc:  # 404/422 are idempotent terminal outcomes
            if self._status_code(exc) in {404, 422}:
                return CancelResult(
                    order_id=cleaned,
                    cancel_requested=False,
                    already_terminal=True,
                    status="already_terminal",
                )
            raise
        return CancelResult(
            order_id=cleaned,
            cancel_requested=True,
            already_terminal=False,
            status="cancel_requested",
        )

    def replace_order(
        self,
        order_id: str,
        *,
        asset: AlpacaAsset,
        capability_check: Callable[[str], bool] | None,
        limit_price: Decimal | int | str | None = None,
        stop_price: Decimal | int | str | None = None,
        trail_price: Decimal | int | str | None = None,
        trail_percent: Decimal | int | str | None = None,
        order: AlpacaOrder | None = None,
    ) -> AlpacaOrder:
        """Replace only supported price/trail fields after a proven capability."""

        self._require_live_authorization()
        cleaned = _require_text(order_id, field="order_id")
        asset = self._validate_asset(asset)
        current = order if order is not None else self.get_order(cleaned)
        if current is None:
            raise AlpacaNotFound("order not found", status_code=404)
        if current.order_id != cleaned:
            raise ValueError("supplied order does not match order_id")
        if current.environment is not self._environment:
            raise ValueError("supplied order environment does not match the order service")
        if current.notional is not None:
            raise CapabilityUnavailable(
                "replace_notional_order",
                "Alpaca notional orders cannot be replaced",
            )
        if current.order_class not in {None, "", "simple"}:
            raise CapabilityUnavailable(
                "replace_linked_order",
                "linked OTO, bracket, and OCO replacement is not proven",
            )
        changes = {
            "limit_price": limit_price,
            "stop_price": stop_price,
            "trail_price": trail_price,
            "trail_percent": trail_percent,
        }
        supplied = [name for name, value in changes.items() if value is not None]
        if not supplied:
            raise ValueError("a replacement price or trail field is required")
        trail_fields = {name for name in supplied if name.startswith("trail_")}
        if len(trail_fields) > 1 or (trail_fields and len(supplied) > 1):
            raise ValueError("replace exactly one trail field, without price fields")
        capability_fields = (
            next(iter(trail_fields)) if trail_fields else "_and_".join(supplied)
        )
        capability = "replace_" + capability_fields
        self._require_capability(capability_check, capability)
        body: dict[str, str] = {}
        for field in ("limit_price", "stop_price", "trail_price"):
            value = changes[field]
            if value is not None:
                body[field] = str(quantize_price(value, asset, strict=True))
        if trail_percent is not None:
            body["trail_percent"] = str(
                _positive_decimal(trail_percent, field="trail_percent")
            )
        data = self._client.patch(f"/v2/orders/{cleaned}", json_body=body, params=None)
        return self._normalize_response(data, endpoint="PATCH /v2/orders/{id}")

    def _submit(
        self,
        body: dict[str, Any],
        *,
        cycle_id: str,
        purpose: OrderPurpose | str | Enum,
        attempt: int,
    ) -> AlpacaOrder | NotSubmitted:
        cycle = _require_text(cycle_id, field="cycle_id")
        resolved_purpose = _purpose(purpose)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
            raise ValueError("attempt must be an int >= 0")
        lineage = (cycle, resolved_purpose.value)
        outstanding = self._outstanding.get(lineage)
        if outstanding is not None:
            previous_attempt, previous_client_order_id = outstanding
            try:
                previous = self.get_order_by_client_order_id(previous_client_order_id)
            except BaseException as lookup_error:
                raise OrderOutcomeUnresolved(
                    previous_client_order_id, lookup_error
                ) from lookup_error
            if previous is None:
                del self._outstanding[lineage]
                return NotSubmitted(client_order_id=previous_client_order_id)
            if not previous.is_terminal or previous_attempt == attempt:
                return previous
            del self._outstanding[lineage]

        client_order_id = make_client_order_id(
            self._environment,
            self._fingerprint,
            cycle,
            resolved_purpose,
            attempt,
        )
        request = dict(body)
        request["client_order_id"] = client_order_id
        self._persist_before_send(client_order_id, request)
        try:
            data = self._client.post("/v2/orders", json_body=request, params=None)
        except _UNKNOWN_OUTCOME_ERRORS as submit_error:
            self._outstanding[lineage] = (attempt, client_order_id)
            try:
                found = self.get_order_by_client_order_id(client_order_id)
            except BaseException as lookup_error:
                unresolved = OrderOutcomeUnresolved(client_order_id, lookup_error)
                unresolved.add_note(f"submission error: {submit_error!r}")
                raise unresolved from lookup_error
            if found is None:
                del self._outstanding[lineage]
                return NotSubmitted(client_order_id=client_order_id)
            if found.is_terminal:
                del self._outstanding[lineage]
            return found
        result = self._normalize_response(data, endpoint="POST /v2/orders")
        if result.is_terminal:
            self._outstanding.pop(lineage, None)
        else:
            self._outstanding[lineage] = (attempt, client_order_id)
        return result

    def _persist_before_send(self, client_order_id: str, request: Mapping[str, Any]) -> None:
        if self._persist_hook is None:
            raise TypeError("a persist_before_send hook is required for submission")
        hook = getattr(self._persist_hook, "persist_before_send", self._persist_hook)
        frozen_request = MappingProxyType(dict(request))
        try:
            signature = inspect.signature(hook)
        except (TypeError, ValueError):
            hook(client_order_id, frozen_request)
            return
        try:
            signature.bind(client_order_id, frozen_request)
        except TypeError:
            try:
                signature.bind(client_order_id)
            except TypeError as exc:
                raise TypeError(
                    "persist_before_send must accept client_order_id and optionally request"
                ) from exc
            hook(client_order_id)
        else:
            hook(client_order_id, frozen_request)

    def _validate_asset(self, asset: AlpacaAsset) -> AlpacaAsset:
        if not isinstance(asset, AlpacaAsset):
            raise TypeError("asset must be an AlpacaAsset")
        if asset.environment is not self._environment:
            raise ValueError("asset environment does not match the order service")
        if asset.fingerprint != self._fingerprint:
            raise ValueError("asset fingerprint does not match the order service")
        if not asset.eligibility().eligible:
            raise CapabilityUnavailable("tradable_active_us_equity", "asset is not eligible")
        return asset

    @staticmethod
    def _validate_sizing(
        sizing: OrderSizing, asset: AlpacaAsset, *, tif: str
    ) -> OrderSizing:
        if sizing.qty is not None:
            qty = quantize_quantity(sizing.qty, asset, strict=True)
            fractional = qty != qty.to_integral_value()
            if fractional and not asset.fractionable:
                raise CapabilityUnavailable(
                    "fractional_order", "fractional orders require a fractionable asset"
                )
            if fractional and tif != "day":
                raise CapabilityUnavailable(
                    "fractional_order_tif", "fractional orders require DAY time_in_force"
                )
            return OrderSizing(qty=qty)
        assert sizing.notional is not None
        if not asset.fractionable:
            raise CapabilityUnavailable(
                "notional_order", "notional orders require a fractionable asset"
            )
        if tif != "day":
            raise CapabilityUnavailable(
                "notional_order_tif", "notional orders require DAY time_in_force"
            )
        notional = quantize_price(sizing.notional, asset, strict=True)
        return OrderSizing(notional=notional)

    @staticmethod
    def _check_oversell(
        qty: Decimal | None,
        side: str,
        known_position_qty: Decimal | int | str | None,
    ) -> None:
        if side != "sell" or qty is None or known_position_qty is None:
            return
        known = _positive_decimal(known_position_qty, field="known_position_qty")
        if qty > known:
            raise OversellRejected(qty, known)

    @staticmethod
    def _require_capability(
        checker: Callable[[str], bool] | None, capability: str
    ) -> None:
        if checker is None:
            raise CapabilityUnavailable(capability)
        try:
            proven = checker(capability)
        except Exception as exc:
            raise CapabilityUnavailable(
                capability, f"capability check failed closed for {capability!r}"
            ) from exc
        if proven is not True:
            raise CapabilityUnavailable(capability)

    def _require_live_authorization(self) -> None:
        if self._environment is not AlpacaEnvironment.LIVE:
            return
        marker = getattr(self._client, "live_authorized", None)
        if marker is None:
            marker = getattr(self._client, "_live_authorized", None)
        # AlpacaHttpClient's constructor itself refuses an unauthorized LIVE
        # instance.  Test doubles must carry an explicit marker.
        if marker is None and isinstance(self._client, AlpacaHttpClient):
            marker = True
        if marker is not True:
            raise LiveAuthorizationRequired(
                "LIVE submit, replace, and cancel require explicit live authorization"
            )

    def _normalize_response(self, data: Any, *, endpoint: str) -> AlpacaOrder:
        if not isinstance(data, Mapping):
            raise ValueError(f"{endpoint} must return a JSON object")
        return _normalize_order(data, environment=self._environment)

    @staticmethod
    def _status_code(exc: BaseException) -> int | None:
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            return status
        if isinstance(exc, AlpacaNotFound):
            return 404
        if isinstance(exc, AlpacaUnprocessable):
            return 422
        return None
