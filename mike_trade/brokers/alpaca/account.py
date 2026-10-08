"""Read-only Alpaca Trading API account, position, and clock queries.

Works against any injected HTTP client exposing ``get(path, params=...)``,
so fixtures can be tested without network access or credentials.
All money, quantities, and prices are :class:`~decimal.Decimal`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from mike_trade.brokers.alpaca.auth import AlpacaEnvironment

__all__ = [
    "AlpacaAccountService",
    "AlpacaAccountSnapshot",
    "AccountSafetyReason",
    "evaluate_account_flags",
    "AlpacaPosition",
    "AlpacaClock",
]


@runtime_checkable
class _ReadClient(Protocol):
    def get(self, path: str, params: Mapping[str, Any] | None = ...) -> Any: ...


class AccountSafetyReason(Enum):
    """Explicit unsafe-account reasons. Any member present blocks new entries."""

    TRADING_BLOCKED = "trading_blocked"
    ACCOUNT_BLOCKED = "account_blocked"
    TRADE_SUSPENDED_BY_USER = "trade_suspended_by_user"
    NON_ACTIVE_STATUS = "non_active_status"
    PATTERN_DAY_TRADER = "pattern_day_trader"


def _to_decimal(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a decimal string, int, or Decimal")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field} must be a non-empty numeric string")
        try:
            result = Decimal(text)
        except InvalidOperation:
            raise ValueError(f"{field} is not a valid decimal: {value!r}") from None
    else:
        raise ValueError(
            f"{field} must be Decimal, int, or str; got {type(value).__name__}"
        )
    if not result.is_finite():
        raise ValueError(f"{field} must be finite")
    return result


def _parse_utc(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty datetime string")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field} is not a valid datetime: {value!r}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _is_not_found(exc: BaseException) -> bool:
    if getattr(exc, "status_code", None) == 404:
        return True
    return "NotFound" in type(exc).__name__


def _require_nonempty(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class AlpacaAccountSnapshot:
    """Normalized GET /v2/account snapshot tagged with its source environment."""

    environment: AlpacaEnvironment
    fingerprint: str
    account_id: str
    account_number: str
    status: str
    currency: str
    cash: Decimal
    equity: Decimal
    last_equity: Decimal
    buying_power: Decimal
    regt_buying_power: Decimal
    daytrading_buying_power: Decimal
    non_marginable_buying_power: Decimal
    portfolio_value: Decimal
    multiplier: Decimal
    pattern_day_trader: bool
    trading_blocked: bool
    transfers_blocked: bool
    account_blocked: bool
    trade_suspended_by_user: bool
    shorting_enabled: bool
    daytrade_count: int

    @property
    def unsafe_reasons(self) -> tuple[AccountSafetyReason, ...]:
        return evaluate_account_flags(self)


def evaluate_account_flags(
    snapshot: AlpacaAccountSnapshot,
) -> tuple[AccountSafetyReason, ...]:
    """Return explicit unsafe reasons; empty means safe. Fails closed downstream."""
    reasons: list[AccountSafetyReason] = []
    if snapshot.trading_blocked:
        reasons.append(AccountSafetyReason.TRADING_BLOCKED)
    if snapshot.account_blocked:
        reasons.append(AccountSafetyReason.ACCOUNT_BLOCKED)
    if snapshot.trade_suspended_by_user:
        reasons.append(AccountSafetyReason.TRADE_SUSPENDED_BY_USER)
    if snapshot.status.upper() != "ACTIVE":
        reasons.append(AccountSafetyReason.NON_ACTIVE_STATUS)
    if snapshot.pattern_day_trader:
        reasons.append(AccountSafetyReason.PATTERN_DAY_TRADER)
    return tuple(reasons)


@dataclass(frozen=True, slots=True)
class AlpacaPosition:
    """Normalized Alpaca position tagged with its source environment."""

    environment: AlpacaEnvironment
    fingerprint: str
    symbol: str
    asset_id: str
    side: str
    qty: Decimal
    qty_available: Decimal
    avg_entry_price: Decimal
    market_value: Decimal
    cost_basis: Decimal
    unrealized_pl: Decimal
    current_price: Decimal


@dataclass(frozen=True, slots=True)
class AlpacaClock:
    """Normalized GET /v2/clock snapshot tagged with its source environment."""

    environment: AlpacaEnvironment
    fingerprint: str
    is_open: bool
    timestamp: datetime
    next_open: datetime
    next_close: datetime


def _parse_account(
    data: Mapping[str, Any], *, environment: AlpacaEnvironment, fingerprint: str
) -> AlpacaAccountSnapshot:
    def flag(name: str) -> bool:
        value = data.get(name)
        if not isinstance(value, bool):
            raise ValueError(f"account field {name!r} must be bool")
        return value

    daytrade_count = data.get("daytrade_count", 0)
    if isinstance(daytrade_count, bool) or not isinstance(daytrade_count, int):
        if isinstance(daytrade_count, str) and daytrade_count.strip().lstrip("+-").isdigit():
            daytrade_count = int(daytrade_count.strip())
        else:
            raise ValueError("daytrade_count must be an int")
    return AlpacaAccountSnapshot(
        environment=environment,
        fingerprint=fingerprint,
        account_id=_require_nonempty(data.get("id"), field="id"),
        account_number=_require_nonempty(
            data.get("account_number"), field="account_number"
        ),
        status=_require_nonempty(data.get("status"), field="status"),
        currency=_require_nonempty(data.get("currency"), field="currency"),
        cash=_to_decimal(data.get("cash"), field="cash"),
        equity=_to_decimal(data.get("equity"), field="equity"),
        last_equity=_to_decimal(data.get("last_equity"), field="last_equity"),
        buying_power=_to_decimal(data.get("buying_power"), field="buying_power"),
        regt_buying_power=_to_decimal(
            data.get("regt_buying_power"), field="regt_buying_power"
        ),
        daytrading_buying_power=_to_decimal(
            data.get("daytrading_buying_power"), field="daytrading_buying_power"
        ),
        non_marginable_buying_power=_to_decimal(
            data.get("non_marginable_buying_power"),
            field="non_marginable_buying_power",
        ),
        portfolio_value=_to_decimal(
            data.get("portfolio_value"), field="portfolio_value"
        ),
        multiplier=_to_decimal(data.get("multiplier"), field="multiplier"),
        pattern_day_trader=flag("pattern_day_trader"),
        trading_blocked=flag("trading_blocked"),
        transfers_blocked=flag("transfers_blocked"),
        account_blocked=flag("account_blocked"),
        trade_suspended_by_user=flag("trade_suspended_by_user"),
        shorting_enabled=flag("shorting_enabled"),
        daytrade_count=daytrade_count,
    )


def _parse_position(
    data: Mapping[str, Any], *, environment: AlpacaEnvironment, fingerprint: str
) -> AlpacaPosition:
    return AlpacaPosition(
        environment=environment,
        fingerprint=fingerprint,
        symbol=_require_nonempty(data.get("symbol"), field="symbol").upper(),
        asset_id=_require_nonempty(data.get("asset_id"), field="asset_id"),
        side=_require_nonempty(data.get("side"), field="side").lower(),
        qty=_to_decimal(data.get("qty"), field="qty"),
        qty_available=_to_decimal(
            data.get("qty_available"), field="qty_available"
        ),
        avg_entry_price=_to_decimal(
            data.get("avg_entry_price"), field="avg_entry_price"
        ),
        market_value=_to_decimal(data.get("market_value"), field="market_value"),
        cost_basis=_to_decimal(data.get("cost_basis"), field="cost_basis"),
        unrealized_pl=_to_decimal(
            data.get("unrealized_pl"), field="unrealized_pl"
        ),
        current_price=_to_decimal(
            data.get("current_price"), field="current_price"
        ),
    )


class AlpacaAccountService:
    """Read-only account/position/clock queries over an injected GET client."""

    def __init__(
        self,
        client: Any,
        *,
        environment: AlpacaEnvironment,
        fingerprint: str,
    ) -> None:
        if not isinstance(environment, AlpacaEnvironment):
            raise TypeError("environment must be an AlpacaEnvironment")
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError("fingerprint must be a non-empty string")
        if not hasattr(client, "get") or not callable(client.get):
            raise TypeError("client must expose a get(path, params) method")
        self._client = client
        self._environment = environment
        self._fingerprint = fingerprint

    @property
    def environment(self) -> AlpacaEnvironment:
        return self._environment

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def get_account(self) -> AlpacaAccountSnapshot:
        data = self._client.get("/v2/account", params=None)
        if not isinstance(data, Mapping):
            raise ValueError("GET /v2/account must return a JSON object")
        return _parse_account(
            data, environment=self._environment, fingerprint=self._fingerprint
        )

    def list_positions(self) -> list[AlpacaPosition]:
        data = self._client.get("/v2/positions", params=None)
        if not isinstance(data, list):
            raise ValueError("GET /v2/positions must return a JSON array")
        return [
            _parse_position(
                item, environment=self._environment, fingerprint=self._fingerprint
            )
            for item in data
        ]

    def get_position(self, symbol: str) -> AlpacaPosition | None:
        cleaned = _require_nonempty(symbol, field="symbol").upper()
        try:
            data = self._client.get(f"/v2/positions/{cleaned}", params=None)
        except Exception as exc:  # noqa: BLE001 - duck-typed 404 across client doubles
            if _is_not_found(exc):
                return None
            raise
        if not isinstance(data, Mapping):
            raise ValueError(f"GET /v2/positions/{cleaned} must return a JSON object")
        return _parse_position(
            data, environment=self._environment, fingerprint=self._fingerprint
        )

    def get_clock(self) -> AlpacaClock:
        data = self._client.get("/v2/clock", params=None)
        if not isinstance(data, Mapping):
            raise ValueError("GET /v2/clock must return a JSON object")
        is_open = data.get("is_open")
        if not isinstance(is_open, bool):
            raise ValueError("clock field 'is_open' must be bool")
        return AlpacaClock(
            environment=self._environment,
            fingerprint=self._fingerprint,
            is_open=is_open,
            timestamp=_parse_utc(data.get("timestamp"), field="timestamp"),
            next_open=_parse_utc(data.get("next_open"), field="next_open"),
            next_close=_parse_utc(data.get("next_close"), field="next_close"),
        )
