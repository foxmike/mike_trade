"""Declarative, immutable strategy scheduling triggers.

The declarations in this module contain configuration only.  They deliberately
have no references to strategies, market-data providers, or execution services.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Final

from mike_trade.domain.errors import ConfigurationError
from mike_trade.market_data.candles import CandleTimeframe

__all__ = [
    "TriggerConfigurationError",
    "TriggerKind",
    "StrategyTrigger",
    "Trigger",
    "MARKET_DATA_ARRIVAL",
    "QUOTE_CHANGE",
    "SESSION_OPEN",
    "ENTRY_CUTOFF",
    "SESSION_CLOSE",
    "MANUAL",
    "CANDLE_CLOSED",
    "INTERVAL",
]


class TriggerConfigurationError(ConfigurationError, ValueError):
    """Raised when a declarative trigger cannot be loaded safely."""


class TriggerKind(StrEnum):
    """The complete set of scheduler trigger kinds."""

    MARKET_DATA_ARRIVAL = "MARKET_DATA_ARRIVAL"
    QUOTE_CHANGE = "QUOTE_CHANGE"
    CANDLE_CLOSED = "CANDLE_CLOSED"
    INTERVAL = "INTERVAL"
    SESSION_OPEN = "SESSION_OPEN"
    ENTRY_CUTOFF = "ENTRY_CUTOFF"
    SESSION_CLOSE = "SESSION_CLOSE"
    MANUAL = "MANUAL"


def _error(message: str) -> TriggerConfigurationError:
    return TriggerConfigurationError(message, code="invalid_strategy_trigger")


def _kind(value: object) -> TriggerKind:
    if isinstance(value, TriggerKind):
        return value
    if isinstance(value, str):
        try:
            return TriggerKind(value.strip().upper())
        except ValueError:
            pass
    raise _error(f"unknown trigger kind {value!r}")


def _timeframe(value: object) -> CandleTimeframe:
    if isinstance(value, CandleTimeframe):
        return value
    if isinstance(value, str):
        try:
            return CandleTimeframe(value.strip().lower())
        except ValueError:
            pass
    supported = ", ".join(frame.value for frame in CandleTimeframe)
    raise _error(f"unsupported candle timeframe {value!r}; expected one of {supported}")


@dataclass(frozen=True, slots=True)
class StrategyTrigger:
    """One validated trigger attached to a BUY or SELL strategy declaration.

    ``timeframe`` is present only for :attr:`TriggerKind.CANDLE_CLOSED`, and
    ``interval_seconds`` only for :attr:`TriggerKind.INTERVAL`.
    """

    kind: TriggerKind | str
    timeframe: CandleTimeframe | str | None = None
    interval_seconds: float | int | None = None

    def __post_init__(self) -> None:
        kind = _kind(self.kind)
        object.__setattr__(self, "kind", kind)

        if kind is TriggerKind.CANDLE_CLOSED:
            if self.timeframe is None:
                raise _error("CANDLE_CLOSED requires a supported timeframe")
            object.__setattr__(self, "timeframe", _timeframe(self.timeframe))
        elif self.timeframe is not None:
            raise _error(f"{kind.value} must not specify a timeframe")

        if kind is TriggerKind.INTERVAL:
            seconds = self.interval_seconds
            if (
                isinstance(seconds, bool)
                or not isinstance(seconds, int | float)
                or not isfinite(seconds)
                or seconds <= 0
            ):
                raise _error("INTERVAL requires positive finite interval_seconds")
            object.__setattr__(self, "interval_seconds", float(seconds))
        elif self.interval_seconds is not None:
            raise _error(f"{kind.value} must not specify interval_seconds")

    @classmethod
    def candle_closed(cls, timeframe: CandleTimeframe | str) -> StrategyTrigger:
        return cls(TriggerKind.CANDLE_CLOSED, timeframe=timeframe)

    @classmethod
    def interval(cls, seconds: float | int) -> StrategyTrigger:
        return cls(TriggerKind.INTERVAL, interval_seconds=seconds)

    @classmethod
    def market_data_arrival(cls) -> StrategyTrigger:
        return cls(TriggerKind.MARKET_DATA_ARRIVAL)

    @classmethod
    def quote_change(cls) -> StrategyTrigger:
        return cls(TriggerKind.QUOTE_CHANGE)

    @classmethod
    def session_open(cls) -> StrategyTrigger:
        return cls(TriggerKind.SESSION_OPEN)

    @classmethod
    def entry_cutoff(cls) -> StrategyTrigger:
        return cls(TriggerKind.ENTRY_CUTOFF)

    @classmethod
    def session_close(cls) -> StrategyTrigger:
        return cls(TriggerKind.SESSION_CLOSE)

    @classmethod
    def manual(cls) -> StrategyTrigger:
        return cls(TriggerKind.MANUAL)


# A short alias is convenient in strategy class configuration.
Trigger = StrategyTrigger

# Parameter-free declarations are safe to share because they are frozen values.
MARKET_DATA_ARRIVAL: Final = StrategyTrigger(TriggerKind.MARKET_DATA_ARRIVAL)
QUOTE_CHANGE: Final = StrategyTrigger(TriggerKind.QUOTE_CHANGE)
SESSION_OPEN: Final = StrategyTrigger(TriggerKind.SESSION_OPEN)
ENTRY_CUTOFF: Final = StrategyTrigger(TriggerKind.ENTRY_CUTOFF)
SESSION_CLOSE: Final = StrategyTrigger(TriggerKind.SESSION_CLOSE)
MANUAL: Final = StrategyTrigger(TriggerKind.MANUAL)


def CANDLE_CLOSED(timeframe: CandleTimeframe | str) -> StrategyTrigger:
    """Build a closed-candle declaration for an h.1-supported timeframe."""

    return StrategyTrigger.candle_closed(timeframe)


def INTERVAL(seconds: float | int) -> StrategyTrigger:
    """Build a positive elapsed-time declaration."""

    return StrategyTrigger.interval(seconds)
