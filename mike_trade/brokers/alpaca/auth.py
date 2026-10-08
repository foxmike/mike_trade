"""Authenticated environment configuration for the Alpaca individual Trading API.

This module covers the individual trading endpoints only
(``paper-api.alpaca.markets`` and ``api.alpaca.markets``). The enterprise
Broker API (``broker-api.*``) is never used here: any URL pointing at a
``broker-api.*`` host is rejected with :class:`EnvironmentMismatchError`.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping
from urllib.parse import urlsplit

from mike_trade.domain.errors import LiveTradingNotAuthorizedError, ModeIsolationError

__all__ = [
    "AlpacaEnvironment",
    "AlpacaCredentials",
    "credential_fingerprint",
    "LiveAuthorizationRequired",
    "EnvironmentMismatchError",
]

_PAPER_HOST = "paper-api.alpaca.markets"
_LIVE_HOST = "api.alpaca.markets"
_FINGERPRINT_LENGTH = 16


class LiveAuthorizationRequired(LiveTradingNotAuthorizedError):
    """A LIVE Alpaca client was requested without explicit live authorization."""


class EnvironmentMismatchError(ModeIsolationError):
    """Credentials, base URL, or persisted fingerprint disagree on environment."""


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _reject_broker_api_host(url: str) -> None:
    host = _host_of(url)
    if host == "broker-api.alpaca.markets" or host.startswith("broker-api."):
        raise EnvironmentMismatchError(
            "Alpaca Broker API (enterprise) endpoints are forbidden; "
            "use the individual Trading API only. "
            f"Rejected host: {host or url!r}."
        )


class AlpacaEnvironment(Enum):
    """Exactly two Alpaca Trading API environments: paper and live."""

    PAPER = "PAPER"
    LIVE = "LIVE"

    @property
    def base_url(self) -> str:
        """Trading API base URL for this environment."""
        if self is AlpacaEnvironment.PAPER:
            return f"https://{_PAPER_HOST}"
        return f"https://{_LIVE_HOST}"

    @property
    def trade_stream_url(self) -> str:
        """Trade-updates websocket stream URL for this environment."""
        if self is AlpacaEnvironment.PAPER:
            return f"wss://{_PAPER_HOST}/stream"
        return f"wss://{_LIVE_HOST}/stream"

    @classmethod
    def from_base_url(cls, base_url: str) -> AlpacaEnvironment:
        """Resolve the environment owning ``base_url``.

        Raises :class:`EnvironmentMismatchError` for the Broker API host or
        any host that is not one of the two Trading API hosts.
        """
        _reject_broker_api_host(base_url)
        host = _host_of(base_url)
        if host == _PAPER_HOST:
            return cls.PAPER
        if host == _LIVE_HOST:
            return cls.LIVE
        raise EnvironmentMismatchError(
            f"Unknown Alpaca Trading API host: {host or base_url!r}; "
            f"expected {_PAPER_HOST!r} or {_LIVE_HOST!r}."
        )


@dataclass(frozen=True, slots=True, repr=False)
class AlpacaCredentials:
    """Immutable Alpaca API credentials bound to one environment.

    The secret is excluded from ``repr`` and ``str``. There is no fallback
    between paper and live credential sources.
    """

    key_id: str
    secret_key: str = field(repr=False)
    environment: AlpacaEnvironment = AlpacaEnvironment.PAPER

    def __post_init__(self) -> None:
        if not isinstance(self.key_id, str) or not self.key_id.strip():
            raise ValueError("key_id must be a non-empty string")
        if not isinstance(self.secret_key, str) or not self.secret_key.strip():
            raise ValueError("secret_key must be a non-empty string")
        if not isinstance(self.environment, AlpacaEnvironment):
            raise TypeError("environment must be an AlpacaEnvironment")

    def __repr__(self) -> str:
        return (
            f"AlpacaCredentials(key_id={self.key_id!r}, "
            f"environment={self.environment!r}, "
            f"fingerprint={credential_fingerprint(self)!r})"
        )

    def __str__(self) -> str:
        return (
            f"AlpacaCredentials({self.environment.value} "
            f"key_id={self.key_id!r} fingerprint={credential_fingerprint(self)!r})"
        )

    @classmethod
    def from_env(
        cls,
        environment: AlpacaEnvironment,
        env: Mapping[str, str] | None = None,
    ) -> AlpacaCredentials:
        """Load credentials for ``environment`` from environment variables.

        Paper reads ``ALPACA_PAPER_KEY_ID``/``ALPACA_PAPER_SECRET_KEY`` and
        live reads ``ALPACA_LIVE_KEY_ID``/``ALPACA_LIVE_SECRET_KEY``. There
        is no fallback from one environment's variables to the other's.
        """
        if not isinstance(environment, AlpacaEnvironment):
            raise TypeError("environment must be an AlpacaEnvironment")
        if environment is AlpacaEnvironment.PAPER:
            key_var, secret_var = "ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET_KEY"
        else:
            key_var, secret_var = "ALPACA_LIVE_KEY_ID", "ALPACA_LIVE_SECRET_KEY"
        source: Mapping[str, str] = os.environ if env is None else env
        key_id = (source.get(key_var) or "").strip()
        secret_key = (source.get(secret_var) or "").strip()
        missing = [name for name, value in ((key_var, key_id), (secret_var, secret_key)) if not value]
        if missing:
            raise ValueError(
                f"Missing Alpaca {environment.value.lower()} credentials: "
                f"{', '.join(missing)} is not set. "
                "Credentials never fall back across environments."
            )
        return cls(key_id=key_id, secret_key=secret_key, environment=environment)


def credential_fingerprint(credentials: AlpacaCredentials) -> str:
    """Non-reversible fingerprint identifying environment + key id.

    A truncated SHA-256 over ``environment + key_id``. Used for
    state-directory and ledger isolation; it never exposes the secret.
    """
    if not isinstance(credentials, AlpacaCredentials):
        raise TypeError("credentials must be an AlpacaCredentials")
    digest = hashlib.sha256(
        f"{credentials.environment.value}:{credentials.key_id}".encode("utf-8")
    ).hexdigest()
    return digest[:_FINGERPRINT_LENGTH]
