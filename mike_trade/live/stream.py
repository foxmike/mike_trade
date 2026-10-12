"""Trade-update stream -> reconciliation trigger.

Alpaca's ``trade_updates`` stream is *advisory* here.  Its ``execution_id`` is a different
identifier from the account-activity id the ledger keys fills by, so the stream never writes a
fill: it only wakes the reconciler, which then re-reads positions, orders and activities and
ingests executions idempotently.  A reconnect is an explicit gap marker and triggers a pass
too, so a missed message cannot leave the engine believing it still owns shares.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from mike_trade.brokers.alpaca.updates import (
    StreamAuthError,
    StreamReconnected,
    TradeUpdate,
    TradeUpdateStream,
)

__all__ = ["TradeUpdateWatcher", "WebsocketsTransport"]


class WebsocketsTransport:
    """Production websocket transport (``websockets`` is imported only when connecting)."""

    async def connect(self, url: str) -> Any:
        from websockets.asyncio.client import connect

        return await connect(url)


class TradeUpdateWatcher:
    """Consume the stream forever, kicking ``on_event`` for every update or gap."""

    def __init__(
        self,
        stream_factory: Callable[[], TradeUpdateStream],
        on_event: Callable[[], None],
        *,
        retry_delay: float = 5.0,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._stream_factory = stream_factory
        self._on_event = on_event
        self._retry_delay = retry_delay
        self._sleep = sleep
        self.updates = 0
        self.gaps = 0
        self.connected = False
        self.last_error: str | None = None

    async def run(self) -> None:
        while True:
            try:
                stream = self._stream_factory()
                self.connected = True
                async for event in stream.events():
                    if isinstance(event, StreamReconnected):
                        self.gaps += 1
                    elif isinstance(event, TradeUpdate):
                        self.updates += 1
                    self._on_event()
            except asyncio.CancelledError:
                self.connected = False
                raise
            except StreamAuthError as exc:
                self.last_error = f"auth: {exc}"
            except Exception as exc:  # reconnect bounds exhausted, bad frame, ...
                self.last_error = f"{type(exc).__name__}: {exc}"
            self.connected = False
            self._on_event()  # whatever happened while disconnected must be reconciled
            await self._sleep(self._retry_delay)


