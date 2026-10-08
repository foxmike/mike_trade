"""Normalize Alpaca ``trade_updates`` and manage a read-only stream session.

The websocket transport is injected and intentionally tiny, allowing contract
tests to replay JSON without opening a socket.  This module sends only Alpaca's
authentication and ``listen`` frames; it has no order mutation operations.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from mike_trade.brokers.alpaca.auth import (
    AlpacaCredentials,
    AlpacaEnvironment,
    EnvironmentMismatchError,
    credential_fingerprint,
)
from mike_trade.brokers.alpaca.orders import AlpacaOrder, _normalize_order
from mike_trade.domain.errors import BrokerError

__all__ = [
    "TradeUpdate",
    "TradeUpdateEvent",
    "TradeUpdateStream",
    "StreamReconnected",
    "StreamAuthError",
    "ReconnectPolicy",
]


class TradeUpdateEvent(StrEnum):
    NEW = "new"
    ACCEPTED = "accepted"
    PENDING_NEW = "pending_new"
    PARTIAL_FILL = "partial_fill"
    FILL = "fill"
    REJECTED = "rejected"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REPLACED = "replaced"
    PENDING_CANCEL = "pending_cancel"
    PENDING_REPLACE = "pending_replace"
    DONE_FOR_DAY = "done_for_day"
    STOPPED = "stopped"
    SUSPENDED = "suspended"
    CALCULATED = "calculated"
    UNKNOWN = "unknown"


_KNOWN_EVENTS = {
    member.value: member
    for member in TradeUpdateEvent
    if member is not TradeUpdateEvent.UNKNOWN
}


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, field=field)


def _decimal(value: Any, *, field: str) -> Decimal | None:
    if value is None:
        return None
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


def _timestamp(value: Any, *, field: str = "timestamp") -> datetime:
    text = _require_text(value, field=field)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field} is not a valid timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _environment(value: Any) -> AlpacaEnvironment:
    raw = getattr(value, "value", value)
    text = _require_text(raw, field="environment").upper()
    try:
        return AlpacaEnvironment(text)
    except ValueError:
        raise EnvironmentMismatchError(f"unknown Alpaca stream environment {text!r}") from None


def _decode_message(message: Any) -> Mapping[str, Any]:
    if isinstance(message, bytes):
        message = message.decode("utf-8")
    if isinstance(message, str):
        try:
            message = json.loads(message, parse_float=Decimal, parse_int=Decimal)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("websocket message is not valid JSON") from None
    if not isinstance(message, Mapping):
        raise ValueError("websocket message must be a JSON object")
    return message


def _payload(message: Mapping[str, Any]) -> Mapping[str, Any]:
    stream_name = message.get("stream", message.get("T"))
    data = message.get("data")
    if stream_name == "trade_updates":
        if data is None:
            return message
        if not isinstance(data, Mapping):
            raise ValueError("trade_updates data must be a JSON object")
        return data
    # Accept an already-unwrapped fixture or transport payload.
    if "event" in message and "order" in message:
        return message
    raise ValueError("message is not a trade_updates event")


def _assert_message_scope(
    message: Mapping[str, Any],
    data: Mapping[str, Any],
    *,
    environment: AlpacaEnvironment,
    fingerprint: str,
) -> None:
    order = data.get("order")
    sources = [message, data]
    if isinstance(order, Mapping):
        sources.append(order)
    for source in sources:
        for name in ("environment", "account_environment", "source_environment"):
            if source.get(name) is not None and _environment(source[name]) is not environment:
                raise EnvironmentMismatchError(
                    f"trade update environment {source[name]!r} does not match "
                    f"session environment {environment.value}"
                )
        for name in ("fingerprint", "account_fingerprint"):
            if source.get(name) is not None and source[name] != fingerprint:
                raise EnvironmentMismatchError(
                    "trade update account fingerprint does not match stream session"
                )


@dataclass(frozen=True, slots=True)
class TradeUpdate:
    environment: AlpacaEnvironment
    fingerprint: str
    event: TradeUpdateEvent
    raw_event: str
    order: AlpacaOrder
    timestamp: datetime
    execution_id: str | None = None
    price: Decimal | None = None
    qty: Decimal | None = None
    position_qty: Decimal | None = None
    reason: str | None = None
    raw: Mapping[str, Any] | None = None

    @classmethod
    def from_message(
        cls,
        message: Mapping[str, Any] | str | bytes,
        *,
        environment: AlpacaEnvironment,
        fingerprint: str,
    ) -> TradeUpdate:
        """Normalize one envelope (or already-unwrapped) trade update."""

        if not isinstance(environment, AlpacaEnvironment):
            raise TypeError("environment must be an AlpacaEnvironment")
        resolved_fingerprint = _require_text(fingerprint, field="fingerprint")
        envelope = _decode_message(message)
        data = _payload(envelope)
        _assert_message_scope(
            envelope,
            data,
            environment=environment,
            fingerprint=resolved_fingerprint,
        )
        raw_event = _require_text(data.get("event"), field="event").lower()
        event = _KNOWN_EVENTS.get(raw_event, TradeUpdateEvent.UNKNOWN)
        raw_order = data.get("order")
        if not isinstance(raw_order, Mapping):
            raise ValueError("trade update order must be a JSON object")
        return cls(
            environment=environment,
            fingerprint=resolved_fingerprint,
            event=event,
            raw_event=raw_event,
            order=_normalize_order(raw_order, environment=environment),
            timestamp=_timestamp(data.get("timestamp")),
            execution_id=_optional_text(data.get("execution_id"), field="execution_id"),
            price=_decimal(data.get("price"), field="price"),
            qty=_decimal(data.get("qty"), field="qty"),
            position_qty=_decimal(data.get("position_qty"), field="position_qty"),
            reason=_optional_text(data.get("reason"), field="reason"),
            raw=dict(envelope),
        )

    @property
    def is_unknown(self) -> bool:
        return self.event is TradeUpdateEvent.UNKNOWN

    @property
    def incremental_qty(self) -> Decimal | None:
        """Execution quantity in this message (distinct from order.filled_qty)."""

        return self.qty

    @property
    def cumulative_qty(self) -> Decimal | None:
        return self.order.filled_qty


class StreamAuthError(BrokerError):
    """Alpaca rejected websocket authentication or the listen request."""

    def __init__(self, message: str, *, response: Mapping[str, Any] | None = None) -> None:
        self.response = None if response is None else dict(response)
        super().__init__(message, code="alpaca_stream_auth", context={})


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    """Maximum reconnect attempts and bounded exponential delay with jitter."""

    max_attempts: int = 5
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    jitter_ratio: float = 0.25

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise ValueError("max_attempts must be an int >= 1")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be an int >= 1")
        for name in ("base_delay_seconds", "max_delay_seconds", "jitter_ratio"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite non-negative number")
            if not math.isfinite(float(value)) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")
        if self.jitter_ratio > 1:
            raise ValueError("jitter_ratio must be <= 1")

    def delay_for_attempt(
        self, attempt: int, rng: Callable[[], float] | None = None
    ) -> float:
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("attempt must be an int >= 1")
        base = min(
            float(self.max_delay_seconds),
            float(self.base_delay_seconds) * (2.0 ** (attempt - 1)),
        )
        if rng is not None and self.jitter_ratio:
            draw = float(rng())
            if not math.isfinite(draw) or not 0 <= draw <= 1:
                raise ValueError("rng must return a finite value from 0 through 1")
            base *= 1.0 + float(self.jitter_ratio) * (2.0 * draw - 1.0)
        return max(0.0, min(float(self.max_delay_seconds), base))


@dataclass(frozen=True, slots=True)
class StreamReconnected:
    """Explicit gap marker: REST reconciliation is required after reconnect."""

    environment: AlpacaEnvironment
    fingerprint: str
    attempt: int
    reconnected_at: datetime
    last_seen_timestamp: datetime | None
    event: str = "gap"
    requires_reconciliation: bool = True
    reconcile: tuple[str, ...] = (
        "open_orders",
        "orders_by_client_order_id",
        "activities_since_last_seen",
    )

    @property
    def gap_since(self) -> datetime | None:
        return self.last_seen_timestamp


class TradeUpdateStream:
    """Authenticate, listen, normalize, and reconnect an injected transport."""

    def __init__(
        self,
        credentials: AlpacaCredentials,
        transport: Any,
        *,
        environment: AlpacaEnvironment | None = None,
        fingerprint: str | None = None,
        reconnect_policy: ReconnectPolicy | None = None,
        sleep: Callable[[float], Awaitable[None] | None] | None = None,
        rng: Callable[[], float] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(credentials, AlpacaCredentials):
            raise TypeError("credentials must be AlpacaCredentials")
        if not callable(getattr(transport, "connect", None)):
            raise TypeError("transport must expose connect(url)")
        resolved_environment = environment or credentials.environment
        if not isinstance(resolved_environment, AlpacaEnvironment):
            raise TypeError("environment must be an AlpacaEnvironment")
        if resolved_environment is not credentials.environment:
            raise EnvironmentMismatchError(
                "stream environment does not match credential environment"
            )
        resolved_fingerprint = fingerprint or credential_fingerprint(credentials)
        self._credentials = credentials
        self._transport = transport
        self._environment = resolved_environment
        self._fingerprint = _require_text(resolved_fingerprint, field="fingerprint")
        self._policy = reconnect_policy or ReconnectPolicy()
        if not isinstance(self._policy, ReconnectPolicy):
            raise TypeError("reconnect_policy must be ReconnectPolicy")
        self._sleep = sleep or asyncio.sleep
        if not callable(self._sleep):
            raise TypeError("sleep must be callable")
        self._rng = rng or random.random
        if not callable(self._rng):
            raise TypeError("rng must be callable")
        self._clock = clock or (lambda: datetime.now(UTC))
        if not callable(self._clock):
            raise TypeError("clock must be callable")

    @property
    def environment(self) -> AlpacaEnvironment:
        return self._environment

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def normalize(self, message: Mapping[str, Any] | str | bytes) -> TradeUpdate:
        return TradeUpdate.from_message(
            message, environment=self._environment, fingerprint=self._fingerprint
        )

    def __aiter__(self) -> AsyncIterator[TradeUpdate | StreamReconnected]:
        return self.events()

    async def events(self) -> AsyncIterator[TradeUpdate | StreamReconnected]:
        """Yield updates forever, making every recovered disconnect explicit."""

        connection = await self._open_and_subscribe()
        last_seen: datetime | None = None
        try:
            while True:
                try:
                    message = await self._receive(connection)
                except (ConnectionError, EOFError, OSError, StopAsyncIteration) as exc:
                    await self._close(connection)
                    connection = await self._reconnect(exc)
                    marker_time = self._clock()
                    if marker_time.tzinfo is None or marker_time.utcoffset() is None:
                        raise ValueError(
                            "clock must return a timezone-aware datetime"
                        ) from None
                    yield StreamReconnected(
                        environment=self._environment,
                        fingerprint=self._fingerprint,
                        attempt=self._last_reconnect_attempt,
                        reconnected_at=marker_time.astimezone(UTC),
                        last_seen_timestamp=last_seen,
                    )
                    continue
                update = self.normalize(message)
                last_seen = update.timestamp
                yield update
        finally:
            await self._close(connection)

    async def _reconnect(self, original: BaseException) -> Any:
        last_error: BaseException = original
        for attempt in range(1, self._policy.max_attempts + 1):
            delay = self._policy.delay_for_attempt(attempt, self._rng)
            await _maybe_await(self._sleep(delay))
            try:
                connection = await self._open_and_subscribe()
            except StreamAuthError:
                raise
            except (ConnectionError, EOFError, OSError, StopAsyncIteration) as exc:
                last_error = exc
                continue
            self._last_reconnect_attempt = attempt
            return connection
        raise last_error

    async def _open_and_subscribe(self) -> Any:
        connection = await _maybe_await(
            self._transport.connect(self._environment.trade_stream_url)
        )
        if connection is None:
            connection = self._transport
        await self._send(
            connection,
            {
                "action": "auth",
                "key": self._credentials.key_id,
                "secret": self._credentials.secret_key,
            },
        )
        auth_response = _decode_message(await self._receive(connection))
        auth_data = auth_response.get("data")
        authorized = (
            auth_response.get("stream") == "authorization"
            and isinstance(auth_data, Mapping)
            and str(auth_data.get("status", "")).lower() == "authorized"
        )
        if not authorized:
            raise StreamAuthError(
                "Alpaca trade_updates authentication failed", response=auth_response
            )
        await self._send(
            connection,
            {"action": "listen", "data": {"streams": ["trade_updates"]}},
        )
        listen_response = _decode_message(await self._receive(connection))
        listen_data = listen_response.get("data")
        if listen_response.get("stream") == "authorization":
            raise StreamAuthError(
                "Alpaca rejected the trade_updates listen request",
                response=listen_response,
            )
        streams = listen_data.get("streams") if isinstance(listen_data, Mapping) else None
        if listen_response.get("stream") != "listening" or not isinstance(streams, list):
            raise StreamAuthError(
                "Alpaca did not acknowledge the trade_updates subscription",
                response=listen_response,
            )
        if "trade_updates" not in streams:
            raise StreamAuthError(
                "Alpaca listening acknowledgement omitted trade_updates",
                response=listen_response,
            )
        return connection

    @staticmethod
    async def _send(connection: Any, payload: Mapping[str, Any]) -> None:
        if callable(getattr(connection, "send_json", None)):
            await _maybe_await(connection.send_json(dict(payload)))
            return
        if callable(getattr(connection, "send", None)):
            await _maybe_await(connection.send(json.dumps(payload)))
            return
        raise TypeError("websocket connection must expose send_json() or send()")

    @staticmethod
    async def _receive(connection: Any) -> Any:
        for name in ("receive_json", "recv_json", "receive", "recv"):
            method = getattr(connection, name, None)
            if callable(method):
                return await _maybe_await(method())
        raise TypeError(
            "websocket connection must expose receive_json(), recv_json(), receive(), or recv()"
        )

    @staticmethod
    async def _close(connection: Any) -> None:
        close = getattr(connection, "close", None)
        if callable(close):
            await _maybe_await(close())


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
