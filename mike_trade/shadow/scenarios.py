"""Concurrent SHADOW scenarios over one shared real-time market-data stream.

Multiple named scenarios run side by side on a single shared snapshot: the
shared feed is invoked exactly once per event and the immutable snapshot is
copied per scenario before any scenario touches it.  Each scenario owns an
isolated SHADOW account (``account_id``/``scenario_id`` scope) with its own
starting cash, budgets, BUY/SELL strategy assignments, entry limits,
loss-streak lockout, cooldowns and fill economics.  Mutations for one
scenario are serialized; scenarios never share fills, streaks, limits,
cooldowns or cash.  Execution stays entirely on the shadow path
(:class:`ShadowAccount` / injected fill/stop simulators); no broker, network, or secret store is ever touched.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from mike_trade.brokers.shadow.account import ShadowAccount
from mike_trade.brokers.shadow.fills import FillAssumptions, ShadowFillSimulator
from mike_trade.brokers.shadow.ledger import ShadowLedger
from mike_trade.brokers.shadow.stops import StopTriggerAssumptions, ShadowStopSimulator
from mike_trade.domain.accounts import AccountEnvironment, AccountScope
from mike_trade.domain.errors import ModeIsolationError
from mike_trade.domain.trading import EntryEligibilityPolicy, OrderIntent, Position
from mike_trade.market_data.context import MarketContext, QuoteSnapshot
from mike_trade.persistence.transaction import WriterSession

__all__ = [
    "ScenarioConfig",
    "ScenarioRegistry",
    "ScenarioRunner",
    "ScenarioProjection",
]

_ZERO = Decimal("0")
_ONE = Decimal("1")
_FEED = "shadow-shared"
_RESERVED_IDS = frozenset({"PAPER", "LIVE"})


# --------------------------------------------------------------------- validation


def _scenario_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    text = value.strip()
    if text.upper() in _RESERVED_IDS:
        raise ValueError(f"{name} {text!r} collides with a reserved PAPER/LIVE identity")
    if ":" in text:
        raise ValueError(f"{name} {text!r} must not contain ':'")
    return text


def _money(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise ValueError(f"{name} must be Decimal; got {type(value).__name__}")
    if not value.is_finite() or value < _ZERO:
        raise ValueError(f"{name} must be a finite Decimal >= 0")
    return value


def _duration(value: object, name: str) -> timedelta:
    if not isinstance(value, timedelta) or value < timedelta(0):
        raise ValueError(f"{name} must be a non-negative timedelta")
    return value


def _strategy_ids(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{name} must be a tuple/list of strategy ids")
    ids: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"{name} entries must be non-empty strings")
        ids.append(entry.strip())
    return tuple(ids)


# ------------------------------------------------------------------ public config


@dataclass(frozen=True, slots=True)
class ScenarioConfig:
    """Immutable per-scenario economics, assignments and limits."""

    scenario_id: str
    starting_cash: Decimal
    per_symbol_budget: Decimal
    total_budget: Decimal
    account_id: str = "ACCT"
    buy_strategies: tuple[str, ...] = ()
    sell_strategies: tuple[str, ...] = ()
    max_entries_per_symbol_per_session: int = 3
    loss_streak_lockout: int | None = None
    post_sale_cooldown: timedelta = timedelta(0)
    fee_per_share: Decimal = _ZERO
    fee_bps: Decimal = _ZERO
    fee_per_fill: Decimal = _ZERO
    slippage_bps: Decimal = _ZERO
    slippage_ticks: Decimal = _ZERO
    latency: timedelta = timedelta(0)
    participation: Decimal = _ONE
    allow_fractional: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_id", _scenario_id(self.scenario_id, "scenario_id"))
        object.__setattr__(self, "account_id", _scenario_id(self.account_id, "account_id"))
        object.__setattr__(self, "starting_cash", _money(self.starting_cash, "starting_cash"))
        object.__setattr__(
            self, "per_symbol_budget", _money(self.per_symbol_budget, "per_symbol_budget")
        )
        object.__setattr__(self, "total_budget", _money(self.total_budget, "total_budget"))
        if self.total_budget < self.per_symbol_budget:
            raise ValueError("total_budget must be >= per_symbol_budget")
        object.__setattr__(
            self, "buy_strategies", _strategy_ids(self.buy_strategies, "buy_strategies")
        )
        object.__setattr__(
            self, "sell_strategies", _strategy_ids(self.sell_strategies, "sell_strategies")
        )
        entries = self.max_entries_per_symbol_per_session
        if isinstance(entries, bool) or not isinstance(entries, int) or entries < 1:
            raise ValueError("max_entries_per_symbol_per_session must be an int >= 1")
        if self.loss_streak_lockout is not None and (
            isinstance(self.loss_streak_lockout, bool)
            or not isinstance(self.loss_streak_lockout, int)
            or self.loss_streak_lockout < 1
        ):
            raise ValueError("loss_streak_lockout must be an int >= 1 or None")
        object.__setattr__(
            self, "post_sale_cooldown", _duration(self.post_sale_cooldown, "post_sale_cooldown")
        )
        object.__setattr__(self, "latency", _duration(self.latency, "latency"))
        for name in ("fee_per_share", "fee_bps", "fee_per_fill", "slippage_bps", "slippage_ticks"):
            object.__setattr__(self, name, _money(getattr(self, name), name))
        participation = self.participation
        if (
            isinstance(participation, bool)
            or not isinstance(participation, Decimal)
            or not participation.is_finite()
            or participation <= _ZERO
            or participation > _ONE
        ):
            raise ValueError("participation must be a finite Decimal in (0, 1]")
        if not isinstance(self.allow_fractional, bool):
            raise ValueError("allow_fractional must be bool")

    @property
    def scope(self) -> AccountScope:
        """This scenario's isolated SHADOW scope."""
        return AccountScope(AccountEnvironment.SHADOW, self.account_id, self.scenario_id)

    def eligibility_policy(self) -> EntryEligibilityPolicy:
        """Entry limits, loss-streak lockout and cooldown for this scenario only."""
        return EntryEligibilityPolicy(
            max_filled_entry_cycles_per_symbol_per_session=(
                self.max_entries_per_symbol_per_session
            ),
            consecutive_loss_lockout_threshold=self.loss_streak_lockout,
            post_sale_cooldown=self.post_sale_cooldown,
        )

    def fill_assumptions(self) -> FillAssumptions:
        """Fee/slippage/latency/participation economics for this scenario only."""
        return FillAssumptions(
            latency=self.latency,
            slippage_bps=self.slippage_bps,
            slippage_ticks=self.slippage_ticks,
            participation=self.participation,
            fee_per_fill=self.fee_per_fill,
            fee_per_share=self.fee_per_share,
            fee_bps=self.fee_bps,
            allow_fractional=self.allow_fractional,
        )


class ScenarioRegistry:
    """Named-scenario registry; rejects duplicates and PAPER/LIVE collisions."""

    def __init__(self) -> None:
        self._configs: dict[str, ScenarioConfig] = {}

    def register(self, config: ScenarioConfig) -> ScenarioConfig:
        if not isinstance(config, ScenarioConfig):
            raise TypeError("config must be a ScenarioConfig")
        if config.scenario_id in self._configs:
            raise ValueError(f"duplicate scenario {config.scenario_id!r}")
        self._configs[config.scenario_id] = config
        return config

    def get(self, scenario_id: str) -> ScenarioConfig:
        try:
            return self._configs[scenario_id]
        except KeyError:
            raise ValueError(f"unknown scenario {scenario_id!r}") from None

    def __contains__(self, scenario_id: object) -> bool:
        return scenario_id in self._configs

    def __len__(self) -> int:
        return len(self._configs)

    @property
    def scenario_ids(self) -> tuple[str, ...]:
        return tuple(self._configs)


@dataclass(frozen=True, slots=True)
class ScenarioProjection:
    """Per-scenario read projection; always labeled SHADOW/estimated, never aggregated."""

    scenario_id: str
    account_scope_id: str
    mode: str
    estimated: bool
    broker_confirmed: bool
    as_of: datetime
    cash: Decimal
    equity: Decimal
    buying_power: Decimal
    positions: tuple[Position, ...] = ()

    def __post_init__(self) -> None:
        if self.mode != AccountEnvironment.SHADOW.value:
            raise ValueError("scenario projections are always SHADOW")
        if self.estimated is not True or self.broker_confirmed is not False:
            raise ValueError("scenario projections are estimated and never broker-confirmed")


# ------------------------------------------------------- injection protocols (private)


class _FillSimulator(Protocol):
    def apply_fill_to_account(
        self,
        account: ShadowAccount,
        broker_order_id: str,
        quotes: Sequence[QuoteSnapshot] | MarketContext,
        *,
        as_of: datetime,
    ) -> object: ...


class _SharedFeed(Protocol):
    async def snapshot(
        self, symbols: tuple[str, ...]
    ) -> Mapping[str, MarketContext]: ...


_StrategyFn = Callable[[ScenarioConfig, MarketContext], OrderIntent | None]
_FillFactory = Callable[[ScenarioConfig], _FillSimulator]


@dataclass(slots=True)
class _ScenarioRuntime:
    config: ScenarioConfig
    ledger: ShadowLedger
    account: ShadowAccount
    fills: _FillSimulator
    stops: ShadowStopSimulator
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _default_fill_factory(config: ScenarioConfig) -> _FillSimulator:
    return ShadowFillSimulator(config.fill_assumptions())


def _assumption_mapping(simulator: _FillSimulator) -> dict[str, str]:
    assumptions = getattr(simulator, "assumptions", None)
    as_mapping = getattr(assumptions, "as_mapping", None)
    if callable(as_mapping):
        raw = as_mapping()
        if isinstance(raw, Mapping):
            return {str(key): str(value) for key, value in raw.items()}
    return {}


class ScenarioRunner:
    """Owns one shared-writer session and fans one shared snapshot out to scenarios."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], datetime] | object,
        fill_factory: _FillFactory | None = None,
        stop_assumptions: StopTriggerAssumptions | None = None,
        max_concurrency: int = 4,
    ) -> None:
        self._session = WriterSession(db_path)
        if self._session.environment is not AccountEnvironment.SHADOW:
            self._session.close()
            raise ModeIsolationError(
                "shadow scenarios refuse a "
                f"{self._session.environment.value} database",
                code="scenario_mode_mismatch",
            )
        if callable(clock):
            self._clock: Callable[[], datetime] | object = clock
        elif hasattr(clock, "now") and callable(getattr(clock, "now")):
            self._clock = clock
        else:
            self._session.close()
            raise TypeError("clock must be a callable or expose now()")
        if fill_factory is not None and not callable(fill_factory):
            self._session.close()
            raise TypeError("fill_factory must be callable or None")
        if stop_assumptions is not None and not isinstance(
            stop_assumptions, StopTriggerAssumptions
        ):
            self._session.close()
            raise TypeError("stop_assumptions must be StopTriggerAssumptions or None")
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency < 1
        ):
            self._session.close()
            raise ValueError("max_concurrency must be an int >= 1")
        self._fill_factory: _FillFactory = fill_factory or _default_fill_factory
        self._stop_assumptions = stop_assumptions or StopTriggerAssumptions()
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._runtimes: dict[str, _ScenarioRuntime] = {}
        self._buy_strategies: dict[str, _StrategyFn] = {}
        self._sell_strategies: dict[str, _StrategyFn] = {}

    # ---- setup

    def register_strategy(self, strategy_id: str, fn: _StrategyFn, *, side: str) -> None:
        """Assign a strategy plugin to the BUY or SELL side for scenario configs."""
        if not isinstance(strategy_id, str) or not strategy_id.strip():
            raise ValueError("strategy_id must be a non-empty string")
        if not callable(fn):
            raise TypeError("strategy plugin must be callable")
        normalized = side.strip().upper() if isinstance(side, str) else ""
        if normalized == "BUY":
            table = self._buy_strategies
        elif normalized == "SELL":
            table = self._sell_strategies
        else:
            raise ValueError("side must be BUY or SELL")
        key = strategy_id.strip()
        if key in table:
            raise ValueError(f"duplicate {normalized} strategy {key!r}")
        table[key] = fn

    def add_scenario(self, config: ScenarioConfig) -> AccountScope:
        """Create a scenario account once, or reload it without re-applying cash."""
        if not isinstance(config, ScenarioConfig):
            raise TypeError("config must be a ScenarioConfig")
        if config.scenario_id in self._runtimes:
            raise ValueError(f"duplicate scenario {config.scenario_id!r}")
        scope = config.scope
        ledger = ShadowLedger(
            self._session.bind_scope(scope.scope_id), scope, clock=self._clock
        )
        fills = self._fill_factory(config)
        if ledger.read_events():
            account = ShadowAccount.open(ledger)
        else:
            account = ShadowAccount.create(
                ledger,
                starting_cash=config.starting_cash,
                per_symbol_budget=config.per_symbol_budget,
                total_budget=config.total_budget,
                eligibility=config.eligibility_policy(),
                allow_fractional=config.allow_fractional,
                assumptions=_assumption_mapping(fills),
                feed=_FEED,
            )
        stops = ShadowStopSimulator(account, self._stop_assumptions)
        self._runtimes[config.scenario_id] = _ScenarioRuntime(
            config=config, ledger=ledger, account=account, fills=fills, stops=stops
        )
        return scope

    def reload_scenario(self, scenario_id: str) -> AccountScope:
        """Replay one scenario ledger into a fresh account; starting cash is preserved."""
        runtime = self._lookup(scenario_id)
        ledger = ShadowLedger(
            self._session.bind_scope(runtime.config.scope.scope_id),
            runtime.config.scope,
            clock=self._clock,
        )
        runtime.ledger = ledger
        runtime.account = ShadowAccount.open(ledger)
        runtime.stops = ShadowStopSimulator(runtime.account, self._stop_assumptions)
        return runtime.config.scope

    def get_account(self, scenario_id: str) -> ShadowAccount:
        return self._lookup(scenario_id).account

    @property
    def scenario_ids(self) -> tuple[str, ...]:
        return tuple(self._runtimes)

    def close(self) -> None:
        self._session.close()

    # ---- shared-feed fan-out

    async def run_once(
        self, symbols: Sequence[str], *, feed: _SharedFeed
    ) -> dict[str, list[str]]:
        """Invoke the shared feed once, then evaluate every scenario on a copy."""
        names = tuple(symbols)
        if not names:
            raise ValueError("symbols must be non-empty")
        shared = await feed.snapshot(names)
        if not isinstance(shared, Mapping):
            raise TypeError("feed snapshot must be a mapping of symbol to MarketContext")
        return await self.dispatch(shared)

    async def dispatch(
        self, shared: Mapping[str, MarketContext]
    ) -> dict[str, list[str]]:
        """Fan out one already-fetched immutable snapshot with bounded concurrency."""
        if not isinstance(shared, Mapping):
            raise TypeError("shared snapshot must be a mapping of symbol to MarketContext")
        if not self._runtimes:
            raise ValueError("no scenarios registered")
        # Snapshot values are frozen/immutable; each scenario gets its own
        # copied mapping so per-scenario handling can never affect the shared
        # feed data or another scenario's view of it.
        copies = {scenario_id: dict(shared) for scenario_id in self._runtimes}

        async def _guarded(scenario_id: str) -> tuple[str, list[str]]:
            async with self._semaphore:
                return scenario_id, await self._run_one(
                    self._runtimes[scenario_id], copies[scenario_id]
                )

        results = await asyncio.gather(
            *(_guarded(scenario_id) for scenario_id in self._runtimes)
        )
        return dict(results)

    async def projection(self, scenario_id: str) -> ScenarioProjection:
        """Read one scenario by id; labeled SHADOW/estimated with no aggregate view."""
        runtime = self._lookup(scenario_id)
        async with runtime.lock:
            snapshot = await runtime.account.get_account()
            positions = await runtime.account.list_positions()
        return ScenarioProjection(
            scenario_id=scenario_id,
            account_scope_id=snapshot.scope.scope_id,
            mode=AccountEnvironment.SHADOW.value,
            estimated=True,
            broker_confirmed=False,
            as_of=snapshot.as_of,
            cash=snapshot.cash,
            equity=snapshot.equity,
            buying_power=snapshot.buying_power,
            positions=tuple(positions),
        )

    # ---- internals

    def _lookup(self, scenario_id: str) -> _ScenarioRuntime:
        try:
            return self._runtimes[scenario_id]
        except KeyError:
            raise ValueError(f"unknown scenario {scenario_id!r}") from None

    def _now(self) -> datetime:
        clock = self._clock
        now = clock() if callable(clock) else clock.now()  # type: ignore[union-attr]
        if not isinstance(now, datetime):
            raise TypeError("clock must produce datetimes")
        return now

    def _resolve(self, table: Mapping[str, _StrategyFn], strategy_id: str, side: str) -> _StrategyFn:
        try:
            return table[strategy_id]
        except KeyError:
            raise ValueError(
                f"unknown {side} strategy {strategy_id!r}"
            ) from None

    async def _run_one(
        self, runtime: _ScenarioRuntime, snapshot: dict[str, MarketContext]
    ) -> list[str]:
        config = runtime.config
        account = runtime.account
        submitted: list[str] = []
        async with runtime.lock:
            for symbol, context in snapshot.items():
                if not isinstance(context, MarketContext):
                    raise TypeError(f"snapshot[{symbol!r}] must be a MarketContext")
                quote = context.latest_quote
                if quote is not None:
                    account.update_quote(symbol, (quote.bid + quote.ask) / 2)
                for side, table in (
                    ("BUY", self._buy_strategies),
                    ("SELL", self._sell_strategies),
                ):
                    for strategy_id in (
                        config.buy_strategies if side == "BUY" else config.sell_strategies
                    ):
                        plugin = self._resolve(table, strategy_id, side)
                        intent = plugin(config, context)
                        if intent is None:
                            continue
                        if not isinstance(intent, OrderIntent):
                            raise TypeError(
                                f"{side} strategy {strategy_id!r} must return "
                                "OrderIntent or None"
                            )
                        order = await account.submit_order(
                            intent, strategy_key=strategy_id
                        )
                        submitted.append(order.broker_order_id)
            open_orders = await account.list_open_orders()
            as_of = self._now()
            for order in open_orders:
                quotes = [
                    context.latest_quote
                    for context in snapshot.values()
                    if context.latest_quote is not None
                    and context.symbol == order.symbol
                ]
                if not quotes:
                    continue
                outcome = runtime.fills.apply_fill_to_account(
                    account, order.broker_order_id, quotes, as_of=as_of
                )
                if inspect.isawaitable(outcome):
                    await outcome
        return submitted
