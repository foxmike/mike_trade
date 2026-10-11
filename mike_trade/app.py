"""Dependency-injected production composition root for :mod:`mike_trade`.

Concrete provider imports are lazy.  Building SHADOW/BACKTEST therefore cannot
load an Alpaca adapter, read broker credentials, or touch the network.  Resource
opening remains part of :class:`mike_trade.runtime.Runtime` startup so the
instance lock is always acquired before SQLite migration or broker validation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from mike_trade.brokers.shadow.account import ShadowAccount
from mike_trade.brokers.shadow.fills import FillAssumptions, ShadowFillSimulator
from mike_trade.brokers.shadow.ledger import ShadowLedger
from mike_trade.brokers.shadow.stops import ShadowStopSimulator
from mike_trade.domain.accounts import (
    AccountEnvironment,
    AccountScope,
    AccountSnapshot,
    BrokerCapabilities,
    HealthStatus,
)
from mike_trade.domain.errors import (
    BrokerRejectedError,
    ConfigurationError,
    EntryBlockedError,
    InsufficientBuyingPowerError,
    LiveTradingNotAuthorizedError,
)
from mike_trade.domain.protection import ProtectionKind, ProtectionPolicy
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import (
    BrokerOrder,
    BrokerOrderStatus,
    EntryEligibilityPolicy,
    Fill,
    ManagementStatus,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    Position,
    PositionDirection,
    TimeInForce,
)
from mike_trade.execution.coordinator import (
    DecisionReason,
    OrderCommand,
    OrderCommandCoordinator,
)
from mike_trade.market_data.cache import MarketDataCache, MarketDataUpdate
from mike_trade.market_data.candles import (
    Candle,
    CandleBuilder,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    MinuteBar,
    PriceBasis,
)
from mike_trade.market_data.context import (
    ContextBuilder,
    IndicatorInput,
    IndicatorSpec,
    MarketContext,
)
from mike_trade.market_data.session_reference import SessionReferenceTracker
from mike_trade.market_status import (
    MarketStatusConfig,
    MarketStatusEntryGate,
    MarketStatusMonitor,
    TradingAuthorizationPolicy,
)
from mike_trade.observability.logging import configure_logging, get_logger
from mike_trade.persistence.maintenance import check_integrity, checkpoint
from mike_trade.persistence.repositories import (
    CashEquityRepository,
    RunConfigRepository,
    ServiceHealthRepository,
    StrategyStateRepository,
)
from mike_trade.persistence.schema import initialize_database, to_db_timestamp
from mike_trade.persistence.transaction import WriterSession
from mike_trade.portfolio.budgets import BudgetPolicy
from mike_trade.portfolio.reservations import ReservationLedger
from mike_trade.risk.protection import ProtectionCoordinator
from mike_trade.risk.stop_config import protection_policy_for_entry
from mike_trade.risk.stop_policy import (
    ProtectionMode as RiskProtectionMode,
)
from mike_trade.risk.stop_policy import (
    ProtectionPolicy as RiskProtectionPolicy,
)
from mike_trade.runtime import (
    BrokerPort,
    ClockPort,
    ControlsPort,
    EntryGate,
    ExecutionMode,
    ExecutionPort,
    MarketDataPort,
    PersistencePort,
    PortfolioPort,
    ReportingPort,
    ResearchPort,
    RiskPort,
    Runtime,
    RuntimeInstanceLock,
    SchedulerPort,
    SystemClock,
)
from mike_trade.scheduling import calendar as exchange_calendar
from mike_trade.scheduling.scheduler import StrategyScheduler
from mike_trade.scheduling.triggers import CANDLE_CLOSED
from mike_trade.signals.decisions import (
    EntryCandidate,
    ExitCandidate,
    arbitrate_entry_decisions,
    arbitrate_exit_decisions,
)
from mike_trade.strategies.base import StrategyEvaluation
from mike_trade.strategies.buy.aroon_trend import AroonTrendBuy
from mike_trade.strategies.buy.afternoon_breakout import AfternoonBreakoutBuy
from mike_trade.strategies.buy.cci_zero_line_crossover import CciZeroLineCrossoverBuy
from mike_trade.strategies.buy.bollinger_band_breakout import BollingerBandBreakoutBuy
from mike_trade.strategies.buy.cci_reversion import CciReversionBuy
from mike_trade.strategies.buy.stochastic_oversold_reversal import StochasticOversoldReversalBuy
from mike_trade.strategies.buy.money_flow_index import MoneyFlowIndexBuy
from mike_trade.strategies.buy.chaikin_money_flow import ChaikinMoneyFlowBuy
from mike_trade.strategies.buy.engulfing_candle_reversal import EngulfingCandleReversalBuy
from mike_trade.strategies.buy.awesome_oscillator import AwesomeOscillatorBuy
from mike_trade.strategies.buy.hull_moving_average import HullMovingAverageBuy
from mike_trade.strategies.buy.high_of_day_momentum import HighOfDayMomentumBuy
from mike_trade.strategies.buy.gap_and_go import GapAndGoBuy
from mike_trade.strategies.buy.gap_fill import GapFillBuy
from mike_trade.strategies.buy.gap_reversal import GapReversalBuy
from mike_trade.strategies.buy.keltner_channel_breakout import KeltnerChannelBreakoutBuy
from mike_trade.strategies.buy.linear_regression_trend import LinearRegressionTrendBuy
from mike_trade.strategies.buy.moving_average_envelope import MovingAverageEnvelopeBuy
from mike_trade.strategies.buy.moving_average_reclaim import MovingAverageReclaimBuy
from mike_trade.strategies.buy.nr7_breakout import Nr7BreakoutBuy
from mike_trade.strategies.buy.rate_of_change_momentum import RateOfChangeMomentumBuy
from mike_trade.strategies.buy.triple_moving_average import TripleMovingAverageBuy
from mike_trade.strategies.buy.dmi_crossover import DmiCrossoverBuy
from mike_trade.strategies.buy.donchian_channel_breakout import DonchianChannelBreakoutBuy
from mike_trade.strategies.buy.ema_crossover import EmaCrossoverBuy
from mike_trade.strategies.buy.inside_bar_breakout import InsideBarBreakoutBuy
from mike_trade.strategies.buy.macd_signal_crossover import MacdSignalCrossoverBuy
from mike_trade.strategies.buy.macd_zero_line_crossover import MacdZeroLineCrossoverBuy
from mike_trade.strategies.buy.momentum_breakout import MomentumBreakoutBuy
from mike_trade.strategies.buy.midday_range_breakout import MiddayRangeBreakoutBuy
from mike_trade.strategies.buy.opening_drive import OpeningDriveBuy
from mike_trade.strategies.buy.opening_range_breakout import OpeningRangeBreakoutBuy
from mike_trade.strategies.buy.opening_range_retest import OpeningRangeRetestBuy
from mike_trade.strategies.buy.parabolic_sar import ParabolicSarBuy
from mike_trade.strategies.buy.power_hour_momentum import PowerHourMomentumBuy
from mike_trade.strategies.short.short_ema_bearish_crossover import ShortEmaBearishCrossover
from mike_trade.strategies.short.short_supertrend_bearish_flip import ShortSupertrendBearishFlip
from mike_trade.strategies.short.short_donchian_breakdown import ShortDonchianBreakdown
from mike_trade.strategies.short.short_sma_bearish_crossover import ShortSmaBearishCrossover
from mike_trade.strategies.short.short_macd_bearish_crossover import ShortMacdBearishCrossover
from mike_trade.strategies.short.short_macd_zero_line_breakdown import (
    ShortMacdZeroLineBreakdown,
)
from mike_trade.strategies.short.short_rsi_centerline_breakdown import (
    ShortRsiCenterlineBreakdown,
)
from mike_trade.strategies.short.short_dmi_bearish_crossover import ShortDmiBearishCrossover
from mike_trade.strategies.short.short_aroon_bearish_reversal import ShortAroonBearishReversal
from mike_trade.strategies.short.short_parabolic_sar_bearish_reversal import (
    ShortParabolicSarBearishReversal,
)
from mike_trade.strategies.short.short_ichimoku_bearish_breakdown import (
    ShortIchimokuBearishBreakdown,
)
from mike_trade.strategies.short.short_opening_range_breakdown import (
    ShortOpeningRangeBreakdown,
)
from mike_trade.strategies.short.short_vwap_breakdown import ShortVwapBreakdown
from mike_trade.strategies.short.short_bollinger_lower_band_breakdown import (
    ShortBollingerLowerBandBreakdown,
)
from mike_trade.strategies.short.short_keltner_channel_breakdown import (
    ShortKeltnerChannelBreakdown,
)
from mike_trade.strategies.short.short_obv_breakdown import ShortObvBreakdown
from mike_trade.strategies.short.short_cci_zero_line_breakdown import (
    ShortCciZeroLineBreakdown,
)
from mike_trade.strategies.short.short_adx_bearish_trend_confirmation import (
    ShortAdxBearishTrendConfirmation,
)
from mike_trade.strategies.cover.cover_ema_bullish_crossover import CoverEmaBullishCrossover
from mike_trade.strategies.cover.cover_supertrend_bullish_flip import CoverSupertrendBullishFlip
from mike_trade.strategies.cover.cover_donchian_breakout import CoverDonchianBreakout
from mike_trade.strategies.cover.cover_sma_bullish_crossover import CoverSmaBullishCrossover
from mike_trade.strategies.cover.cover_macd_bullish_crossover import CoverMacdBullishCrossover
from mike_trade.strategies.cover.cover_macd_zero_line_crossover import CoverMacdZeroLineCrossover
from mike_trade.strategies.cover.cover_rsi_oversold_reversal import CoverRsiOversoldReversal
from mike_trade.strategies.cover.cover_rsi_centerline_crossover import CoverRsiCenterlineCrossover
from mike_trade.strategies.cover.cover_dmi_bullish_crossover import CoverDmiBullishCrossover
from mike_trade.strategies.cover.cover_aroon_bullish_reversal import CoverAroonBullishReversal
from mike_trade.strategies.cover.cover_parabolic_sar_bullish_reversal import CoverParabolicSarBullishReversal
from mike_trade.strategies.cover.cover_vwap_reclaim import CoverVwapReclaim
from mike_trade.strategies.cover.cover_bollinger_lower_band_reversion import CoverBollingerLowerBandReversion
from mike_trade.strategies.cover.cover_keltner_channel_reversion import CoverKeltnerChannelReversion
from mike_trade.strategies.cover.cover_cci_oversold_reversal import CoverCciOversoldReversal
from mike_trade.strategies.cover.cover_stochastic_bullish_crossover import CoverStochasticBullishCrossover
from mike_trade.strategies.cover.cover_williams_r_bullish_reversal import CoverWilliamsRBullishReversal
from mike_trade.strategies.cover.cover_chaikin_money_flow_recovery import CoverChaikinMoneyFlowRecovery
from mike_trade.strategies.buy.previous_close_reclaim import PreviousCloseReclaimBuy
from mike_trade.strategies.buy.previous_day_high_breakout import PreviousDayHighBreakoutBuy
from mike_trade.strategies.buy.previous_day_low_reversal import PreviousDayLowReversalBuy
from mike_trade.strategies.buy.rsi_centerline_crossover import RsiCenterlineCrossoverBuy
from mike_trade.strategies.buy.rsi_oversold_reversal import RsiOversoldReversalBuy
from mike_trade.strategies.buy.sma_crossover import SmaCrossoverBuy
from mike_trade.strategies.buy.stochastic_crossover import StochasticCrossoverBuy
from mike_trade.strategies.buy.vwap_bounce import VwapBounceBuy
from mike_trade.strategies.buy.williams_r_reversion import WilliamsRReversionBuy
from mike_trade.strategies.buy.adaptive_moving_average import AdaptiveMovingAverageBuy
from mike_trade.strategies.buy.adx_trend_following import AdxTrendFollowingBuy
from mike_trade.strategies.buy.atr_expansion_breakout import AtrExpansionBreakoutBuy
from mike_trade.strategies.buy.bollinger_squeeze import BollingerSqueezeBuy
from mike_trade.strategies.buy.connors_rsi_mean_reversion import ConnorsRsiMeanReversionBuy
from mike_trade.strategies.buy.ease_of_movement import EaseOfMovementBuy
from mike_trade.strategies.buy.elder_impulse_system import ElderImpulseSystemBuy
from mike_trade.strategies.buy.ema_pullback import EmaPullbackBuy
from mike_trade.strategies.buy.hammer_reversal import HammerReversalBuy
from mike_trade.strategies.buy.higher_high_higher_low import HigherHighHigherLowBuy
from mike_trade.strategies.buy.ichimoku_cloud_breakout import IchimokuCloudBreakoutBuy
from mike_trade.strategies.buy.keltner_channel_reversion import KeltnerChannelReversionBuy
from mike_trade.strategies.buy.moving_average_ribbon import MovingAverageRibbonBuy
from mike_trade.strategies.buy.resistance_breakout import ResistanceBreakoutBuy
from mike_trade.strategies.buy.supertrend import SupertrendBuy
from mike_trade.strategies.buy.volatility_squeeze_breakout import VolatilitySqueezeBreakoutBuy
from mike_trade.strategies.buy.obv_breakout import ObvBreakoutBuy
from mike_trade.strategies.buy.accumulation_distribution_breakout import AccumulationDistributionBreakoutBuy
from mike_trade.strategies.buy.volume_price_trend_breakout import VolumePriceTrendBreakoutBuy
from mike_trade.strategies.buy.tsi_momentum import TsiMomentumBuy
from mike_trade.strategies.buy.stochastic_rsi import StochasticRsiBuy
from mike_trade.strategies.buy.ultimate_oscillator import UltimateOscillatorBuy
from mike_trade.strategies.buy.holy_grail_pullback import HolyGrailPullbackBuy
from mike_trade.strategies.buy.rsi2_trend_pullback import Rsi2TrendPullbackBuy
from mike_trade.strategies.buy.z_score_mean_reversion import ZScoreMeanReversionBuy
from mike_trade.strategies.buy.double_bottom_reversal import DoubleBottomReversalBuy
from mike_trade.strategies.buy.inverse_head_and_shoulders import InverseHeadAndShouldersBuy
from mike_trade.strategies.buy.ascending_triangle_breakout import AscendingTriangleBreakoutBuy
from mike_trade.strategies.buy.trendline_breakout import TrendlineBreakoutBuy
from mike_trade.strategies.buy.triangle_breakout import TriangleBreakoutBuy
from mike_trade.strategies.buy.bull_flag_breakout import BullFlagBreakoutBuy
from mike_trade.strategies.indicators.core import VWAPCalculator
from mike_trade.strategies.presets.pairs import resolve_pair_preset
from mike_trade.strategies.registry import StrategyRegistry
from mike_trade.strategies.sell.aroon_bearish_reversal import AroonBearishReversalSell
from mike_trade.strategies.sell.base import ExitSource
from mike_trade.strategies.sell.cci_zero_line_breakdown import CciZeroLineBreakdownSell
from mike_trade.strategies.sell.cci_bearish_reversal import CciBearishReversalSell
from mike_trade.strategies.sell.money_flow_index_reversal import MoneyFlowIndexReversalSell
from mike_trade.strategies.sell.chaikin_money_flow_reversal import ChaikinMoneyFlowReversalSell
from mike_trade.strategies.sell.bearish_engulfing_exit import BearishEngulfingExitSell
from mike_trade.strategies.sell.awesome_oscillator_reversal import AwesomeOscillatorReversalSell
from mike_trade.strategies.sell.keltner_channel_breakdown import KeltnerChannelBreakdownSell
from mike_trade.strategies.sell.previous_swing_low_break import PreviousSwingLowBreakSell
from mike_trade.strategies.sell.dmi_bearish_crossover import DmiBearishCrossoverSell
from mike_trade.strategies.sell.donchian_lower_channel_exit import DonchianLowerChannelExitSell
from mike_trade.strategies.sell.ema_bearish_crossover import EmaBearishCrossoverSell
from mike_trade.strategies.sell.macd_bearish_crossover import MacdBearishCrossoverSell
from mike_trade.strategies.sell.macd_zero_line_breakdown import MacdZeroLineBreakdownSell
from mike_trade.strategies.sell.momentum_reversal import MomentumReversalSell
from mike_trade.strategies.sell.parabolic_sar_reversal import ParabolicSarReversalSell
from mike_trade.strategies.sell.price_below_ema import PriceBelowEmaSell
from mike_trade.strategies.sell.price_below_sma import PriceBelowSmaSell
from mike_trade.strategies.sell.rsi_centerline_breakdown import RsiCenterlineBreakdownSell
from mike_trade.strategies.sell.rsi_reversal import RsiReversalSell
from mike_trade.strategies.sell.sma_bearish_crossover import SmaBearishCrossoverSell
from mike_trade.strategies.sell.stochastic_bearish_crossover import StochasticBearishCrossoverSell
from mike_trade.strategies.sell.vwap_breakdown import VwapBreakdownSell
from mike_trade.strategies.sell.williams_r_reversal import WilliamsRReversalSell
from mike_trade.strategies.sell.adx_trend_weakening import AdxTrendWeakeningSell
from mike_trade.strategies.sell.bollinger_upper_band_reversal import BollingerUpperBandReversalSell
from mike_trade.strategies.sell.failed_breakout_exit import FailedBreakoutExitSell
from mike_trade.strategies.sell.lower_low_confirmation import LowerLowConfirmationSell
from mike_trade.strategies.sell.opening_range_breakdown import OpeningRangeBreakdownSell
from mike_trade.strategies.sell.stochastic_rsi_reversal import StochasticRsiReversalSell
from mike_trade.strategies.sell.trendline_breakdown import TrendlineBreakdownSell
from mike_trade.strategies.sell.ultimate_oscillator_reversal import UltimateOscillatorReversalSell
from mike_trade.strategies.sell.accumulation_distribution_breakdown import (
    AccumulationDistributionBreakdownSell,
)
from mike_trade.strategies.sell.bearish_momentum_divergence import BearishMomentumDivergenceSell
from mike_trade.strategies.sell.double_top_reversal import DoubleTopReversalSell
from mike_trade.strategies.sell.head_and_shoulders_breakdown import HeadAndShouldersBreakdownSell
from mike_trade.strategies.sell.ichimoku_bearish_exit import IchimokuBearishExitSell
from mike_trade.strategies.sell.obv_breakdown import ObvBreakdownSell
from mike_trade.strategies.sell.shooting_star_reversal import ShootingStarReversalSell
from mike_trade.strategies.sell.supertrend_bearish_flip import SupertrendBearishFlipSell

__all__ = [
    "AppSettings",
    "ShadowScenarioSettings",
    "SQLitePersistence",
    "ProductionMarketData",
    "AlpacaProductionMarketData",
    "StrategyAssignment",
    "SymbolStrategySettings",
    "VwapReferenceLifecycle",
    "build_runtime",
    "load_app_settings",
]


@dataclass(frozen=True, slots=True)
class ShadowScenarioSettings:
    name: str = "vwap_reference_shadow"
    starting_cash: Decimal = Decimal("25000.00")
    per_symbol_budget: Decimal = Decimal("3000.00")
    total_budget: Decimal = Decimal("10000.00")
    max_entry_cycles_per_session: int = 1
    consecutive_loss_limit: int | None = 2
    lockout_scope_by_strategy: bool = False
    post_sale_cooldown: timedelta = timedelta(minutes=5)
    minimum_hold: timedelta = timedelta(minutes=1)
    allow_fractional: bool = False
    fill_assumptions: FillAssumptions = field(default_factory=FillAssumptions)
    #: Opt-in (fail closed): SHORT strategies are only eligible when the scenario authorizes
    #: short selling.  The margin multiplier (>= 1) is the Regulation T style requirement:
    #: sale proceeds stay locked and ``multiplier - 1`` x notional of own cash is required.
    #: ``short_borrow_bps_per_day`` is an explicit *estimated* stock-borrow assumption.
    short_selling_enabled: bool = False
    short_margin_multiplier: Decimal = Decimal("1.5")
    short_borrow_bps_per_day: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if not self.name.strip() or ":" in self.name:
            raise ValueError("scenario name must be non-empty and contain no ':'")
        if self.short_margin_multiplier < Decimal("1"):
            raise ValueError("short_margin_multiplier must be >= 1")
        if self.short_borrow_bps_per_day < Decimal("0"):
            raise ValueError("short_borrow_bps_per_day must be >= 0")
        if self.name.upper() in {"PAPER", "LIVE"}:
            raise ValueError("scenario name collides with a broker environment")
        if self.total_budget < self.per_symbol_budget:
            raise ValueError("total_budget must be >= per_symbol_budget")


@dataclass(frozen=True, slots=True)
class StrategyAssignment:
    """One configured strategy instance: a registered plugin plus its parameters."""

    name: str
    plugin: str
    parameters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.plugin.strip():
            raise ValueError("strategy name and plugin must be non-empty")
        object.__setattr__(self, "parameters", dict(self.parameters))


@dataclass(frozen=True, slots=True)
class SymbolStrategySettings:
    """One symbol's independent BUY, SELL, SHORT and COVER strategy collections.

    Every collection is arbitrary-length. ``sells`` and ``covers`` may be empty: the symbol
    is then protected by its stop policy alone.  A symbol needs at least one entry strategy
    (BUY, or SHORT for a short-only symbol).
    """

    symbol: str
    buys: tuple[StrategyAssignment, ...]
    sells: tuple[StrategyAssignment, ...] = ()
    shorts: tuple[StrategyAssignment, ...] = ()
    covers: tuple[StrategyAssignment, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "buys", tuple(self.buys))
        object.__setattr__(self, "sells", tuple(self.sells))
        object.__setattr__(self, "shorts", tuple(self.shorts))
        object.__setattr__(self, "covers", tuple(self.covers))
        if not self.buys and not self.shorts:
            raise ValueError(f"{self.symbol}: at least one BUY strategy is required")


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Non-secret application composition inputs."""

    mode: ExecutionMode = ExecutionMode.SHADOW
    enable_live_trading: bool = False
    #: ``execution.short_selling_enabled``.  Never activates LIVE shorting: a LIVE broker
    #: adapter is always constructed with ``short_authorized=False``.
    short_selling_enabled: bool = False
    log_level: str = "INFO"
    db_path: str | Path | None = None
    data_dir: str | Path = Path("data")
    account_id: str = "default"
    symbols: tuple[str, ...] = ("SPY",)
    shadow_scenarios: tuple[ShadowScenarioSettings, ...] = (
        ShadowScenarioSettings(),
    )
    total_budget: Decimal = Decimal("10000.00")
    per_symbol_budget: Decimal = Decimal("3000.00")
    entry_cutoff_minutes: int = 30
    stop_trail_percent: Decimal = Decimal("1.5")
    max_bar_age: timedelta = timedelta(seconds=90)
    market_data_provider: str = "memory"
    market_data_feed: str = "iex"
    market_data_warmup_bars: int = 60
    heartbeat_interval_seconds: float = 5.0
    backtest_start: datetime = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    auto_eod_liquidation: bool = False
    #: Per-symbol strategy collections; symbols without an entry run the
    #: production ``vwap_reference`` pair.
    symbol_strategies: tuple[SymbolStrategySettings, ...] = ()
    #: Per-symbol normalized ``stop_policy`` (fixed / native_trailing /
    #: stepped_trailing); symbols without an entry use ``stop_trail_percent``.
    symbol_stop_policies: Mapping[str, Mapping[str, object]] = field(
        default_factory=dict, compare=False
    )
    credential_environment: Mapping[str, str] | None = field(
        default=None, repr=False, compare=False
    )
    market_status_enabled: bool = False
    market_status_config: MarketStatusConfig | None = None
    market_status_policy: TradingAuthorizationPolicy | None = None
    market_status_reference_symbols: tuple[str, ...] = ("SPY", "QQQ", "IWM")

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", ExecutionMode(self.mode))
        symbols = tuple(dict.fromkeys(symbol.strip().upper() for symbol in self.symbols))
        if not symbols or any(not symbol for symbol in symbols):
            raise ValueError("symbols must contain at least one non-empty symbol")
        object.__setattr__(self, "symbols", symbols)
        if self.market_status_reference_symbols:
            object.__setattr__(
                self,
                "market_status_reference_symbols",
                tuple(
                    dict.fromkeys(
                        s.strip().upper()
                        for s in self.market_status_reference_symbols
                        if s.strip()
                    )
                ),
            )
        if self.mode is ExecutionMode.SHADOW and not self.shadow_scenarios:
            raise ValueError("SHADOW requires at least one named scenario")
        names = [scenario.name for scenario in self.shadow_scenarios]
        if len(names) != len(set(names)):
            raise ValueError("shadow scenario names must be unique")
        if self.auto_eod_liquidation:
            raise ConfigurationError(
                "automatic EOD liquidation is intentionally opt-in outside vwap_reference",
                code="EOD_LIQUIDATION_NOT_CONFIGURED",
            )
        if self.market_data_provider not in {"memory", "alpaca"}:
            raise ValueError("market_data_provider must be memory or alpaca")
        if self.market_data_feed not in {"iex", "sip"}:
            raise ValueError("market_data_feed must be iex or sip")
        if self.market_data_warmup_bars < 1:
            raise ValueError("market_data_warmup_bars must be positive")
        if self.heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")

    @property
    def database_path(self) -> Path:
        if self.db_path is not None and str(self.db_path) != ":memory:":
            return Path(self.db_path).expanduser().resolve()
        return (Path(self.data_dir).expanduser() / f"{self.mode.value.lower()}.sqlite3").resolve()


class ExchangeCalendar:
    def __init__(self, clock: ClockPort) -> None:
        self.clock = clock

    def session_for(self, day: Any) -> Any:
        return exchange_calendar.session_for(day)

    def entry_cutoff(self, session: Any, minutes_before_close: int) -> datetime:
        return exchange_calendar.entry_cutoff(session, minutes_before_close)

    def validate_startup(self) -> bool:
        # The bundled calendar owns explicit early-close rules and rejects unknown years.
        local_day = self.clock.now().astimezone(exchange_calendar.NEW_YORK).date()
        if exchange_calendar.is_trading_day(local_day):
            exchange_calendar.session_for(local_day)
        return True


class SQLitePersistence:
    """Mode-bound WAL database lifecycle and dashboard heartbeat sink."""

    def __init__(
        self,
        path: Path,
        environment: AccountEnvironment,
        scopes: Sequence[AccountScope],
        clock: ClockPort,
    ) -> None:
        self.path = path
        self.environment = environment
        self.scopes = tuple(scopes)
        self.clock = clock
        self.session: WriterSession | None = None
        self._health: ServiceHealthRepository | None = None
        self.loaded_fingerprint: str | None = None
        self.durable_entry_reasons: tuple[str, ...] = ()

    def open_migrate_integrity(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = initialize_database(self.path, self.environment, now=self.clock.now())
        conn.close()
        report = check_integrity(self.path)
        if not report.ok:
            raise ConfigurationError(
                f"database integrity check failed: {report.integrity_check_messages!r}",
                code="DATABASE_INTEGRITY_FAILED",
            )
        self.session = WriterSession(self.path)
        if self.scopes:
            from mike_trade.persistence.repositories import AccountScopeRepository

            for scope in self.scopes:
                writer = self.session.bind_scope(scope.scope_id)
                AccountScopeRepository(writer, self.clock).register_scope(scope)
            self._health = ServiceHealthRepository(
                self.session.bind_scope(self.scopes[0].scope_id), self.clock
            )

    def load_durable_controls(self, config_fingerprint: str) -> None:
        if self.session is None:
            raise RuntimeError("database is not open")
        from mike_trade.controls.inbox import load_control_state

        self.loaded_fingerprint = config_fingerprint
        reasons: list[str] = []
        for scope in self.scopes:
            state = load_control_state(self.session, scope.scope_id)
            if state.paused:
                reasons.append(f"{scope.scope_id}:PAUSED")
            if state.entries_disabled:
                reasons.append(f"{scope.scope_id}:ENTRY_KILL_SWITCH")
            writer = self.session.bind_scope(scope.scope_id)
            prior = writer.connection.execute(
                "SELECT config_fingerprint_sha256 FROM run_configs "
                "WHERE account_scope_id = ? ORDER BY recorded_at DESC LIMIT 1",
                (scope.scope_id,),
            ).fetchone()
            if prior is None:
                RunConfigRepository(writer, self.clock).record_run_config(
                    {"config_fingerprint_sha256": config_fingerprint},
                    fingerprint_sha256=config_fingerprint,
                )
            elif prior[0] != config_fingerprint:
                reasons.append(f"{scope.scope_id}:CONFIG_FINGERPRINT_CHANGED")
        self.durable_entry_reasons = tuple(reasons)

    def entries_enabled(self) -> bool:
        return not self.durable_entry_reasons

    def heartbeat(self, details: Mapping[str, str]) -> None:
        if self._health is not None:
            self._health.heartbeat("engine", HealthStatus.HEALTHY, details)

    def heartbeat_component(
        self, component: str, details: Mapping[str, object]
    ) -> None:
        if self._health is None:
            return
        connected = str(details.get("connected", "")).lower() == "true"
        status = HealthStatus.HEALTHY if connected else HealthStatus.DEGRADED
        self._health.heartbeat(
            component,
            status,
            {str(key): str(value) for key, value in details.items()},
        )

    def append_event(self, event: Mapping[str, object]) -> None:
        # Domain mutation is owned by typed repositories/ledgers, never an untyped append.
        raise TypeError("use a typed persistence repository")

    def flush(self) -> None:
        if self.session is not None:
            self.session.connection.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def checkpoint(self) -> None:
        if self.session is not None:
            checkpoint(self.path, "PASSIVE")

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None
            self._health = None


class _PreviousVWAP:
    """Indicator adapter returning the preceding completed-bar session VWAP."""

    def __init__(self) -> None:
        self._vwap = VWAPCalculator()
        self._previous: Decimal | None = None

    def update(self, bar: object) -> Decimal | None:
        current = self._vwap.update(cast(Any, bar))
        result, self._previous = self._previous, current
        return result


class ProductionMarketData:
    """Shared cache/candle/context engine used by stream, replay, and offline tests."""

    def __init__(self, clock: ClockPort, max_age: timedelta) -> None:
        self.clock = clock
        self.cache = MarketDataCache()
        self.candles = CandleBuilder(
            timeframes=(CandleTimeframe.M1,), max_history=500,
            session_open=time(9, 30),
        )
        self.session_references = SessionReferenceTracker()
        specs = {
            "vwap": IndicatorSpec(
                CandleTimeframe.M1, VWAPCalculator, IndicatorInput.SESSION_BAR
            ),
            "vwap_prev": IndicatorSpec(
                CandleTimeframe.M1, _PreviousVWAP, IndicatorInput.SESSION_BAR
            ),
        }
        self.contexts = ContextBuilder(
            candle_builder=self.candles,
            quote_trade_cache=self.cache,
            clock=clock,
            indicator_specs=specs,
            max_age=max_age,
            session_references=self.session_references,
        )
        self.running = False
        self.closed = False
        self.acquisitions: dict[str, int] = {}
        self._market_status_monitor: Any = None
        self._reference_symbols: tuple[str, ...] = ("SPY", "QQQ", "IWM")
        self._trading_symbols: tuple[str, ...] = ()

    def bind_market_status(
        self,
        monitor: Any,
        reference_symbols: Sequence[str] = ("SPY", "QQQ", "IWM"),
        trading_symbols: Sequence[str] = (),
    ) -> None:
        self._market_status_monitor = monitor
        self._reference_symbols = tuple(
            dict.fromkeys(s.strip().upper() for s in reference_symbols if s.strip())
        )
        self._trading_symbols = tuple(
            dict.fromkeys(s.strip().upper() for s in trading_symbols if s.strip())
        )

    def on_candle(self, candle: Candle) -> None:
        self.session_references.add_bar(candle)
        if (
            self._market_status_monitor is not None
            and candle.symbol.upper() in self._reference_symbols
        ):
            self._market_status_monitor.add_bar(candle)

    def ingest(self, update: MarketDataUpdate) -> None:
        self.cache.update(update)

    def subscribe(self, symbols: Sequence[str]) -> None:
        for symbol in symbols:
            self.acquisitions.setdefault(symbol.upper(), 0)

    def warm(self, symbols: Sequence[str]) -> bool:
        self.subscribe(symbols)
        return True

    def start(self) -> None:
        self.running = True

    def is_fresh(self) -> bool:
        return self.running and not self.closed

    def snapshot(self, symbols: Sequence[str]) -> dict[str, MarketContext]:
        result: dict[str, MarketContext] = {}
        for symbol in tuple(dict.fromkeys(item.upper() for item in symbols)):
            self.acquisitions[symbol] = self.acquisitions.get(symbol, 0) + 1
            result[symbol] = self.contexts.build(
                symbol,
                timeframes=(CandleTimeframe.M1,),
                indicators=("vwap", "vwap_prev"),
            )
        return result

    def close(self) -> None:
        self.running = False
        self.closed = True


class _AlpacaDataHttpClient:
    """Authenticated client for the existing historical-bars adapter."""

    def __init__(self, key_id: str, secret_key: str) -> None:
        import httpx

        self._client = httpx.Client(
            base_url="https://data.alpaca.markets",
            headers={
                "APCA-API-KEY-ID": key_id,
                "APCA-API-SECRET-KEY": secret_key,
            },
            timeout=20.0,
        )

    def get(self, path: str, params: Mapping[str, object] | None = None) -> object:
        response = self._client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()


class AlpacaProductionMarketData(ProductionMarketData):
    """Alpaca acquisition feeding the existing cache, candles and lifecycle."""

    def __init__(
        self,
        clock: ClockPort,
        max_age: timedelta,
        *,
        feed: str,
        warmup_bars: int,
        environ: Mapping[str, str] | None,
        startup_timeout: float = 20.0,
    ) -> None:
        super().__init__(clock, max_age)
        self.feed = feed
        self.warmup_bars = warmup_bars
        self.environ = os.environ if environ is None else environ
        self.startup_timeout = startup_timeout
        self._symbols: tuple[str, ...] = ()
        self._stream: object | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._subscribed = asyncio.Event()
        self._scheduler: object | None = None
        self._lifecycle: object | None = None
        self._evaluations = 0
        self._events = 0
        self._logger = get_logger("mike_trade.market_data.alpaca")

    def bind_engine(
        self,
        scheduler: object,
        lifecycle: object | None,
        trading_symbols: Sequence[str] = (),
    ) -> None:
        self._scheduler = scheduler
        self._lifecycle = lifecycle
        if trading_symbols:
            self._trading_symbols = tuple(
                dict.fromkeys(s.strip().upper() for s in trading_symbols if s.strip())
            )

    def _credentials(self) -> tuple[str, str]:
        key_var = "MIKE_TRADE_ALPACA_DATA_KEY_ID"
        secret_var = "MIKE_TRADE_ALPACA_DATA_SECRET_KEY"
        key_id = (self.environ.get(key_var) or "").strip()
        secret_key = (self.environ.get(secret_var) or "").strip()
        missing = [
            name
            for name, value in ((key_var, key_id), (secret_var, secret_key))
            if not value
        ]
        if missing:
            raise ConfigurationError(
                f"missing Alpaca market-data credential variable(s): {', '.join(missing)}",
                code="MARKET_DATA_CREDENTIALS_MISSING",
            )
        return key_id, secret_key

    async def warm(self, symbols: Sequence[str]) -> bool:
        from mike_trade.market_data.alpaca_history import AlpacaHistoricalBars

        trading = tuple(dict.fromkeys(symbol.upper() for symbol in symbols))
        self._trading_symbols = trading
        all_symbols = tuple(dict.fromkeys((*trading, *self._reference_symbols)))
        self._symbols = all_symbols
        self.subscribe(self._symbols)
        key_id, secret_key = self._credentials()
        client = _AlpacaDataHttpClient(key_id, secret_key)
        now = self.clock.now()

        def fetch() -> dict[str, tuple[object, ...]]:
            history = AlpacaHistoricalBars(client, max_pages=20)
            start = now - timedelta(days=14)
            return {
                symbol: history.fetch_bars(
                    symbol, "1Min", start, now, feed=self.feed, adjustment="raw"
                )
                for symbol in self._symbols
            }

        try:
            result = await asyncio.to_thread(fetch)
        finally:
            client.close()
        for symbol, bars in result.items():
            completed_bars = tuple(
                bar
                for bar in bars
                if cast(Any, bar).timestamp + timedelta(minutes=1) <= now
            )
            for bar in completed_bars:
                item = cast(Any, bar)
                c = Candle(
                    symbol=symbol,
                    timeframe=CandleTimeframe.M1,
                    start=item.timestamp,
                    end=item.timestamp + timedelta(minutes=1),
                    open=item.open,
                    high=item.high,
                    low=item.low,
                    close=item.close,
                    volume=Decimal(item.volume),
                    feed_id=item.feed_id,
                    consolidated=item.feed_id == "sip",
                    origin=CandleOrigin.HISTORICAL_REST,
                    price_basis=PriceBasis.RAW,
                    status=CandleStatus.CLOSED,
                    trade_count=(
                        int(item.trade_count) if item.trade_count is not None else None
                    ),
                )
                self.session_references.add_bar(c)
                if (
                    self._market_status_monitor is not None
                    and symbol in self._reference_symbols
                ):
                    self._market_status_monitor.add_bar(c)
            for bar in completed_bars[-(self.warmup_bars + 1):]:
                item = cast(Any, bar)
                self.candles.on_minute_bar(
                    MinuteBar(
                        symbol=symbol,
                        start=item.timestamp,
                        open=item.open,
                        high=item.high,
                        low=item.low,
                        close=item.close,
                        volume=Decimal(item.volume),
                        feed_id=item.feed_id,
                        consolidated=item.feed_id == "sip",
                        origin=CandleOrigin.HISTORICAL_REST,
                        trade_count=(
                            int(item.trade_count) if item.trade_count is not None else None
                        ),
                    )
                )
        self._logger.info(
            "Alpaca historical warmup complete",
            extra={
                "feed": self.feed,
                "symbols": list(self._symbols),
                "bars": {symbol: len(bars) for symbol, bars in result.items()},
            },
        )
        return True

    def _on_state(self, change: object) -> None:
        from mike_trade.market_data.alpaca_stream import ConnectionState

        state = getattr(change, "state", None)
        details = {"state": getattr(state, "value", str(state)), "feed": self.feed}
        reason = getattr(change, "reason", None)
        if reason:
            details["reason"] = reason
        self._logger.info("Alpaca market-data connection state", extra=details)
        if state is ConnectionState.SUBSCRIBED:
            self._subscribed.set()

    async def start(self) -> None:
        if self.running:
            return
        from mike_trade.market_data.alpaca_stream import (
            AlpacaMarketDataStream,
            Channel,
            StreamCredentials,
            WebsocketsTransport,
        )

        key_id, secret_key = self._credentials()
        self._stream = AlpacaMarketDataStream(
            StreamCredentials(key_id, secret_key),
            WebsocketsTransport(),
            self.clock,
            feed=self.feed,
            sip_entitled=self.feed == "sip",
            on_state=self._on_state,
        )
        pairs = tuple(
            (symbol, channel)
            for symbol in self._symbols
            for channel in (Channel.BARS, Channel.QUOTES, Channel.TRADES)
        )
        await cast(Any, self._stream).subscribe(pairs)
        self.running = True
        self.closed = False
        self._stream_task = asyncio.create_task(
            self._consume(), name="alpaca-market-data-stream"
        )
        waiter = asyncio.create_task(self._subscribed.wait())
        done, _pending = await asyncio.wait(
            {self._stream_task, waiter},
            timeout=self.startup_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if waiter in done and waiter.result():
            return
        waiter.cancel()
        if self._stream_task in done:
            await self._stream_task
            raise RuntimeError("Alpaca market-data stream ended before subscribing")
        raise TimeoutError("Alpaca market-data subscription acknowledgement timed out")

    async def _consume(self) -> None:
        from mike_trade.market_data.alpaca_stream import StreamBar, SubscriptionUpdate

        assert self._stream is not None
        async for event in cast(Any, self._stream).events():
            if isinstance(event, SubscriptionUpdate):
                self._logger.info(
                    "Alpaca subscriptions acknowledged",
                    extra={
                        "feed": self.feed,
                        "subscription_count": len(event.subscriptions),
                        "symbols": list(self._symbols),
                    },
                )
                continue
            self._events += 1
            self.cache.store_event(event, source="stream")
            if isinstance(event, StreamBar):
                await self._on_bar(event)

    async def _on_bar(self, event: object) -> None:
        item = cast(Any, event)
        ingested = self.candles.on_minute_bar(
            MinuteBar(
                symbol=item.symbol,
                start=item.provider_source_ts,
                open=item.open,
                high=item.high,
                low=item.low,
                close=item.close,
                volume=item.volume,
                feed_id=item.feed_id,
                consolidated=item.feed_id == "sip",
                trade_count=item.trade_count,
            )
        )
        closed = [bar for bar in ingested.closed if bar.timeframe is CandleTimeframe.M1]
        for bar in closed:
            self.session_references.add_bar(bar)
            if (
                self._market_status_monitor is not None
                and bar.symbol.upper() in self._reference_symbols
            ):
                self._market_status_monitor.add_bar(bar)
        if not closed or self._scheduler is None or self._lifecycle is None:
            return
        if self._trading_symbols and item.symbol.upper() not in self._trading_symbols:
            return
        latest = closed[-1]
        due = cast(Any, self._scheduler).on_candle_closed(
            item.symbol, CandleTimeframe.M1, latest.end
        )
        if not due:
            return
        context = self.contexts.build(
            item.symbol,
            timeframes=(CandleTimeframe.M1,),
            indicators=("vwap", "vwap_prev"),
        )
        actions = await cast(Any, self._lifecycle).process({item.symbol: context})
        self._evaluations += 1
        self._logger.info(
            "Strategy evaluation completed",
            extra={
                "symbol": item.symbol,
                "bar_end": latest.end,
                "evaluation_count": self._evaluations,
                "actions": actions,
            },
        )

    def is_fresh(self) -> bool:
        return self.running and self._subscribed.is_set() and not self.closed

    def health_details(self) -> Mapping[str, object]:
        state = getattr(getattr(self._stream, "state", None), "value", "DISCONNECTED")
        return {
            "connected": str(self.is_fresh()).lower(),
            "state": state,
            "feed": self.feed,
            "subscriptions": len(self.active_subscriptions),
            "events": self._events,
            "evaluations": self._evaluations,
        }

    @property
    def active_subscriptions(self) -> frozenset[object]:
        if self._stream is None:
            return frozenset()
        return cast(Any, self._stream).active_subscriptions

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.running = False
        if self._stream is not None:
            await cast(Any, self._stream).close()
        task, self._stream_task = self._stream_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def load_app_settings(
    config_path: str | Path | None = None,
    *,
    mode_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    cli_overrides: Sequence[str] = (),
) -> AppSettings:
    """Map the validated operator configuration into the composition root."""
    from mike_trade.controls.config_commands import load_effective_config

    source = os.environ if environ is None else environ
    effective = load_effective_config(
        config_path,
        mode_path=mode_path,
        environ=source,
        cli_overrides=list(cli_overrides),
    )
    execution = cast(Mapping[str, Any], effective["execution"])
    service = cast(Mapping[str, Any], effective["service"])
    market_data = cast(Mapping[str, Any], effective["market_data"])
    risk = cast(Mapping[str, Any], effective["risk"])
    symbol_map = cast(Mapping[str, Mapping[str, Any]], effective["symbols"])
    symbols = tuple(name for name, item in symbol_map.items() if item["enabled"])
    if not symbols:
        raise ConfigurationError("configuration has no enabled symbols", code="NO_ENABLED_SYMBOLS")
    selected = [symbol_map[symbol] for symbol in symbols]

    def one_value(name: str) -> Any:
        values = {item[name] for item in selected}
        if len(values) != 1:
            raise ConfigurationError(
                f"runtime requires one shared {name} value across enabled symbols",
                code="DIVERGENT_SYMBOL_RUNTIME_SETTING",
                context={"setting": name},
            )
        return next(iter(values))

    configured_symbols = set(symbols)
    scenarios: list[ShadowScenarioSettings] = []
    for raw in cast(Sequence[Mapping[str, Any]], effective["shadow_scenarios"]):
        if not raw["enabled"]:
            continue
        if set(cast(Sequence[str], raw["symbols"])) != configured_symbols:
            raise ConfigurationError(
                "runtime requires each enabled shadow scenario to use the enabled universe",
                code="SHADOW_SCENARIO_UNIVERSE_MISMATCH",
            )
        scenarios.append(
            ShadowScenarioSettings(
                name=str(raw["name"]),
                starting_cash=cast(Decimal, raw["starting_cash"]),
                per_symbol_budget=cast(Decimal, one_value("budget")),
                total_budget=cast(Decimal, risk["total_application_budget"]),
                max_entry_cycles_per_session=int(one_value("max_entry_cycles_per_session")),
                consecutive_loss_limit=cast(int | None, one_value("consecutive_loss_limit")),
                lockout_scope_by_strategy=one_value("consecutive_loss_scope") == "strategy",
                post_sale_cooldown=timedelta(
                    seconds=int(one_value("post_sale_cooldown_seconds"))
                ),
                minimum_hold=timedelta(seconds=int(one_value("min_hold_seconds"))),
                allow_fractional=bool(execution["fractional_enabled"]),
                short_selling_enabled=bool(execution["short_selling_enabled"]),
            )
        )

    cutoff_hour, cutoff_minute = map(int, str(execution["entry_cutoff"]).split(":"))
    cutoff_from_close = (16 * 60) - (cutoff_hour * 60 + cutoff_minute)
    if cutoff_from_close < 0:
        raise ConfigurationError("entry cutoff must not be after 16:00 ET")
    mode = ExecutionMode(str(execution["account_mode"]).upper())
    stop_policies = {name: dict(symbol_map[name]["stop_policy"]) for name in symbols}
    trail_values = {
        policy.get("trail_percent")
        for policy in stop_policies.values()
        if policy["type"] == "native_trailing"
    }
    if mode is not ExecutionMode.SHADOW and (
        {policy["type"] for policy in stop_policies.values()} != {"native_trailing"}
        or len(trail_values) != 1
        or None in trail_values
    ):
        raise ConfigurationError(
            "PAPER/LIVE runtime requires one shared native_trailing trail_percent; fixed, "
            "amount-based and stepped stop policies are only wired for SHADOW",
            code="UNSUPPORTED_STOP_POLICY",
        )
    strategy_map = cast(Mapping[str, Mapping[str, Any]], effective["strategies"])

    def assignments(names: Sequence[str]) -> tuple[StrategyAssignment, ...]:
        return tuple(
            StrategyAssignment(
                name, str(strategy_map[name]["plugin"]),
                cast(Mapping[str, object], strategy_map[name]["parameters"]),
            )
            for name in names
        )

    symbol_strategies = tuple(
        SymbolStrategySettings(
            name,
            assignments(symbol_map[name]["buy_strategies"]),
            assignments(symbol_map[name]["sell_strategies"]),
            assignments(symbol_map[name].get("short_strategies", ())),
            assignments(symbol_map[name].get("cover_strategies", ())),
        )
        for name in symbols
    )
    data_dir = Path(str(service["data_dir"]))
    db_path = source.get("MIKE_TRADE_DB_PATH") or data_dir / f"{mode.value.lower()}.sqlite3"
    return AppSettings(
        mode=mode,
        enable_live_trading=bool(execution["live_trading_enabled"]),
        short_selling_enabled=bool(execution["short_selling_enabled"]),
        market_status_enabled=bool(execution["market_status_enabled"]),
        db_path=db_path,
        data_dir=data_dir,
        symbols=symbols,
        shadow_scenarios=tuple(scenarios),
        total_budget=cast(Decimal, risk["total_application_budget"]),
        per_symbol_budget=cast(Decimal, one_value("budget")),
        entry_cutoff_minutes=cutoff_from_close,
        stop_trail_percent=(
            cast(Decimal, next(iter(trail_values)))
            if len(trail_values) == 1 and None not in trail_values
            else Decimal("1.5")
        ),
        symbol_strategies=symbol_strategies,
        symbol_stop_policies=stop_policies,
        max_bar_age=timedelta(seconds=int(market_data["max_bar_age_seconds"])),
        market_data_provider=str(market_data["provider"]),
        market_data_feed=str(market_data["feed"]),
        market_data_warmup_bars=int(market_data["warmup_bars"]),
        heartbeat_interval_seconds=float(service["engine_heartbeat_interval_seconds"]),
        auto_eod_liquidation=bool(execution["end_of_day_close"]["enabled"]),
        credential_environment=source,
    )


@dataclass(slots=True)
class _ShadowScenario:
    settings: ShadowScenarioSettings
    ledger: ShadowLedger
    account: ShadowAccount
    fills: ShadowFillSimulator
    stops: ShadowStopSimulator
    states: dict[tuple[str, ...], dict[str, object]]
    coordinator: OrderCommandCoordinator


class _CoordinatorProviders:
    """Account facts for the order-command coordinator, direction-aware.

    A long exit counts only a LONG position and SELL orders; a short exit (cover) counts only
    a SHORT position and BUY_TO_COVER orders.  Cash is *unrestricted* cash: the proceeds of
    open shorts are restricted collateral and never available to reserve for another entry.
    """

    def __init__(self, account: ShadowAccount) -> None:
        self.account = account

    def quote_age(self, _intent: object, _now: datetime) -> timedelta:
        return timedelta(0)

    def has_open_or_unresolved_order(self, key: object, purpose: str) -> bool:
        symbol = cast(Any, key).symbol
        for order in self.account.state.orders.values():
            if not order.is_open or order.symbol != symbol:
                continue
            if purpose in ("ENTRY_LONG", "ENTRY_SHORT") and order.side.is_entry:
                return True  # one working entry per symbol, whichever direction
            if purpose == "EXIT_LONG" and order.side is OrderSide.SELL and (
                order.purpose is not OrderPurpose.PROTECTIVE_STOP
            ):
                return True
            if purpose == "EXIT_SHORT" and order.side is OrderSide.BUY_TO_COVER and (
                order.purpose is not OrderPurpose.PROTECTIVE_STOP
            ):
                return True
        return False

    def _confirmed(self, key: object, direction: PositionDirection) -> Decimal:
        position = self.account.state.positions.get(cast(Any, key).symbol)
        if position is None or position.direction is not direction:
            return Decimal("0")
        return position.quantity

    def confirmed_long_quantity(self, key: object) -> Decimal:
        return self._confirmed(key, PositionDirection.LONG)

    def confirmed_short_quantity(self, key: object) -> Decimal:
        return self._confirmed(key, PositionDirection.SHORT)

    def _committed(self, key: object, side: OrderSide, *, protective_only: bool) -> Decimal:
        return sum(
            (order.remaining for order in self.account.state.open_orders()
             if order.symbol == cast(Any, key).symbol and order.side is side
             and (not protective_only or order.purpose is OrderPurpose.PROTECTIVE_STOP)),
            Decimal("0"),
        )

    def committed_sell_quantity(self, key: object) -> Decimal:
        return self._committed(key, OrderSide.SELL, protective_only=False)

    def protective_sell_quantity(self, key: object) -> Decimal:
        return self._committed(key, OrderSide.SELL, protective_only=True)

    def committed_cover_quantity(self, key: object) -> Decimal:
        return self._committed(key, OrderSide.BUY_TO_COVER, protective_only=False)

    def protective_cover_quantity(self, key: object) -> Decimal:
        return self._committed(key, OrderSide.BUY_TO_COVER, protective_only=True)

    async def available_cash(self, _key: object) -> Decimal:
        state = self.account.state
        return state.cash - state.restricted_collateral()


@dataclass(frozen=True, slots=True)
class _CommandIntent:
    execution_mode: str
    account_key: str
    symbol: str
    purpose: str
    signal_expires_at: datetime
    quote_reference_time: datetime
    qty: Decimal | None
    notional: Decimal | None
    limit_price: Decimal | None


class ShadowBrokerGroup:
    """One isolated durable simulated account per named SHADOW scenario."""

    environment = AccountEnvironment.SHADOW

    def __init__(
        self,
        persistence: SQLitePersistence,
        clock: ClockPort,
        account_id: str,
        scenarios: Sequence[ShadowScenarioSettings],
    ) -> None:
        self.persistence = persistence
        self.clock = clock
        self.account_id = account_id
        self.configs = tuple(scenarios)
        self.scenarios: dict[str, _ShadowScenario] = {}
        self._last_balance_snapshot_at: datetime | None = None

    async def refresh_balance_snapshots(self) -> None:
        """Publish the isolated simulated balances for dashboard readers."""
        if not self.scenarios or self.persistence.session is None:
            return
        now = self.clock.now()
        if (self._last_balance_snapshot_at is not None
                and now - self._last_balance_snapshot_at < timedelta(minutes=1)):
            return
        for scenario in self.scenarios.values():
            snapshot = await scenario.account.get_account()
            writer = self.persistence.session.bind_scope(snapshot.scope.scope_id)
            CashEquityRepository(writer, self.clock).append_equity_snapshot(snapshot)
        self._last_balance_snapshot_at = now

    def validate_startup(self) -> bool:
        if self.persistence.session is None:
            raise RuntimeError("persistence must be opened before shadow accounts")
        if self.scenarios:
            return True
        for config in self.configs:
            scope = AccountScope(AccountEnvironment.SHADOW, self.account_id, config.name)
            ledger = ShadowLedger(
                self.persistence.session.bind_scope(scope.scope_id), scope, clock=self.clock
            )
            fills = ShadowFillSimulator(config.fill_assumptions)
            account = (
                ShadowAccount.open(ledger)
                if ledger.read_events()
                else ShadowAccount.create(
                    ledger,
                    starting_cash=config.starting_cash,
                    per_symbol_budget=config.per_symbol_budget,
                    total_budget=config.total_budget,
                    eligibility=EntryEligibilityPolicy(
                        config.max_entry_cycles_per_session,
                        config.consecutive_loss_limit,
                        config.lockout_scope_by_strategy,
                        config.post_sale_cooldown,
                        config.minimum_hold,
                    ),
                    allow_fractional=config.allow_fractional,
                    short_margin_multiplier=config.short_margin_multiplier,
                    short_borrow_bps_per_day=config.short_borrow_bps_per_day,
                    assumptions=config.fill_assumptions.as_mapping(),
                    feed="iex:shadow",
                )
            )
            providers = _CoordinatorProviders(account)
            coordinator = OrderCommandCoordinator(
                clock=self.clock,
                quote_age_provider=providers,
                open_order_provider=providers,
                position_provider=providers,
                buying_power_budget_provider=providers,
                max_quote_age=timedelta(seconds=90),
            )
            self.scenarios[config.name] = _ShadowScenario(
                config,
                ledger,
                account,
                fills,
                ShadowStopSimulator(account),
                {},
                coordinator,
            )
        return True

    @property
    def primary(self) -> ShadowAccount:
        self.validate_startup()
        return self.scenarios[self.configs[0].name].account

    def capabilities(self) -> object:
        return self.primary.capabilities()

    async def get_account(self) -> object:
        return await self.primary.get_account()

    async def list_positions(self) -> Sequence[object]:
        return await self.primary.list_positions()

    async def list_open_orders(self) -> Sequence[object]:
        return await self.primary.list_open_orders()

    async def list_linked_orders(self) -> Sequence[object]:
        return await self.primary.list_open_orders()

    async def get_order(self, order_id: str) -> object:
        return await self.primary.get_order(order_id)

    async def get_order_by_client_id(self, client_id: str) -> object:
        return await self.primary.get_order_by_client_id(client_id)

    async def list_fills(self, since: datetime) -> Sequence[object]:
        return await self.primary.list_fills(since)

    async def submit_order(self, intent: object) -> object:
        return await self.primary.submit_order(cast(OrderIntent, intent))

    async def replace_order(self, order_id: str, **values: object) -> object:
        return await self.primary.replace_order(order_id, **values)

    async def cancel_order(self, order_id: str) -> None:
        await self.primary.cancel_order(order_id)

    def order_updates_fresh(self) -> bool:
        return True


class _CleanShadowReconciler:
    def __init__(self, group: ShadowBrokerGroup) -> None:
        self.group = group

    def reconcile(self, _facts: Mapping[str, object] | None = None) -> object:
        self.group.validate_startup()
        # Re-opening each ledger through ShadowAccount already verifies its hash chain
        # and validates every event with the same reducer used by live simulation.
        for scenario in self.group.scenarios.values():
            if not scenario.account.state.is_created:
                raise RuntimeError("shadow account replay did not produce an account")
        return SimpleNamespace(clean=True, entries_permitted=True, reasons=(), status="CLEAN")


class _BrokerReconciler:
    """Open-time binding of the production reconciler to the mode database writer."""

    def __init__(
        self,
        persistence: SQLitePersistence,
        broker: object,
        clock: ClockPort,
        scope: AccountScope,
    ) -> None:
        self.persistence = persistence
        self.broker = broker
        self.clock = clock
        self.scope = scope

    async def reconcile(self, _facts: Mapping[str, object] | None = None) -> object:
        from mike_trade.execution.reconcile import AccountReconciler

        if self.persistence.session is None:
            raise RuntimeError("persistence must be open before reconciliation")
        writer = self.persistence.session.bind_scope(self.scope.scope_id)
        return await AccountReconciler(
            broker=cast(Any, self.broker), writer=writer, clock=self.clock
        ).reconcile()


class _CleanBacktestReconciler:
    def reconcile(self, _facts: Mapping[str, object] | None = None) -> object:
        return SimpleNamespace(clean=True, entries_permitted=True, reasons=(), status="CLEAN")


@dataclass(frozen=True, slots=True)
class _Binding:
    """One configured strategy instance bound to a symbol and a role."""

    strategy_id: str
    version: str
    role: str  # "buy" | "sell" | "short" | "cover"
    strategy: Any
    parameters: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _Collections:
    """A symbol's BUY/SELL/SHORT/COVER strategies in deterministic (strategy_id) order."""

    buys: tuple[_Binding, ...]
    sells: tuple[_Binding, ...]
    shorts: tuple[_Binding, ...] = ()
    covers: tuple[_Binding, ...] = ()


#: The reference pair keeps its original persisted strategy-instance ids so a
#: restart after upgrading resumes the same durable evaluation state.
_LEGACY_INSTANCES = {("buy", "vwap_bounce"), ("sell", "vwap_breakdown")}


class VwapReferenceLifecycle:
    """Production SHADOW evaluator for per-symbol BUY/SELL strategy collections.

    Without ``assignments`` every symbol runs the mandatory ``vwap_reference``
    pair (``vwap_bounce`` BUY, ``vwap_breakdown`` SELL). With ``assignments``
    each symbol has its own arbitrary-length BUY and SELL collections:

    * every strategy gets independent parameters and persisted evaluation state
      keyed by ``(role, strategy_id, symbol)`` and sees the same immutable
      ``MarketContext`` (no strategy fetches or calculates data itself);
    * flat: all BUY strategies are evaluated and
      ``arbitrate_entry_decisions`` selects at most one entry, so simultaneous
      signals can neither duplicate a purchase nor scale a position;
    * held: all SELL strategies are evaluated and ``arbitrate_exit_decisions``
      selects at most one full-remainder exit (minimum hold, pending exit);
    * the protective stop is evaluated first, independent of any SELL strategy,
      and is cancelled only to make way for a discretionary exit.
    """

    def __init__(
        self,
        group: ShadowBrokerGroup,
        clock: ClockPort,
        entry_gate: EntryGate,
        cutoff_minutes: int,
        protection_policy: ProtectionPolicy,
        buy_parameters: Mapping[str, object],
        sell_parameters: Mapping[str, object],
        *,
        assignments: Sequence[SymbolStrategySettings] = (),
        registry: StrategyRegistry | None = None,
        stop_specs: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        self.group = group
        self.clock = clock
        self.entry_gate = entry_gate
        self.cutoff_minutes = cutoff_minutes
        self.protection_policy = protection_policy
        self.buy = VwapBounceBuy()
        self.sell = VwapBreakdownSell()
        self.buy_parameters = dict(buy_parameters)
        self.sell_parameters = dict(sell_parameters)
        self._stop_specs = {k.upper(): dict(v) for k, v in (stop_specs or {}).items()}
        self._default = _Collections(
            (_Binding(VwapBounceBuy.spec.strategy_id, VwapBounceBuy.spec.version, "buy",
                      self.buy, self.buy_parameters),),
            (_Binding(VwapBreakdownSell.spec.strategy_id, VwapBreakdownSell.spec.version,
                      "sell", self.sell, self.sell_parameters),),
        )
        self._by_symbol: dict[str, _Collections] = {}
        if assignments:
            if registry is None:
                raise ConfigurationError(
                    "strategy assignments require a strategy registry",
                    code="STRATEGY_REGISTRY_MISSING",
                )
            for item in assignments:
                self._by_symbol[item.symbol] = _Collections(
                    self._bind(registry, item.symbol, StrategyRole.BUY, item.buys),
                    self._bind(registry, item.symbol, StrategyRole.SELL, item.sells),
                    self._bind(registry, item.symbol, StrategyRole.SHORT, item.shorts),
                    self._bind(registry, item.symbol, StrategyRole.COVER, item.covers),
                )
        self._in_flight = 0

    @staticmethod
    def _bind(
        registry: StrategyRegistry,
        symbol: str,
        role: StrategyRole,
        assigned: Sequence[StrategyAssignment],
    ) -> tuple[_Binding, ...]:
        """Resolve and validate one side of a symbol's collection; fail closed."""
        bindings: list[_Binding] = []
        for item in assigned:
            try:
                cls = registry.get(item.plugin)
                parameters = registry.validate_parameters(item.plugin, item.parameters)
            except Exception as exc:
                raise ConfigurationError(
                    f"{symbol}: strategy {item.name!r} (plugin {item.plugin!r}) is not "
                    f"usable: {exc}",
                    code="STRATEGY_NOT_USABLE",
                ) from exc
            spec = cls.spec
            if spec.side is not role:
                raise ConfigurationError(
                    f"{symbol}: strategy {item.name!r} is a {spec.side.value} strategy "
                    f"configured as {role.value}",
                    code="STRATEGY_SIDE_MISMATCH",
                )
            if spec.trigger_timeframe is not CandleTimeframe.M1 or any(
                tf is not CandleTimeframe.M1 for tf in spec.required_timeframes
            ):
                raise ConfigurationError(
                    f"{symbol}: strategy {item.name!r} needs non-1m candles; the SHADOW "
                    "stream currently builds the 1m shared context only",
                    code="STRATEGY_TIMEFRAME_UNSUPPORTED",
                )
            bindings.append(
                _Binding(spec.strategy_id, spec.version, role.value.lower(), cls(), parameters)
            )
        if len({b.strategy_id for b in bindings}) != len(bindings):
            raise ConfigurationError(
                f"{symbol}: a strategy may appear only once per symbol and side",
                code="STRATEGY_DUPLICATE",
            )
        return tuple(sorted(bindings, key=lambda b: b.strategy_id))

    def collections(self, symbol: str) -> _Collections:
        """The BUY/SELL collections evaluated for ``symbol``."""
        return self._by_symbol.get(symbol.upper(), self._default)

    def verify_startup(self) -> object:
        reasons: list[str] = []
        self.group.validate_startup()
        for name, scenario in self.group.scenarios.items():
            for symbol, position in scenario.account.state.positions.items():
                stop = scenario.stops.state(position.cycle_id)
                if (
                    stop is None
                    or stop.status != "ACTIVE"
                    or stop.covered_quantity != position.quantity
                ):
                    reasons.append(f"{name}:{symbol}:PROTECTIVE_COVERAGE_DRIFT")
        return SimpleNamespace(
            clean=not reasons,
            entries_permitted=not reasons,
            reasons=tuple(reasons),
            status="CLEAN" if not reasons else "DRIFTED",
        )

    async def process(self, contexts: Mapping[str, MarketContext]) -> dict[str, list[str]]:
        """Evaluate one immutable shared snapshot once per scenario."""
        self.group.validate_startup()
        result: dict[str, list[str]] = {}
        self._in_flight += 1
        try:
            for name, scenario in self.group.scenarios.items():
                actions: list[str] = []
                for symbol, context in contexts.items():
                    if context.symbol != symbol.upper():
                        raise ValueError("context mapping key must match context.symbol")
                    actions.extend(await self._process_symbol(scenario, context))
                result[name] = actions
            return result
        finally:
            self._in_flight -= 1

    def _policy_for(self, symbol: str, entry_price: Decimal) -> ProtectionPolicy:
        spec = self._stop_specs.get(symbol.upper())
        if spec is None:
            return self.protection_policy
        return protection_policy_for_entry(spec, entry_price)

    async def _arm(
        self, scenario: _ShadowScenario, cycle_id: str, symbol: str, at: datetime
    ) -> None:
        position = scenario.account.state.positions.get(symbol)
        if position is None:
            return
        await scenario.stops.arm(
            cycle_id, symbol, self._policy_for(symbol, position.avg_cost), at=at
        )

    def _entry_permitted(self, direction: PositionDirection) -> bool:
        """Runtime gate (and, when enabled, market-state policy) for one entry direction."""
        authorizes = getattr(self.entry_gate, "authorizes", None)
        if callable(authorizes):
            return bool(authorizes(direction))
        return bool(self.entry_gate.authorized)

    @staticmethod
    def _open_orders(account: ShadowAccount, symbol: str, *sides: OrderSide) -> bool:
        return any(
            order.symbol == symbol and order.side in sides and order.is_open
            and order.purpose is not OrderPurpose.PROTECTIVE_STOP
            for order in account.state.orders.values()
        )

    async def _process_symbol(
        self, scenario: _ShadowScenario, context: MarketContext
    ) -> list[str]:
        actions: list[str] = []
        account = scenario.account
        quote = context.latest_quote
        if quote is not None:
            account.update_quote(context.symbol, (quote.bid + quote.ask) / Decimal("2"))

        # Existing entry/exit orders may consume another deterministic partial fill,
        # but strategy replay can never submit a second order for the same signal.
        for order in tuple(await account.list_open_orders()):
            stored = account.state.orders[order.broker_order_id]
            if stored.purpose is OrderPurpose.PROTECTIVE_STOP or quote is None:
                continue
            fill = scenario.fills.apply_fill_to_account(
                account, order.broker_order_id, context, as_of=context.as_of
            )
            if fill.is_fill:
                actions.append(f"FILL:{order.side.value}:{fill.quantity}")
                if order.side.is_entry:  # BUY or SELL_SHORT: protect the new position
                    await self._arm(scenario, stored.cycle_id, context.symbol, context.as_of)

        position = account.state.positions.get(context.symbol)
        if position is not None and quote is not None:
            stop = scenario.stops.state(position.cycle_id)
            if stop is not None and stop.status not in {"CANCELED", "TRIGGERED"}:
                updated = await scenario.stops.evaluate(
                    position.cycle_id, quote, at=context.as_of,
                    trade=context.latest_trade,
                )
                if updated.status == "TRIGGERED":
                    actions.append("STOP_EXIT")
                    return actions

        collections = self.collections(context.symbol)
        position = account.state.positions.get(context.symbol)
        if position is not None:
            # A position is held.  Only the exit role of its own direction may act: SELL
            # closes a long, COVER closes a short.  Every other role keeps its indicator
            # state continuous (advance-only): it can never signal an order, and any signal
            # it would raise is discarded.  Clearing the eligibility flag makes the strategy
            # gate block every entry signal.
            long_held = position.direction is PositionDirection.LONG
            exit_bindings = collections.sells if long_held else collections.covers
            other_exit_bindings = collections.covers if long_held else collections.sells
            exit_source = ExitSource.STRATEGY_SELL if long_held else ExitSource.STRATEGY_COVER
            exit_side = OrderSide.SELL if long_held else OrderSide.BUY_TO_COVER
            for binding in (*collections.buys, *collections.shorts):
                state = self._state(scenario, binding, context.symbol)
                state.pop("eligibility", None)
                evaluation = binding.strategy.evaluate(context, binding.parameters, state)
                self._save_state(scenario, binding, context.symbol, evaluation.state_updates)
            for binding in other_exit_bindings:
                state = self._state(scenario, binding, context.symbol)
                evaluation = binding.strategy.evaluate(context, binding.parameters, state)
                self._save_state(scenario, binding, context.symbol, evaluation.state_updates)
            # Every configured exit strategy of this direction evaluates the held position
            # (its state advances even when a hold or pending exit blocks the exit).
            exits: dict[str, _Binding] = {}
            for binding in exit_bindings:
                state = self._state(scenario, binding, context.symbol)
                evaluation = binding.strategy.evaluate(context, binding.parameters, state)
                self._save_state(scenario, binding, context.symbol, evaluation.state_updates)
                if evaluation.is_signal:
                    exits[evaluation.signal_id or ""] = binding
            if exits:
                domain_position = Position(
                    account.scope.scope_id, position.symbol, position.quantity,
                    position.avg_cost, position.cost_basis, context.as_of,
                    management=ManagementStatus.MANAGED, cycle_id=position.cycle_id,
                    direction=position.direction,
                )
                decision = arbitrate_exit_decisions(
                    [
                        ExitCandidate(
                            signal_id=signal_id, source=exit_source,
                            symbol=context.symbol, strategy_id=binding.strategy_id,
                            version=binding.version, position_cycle_id=position.cycle_id,
                            bar_time=self._bar_time(context),
                        )
                        for signal_id, binding in exits.items()
                    ],
                    symbol=context.symbol, position=domain_position,
                    evaluation_time=context.as_of,
                    minimum_hold=scenario.settings.minimum_hold,
                    entry_filled_at=position.opened_at,
                    pending_exit_source=(
                        exit_source
                        if self._open_orders(account, context.symbol, exit_side)
                        else None
                    ),
                )
                if decision.selected is not None:
                    actions.extend(
                        await self._exit(scenario, context, decision.selected.signal_id)
                    )
            return actions

        # Flat.  SELL and COVER have nothing to close: keep their indicators advancing and
        # discard any signal.  BUY and (when authorized) SHORT compete for the one entry.
        for binding in (*collections.sells, *collections.covers):
            state = self._state(scenario, binding, context.symbol)
            evaluation = binding.strategy.evaluate(context, binding.parameters, state)
            self._save_state(scenario, binding, context.symbol, evaluation.state_updates)
        entries: dict[str, tuple[_Binding, StrategyEvaluation]] = {}
        for binding in collections.buys:
            state = self._state(scenario, binding, context.symbol)
            state["eligibility"] = "WATCHING_BUY"
            evaluation = binding.strategy.evaluate(context, binding.parameters, state)
            self._save_state(scenario, binding, context.symbol, evaluation.state_updates)
            if evaluation.is_signal:
                entries[evaluation.signal_id or ""] = (binding, evaluation)
        for binding in collections.shorts:
            state = self._state(scenario, binding, context.symbol)
            authorized = scenario.settings.short_selling_enabled
            if authorized:
                state["eligibility"] = "WATCHING_SHORT"
            else:
                state.pop("eligibility", None)  # not authorized: ineligible, advance-only
            evaluation = binding.strategy.evaluate(context, binding.parameters, state)
            self._save_state(scenario, binding, context.symbol, evaluation.state_updates)
            if authorized and evaluation.is_signal:
                entries[evaluation.signal_id or ""] = (binding, evaluation)
        if not entries:
            return actions
        # Runtime / market-state authorization is per entry direction.  Candidates of a
        # direction that is not currently authorized are dropped *before* arbitration, so a
        # restricted BUY can never suppress an authorized SHORT (or the reverse).
        entries = {
            signal_id: (binding, evaluation)
            for signal_id, (binding, evaluation) in entries.items()
            if self._entry_permitted(
                PositionDirection.SHORT if binding.role == "short" else PositionDirection.LONG
            )
        }
        if not entries:
            return actions
        arbitration = arbitrate_entry_decisions(
            [
                EntryCandidate(
                    signal_id, binding.strategy_id, binding.version, context.symbol,
                    evaluation.bar_time,
                    PositionDirection.SHORT if binding.role == "short" else PositionDirection.LONG,
                )
                for signal_id, (binding, evaluation) in entries.items()
            ],
            symbol=context.symbol, evaluation_time=context.as_of, position_open=False,
            entry_pending=self._open_orders(
                account, context.symbol, OrderSide.BUY, OrderSide.SELL_SHORT
            ),
        )
        if arbitration.selected is None:
            return actions
        binding, evaluation = entries[arbitration.selected.signal_id]
        short = binding.role == "short"
        local_day = context.as_of.astimezone(exchange_calendar.NEW_YORK).date()
        session = exchange_calendar.session_for(local_day)
        if context.as_of >= exchange_calendar.entry_cutoff(session, self.cutoff_minutes):
            return actions
        reference = evaluation.reference_price
        if reference is None:
            return actions
        # Size on the ask for both directions: the account validates the budget at the quote
        # mid, so the higher side guarantees the order fits (a short sells at the lower bid).
        sizing_price = (
            context.latest_quote.ask if context.latest_quote is not None else reference
        )
        quantity = scenario.settings.per_symbol_budget / sizing_price
        if not scenario.settings.allow_fractional:
            quantity = quantity.to_integral_value(rounding=ROUND_DOWN)
        else:
            quantity = quantity.quantize(Decimal("0.000000001"), rounding=ROUND_DOWN)
        if quantity <= 0:
            return actions
        cycle_id = f"{context.symbol}-{evaluation.signal_id}"
        client_id = f"shadow-entry-{evaluation.signal_id}"
        command_intent = _CommandIntent(
            "SHADOW", scenario.account.scope.scope_id, context.symbol,
            "ENTRY_SHORT" if short else "ENTRY_LONG",
            context.as_of + timedelta(minutes=1), context.as_of,
            quantity, None, reference,
        )
        if short:
            # Short-sale proceeds are restricted collateral: only the own-cash margin part
            # (multiplier - 1) x notional is reserved from available cash.
            multiplier = account.state.short_margin_multiplier
            reservation_amount = max(
                quantity * reference * (multiplier - 1), Decimal("0.01")
            )
        else:
            reservation_amount = quantity * reference
        command = OrderCommand(command_intent, reservation_amount=reservation_amount)
        decision = await scenario.coordinator.authorize(command)
        if not decision.authorized:
            return actions
        intent = OrderIntent(
            intent_id=f"intent-{evaluation.signal_id}", client_order_id=client_id,
            account_scope_id=account.scope.scope_id, cycle_id=cycle_id,
            symbol=context.symbol,
            side=OrderSide.SELL_SHORT if short else OrderSide.BUY,
            purpose=OrderPurpose.ENTRY,
            order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY,
            created_at=context.as_of, quantity=quantity, signal_id=evaluation.signal_id,
        )
        try:
            order = await account.submit_order(intent, strategy_key=binding.strategy_id)
        except (BrokerRejectedError, EntryBlockedError, InsufficientBuyingPowerError) as exc:
            if not short:
                raise
            # The account's own margin / exposure / eligibility rules refused the short:
            # release the authorization hold and record the refusal; nothing was submitted.
            if decision.reservation is not None:
                await scenario.coordinator.release_reservation(decision.reservation)
            else:
                await scenario.coordinator.complete(command)
            actions.append(f"ENTRY_REFUSED:SHORT:{getattr(exc, 'code', 'refused')}")
            return actions
        actions.append(f"ENTRY:{order.broker_order_id}")
        if context.latest_quote is not None:
            fill = scenario.fills.apply_fill_to_account(
                account, order.broker_order_id, context, as_of=context.as_of
            )
            if fill.is_fill:
                actions.append(f"FILL:{intent.side.value}:{fill.quantity}")
                await self._arm(scenario, cycle_id, context.symbol, context.as_of)
        if decision.reservation is not None:
            # The shadow account now durably owns both the filled commitment and any
            # open-order remainder; the coordinator's short-lived authorization hold
            # must not double-count that capital.
            await scenario.coordinator.release_reservation(decision.reservation)
        else:
            await scenario.coordinator.complete(command)
        return actions

    @staticmethod
    def _bar_time(context: MarketContext) -> datetime | None:
        bars = context.candles.get(CandleTimeframe.M1, ())
        return bars[-1].start if bars else None

    async def _exit(
        self, scenario: _ShadowScenario, context: MarketContext, signal_id: str
    ) -> list[str]:
        position = scenario.account.state.positions[context.symbol]
        long_held = position.direction is PositionDirection.LONG
        command_intent = _CommandIntent(
            "SHADOW", scenario.account.scope.scope_id, context.symbol,
            "EXIT_LONG" if long_held else "EXIT_SHORT",
            context.as_of + timedelta(minutes=1), context.as_of,
            position.quantity, None, None,
        )
        command = OrderCommand(command_intent, cancel_protective_first=True)
        decision = await scenario.coordinator.authorize(command)
        if decision.reason not in {
            DecisionReason.AUTHORIZED, DecisionReason.REQUIRES_PROTECTIVE_CANCEL
        }:
            return []
        await scenario.stops.prepare_discretionary_exit(
            position.cycle_id, at=context.as_of
        )
        intent = OrderIntent(
            intent_id=f"exit-intent-{signal_id}",
            client_order_id=f"shadow-exit-{signal_id}",
            account_scope_id=scenario.account.scope.scope_id,
            cycle_id=position.cycle_id, symbol=context.symbol,
            side=OrderSide.SELL if long_held else OrderSide.BUY_TO_COVER,
            purpose=OrderPurpose.STRATEGY_EXIT,
            order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY,
            created_at=context.as_of, quantity=position.quantity, signal_id=signal_id,
        )
        order = await scenario.stops.submit_discretionary_exit(intent)
        actions = [f"EXIT:{order.broker_order_id}"]
        if context.latest_quote is not None:
            fill = scenario.fills.apply_fill_to_account(
                scenario.account, order.broker_order_id, context, as_of=context.as_of
            )
            if fill.is_fill:
                actions.append(f"FILL:{intent.side.value}:{fill.quantity}")
        await scenario.coordinator.complete(command)
        return actions

    def _state(
        self, scenario: _ShadowScenario, binding: _Binding, symbol: str
    ) -> dict[str, object]:
        key = (binding.role, binding.strategy_id, symbol)
        if key not in scenario.states:
            loaded: dict[str, object] = {}
            session = self.group.persistence.session
            if session is not None:
                instance_id = self._ensure_strategy_instance(scenario, binding, symbol)
                repo = StrategyStateRepository(
                    session.bind_scope(scenario.account.scope.scope_id), self.clock
                )
                record = repo.load_state(instance_id, symbol)
                if record is not None:
                    loaded = dict(cast(Mapping[str, object], record["state"]))
            scenario.states[key] = loaded
        return scenario.states[key]

    def _save_state(
        self,
        scenario: _ShadowScenario,
        binding: _Binding,
        symbol: str,
        updates: Mapping[str, object],
    ) -> None:
        state = self._state(scenario, binding, symbol)
        state.update(updates)
        session = self.group.persistence.session
        if session is not None:
            instance_id = self._ensure_strategy_instance(scenario, binding, symbol)
            StrategyStateRepository(
                session.bind_scope(scenario.account.scope.scope_id), self.clock
            ).save_state(instance_id, symbol, state)

    def _ensure_strategy_instance(
        self, scenario: _ShadowScenario, binding: _Binding, symbol: str
    ) -> str:
        session = self.group.persistence.session
        if session is None:
            raise RuntimeError("persistence is not open")
        scope = scenario.account.scope.scope_id
        legacy = (binding.role, binding.strategy_id) in _LEGACY_INSTANCES
        instance_id = (
            f"{scope}:vwap_reference:{binding.role}:{symbol}"
            if legacy
            else f"{scope}:{binding.strategy_id}:{binding.role}:{symbol}"
        )
        with session.unit_of_work() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO strategy_instances("
                "instance_id,account_scope_id,strategy_key,strategy_version,role,"
                "symbol,parameters_json,enabled,pair_key,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    instance_id,
                    scope,
                    binding.strategy_id,
                    binding.version,
                    binding.role.upper(),
                    symbol,
                    json.dumps(dict(binding.parameters), sort_keys=True, default=str),
                    1,
                    "vwap_reference" if legacy else None,
                    to_db_timestamp(self.clock.now()),
                ),
            )
        return instance_id

    async def drain(self) -> None:
        while self._in_flight:
            import asyncio

            await asyncio.sleep(0)


class _NoopReporting:
    def publish(self, _report: Mapping[str, object]) -> None:
        return None


class _RiskVerifier:
    def verify_startup(self) -> bool:
        return True

    def check_entry(self, _intent: object) -> None:
        return None


class _PortfolioView:
    def __init__(self, broker: object) -> None:
        self.broker = broker

    def positions(self) -> Sequence[object]:
        return ()


class _BacktestBrokerAdapter:
    """Credentials-free adapter over the real deterministic replay simulator."""

    environment = AccountEnvironment.BACKTEST

    def __init__(self, clock: ClockPort) -> None:
        from mike_trade.backtest.clock import ReplayClock
        from mike_trade.backtest.execution import HistoricalExecutionSimulator
        from mike_trade.backtest.feed import DataProvenance, HistoricalReplayFeed

        if not isinstance(clock, ReplayClock):
            raise ConfigurationError(
                "default BACKTEST composition requires ReplayClock",
                code="BACKTEST_CLOCK_NOT_REPLAY",
            )
        self.feed = HistoricalReplayFeed(
            (),
            DataProvenance(
                source="composition-root",
                feed="IEX",
                is_consolidated=False,
                revision="empty-default",
            ),
            clock=clock,
        )
        self.simulator = HistoricalExecutionSimulator(self.feed, clock)

    def validate_startup(self) -> bool:
        return True

    def capabilities(self) -> object:
        return self.simulator.capabilities()

    async def get_account(self) -> object:
        # HistoricalExecutionSimulator intentionally does not model cash/equity.
        # The runtime still fetches an explicit account fact during reconciliation.
        return SimpleNamespace(environment=self.environment, cash=None, equity=None)

    async def list_positions(self) -> Sequence[object]:
        return await self.simulator.list_positions()

    async def list_open_orders(self) -> Sequence[object]:
        return await self.simulator.list_open_orders()

    async def list_linked_orders(self) -> Sequence[object]:
        return await self.simulator.list_open_orders()

    async def list_fills(self, since: datetime) -> Sequence[object]:
        return await self.simulator.list_fills(since)

    async def submit_order(self, intent: object) -> object:
        return await self.simulator.submit_order(cast(OrderIntent, intent))

    async def cancel_order(self, order_id: str) -> None:
        await self.simulator.cancel_order(order_id)

    def order_updates_fresh(self) -> bool:
        return True


class AlpacaBrokerAdapter:
    """Lazy PAPER/LIVE individual-Trading-API adapter (never used by SHADOW)."""

    def __init__(
        self,
        mode: ExecutionMode,
        account_id: str,
        clock: ClockPort,
        *,
        authorized: bool,
        short_authorized: bool = False,
        capabilities_matrix: object | None = None,
        environ: Mapping[str, str] | None,
    ) -> None:
        self.mode = mode
        self.environment = AccountEnvironment(mode.value)
        self.scope = AccountScope(self.environment, account_id)
        self.clock = clock
        self.authorized = authorized
        self.short_authorized = short_authorized
        #: Alpaca capability matrix; the default leaves every short-side order shape UNPROVEN,
        #: so a short never reaches the broker until an operator records a verification.
        self.capabilities_matrix = capabilities_matrix
        self.environ = environ
        self._client: object | None = None
        self._account: object | None = None
        self._orders: object | None = None
        self._activities: object | None = None
        self._assets: object | None = None
        self._provider_clock: object | None = None

    def validate_startup(self) -> bool:
        from mike_trade.brokers.alpaca.account import AlpacaAccountService
        from mike_trade.brokers.alpaca.activities import AlpacaActivityService
        from mike_trade.brokers.alpaca.assets import AlpacaAssetService
        from mike_trade.brokers.alpaca.auth import (
            AlpacaCredentials,
            AlpacaEnvironment,
            credential_fingerprint,
        )
        from mike_trade.brokers.alpaca.http import AlpacaHttpClient
        from mike_trade.brokers.alpaca.orders import AlpacaOrderService

        environment = (
            AlpacaEnvironment.PAPER if self.mode is ExecutionMode.PAPER
            else AlpacaEnvironment.LIVE
        )
        credentials = AlpacaCredentials.from_env(environment, self.environ)
        client = AlpacaHttpClient(
            credentials, live_authorized=self.authorized,
            persisted_fingerprint=credential_fingerprint(credentials),
        )
        fingerprint = credential_fingerprint(credentials)
        self._client = client
        self._account = AlpacaAccountService(
            client, environment=environment, fingerprint=fingerprint
        )
        self._orders = AlpacaOrderService(client)
        self._activities = AlpacaActivityService(client)
        self._assets = AlpacaAssetService(
            client, environment=environment, fingerprint=fingerprint
        )
        self._provider_clock = cast(Any, self._account).get_clock()
        return True

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            provider="alpaca",
            environment=self.environment,
            supported_order_types=frozenset(
                {"market", "limit", "stop", "trailing_stop"}
            ),
            supports_fractional=True,
            supports_notional=True,
            supports_native_trailing_stop=True,
            supports_extended_hours=False,
            supports_short=True,
            paper_verified_combinations=frozenset(
                {("market", True), ("limit", True)}
            ),
        )

    def validate_assets(self, symbols: Sequence[str]) -> object:
        self._ready()
        reasons: list[str] = []
        for symbol in symbols:
            asset = cast(Any, self._assets).get_asset(symbol)
            verdict = cast(Any, self._assets).check_eligibility(asset)
            reasons.extend(f"{symbol}:{reason}" for reason in verdict.reasons)
        return SimpleNamespace(
            clean=not reasons,
            reasons=tuple(reasons),
            status="CLEAN" if not reasons else "DRIFTED",
        )

    def _ready(self) -> None:
        if self._client is None:
            self.validate_startup()

    def _account_snapshot(self, value: object) -> AccountSnapshot:
        item = cast(Any, value)
        return AccountSnapshot(
            scope=self.scope,
            as_of=self.clock.now(),
            cash=item.cash,
            equity=item.equity,
            buying_power=item.buying_power,
            daytrading_buying_power=item.daytrading_buying_power,
            portfolio_value=item.portfolio_value,
            pattern_day_trader=item.pattern_day_trader,
            trading_blocked=item.trading_blocked,
            account_blocked=item.account_blocked,
            trade_suspended_by_user=item.trade_suspended_by_user,
            shorting_enabled=item.shorting_enabled,
            source=f"alpaca:{self.mode.value.lower()}",
            currency=item.currency,
        )

    def _position(self, value: object) -> Position:
        item = cast(Any, value)
        if item.side not in {"long", "short"} or item.qty == 0:
            raise RuntimeError(f"unsupported external position: {item.symbol}")
        return Position(
            account_scope_id=self.scope.scope_id,
            symbol=item.symbol,
            quantity=abs(item.qty),
            avg_entry_price=item.avg_entry_price,
            # Alpaca reports a short with negative qty/cost/value; the domain carries a
            # positive magnitude plus an explicit direction.
            cost_basis=abs(item.cost_basis),
            as_of=self.clock.now(),
            market_value=None if item.market_value is None else abs(item.market_value),
            management=ManagementStatus.UNMANAGED,
            direction=(
                PositionDirection.LONG
                if item.side == "long"
                else PositionDirection.SHORT
            ),
        )

    def _order(self, value: object | None) -> BrokerOrder | None:
        if value is None:
            return None
        item = cast(Any, value)
        quantity = item.qty
        if quantity is None:
            raise RuntimeError(
                f"Alpaca order {item.order_id} has notional sizing without a quantity"
            )
        try:
            broker_side = str(item.side).lower()
            side = OrderSide.BUY if broker_side == "buy" else OrderSide.SELL
            try:
                from mike_trade.brokers.alpaca.orders import (
                    OrderPurpose as AlpacaOrderPurpose,
                    parse_client_order_id,
                )

                lineage = parse_client_order_id(item.client_order_id)["purpose"]
                if lineage is AlpacaOrderPurpose.ENTRY_SHORT:
                    side = OrderSide.SELL_SHORT
                elif lineage is AlpacaOrderPurpose.EXIT_SHORT:
                    side = OrderSide.BUY_TO_COVER
            except (ValueError, KeyError):
                pass
            order_type = OrderType(str(item.type).upper())
            status = BrokerOrderStatus(str(item.status).upper())
        except ValueError:
            status = BrokerOrderStatus.UNKNOWN
            side = OrderSide(str(item.side).upper())
            order_type = OrderType(str(item.type).upper())
        submitted = item.submitted_at or item.created_at or self.clock.now()
        updated = item.updated_at or submitted
        return BrokerOrder(
            broker_order_id=item.order_id,
            client_order_id=item.client_order_id,
            account_scope_id=self.scope.scope_id,
            symbol=item.symbol,
            side=side,
            order_type=order_type,
            status=status,
            quantity=quantity,
            filled_quantity=item.filled_qty or Decimal("0"),
            submitted_at=submitted,
            updated_at=updated,
            filled_avg_price=item.filled_avg_price,
            limit_price=item.limit_price,
            stop_price=item.stop_price,
            trail_price=item.trail_price,
            trail_percent=item.trail_percent,
        )

    async def get_account(self) -> object:
        self._ready()
        return self._account_snapshot(cast(Any, self._account).get_account())

    async def list_positions(self) -> Sequence[object]:
        self._ready()
        return tuple(
            self._position(item) for item in cast(Any, self._account).list_positions()
        )

    async def list_open_orders(self) -> Sequence[object]:
        self._ready()
        return tuple(
            cast(BrokerOrder, self._order(item))
            for item in cast(Any, self._orders).list_open_orders()
        )

    async def list_linked_orders(self) -> Sequence[object]:
        return await self.list_open_orders()

    async def get_order(self, order_id: str) -> object:
        self._ready()
        return self._order(cast(Any, self._orders).get_order(order_id))

    async def get_order_by_client_id(self, client_id: str) -> object:
        self._ready()
        return self._order(
            cast(Any, self._orders).get_order_by_client_order_id(client_id)
        )

    async def list_fills(self, since: datetime) -> Sequence[object]:
        self._ready()
        activities = cast(Any, self._activities).list_activities(
            activity_types="FILL", after=since
        )
        return tuple(
            Fill(
                fill_id=item.id,
                broker_order_id=item.order_id,
                account_scope_id=self.scope.scope_id,
                symbol=item.symbol,
                side=self._fill_side(item),
                quantity=item.qty,
                price=item.price,
                fee=Decimal("0"),
                filled_at=item.transaction_time,
            )
            for item in activities
        )

    def _fill_side(self, item: object) -> OrderSide:
        """Position-effect-aware side of a broker fill activity.

        Alpaca reports ``buy``/``sell`` (some activities say ``sell_short``/``buy_to_cover``).
        A plain buy/sell is resolved through the order's own client-order-id lineage, which
        records whether the order was a short entry or a cover; an order we cannot attribute
        stays BUY/SELL and the reconciler's direction checks flag any mismatch.
        """
        activity = cast(Any, item)
        raw = str(activity.side).strip().lower()
        explicit = {"sell_short": OrderSide.SELL_SHORT, "buy_to_cover": OrderSide.BUY_TO_COVER}
        if raw in explicit:
            return explicit[raw]
        side = OrderSide(raw.upper())
        try:
            order = cast(Any, self._orders).get_order(activity.order_id)
            converted = self._order(order) if order is not None else None
        except Exception:  # unattributable fill: keep the broker word; reconciliation decides
            converted = None
        if converted is not None and converted.side.broker_side == side.broker_side:
            return converted.side
        return side

    async def submit_order(self, intent: object) -> object:
        self._ready()
        if not isinstance(intent, OrderIntent):
            raise TypeError("Alpaca submission requires OrderIntent")
        if intent.account_scope_id != self.scope.scope_id:
            raise ConfigurationError(
                "order intent account scope differs from Alpaca adapter",
                code="CROSS_MODE_ORDER_INTENT",
            )
        if intent.order_type not in {OrderType.MARKET, OrderType.LIMIT}:
            raise ConfigurationError(
                f"generic Alpaca submission does not accept {intent.order_type.value}",
                code="PROTECTIVE_ORDER_REQUIRES_COORDINATOR",
            )
        asset = cast(Any, self._assets).get_asset(intent.symbol)
        eligibility = cast(Any, self._assets).check_eligibility(asset)
        if not eligibility.eligible:
            raise ConfigurationError(
                f"asset {intent.symbol} is ineligible: {eligibility.reasons!r}",
                code="ASSET_INELIGIBLE",
            )
        if intent.side is OrderSide.SELL_SHORT:
            account = cast(Any, self._account).get_account()
            if not (
                self.short_authorized
                and account.shorting_enabled
                and account.buying_power > 0
                and asset.marginable
                and asset.shortable
                and asset.standard_short_borrow_available
            ):
                raise ConfigurationError(
                    "short entry requires explicit authorization, margin buying "
                    "power, and current easy-to-borrow shortability",
                    code="SHORT_ENTRY_NOT_AUTHORIZED",
                )
        # Always verify the symbol's CURRENT broker position: Alpaca transports a short sale
        # as ``sell`` and a cover as ``buy``, so only the verified position and its direction
        # can prove an order opens, closes, adds to or would REVERSE a position.
        observed = cast(Any, self._account).get_position(intent.symbol)
        known_position = None if observed is None else abs(observed.qty)
        known_side = None if observed is None else str(observed.side).lower()
        from mike_trade.brokers.alpaca.capabilities import default_alpaca_capabilities
        from mike_trade.brokers.alpaca.orders import OrderPurpose as AlpacaOrderPurpose

        matrix = self.capabilities_matrix or default_alpaca_capabilities()
        capability_check = cast(Any, matrix).as_legacy_checker(self.mode.value)

        alpaca_purpose = (
            AlpacaOrderPurpose.ENTRY_SHORT
            if intent.side is OrderSide.SELL_SHORT
            else (
                AlpacaOrderPurpose.EXIT_SHORT
                if intent.side is OrderSide.BUY_TO_COVER
                else intent.purpose
            )
        )
        result = cast(Any, self._orders).submit_order(
            asset=asset,
            side=intent.side.broker_side,
            order_type=intent.order_type.value,
            qty=intent.quantity,
            notional=intent.notional,
            limit_price=intent.limit_price,
            time_in_force=intent.time_in_force.value,
            cycle_id=intent.cycle_id,
            purpose=alpaca_purpose,
            known_position_qty=known_position,
            known_position_side=known_side,
            capability_check=capability_check,
        )
        converted = self._order(result) if hasattr(result, "order_id") else result
        return converted

    async def cancel_order(self, order_id: str) -> object:
        self._ready()
        return cast(Any, self._orders).cancel_order(order_id)

    def order_updates_fresh(self) -> bool:
        return self._client is not None


def _environment(mode: ExecutionMode) -> AccountEnvironment:
    return AccountEnvironment(mode.value)


def _scopes(settings: AppSettings) -> tuple[AccountScope, ...]:
    environment = _environment(settings.mode)
    if settings.mode is ExecutionMode.SHADOW:
        return tuple(
            AccountScope(environment, settings.account_id, scenario.name)
            for scenario in settings.shadow_scenarios
        )
    if settings.mode is ExecutionMode.BACKTEST:
        return (AccountScope(environment, settings.account_id, "default"),)
    return (AccountScope(environment, settings.account_id),)


def _is_legacy_pair(item: SymbolStrategySettings) -> bool:
    return (
        len(item.buys) == 1 and len(item.sells) == 1
        and not item.shorts and not item.covers
        and item.buys[0].plugin == "vwap_bounce" and not item.buys[0].parameters
        and item.sells[0].plugin == "vwap_breakdown" and not item.sells[0].parameters
    )


def _fingerprint(settings: AppSettings) -> str:
    document = {
        "mode": settings.mode.value,
        "account_id": settings.account_id,
        "symbols": settings.symbols,
        "scenarios": [
            {
                "name": item.name,
                "starting_cash": str(item.starting_cash),
                "per_symbol_budget": str(item.per_symbol_budget),
                "total_budget": str(item.total_budget),
                **(
                    {
                        "short_selling": True,
                        "short_margin_multiplier": str(item.short_margin_multiplier),
                        "short_borrow_bps_per_day": str(item.short_borrow_bps_per_day),
                    }
                    if item.short_selling_enabled
                    else {}
                ),
            }
            for item in settings.shadow_scenarios
        ],
        "entry_cutoff_minutes": settings.entry_cutoff_minutes,
        "stop_trail_percent": str(settings.stop_trail_percent),
    }
    # The single vwap_reference pair with a native trailing stop is the legacy
    # shape; its fingerprint must not change (a changed fingerprint blocks
    # entries until an operator acknowledges it). Any other strategy
    # collection or stop policy is configuration drift and is fingerprinted.
    custom = [
        {
            "symbol": item.symbol,
            "buy": [[a.plugin, dict(a.parameters)] for a in item.buys],
            "sell": [[a.plugin, dict(a.parameters)] for a in item.sells],
            **(
                {
                    "short": [[a.plugin, dict(a.parameters)] for a in item.shorts],
                    "cover": [[a.plugin, dict(a.parameters)] for a in item.covers],
                }
                if item.shorts or item.covers
                else {}
            ),
        }
        for item in settings.symbol_strategies
        if not _is_legacy_pair(item)
    ]
    if custom:
        document["strategies"] = custom
    custom_stops = {
        symbol: {key: str(value) for key, value in spec.items()}
        for symbol, spec in sorted(settings.symbol_stop_policies.items())
        if not (
            spec.get("type") == "native_trailing"
            and spec.get("trail_percent") == settings.stop_trail_percent
        )
    }
    if custom_stops:
        document["stop_policies"] = custom_stops
    if settings.market_status_enabled:  # absent when off: existing fingerprints are unchanged
        document["market_status"] = list(settings.market_status_reference_symbols)
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_runtime(
    settings: AppSettings, *, overrides: Mapping[str, object] | None = None
) -> Runtime:
    """Build the complete production object graph without opening external resources."""
    if settings.mode is ExecutionMode.LIVE and not settings.enable_live_trading:
        raise LiveTradingNotAuthorizedError(
            "LIVE construction requires explicit enable_live_trading=True authorization.",
            code="LIVE_NOT_AUTHORIZED",
        )
    supplied = dict(overrides or {})
    allowed = {
        "clock", "scheduler", "broker", "market_data", "research", "persistence",
        "portfolio", "risk", "execution", "reporting", "controls", "logger",
        "calendar", "candle_engine", "strategies", "eligibility", "budget_manager",
        "protection", "reconciliation", "instance_lock", "startup_validator",
        "market_status_monitor",
    }
    unknown = sorted(set(supplied) - allowed)
    if unknown:
        raise ConfigurationError(
            f"Unknown runtime override(s): {', '.join(unknown)}",
            code="UNKNOWN_OVERRIDE", context={"unknown": unknown},
        )

    configure_logging(level=settings.log_level)
    if "clock" in supplied:
        clock = cast(ClockPort, supplied["clock"])
    elif settings.mode is ExecutionMode.BACKTEST:
        from mike_trade.backtest.clock import ReplayClock

        clock = ReplayClock(settings.backtest_start)
    else:
        clock = SystemClock()
    calendar = supplied.get("calendar", ExchangeCalendar(clock))
    default_market_data: MarketDataPort
    if settings.market_data_provider == "alpaca" and settings.mode is not ExecutionMode.BACKTEST:
        default_market_data = cast(
            MarketDataPort,
            AlpacaProductionMarketData(
                clock,
                settings.max_bar_age,
                feed=settings.market_data_feed,
                warmup_bars=settings.market_data_warmup_bars,
                environ=settings.credential_environment,
            ),
        )
    else:
        default_market_data = ProductionMarketData(clock, settings.max_bar_age)
    market_data = cast(MarketDataPort, supplied.get("market_data", default_market_data))
    registry = StrategyRegistry()
    for strategy_cls in (
        VwapBounceBuy, MomentumBreakoutBuy, OpeningRangeBreakoutBuy,
        VwapBreakdownSell, MomentumReversalSell, RsiReversalSell,
        AroonTrendBuy,
        CciZeroLineCrossoverBuy,
        DmiCrossoverBuy,
        DonchianChannelBreakoutBuy,
        EmaCrossoverBuy,
        InsideBarBreakoutBuy,
        MacdSignalCrossoverBuy,
        MacdZeroLineCrossoverBuy,
        ParabolicSarBuy,
        RsiCenterlineCrossoverBuy,
        RsiOversoldReversalBuy,
        SmaCrossoverBuy,
        StochasticCrossoverBuy,
        WilliamsRReversionBuy,
        BollingerBandBreakoutBuy,
        CciReversionBuy,
        StochasticOversoldReversalBuy,
        MoneyFlowIndexBuy,
        ChaikinMoneyFlowBuy,
        EngulfingCandleReversalBuy,
        AwesomeOscillatorBuy,
        HullMovingAverageBuy,
        KeltnerChannelBreakoutBuy,
        LinearRegressionTrendBuy,
        MovingAverageEnvelopeBuy,
        MovingAverageReclaimBuy,
        Nr7BreakoutBuy,
        RateOfChangeMomentumBuy,
        TripleMovingAverageBuy,
        AdaptiveMovingAverageBuy,
        AdxTrendFollowingBuy,
        AtrExpansionBreakoutBuy,
        BollingerSqueezeBuy,
        ConnorsRsiMeanReversionBuy,
        EaseOfMovementBuy,
        ElderImpulseSystemBuy,
        EmaPullbackBuy,
        HammerReversalBuy,
        HigherHighHigherLowBuy,
        IchimokuCloudBreakoutBuy,
        KeltnerChannelReversionBuy,
        MovingAverageRibbonBuy,
        ResistanceBreakoutBuy,
        SupertrendBuy,
        VolatilitySqueezeBreakoutBuy,
        OpeningDriveBuy,
        OpeningRangeRetestBuy,
        HighOfDayMomentumBuy,
        MiddayRangeBreakoutBuy,
        AfternoonBreakoutBuy,
        PowerHourMomentumBuy,
        ShortEmaBearishCrossover,
        ShortSupertrendBearishFlip,
        ShortDonchianBreakdown,
        ShortSmaBearishCrossover,
        ShortMacdBearishCrossover,
        ShortMacdZeroLineBreakdown,
        ShortRsiCenterlineBreakdown,
        ShortDmiBearishCrossover,
        ShortAroonBearishReversal,
        ShortParabolicSarBearishReversal,
        ShortIchimokuBearishBreakdown,
        ShortOpeningRangeBreakdown,
        ShortVwapBreakdown,
        ShortBollingerLowerBandBreakdown,
        ShortKeltnerChannelBreakdown,
        ShortObvBreakdown,
        ShortCciZeroLineBreakdown,
        ShortAdxBearishTrendConfirmation,
        CoverEmaBullishCrossover,
        CoverSupertrendBullishFlip,
        CoverDonchianBreakout,
        CoverSmaBullishCrossover,
        CoverMacdBullishCrossover,
        CoverMacdZeroLineCrossover,
        CoverRsiOversoldReversal,
        CoverRsiCenterlineCrossover,
        CoverDmiBullishCrossover,
        CoverAroonBullishReversal,
        CoverParabolicSarBullishReversal,
        CoverVwapReclaim,
        CoverBollingerLowerBandReversion,
        CoverKeltnerChannelReversion,
        CoverCciOversoldReversal,
        CoverStochasticBullishCrossover,
        CoverWilliamsRBullishReversal,
        CoverChaikinMoneyFlowRecovery,
        ObvBreakoutBuy,
        AccumulationDistributionBreakoutBuy,
        VolumePriceTrendBreakoutBuy,
        TsiMomentumBuy,
        StochasticRsiBuy,
        UltimateOscillatorBuy,
        HolyGrailPullbackBuy,
        Rsi2TrendPullbackBuy,
        ZScoreMeanReversionBuy,
        DoubleBottomReversalBuy,
        InverseHeadAndShouldersBuy,
        AscendingTriangleBreakoutBuy,
        TrendlineBreakoutBuy,
        TriangleBreakoutBuy,
        BullFlagBreakoutBuy,
        GapAndGoBuy,
        GapFillBuy,
        GapReversalBuy,
        PreviousCloseReclaimBuy,
        PreviousDayHighBreakoutBuy,
        PreviousDayLowReversalBuy,
        AroonBearishReversalSell,
        CciZeroLineBreakdownSell,
        DmiBearishCrossoverSell,
        DonchianLowerChannelExitSell,
        EmaBearishCrossoverSell,
        MacdBearishCrossoverSell,
        MacdZeroLineBreakdownSell,
        ParabolicSarReversalSell,
        PriceBelowEmaSell,
        PriceBelowSmaSell,
        RsiCenterlineBreakdownSell,
        SmaBearishCrossoverSell,
        StochasticBearishCrossoverSell,
        WilliamsRReversalSell,
        CciBearishReversalSell,
        MoneyFlowIndexReversalSell,
        ChaikinMoneyFlowReversalSell,
        BearishEngulfingExitSell,
        AwesomeOscillatorReversalSell,
        KeltnerChannelBreakdownSell,
        PreviousSwingLowBreakSell,
        AdxTrendWeakeningSell,
        BollingerUpperBandReversalSell,
        FailedBreakoutExitSell,
        LowerLowConfirmationSell,
        OpeningRangeBreakdownSell,
        StochasticRsiReversalSell,
        TrendlineBreakdownSell,
        UltimateOscillatorReversalSell,
        AccumulationDistributionBreakdownSell,
        BearishMomentumDivergenceSell,
        DoubleTopReversalSell,
        HeadAndShouldersBreakdownSell,
        IchimokuBearishExitSell,
        ObvBreakdownSell,
        ShootingStarReversalSell,
        SupertrendBearishFlipSell,
    ):
        registry.register(strategy_cls)
    strategies = supplied.get("strategies", registry)
    assigned = {item.symbol: item for item in settings.symbol_strategies}
    pair_resolution = (
        resolve_pair_preset("vwap_reference", registry=registry, mode="shadow")
        if settings.mode is ExecutionMode.SHADOW
        else None
    )
    scheduler = supplied.get(
        "scheduler",
        StrategyScheduler(
            clock, calendar=cast(Any, calendar), watched_symbols=settings.symbols,
            entry_cutoff_minutes=settings.entry_cutoff_minutes,
        ),
    )
    if isinstance(scheduler, StrategyScheduler):
        for symbol in settings.symbols:
            item = assigned.get(symbol)
            plan = (
                [("vwap_bounce", StrategyRole.BUY), ("vwap_breakdown", StrategyRole.SELL)]
                if item is None
                else [(a.plugin, StrategyRole.BUY) for a in item.buys]
                + [(a.plugin, StrategyRole.SELL) for a in item.sells]
                + [(a.plugin, StrategyRole.SHORT) for a in item.shorts]
                + [(a.plugin, StrategyRole.COVER) for a in item.covers]
            )
            for strategy_id, side in plan:
                scheduler.register(
                    symbol=symbol, strategy_id=strategy_id, side=side,
                    triggers=(CANDLE_CLOSED(CandleTimeframe.M1),),
                )

    scopes = _scopes(settings)
    persistence = cast(
        PersistencePort,
        supplied.get(
            "persistence",
            SQLitePersistence(settings.database_path, _environment(settings.mode), scopes, clock),
        ),
    )
    controls = supplied.get("controls")
    raw_entry_gate = controls if isinstance(controls, EntryGate) else EntryGate()
    if controls is None:
        controls = raw_entry_gate

    market_status_monitor = supplied.get("market_status_monitor")
    if market_status_monitor is None and settings.market_status_enabled:
        ms_config = settings.market_status_config or MarketStatusConfig(
            symbols=settings.market_status_reference_symbols
        )
        ms_policy = settings.market_status_policy or TradingAuthorizationPolicy()
        market_status_monitor = MarketStatusMonitor(
            config=ms_config,
            policy=ms_policy,
            clock=clock,
        )

    if settings.market_status_enabled and market_status_monitor is not None:
        entry_gate = MarketStatusEntryGate(
            monitor=market_status_monitor,
            inner_gate=raw_entry_gate,
        )
    else:
        entry_gate = raw_entry_gate
    if supplied.get("controls") is None:
        # The runtime treats "controls is the entry gate" as "no separate operator control";
        # keep that identity when the market-status wrapper stands in for the plain gate.
        controls = entry_gate

    bind_ms = getattr(market_data, "bind_market_status", None)
    if callable(bind_ms) and market_status_monitor is not None:
        bind_ms(
            market_status_monitor,
            reference_symbols=settings.market_status_reference_symbols,
            trading_symbols=settings.symbols,
        )

    if "broker" in supplied:
        broker = supplied["broker"]
    elif settings.mode is ExecutionMode.SHADOW:
        if not isinstance(persistence, SQLitePersistence):
            raise ConfigurationError("default SHADOW broker requires SQLitePersistence")
        broker = ShadowBrokerGroup(
            persistence, clock, settings.account_id, settings.shadow_scenarios
        )
    elif settings.mode is ExecutionMode.BACKTEST:
        broker = _BacktestBrokerAdapter(clock)
    else:
        broker = AlpacaBrokerAdapter(
            settings.mode, settings.account_id, clock,
            authorized=settings.enable_live_trading,
            # Shorting is opt-in and PAPER-only here; LIVE shorting is never activated.
            short_authorized=(
                settings.short_selling_enabled and settings.mode is ExecutionMode.PAPER
            ),
            environ=settings.credential_environment,
        )

    budget_policy = BudgetPolicy(
        settings.total_budget, settings.per_symbol_budget, Decimal("0"), False
    )
    budget_manager = supplied.get(
        "budget_manager", ReservationLedger(budget_policy, clock=clock)
    )
    protection_policy = ProtectionPolicy(
        ProtectionKind.BROKER_TRAILING_STOP,
        trail_percent=settings.stop_trail_percent,
        require_broker_held=True,
    )
    risk_protection_policy = RiskProtectionPolicy(
        mode=RiskProtectionMode.TRAILING_NATIVE,
        trail_percent=settings.stop_trail_percent,
    )
    lifecycle: object | None = None
    if isinstance(broker, ShadowBrokerGroup):
        lifecycle = VwapReferenceLifecycle(
            broker, clock, cast(EntryGate, entry_gate), settings.entry_cutoff_minutes,
            protection_policy,
            cast(Mapping[str, object], pair_resolution["buy_params"]),
            cast(Mapping[str, object], pair_resolution["sell_params"]),
            assignments=tuple(
                item for item in settings.symbol_strategies if not _is_legacy_pair(item)
            ),
            registry=cast(StrategyRegistry, strategies),
            stop_specs={
                symbol: spec
                for symbol, spec in settings.symbol_stop_policies.items()
                if not (
                    spec.get("type") == "native_trailing"
                    and spec.get("trail_percent") == settings.stop_trail_percent
                )
            },
        )
    execution = supplied.get("execution", lifecycle or broker)
    protection = supplied.get(
        "protection",
        lifecycle
        or ProtectionCoordinator(cast(Any, broker), clock, risk_protection_policy),
    )
    if "reconciliation" in supplied:
        reconciliation = supplied["reconciliation"]
    elif isinstance(broker, ShadowBrokerGroup):
        reconciliation = _CleanShadowReconciler(broker)
    elif settings.mode is ExecutionMode.BACKTEST:
        reconciliation = _CleanBacktestReconciler()
    elif isinstance(persistence, SQLitePersistence):
        reconciliation = _BrokerReconciler(persistence, broker, clock, scopes[0])
    else:
        reconciliation = None
    logger = cast(
        logging.Logger, supplied.get("logger", get_logger("mike_trade.app"))
    )
    runtime = Runtime(
        mode=settings.mode,
        clock=clock,
        scheduler=cast(SchedulerPort, scheduler),
        broker=cast(BrokerPort, broker),
        market_data=market_data,
        research=cast(ResearchPort | None, supplied.get("research")),
        persistence=persistence,
        portfolio=cast(PortfolioPort, supplied.get("portfolio", _PortfolioView(broker))),
        risk=cast(RiskPort, supplied.get("risk", _RiskVerifier())),
        execution=cast(ExecutionPort, execution),
        reporting=cast(ReportingPort, supplied.get("reporting", _NoopReporting())),
        controls=cast(ControlsPort, controls),
        logger=logger,
        calendar=calendar,
        candle_engine=supplied.get(
            "candle_engine", getattr(market_data, "candles", None)
        ),
        strategies=strategies,
        eligibility=supplied.get("eligibility", broker),
        budget_manager=budget_manager,
        protection=protection,
        reconciliation=reconciliation,
        instance_lock=supplied.get(
            "instance_lock", RuntimeInstanceLock(f"{settings.database_path}.instance.lock")
        ),
        startup_validator=supplied.get("startup_validator"),
        config_fingerprint=_fingerprint(settings),
        account_scope_id=scopes[0].scope_id,
        symbols=settings.symbols,
        lifecycle=lifecycle,
        market_status_monitor=market_status_monitor,
        entry_gate=entry_gate,
        heartbeat_interval=settings.heartbeat_interval_seconds,
    )
    bind_engine = getattr(market_data, "bind_engine", None)
    if callable(bind_engine):
        try:
            bind_engine(scheduler, lifecycle, trading_symbols=settings.symbols)
        except TypeError:
            bind_engine(scheduler, lifecycle)
    logger.info(
        "Runtime graph built",
        extra={
            "mode": settings.mode.value,
            "database": str(settings.database_path),
            "symbols": len(settings.symbols),
            "scenarios": (
                len(settings.shadow_scenarios)
                if settings.mode is ExecutionMode.SHADOW
                else 0
            ),
        },
    )
    return runtime
