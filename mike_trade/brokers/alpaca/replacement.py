"""Alpaca implementation of the provider-neutral stop replacement port.

Only the injected HTTP/SDK client is used.  This module never constructs a
client, reads credentials, or performs implicit network access.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import cast

from mike_trade.domain.trading import TimeInForce
from mike_trade.execution.replace import ReplacementOutcome, ReplacementStatus

__all__ = ["AlpacaStopReplacementAdapter"]

_ZERO = Decimal("0")
_OPEN = frozenset({"accepted", "new", "pending_new", "pending_replace"})


def _positive_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{name} must be Decimal, int, or str")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation:
            raise ValueError(f"{name} is not a valid Decimal") from None
    else:
        raise ValueError(f"{name} must be Decimal, int, or str")
    if not result.is_finite() or result <= _ZERO:
        raise ValueError(f"{name} must be a finite value > 0")
    return result


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _value(data: object, *names: str) -> object | None:
    if isinstance(data, Mapping):
        for name in names:
            if name in data:
                return cast(object, data[name])
        return None
    for name in names:
        if hasattr(data, name):
            return cast(object, getattr(data, name))
    return None


class AlpacaStopReplacementAdapter:
    """Single-shot PATCH/DELETE adapter with explicit capability flags."""

    def __init__(
        self,
        client: object,
        *,
        price_increment: Decimal,
        standalone_stop_verified: bool = True,
        replace_verified: bool = True,
        fractional_stop_verified_in_paper: bool = False,
    ) -> None:
        if client is None:
            raise TypeError("client is required")
        self._client = client
        self._price_increment = _positive_decimal(price_increment, "price_increment")
        for name, value in (
            ("standalone_stop_verified", standalone_stop_verified),
            ("replace_verified", replace_verified),
            ("fractional_stop_verified_in_paper", fractional_stop_verified_in_paper),
        ):
            if not isinstance(value, bool):
                raise TypeError(f"{name} must be bool")
        self._standalone_stop_verified = standalone_stop_verified
        self._replace_verified = replace_verified
        self._fractional_stop_verified = fractional_stop_verified_in_paper
        self._requests: dict[str, tuple[Decimal, Decimal, str | None]] = {}
        self._cancel_pending: set[str] = set()

    def supports_standalone_stop(self, *, qty: Decimal, tif: TimeInForce) -> bool:
        quantity = _positive_decimal(qty, "qty")
        self._tif(tif)
        return self._standalone_stop_verified and (
            quantity == quantity.to_integral_value() or self._fractional_stop_verified
        )

    def supports_replace(self, *, qty: Decimal, tif: TimeInForce) -> bool:
        quantity = _positive_decimal(qty, "qty")
        self._tif(tif)
        return self._replace_verified and (
            quantity == quantity.to_integral_value() or self._fractional_stop_verified
        )

    def price_increment(self) -> Decimal:
        return self._price_increment

    def replace_stop(
        self,
        order_id: str,
        new_stop_price: Decimal,
        qty: Decimal,
        tif: TimeInForce,
        client_order_id: str,
    ) -> ReplacementOutcome:
        old_id = _text(order_id, "order_id")
        price = _positive_decimal(new_stop_price, "new_stop_price")
        quantity = _positive_decimal(qty, "qty")
        tif_value = self._tif(tif)
        client_id = _text(client_order_id, "client_order_id")
        if not self.supports_standalone_stop(qty=quantity, tif=tif):
            return self._unsupported(old_id, "standalone stop combination is not verified")
        if not self.supports_replace(qty=quantity, tif=tif):
            return self._unsupported(old_id, "stop replacement combination is not verified")
        body = {
            "stop_price": str(price),
            "qty": str(quantity),
            "time_in_force": tif_value,
            "client_order_id": client_id,
        }
        # Record the expected values before PATCH so even a compact
        # ``status=replaced`` response can be normalized without guessing.
        self._requests[old_id] = (price, quantity, None)
        try:
            data = self._patch(old_id, body)
        except Exception as exc:
            return ReplacementOutcome(
                ReplacementStatus.UNKNOWN,
                old_id,
                observed_at=datetime.now(UTC),
                detail=f"Alpaca replace outcome unknown: {exc!r}",
            )
        result = self._map(data, protected_order_id=old_id, mutation="replace")
        self._requests[old_id] = (price, quantity, result.broker_order_id)
        if result.status is ReplacementStatus.CONFIRMED:
            result = self._apply_requested(result, old_id)
        return result

    def cancel_stop(self, order_id: str) -> ReplacementOutcome:
        old_id = _text(order_id, "order_id")
        self._cancel_pending.add(old_id)
        try:
            data = self._delete(old_id)
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code not in {404, 422}:
                return ReplacementOutcome(
                    ReplacementStatus.UNKNOWN,
                    old_id,
                    observed_at=datetime.now(UTC),
                    detail=f"Alpaca cancel outcome unknown: {exc!r}",
                )
            # A terminal response needs lookup to distinguish cancel from fill.
            return self.lookup_order(old_id)
        if data is None:
            return ReplacementOutcome(
                ReplacementStatus.UNKNOWN,
                old_id,
                observed_at=datetime.now(UTC),
                detail="Alpaca accepted cancellation; final status is unconfirmed",
            )
        result = self._map(data, protected_order_id=old_id, mutation="cancel")
        if result.status is ReplacementStatus.CONFIRMED:
            self._cancel_pending.discard(old_id)
        return result

    def lookup_order(self, order_id: str) -> ReplacementOutcome:
        old_id = _text(order_id, "order_id")
        try:
            data = self._get(old_id)
        except Exception as exc:
            return ReplacementOutcome(
                ReplacementStatus.UNKNOWN,
                old_id,
                observed_at=datetime.now(UTC),
                detail=f"Alpaca lookup failed: {exc!r}",
            )
        if data is None:
            return ReplacementOutcome(
                ReplacementStatus.UNKNOWN,
                old_id,
                observed_at=datetime.now(UTC),
                detail="Alpaca lookup returned no order",
            )
        result = self._map(data, protected_order_id=old_id, mutation="lookup")
        if result.status is ReplacementStatus.CONFIRMED and old_id in self._cancel_pending:
            self._cancel_pending.discard(old_id)
        if result.status is ReplacementStatus.CONFIRMED and old_id in self._requests:
            result = self._apply_requested(result, old_id)
        return result

    def outcome_from_trade_update(
        self, update: object, *, protected_order_id: str
    ) -> ReplacementOutcome:
        """Map an already-authenticated Alpaca trade update without doing I/O."""

        payload = _value(update, "order") or update
        event = _value(update, "event")
        if event is not None:
            if isinstance(payload, Mapping):
                payload = dict(payload)
                payload["status"] = getattr(event, "value", event)
            elif hasattr(payload, "status"):
                # Normalized TradeUpdate.order already carries the same status.
                pass
        result = self._map(payload, protected_order_id=protected_order_id, mutation="update")
        if result.status is ReplacementStatus.CONFIRMED and protected_order_id in self._requests:
            result = self._apply_requested(result, protected_order_id)
        return result

    def _apply_requested(self, result: ReplacementOutcome, old_id: str) -> ReplacementOutcome:
        from dataclasses import replace

        price, quantity, submitted_id = self._requests[old_id]
        return replace(
            result,
            broker_order_id=result.broker_order_id or submitted_id or old_id,
            stop_price=price,
            quantity=quantity,
        )

    @staticmethod
    def _unsupported(order_id: str, detail: str) -> ReplacementOutcome:
        return ReplacementOutcome(
            ReplacementStatus.UNSUPPORTED,
            order_id,
            observed_at=datetime.now(UTC),
            detail=detail,
        )

    def _map(
        self, data: object, *, protected_order_id: str, mutation: str
    ) -> ReplacementOutcome:
        status_value = _value(data, "status")
        raw_status = getattr(status_value, "value", status_value)
        status = str(raw_status or "unknown").strip().lower()
        order_id_value = _value(data, "id", "order_id", "broker_order_id")
        current_id = str(order_id_value).strip() if order_id_value else None
        replacement_value = _value(data, "replaced_by", "replacement_order_id")
        replacement_id = str(replacement_value).strip() if replacement_value else None
        qty = self._optional_decimal(_value(data, "qty", "quantity"), "qty")
        filled = self._nonnegative_decimal(
            _value(data, "filled_qty", "filled_quantity"), "filled_qty"
        ) or _ZERO
        stop = self._optional_decimal(_value(data, "stop_price"), "stop_price")
        observed_at = self._timestamp(_value(data, "updated_at", "timestamp"))

        if status == "replaced":
            return ReplacementOutcome(
                ReplacementStatus.CONFIRMED,
                protected_order_id,
                broker_order_id=replacement_id or current_id or protected_order_id,
                stop_price=stop or self._requested_price(protected_order_id),
                quantity=qty or self._requested_qty(protected_order_id),
                observed_at=observed_at,
                detail="Alpaca confirmed order replacement",
            )
        if status in {"filled", "partially_filled", "partial_fill"}:
            if filled <= _ZERO and status == "filled" and qty is not None:
                filled = qty
            remaining = max(_ZERO, (qty or filled) - filled)
            return ReplacementOutcome(
                ReplacementStatus.STOP_FILLED_DURING_REPLACE,
                protected_order_id,
                broker_order_id=current_id or protected_order_id,
                filled_quantity=filled,
                remaining_quantity=remaining,
                observed_at=observed_at,
                detail=f"old Alpaca stop was {status} during {mutation}",
            )
        if status == "rejected":
            return ReplacementOutcome(
                ReplacementStatus.REJECTED,
                protected_order_id,
                broker_order_id=current_id,
                observed_at=observed_at,
                detail="Alpaca rejected the stop mutation",
            )
        if status == "canceled" and (
            mutation == "cancel" or protected_order_id in self._cancel_pending
        ):
            # A confirmed cancellation is represented as CONFIRMED.  The
            # coordinator's cancel path supplies its prior stop/quantity.
            requested_price = self._requested_price(protected_order_id)
            requested_qty = self._requested_qty(protected_order_id)
            confirmed_price = requested_price or stop
            confirmed_qty = requested_qty or qty
            if confirmed_price is not None and confirmed_qty is not None:
                return ReplacementOutcome(
                    ReplacementStatus.CONFIRMED,
                    protected_order_id,
                    broker_order_id=current_id or protected_order_id,
                    stop_price=confirmed_price,
                    quantity=confirmed_qty,
                    observed_at=observed_at,
                    detail="Alpaca confirmed cancellation",
                )
            return ReplacementOutcome(
                ReplacementStatus.UNKNOWN,
                protected_order_id,
                broker_order_id=current_id,
                observed_at=observed_at,
                detail="Alpaca confirmed cancellation; no replacement is confirmed",
            )
        detail = (
            "Alpaca acknowledged replacement but has not confirmed it"
            if status in _OPEN
            else f"Alpaca status {status!r} does not confirm replacement"
        )
        return ReplacementOutcome(
            ReplacementStatus.UNKNOWN,
            protected_order_id,
            broker_order_id=current_id,
            stop_price=stop,
            quantity=qty,
            observed_at=observed_at,
            detail=detail,
        )

    def _patch(self, order_id: str, body: Mapping[str, str]) -> object:
        method = getattr(self._client, "patch", None)
        if callable(method):
            return method(f"/v2/orders/{order_id}", json_body=dict(body), params=None)
        method = getattr(self._client, "replace_order_by_id", None)
        if callable(method):
            return method(order_id, **dict(body))
        method = getattr(self._client, "replace_order", None)
        if callable(method):
            return method(order_id, **dict(body))
        raise TypeError("client must expose patch, replace_order_by_id, or replace_order")

    def _delete(self, order_id: str) -> object:
        method = getattr(self._client, "delete", None)
        if callable(method):
            return method(f"/v2/orders/{order_id}", params=None)
        for name in ("cancel_order_by_id", "cancel_order"):
            method = getattr(self._client, name, None)
            if callable(method):
                return method(order_id)
        raise TypeError("client must expose delete, cancel_order_by_id, or cancel_order")

    def _get(self, order_id: str) -> object:
        method = getattr(self._client, "get", None)
        if callable(method):
            return method(f"/v2/orders/{order_id}", params=None)
        for name in ("get_order_by_id", "get_order"):
            method = getattr(self._client, name, None)
            if callable(method):
                return method(order_id)
        raise TypeError("client must expose get, get_order_by_id, or get_order")

    def _requested_price(self, order_id: str) -> Decimal | None:
        request = self._requests.get(order_id)
        return None if request is None else request[0]

    def _requested_qty(self, order_id: str) -> Decimal | None:
        request = self._requests.get(order_id)
        return None if request is None else request[1]

    @staticmethod
    def _optional_decimal(value: object | None, name: str) -> Decimal | None:
        if value is None:
            return None
        return _positive_decimal(value, name)

    @staticmethod
    def _nonnegative_decimal(value: object | None, name: str) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, bool) or isinstance(value, float):
            raise ValueError(f"{name} must be Decimal, int, or str")
        try:
            result = value if isinstance(value, Decimal) else Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            raise ValueError(f"{name} is not a valid Decimal") from None
        if not result.is_finite() or result < _ZERO:
            raise ValueError(f"{name} must be finite and >= 0")
        return result

    @staticmethod
    def _timestamp(value: object | None) -> datetime:
        if value is None:
            return datetime.now(UTC)
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            text = value.strip().replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
        else:
            return datetime.now(UTC)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Alpaca timestamp must be timezone-aware")
        return parsed.astimezone(UTC)

    @staticmethod
    def _tif(value: TimeInForce | str | Enum) -> str:
        raw = getattr(value, "value", value)
        if not isinstance(raw, str) or raw.strip().lower() not in {"day", "gtc"}:
            raise ValueError("tif must be DAY or GTC")
        return raw.strip().lower()
