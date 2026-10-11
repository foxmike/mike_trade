"""Pydantic v2 schemas for Market Status Monitor REST API v2."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

MarketState = Literal["NORMAL", "WARNING", "CRASH", "STABILIZING", "RECOVERY"]
PermissionLevel = Literal["ALLOWED", "RESTRICTED", "BLOCKED"]


class TradingAuthorizationResponse(BaseModel):
    """Trading authorization recommendations derived from market state."""

    state: MarketState = Field(..., description="Active market risk state")
    evaluated_at: datetime = Field(..., description="Evaluation timestamp")
    buy_allowed: bool = Field(..., description="Whether new BUY entries are permitted")
    short_allowed: bool = Field(..., description="Whether new SHORT entries are permitted")
    sell_allowed: bool = Field(
        default=True, description="Safety invariant: SELL exits are NEVER blocked"
    )
    cover_allowed: bool = Field(
        default=True, description="Safety invariant: COVER exits are NEVER blocked"
    )
    protective_stops_allowed: bool = Field(
        default=True, description="Safety invariant: protective stops are NEVER blocked"
    )
    emergency_liquidation_allowed: bool = Field(
        default=True,
        description="Safety invariant: emergency liquidation is NEVER blocked",
    )
    buy_permission: PermissionLevel = Field(..., description="Degree of BUY entry authorization")
    short_permission: PermissionLevel = Field(
        ..., description="Degree of SHORT entry authorization"
    )
    reasons: list[str] = Field(default_factory=list, description="Policy restriction explanations")


class PolicyRulesResponse(BaseModel):
    """Configured trading authorization policy rules."""

    warning_restricts_buy: bool = Field(
        default=True, description="WARNING state restricts new BUY entries"
    )
    stabilizing_blocks_buy: bool = True
    stabilizing_restricts_short: bool = True
    recovery_allows_buy: bool = True
    recovery_restricts_short: bool = True
    crash_allows_short: bool = True


class CompositeMetricsResponse(BaseModel):
    """Broad-market consensus and episode metrics across reference symbols."""

    composite_drawdown_percent: float | None = Field(
        None, description="Average percentage drawdown across active reference symbols"
    )
    worst_drawdown_percent: float | None = Field(
        None, description="Most severe individual drawdown among reference symbols"
    )
    realized_volatility_bps: float | None = Field(
        None, description="Sample standard deviation of returns in basis points (20m)"
    )
    momentum_bps: float | None = Field(
        None, description="Percentage difference from 20-bar SMA in basis points"
    )
    breadth_weakness_ratio: float | None = Field(
        None, description="Fraction of reference symbols breaching warning threshold"
    )
    trough_drawdown_percent: float | None = Field(
        None, description="Deepest composite drawdown reached during current episode"
    )
    bars_since_trough: int = Field(
        0, description="Completed 1m bars elapsed since lowest trough"
    )
    peak_volatility_bps: float | None = Field(
        None, description="Peak realized volatility recorded during crash episode"
    )
    rebound_from_trough_percent: float | None = Field(
        None, description="Rebound magnitude (percentage points) off the trough"
    )
    vix_level: float | None = Field(None, description="External VIX indicator if recorded")
    vix_timestamp: datetime | None = Field(None, description="VIX recording timestamp")


class BenchmarkSymbolResponse(BaseModel):
    """Real-time metrics and threshold checks for an individual reference ETF."""

    symbol: str = Field(..., description="Reference index symbol (SPY, QQQ, IWM)")
    latest_price: float | None = Field(None, description="Most recent completed 1m close price")
    peak_price: float | None = Field(
        None, description="Rolling peak high over lookback window (default 390 bars)"
    )
    drawdown_percent: float | None = Field(
        None, description="Percentage decline from peak high"
    )
    realized_volatility_bps: float | None = Field(
        None, description="Realized volatility in basis points"
    )
    momentum_bps: float | None = Field(
        None, description="Momentum distance from 20-bar SMA in basis points"
    )
    last_bar_timestamp: datetime | None = Field(
        None, description="Start timestamp of the most recent bar"
    )
    bar_count: int = Field(0, description="Available completed bars in observation window")
    is_stale: bool = Field(False, description="Whether benchmark data age exceeds freshness limit")
    warning_breached: bool = Field(
        False, description="Whether drawdown breaches warning threshold (-1.5%)"
    )
    crash_breached: bool = Field(
        False, description="Whether drawdown breaches crash threshold (-3.5%)"
    )
    flash_crash_breached: bool = Field(
        False, description="Whether drawdown breaches acute flash crash threshold (-5.0%)"
    )


class ThresholdsConfigResponse(BaseModel):
    """Configured thresholds, hysteresis bars, and window parameters."""

    reference_symbols: list[str] = Field(default_factory=lambda: ["SPY", "QQQ", "IWM"])
    min_required_symbols: int = 2
    warning_drawdown_percent: float = -1.5
    crash_drawdown_percent: float = -3.5
    flash_crash_drawdown_percent: float = -5.0
    warning_cleared_percent: float = -0.8
    stabilizing_trough_rebound_percent: float = 0.5
    recovery_drawdown_percent: float = -1.2
    normal_drawdown_percent: float = -0.5
    warning_volatility_bps: float = 25.0
    crash_volatility_bps: float = 50.0
    stabilizing_volatility_decay_ratio: float = 0.80
    warning_breadth_ratio: float = 0.66
    crash_breadth_ratio: float = 0.66
    recovery_breadth_ratio: float = 0.66
    warning_confirmation_bars: int = 2
    warning_cleared_confirmation_bars: int = 3
    crash_confirmation_bars: int = 2
    stabilizing_min_bars_in_crash: int = 5
    stabilizing_no_new_lows_bars: int = 5
    stabilizing_confirmation_bars: int = 3
    recovery_min_bars_in_stabilizing: int = 5
    recovery_confirmation_bars: int = 3
    normal_confirmation_bars: int = 3
    relapse_to_crash_bars: int = 1
    peak_lookback_bars: int = 390
    volatility_window_bars: int = 20
    momentum_window_bars: int = 20
    max_observation_age_seconds: float = 300.0


class MarketStatusOverviewResponse(BaseModel):
    """Complete Market Status Monitor dashboard overview."""

    account_id: str = Field(..., description="Active account scope identifier")
    market_state: MarketState = Field(..., description="Active market risk state")
    previous_state: MarketState | None = Field(None, description="Previous market risk state")
    state_since: datetime = Field(..., description="Timestamp of most recent state transition")
    duration_seconds: float = Field(..., description="Seconds elapsed in current state")
    bars_in_state: int = Field(0, description="Completed 1m reference bars in current state")
    last_evaluation_time: datetime | None = Field(
        None, description="Timestamp of most recent state evaluation"
    )
    last_step_end: datetime | None = Field(
        None, description="Timestamp of latest completed minute bar that advanced the monitor"
    )
    engine_online: bool = Field(..., description="Whether engine heartbeat is actively ticking")
    engine_heartbeat_at: datetime | None = Field(None, description="Last recorded engine heartbeat")
    is_stale: bool = Field(..., description="Whether market data or engine status is stale")
    data_available: bool = Field(..., description="Whether market observations were recorded")
    fail_closed: bool = Field(
        default=False, description="Whether monitor is in fail-closed safety lockout"
    )
    fail_closed_reason: str | None = Field(None, description="Reason for fail-closed lockout")
    reason: str = Field(..., description="Trigger reason for the current state")
    recovery_requirements: str = Field(
        ..., description="Exact operational criteria required to advance from current state"
    )
    authorization: TradingAuthorizationResponse
    policy_rules: PolicyRulesResponse
    composite_metrics: CompositeMetricsResponse
    benchmarks: list[BenchmarkSymbolResponse] = Field(default_factory=list)


class TransitionRecordResponse(BaseModel):
    """Historical audit record of an explicit market state transition."""

    from_state: str = Field(..., description="State transitioned from")
    to_state: str = Field(..., description="State transitioned to")
    timestamp: datetime = Field(..., description="When transition occurred")
    reason: str = Field(..., description="Recorded rule trigger / justification")
    metrics_summary: dict[str, Any] = Field(
        default_factory=dict, description="Snapshot of metrics at transition time"
    )


class TransitionsListResponse(BaseModel):
    """Chronological transition history response."""

    account_id: str = Field(..., description="Account scope identifier")
    transitions: list[TransitionRecordResponse] = Field(default_factory=list)
    total: int = Field(..., description="Total recorded transitions count")


class MarketBarItem(BaseModel):
    """One completed 1m reference candle for charting."""

    timestamp: datetime = Field(..., description="Bar start timestamp")
    open: float = Field(..., description="Bar open price")
    high: float = Field(..., description="Bar high price")
    low: float = Field(..., description="Bar low price")
    close: float = Field(..., description="Bar close price")
    volume: float = Field(..., description="Bar volume")
    vwap: float | None = Field(None, description="Volume-weighted average price")


class SymbolBarsResponse(BaseModel):
    """Reference symbol candlestick series."""

    symbol: str = Field(..., description="Ticker symbol (e.g. SPY)")
    timeframe: str = Field(default="1m", description="Bar aggregation timeframe")
    bars: list[MarketBarItem] = Field(default_factory=list)


class MarketBarsResponse(BaseModel):
    """Persisted market bars response for SPY, QQQ, and IWM."""

    account_id: str = Field(..., description="Account scope identifier")
    as_of: datetime = Field(..., description="Query timestamp")
    symbols: list[SymbolBarsResponse] = Field(default_factory=list)
