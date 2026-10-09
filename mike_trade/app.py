"""Dependency-injected production composition root for :mod:`mike_trade`.

Concrete provider imports are lazy.  Building SHADOW/BACKTEST therefore cannot
load an Alpaca adapter, read broker credentials, or touch the network.  Resource
opening remains part of :class:`mike_trade.runtime.Runtime` startup so the
instance lock is always acquired before SQLite migration or broker validation.
"""

from __future__ import annotations

import hashlib
import json
import logging
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
from mike_trade.domain.errors import ConfigurationError, LiveTradingNotAuthorizedError
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
    TimeInForce,
)
from mike_trade.execution.coordinator import (
    DecisionReason,
    OrderCommand,
    OrderCommandCoordinator,
)
from mike_trade.market_data.cache import MarketDataCache, MarketDataUpdate
from mike_trade.market_data.candles import CandleBuilder, CandleTimeframe
from mike_trade.market_data.context import (
    ContextBuilder,
    IndicatorInput,
    IndicatorSpec,
    MarketContext,
)
from mike_trade.observability.logging import configure_logging, get_logger
from mike_trade.persistence.maintenance import check_integrity, checkpoint
from mike_trade.persistence.repositories import (
    RunConfigRepository,
    ServiceHealthRepository,
    StrategyStateRepository,
)
from mike_trade.persistence.schema import initialize_database, to_db_timestamp
from mike_trade.persistence.transaction import WriterSession
from mike_trade.portfolio.budgets import BudgetPolicy
from mike_trade.portfolio.reservations import ReservationLedger
from mike_trade.risk.protection import ProtectionCoordinator
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
from mike_trade.strategies.buy.vwap_bounce import VwapBounceBuy
from mike_trade.strategies.indicators.core import VWAPCalculator
from mike_trade.strategies.presets.pairs import resolve_pair_preset
from mike_trade.strategies.registry import StrategyRegistry
from mike_trade.strategies.sell.vwap_breakdown import VwapBreakdownSell

__all__ = [
    "AppSettings",
    "ShadowScenarioSettings",
    "SQLitePersistence",
    "ProductionMarketData",
    "VwapReferenceLifecycle",
    "build_runtime",
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

    def __post_init__(self) -> None:
        if not self.name.strip() or ":" in self.name:
            raise ValueError("scenario name must be non-empty and contain no ':'")
        if self.name.upper() in {"PAPER", "LIVE"}:
            raise ValueError("scenario name collides with a broker environment")
        if self.total_budget < self.per_symbol_budget:
            raise ValueError("total_budget must be >= per_symbol_budget")


@dataclass(frozen=True, slots=True)
class AppSettings:
    """Non-secret application composition inputs."""

    mode: ExecutionMode = ExecutionMode.SHADOW
    enable_live_trading: bool = False
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
    backtest_start: datetime = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    auto_eod_liquidation: bool = False
    credential_environment: Mapping[str, str] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", ExecutionMode(self.mode))
        symbols = tuple(dict.fromkeys(symbol.strip().upper() for symbol in self.symbols))
        if not symbols or any(not symbol for symbol in symbols):
            raise ValueError("symbols must contain at least one non-empty symbol")
        object.__setattr__(self, "symbols", symbols)
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
            timeframes=(CandleTimeframe.M1,), max_history=500, clock=clock,
            session_open=time(9, 30),
        )
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
        )
        self.running = False
        self.closed = False
        self.acquisitions: dict[str, int] = {}

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


@dataclass(slots=True)
class _ShadowScenario:
    settings: ShadowScenarioSettings
    ledger: ShadowLedger
    account: ShadowAccount
    fills: ShadowFillSimulator
    stops: ShadowStopSimulator
    states: dict[tuple[str, str], dict[str, object]]
    coordinator: OrderCommandCoordinator


class _CoordinatorProviders:
    def __init__(self, account: ShadowAccount) -> None:
        self.account = account

    def quote_age(self, _intent: object, _now: datetime) -> timedelta:
        return timedelta(0)

    def has_open_or_unresolved_order(self, key: object, purpose: str) -> bool:
        symbol = cast(Any, key).symbol
        for order in self.account.state.orders.values():
            if not order.is_open or order.symbol != symbol:
                continue
            if purpose == "ENTRY_LONG" and order.side is OrderSide.BUY:
                return True
            if purpose == "EXIT_LONG" and order.side is OrderSide.SELL and (
                order.purpose is not OrderPurpose.PROTECTIVE_STOP
            ):
                return True
        return False

    def confirmed_long_quantity(self, key: object) -> Decimal:
        position = self.account.state.positions.get(cast(Any, key).symbol)
        return Decimal("0") if position is None else position.quantity

    def committed_sell_quantity(self, key: object) -> Decimal:
        return sum(
            (order.remaining for order in self.account.state.open_orders()
             if order.symbol == cast(Any, key).symbol and order.side is OrderSide.SELL),
            Decimal("0"),
        )

    def protective_sell_quantity(self, key: object) -> Decimal:
        return sum(
            (order.remaining for order in self.account.state.open_orders()
             if order.symbol == cast(Any, key).symbol
             and order.purpose is OrderPurpose.PROTECTIVE_STOP),
            Decimal("0"),
        )

    async def available_cash(self, _key: object) -> Decimal:
        return (await self.account.get_account()).cash


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


class VwapReferenceLifecycle:
    """Production evaluator for the mandatory vwap_reference BUY/SELL pair."""

    def __init__(
        self,
        group: ShadowBrokerGroup,
        clock: ClockPort,
        entry_gate: EntryGate,
        cutoff_minutes: int,
        protection_policy: ProtectionPolicy,
        buy_parameters: Mapping[str, object],
        sell_parameters: Mapping[str, object],
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
        self._in_flight = 0

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
                if order.side is OrderSide.BUY:
                    await scenario.stops.arm(
                        stored.cycle_id, context.symbol, self.protection_policy,
                        at=context.as_of,
                    )

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

        position = account.state.positions.get(context.symbol)
        if position is not None:
            state = self._state(scenario, "sell", context.symbol)
            evaluation = self.sell.evaluate(context, self.sell_parameters, state)
            self._save_state(scenario, "sell", context.symbol, evaluation.state_updates)
            held = context.as_of - position.opened_at
            if evaluation.is_signal and held >= scenario.settings.minimum_hold:
                actions.extend(await self._exit(scenario, context, evaluation.signal_id or ""))
            return actions

        state = self._state(scenario, "buy", context.symbol)
        state["eligibility"] = "WATCHING_BUY"
        evaluation = self.buy.evaluate(context, self.buy_parameters, state)
        self._save_state(scenario, "buy", context.symbol, evaluation.state_updates)
        if not evaluation.is_signal or not self.entry_gate.authorized:
            return actions
        local_day = context.as_of.astimezone(exchange_calendar.NEW_YORK).date()
        session = exchange_calendar.session_for(local_day)
        if context.as_of >= exchange_calendar.entry_cutoff(session, self.cutoff_minutes):
            return actions
        reference = evaluation.reference_price
        if reference is None:
            return actions
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
            "SHADOW", scenario.account.scope.scope_id, context.symbol, "ENTRY_LONG",
            context.as_of + timedelta(minutes=1), context.as_of,
            quantity, None, reference,
        )
        command = OrderCommand(command_intent, reservation_amount=quantity * reference)
        decision = await scenario.coordinator.authorize(command)
        if not decision.authorized:
            return actions
        intent = OrderIntent(
            intent_id=f"intent-{evaluation.signal_id}", client_order_id=client_id,
            account_scope_id=account.scope.scope_id, cycle_id=cycle_id,
            symbol=context.symbol, side=OrderSide.BUY, purpose=OrderPurpose.ENTRY,
            order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY,
            created_at=context.as_of, quantity=quantity, signal_id=evaluation.signal_id,
        )
        order = await account.submit_order(intent, strategy_key="vwap_bounce")
        actions.append(f"ENTRY:{order.broker_order_id}")
        if context.latest_quote is not None:
            fill = scenario.fills.apply_fill_to_account(
                account, order.broker_order_id, context, as_of=context.as_of
            )
            if fill.is_fill:
                actions.append(f"FILL:BUY:{fill.quantity}")
                await scenario.stops.arm(
                    cycle_id, context.symbol, self.protection_policy, at=context.as_of
                )
        if decision.reservation is not None:
            # The shadow account now durably owns both the filled commitment and any
            # open-order remainder; the coordinator's short-lived authorization hold
            # must not double-count that capital.
            await scenario.coordinator.release_reservation(decision.reservation)
        else:
            await scenario.coordinator.complete(command)
        return actions

    async def _exit(
        self, scenario: _ShadowScenario, context: MarketContext, signal_id: str
    ) -> list[str]:
        position = scenario.account.state.positions[context.symbol]
        command_intent = _CommandIntent(
            "SHADOW", scenario.account.scope.scope_id, context.symbol, "EXIT_LONG",
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
            side=OrderSide.SELL, purpose=OrderPurpose.STRATEGY_EXIT,
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
                actions.append(f"FILL:SELL:{fill.quantity}")
        await scenario.coordinator.complete(command)
        return actions

    def _state(
        self, scenario: _ShadowScenario, role: str, symbol: str
    ) -> dict[str, object]:
        key = (role, symbol)
        if key not in scenario.states:
            loaded: dict[str, object] = {}
            session = self.group.persistence.session
            if session is not None:
                instance_id = self._ensure_strategy_instance(
                    scenario, role, symbol
                )
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
        role: str,
        symbol: str,
        updates: Mapping[str, object],
    ) -> None:
        state = self._state(scenario, role, symbol)
        state.update(updates)
        session = self.group.persistence.session
        if session is not None:
            instance_id = self._ensure_strategy_instance(scenario, role, symbol)
            StrategyStateRepository(
                session.bind_scope(scenario.account.scope.scope_id), self.clock
            ).save_state(instance_id, symbol, state)

    def _ensure_strategy_instance(
        self, scenario: _ShadowScenario, role: str, symbol: str
    ) -> str:
        session = self.group.persistence.session
        if session is None:
            raise RuntimeError("persistence is not open")
        scope = scenario.account.scope.scope_id
        instance_id = f"{scope}:vwap_reference:{role}:{symbol}"
        strategy_key = "vwap_bounce" if role == "buy" else "vwap_breakdown"
        with session.unit_of_work() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO strategy_instances("
                "instance_id,account_scope_id,strategy_key,strategy_version,role,"
                "symbol,parameters_json,enabled,pair_key,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    instance_id,
                    scope,
                    strategy_key,
                    "1.0.0",
                    role.upper(),
                    symbol,
                    json.dumps(
                        self.buy_parameters if role == "buy" else self.sell_parameters,
                        sort_keys=True,
                        default=str,
                    ),
                    1,
                    "vwap_reference",
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
        environ: Mapping[str, str] | None,
    ) -> None:
        self.mode = mode
        self.environment = AccountEnvironment(mode.value)
        self.scope = AccountScope(self.environment, account_id)
        self.clock = clock
        self.authorized = authorized
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
            supports_short=False,
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
        if item.side != "long" or item.qty <= 0:
            raise RuntimeError(f"unsupported external short position: {item.symbol}")
        return Position(
            account_scope_id=self.scope.scope_id,
            symbol=item.symbol,
            quantity=item.qty,
            avg_entry_price=item.avg_entry_price,
            cost_basis=item.cost_basis,
            as_of=self.clock.now(),
            market_value=item.market_value,
            management=ManagementStatus.UNMANAGED,
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
            side = OrderSide(str(item.side).upper())
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
                side=OrderSide(item.side.upper()),
                quantity=item.qty,
                price=item.price,
                fee=Decimal("0"),
                filled_at=item.transaction_time,
            )
            for item in activities
        )

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
        known_position = None
        if intent.side is OrderSide.SELL:
            observed = cast(Any, self._account).get_position(intent.symbol)
            known_position = None if observed is None else observed.qty
        result = cast(Any, self._orders).submit_order(
            asset=asset,
            side=intent.side.value,
            order_type=intent.order_type.value,
            qty=intent.quantity,
            notional=intent.notional,
            limit_price=intent.limit_price,
            time_in_force=intent.time_in_force.value,
            cycle_id=intent.cycle_id,
            purpose=intent.purpose,
            known_position_qty=known_position,
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
            }
            for item in settings.shadow_scenarios
        ],
        "entry_cutoff_minutes": settings.entry_cutoff_minutes,
        "stop_trail_percent": str(settings.stop_trail_percent),
    }
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
    market_data = cast(
        MarketDataPort,
        supplied.get("market_data", ProductionMarketData(clock, settings.max_bar_age)),
    )
    registry = StrategyRegistry()
    registry.register(VwapBounceBuy)
    registry.register(VwapBreakdownSell)
    strategies = supplied.get("strategies", registry)
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
            scheduler.register(
                symbol=symbol, strategy_id="vwap_bounce", side=StrategyRole.BUY,
                triggers=(CANDLE_CLOSED(CandleTimeframe.M1),),
            )
            scheduler.register(
                symbol=symbol, strategy_id="vwap_breakdown", side=StrategyRole.SELL,
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
    entry_gate = controls if isinstance(controls, EntryGate) else EntryGate()
    if controls is None:
        controls = entry_gate

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
        entry_gate=entry_gate,
    )
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
