"""Broker-neutral provider contracts.

Adapters translate provider types at the boundary and never leak SDK objects
through these interfaces.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from mike_trade.domain.accounts import AccountEnvironment, AccountSnapshot, BrokerCapabilities

if TYPE_CHECKING:
    from mike_trade.domain.trading import BrokerOrder, Fill, OrderIntent, Position


@runtime_checkable
class BrokerPort(Protocol):
    """Async broker gateway whose adapters normalize provider objects at the boundary."""

    environment: AccountEnvironment

    def capabilities(self) -> BrokerCapabilities:
        """Describe explicitly supported operations for this environment."""
        ...

    async def get_account(self) -> AccountSnapshot:
        ...

    async def list_positions(self) -> Sequence["Position"]:
        ...

    async def list_open_orders(self) -> Sequence["BrokerOrder"]:
        ...

    async def get_order(self, broker_order_id: str) -> "BrokerOrder | None":
        ...

    async def get_order_by_client_id(self, client_order_id: str) -> "BrokerOrder | None":
        ...

    async def submit_order(self, intent: "OrderIntent") -> "BrokerOrder":
        ...

    async def replace_order(
        self,
        broker_order_id: str,
        *,
        stop_price: Decimal | None = None,
        trail: Decimal | None = None,
        quantity: Decimal | None = None,
    ) -> "BrokerOrder":
        ...

    async def cancel_order(self, broker_order_id: str) -> None:
        ...

    async def list_fills(self, since: datetime) -> Sequence["Fill"]:
        ...
