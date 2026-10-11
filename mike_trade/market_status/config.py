"""Configurable thresholds, observation windows, and hysteresis parameters.

Defaults reflect conservative, defensible risk-control assumptions (not optimized
or validated crash-prediction thresholds).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

__all__ = [
    "ReferenceSymbolsConfig",
    "DrawdownThresholds",
    "VolatilityThresholds",
    "BreadthThresholds",
    "HysteresisConfig",
    "WindowConfig",
    "MarketStatusConfig",
]


@dataclass(frozen=True, slots=True)
class ReferenceSymbolsConfig:
    """Configures broad-market index/ETF reference symbols."""

    symbols: tuple[str, ...] = ("SPY", "QQQ", "IWM")
    min_required_symbols: int = 2

    def __post_init__(self) -> None:
        normalized = tuple(dict.fromkeys(s.strip().upper() for s in self.symbols if s.strip()))
        if not normalized:
            raise ValueError("symbols must contain at least one non-empty symbol")
        if self.min_required_symbols < 1 or self.min_required_symbols > len(normalized):
            raise ValueError("min_required_symbols must be between 1 and total symbols count")
        object.__setattr__(self, "symbols", normalized)


@dataclass(frozen=True, slots=True)
class DrawdownThresholds:
    """Percentage drawdown thresholds from rolling reference peaks."""

    # Drawdown threshold (negative percentage) to trigger WARNING
    warning_percent: Decimal = Decimal("-1.5")
    # Drawdown threshold to trigger standard CRASH
    crash_percent: Decimal = Decimal("-3.5")
    # Acute flash-crash threshold triggering instant CRASH without confirmation delay
    flash_crash_percent: Decimal = Decimal("-5.0")
    # Hysteresis threshold: drawdown must recover above this to return WARNING -> NORMAL
    warning_cleared_percent: Decimal = Decimal("-0.8")
    # Minimum rebound off trough (percentage points) required to consider STABILIZING
    stabilizing_trough_rebound_percent: Decimal = Decimal("0.5")
    # Drawdown threshold to consider entering RECOVERY
    recovery_drawdown_percent: Decimal = Decimal("-1.2")
    # Drawdown threshold required to return RECOVERY -> NORMAL
    normal_drawdown_percent: Decimal = Decimal("-0.5")

    def __post_init__(self) -> None:
        if self.crash_percent >= self.warning_percent:
            raise ValueError("crash_percent must be strictly more negative than warning_percent")
        if self.flash_crash_percent >= self.crash_percent:
            raise ValueError("flash_crash_percent must be strictly more negative than crash_percent")
        if self.warning_cleared_percent <= self.warning_percent:
            raise ValueError("warning_cleared_percent must be above warning_percent for hysteresis")
        if self.normal_drawdown_percent <= self.recovery_drawdown_percent:
            raise ValueError("normal_drawdown_percent must be above recovery_drawdown_percent")


@dataclass(frozen=True, slots=True)
class VolatilityThresholds:
    """Realized volatility thresholds measured in basis points per period."""

    # Realized return standard deviation in basis points to flag elevated volatility
    warning_bps: Decimal = Decimal("25")
    # Realized return standard deviation in basis points to flag acute crash volatility
    crash_bps: Decimal = Decimal("50")
    # Ratio of current volatility to crash peak volatility needed for STABILIZING (decay <= 80%)
    stabilizing_volatility_decay_ratio: Decimal = Decimal("0.80")

    def __post_init__(self) -> None:
        if self.crash_bps <= self.warning_bps:
            raise ValueError("crash_bps must be strictly greater than warning_bps")
        if not (Decimal("0") < self.stabilizing_volatility_decay_ratio <= Decimal("1.0")):
            raise ValueError("stabilizing_volatility_decay_ratio must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class BreadthThresholds:
    """Consensus fraction among reference symbols required for state changes."""

    # Fraction of reference symbols deteriorating required for WARNING
    warning_breadth_ratio: Decimal = Decimal("0.66")
    # Fraction of reference symbols deteriorating required for CRASH
    crash_breadth_ratio: Decimal = Decimal("0.66")
    # Fraction of reference symbols participating in recovery
    recovery_breadth_ratio: Decimal = Decimal("0.66")

    def __post_init__(self) -> None:
        for name, value in (
            ("warning_breadth_ratio", self.warning_breadth_ratio),
            ("crash_breadth_ratio", self.crash_breadth_ratio),
            ("recovery_breadth_ratio", self.recovery_breadth_ratio),
        ):
            if not (Decimal("0") < value <= Decimal("1.0")):
                raise ValueError(f"{name} must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class HysteresisConfig:
    """Bar confirmation counts to prevent rapid state oscillation / flip-flopping."""

    warning_confirmation_bars: int = 2
    warning_cleared_confirmation_bars: int = 3
    crash_confirmation_bars: int = 2
    stabilizing_min_bars_in_crash: int = 5
    stabilizing_no_new_lows_bars: int = 5
    stabilizing_confirmation_bars: int = 3
    recovery_min_bars_in_stabilizing: int = 5
    recovery_confirmation_bars: int = 3
    normal_confirmation_bars: int = 3
    # Relapse back to crash occurs promptly
    relapse_to_crash_bars: int = 1

    def __post_init__(self) -> None:
        for field_name in self.__annotations__:
            val = getattr(self, field_name)
            if not isinstance(val, int) or val < 1:
                raise ValueError(f"{field_name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class WindowConfig:
    """Lookback window lengths and staleness tolerances."""

    # Peak lookback in completed M1 bars (default 390 = 1 standard NYSE session)
    peak_lookback_bars: int = 390
    # Volatility sample window in completed bars
    volatility_window_bars: int = 20
    # Momentum comparison window in completed bars
    momentum_window_bars: int = 20
    # Maximum data age in seconds before market observations are deemed stale
    max_observation_age_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.peak_lookback_bars < 5:
            raise ValueError("peak_lookback_bars must be at least 5")
        if self.volatility_window_bars < 2:
            raise ValueError("volatility_window_bars must be at least 2")
        if self.momentum_window_bars < 1:
            raise ValueError("momentum_window_bars must be at least 1")
        if self.max_observation_age_seconds <= 0:
            raise ValueError("max_observation_age_seconds must be positive")


@dataclass(frozen=True, slots=True)
class MarketStatusConfig:
    """Unified configuration for the Market Status Monitor."""

    symbols: ReferenceSymbolsConfig = field(default_factory=ReferenceSymbolsConfig)
    drawdowns: DrawdownThresholds = field(default_factory=DrawdownThresholds)
    volatility: VolatilityThresholds = field(default_factory=VolatilityThresholds)
    breadth: BreadthThresholds = field(default_factory=BreadthThresholds)
    hysteresis: HysteresisConfig = field(default_factory=HysteresisConfig)
    windows: WindowConfig = field(default_factory=WindowConfig)
