"""Contracts for the Alpaca Trading API auth + bounded HTTP foundation.

Fixture and mock transports only: no network access and no real credentials.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

import httpx
import pytest

from mike_trade.brokers.alpaca.auth import (
    AlpacaCredentials,
    AlpacaEnvironment,
    EnvironmentMismatchError,
    LiveAuthorizationRequired,
    credential_fingerprint,
)
from mike_trade.brokers.alpaca.http import (
    AlpacaApiError,
    AlpacaAuthError,
    AlpacaHttpClient,
    AlpacaNotFound,
    AlpacaRateLimited,
    AlpacaServerError,
    AlpacaTimeout,
    AlpacaTransportError,
    AlpacaUnknownOutcome,
    AlpacaUnprocessable,
    RetryPolicy,
)

PAPER_KEY = "PAPERKEY123"
PAPER_SECRET = "paper-secret-value-xyz"
LIVE_KEY = "LIVEKEY456"
LIVE_SECRET = "live-secret-value-abc"


def paper_creds() -> AlpacaCredentials:
    return AlpacaCredentials(key_id=PAPER_KEY, secret_key=PAPER_SECRET, environment=AlpacaEnvironment.PAPER)


def live_creds() -> AlpacaCredentials:
    return AlpacaCredentials(key_id=LIVE_KEY, secret_key=LIVE_SECRET, environment=AlpacaEnvironment.LIVE)


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def sequenced_transport(
    script: list[Any],
    calls: list[httpx.Request],
    *,
    default_headers: dict[str, str] | None = None,
) -> httpx.MockTransport:
    """Build a MockTransport replaying queued responses/exceptions in order."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        action = script[min(len(calls) - 1, len(script) - 1)]
        if isinstance(action, BaseException):
            raise action
        status, body, headers = action
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body, headers=headers, request=request)
        return httpx.Response(status, content=body, headers=headers, request=request)

    _ = default_headers
    return httpx.MockTransport(handler)


def make_client(
    creds: AlpacaCredentials,
    script: list[Any],
    calls: list[httpx.Request],
    *,
    sleep: FakeSleep | None = None,
    clock: Any = None,
    live_authorized: bool = False,
    **kwargs: Any,
) -> tuple[AlpacaHttpClient, FakeSleep]:
    fake_sleep = sleep if sleep is not None else FakeSleep()
    transport = sequenced_transport(script, calls)
    client = AlpacaHttpClient(
        creds,
        transport=transport,
        sleep=fake_sleep,
        clock=(lambda: 1000.0) if clock is None else clock,
        live_authorized=live_authorized,
        **kwargs,
    )
    return client, fake_sleep


def timeout_exc(request: httpx.Request) -> httpx.ConnectTimeout:
    return httpx.ConnectTimeout("connect timed out", request=request)


# -- environments and URLs ----------------------------------------------------


def test_environment_base_urls_are_distinct_trading_hosts() -> None:
    assert AlpacaEnvironment.PAPER.base_url == "https://paper-api.alpaca.markets"
    assert AlpacaEnvironment.LIVE.base_url == "https://api.alpaca.markets"
    assert AlpacaEnvironment.PAPER.base_url != AlpacaEnvironment.LIVE.base_url
    assert list(AlpacaEnvironment) == [AlpacaEnvironment.PAPER, AlpacaEnvironment.LIVE]


def test_trade_stream_urls_defined_per_environment() -> None:
    assert AlpacaEnvironment.PAPER.trade_stream_url == "wss://paper-api.alpaca.markets/stream"
    assert AlpacaEnvironment.LIVE.trade_stream_url == "wss://api.alpaca.markets/stream"


def test_no_broker_api_hostname_anywhere() -> None:
    for env in AlpacaEnvironment:
        assert "broker-api" not in env.base_url
        assert "broker-api" not in env.trade_stream_url


def test_from_base_url_resolves_environments() -> None:
    assert AlpacaEnvironment.from_base_url("https://paper-api.alpaca.markets") is AlpacaEnvironment.PAPER
    assert AlpacaEnvironment.from_base_url("https://api.alpaca.markets/") is AlpacaEnvironment.LIVE


def test_broker_api_host_rejected() -> None:
    with pytest.raises(EnvironmentMismatchError):
        AlpacaEnvironment.from_base_url("https://broker-api.alpaca.markets/v2/accounts")
    with pytest.raises(EnvironmentMismatchError):
        AlpacaHttpClient(paper_creds(), base_url="https://broker-api.alpaca.markets")


def test_client_defaults_to_environment_base_url() -> None:
    client = AlpacaHttpClient(paper_creds(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}, request=r)))
    try:
        assert client.base_url == "https://paper-api.alpaca.markets"
    finally:
        client.close()
    live = AlpacaHttpClient(
        live_creds(), live_authorized=True,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}, request=r)),
    )
    try:
        assert live.base_url == "https://api.alpaca.markets"
    finally:
        live.close()


def test_cross_environment_credential_mismatch() -> None:
    with pytest.raises(EnvironmentMismatchError):
        AlpacaHttpClient(paper_creds(), base_url="https://api.alpaca.markets")
    with pytest.raises(EnvironmentMismatchError):
        AlpacaHttpClient(live_creds(), base_url="https://paper-api.alpaca.markets", live_authorized=True)


def test_persisted_fingerprint_mismatch_rejected() -> None:
    creds = paper_creds()
    good = credential_fingerprint(creds)
    other_fp = credential_fingerprint(live_creds())
    with pytest.raises(EnvironmentMismatchError):
        AlpacaHttpClient(creds, persisted_fingerprint=other_fp)
    with pytest.raises(EnvironmentMismatchError):
        AlpacaHttpClient(creds, persisted_fingerprint="0" * 16)
    client = AlpacaHttpClient(creds, persisted_fingerprint=good)
    client.close()
    assert client.fingerprint == good


# -- fingerprints and redaction -----------------------------------------------


def test_fingerprint_stable_and_excludes_secret() -> None:
    first = credential_fingerprint(paper_creds())
    again = credential_fingerprint(AlpacaCredentials(key_id=PAPER_KEY, secret_key="different-secret", environment=AlpacaEnvironment.PAPER))
    assert first == again
    assert PAPER_SECRET not in first
    assert "different-secret" not in again
    live_fp = credential_fingerprint(AlpacaCredentials(key_id=PAPER_KEY, secret_key=PAPER_SECRET, environment=AlpacaEnvironment.LIVE))
    assert live_fp != first  # same key, different environment -> differs


def test_secret_redacted_in_repr_and_str() -> None:
    creds = paper_creds()
    assert PAPER_SECRET not in repr(creds)
    assert PAPER_SECRET not in str(creds)
    assert PAPER_KEY in repr(creds)


def test_secret_redacted_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[httpx.Request] = []
    client, _ = make_client(paper_creds(), [(200, {"ok": True}, {})], calls)
    try:
        caplog.set_level(logging.DEBUG, logger="mike_trade.brokers.alpaca.http")
        client.get("/v2/account")
    finally:
        client.close()
    assert PAPER_SECRET not in caplog.text
    request_records = [r for r in caplog.records if getattr(r, "method", None) == "GET"]
    assert request_records, "expected a logged alpaca request record"
    for record in caplog.records:
        assert PAPER_SECRET not in record.getMessage()
        headers = getattr(record, "headers", None)
        if headers is not None:
            assert PAPER_SECRET not in json.dumps(headers)
    logged_headers = [getattr(r, "headers", {}) for r in request_records]
    assert any(h.get("APCA-API-SECRET-KEY") == "***REDACTED***" for h in logged_headers)
    assert client.redacted_headers()["APCA-API-SECRET-KEY"] == "***REDACTED***"
    assert PAPER_SECRET not in json.dumps(client.redacted_headers())


def test_auth_headers_sent_but_never_logged_raw() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("APCA-API-KEY-ID", "")
        seen["secret"] = request.headers.get("APCA-API-SECRET-KEY", "")
        return httpx.Response(200, json={}, request=request)

    client = AlpacaHttpClient(paper_creds(), transport=httpx.MockTransport(handler))
    try:
        client.get("/v2/account")
    finally:
        client.close()
    assert seen["key"] == PAPER_KEY
    assert seen["secret"] == PAPER_SECRET


# -- credential loading and live authorization --------------------------------


def test_from_env_no_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "ALPACA_PAPER_KEY_ID",
        "ALPACA_PAPER_SECRET_KEY",
        "ALPACA_LIVE_KEY_ID",
        "ALPACA_LIVE_SECRET_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", PAPER_KEY)
    monkeypatch.setenv("ALPACA_PAPER_SECRET_KEY", PAPER_SECRET)
    creds = AlpacaCredentials.from_env(AlpacaEnvironment.PAPER)
    assert creds.key_id == PAPER_KEY
    assert creds.environment is AlpacaEnvironment.PAPER
    with pytest.raises(ValueError):
        AlpacaCredentials.from_env(AlpacaEnvironment.LIVE)  # no fallback to paper vars
    monkeypatch.setenv("ALPACA_LIVE_KEY_ID", LIVE_KEY)
    with pytest.raises(ValueError):
        AlpacaCredentials.from_env(AlpacaEnvironment.LIVE)  # secret still missing


def test_live_client_requires_explicit_authorization() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}, request=r))
    with pytest.raises(LiveAuthorizationRequired):
        AlpacaHttpClient(live_creds(), transport=transport)
    client = AlpacaHttpClient(live_creds(), transport=transport, live_authorized=True)
    client.close()


def test_paper_client_needs_no_live_authorization() -> None:
    client = AlpacaHttpClient(
        paper_creds(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}, request=r))
    )
    client.close()


# -- status code mapping ------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "exc_type"),
    [
        (401, AlpacaAuthError),
        (403, AlpacaAuthError),
        (404, AlpacaNotFound),
        (422, AlpacaUnprocessable),
        (429, AlpacaRateLimited),
        (500, AlpacaServerError),
        (503, AlpacaServerError),
    ],
)
def test_status_mapping(status: int, exc_type: type[AlpacaApiError]) -> None:
    calls: list[httpx.Request] = []
    body = {"code": status * 10000 + 7, "message": f"boom-{status}"}
    headers = {"x-request-id": "req-123"}
    if status == 429:
        headers["Retry-After"] = "0"
    client, _ = make_client(paper_creds(), [(status, body, headers)], calls)
    try:
        with pytest.raises(exc_type) as exc_info:
            client.post("/v2/orders", json_body={"symbol": "AAPL"})
    finally:
        client.close()
    err = exc_info.value
    assert isinstance(err, AlpacaApiError)
    assert err.status_code == status
    assert err.error_code == status * 10000 + 7
    assert err.message == f"boom-{status}"
    assert err.request_id == "req-123"
    assert len(calls) == 1  # POST never retried


def test_unknown_4xx_maps_to_base_error() -> None:
    calls: list[httpx.Request] = []
    client, _ = make_client(paper_creds(), [(418, {"message": "teapot"}, {})], calls)
    try:
        with pytest.raises(AlpacaApiError) as exc_info:
            client.get("/v2/account")
    finally:
        client.close()
    err = exc_info.value
    assert type(err) is AlpacaApiError
    assert err.status_code == 418


def test_rate_limited_carries_retry_hints() -> None:
    calls: list[httpx.Request] = []
    headers = {"Retry-After": "2", "X-RateLimit-Reset": "1009.5"}
    client, _ = make_client(paper_creds(), [(429, {"code": 42910000, "message": "slow down"}, headers)], calls)
    try:
        with pytest.raises(AlpacaRateLimited) as exc_info:
            client.post("/v2/orders", json_body={"symbol": "AAPL"})
    finally:
        client.close()
    err = exc_info.value
    assert err.retry_after == 2.0
    assert err.rate_limit_reset == 1009.5


# -- retries ------------------------------------------------------------------


def test_get_retries_with_bounded_exponential_backoff() -> None:
    calls: list[httpx.Request] = []
    script = [
        (503, {"message": "bad gateway"}, {}),
        (500, {"message": "internal"}, {}),
        (200, {"id": "acct-1"}, {}),
    ]
    policy = RetryPolicy(max_attempts=3, base_delay_seconds=0.5, max_delay_seconds=8.0, jitter_ratio=0.0)
    client, sleep = make_client(paper_creds(), script, calls, retry_policy=policy)
    try:
        result = client.get("/v2/account")
    finally:
        client.close()
    assert result == {"id": "acct-1"}
    assert len(calls) == 3
    assert sleep.calls == [0.5, 1.0]


def test_get_retry_exhaustion_raises_server_error() -> None:
    calls: list[httpx.Request] = []
    policy = RetryPolicy(max_attempts=2, base_delay_seconds=0.5, jitter_ratio=0.0)
    client, sleep = make_client(
        paper_creds(), [(503, {"message": "down"}, {})], calls, retry_policy=policy
    )
    try:
        with pytest.raises(AlpacaServerError):
            client.get("/v2/account")
    finally:
        client.close()
    assert len(calls) == 2
    assert sleep.calls == [0.5]


def test_429_retry_after_honored_with_fake_sleep() -> None:
    calls: list[httpx.Request] = []
    script = [
        (429, {"message": "slow"}, {"Retry-After": "2"}),
        (200, {"id": "acct-1"}, {}),
    ]
    policy = RetryPolicy(max_attempts=3, base_delay_seconds=0.5, jitter_ratio=0.0)
    client, sleep = make_client(paper_creds(), script, calls, retry_policy=policy)
    try:
        result = client.get("/v2/account")
    finally:
        client.close()
    assert result == {"id": "acct-1"}
    assert len(calls) == 2
    assert sleep.calls == [2.0]


def test_429_rate_limit_reset_honored() -> None:
    calls: list[httpx.Request] = []
    script = [
        (429, {"message": "slow"}, {"X-RateLimit-Reset": "1005.0"}),
        (200, {"id": "acct-1"}, {}),
    ]
    policy = RetryPolicy(max_attempts=3, base_delay_seconds=0.5, jitter_ratio=0.0)
    client, sleep = make_client(
        paper_creds(), script, calls, retry_policy=policy, clock=lambda: 1000.0
    )
    try:
        client.get("/v2/account")
    finally:
        client.close()
    assert sleep.calls == [5.0]


def test_get_timeout_retries_then_raises_timeout() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("read timed out", request=request)

    policy = RetryPolicy(max_attempts=3, base_delay_seconds=0.5, jitter_ratio=0.0)
    sleep = FakeSleep()
    client = AlpacaHttpClient(
        paper_creds(),
        transport=httpx.MockTransport(handler),
        sleep=sleep,
        retry_policy=policy,
    )
    try:
        with pytest.raises(AlpacaTimeout) as exc_info:
            client.get("/v2/account")
    finally:
        client.close()
    assert not isinstance(exc_info.value, AlpacaUnknownOutcome)
    assert exc_info.value.unknown_outcome is False
    assert len(calls) == 3
    assert sleep.calls == [0.5, 1.0]


def test_get_transport_error_retries_then_raises_transport_error() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("refused", request=request)

    policy = RetryPolicy(max_attempts=2, base_delay_seconds=0.25, jitter_ratio=0.0)
    sleep = FakeSleep()
    client = AlpacaHttpClient(
        paper_creds(), transport=httpx.MockTransport(handler), sleep=sleep, retry_policy=policy
    )
    try:
        with pytest.raises(AlpacaTransportError):
            client.get("/v2/account")
    finally:
        client.close()
    assert len(calls) == 2
    assert sleep.calls == [0.25]


def test_no_retry_on_order_mutations() -> None:
    for method in ("post", "patch", "delete"):
        calls: list[httpx.Request] = []
        client, sleep = make_client(paper_creds(), [(500, {"message": "oops"}, {})], calls)
        try:
            with pytest.raises(AlpacaServerError):
                getattr(client, method)("/v2/orders/abc")
        finally:
            client.close()
        assert len(calls) == 1, method
        assert sleep.calls == [], method


def test_timeout_on_order_post_surfaces_unknown_outcome() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise timeout_exc(request)

    sleep = FakeSleep()
    client = AlpacaHttpClient(paper_creds(), transport=httpx.MockTransport(handler), sleep=sleep)
    try:
        with pytest.raises(AlpacaUnknownOutcome) as exc_info:
            client.post("/v2/orders", json_body={"symbol": "AAPL", "qty": "1"})
    finally:
        client.close()
    assert isinstance(exc_info.value, AlpacaTimeout)
    assert exc_info.value.unknown_outcome is True
    assert len(calls) == 1  # exactly one transport call: never retried
    assert sleep.calls == []


def test_connection_loss_on_order_post_is_unknown_outcome() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("connection reset", request=request)

    client = AlpacaHttpClient(paper_creds(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(AlpacaUnknownOutcome):
            client.post("/v2/orders", json_body={"symbol": "AAPL"})
    finally:
        client.close()
    assert len(calls) == 1


# -- decimal parsing and timeouts ----------------------------------------------


def test_monetary_and_quantity_fields_parse_to_decimal() -> None:
    calls: list[httpx.Request] = []
    raw = (
        b'{"qty": "10.5", "price": 12.34, "filled_qty": 2, "cash": "100.25",'
        b' "symbol": "AAPL", "status": "accepted",'
        b' "legs": [{"qty": "1.25", "limit_price": "9.99"}]}'
    )
    client, _ = make_client(paper_creds(), [(200, raw, {})], calls)
    try:
        result = client.get("/v2/orders/abc")
    finally:
        client.close()

    def assert_no_floats(node: Any) -> None:
        assert not isinstance(node, float), f"float leaked: {node!r}"
        if isinstance(node, dict):
            for value in node.values():
                assert_no_floats(value)
        elif isinstance(node, list):
            for value in node:
                assert_no_floats(value)

    assert_no_floats(result)
    assert result["qty"] == Decimal("10.5")
    assert result["price"] == Decimal("12.34")
    assert result["filled_qty"] == Decimal(2)
    assert result["cash"] == Decimal("100.25")
    assert result["symbol"] == "AAPL"
    assert result["legs"][0]["qty"] == Decimal("1.25")
    assert result["legs"][0]["limit_price"] == Decimal("9.99")


def test_requests_carry_explicit_finite_timeouts() -> None:
    client = AlpacaHttpClient(
        paper_creds(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}, request=r)),
        connect_timeout=2.0,
        read_timeout=7.0,
    )
    try:
        timeout = client.httpx_timeout
        assert timeout.connect == 2.0
        assert timeout.read == 7.0
    finally:
        client.close()
    defaulted = AlpacaHttpClient(
        paper_creds(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}, request=r)),
    )
    try:
        assert defaulted.httpx_timeout.connect is not None
        assert defaulted.httpx_timeout.read is not None
        assert float(defaulted.httpx_timeout.connect) > 0
        assert float(defaulted.httpx_timeout.read) > 0
    finally:
        defaulted.close()
    with pytest.raises(ValueError):
        AlpacaHttpClient(paper_creds(), connect_timeout=0)
    with pytest.raises(ValueError):
        AlpacaHttpClient(paper_creds(), read_timeout=float("inf"))


def test_retry_policy_validation() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(max_delay_seconds=0.1, base_delay_seconds=1.0)
    policy = RetryPolicy(max_attempts=4, base_delay_seconds=1.0, max_delay_seconds=4.0, jitter_ratio=0.0)
    assert policy.delay_for_attempt(1) == 1.0
    assert policy.delay_for_attempt(2) == 2.0
    assert policy.delay_for_attempt(3) == 4.0
    assert policy.delay_for_attempt(10) == 4.0  # bounded
