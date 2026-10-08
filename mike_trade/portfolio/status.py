"""Read-only per-symbol trading phase projections.

This module deliberately accepts snapshots as inputs and contains no broker,
persistence, or configuration access.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

from mike_trade.domain.protection import ProtectionStatus
from mike_trade.portfolio.cycles import CycleState, PositionCycle

__all__ = ["SymbolTradingStatus", "build_symbol_status", "build_account_status", "phase_label"]

_ZERO = Decimal("0")
_PHASE_LABELS = {
    CycleState.DISABLED: "disabled",
    CycleState.WATCHING_BUY: "watching for entry",
    CycleState.ENTRY_RESERVED: "entry reserved",
    CycleState.BUY_SUBMITTING: "submitting entry",
    CycleState.BUY_PENDING: "entry pending",
    CycleState.PARTIALLY_OWNED: "partially filled",
    CycleState.PROTECTIVE_PENDING: "awaiting protection",
    CycleState.WATCHING_SELL: "watching for exit",
    CycleState.SELL_SUBMITTING: "submitting exit",
    CycleState.SELL_PENDING: "exit pending",
    CycleState.CLOSING: "closing position",
    CycleState.RECONCILING: "reconciling",
    CycleState.PAUSED: "paused",
    CycleState.ERROR: "error",
    CycleState.UNMANAGED: "unmanaged position",
    CycleState.CLOSED: "closed",
}

_BLOCK_ORDER = (
    "paused", "kill_switch", "reconciling", "unknown_stop", "stale_data",
    "unresolved_orders", "unsafe_account", "insufficient_buying_power", "cutoff",
    "cooldown", "lockout", "max_positions", "daily_loss_guard",
)


def _utc(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        normalized = value.astimezone(UTC)
        return normalized.isoformat(timespec="microseconds")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def phase_label(state: CycleState) -> str:
    """Return the stable display label for a raw composite cycle state."""
    if not isinstance(state, CycleState):
        raise TypeError("state must be CycleState")
    return _PHASE_LABELS[state]


@dataclass(frozen=True, slots=True)
class SymbolTradingStatus:
    """Immutable JSON-ready view of one scoped symbol cycle."""

    account_or_scenario_id: str
    mode: str
    symbol: str
    cycle_state: CycleState
    phase_label: str
    entry_order_status: str | None
    sell_order_status: str | None
    protection_status: str | None
    held_quantity: Decimal
    remaining_managed_quantity: Decimal
    average_entry_price: Decimal | None
    entry_slots_used: int
    entry_slots_allowed: int | None
    cooldown_until: datetime | None
    lockout_flags: tuple[str, ...]
    minimum_hold_until: datetime | None
    entries_blocked: bool
    block_reasons: tuple[str, ...]
    unmanaged: bool
    last_transition_reason: str
    updated_at: datetime
    stale: bool

    def to_json_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible mapping, retaining exact Decimal strings."""
        return _json_value(asdict(self))

    def to_json(self) -> str:
        """Serialize this projection to JSON."""
        return json.dumps(self.to_json_dict(), sort_keys=True)


def build_symbol_status(
    cycle: PositionCycle,
    *,
    mode: str,
    eligibility: Mapping[str, Any] | None = None,
    heartbeat_at: datetime | None = None,
    data_age: timedelta | None = None,
    stale_after: timedelta = timedelta(seconds=60),
    now: datetime | None = None,
) -> SymbolTradingStatus:
    """Build a projection from a cycle and explicit read-only gate snapshots.

    ``eligibility`` accepts explicit gate keys matching the public block reason
    names, plus ``entry_slots_used``, ``entry_slots_allowed``, ``cooldown_until``,
    ``lockout_flags`` and ``minimum_hold_until``. A gate is active when its value
    is truthy. ``data_age`` takes precedence over the age derived from heartbeat.
    """
    if not isinstance(cycle, PositionCycle):
        raise TypeError("cycle must be PositionCycle")
    if not isinstance(mode, str) or not mode.strip():
        raise ValueError("mode must be a non-empty string")
    mode = mode.strip().upper()
    gates = eligibility or {}
    if not isinstance(gates, Mapping):
        raise TypeError("eligibility must be a mapping")
    current = _utc(now, "now") or datetime.now(UTC)
    heartbeat = _utc(heartbeat_at, "heartbeat_at")
    if not isinstance(stale_after, timedelta) or stale_after < timedelta(0):
        raise ValueError("stale_after must be a non-negative timedelta")
    if data_age is not None and (not isinstance(data_age, timedelta) or data_age < timedelta(0)):
        raise ValueError("data_age must be a non-negative timedelta")
    age = data_age if data_age is not None else (current - heartbeat if heartbeat else None)
    stale = age is not None and age > stale_after
    held = cycle.remaining_qty
    unmanaged = cycle.state is CycleState.UNMANAGED
    remaining_managed = _ZERO if unmanaged else held
    cooldown = _utc(gates.get("cooldown_until"), "cooldown_until") or cycle.cooldown_until
    if cooldown is not None:
        cooldown = _utc(cooldown, "cooldown_until")
    minimum_hold_until = _utc(gates.get("minimum_hold_until"), "minimum_hold_until")

    active = {key for key in _BLOCK_ORDER if bool(gates.get(key, False))}
    if cycle.state is CycleState.PAUSED:
        active.add("paused")
    if cycle.state is CycleState.RECONCILING:
        active.add("reconciling")
    unknown_protection = cycle.protection_status is ProtectionStatus.UNKNOWN
    if held > _ZERO and not unmanaged and cycle.protection_status is None:
        unknown_protection = True
    if unknown_protection:
        active.add("unknown_stop")
    if stale or gates.get("stale_data", False):
        active.add("stale_data")
    if cooldown is not None and cooldown > current:
        active.add("cooldown")
    lockout_flags = gates.get("lockout_flags", ())
    if isinstance(lockout_flags, str):
        lockout_flags = (lockout_flags,)
    lockout_flags = tuple(str(flag) for flag in lockout_flags)
    if lockout_flags:
        active.add("lockout")
    reasons = tuple(reason for reason in _BLOCK_ORDER if reason in active)
    slots_used = int(gates.get("entry_slots_used", int(cycle.entry_slot_consumed)))
    slots_allowed = gates.get("entry_slots_allowed")
    updated_at = cycle.last_transition_at or heartbeat or current
    return SymbolTradingStatus(
        account_or_scenario_id=cycle.account_or_scenario_id,
        mode=mode,
        symbol=cycle.symbol,
        cycle_state=cycle.state,
        phase_label=phase_label(cycle.state),
        entry_order_status=cycle.entry_order_status.value if cycle.entry_order_status else None,
        sell_order_status=cycle.sell_order_status.value if cycle.sell_order_status else None,
        protection_status=cycle.protection_status.value if cycle.protection_status else None,
        held_quantity=held,
        remaining_managed_quantity=remaining_managed,
        average_entry_price=cycle.average_entry_price,
        entry_slots_used=slots_used,
        entry_slots_allowed=slots_allowed,
        cooldown_until=cooldown,
        lockout_flags=lockout_flags,
        minimum_hold_until=minimum_hold_until,
        entries_blocked=bool(reasons),
        block_reasons=reasons,
        unmanaged=unmanaged,
        last_transition_reason=cycle.last_transition_reason,
        updated_at=updated_at,
        stale=stale,
    )


def build_account_status(
    cycles: Iterable[PositionCycle],
    *,
    account_or_scenario_id: str,
    mode: str,
    watched_symbols: Iterable[str] | None = None,
    eligibility_by_symbol: Mapping[str, Mapping[str, Any]] | None = None,
    heartbeat_at: datetime | None = None,
    data_age: timedelta | None = None,
    stale_after: timedelta = timedelta(seconds=60),
    now: datetime | None = None,
) -> tuple[SymbolTradingStatus, ...]:
    """Project only the requested account/scenario and mode, in symbol order."""
    if not isinstance(account_or_scenario_id, str) or not account_or_scenario_id.strip():
        raise ValueError("account_or_scenario_id must be a non-empty string")
    if not isinstance(mode, str) or not mode.strip():
        raise ValueError("mode must be a non-empty string")
    requested_id = account_or_scenario_id.strip()
    requested_mode = mode.strip().upper()
    wanted = None if watched_symbols is None else {symbol.strip().upper() for symbol in watched_symbols}
    eligibility = eligibility_by_symbol or {}
    rows = []
    for cycle in cycles:
        if cycle.account_or_scenario_id != requested_id:
            continue
        scope_mode = cycle.account_or_scenario_id.split(":", 1)[0].upper()
        if scope_mode != requested_mode:
            continue
        if wanted is not None and cycle.symbol not in wanted:
            continue
        rows.append(
            build_symbol_status(
                cycle,
                mode=requested_mode,
                eligibility=eligibility.get(cycle.symbol, {}),
                heartbeat_at=heartbeat_at,
                data_age=data_age,
                stale_after=stale_after,
                now=now,
            )
        )
    rows.sort(key=lambda row: row.symbol)
    if len({row.symbol for row in rows}) != len(rows):
        raise ValueError("account status requires at most one cycle per watched symbol")
    if len(rows) > 10:
        raise ValueError("account status may contain at most 10 watched symbols")
    return tuple(rows)
