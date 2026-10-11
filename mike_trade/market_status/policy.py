"""Trading authorization policy and portfolio decision contracts.

Translates market states into typed trading authorization recommendations.
Guarantees the core safety invariant: protective stops, SELL exits, COVER exits,
and emergency liquidations are NEVER blocked under any market state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from mike_trade.market_status.state import MarketState

if TYPE_CHECKING:
    from mike_trade.domain.signals import Signal, SignalAction
    from mike_trade.portfolio.manager import EntryGateDecision, EntryRequest

__all__ = [
    "ActionKind",
    "PermissionLevel",
    "TradingAuthorizationDecision",
    "TradingAuthorizationPolicy",
    "MarketStatusEntryGate",
]


class ActionKind(StrEnum):
    """Trading action types classified by authorization policy."""

    BUY = "BUY"
    SHORT = "SHORT"
    SELL = "SELL"
    COVER = "COVER"
    PROTECTIVE_STOP = "PROTECTIVE_STOP"
    EMERGENCY_LIQUIDATION = "EMERGENCY_LIQUIDATION"


class PermissionLevel(StrEnum):
    """Authorization permission degree for new position entries."""

    ALLOWED = "ALLOWED"
    RESTRICTED = "RESTRICTED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class TradingAuthorizationDecision:
    """Typed authorization decision consumed by the portfolio and risk engines."""

    state: MarketState
    evaluated_at: datetime
    buy_allowed: bool
    short_allowed: bool
    sell_allowed: bool = True
    cover_allowed: bool = True
    protective_stops_allowed: bool = True
    emergency_liquidation_allowed: bool = True
    buy_permission: PermissionLevel = PermissionLevel.ALLOWED
    short_permission: PermissionLevel = PermissionLevel.ALLOWED
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, MarketState):
            raise TypeError("state must be a MarketState enum member")
        if not isinstance(self.evaluated_at, datetime) or self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be a timezone-aware datetime")
        object.__setattr__(self, "evaluated_at", self.evaluated_at.astimezone(UTC))
        object.__setattr__(self, "reasons", tuple(self.reasons))

        # Core invariant: exits, stops, covers, and liquidations MUST NEVER be blocked
        if not self.sell_allowed:
            raise ValueError("Invariant violation: SELL exits must never be blocked")
        if not self.cover_allowed:
            raise ValueError("Invariant violation: COVER exits must never be blocked")
        if not self.protective_stops_allowed:
            raise ValueError("Invariant violation: protective stops must never be blocked")
        if not self.emergency_liquidation_allowed:
            raise ValueError("Invariant violation: emergency liquidation must never be blocked")

    def is_action_allowed(self, action: ActionKind | str) -> bool:
        """Query whether a specific action kind is permitted under this decision."""
        raw = getattr(action, "value", action)
        normalized = str(raw).strip().upper()
        if normalized in {"SELL", "EXIT_LONG"}:
            return self.sell_allowed
        if normalized in {"COVER", "COVER_SHORT", "EXIT_SHORT"}:
            return self.cover_allowed
        if normalized in {"PROTECTIVE_STOP", "STOP"}:
            return self.protective_stops_allowed
        if normalized in {"EMERGENCY_LIQUIDATION", "LIQUIDATION"}:
            return self.emergency_liquidation_allowed
        if normalized in {"BUY", "ENTER_LONG", "LONG"}:
            return self.buy_allowed
        if normalized in {"SHORT", "ENTER_SHORT"}:
            return self.short_allowed
        # Default safe behavior for unrecognized actions: fail closed
        return False


@dataclass(frozen=True, slots=True)
class TradingAuthorizationPolicy:
    """Configurable policy mapping market conditions to action authorizations."""

    # When True, WARNING state restricts new BUY entries (defaults to True per spec)
    warning_restricts_buy: bool = True
    # When True, STABILIZING state blocks new BUY entries (defaults to True per spec)
    stabilizing_blocks_buy: bool = True
    # When True, STABILIZING state restricts new SHORT entries (defaults to True per spec)
    stabilizing_restricts_short: bool = True
    # When True, RECOVERY state allows qualified BUY entries (defaults to True per spec)
    recovery_allows_buy: bool = True
    # When True, RECOVERY state restricts new SHORT entries (defaults to True per spec)
    recovery_restricts_short: bool = True
    # When True, CRASH state allows qualified SHORT entries (defaults to True per spec)
    crash_allows_short: bool = True

    def evaluate(
        self,
        state: MarketState,
        evaluated_at: datetime,
        *,
        data_available: bool = True,
        is_stale: bool = False,
    ) -> TradingAuthorizationDecision:
        """Derive an authoritative trading authorization decision from market state."""
        # Conservative fail-closed behavior when market-state data is unavailable or stale.
        # Note: Exits, stops, covers, and liquidations remain unconditionally permitted!
        if not data_available:
            return TradingAuthorizationDecision(
                state=state,
                evaluated_at=evaluated_at,
                buy_allowed=False,
                short_allowed=False,
                buy_permission=PermissionLevel.BLOCKED,
                short_permission=PermissionLevel.BLOCKED,
                reasons=("MARKET_DATA_UNAVAILABLE",),
            )
        if is_stale:
            return TradingAuthorizationDecision(
                state=state,
                evaluated_at=evaluated_at,
                buy_allowed=False,
                short_allowed=False,
                buy_permission=PermissionLevel.BLOCKED,
                short_permission=PermissionLevel.BLOCKED,
                reasons=("MARKET_DATA_STALE",),
            )

        match state:
            case MarketState.NORMAL:
                return TradingAuthorizationDecision(
                    state=state,
                    evaluated_at=evaluated_at,
                    buy_allowed=True,
                    short_allowed=True,
                    buy_permission=PermissionLevel.ALLOWED,
                    short_permission=PermissionLevel.ALLOWED,
                    reasons=(),
                )

            case MarketState.WARNING:
                buy_allowed = not self.warning_restricts_buy
                return TradingAuthorizationDecision(
                    state=state,
                    evaluated_at=evaluated_at,
                    buy_allowed=buy_allowed,
                    short_allowed=True,
                    buy_permission=PermissionLevel.RESTRICTED,
                    short_permission=PermissionLevel.ALLOWED,
                    reasons=("MARKET_WARNING_ELEVATED_RISK",),
                )

            case MarketState.CRASH:
                short_allowed = self.crash_allows_short
                return TradingAuthorizationDecision(
                    state=state,
                    evaluated_at=evaluated_at,
                    buy_allowed=False,
                    short_allowed=short_allowed,
                    buy_permission=PermissionLevel.BLOCKED,
                    short_permission=(
                        PermissionLevel.ALLOWED if short_allowed else PermissionLevel.BLOCKED
                    ),
                    reasons=("MARKET_CRASH_ACUTE_RISK",),
                )

            case MarketState.STABILIZING:
                buy_allowed = not self.stabilizing_blocks_buy
                short_allowed = not self.stabilizing_restricts_short
                return TradingAuthorizationDecision(
                    state=state,
                    evaluated_at=evaluated_at,
                    buy_allowed=buy_allowed,
                    short_allowed=short_allowed,
                    buy_permission=PermissionLevel.BLOCKED if not buy_allowed else PermissionLevel.RESTRICTED,
                    short_permission=PermissionLevel.RESTRICTED if not short_allowed else PermissionLevel.ALLOWED,
                    reasons=("MARKET_STABILIZING_OBSERVATION",),
                )

            case MarketState.RECOVERY:
                buy_allowed = self.recovery_allows_buy
                short_allowed = not self.recovery_restricts_short
                return TradingAuthorizationDecision(
                    state=state,
                    evaluated_at=evaluated_at,
                    buy_allowed=buy_allowed,
                    short_allowed=short_allowed,
                    buy_permission=PermissionLevel.RESTRICTED if buy_allowed else PermissionLevel.BLOCKED,
                    short_permission=PermissionLevel.RESTRICTED if not short_allowed else PermissionLevel.ALLOWED,
                    reasons=("MARKET_RECOVERY_CONDITIONAL",),
                )


class MarketStatusEntryGate:
    """Adapter implementing the portfolio EntryGate protocol using MarketStatusMonitor.

    Consulted only on entry requests; exit requests and risk liquidations
    are never blocked. Acts as an additional restriction layer on top of
    any existing risk/eligibility gate without bypassing or weakening them.
    """

    def __init__(self, monitor: Any, inner_gate: Any = None) -> None:
        self._monitor = monitor
        self._inner_gate = inner_gate
        self._manual_blocked_reasons: tuple[str, ...] = ()

    @property
    def monitor(self) -> Any:
        return self._monitor

    @property
    def inner_gate(self) -> Any:
        return self._inner_gate

    # --- Runtime / SHADOW EntryGate protocol compatibility ---

    @property
    def authorized(self) -> bool:
        if self._manual_blocked_reasons:
            return False
        if self._inner_gate is not None:
            if hasattr(self._inner_gate, "authorized") and not self._inner_gate.authorized:
                return False
            if hasattr(self._inner_gate, "entries_enabled") and not self._inner_gate.entries_enabled():
                return False
        snap = self._monitor.evaluate()
        return snap.authorization.buy_allowed

    @property
    def reasons(self) -> tuple[str, ...]:
        result: list[str] = list(self._manual_blocked_reasons)
        if self._inner_gate is not None and hasattr(self._inner_gate, "reasons"):
            result.extend(self._inner_gate.reasons)
        snap = self._monitor.evaluate()
        if not snap.authorization.buy_allowed:
            result.extend(snap.authorization.reasons or (f"MARKET_STATE_{snap.state.value}_BUY_RESTRICTED",))
        return tuple(result)

    def entries_enabled(self) -> bool:
        return self.authorized

    def block(self, *reasons: str) -> None:
        normalized = tuple(str(r).strip() for r in reasons if str(r).strip())
        self._manual_blocked_reasons = normalized or ("ENTRY_BLOCKED",)
        if self._inner_gate is not None and hasattr(self._inner_gate, "block"):
            self._inner_gate.block(*reasons)

    def allow(self) -> None:
        self._manual_blocked_reasons = ()
        if self._inner_gate is not None and hasattr(self._inner_gate, "allow"):
            self._inner_gate.allow()

    # --- PortfolioManager EntryGate protocol compatibility ---

    async def evaluate_entry(
        self,
        account_scope_id: str,
        symbol: str,
        strategy_id: str,
        now: datetime,
        *,
        action: Any = "ENTER_LONG",
    ) -> EntryGateDecision:
        from mike_trade.portfolio.manager import EntryGateDecision

        if self._manual_blocked_reasons:
            return EntryGateDecision(allowed=False, reasons=self._manual_blocked_reasons)

        # 1. Existing risk/eligibility checks are consulted first (never bypassed or weakened)
        if self._inner_gate is not None and hasattr(self._inner_gate, "evaluate_entry"):
            inner_decision = await self._inner_gate.evaluate_entry(
                account_scope_id, symbol, strategy_id, now
            )
            if inner_decision.blocked:
                return inner_decision

        # 2. Market Status evaluation
        snapshot = self._monitor.evaluate(as_of=now)
        decision: TradingAuthorizationDecision = snapshot.authorization
        action_name = getattr(action, "value", str(action)).upper()

        if action_name in {"ENTER_LONG", "BUY"}:
            if not decision.buy_allowed:
                reasons = decision.reasons or (f"MARKET_STATE_{decision.state.value}_BUY_RESTRICTED",)
                return EntryGateDecision(allowed=False, reasons=reasons)
            return EntryGateDecision(allowed=True)

        if action_name in {"ENTER_SHORT", "SHORT"}:
            if not decision.short_allowed:
                reasons = decision.reasons or (f"MARKET_STATE_{decision.state.value}_SHORT_RESTRICTED",)
                return EntryGateDecision(allowed=False, reasons=reasons)
            return EntryGateDecision(allowed=True)

        # Exits, stops, covers, and liquidations are NEVER blocked
        return EntryGateDecision(allowed=True)

    async def evaluate(self, request: EntryRequest) -> EntryGateDecision:
        """Evaluate entry request against current market authorization decision."""
        from mike_trade.portfolio.manager import EntryGateDecision

        if self._manual_blocked_reasons:
            return EntryGateDecision(allowed=False, reasons=self._manual_blocked_reasons)

        if self._inner_gate is not None and hasattr(self._inner_gate, "evaluate"):
            inner_decision = await self._inner_gate.evaluate(request)
            if inner_decision.blocked:
                return inner_decision

        signal = getattr(request, "signal", None)
        action = getattr(signal, "action", None)
        bar_time = getattr(signal, "bar_time", None) or getattr(signal, "generated_at", None)

        snapshot = self._monitor.evaluate()
        decision: TradingAuthorizationDecision = snapshot.authorization

        action_name = getattr(action, "value", str(action)).upper() if action else "ENTER_LONG"

        if action_name in {"ENTER_LONG", "BUY"}:
            if not decision.buy_allowed:
                reasons = decision.reasons or (f"MARKET_STATE_{decision.state.value}_BUY_RESTRICTED",)
                return EntryGateDecision(allowed=False, reasons=reasons)
            return EntryGateDecision(allowed=True)

        if action_name in {"ENTER_SHORT", "SHORT"}:
            if not decision.short_allowed:
                reasons = decision.reasons or (f"MARKET_STATE_{decision.state.value}_SHORT_RESTRICTED",)
                return EntryGateDecision(allowed=False, reasons=reasons)
            return EntryGateDecision(allowed=True)

        # Exits are unconditionally allowed through entry gate
        return EntryGateDecision(allowed=True)

    async def record_entry_fill(self, cycle: Any) -> None:
        if self._inner_gate is not None and hasattr(self._inner_gate, "record_entry_fill"):
            await self._inner_gate.record_entry_fill(cycle)

    async def record_cycle_closed(self, cycle: Any) -> None:
        if self._inner_gate is not None and hasattr(self._inner_gate, "record_cycle_closed"):
            await self._inner_gate.record_cycle_closed(cycle)
