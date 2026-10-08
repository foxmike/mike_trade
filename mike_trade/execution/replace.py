"""Provider-neutral, confirmation-first protective-stop replacement workflow.

The coordinator deliberately owns no durable state.  It requires an injected
intent recorder before a mutation and keeps only short-lived in-process state
used to serialize replacements.  A restart therefore requires normal broker
reconciliation; it must never infer protection from this object's memory.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from mike_trade.domain.trading import TimeInForce

__all__ = [
    "StopReplacementPort",
    "StopReplacementCoordinator",
    "ReplacementOutcome",
    "ReplacementStatus",
]

_ZERO = Decimal("0")
_UNCONFIRMED_EVENT = "TRAIL_UPDATE_UNCONFIRMED"


class _Clock(Protocol):
    def now(self) -> datetime: ...


class ReplacementStatus(StrEnum):
    """Provider-neutral disposition of a stop mutation or observation."""

    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    STOP_FILLED_DURING_REPLACE = "STOP_FILLED_DURING_REPLACE"
    UNSUPPORTED = "UNSUPPORTED"


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise ValueError(f"{name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    if positive and value <= _ZERO:
        raise ValueError(f"{name} must be > 0")
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ReplacementOutcome:
    """A mutation result; only ``CONFIRMED`` changes authoritative protection.

    ``authoritative_*`` describes the last broker-verified protection.  On an
    unconfirmed result it intentionally continues to describe the old stop.
    ``remaining_quantity`` is populated for a fill race and excludes shares
    already sold by the old stop.
    """

    status: ReplacementStatus
    protected_order_id: str
    broker_order_id: str | None = None
    stop_price: Decimal | None = None
    quantity: Decimal | None = None
    filled_quantity: Decimal = _ZERO
    remaining_quantity: Decimal | None = None
    authoritative_order_id: str | None = None
    authoritative_stop_price: Decimal | None = None
    authoritative_quantity: Decimal | None = None
    event: str | None = None
    entries_blocked: bool = False
    reconciliation_required: bool = False
    coalesced: bool = False
    coalesced_stop_price: Decimal | None = None
    observed_at: datetime | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, ReplacementStatus):
            object.__setattr__(self, "status", ReplacementStatus(self.status))
        object.__setattr__(
            self, "protected_order_id", _text(self.protected_order_id, "protected_order_id")
        )
        for name in ("broker_order_id", "authoritative_order_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _text(value, name))
        for name in (
            "stop_price",
            "quantity",
            "authoritative_stop_price",
            "authoritative_quantity",
            "coalesced_stop_price",
        ):
            value = getattr(self, name)
            if value is not None:
                _decimal(value, name, positive=True)
        if self.remaining_quantity is not None:
            _decimal(self.remaining_quantity, "remaining_quantity")
            if self.remaining_quantity < _ZERO:
                raise ValueError("remaining_quantity must be >= 0")
        _decimal(self.filled_quantity, "filled_quantity")
        if self.filled_quantity < _ZERO:
            raise ValueError("filled_quantity must be >= 0")
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        for name in ("entries_blocked", "reconciliation_required", "coalesced"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")
        if self.status is ReplacementStatus.CONFIRMED:
            if self.broker_order_id is None or self.stop_price is None or self.quantity is None:
                raise ValueError("CONFIRMED requires broker_order_id, stop_price, and quantity")
        if self.status is ReplacementStatus.STOP_FILLED_DURING_REPLACE:
            if self.filled_quantity <= _ZERO or self.remaining_quantity is None:
                raise ValueError(
                    "STOP_FILLED_DURING_REPLACE requires positive filled_quantity and "
                    "remaining_quantity"
                )

    @property
    def effective(self) -> bool:
        return self.status is ReplacementStatus.CONFIRMED

    @property
    def trail_update_unconfirmed(self) -> bool:
        return self.event == _UNCONFIRMED_EVENT

    @property
    def new_broker_order_id(self) -> str | None:
        return self.broker_order_id if self.effective else None

    @property
    def new_stop_price(self) -> Decimal | None:
        return self.stop_price if self.effective else None

    @property
    def qty(self) -> Decimal | None:
        return self.quantity

    @property
    def filled_qty(self) -> Decimal:
        return self.filled_quantity

    @property
    def block_new_entries(self) -> bool:
        return self.entries_blocked

    @property
    def requires_reconciliation(self) -> bool:
        return self.reconciliation_required

    @property
    def signal(self) -> str | None:
        return self.event


@runtime_checkable
class StopReplacementPort(Protocol):
    """Minimal broker boundary needed by :class:`StopReplacementCoordinator`."""

    def supports_standalone_stop(self, *, qty: Decimal, tif: TimeInForce) -> bool: ...

    def supports_replace(self, *, qty: Decimal, tif: TimeInForce) -> bool: ...

    def price_increment(self) -> Decimal: ...

    def replace_stop(
        self,
        order_id: str,
        new_stop_price: Decimal,
        qty: Decimal,
        tif: TimeInForce,
        client_order_id: str,
    ) -> ReplacementOutcome: ...

    def cancel_stop(self, order_id: str) -> ReplacementOutcome: ...

    def lookup_order(self, order_id: str) -> ReplacementOutcome: ...


@dataclass(frozen=True, slots=True)
class _Pending:
    key: str
    order_id: str
    price: Decimal
    quantity: Decimal
    tif: TimeInForce
    client_order_id: str
    last_confirmed_stop: Decimal
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class _Held:
    price: Decimal
    quantity: Decimal
    tif: TimeInForce
    client_order_id: str


class StopReplacementCoordinator:
    """Serialize, persist, submit, and confirm protective-stop mutations.

    The methods are synchronous because the existing Alpaca order boundary is
    synchronous.  A provider adapter must not return an awaitable: silently
    losing an asynchronous mutation would violate the confirmation contract.
    """

    def __init__(
        self,
        port: StopReplacementPort,
        intent_recorder: Callable[[Mapping[str, object]], object] | object,
        *,
        min_replace_interval: timedelta = timedelta(0),
        clock: Callable[[], datetime] | _Clock | None = None,
    ) -> None:
        if not isinstance(min_replace_interval, timedelta) or min_replace_interval < timedelta(0):
            raise ValueError("min_replace_interval must be a timedelta >= 0")
        if not callable(intent_recorder) and not callable(
            getattr(intent_recorder, "record_replacement_intent", None)
        ):
            raise TypeError("intent_recorder must be callable or expose record_replacement_intent")
        self._port = port
        self._intent_recorder = intent_recorder
        self._min_replace_interval = min_replace_interval
        self._clock = clock or (lambda: datetime.now(UTC))
        self._pending: dict[str, _Pending] = {}
        self._held: dict[str, _Held] = {}
        self._last_request_at: dict[str, datetime] = {}

    def replace_stop(
        self,
        *,
        protected_order_id: str,
        candidate_stop: Decimal,
        last_confirmed_stop: Decimal,
        qty: Decimal,
        tif: TimeInForce,
        client_order_id: str,
        position_id: str | None = None,
    ) -> ReplacementOutcome:
        """Request one replacement, returning no effective change without confirmation."""

        order_id = _text(protected_order_id, "protected_order_id")
        client_id = _text(client_order_id, "client_order_id")
        key = _text(position_id, "position_id") if position_id is not None else order_id
        candidate = _decimal(candidate_stop, "candidate_stop", positive=True)
        confirmed = _decimal(last_confirmed_stop, "last_confirmed_stop", positive=True)
        quantity = _decimal(qty, "qty", positive=True)
        resolved_tif = self._tif(tif)
        now = self._now()

        capability_error = self._capability_error(quantity, resolved_tif)
        if capability_error is not None:
            return self._local(
                ReplacementStatus.UNSUPPORTED,
                order_id,
                confirmed,
                quantity,
                now,
                capability_error,
            )

        rounded = self._round_down(candidate)
        if rounded <= confirmed:
            return self._local(
                ReplacementStatus.REJECTED,
                order_id,
                confirmed,
                quantity,
                now,
                "broker-rounded candidate must be strictly above the last confirmed stop",
            )

        pending = self._pending.get(key)
        if pending is not None:
            self._held[key] = _Held(rounded, quantity, resolved_tif, client_id)
            return self._unconfirmed(
                order_id=order_id,
                last_stop=confirmed,
                quantity=quantity,
                now=now,
                detail="replacement already pending; latest candidate held locally",
                coalesced=True,
                coalesced_stop=rounded,
            )

        previous = self._last_request_at.get(key)
        if previous is not None and now - previous < self._min_replace_interval:
            return self._local(
                ReplacementStatus.REJECTED,
                order_id,
                confirmed,
                quantity,
                now,
                "minimum replacement interval has not elapsed",
            )

        # A last-moment read prevents placing another stop after the old stop
        # has already sold some or all of the protected shares.
        observed = self._safe_lookup(order_id, now=now)
        if observed.status is ReplacementStatus.STOP_FILLED_DURING_REPLACE:
            return self._with_authority(observed, order_id, confirmed, quantity, now)

        intent = MappingProxyType(
            {
                "action": "replace_stop",
                "protected_order_id": order_id,
                "position_id": position_id,
                "new_stop_price": rounded,
                "last_confirmed_stop": confirmed,
                "qty": quantity,
                "time_in_force": resolved_tif,
                "client_order_id": client_id,
                "recorded_at": now,
            }
        )
        self._record_intent(intent)
        pending = _Pending(
            key, order_id, rounded, quantity, resolved_tif, client_id, confirmed, now
        )
        self._pending[key] = pending
        self._last_request_at[key] = now

        try:
            result = self._require_outcome(
                self._port.replace_stop(order_id, rounded, quantity, resolved_tif, client_id)
            )
        except Exception as exc:
            return self._unconfirmed(
                order_id=order_id,
                last_stop=confirmed,
                quantity=quantity,
                now=now,
                detail=f"replace call outcome unknown: {exc!r}",
            )

        normalized = self._normalize_result(result, pending, now)
        if normalized.status is ReplacementStatus.UNKNOWN:
            lookup = self._safe_lookup(order_id, now=now)
            if lookup.status is not ReplacementStatus.UNKNOWN:
                normalized = self._normalize_result(lookup, pending, now)
        return self._finish(key, normalized)

    request_replace = replace_stop
    replace = replace_stop

    def confirm(
        self,
        protected_order_id: str,
        observation: ReplacementOutcome | None = None,
        *,
        position_id: str | None = None,
    ) -> ReplacementOutcome:
        """Resolve a pending replacement from a trade update or broker lookup."""

        order_id = _text(protected_order_id, "protected_order_id")
        key = _text(position_id, "position_id") if position_id is not None else order_id
        pending = self._pending.get(key)
        if pending is None:
            return ReplacementOutcome(
                ReplacementStatus.UNKNOWN,
                order_id,
                authoritative_order_id=order_id,
                event=_UNCONFIRMED_EVENT,
                entries_blocked=True,
                reconciliation_required=True,
                observed_at=self._now(),
                detail="no in-process replacement intent; broker reconciliation required",
            )
        now = self._now()
        raw = self._safe_lookup(order_id, now=now) if observation is None else observation
        result = self._normalize_result(self._require_outcome(raw), pending, now)
        return self._finish(key, result)

    confirm_replacement = confirm
    handle_trade_update = confirm

    def cancel_stop(
        self,
        *,
        protected_order_id: str,
        last_confirmed_stop: Decimal,
        qty: Decimal,
        client_order_id: str,
    ) -> ReplacementOutcome:
        """Persist and request cancellation; a request acknowledgement is not final."""

        order_id = _text(protected_order_id, "protected_order_id")
        confirmed = _decimal(last_confirmed_stop, "last_confirmed_stop", positive=True)
        quantity = _decimal(qty, "qty", positive=True)
        now = self._now()
        observed = self._safe_lookup(order_id, now=now)
        if observed.status is ReplacementStatus.STOP_FILLED_DURING_REPLACE:
            return self._with_authority(observed, order_id, confirmed, quantity, now)
        self._record_intent(
            MappingProxyType(
                {
                    "action": "cancel_stop",
                    "protected_order_id": order_id,
                    "last_confirmed_stop": confirmed,
                    "qty": quantity,
                    "client_order_id": _text(client_order_id, "client_order_id"),
                    "recorded_at": now,
                }
            )
        )
        try:
            result = self._require_outcome(self._port.cancel_stop(order_id))
        except Exception as exc:
            return self._unconfirmed(
                order_id=order_id,
                last_stop=confirmed,
                quantity=quantity,
                now=now,
                detail=f"cancel call outcome unknown: {exc!r}",
            )
        if result.status is ReplacementStatus.UNKNOWN:
            looked_up = self._safe_lookup(order_id, now=now)
            if looked_up.status is not ReplacementStatus.UNKNOWN:
                result = looked_up
        if result.status in {ReplacementStatus.REJECTED, ReplacementStatus.UNKNOWN}:
            return self._unconfirmed(
                order_id=order_id,
                last_stop=confirmed,
                quantity=quantity,
                now=now,
                detail=result.detail or "cancel is not broker-confirmed",
                status=result.status,
            )
        if result.status is ReplacementStatus.CONFIRMED:
            # This confirms removal, not a new protective order.
            return replace(
                result,
                protected_order_id=order_id,
                authoritative_order_id=None,
                authoritative_stop_price=None,
                authoritative_quantity=None,
                entries_blocked=False,
                reconciliation_required=False,
                observed_at=result.observed_at or now,
            )
        return self._with_authority(result, order_id, confirmed, quantity, now)

    def pending(self, protected_order_id: str) -> bool:
        return _text(protected_order_id, "protected_order_id") in self._pending

    def coalesced_candidate(self, protected_order_id: str) -> Decimal | None:
        held = self._held.get(_text(protected_order_id, "protected_order_id"))
        return None if held is None else held.price

    def _finish(self, key: str, result: ReplacementOutcome) -> ReplacementOutcome:
        held = self._held.get(key)
        if result.status in {
            ReplacementStatus.CONFIRMED,
            ReplacementStatus.STOP_FILLED_DURING_REPLACE,
        }:
            self._pending.pop(key, None)
            self._held.pop(key, None)
        if held is not None:
            result = replace(
                result,
                coalesced=True,
                coalesced_stop_price=held.price,
                detail=(result.detail + "; " if result.detail else "")
                + "a newer candidate was coalesced while this request was pending",
            )
        return result

    def _normalize_result(
        self, result: ReplacementOutcome, pending: _Pending, now: datetime
    ) -> ReplacementOutcome:
        if result.status is ReplacementStatus.CONFIRMED:
            return replace(
                result,
                protected_order_id=pending.order_id,
                stop_price=result.stop_price or pending.price,
                quantity=result.quantity or pending.quantity,
                authoritative_order_id=result.broker_order_id,
                authoritative_stop_price=result.stop_price or pending.price,
                authoritative_quantity=result.quantity or pending.quantity,
                event=None,
                entries_blocked=False,
                reconciliation_required=False,
                observed_at=result.observed_at or now,
            )
        if result.status is ReplacementStatus.STOP_FILLED_DURING_REPLACE:
            return self._with_authority(
                result,
                pending.order_id,
                pending.last_confirmed_stop,
                pending.quantity,
                now,
            )
        if result.status in {ReplacementStatus.REJECTED, ReplacementStatus.UNKNOWN}:
            return self._unconfirmed(
                order_id=pending.order_id,
                last_stop=pending.last_confirmed_stop,
                quantity=pending.quantity,
                now=result.observed_at or now,
                detail=result.detail,
                status=result.status,
                broker_order_id=result.broker_order_id,
            )
        return self._with_authority(
            result, pending.order_id, pending.last_confirmed_stop, pending.quantity, now
        )

    @staticmethod
    def _with_authority(
        result: ReplacementOutcome,
        order_id: str,
        stop: Decimal,
        quantity: Decimal,
        now: datetime,
    ) -> ReplacementOutcome:
        return replace(
            result,
            protected_order_id=order_id,
            authoritative_order_id=(
                result.broker_order_id
                if result.status is ReplacementStatus.CONFIRMED
                else order_id
            ),
            authoritative_stop_price=(
                result.stop_price if result.status is ReplacementStatus.CONFIRMED else stop
            ),
            authoritative_quantity=(
                result.quantity if result.status is ReplacementStatus.CONFIRMED else quantity
            ),
            observed_at=result.observed_at or now,
        )

    @staticmethod
    def _unconfirmed(
        *,
        order_id: str,
        last_stop: Decimal,
        quantity: Decimal,
        now: datetime,
        detail: str,
        status: ReplacementStatus = ReplacementStatus.UNKNOWN,
        broker_order_id: str | None = None,
        coalesced: bool = False,
        coalesced_stop: Decimal | None = None,
    ) -> ReplacementOutcome:
        return ReplacementOutcome(
            status,
            order_id,
            broker_order_id=broker_order_id,
            authoritative_order_id=order_id,
            authoritative_stop_price=last_stop,
            authoritative_quantity=quantity,
            event=_UNCONFIRMED_EVENT,
            entries_blocked=True,
            reconciliation_required=True,
            coalesced=coalesced,
            coalesced_stop_price=coalesced_stop,
            observed_at=now,
            detail=detail,
        )

    @staticmethod
    def _local(
        status: ReplacementStatus,
        order_id: str,
        stop: Decimal,
        quantity: Decimal,
        now: datetime,
        detail: str,
    ) -> ReplacementOutcome:
        return ReplacementOutcome(
            status,
            order_id,
            authoritative_order_id=order_id,
            authoritative_stop_price=stop,
            authoritative_quantity=quantity,
            observed_at=now,
            detail=detail,
        )

    def _capability_error(self, qty: Decimal, tif: TimeInForce) -> str | None:
        try:
            standalone = self._capability("supports_standalone_stop", qty, tif)
            replacement = self._capability("supports_replace", qty, tif)
        except Exception as exc:
            return f"capability query failed closed: {exc!r}"
        if not standalone:
            return "provider lacks a verified standalone-stop capability for this combination"
        if not replacement:
            return "provider lacks a verified stop-replace capability for this combination"
        return None

    def _capability(self, name: str, qty: Decimal, tif: TimeInForce) -> bool:
        member = getattr(self._port, name)
        value = member(qty=qty, tif=tif) if callable(member) else member
        if inspect.isawaitable(value):
            raise TypeError("asynchronous capability query is not supported")
        return value is True

    def _round_down(self, candidate: Decimal) -> Decimal:
        member = self._port.price_increment
        increment = member() if callable(member) else member
        tick = _decimal(increment, "price_increment", positive=True)
        rounded = (candidate / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        return _decimal(rounded, "rounded candidate", positive=True)

    def _safe_lookup(self, order_id: str, *, now: datetime) -> ReplacementOutcome:
        try:
            return self._require_outcome(self._port.lookup_order(order_id))
        except Exception as exc:
            return ReplacementOutcome(
                ReplacementStatus.UNKNOWN,
                order_id,
                observed_at=now,
                detail=f"order lookup failed: {exc!r}",
            )

    @staticmethod
    def _require_outcome(value: object) -> ReplacementOutcome:
        if inspect.isawaitable(value):
            raise TypeError("asynchronous stop replacement ports are not supported")
        if not isinstance(value, ReplacementOutcome):
            raise TypeError("stop replacement port must return ReplacementOutcome")
        return value

    def _record_intent(self, intent: Mapping[str, object]) -> None:
        recorder = self._intent_recorder
        method = getattr(recorder, "record_replacement_intent", None)
        result = method(intent) if callable(method) else recorder(intent)  # type: ignore[operator]
        if inspect.isawaitable(result):
            raise TypeError("asynchronous intent recorders are not supported")

    def _now(self) -> datetime:
        source = self._clock
        value: object = source() if callable(source) else source.now()
        return _utc(value, "clock result")

    @staticmethod
    def _tif(value: TimeInForce | str) -> TimeInForce:
        if isinstance(value, TimeInForce):
            return value
        if isinstance(value, str):
            try:
                return TimeInForce[value.strip().upper()]
            except KeyError:
                pass
        raise ValueError("tif must be TimeInForce.DAY or TimeInForce.GTC")
