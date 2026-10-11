"""Typed market state, transition records, and state snapshots.

Defines the five canonical market states, valid transitions, state change records,
and immutable market state snapshots consumed by portfolio and risk engines.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mike_trade.market_status.metrics import MarketMetricsSnapshot
    from mike_trade.market_status.policy import TradingAuthorizationDecision

__all__ = [
    "MarketState",
    "VALID_TRANSITIONS",
    "DIRECT_TRANSITIONS",
    "StateTransitionRecord",
    "MarketStateSnapshot",
]


class MarketState(StrEnum):
    """Canonical classification of overall market risk condition."""

    NORMAL = "NORMAL"
    WARNING = "WARNING"
    CRASH = "CRASH"
    STABILIZING = "STABILIZING"
    RECOVERY = "RECOVERY"


#: Standard progressive lifecycle transitions
PROGRESSIVE_TRANSITIONS: frozenset[tuple[MarketState, MarketState]] = frozenset({
    (MarketState.NORMAL, MarketState.WARNING),
    (MarketState.WARNING, MarketState.CRASH),
    (MarketState.CRASH, MarketState.STABILIZING),
    (MarketState.STABILIZING, MarketState.RECOVERY),
    (MarketState.RECOVERY, MarketState.NORMAL),
})

#: Permitted shortcut / direct transitions when justified
DIRECT_TRANSITIONS: frozenset[tuple[MarketState, MarketState]] = frozenset({
    # False alarm clearing back to normal
    (MarketState.WARNING, MarketState.NORMAL),
    # Sudden flash crash or deep gap from normal without intermediate warning
    (MarketState.NORMAL, MarketState.CRASH),
    # Secondary breakdown / failed stabilization back to crash
    (MarketState.STABILIZING, MarketState.CRASH),
    # Failed recovery reversing immediately into crash
    (MarketState.RECOVERY, MarketState.CRASH),
    # Direct normalization if stabilization exceeds recovery thresholds
    (MarketState.STABILIZING, MarketState.NORMAL),
    # Direct warning if recovery stalls and re-deteriorates
    (MarketState.RECOVERY, MarketState.WARNING),
})

#: Complete set of all valid state transitions
VALID_TRANSITIONS: frozenset[tuple[MarketState, MarketState]] = (
    PROGRESSIVE_TRANSITIONS | DIRECT_TRANSITIONS
)


@dataclass(frozen=True, slots=True)
class StateTransitionRecord:
    """Historical audit record of an explicit market state transition."""

    from_state: MarketState
    to_state: MarketState
    timestamp: datetime
    reason: str
    metrics_summary: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.from_state, MarketState):
            raise TypeError("from_state must be a MarketState enum member")
        if not isinstance(self.to_state, MarketState):
            raise TypeError("to_state must be a MarketState enum member")
        if not isinstance(self.timestamp, datetime) or self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be a timezone-aware datetime")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("reason must be a non-empty string")
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(UTC))
        object.__setattr__(
            self,
            "metrics_summary",
            MappingProxyType(dict(self.metrics_summary)),
        )


@dataclass(frozen=True, slots=True)
class MarketStateSnapshot:
    """Point-in-time snapshot of market state, metrics, and policy authorization."""

    state: MarketState
    as_of: datetime
    state_since: datetime
    bars_in_state: int
    previous_state: MarketState | None
    data_available: bool
    is_stale: bool
    reasons: tuple[str, ...]
    metrics: MarketMetricsSnapshot
    authorization: TradingAuthorizationDecision
    last_transition: StateTransitionRecord | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, MarketState):
            raise TypeError("state must be a MarketState enum member")
        if not isinstance(self.as_of, datetime) or self.as_of.tzinfo is None:
            raise ValueError("as_of must be a timezone-aware datetime")
        if not isinstance(self.state_since, datetime) or self.state_since.tzinfo is None:
            raise ValueError("state_since must be a timezone-aware datetime")
        if self.bars_in_state < 0:
            raise ValueError("bars_in_state cannot be negative")
        object.__setattr__(self, "as_of", self.as_of.astimezone(UTC))
        object.__setattr__(self, "state_since", self.state_since.astimezone(UTC))
        object.__setattr__(self, "reasons", tuple(self.reasons))
