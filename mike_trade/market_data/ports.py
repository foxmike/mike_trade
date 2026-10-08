"""Shared market-data service contracts.

Strategies never use these ports directly; only the shared market-data service
acquires and distributes provider data.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from mike_trade.domain.accounts import MarketDataCapabilities

if TYPE_CHECKING:
    from mike_trade.domain.market import Candle, MarketEvent, Timeframe


@runtime_checkable
class MarketDataPort(Protocol):
    """Provider-facing interface consumed by the shared market-data service."""

    def capabilities(self) -> MarketDataCapabilities:
        ...

    async def subscribe(self, symbols: Sequence[str]) -> AsyncIterator["MarketEvent"]:
        ...

    async def unsubscribe(self, symbols: Sequence[str]) -> None:
        ...

    async def fetch_bars(
        self,
        symbol: str,
        timeframe: "Timeframe",
        start: datetime,
        end: datetime,
    ) -> Sequence["Candle"]:
        ...

    async def close(self) -> None:
        ...


class ResearchPort(Protocol):
    """Optional research input source; core trading does not require it."""

    async def fetch(self, symbol: str, as_of: datetime) -> Mapping[str, object] | None:
        ...
