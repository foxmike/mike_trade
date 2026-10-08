"""
Domain exceptions hierarchy for mike_trade.
"""

from collections.abc import Mapping
from typing import Any


class MikeTradeError(Exception):
    """Base exception for all domain and runtime errors in mike_trade."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.context = context if context is not None else {}


class ConfigurationError(MikeTradeError):
    """Raised when application configuration is missing, invalid, or inconsistent."""


class LiveTradingNotAuthorizedError(MikeTradeError):
    """Raised when an action requires live trading authorization that is not explicitly enabled."""


class ModeIsolationError(MikeTradeError):
    """Raised when state, credentials, or operations cross isolated execution modes."""


class StaleMarketDataError(MikeTradeError):
    """Raised when market data is stale or beyond maximum acceptable age."""


class ReconciliationDriftError(MikeTradeError):
    """Raised when internal state drifts from broker authoritative state."""


class InsufficientBuyingPowerError(MikeTradeError):
    """Raised when account buying power is insufficient for a trade intent."""


class EntryBlockedError(MikeTradeError):
    """Raised when entry filters, limits, cooldowns, or lockouts block a new position."""


class UnsupportedOrderCapabilityError(MikeTradeError):
    """Raised when a requested order type or combination is unsupported by the broker/mode."""


class BrokerError(MikeTradeError):
    """Base exception for broker interaction failures."""


class BrokerRejectedError(BrokerError):
    """Raised when a broker explicitly rejects an order or request."""


class BrokerUnavailableError(BrokerError):
    """Raised when a broker endpoint is unreachable or unavailable."""


class PersistenceError(MikeTradeError):
    """Raised when database operations, migrations, or journal persistence fail."""


class StrategyError(MikeTradeError):
    """Raised when strategy evaluation or execution logic fails."""
