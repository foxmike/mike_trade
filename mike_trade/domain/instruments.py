"""Immutable value objects for instruments, sessions, money, and numerics.

Standard library only (dataclasses, Enum, decimal, datetime, zoneinfo, re).
This module never imports broker SDKs, adapters, strategy code, or any other
``mike_trade`` module at runtime. All numeric validation raises ``ValueError``.

Design choices (3):
1. ``MarketSession.kind_at`` treats 09:30-16:00 America/New_York as REGULAR;
   timestamps inside the outer ``opens_at``/``closes_at`` bounds but outside
   that window map to PRE_MARKET/AFTER_HOURS, and anything outside maps to
   CLOSED.
2. Aware ``entry_cutoff`` times are normalized to naive America/New_York wall
   time; naive times are interpreted as New_York wall time directly.
3. Symbols are normalized with ``strip().upper()`` and validated against
   ``^[A-Z][A-Z0-9.\\-]{0,9}$`` (max 10 chars); precision is never rounded or
   quantized because tick sizes are provider-specific.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "to_decimal",
    "require_non_negative",
    "require_positive",
    "require_utc",
    "Money",
    "AssetClass",
    "Instrument",
    "SessionKind",
    "MarketSession",
    "TradingSessionPolicy",
]

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_NY_ZONE = "America/New_York"
_REGULAR_OPEN = time(9, 30)
_REGULAR_CLOSE = time(16, 0)
_REGULAR_SESSION_MINUTES = 390


def to_decimal(value: Decimal | int | str, *, field: str) -> Decimal:
    """Coerce ``value`` to an exact :class:`Decimal` without rounding.

    Rejects ``float``, ``bool``, NaN/sNaN, infinities, and unparsable
    strings with ``ValueError``.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field} must be Decimal, int, or str; got {type(value).__name__}")
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
            raise ValueError(f"{field} is not a valid decimal string: {value!r}") from None
    else:
        raise ValueError(f"{field} must be Decimal, int, or str; got {type(value).__name__}")
    if result.is_nan() or result.is_infinite():
        raise ValueError(f"{field} must be finite; got {result!s}")
    return result


def require_non_negative(value: Decimal | int | str, *, field: str) -> Decimal:
    """Return ``value`` as :class:`Decimal`, rejecting negatives."""
    result = to_decimal(value, field=field)
    if result < 0:
        raise ValueError(f"{field} must be >= 0; got {result!s}")
    return result


def require_positive(value: Decimal | int | str, *, field: str) -> Decimal:
    """Return ``value`` as :class:`Decimal`, rejecting zero and negatives."""
    result = to_decimal(value, field=field)
    if result <= 0:
        raise ValueError(f"{field} must be > 0; got {result!s}")
    return result


def require_utc(ts: datetime, *, field: str) -> datetime:
    """Reject naive datetimes; normalize aware datetimes to UTC."""
    if not isinstance(ts, datetime):
        raise ValueError(f"{field} must be a datetime; got {type(ts).__name__}")
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return ts.astimezone(UTC)


def _require_currency(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _CURRENCY_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a 3-letter uppercase code; got {value!r}")
    return value


def _require_flag(value: object, *, field: str) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{field} must be bool or None; got {type(value).__name__}")
    return value  # type: ignore[return-value]


def _as_ny_wall_time(value: time) -> time:
    if value.tzinfo is None:
        return value
    probe = datetime.combine(date(2024, 1, 15), value)
    return probe.astimezone(ZoneInfo(_NY_ZONE)).time()


@dataclass(frozen=True, slots=True)
class Money:
    """An exact decimal amount plus a 3-letter uppercase currency code."""

    amount: Decimal
    currency: str = "USD"

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", to_decimal(self.amount, field="amount"))
        object.__setattr__(self, "currency", _require_currency(self.currency, field="currency"))

    def _check_compatible(self, other: object) -> Money:
        if not isinstance(other, Money):
            raise ValueError(f"Money operand must be Money; got {type(other).__name__}")
        if other.currency != self.currency:
            raise ValueError(f"currency mismatch: {self.currency} != {other.currency}")
        return other

    def add(self, other: Money) -> Money:
        """Return ``self + other``; currencies must match."""
        peer = self._check_compatible(other)
        return Money(self.amount + peer.amount, self.currency)

    def subtract(self, other: Money) -> Money:
        """Return ``self - other``; currencies must match."""
        peer = self._check_compatible(other)
        return Money(self.amount - peer.amount, self.currency)


class AssetClass(Enum):
    """Supported asset classes. Only US equities are active."""

    US_EQUITY = "US_EQUITY"


@dataclass(frozen=True, slots=True)
class Instrument:
    """A tradable instrument identifier with provider-reported capability flags."""

    symbol: str
    asset_class: AssetClass
    primary_exchange: str | None = None
    currency: str = "USD"
    tradable: bool | None = None
    fractionable: bool | None = None
    shortable: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str):
            raise ValueError(f"symbol must be str; got {type(self.symbol).__name__}")
        normalized = self.symbol.strip().upper()
        if _SYMBOL_RE.fullmatch(normalized) is None:
            raise ValueError(f"symbol is invalid: {self.symbol!r}")
        object.__setattr__(self, "symbol", normalized)
        if not isinstance(self.asset_class, AssetClass):
            raise ValueError(
                f"asset_class must be AssetClass; got {type(self.asset_class).__name__}"
            )
        if self.primary_exchange is not None:
            if not isinstance(self.primary_exchange, str) or not self.primary_exchange.strip():
                raise ValueError("primary_exchange must be a non-empty str or None")
            object.__setattr__(self, "primary_exchange", self.primary_exchange.strip())
        object.__setattr__(self, "currency", _require_currency(self.currency, field="currency"))
        object.__setattr__(self, "tradable", _require_flag(self.tradable, field="tradable"))
        object.__setattr__(
            self, "fractionable", _require_flag(self.fractionable, field="fractionable")
        )
        object.__setattr__(self, "shortable", _require_flag(self.shortable, field="shortable"))


class SessionKind(Enum):
    """Intraday session classification for a point in time."""

    PRE_MARKET = "PRE_MARKET"
    REGULAR = "REGULAR"
    AFTER_HOURS = "AFTER_HOURS"
    CLOSED = "CLOSED"


@dataclass(frozen=True, slots=True)
class MarketSession:
    """One exchange session day with UTC open/close bounds."""

    session_date: date
    opens_at: datetime
    closes_at: datetime
    exchange: str = "XNYS"
    timezone_name: str = "America/New_York"

    def __post_init__(self) -> None:
        if not isinstance(self.session_date, date) or isinstance(self.session_date, datetime):
            raise ValueError("session_date must be a date")
        try:
            ZoneInfo(self.timezone_name)
        except (ValueError, ZoneInfoNotFoundError, TypeError):
            raise ValueError(f"unknown timezone: {self.timezone_name!r}") from None
        opens = require_utc(self.opens_at, field="opens_at")
        closes = require_utc(self.closes_at, field="closes_at")
        if not opens < closes:
            raise ValueError("opens_at must be earlier than closes_at")
        object.__setattr__(self, "opens_at", opens)
        object.__setattr__(self, "closes_at", closes)
        if not isinstance(self.exchange, str) or not self.exchange.strip():
            raise ValueError("exchange must be a non-empty str")
        object.__setattr__(self, "exchange", self.exchange.strip())

    def local(self, ts_utc: datetime) -> datetime:
        """Return ``ts_utc`` expressed in the session's New York timezone."""
        return require_utc(ts_utc, field="ts_utc").astimezone(ZoneInfo(self.timezone_name))

    def kind_at(self, ts_utc: datetime) -> SessionKind:
        """Classify ``ts_utc`` against the session bounds and regular hours."""
        ts = require_utc(ts_utc, field="ts_utc")
        if ts < self.opens_at or ts >= self.closes_at:
            return SessionKind.CLOSED
        wall = ts.astimezone(ZoneInfo(self.timezone_name)).time()
        if wall < _REGULAR_OPEN:
            return SessionKind.PRE_MARKET
        if wall < _REGULAR_CLOSE:
            return SessionKind.REGULAR
        return SessionKind.AFTER_HOURS


@dataclass(frozen=True, slots=True)
class TradingSessionPolicy:
    """Regular-hours gating, entry cutoff, and optional end-of-day close."""

    regular_hours_only: bool = True
    entry_cutoff: time | int | None = None
    eod_close_enabled: bool = False
    eod_close_minutes_before_close: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.regular_hours_only, bool):
            raise ValueError("regular_hours_only must be bool")
        if not isinstance(self.eod_close_enabled, bool):
            raise ValueError("eod_close_enabled must be bool")
        object.__setattr__(self, "entry_cutoff", self._validate_cutoff(self.entry_cutoff))
        if self.eod_close_minutes_before_close is not None:
            minutes = self.eod_close_minutes_before_close
            if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
                raise ValueError("eod_close_minutes_before_close must be an int >= 0 or None")

    @staticmethod
    def _validate_cutoff(value: time | int | None) -> time | int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError("entry_cutoff must be a time, an int offset, or None")
        if isinstance(value, time):
            wall = _as_ny_wall_time(value)
            if not _REGULAR_OPEN <= wall <= _REGULAR_CLOSE:
                raise ValueError(f"entry_cutoff {value!r} falls outside the regular session")
            return wall
        if isinstance(value, int):
            if not 0 <= value <= _REGULAR_SESSION_MINUTES:
                raise ValueError("entry_cutoff offset must be within 0..390 minutes before close")
            return value
        raise ValueError(
            f"entry_cutoff must be a time, an int offset, or None; got {type(value).__name__}"
        )
