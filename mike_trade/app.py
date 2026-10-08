"""
Application composition root and runtime builder for mike_trade.

Only this module (and ``runtime.py``) may wire concrete adapters. Concrete adapter imports, when
added, must be lazy inside factory functions so that importing this module never loads broker
SDKs or touches the network.
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn, cast

from mike_trade.domain.errors import (
    BrokerUnavailableError,
    ConfigurationError,
    EntryBlockedError,
    LiveTradingNotAuthorizedError,
    PersistenceError,
)
from mike_trade.observability.logging import configure_logging, get_logger
from mike_trade.runtime import (
    BrokerPort,
    ClockPort,
    ControlsPort,
    ExecutionMode,
    ExecutionPort,
    MarketDataPort,
    PersistencePort,
    PortfolioPort,
    ReportingPort,
    ResearchPort,
    RiskPort,
    Runtime,
    SchedulerPort,
    SystemClock,
)

# Runtime slots that may be supplied through ``overrides`` (``mode`` always comes from settings).
OVERRIDABLE_SLOTS = frozenset(
    {
        "clock",
        "scheduler",
        "broker",
        "market_data",
        "research",
        "persistence",
        "portfolio",
        "risk",
        "execution",
        "reporting",
        "controls",
        "logger",
    }
)


@dataclass(frozen=True)
class AppSettings:
    """Application composition inputs. Holds no secret values."""

    mode: ExecutionMode = ExecutionMode.SHADOW
    enable_live_trading: bool = False
    log_level: str = "INFO"
    db_path: str = ":memory:"
    symbols: tuple[str, ...] = ("AAPL",)


def _not_configured(component: str, operation: str, error: type[Exception]) -> NoReturn:
    raise error(f"{component} component is not configured; cannot {operation}")


class NullScheduler:
    """Offline scheduler placeholder."""

    def schedule(self, name: str, due: datetime) -> None:
        _not_configured("scheduler", "schedule tasks", ConfigurationError)


class NullBroker:
    """Offline broker placeholder."""

    def submit_order(self, order: object) -> object:
        _not_configured("broker", "submit orders", BrokerUnavailableError)

    def cancel_order(self, order_id: str) -> None:
        _not_configured("broker", "cancel orders", BrokerUnavailableError)


class NullMarketData:
    """Offline market-data placeholder."""

    def subscribe(self, symbols: Sequence[str]) -> None:
        _not_configured("market_data", "subscribe to market data", ConfigurationError)


class NullPersistence:
    """Offline persistence placeholder."""

    def append_event(self, event: Mapping[str, object]) -> None:
        _not_configured("persistence", "append events", PersistenceError)


class NullPortfolio:
    """Offline portfolio placeholder."""

    def positions(self) -> Sequence[object]:
        _not_configured("portfolio", "read positions", ConfigurationError)


class NullRisk:
    """Offline risk placeholder; blocks every entry."""

    def check_entry(self, intent: object) -> None:
        _not_configured("risk", "evaluate entries", EntryBlockedError)


class NullExecution:
    """Offline execution placeholder; accepts no intents."""

    def submit_intent(self, intent: object) -> None:
        _not_configured("execution", "submit intents", EntryBlockedError)


class NullReporting:
    """Offline reporting placeholder."""

    def publish(self, report: Mapping[str, object]) -> None:
        _not_configured("reporting", "publish reports", ConfigurationError)


class NullControls:
    """Offline controls placeholder; fails closed instead of reporting entries enabled."""

    def entries_enabled(self) -> bool:
        _not_configured("controls", "read entry state", EntryBlockedError)


def _placeholder_factories() -> dict[str, Callable[[], object]]:
    """Safe offline defaults. Concrete adapter imports must be lazy inside factories."""
    return {
        "clock": SystemClock,
        "scheduler": NullScheduler,
        "broker": NullBroker,
        "market_data": NullMarketData,
        "research": lambda: None,
        "persistence": NullPersistence,
        "portfolio": NullPortfolio,
        "risk": NullRisk,
        "execution": NullExecution,
        "reporting": NullReporting,
        "controls": NullControls,
    }


# LIVE is deliberately absent: no live broker adapter exists yet.
_FACTORIES_BY_MODE: Mapping[ExecutionMode, Callable[[], dict[str, Callable[[], object]]]] = {
    ExecutionMode.BACKTEST: _placeholder_factories,
    ExecutionMode.SHADOW: _placeholder_factories,
    ExecutionMode.PAPER: _placeholder_factories,
}


def build_runtime(
    settings: AppSettings, *, overrides: Mapping[str, object] | None = None
) -> Runtime:
    """Builds the dependency-injected Runtime object graph.

    Component factories are selected by ``settings.mode``; entries in ``overrides`` take
    precedence so tests can supply fakes. Building a LIVE runtime fails closed.
    """
    supplied = dict(overrides or {})
    unknown = sorted(set(supplied) - OVERRIDABLE_SLOTS)
    if unknown:
        raise ConfigurationError(
            f"Unknown runtime override(s): {', '.join(unknown)}",
            code="UNKNOWN_OVERRIDE",
            context={"unknown": unknown},
        )

    if settings.mode == ExecutionMode.LIVE:
        if not settings.enable_live_trading:
            raise LiveTradingNotAuthorizedError(
                "Live trading requires explicit enable_live_trading=True authorization.",
                code="LIVE_NOT_AUTHORIZED",
            )
        raise ConfigurationError(
            "No live broker adapter is available; refusing to build a LIVE runtime.",
            code="LIVE_ADAPTER_UNAVAILABLE",
        )

    factories = _FACTORIES_BY_MODE[settings.mode]()

    def resolve(slot: str) -> object:
        return supplied[slot] if slot in supplied else factories[slot]()

    configure_logging(level=settings.log_level)
    logger = (
        cast(logging.Logger, supplied["logger"])
        if "logger" in supplied
        else get_logger("mike_trade.app")
    )

    runtime = Runtime(
        mode=settings.mode,
        clock=cast(ClockPort, resolve("clock")),
        scheduler=cast(SchedulerPort, resolve("scheduler")),
        broker=cast(BrokerPort, resolve("broker")),
        market_data=cast(MarketDataPort, resolve("market_data")),
        research=cast(ResearchPort | None, resolve("research")),
        persistence=cast(PersistencePort, resolve("persistence")),
        portfolio=cast(PortfolioPort, resolve("portfolio")),
        risk=cast(RiskPort, resolve("risk")),
        execution=cast(ExecutionPort, resolve("execution")),
        reporting=cast(ReportingPort, resolve("reporting")),
        controls=cast(ControlsPort, resolve("controls")),
        logger=logger,
    )

    logger.info(
        "Runtime built",
        extra={
            "mode": settings.mode.value,
            "symbol_count": len(settings.symbols),
            "overrides": sorted(supplied),
        },
    )
    return runtime
