"""Account-level cumulative daily-loss control (business rule V2: 47).

``cumulative = realized trading-day P/L + unrealized P/L on open positions``.  When the
cumulative LOSS reaches (or exceeds) the limit the account must

1. immediately submit market exits for every open position, and
2. block every new entry for the remainder of that New York trading day.

This module only *decides*: it holds no positions, orders or P/L ledger of its own (the engines
that already own the account state - backtest runner, SHADOW account, live ledger - supply the
two P/L figures) and it never submits an order.  The latch is per New York trading date: it
survives the P/L later recovering (the rule blocks "for the remainder of the trading day") and
resets only when the date changes.  Exits are never blocked by it.

The default limit is Michelle's $150.00.  A stricter (smaller) account-wide limit already
configured elsewhere is honoured through :func:`effective_limit`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from mike_trade.scheduling.calendar import NEW_YORK

DEFAULT_DAILY_LOSS_LIMIT = Decimal("150.00")
_ZERO = Decimal(0)


def effective_limit(*limits: Decimal | None) -> Decimal:
    """The strictest configured limit; Michelle's $150 applies when none is stricter."""
    configured = [limit for limit in limits if limit is not None and limit > _ZERO]
    return min([DEFAULT_DAILY_LOSS_LIMIT, *configured])


def trading_date(at: datetime) -> date:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at must be timezone-aware")
    return at.astimezone(NEW_YORK).date()


@dataclass(frozen=True, slots=True)
class DailyLossReading:
    trading_date: date
    realized: Decimal
    unrealized: Decimal
    cumulative: Decimal  # realized + unrealized (negative = loss)
    limit: Decimal
    breached_now: bool  # this observation's own cumulative loss reaches the limit
    latched: bool  # breached at any time today (entries blocked, liquidation required)
    newly_latched: bool  # the first observation that tripped the latch

    def facts(self) -> dict[str, str]:
        return {
            "trading_date": self.trading_date.isoformat(),
            "realized_pnl": str(self.realized),
            "unrealized_pnl": str(self.unrealized),
            "cumulative_pnl": str(self.cumulative),
            "daily_loss_limit": str(self.limit),
            "latched": str(self.latched),
        }


class DailyLossGuard:
    """Per-account latch over the cumulative daily P/L (one instance per account scope)."""

    def __init__(self, limit: Decimal = DEFAULT_DAILY_LOSS_LIMIT) -> None:
        if not isinstance(limit, Decimal) or not limit.is_finite() or limit <= _ZERO:
            raise ValueError("daily loss limit must be a positive finite Decimal")
        self.limit = limit
        self._day: date | None = None
        self._latched_at: datetime | None = None
        self._last: DailyLossReading | None = None

    # -------------------------------------------------------------------- queries

    @property
    def last(self) -> DailyLossReading | None:
        return self._last

    def latched(self, at: datetime) -> bool:
        """True while ``at`` falls on the trading date whose loss limit was reached."""
        return self._latched_at is not None and self._day == trading_date(at)

    def entries_blocked(self, at: datetime) -> bool:
        """Rule 47: no new entry for the rest of the trading day once the limit was reached."""
        return self.latched(at)

    def liquidation_required(self, at: datetime, open_positions: int) -> bool:
        """Open positions must be market-closed while the latch is set (retry until flat)."""
        return self.latched(at) and open_positions > 0

    # ------------------------------------------------------------------- updates

    def observe(self, at: datetime, realized: Decimal, unrealized: Decimal) -> DailyLossReading:
        """Record the current day P/L figures and trip the latch when the limit is reached."""
        day = trading_date(at)
        if self._day != day:  # a new trading day clears yesterday's latch
            self._day, self._latched_at = day, None
        cumulative = realized + unrealized
        breached = cumulative <= -self.limit
        newly = breached and self._latched_at is None
        if newly:
            self._latched_at = at.astimezone(UTC)
        reading = DailyLossReading(
            day, realized, unrealized, cumulative, self.limit, breached,
            self._latched_at is not None, newly,
        )
        self._last = reading
        return reading

    def restore(self, at: datetime) -> None:
        """Re-establish a latch that was persisted before a restart."""
        self._day, self._latched_at = trading_date(at), at.astimezone(UTC)
