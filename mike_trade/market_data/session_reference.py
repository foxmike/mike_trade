"""Point-in-time regular-session OHLC references from completed M1 candles.

The tracker is deliberately small and bounded: for each symbol it retains one
in-progress session and the immediately preceding session result.  A session
OHLC exists only when every expected NYSE regular-session minute was observed.
Premarket and after-hours bars are ignored, and an incomplete session is never
replaced with an older complete one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from mike_trade.market_data.candles import (
    Candle,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.scheduling.calendar import (
    NEW_YORK,
    Session,
    is_trading_day,
    previous_session,
    session_for,
)

__all__ = ["RegularSessionOHLC", "SessionReference", "SessionReferenceTracker"]

_MINUTE = timedelta(minutes=1)


def _price(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise ValueError(f"{field} must be Decimal")
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{field} must be positive and finite")
    return value


def _aware_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class RegularSessionOHLC:
    """OHLC from one fully observed, completed regular exchange session."""

    session_id: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    open_time: datetime
    close_time: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("session_id must be a non-empty str")
        for name in ("open", "high", "low", "close"):
            object.__setattr__(self, name, _price(getattr(self, name), name))
        object.__setattr__(self, "open_time", _aware_utc(self.open_time, "open_time"))
        object.__setattr__(self, "close_time", _aware_utc(self.close_time, "close_time"))
        if self.close_time <= self.open_time:
            raise ValueError("close_time must follow open_time")
        if self.high < self.low or not self.low <= self.open <= self.high:
            raise ValueError("session OHLC is inconsistent")
        if not self.low <= self.close <= self.high:
            raise ValueError("session close must be within low/high")


@dataclass(frozen=True, slots=True)
class SessionReference:
    """References available at one point during the current exchange session."""

    current_session_id: str
    previous_session_id: str
    current_open: Decimal | None
    previous: RegularSessionOHLC | None

    def __post_init__(self) -> None:
        for name in ("current_session_id", "previous_session_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty str")
        if self.current_open is not None:
            object.__setattr__(self, "current_open", _price(self.current_open, "current_open"))
        if self.previous is not None:
            if not isinstance(self.previous, RegularSessionOHLC):
                raise ValueError("previous must be RegularSessionOHLC or None")
            if self.previous.session_id != self.previous_session_id:
                raise ValueError("previous session id does not match previous_session_id")


@dataclass(slots=True)
class _Accumulator:
    session: Session
    symbol: str
    expected_start: datetime
    count: int = 0
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    feed_identity: tuple[str, bool, PriceBasis] | None = None
    valid: bool = True

    @classmethod
    def create(cls, session: Session, symbol: str) -> _Accumulator:
        return cls(session=session, symbol=symbol, expected_start=session.open_utc)

    def add(self, bar: Candle) -> None:
        if bar.start != self.expected_start or bar.end != bar.start + _MINUTE:
            self.valid = False
        identity = (bar.feed_id, bar.consolidated, bar.price_basis)
        if self.feed_identity is None:
            self.feed_identity = identity
        elif self.feed_identity != identity:
            self.valid = False
        if self.count == 0 and bar.start == self.session.open_utc:
            self.open = bar.open
        self.high = bar.high if self.high is None else max(self.high, bar.high)
        self.low = bar.low if self.low is None else min(self.low, bar.low)
        self.close = bar.close
        self.expected_start = bar.end
        self.count += 1

    def completed(self) -> RegularSessionOHLC | None:
        expected = int((self.session.close_utc - self.session.open_utc) / _MINUTE)
        if (
            not self.valid
            or self.count != expected
            or self.expected_start != self.session.close_utc
            or self.open is None
            or self.high is None
            or self.low is None
            or self.close is None
        ):
            return None
        return RegularSessionOHLC(
            session_id=self.session.session_id,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            open_time=self.session.open_utc,
            close_time=self.session.close_utc,
        )


@dataclass(slots=True)
class _SymbolState:
    active: _Accumulator | None = None
    previous_session_id: str | None = None
    previous: RegularSessionOHLC | None = None
    latest_end: datetime | None = None


class SessionReferenceTracker:
    """Sequentially aggregate complete regular sessions for strategy contexts."""

    def __init__(self) -> None:
        self._states: dict[str, _SymbolState] = {}

    def add_bar(self, bar: Candle) -> None:
        """Observe one completed RAW M1 candle; extended-hours bars are ignored."""
        if not isinstance(bar, Candle):
            raise TypeError("bar must be Candle")
        if (
            bar.timeframe is not CandleTimeframe.M1
            or bar.status is not CandleStatus.CLOSED
            or bar.price_basis is not PriceBasis.RAW
        ):
            return
        day = bar.start.astimezone(NEW_YORK).date()
        if not is_trading_day(day):
            return
        session = session_for(day)
        if not (session.open_utc <= bar.start and bar.end <= session.close_utc):
            return

        state = self._states.setdefault(bar.symbol, _SymbolState())
        if state.latest_end is not None and bar.end <= state.latest_end:
            # REST warmup and the live stream may overlap. Earlier observations cannot
            # alter the already accumulated prefix and are therefore idempotent.
            return

        active = state.active
        if active is None or active.session.session_id != session.session_id:
            expected_previous = previous_session(day).session_id
            completed = active.completed() if active is not None else None
            state.previous_session_id = expected_previous
            state.previous = (
                completed
                if completed is not None and completed.session_id == expected_previous
                else None
            )
            state.active = _Accumulator.create(session, bar.symbol)
        assert state.active is not None
        state.active.add(bar)
        state.latest_end = bar.end

    def reference(self, symbol: str, as_of: datetime) -> SessionReference | None:
        """Return point-in-time references, or ``None`` outside a known trading day.

        If observations later than ``as_of`` have already entered this tracker, all
        prices fail closed instead of exposing future-derived state.
        """
        key = symbol.strip().upper() if isinstance(symbol, str) else ""
        if not key:
            raise ValueError("symbol must be a non-empty str")
        instant = _aware_utc(as_of, "as_of")
        day = instant.astimezone(NEW_YORK).date()
        if not is_trading_day(day):
            return None
        current = session_for(day)
        previous_id = previous_session(day).session_id
        state = self._states.get(key)
        if state is None or (state.latest_end is not None and state.latest_end > instant):
            return SessionReference(current.session_id, previous_id, None, None)
        active = state.active
        current_open = (
            active.open
            if active is not None and active.session.session_id == current.session_id
            else None
        )
        previous = (
            state.previous
            if state.previous_session_id == previous_id
            and state.previous is not None
            and state.previous.session_id == previous_id
            else None
        )
        return SessionReference(current.session_id, previous_id, current_open, previous)
