"""Market Status Monitor subsystem.

Classifies broad-market risk states and provides deterministic trading
authorization decisions for portfolio and risk execution engines.
"""

from __future__ import annotations

from mike_trade.market_status.config import (
    BreadthThresholds,
    DrawdownThresholds,
    HysteresisConfig,
    MarketStatusConfig,
    ReferenceSymbolsConfig,
    VolatilityThresholds,
    WindowConfig,
)
from mike_trade.market_status.metrics import (
    MarketMetricsSnapshot,
    calculate_market_metrics,
)
from mike_trade.market_status.monitor import MarketStatusMonitor
from mike_trade.market_status.persistence import (
    InMemoryMarketStatusRepository,
    JsonFileMarketStatusRepository,
    MarketStatusRepository,
)
from mike_trade.market_status.policy import (
    ActionKind,
    MarketStatusEntryGate,
    PermissionLevel,
    TradingAuthorizationDecision,
    TradingAuthorizationPolicy,
)
from mike_trade.market_status.state import (
    DIRECT_TRANSITIONS,
    PROGRESSIVE_TRANSITIONS,
    VALID_TRANSITIONS,
    MarketState,
    MarketStateSnapshot,
    StateTransitionRecord,
)

__all__ = [
    # State
    "MarketState",
    "PROGRESSIVE_TRANSITIONS",
    "DIRECT_TRANSITIONS",
    "VALID_TRANSITIONS",
    "StateTransitionRecord",
    "MarketStateSnapshot",
    # Policy
    "ActionKind",
    "PermissionLevel",
    "TradingAuthorizationDecision",
    "TradingAuthorizationPolicy",
    "MarketStatusEntryGate",
    # Config
    "ReferenceSymbolsConfig",
    "DrawdownThresholds",
    "VolatilityThresholds",
    "BreadthThresholds",
    "HysteresisConfig",
    "WindowConfig",
    "MarketStatusConfig",
    # Metrics
    "MarketMetricsSnapshot",
    "calculate_market_metrics",
    # Monitor
    "MarketStatusMonitor",
    # Persistence
    "MarketStatusRepository",
    "InMemoryMarketStatusRepository",
    "JsonFileMarketStatusRepository",
]
