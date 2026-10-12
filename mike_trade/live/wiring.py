"""Compose the PAPER/LIVE execution stack onto the existing runtime graph.

Called lazily from :func:`mike_trade.app.build_runtime`; the broker adapter, persistence and
entry gate are the objects the graph already owns.  Nothing here opens a network connection or a
database: the ledger is created when the runtime opens the database (``bind``) and credentials
are read only when the adapter validates its environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from mike_trade.domain.accounts import AccountScope
from mike_trade.domain.errors import ConfigurationError
from mike_trade.domain.protection import ProtectionPolicy
from mike_trade.domain.trading import BudgetPolicy, EntryEligibilityPolicy
from mike_trade.live.gateway import AlpacaLiveGateway
from mike_trade.live.ledger import LiveLedger
from mike_trade.live.lifecycle import LiveSettings, LiveStrategyLifecycle
from mike_trade.live.reconciler import LiveReconciler
from mike_trade.live.stream import TradeUpdateWatcher, WebsocketsTransport

__all__ = ["LiveStack", "build_live_stack", "eod_offset"]


class _StartupReconciliation:
    """Runtime's ``reconciliation`` component: the lifecycle's startup pass."""

    def __init__(self, lifecycle: LiveStrategyLifecycle) -> None:
        self._lifecycle = lifecycle

    async def reconcile(self, facts: Any = None) -> object:
        return await self._lifecycle.reconcile_at_startup(facts)


@dataclass(slots=True)
class LiveStack:
    lifecycle: LiveStrategyLifecycle
    gateway: AlpacaLiveGateway
    reconciliation: _StartupReconciliation


def eod_offset(at: str) -> timedelta:
    """Time before the 16:00 close at which liquidation starts (``"15:55"`` -> 5 minutes)."""
    hour, minute = (int(part) for part in at.split(":", 1))
    offset = (16 * 60) - (hour * 60 + minute)
    if not 0 < offset < 6 * 60:
        raise ConfigurationError(
            f"end-of-day close time {at!r} must be between 10:00 and 16:00 ET",
            code="EOD_TIME_INVALID",
        )
    return timedelta(minutes=offset)


def build_live_stack(
    *,
    settings: Any,
    adapter: Any,
    persistence: Any,
    clock: Any,
    entry_gate: Any,
    registry: Any,
    protection_policy: ProtectionPolicy,
    scope: AccountScope,
) -> LiveStack:
    gateway = AlpacaLiveGateway(adapter, clock)
    eligibility: EntryEligibilityPolicy = settings.live_eligibility or EntryEligibilityPolicy(1, 2)
    live_settings = LiveSettings(
        per_symbol_budget=settings.per_symbol_budget,
        minimum_hold=settings.live_minimum_hold,
        eligibility=eligibility,
        eod_enabled=bool(settings.auto_eod_liquidation),
        eod_offset=eod_offset(settings.eod_close_at),
    )

    def ledger_factory() -> LiveLedger:
        session = persistence.session
        if session is None:
            raise RuntimeError("the operational database must be open before the ledger binds")
        budget = BudgetPolicy(
            settings.per_symbol_budget, settings.total_budget, max(1, len(settings.symbols))
        )
        return LiveLedger(session.bind_scope(scope.scope_id), clock, scope, budget)

    def watcher_factory(reconciler: LiveReconciler) -> Any:
        if not settings.live_trade_updates or adapter.http_transport is not None:
            return None
        from mike_trade.brokers.alpaca.auth import (
            AlpacaCredentials,
            AlpacaEnvironment,
        )
        from mike_trade.brokers.alpaca.updates import TradeUpdateStream

        environment = AlpacaEnvironment(adapter.mode.value)
        credentials = AlpacaCredentials.from_env(environment, adapter.environ)
        return TradeUpdateWatcher(
            lambda: TradeUpdateStream(credentials, WebsocketsTransport()), reconciler.kick
        )

    lifecycle = LiveStrategyLifecycle(
        gateway=gateway,
        ledger_factory=ledger_factory,
        persistence=persistence,
        clock=clock,
        entry_gate=entry_gate,
        cutoff_minutes=settings.entry_cutoff_minutes,
        protection_policy=protection_policy,
        buy_parameters={},
        sell_parameters={},
        assignments=tuple(settings.symbol_strategies),
        registry=registry,
        stop_specs={
            symbol: spec
            for symbol, spec in settings.symbol_stop_policies.items()
            if not (
                spec.get("type") == "native_trailing"
                and spec.get("trail_percent") == settings.stop_trail_percent
            )
        },
        settings=live_settings,
        symbols=tuple(settings.symbols),
        reconcile_interval=float(settings.live_reconcile_interval_seconds),
        watcher_factory=watcher_factory,
    )

    def persist_before_send(client_order_id: str, _request: object) -> None:
        """A broker POST is refused unless its intent is already durably recorded."""
        lifecycle.bind()
        if lifecycle.ledger.intent_by_client(client_order_id) is None:
            raise ConfigurationError(
                f"order {client_order_id!r} has no durably recorded intent; refusing to send",
                code="INTENT_NOT_PERSISTED",
            )

    adapter.persist_before_send = persist_before_send
    return LiveStack(lifecycle, gateway, _StartupReconciliation(lifecycle))


def default_capability_path(settings: Any) -> Path:
    configured = settings.alpaca_capability_records
    return Path(configured) if configured else Path(settings.data_dir) / "alpaca_capabilities.json"

