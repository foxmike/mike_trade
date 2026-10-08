"""
Runtime object graph, execution modes, and component ports.

The ports defined here are minimal, broker-neutral ``typing.Protocol`` types used only to type
the slots of :class:`Runtime`. Concrete implementations are wired in ``mike_trade.app``.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol


class ExecutionMode(StrEnum):
    """Supported execution modes for mike_trade."""

    BACKTEST = "BACKTEST"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE = "LIVE"


class ClockPort(Protocol):
    """Time provider."""

    def now(self) -> datetime:
        """Returns the current timezone-aware UTC datetime."""
        ...


class SchedulerPort(Protocol):
    """Internal due-time scheduler."""

    def schedule(self, name: str, due: datetime) -> None:
        """Registers a named task to fire at a timezone-aware UTC time."""
        ...


class BrokerPort(Protocol):
    """Broker-neutral order gateway."""

    def submit_order(self, order: object) -> object:
        """Submits an order and returns the broker acknowledgement."""
        ...

    def cancel_order(self, order_id: str) -> None:
        """Requests cancellation of an open order."""
        ...


class MarketDataPort(Protocol):
    """Shared market-data service."""

    def subscribe(self, symbols: Sequence[str]) -> None:
        """Subscribes to market data for the given symbols."""
        ...


class ResearchPort(Protocol):
    """Optional offline research-data source."""

    def fetch_inputs(self, symbol: str) -> Mapping[str, object]:
        """Returns research inputs declared for the symbol."""
        ...


class PersistencePort(Protocol):
    """Authoritative append-only event journal."""

    def append_event(self, event: Mapping[str, object]) -> None:
        """Appends a trade-affecting event to the journal."""
        ...


class PortfolioPort(Protocol):
    """Position-cycle and account views."""

    def positions(self) -> Sequence[object]:
        """Returns the currently managed positions."""
        ...


class RiskPort(Protocol):
    """Entry-eligibility policy."""

    def check_entry(self, intent: object) -> None:
        """Raises ``EntryBlockedError`` when the entry intent is not eligible."""
        ...


class ExecutionPort(Protocol):
    """Provider-neutral order manager."""

    def submit_intent(self, intent: object) -> None:
        """Accepts an order intent for durable, serialized execution."""
        ...


class ReportingPort(Protocol):
    """Reporting and audit export."""

    def publish(self, report: Mapping[str, object]) -> None:
        """Publishes a report artifact."""
        ...


class ControlsPort(Protocol):
    """Operator control state."""

    def entries_enabled(self) -> bool:
        """Returns whether new entries are currently permitted."""
        ...


class SystemClock:
    """Default system clock providing timezone-aware UTC datetimes."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class Runtime:
    """Dependency-injected composition root container."""

    mode: ExecutionMode
    clock: ClockPort
    scheduler: SchedulerPort
    broker: BrokerPort
    market_data: MarketDataPort
    research: ResearchPort | None
    persistence: PersistencePort
    portfolio: PortfolioPort
    risk: RiskPort
    execution: ExecutionPort
    reporting: ReportingPort
    controls: ControlsPort
    logger: logging.Logger

    def start(self) -> None:
        """Lifecycle hook to start the runtime.

        Does not place orders, mutate positions, or contact a broker.
        """
        self.logger.info("Runtime started", extra={"mode": self.mode.value})

    def stop(self) -> None:
        """Lifecycle hook to stop the runtime.

        Does not liquidate positions, cancel orders (including protective stops), or contact
        a broker.
        """
        self.logger.info("Runtime stopped", extra={"mode": self.mode.value})
