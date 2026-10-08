"""Broker-neutral account, capability, health, and operator-control snapshots.

Validation decisions: timestamps must have a zero UTC offset; account monetary
values are finite :class:`~decimal.Decimal` instances; and unsafe account flags
are the explicit broker restrictions that directly block trading.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping


def _require_utc(value: datetime, name: str) -> None:
    """Require an aware datetime whose UTC offset is zero."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a UTC-aware datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC (zero offset)")


def _require_decimal(value: Decimal | None, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")


def _require_nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


class AccountEnvironment(Enum):
    BACKTEST = "BACKTEST"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass(frozen=True, slots=True)
class AccountScope:
    """Identity boundary for account-wide cash, buying power, and positions."""

    environment: AccountEnvironment
    account_id: str
    scenario: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.environment, AccountEnvironment):
            raise TypeError("environment must be an AccountEnvironment")
        _require_nonempty(self.account_id, "account_id")
        if self.environment in (AccountEnvironment.SHADOW, AccountEnvironment.BACKTEST):
            _require_nonempty(self.scenario, "scenario")
        elif self.scenario is not None:
            raise ValueError("scenario is only valid for SHADOW and BACKTEST scopes")

    @property
    def scope_id(self) -> str:
        """Stable, environment-qualified scope key."""
        prefix = self.environment.value.lower()
        if self.scenario is None:
            return f"{prefix}:{self.account_id}"
        return f"{prefix}:{self.account_id}:{self.scenario}"


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Immutable account-level financial state; values never belong to a symbol."""

    scope: AccountScope
    as_of: datetime
    cash: Decimal
    equity: Decimal
    buying_power: Decimal
    daytrading_buying_power: Decimal | None = None
    portfolio_value: Decimal | None = None
    pattern_day_trader: bool = False
    trading_blocked: bool = False
    account_blocked: bool = False
    trade_suspended_by_user: bool = False
    shorting_enabled: bool = False
    source: str = ""
    currency: str = "USD"

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AccountScope):
            raise TypeError("scope must be an AccountScope")
        _require_utc(self.as_of, "as_of")
        for name in ("cash", "equity", "buying_power", "daytrading_buying_power", "portfolio_value"):
            _require_decimal(getattr(self, name), name)
        if self.currency != "USD":
            raise ValueError("currency must be USD")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a non-empty provenance string")

    @property
    def unsafe_flags(self) -> tuple[str, ...]:
        """Names of broker restrictions that block new entries."""
        return tuple(
            name
            for name in ("trading_blocked", "account_blocked", "trade_suspended_by_user")
            if getattr(self, name)
        )


@dataclass(frozen=True, slots=True)
class BrokerCapabilities:
    provider: str
    environment: AccountEnvironment
    supported_order_types: frozenset[str]
    supports_fractional: bool
    supports_notional: bool
    supports_native_trailing_stop: bool
    supports_extended_hours: bool
    supports_short: bool = False
    paper_verified_combinations: frozenset[tuple[str, bool]] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _require_nonempty(self.provider, "provider")
        if not isinstance(self.environment, AccountEnvironment):
            raise TypeError("environment must be an AccountEnvironment")
        if not isinstance(self.supported_order_types, frozenset):
            raise TypeError("supported_order_types must be a frozenset")
        if not isinstance(self.paper_verified_combinations, frozenset):
            raise TypeError("paper_verified_combinations must be a frozenset")

    def supports_order(self, order_type: str, fractional: bool) -> bool:
        """Return true only for an explicitly advertised, verified combination."""
        if order_type not in self.supported_order_types:
            return False
        if fractional:
            if not self.supports_fractional:
                return False
            if (order_type, True) not in self.paper_verified_combinations:
                return False
        return True


@dataclass(frozen=True, slots=True)
class MarketDataCapabilities:
    provider: str
    feed: str
    is_consolidated: bool
    supports_streaming: bool
    supported_timeframes: frozenset[str]
    max_streaming_symbols: int | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.provider, "provider")
        _require_nonempty(self.feed, "feed")
        if not isinstance(self.supported_timeframes, frozenset):
            raise TypeError("supported_timeframes must be a frozenset")
        if self.max_streaming_symbols is not None and self.max_streaming_symbols < 0:
            raise ValueError("max_streaming_symbols must be non-negative")


class HealthStatus(Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    component: str
    status: HealthStatus
    observed_at: datetime
    heartbeat_at: datetime | None = None
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_nonempty(self.component, "component")
        if not isinstance(self.status, HealthStatus):
            raise TypeError("status must be a HealthStatus")
        _require_utc(self.observed_at, "observed_at")
        if self.heartbeat_at is not None:
            _require_utc(self.heartbeat_at, "heartbeat_at")
        copied = dict(self.details)
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in copied.items()):
            raise TypeError("details must map strings to strings")
        object.__setattr__(self, "details", MappingProxyType(copied))

    def effective_status(self, now: datetime, max_heartbeat_age: timedelta) -> HealthStatus:
        """Return OFFLINE for absent or stale heartbeat; otherwise the reported status."""
        _require_utc(now, "now")
        if max_heartbeat_age < timedelta(0):
            raise ValueError("max_heartbeat_age must be non-negative")
        if self.heartbeat_at is None or now - self.heartbeat_at > max_heartbeat_age:
            return HealthStatus.OFFLINE
        return self.status


class ControlCommandKind(Enum):
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    ENTRY_KILL_SWITCH_ON = "ENTRY_KILL_SWITCH_ON"
    ENTRY_KILL_SWITCH_OFF = "ENTRY_KILL_SWITCH_OFF"
    CLOSE_POSITION = "CLOSE_POSITION"
    CLOSE_ALL = "CLOSE_ALL"
    FLATTEN = "FLATTEN"
    RECONCILE = "RECONCILE"
    ADOPT_POSITION = "ADOPT_POSITION"
    CLEAR_COOLDOWN = "CLEAR_COOLDOWN"
    RESET_ENTRY_LIMIT = "RESET_ENTRY_LIMIT"


@dataclass(frozen=True, slots=True)
class ControlCommand:
    """Operator command; live position mutations require ``live_confirmed=True``."""

    command_id: str
    kind: ControlCommandKind
    scope: AccountScope
    issued_at: datetime
    issued_by: str
    symbol: str | None = None
    reason: str = ""
    live_confirmed: bool = False

    def __post_init__(self) -> None:
        _require_nonempty(self.command_id, "command_id")
        _require_nonempty(self.issued_by, "issued_by")
        if not isinstance(self.kind, ControlCommandKind):
            raise TypeError("kind must be a ControlCommandKind")
        if not isinstance(self.scope, AccountScope):
            raise TypeError("scope must be an AccountScope")
        _require_utc(self.issued_at, "issued_at")
        if not isinstance(self.reason, str):
            raise TypeError("reason must be a string")
        symbol_required = {
            ControlCommandKind.CLOSE_POSITION,
            ControlCommandKind.ADOPT_POSITION,
            ControlCommandKind.CLEAR_COOLDOWN,
            ControlCommandKind.RESET_ENTRY_LIMIT,
        }
        if self.kind in symbol_required:
            _require_nonempty(self.symbol, "symbol")
        elif self.symbol is not None and not self.symbol.strip():
            raise ValueError("symbol must be non-empty when provided")
        position_mutations = {
            ControlCommandKind.CLOSE_POSITION,
            ControlCommandKind.CLOSE_ALL,
            ControlCommandKind.FLATTEN,
            ControlCommandKind.ADOPT_POSITION,
        }
        if (
            self.scope.environment is AccountEnvironment.LIVE
            and self.kind in position_mutations
            and not self.live_confirmed
        ):
            raise ValueError("live position-mutating command requires live_confirmed=True")
