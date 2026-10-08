"""Bounded HTTP client for the Alpaca individual Trading API.

All requests carry explicit finite connect/read timeouts and the
``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY`` headers; the secret is never
logged. Only ``GET`` requests are retried (bounded exponential backoff with
jitter, honoring ``Retry-After`` / ``X-RateLimit-Reset``). ``POST``, ``PATCH``,
and ``DELETE`` are never retried: a timeout or connection loss on a
non-idempotent request surfaces an unknown-outcome error so callers resolve
the outcome via ``client_order_id`` lookup. JSON bodies parse numbers to
:class:`~decimal.Decimal`, never ``float``.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from mike_trade.brokers.alpaca.auth import (
    AlpacaCredentials,
    AlpacaEnvironment,
    EnvironmentMismatchError,
    LiveAuthorizationRequired,
    credential_fingerprint,
)
from mike_trade.domain.errors import BrokerError

__all__ = [
    "AlpacaHttpClient",
    "AlpacaApiError",
    "AlpacaAuthError",
    "AlpacaNotFound",
    "AlpacaUnprocessable",
    "AlpacaRateLimited",
    "AlpacaServerError",
    "AlpacaTimeout",
    "AlpacaTransportError",
    "AlpacaUnknownOutcome",
    "RetryPolicy",
]

_logger = logging.getLogger(__name__)

_KEY_ID_HEADER = "APCA-API-KEY-ID"
_SECRET_HEADER = "APCA-API-SECRET-KEY"
_REDACTED = "***REDACTED***"

_NUMERIC_STRING_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")

# Monetary / quantity / price fields whose JSON numeric strings (or plain
# JSON integers) must decode to Decimal rather than str/int/float.
_DECIMAL_FIELDS = frozenset(
    {
        "qty",
        "filled_qty",
        "qty_available",
        "notional",
        "price",
        "filled_avg_price",
        "limit_price",
        "stop_price",
        "trail_price",
        "trail_percent",
        "amount",
        "cash",
        "cash_withdrawable",
        "equity",
        "buying_power",
        "daytrading_buying_power",
        "regt_buying_power",
        "portfolio_value",
        "last_equity",
        "multiplier",
        "initial_margin",
        "maintenance_margin",
        "sma",
        "long_market_value",
        "short_market_value",
        "avg_entry_price",
        "market_value",
        "cost_basis",
        "current_price",
        "lastday_price",
        "change_today",
        "unrealized_pl",
        "unrealized_plpc",
        "unrealized_intraday_pl",
        "unrealized_intraday_plpc",
        "accrued_fees",
        "pending_transfer_in",
        "pending_transfer_out",
        "balance",
        "held_for_orders",
        "available",
        "fee",
        "fees",
    }
)


class AlpacaApiError(BrokerError):
    """Base Alpaca provider error with status, error code, message, request id."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: Any | None = None,
        request_id: str | None = None,
    ) -> None:
        context: dict[str, Any] = {
            "status_code": status_code,
            "error_code": error_code,
            "request_id": request_id,
        }
        super().__init__(
            message,
            code=str(error_code) if error_code is not None else None,
            context=context,
        )
        self.status_code = status_code
        self.error_code = error_code
        self.request_id = request_id


class AlpacaAuthError(AlpacaApiError):
    """HTTP 401/403: key rejected, revoked, or lacking permission."""


class AlpacaNotFound(AlpacaApiError):
    """HTTP 404."""


class AlpacaUnprocessable(AlpacaApiError):
    """HTTP 422: request failed Alpaca-side validation."""


class AlpacaRateLimited(AlpacaApiError):
    """HTTP 429, carrying retry-after and rate-limit-reset hints."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: Any | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
        rate_limit_reset: float | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            error_code=error_code,
            request_id=request_id,
        )
        self.retry_after = retry_after
        self.rate_limit_reset = rate_limit_reset


class AlpacaServerError(AlpacaApiError):
    """HTTP 5xx."""


class AlpacaTimeout(AlpacaApiError):
    """A request timed out. ``unknown_outcome`` marks non-idempotent requests."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: Any | None = None,
        request_id: str | None = None,
        unknown_outcome: bool = False,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            error_code=error_code,
            request_id=request_id,
        )
        self.unknown_outcome = unknown_outcome


class AlpacaUnknownOutcome(AlpacaTimeout):
    """Timeout/connection loss on a non-idempotent request: outcome unknown.

    Callers must resolve the outcome via ``client_order_id`` lookup instead
    of retrying the mutation.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: Any | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            error_code=error_code,
            request_id=request_id,
            unknown_outcome=True,
        )


class AlpacaTransportError(AlpacaApiError):
    """Connection-level failure (DNS, refused, reset) with no HTTP response."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff for idempotent GET reads."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    jitter_ratio: float = 0.25

    def __post_init__(self) -> None:
        if not isinstance(self.max_attempts, int) or self.max_attempts < 1:
            raise ValueError("max_attempts must be an int >= 1")
        for name in ("base_delay_seconds", "max_delay_seconds", "jitter_ratio"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
            if float(value) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.jitter_ratio > 1:
            raise ValueError("jitter_ratio must be <= 1")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")

    def delay_for_attempt(self, attempt: int, rng: Callable[[], float] | None = None) -> float:
        """Backoff delay after failed attempt ``attempt`` (1-based)."""
        raw = float(self.base_delay_seconds) * (2.0 ** (attempt - 1))
        capped = min(raw, float(self.max_delay_seconds))
        if rng is not None and self.jitter_ratio > 0:
            draw = float(rng())
            capped = capped * (1.0 + float(self.jitter_ratio) * (2.0 * draw - 1.0))
        return max(0.0, capped)


def _coerce_decimal_strings(value: Any) -> Any:
    """Convert numeric strings/ints in known money/qty fields to Decimal."""
    if isinstance(value, dict):
        return {
            key: (
                _to_decimal(item)
                if key in _DECIMAL_FIELDS and isinstance(item, (str, int)) and not isinstance(item, bool)
                else _coerce_decimal_strings(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_coerce_decimal_strings(item) for item in value]
    return value


def _to_decimal(item: str | int) -> Any:
    if isinstance(item, int):
        return Decimal(item)
    text = item.strip()
    if _NUMERIC_STRING_RE.match(text):
        try:
            return Decimal(text)
        except Exception:
            return item
    return item


def _parse_decimal_json(text: str) -> Any:
    """Parse a JSON document with numbers as Decimal, never float."""
    if not text.strip():
        return None
    try:
        parsed = json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise AlpacaTransportError(f"Malformed JSON response from Alpaca: {exc}") from exc
    return _coerce_decimal_strings(parsed)


def _parse_retry_after(value: str | None, now: float) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        reset_at = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if reset_at is None:
        return None
    timestamp = reset_at.timestamp()
    return max(0.0, timestamp - now)


def _parse_rate_limit_reset(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


class AlpacaHttpClient:
    """Authenticated, bounded client for the Alpaca Trading API."""

    def __init__(
        self,
        credentials: AlpacaCredentials,
        *,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        http_client: httpx.Client | None = None,
        connect_timeout: float = 5.0,
        read_timeout: float = 15.0,
        write_timeout: float = 10.0,
        pool_timeout: float = 5.0,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] | None = None,
        live_authorized: bool = False,
        persisted_fingerprint: str | None = None,
    ) -> None:
        if not isinstance(credentials, AlpacaCredentials):
            raise TypeError("credentials must be an AlpacaCredentials")
        for name, value in (
            ("connect_timeout", connect_timeout),
            ("read_timeout", read_timeout),
            ("write_timeout", write_timeout),
            ("pool_timeout", pool_timeout),
        ):
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"{name} must be a finite number of seconds")
            if float(value) <= 0:
                raise ValueError(f"{name} must be positive")
        if retry_policy is not None and not isinstance(retry_policy, RetryPolicy):
            raise TypeError("retry_policy must be a RetryPolicy")
        if not callable(sleep):
            raise TypeError("sleep must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if rng is not None and not callable(rng):
            raise TypeError("rng must be callable")

        resolved_base = (base_url if base_url is not None else credentials.environment.base_url).rstrip("/")
        self._ensure_consistent_base_url(credentials, resolved_base)
        if credentials.environment is AlpacaEnvironment.LIVE and not live_authorized:
            raise LiveAuthorizationRequired(
                "Refusing to build a LIVE Alpaca client without explicit "
                "live authorization (pass live_authorized=True)."
            )
        if persisted_fingerprint is not None and persisted_fingerprint != credential_fingerprint(credentials):
            raise EnvironmentMismatchError(
                "Persisted account fingerprint does not match these credentials; "
                "paper and live state are never shared."
            )

        self._credentials = credentials
        self._base_url = resolved_base
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._timeout = httpx.Timeout(
            connect=float(connect_timeout),
            read=float(read_timeout),
            write=float(write_timeout),
            pool=float(pool_timeout),
        )
        self._connect_timeout = float(connect_timeout)
        self._read_timeout = float(read_timeout)
        if http_client is not None:
            if transport is not None:
                raise ValueError("pass either http_client or transport, not both")
            self._client = http_client
            self._owns_client = False
        else:
            self._client = httpx.Client(transport=transport, timeout=self._timeout)
            self._owns_client = True

    @staticmethod
    def _ensure_consistent_base_url(credentials: AlpacaCredentials, base_url: str) -> None:
        host = (urlsplit(base_url).hostname or "").lower()
        if host == "broker-api.alpaca.markets" or host.startswith("broker-api."):
            raise EnvironmentMismatchError(
                "Alpaca Broker API (enterprise) endpoints are forbidden; "
                "use the individual Trading API only."
            )
        expected = credentials.environment.base_url.rstrip("/")
        if base_url != expected:
            raise EnvironmentMismatchError(
                f"Credential environment {credentials.environment.value} does not match "
                f"base URL {base_url!r}; expected {expected!r}."
            )

    @property
    def credentials(self) -> AlpacaCredentials:
        return self._credentials

    @property
    def environment(self) -> AlpacaEnvironment:
        return self._credentials.environment

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def fingerprint(self) -> str:
        return credential_fingerprint(self._credentials)

    @property
    def retry_policy(self) -> RetryPolicy:
        return self._retry_policy

    @property
    def httpx_timeout(self) -> httpx.Timeout:
        return self._timeout

    def auth_headers(self) -> dict[str, str]:
        """Live auth headers. Handle with care: never log the returned mapping."""
        return {
            _KEY_ID_HEADER: self._credentials.key_id,
            _SECRET_HEADER: self._credentials.secret_key,
        }

    def redacted_headers(self) -> dict[str, str]:
        """Auth headers with the secret redacted; safe for logging."""
        return {_KEY_ID_HEADER: self._credentials.key_id, _SECRET_HEADER: _REDACTED}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> AlpacaHttpClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- request entry points -------------------------------------------------

    def get(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, *, json_body: Any = None, params: Mapping[str, Any] | None = None) -> Any:
        return self.request("POST", path, json_body=json_body, params=params)

    def patch(self, path: str, *, json_body: Any = None, params: Mapping[str, Any] | None = None) -> Any:
        return self.request("PATCH", path, json_body=json_body, params=params)

    def delete(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        return self.request("DELETE", path, params=params)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        """Send one bounded request; retry only idempotent GET reads."""
        verb = method.upper()
        url = self._base_url + "/" + path.lstrip("/")
        retryable = verb == "GET"
        max_attempts = self._retry_policy.max_attempts if retryable else 1
        attempt = 0
        while True:
            attempt += 1
            _logger.debug(
                "alpaca request",
                extra={
                    "method": verb,
                    "url": url,
                    "attempt": attempt,
                    "headers": self.redacted_headers(),
                },
            )
            try:
                response = self._client.request(
                    verb,
                    url,
                    headers=self.auth_headers(),
                    params=dict(params) if params else None,
                    json=json_body,
                    timeout=self._timeout,
                )
            except httpx.TimeoutException as exc:
                if retryable and attempt < max_attempts:
                    self._sleep(self._retry_policy.delay_for_attempt(attempt, self._rng))
                    continue
                raise self._timeout_error(verb, url, exc) from exc
            except httpx.TransportError as exc:
                if retryable and attempt < max_attempts:
                    self._sleep(self._retry_policy.delay_for_attempt(attempt, self._rng))
                    continue
                raise self._transport_error(verb, url, exc) from exc
            except httpx.RequestError as exc:
                if retryable and attempt < max_attempts:
                    self._sleep(self._retry_policy.delay_for_attempt(attempt, self._rng))
                    continue
                raise self._transport_error(verb, url, exc) from exc

            _logger.debug(
                "alpaca response",
                extra={
                    "method": verb,
                    "url": url,
                    "attempt": attempt,
                    "status_code": response.status_code,
                },
            )
            status = response.status_code
            if 200 <= status < 300:
                return _parse_decimal_json(response.text)
            if status == 429:
                error = self._rate_limit_error(response)
                if retryable and attempt < max_attempts:
                    self._sleep(self._rate_limit_wait(response, attempt))
                    continue
                raise error
            if 500 <= status < 600:
                if retryable and attempt < max_attempts:
                    self._sleep(self._retry_policy.delay_for_attempt(attempt, self._rng))
                    continue
                raise self._map_status_error(response)
            raise self._map_status_error(response)

    # -- error mapping --------------------------------------------------------

    @staticmethod
    def _timeout_error(verb: str, url: str, exc: BaseException) -> AlpacaTimeout:
        if verb == "GET":
            return AlpacaTimeout(f"GET {url} timed out after bounded retries: {exc}")
        return AlpacaUnknownOutcome(
            f"{verb} {url} timed out; outcome is UNKNOWN. "
            "Do not retry the mutation; resolve via client_order_id lookup."
        )

    @staticmethod
    def _transport_error(verb: str, url: str, exc: BaseException) -> AlpacaApiError:
        if verb == "GET":
            return AlpacaTransportError(f"GET {url} transport failure: {exc}")
        return AlpacaUnknownOutcome(
            f"{verb} {url} connection failed; outcome is UNKNOWN. "
            "Do not retry the mutation; resolve via client_order_id lookup."
        )

    @staticmethod
    def _error_payload(response: httpx.Response) -> tuple[Any | None, str]:
        code: Any | None = None
        message = f"Alpaca request failed with status {response.status_code}"
        try:
            data = json.loads(response.text, parse_float=Decimal) if response.text.strip() else None
        except json.JSONDecodeError:
            return None, message
        if isinstance(data, dict):
            raw_code = data.get("code")
            raw_message = data.get("message")
            if raw_code is not None:
                code = raw_code
            if isinstance(raw_message, str) and raw_message.strip():
                message = raw_message.strip()
        return code, message

    @staticmethod
    def _request_id(response: httpx.Response) -> str | None:
        return response.headers.get("x-request-id")

    def _map_status_error(self, response: httpx.Response) -> AlpacaApiError:
        code, message = self._error_payload(response)
        request_id = self._request_id(response)
        status = response.status_code
        if status in (401, 403):
            return AlpacaAuthError(message, status_code=status, error_code=code, request_id=request_id)
        if status == 404:
            return AlpacaNotFound(message, status_code=status, error_code=code, request_id=request_id)
        if status == 422:
            return AlpacaUnprocessable(message, status_code=status, error_code=code, request_id=request_id)
        if status == 429:
            return self._rate_limit_error(response)
        if 500 <= status < 600:
            return AlpacaServerError(message, status_code=status, error_code=code, request_id=request_id)
        return AlpacaApiError(message, status_code=status, error_code=code, request_id=request_id)

    def _rate_limit_error(self, response: httpx.Response) -> AlpacaRateLimited:
        code, message = self._error_payload(response)
        retry_after = _parse_retry_after(response.headers.get("retry-after"), self._clock())
        reset = _parse_rate_limit_reset(response.headers.get("x-ratelimit-reset"))
        return AlpacaRateLimited(
            message,
            status_code=response.status_code,
            error_code=code,
            request_id=self._request_id(response),
            retry_after=retry_after,
            rate_limit_reset=reset,
        )

    def _rate_limit_wait(self, response: httpx.Response, attempt: int) -> float:
        backoff = self._retry_policy.delay_for_attempt(attempt, self._rng)
        waits = [backoff]
        retry_after = _parse_retry_after(response.headers.get("retry-after"), self._clock())
        if retry_after is not None:
            waits.append(retry_after)
        reset = _parse_rate_limit_reset(response.headers.get("x-ratelimit-reset"))
        if reset is not None:
            waits.append(max(0.0, reset - self._clock()))
        return max(waits)
