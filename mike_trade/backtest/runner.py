"""Deterministic offline backtest orchestration and research metrics.

Run artifacts use immutable run-scoped JSON files because changing the existing
SQLite persistence schema would require a migration outside this task's scope.
All collaborators are injected; this module never constructs a broker client.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import HistoricalExecutionSimulator
from mike_trade.backtest.feed import HistoricalReplayFeed, ReplayEvent
from mike_trade.backtest.manifest import (
    HoldoutLock,
    HoldoutViolationError,
    PeriodRole,
    RunManifest,
    canonical_json,
    code_revision,
    config_hash,
    write_manifest,
)
from mike_trade.domain.market import Candle, Timeframe
from mike_trade.domain.protection import ProtectionKind, ProtectionPolicy
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import (
    TERMINAL_ORDER_STATUSES,
    ManagementStatus,
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    Position,
    TimeInForce,
)
from mike_trade.market_data.candles import (
    Candle as StrategyCandle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.risk.fixed_stop import ALPACA_EQUITY_TICK_RULES
from mike_trade.risk.stop_config import normalize_stop_spec, protection_policy_for_entry
from mike_trade.risk.trailing import (
    DistanceKind,
    SteppedTrailingPolicy,
    SteppedTrailingState,
    advance,
    initial_state,
)
from mike_trade.scheduling.calendar import NEW_YORK, entry_cutoff, session_for
from mike_trade.scheduling.scheduler import CandleClosedEvent, StrategyScheduler
from mike_trade.scheduling.triggers import StrategyTrigger, TriggerKind
from mike_trade.signals.decisions import (
    EntryCandidate,
    ExitCandidate,
    arbitrate_entry_decisions,
    arbitrate_exit_decisions,
)
from mike_trade.strategies.base import StrategyEvaluation
from mike_trade.strategies.registry import StrategyRegistry
from mike_trade.strategies.sell.base import ExitSource

__all__ = [
    "BacktestRequest",
    "BacktestResult",
    "BacktestRunner",
    "BacktestMetrics",
    "compute_metrics",
]

ZERO = Decimal("0")

#: Identifies the lifecycle semantics a run was produced under; part of the
#: configuration hash so runs from different lifecycle generations never alias.
#: "2": per-symbol BUY/SELL collections, one-position-per-symbol entry gating,
#: shared entry/exit arbitration, GTC post-fill protective stops (fixed, native
#: trailing, stepped trailing), exit-reason recording, 500-bar shared context.
LIFECYCLE_SEMANTICS_VERSION = "2"

#: Completed bars handed to every strategy; parity with the operational
#: ``CandleBuilder(max_history=500)`` behind the shared ``MarketContext``.
CONTEXT_HISTORY_BARS = 500


@dataclass(frozen=True, slots=True)
class BacktestRequest:
    symbols: tuple[str, ...]
    window_start: datetime
    window_end: datetime
    period_role: PeriodRole
    config: Mapping[str, Any]
    seed: int
    starting_capital: Decimal
    strategy_ids: tuple[str, ...]
    strategy_params: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    strategy_versions: Mapping[str, str] = field(default_factory=dict)
    initial_strategy_state: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    #: Optional per-symbol strategy collections (any number of BUY and SELL
    #: strategy ids per symbol, SELL optional); symbols not listed use
    #: ``strategy_ids``. BUY/SELL roles come from each registered strategy spec.
    symbol_strategies: Mapping[str, Sequence[str]] = field(default_factory=dict)
    #: Optional per-symbol, per-strategy parameter overrides on ``strategy_params``.
    symbol_strategy_params: Mapping[str, Mapping[str, Mapping[str, Any]]] = field(
        default_factory=dict
    )
    #: Completed bars in the shared context window. The default matches the
    #: operational ``CandleBuilder(max_history=500)``; a smaller window is faster
    #: but only faithful for strategies whose contract needs fewer bars (an
    #: opening-range strategy needs the whole session so far).
    context_history_bars: int = CONTEXT_HISTORY_BARS
    holdout_lock: HoldoutLock | None = None
    broker: object | None = None
    result_directory: str | Path = ".backtest-runs"

    def __post_init__(self) -> None:
        if not 1 <= len(self.symbols) <= 10:
            raise ValueError("backtest requires 1-10 operator-supplied symbols")
        if self.window_start.tzinfo is None or self.window_end.tzinfo is None:
            raise ValueError("backtest requires an explicit bounded aware window")
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        if not isinstance(self.starting_capital, Decimal) or self.starting_capital <= ZERO:
            raise ValueError("starting_capital must be positive Decimal")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed must be int")
        if (
            isinstance(self.context_history_bars, bool)
            or not isinstance(self.context_history_bars, int)
            or self.context_history_bars < 1
        ):
            raise ValueError("context_history_bars must be a positive int")


@dataclass(frozen=True, slots=True)
class BacktestResult:
    run_id: str
    manifest: RunManifest
    evaluations: tuple[Mapping[str, Any], ...]
    signals: tuple[Mapping[str, Any], ...]
    intents: tuple[Mapping[str, Any], ...]
    fills: tuple[Mapping[str, Any], ...]
    cycles: tuple[Mapping[str, Any], ...]
    equity_curve: tuple[Mapping[str, Any], ...]
    results_hash: str
    #: Entry/exit arbitration audit records (selected and rejected candidates).
    decisions: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    total_return: Decimal
    net_return: Decimal
    cash: Decimal
    equity_curve: tuple[Mapping[str, Any], ...]
    trades: tuple[Mapping[str, Any], ...]
    max_drawdown: Decimal
    profit_factor: Decimal | None
    win_rate: Decimal
    average_win: Decimal | None
    average_loss: Decimal | None
    expectancy_net: Decimal
    mfe_mae: tuple[Mapping[str, Any], ...]
    by_symbol: Mapping[str, Any]
    by_month: Mapping[str, Any]
    by_time_of_day: Mapping[str, Any]
    capital_utilization: Decimal
    sample_size: int
    expectancy_bootstrap_ci: tuple[Decimal, Decimal] | None
    warnings: tuple[str, ...]


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError("metric money, quantities and prices must be Decimal or decimal strings")


def _dt(value: Any) -> datetime:
    return (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )


def _trade_records(fills: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pair long entries/exits FIFO, preserving costs and timestamps."""
    queues: dict[str, list[dict[str, Any]]] = defaultdict(list)
    trades: list[dict[str, Any]] = []
    ordered = sorted(fills, key=lambda f: _dt(f["time"]))
    for fill in ordered:
        symbol = str(fill["symbol"]).upper()
        side = str(fill["side"]).upper()
        qty, price, fee = (
            _decimal(fill["quantity"]),
            _decimal(fill["price"]),
            _decimal(fill.get("fee", "0")),
        )
        at = _dt(fill["time"])
        if side in ("BUY", "B"):
            queues[symbol].append(
                {
                    "qty": qty,
                    "price": price,
                    "fee": fee,
                    "time": at,
                    "high": _decimal(fill.get("high", price)),
                    "low": _decimal(fill.get("low", price)),
                    "entry_time": at,
                    "entry_strategy": fill.get("strategy"),
                }
            )
            continue
        if side not in ("SELL", "S"):
            continue
        remaining = qty
        while remaining > ZERO and queues[symbol]:
            lot = queues[symbol][0]
            matched = min(remaining, lot["qty"])
            fraction = matched / qty
            entry_fee = (
                lot["fee"] * (matched / (matched + lot["qty"] - matched)) if lot["qty"] else ZERO
            )
            exit_fee = fee * fraction
            gross = (price - lot["price"]) * matched
            pnl = gross - entry_fee - exit_fee
            trades.append(
                {
                    "symbol": symbol,
                    "entry_time": lot["time"],
                    "exit_time": at,
                    "entry_price": lot["price"],
                    "exit_price": price,
                    "quantity": matched,
                    "gross_pnl": gross,
                    "costs": entry_fee + exit_fee,
                    "pnl": pnl,
                    "hold_duration_seconds": Decimal(str((at - lot["time"]).total_seconds())),
                    "mfe": (lot["high"] - lot["price"]) * matched,
                    "mae": (lot["low"] - lot["price"]) * matched,
                    "entry_strategy": lot.get("entry_strategy"),
                    "exit_reason": fill.get("exit_reason"),
                    "exit_strategy": fill.get("strategy"),
                }
            )
            lot["qty"] -= matched
            remaining -= matched
            if lot["qty"] == ZERO:
                queues[symbol].pop(0)
    return trades


def compute_metrics(
    results: BacktestResult | Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    starting_capital: Decimal,
    seed: int = 0,
    bootstrap_samples: int = 2000,
    small_sample_threshold: int = 30,
    bars: Sequence[Mapping[str, Any]] = (),
) -> BacktestMetrics:
    """Compute descriptive, cost-aware statistics from persisted fills/results."""
    if isinstance(results, BacktestResult):
        fills = results.fills
        curve = results.equity_curve
    elif isinstance(results, Mapping):
        fills = results.get("fills", ())
        curve = results.get("equity_curve", ())
    else:
        fills = results
        curve = ()
    capital = _decimal(starting_capital)
    if fills and all("pnl" in row for row in fills):
        trades = []
        for row in fills:
            item = dict(row)
            item["pnl"] = _decimal(item["pnl"])
            item["gross_pnl"] = _decimal(item.get("gross_pnl", item["pnl"]))
            item["costs"] = _decimal(item.get("costs", item["gross_pnl"] - item["pnl"]))
            item["symbol"] = str(item.get("symbol", "UNKNOWN")).upper()
            item["entry_time"] = _dt(item["entry_time"])
            item["exit_time"] = _dt(item["exit_time"])
            item["quantity"] = _decimal(item.get("quantity", "0"))
            item["entry_price"] = _decimal(item.get("entry_price", "0"))
            item.setdefault(
                "hold_duration_seconds",
                Decimal(str((item["exit_time"] - item["entry_time"]).total_seconds())),
            )
            item["mfe"] = _decimal(item.get("mfe", "0"))
            item["mae"] = _decimal(item.get("mae", "0"))
            trades.append(item)
    else:
        trades = _trade_records(fills)
    cumulative = capital
    generated_curve: list[dict[str, Any]] = []
    peak = capital
    max_dd = ZERO
    for trade in trades:
        cumulative += trade["pnl"]
        peak = max(peak, cumulative)
        if peak:
            max_dd = max(max_dd, (peak - cumulative) / peak)
        generated_curve.append(
            {"time": trade["exit_time"], "cash": cumulative, "equity": cumulative}
        )
    equity = tuple(curve) if curve else tuple(generated_curve)
    net_pnl = sum((t["pnl"] for t in trades), ZERO)
    gross_pnl = sum((t["gross_pnl"] for t in trades), ZERO)
    gross_wins = sum((t["pnl"] for t in trades if t["pnl"] > ZERO), ZERO)
    gross_losses = -sum((t["pnl"] for t in trades if t["pnl"] < ZERO), ZERO)
    wins = [t["pnl"] for t in trades if t["pnl"] > ZERO]
    losses = [t["pnl"] for t in trades if t["pnl"] < ZERO]
    n = len(trades)
    expectancy = net_pnl / n if n else ZERO
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_tod: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        local = t["entry_time"].astimezone(NEW_YORK)
        by_symbol[t["symbol"]].append(t)
        by_month[local.strftime("%Y-%m")].append(t)
        bucket = f"{local.hour:02d}:{(local.minute // 30) * 30:02d}"
        by_tod[bucket].append(t)
    summarize = lambda groups: {
        key: {
            "count": len(vals),
            "net_pnl": sum((x["pnl"] for x in vals), ZERO),
            "win_rate": Decimal(sum(x["pnl"] > ZERO for x in vals)) / len(vals),
        }
        for key, vals in sorted(groups.items())
    }
    sample_ci = None
    if n and bootstrap_samples > 0:
        rng = random.Random(seed)
        samples = sorted(
            sum((trades[rng.randrange(n)]["pnl"] for _ in range(n)), ZERO) / n
            for _ in range(bootstrap_samples)
        )
        sample_ci = (
            samples[int((bootstrap_samples - 1) * Decimal("0.025"))],
            samples[int((bootstrap_samples - 1) * Decimal("0.975"))],
        )
    warnings = (
        (f"small sample: {n} closed trades; threshold is {small_sample_threshold}",)
        if n < small_sample_threshold
        else ()
    )
    excursions: list[dict[str, Any]] = []
    for t in trades:
        holding = [
            b
            for b in bars
            if str(b["symbol"]).upper() == t["symbol"]
            and t["entry_time"] <= _dt(b["time"]) <= t["exit_time"]
        ]
        if holding:
            high = max(_decimal(b["high"]) for b in holding)
            low = min(_decimal(b["low"]) for b in holding)
            t["mfe"] = max(ZERO, high - t["entry_price"]) * t["quantity"]
            t["mae"] = min(ZERO, low - t["entry_price"]) * t["quantity"]
        excursions.append(
            {"symbol": t["symbol"], "entry_time": t["entry_time"], "mfe": t["mfe"], "mae": t["mae"]}
        )
    utilization = ZERO
    if fills:
        exposure: dict[str, Decimal] = defaultdict(lambda: ZERO)
        marks: dict[str, Decimal] = {}
        peak_exposure = ZERO
        for f in sorted(fills, key=lambda row: _dt(row["time"])):
            symbol = str(f["symbol"]).upper()
            qty, price = _decimal(f["quantity"]), _decimal(f["price"])
            marks[symbol] = price
            exposure[symbol] += qty if str(f["side"]).upper() in ("BUY", "B") else -qty
            peak_exposure = max(
                peak_exposure,
                sum((max(ZERO, q) * marks.get(s, ZERO) for s, q in exposure.items()), ZERO),
            )
        denom = max((_decimal(p.get("equity", capital)) for p in equity), default=capital)
        utilization = min(Decimal(1), peak_exposure / denom) if denom else ZERO
    final_cash = (
        cumulative
        if not equity
        else _decimal(equity[-1].get("cash", equity[-1].get("equity", cumulative)))
    )
    final_equity = cumulative if not equity else _decimal(equity[-1].get("equity", final_cash))
    return BacktestMetrics(
        total_return=gross_pnl / capital,
        net_return=net_pnl / capital,
        cash=final_cash,
        equity_curve=equity,
        trades=tuple(trades),
        max_drawdown=max_dd,
        profit_factor=None if gross_losses == ZERO else gross_wins / gross_losses,
        win_rate=Decimal(len(wins)) / n if n else ZERO,
        average_win=sum(wins, ZERO) / len(wins) if wins else None,
        average_loss=sum(losses, ZERO) / len(losses) if losses else None,
        expectancy_net=expectancy,
        mfe_mae=tuple(excursions),
        by_symbol=summarize(by_symbol),
        by_month=summarize(by_month),
        by_time_of_day=summarize(by_tod),
        capital_utilization=utilization,
        sample_size=n,
        expectancy_bootstrap_ci=sample_ci,
        warnings=warnings,
    )


@dataclass(frozen=True, slots=True)
class _SymbolPlan:
    """One symbol's independently configured strategy collections."""

    symbol: str
    buys: tuple[type, ...]
    sells: tuple[type, ...]
    params: Mapping[str, Mapping[str, Any]]

    @property
    def all(self) -> tuple[type, ...]:
        return self.buys + self.sells


@dataclass(slots=True)
class _Protection:
    """Backtest bookkeeping for one open cycle's protective stop."""

    order_id: str
    spec: Mapping[str, Any]
    policy: ProtectionPolicy
    entry_reference: Decimal
    current_stop: Decimal
    stepped: SteppedTrailingState | None = None


def _floor_stop(price: Decimal) -> Decimal:
    return ALPACA_EQUITY_TICK_RULES.floor(price)


def _stop_spec_from_config(config: Mapping[str, Any]) -> dict[str, Any] | None:
    """Resolve the protective-stop definition from the request configuration.

    ``protective_stop`` uses the same ``stop_policy`` shape as the operator
    configuration (``fixed`` / ``native_trailing`` / ``stepped_trailing``). The
    earlier ``protective_trailing_percent`` key remains a native-trailing alias.
    """
    explicit = config.get("protective_stop")
    legacy = config.get("protective_trailing_percent")
    if explicit is not None and legacy is not None:
        raise ValueError("configure protective_stop or protective_trailing_percent, not both")
    if explicit is not None:
        return normalize_stop_spec(explicit)
    if legacy is not None:
        return normalize_stop_spec({"type": "native_trailing", "trail_percent": legacy})
    return None


class BacktestRunner:
    """Run registered strategies over a supplied replay feed and simulator.

    Lifecycle semantics deliberately mirror the SHADOW engine's
    (``app.VwapReferenceLifecycle``) by calling the same pure policy:

    * every symbol has its own BUY and SELL strategy collections
      (``BacktestRequest.symbol_strategies``), each strategy with independent
      parameters and persistent state keyed by ``(symbol, strategy_id)``;
    * all strategies of a symbol share one authoritative completed-bar context;
    * one position per symbol: BUY strategies are evaluated only while the
      symbol is flat with no entry order working, and
      ``signals.decisions.arbitrate_entry_decisions`` selects at most one;
    * SELL strategies are evaluated only while a position is held and
      ``arbitrate_exit_decisions`` selects at most one full-remainder exit,
      applying the minimum hold to strategy exits only and refusing a second
      exit while one is working;
    * the protective stop (fixed / native trailing / stepped trailing) is armed
      after the confirmed entry fill, is independent of any SELL strategy, and
      is cancelled only to make way for a discretionary exit;
    * every fill records its purpose, originating strategy and exit reason.
    """

    def __init__(
        self,
        registry: StrategyRegistry,
        feed: HistoricalReplayFeed,
        clock: ReplayClock,
        execution: HistoricalExecutionSimulator,
        *,
        scheduler: StrategyScheduler | None = None,
        intent_factory: Callable[..., Any] | None = None,
        budget_policy: Any = None,
        eligibility_policy: Any = None,
        position_coordinator: Any = None,
        cycle_accounting: Any = None,
        protection_math: Any = None,
    ) -> None:
        self.registry, self.feed, self.clock, self.execution = registry, feed, clock, execution
        self.scheduler = scheduler or StrategyScheduler(clock)
        self.intent_factory = intent_factory
        self.policies = (
            budget_policy,
            eligibility_policy,
            position_coordinator,
            cycle_accounting,
            protection_math,
        )

    @staticmethod
    def _reject_real_broker(broker: object | None, execution: object) -> None:
        for candidate in (broker, execution):
            if candidate is None:
                continue
            module = type(candidate).__module__.lower()
            name = type(candidate).__name__.lower()
            environment = str(getattr(candidate, "environment", "")).lower()
            if "alpaca" in module or "alpaca" in name or environment in {"paper", "live"}:
                raise ValueError("real Alpaca/paper/live broker clients are refused in backtests")

    def _plan(self, request: BacktestRequest) -> dict[str, _SymbolPlan]:
        """Resolve each symbol's BUY/SELL collections and validated parameters."""
        symbols = {s.upper() for s in request.symbols}
        for label, mapping in (
            ("symbol_strategies", request.symbol_strategies),
            ("symbol_strategy_params", request.symbol_strategy_params),
        ):
            stray = sorted({str(k).upper() for k in mapping} - symbols)
            if stray:
                raise ValueError(f"{label} names symbols outside the request: {stray}")
        plans: dict[str, _SymbolPlan] = {}
        for symbol in request.symbols:
            ids = tuple(request.symbol_strategies.get(symbol, request.strategy_ids))
            if len(set(ids)) != len(ids):
                raise ValueError(
                    f"{symbol}: a strategy may appear only once per symbol; got {list(ids)}"
                )
            classes = [
                self.registry.get(sid, request.strategy_versions.get(sid)) for sid in ids
            ]
            overrides = request.symbol_strategy_params.get(symbol, {})
            stray_params = sorted(set(overrides) - set(ids))
            if stray_params:
                raise ValueError(
                    f"{symbol}: parameters supplied for unconfigured strategies {stray_params}"
                )
            params = {}
            for cls in classes:
                sid = cls.spec.strategy_id
                merged = {**request.strategy_params.get(sid, {}), **overrides.get(sid, {})}
                params[sid] = self.registry.validate_parameters(
                    sid, merged, request.strategy_versions.get(sid)
                )
            ordered = sorted(classes, key=lambda c: c.spec.strategy_id)
            plans[symbol] = _SymbolPlan(
                symbol,
                tuple(c for c in ordered if c.spec.side is StrategyRole.BUY),
                tuple(c for c in ordered if c.spec.side is StrategyRole.SELL),
                params,
            )
        return plans

    async def run(self, request: BacktestRequest) -> BacktestResult:
        self._reject_real_broker(request.broker, self.execution)
        if self.feed.seed != request.seed:
            raise ValueError("request seed must match the injected replay feed seed")
        if (
            getattr(self.execution, "_feed", self.feed) is not self.feed
            or getattr(self.execution, "_clock", self.clock) is not self.clock
        ):
            raise ValueError("execution simulator must share the injected replay feed and clock")

        def inspect_broker_config(value: Any, key: str = "") -> None:
            if isinstance(value, Mapping):
                for child_key, child_value in value.items():
                    inspect_broker_config(child_value, str(child_key).lower())
            elif key in {"broker", "broker_type", "provider", "environment", "mode"}:
                if isinstance(value, str) and value.strip().lower() in {
                    "alpaca",
                    "alpaca_paper",
                    "paper",
                    "live",
                    "alpaca_live",
                }:
                    raise ValueError("configuration resolves to a real Alpaca/paper/live broker")
                if not isinstance(value, str):
                    self._reject_real_broker(value, None)

        inspect_broker_config(request.config)
        role = PeriodRole(request.period_role)
        provenance = self.feed.provenance
        plans = self._plan(request)
        stop_spec = _stop_spec_from_config(request.config)
        used: dict[tuple[str, str], type] = {}
        for plan in plans.values():
            for cls in plan.all:
                used.setdefault((cls.spec.strategy_id, cls.spec.version), cls)
        strategies = list(used.values())
        for symbol, plan in plans.items():
            self.scheduler.watch(symbol)
            for cls in plan.all:
                self.scheduler.register(
                    symbol=symbol,
                    strategy_id=cls.spec.strategy_id,
                    side=cls.spec.side,
                    triggers=(
                        StrategyTrigger(
                            TriggerKind.CANDLE_CLOSED, timeframe=cls.spec.trigger_timeframe
                        ),
                    ),
                )
        effective = dict(request.config)
        assumptions = self.execution.assumptions
        amap = dict(assumptions.as_mapping())
        effective.update(
            {
                "seed": request.seed,
                "strategy_ids": list(request.strategy_ids),
                "strategy_versions": {c.spec.strategy_id: c.spec.version for c in strategies},
                "strategy_params": request.strategy_params,
                "initial_strategy_state": request.initial_strategy_state,
                "execution_assumptions": amap,
                "starting_capital": request.starting_capital,
                "symbols": sorted(request.symbols),
                "window_start": request.window_start,
                "window_end": request.window_end,
                "lifecycle_semantics": LIFECYCLE_SEMANTICS_VERSION,
                "context_history_bars": request.context_history_bars,
                "strategy_collections": {
                    s: {
                        "buy": [c.spec.strategy_id for c in p.buys],
                        "sell": [c.spec.strategy_id for c in p.sells],
                        "params": p.params,
                    }
                    for s, p in sorted(plans.items())
                },
            }
        )
        digest = config_hash(effective)
        if role is PeriodRole.HOLDOUT:
            lock = request.holdout_lock
            if lock is None:
                raise HoldoutViolationError("HOLDOUT requires an explicit holdout lock token")
            if lock.research_config_hash != digest:
                raise HoldoutViolationError(
                    "holdout config differs from locked research configuration"
                )
            if (
                request.window_start.astimezone(UTC) < lock.window_start
                or request.window_end.astimezone(UTC) > lock.window_end
            ):
                raise HoldoutViolationError("HOLDOUT run must stay within locked holdout window")
            access = {"token": lock.token, "accessed_at": self.clock.now()}
        else:
            access = None
            lock = request.holdout_lock
            if (
                lock
                and request.window_start.astimezone(UTC) < lock.window_end
                and request.window_end.astimezone(UTC) > lock.window_start
            ):
                raise HoldoutViolationError("research/dev window overlaps locked holdout window")
        dataset_hash = provenance.dataset_hash or ""
        run_id = hashlib.sha256(
            canonical_json(
                {
                    "config_hash": digest,
                    "dataset_hash": dataset_hash,
                    "symbols": sorted(request.symbols),
                    "start": request.window_start,
                    "end": request.window_end,
                    "role": role.value,
                }
            ).encode()
        ).hexdigest()
        manifest = RunManifest(
            run_id,
            request.window_start,
            digest,
            tuple(request.symbols),
            request.window_start,
            request.window_end,
            role,
            code_revision(),
            {
                "source": provenance.source,
                "feed": provenance.feed,
                "is_consolidated": provenance.is_consolidated,
                "adjustment": provenance.adjustment,
                "point_in_time": provenance.point_in_time,
            },
            provenance.revision,
            provenance.revision,
            dataset_hash,
            str(request.starting_capital),
            {k: v for k, v in amap.items() if k.startswith("fee_")},
            {k: v for k, v in amap.items() if "slippage" in k},
            {k: v for k, v in amap.items() if "fill" in k},
            {k: v for k, v in amap.items() if "ambiguity" in k},
            request.seed,
            tuple({"name": c.spec.strategy_id, "version": c.spec.version} for c in strategies),
            access,
        )
        root = Path(request.result_directory)
        scope_id = str(request.config.get("research_scope_id", "backtest:research"))
        minimum_hold = timedelta(seconds=float(request.config.get("minimum_hold_seconds") or 0))
        evaluations: list[Mapping[str, Any]] = []
        signals: list[Mapping[str, Any]] = []
        intents: list[Mapping[str, Any]] = []
        decisions: list[Mapping[str, Any]] = []
        fills: list[Mapping[str, Any]] = []
        cycles: list[dict[str, Any]] = []
        equity_curve: list[dict[str, Any]] = []
        cash = request.starting_capital
        quantities: dict[str, Decimal] = defaultdict(lambda: ZERO)
        costs: dict[str, Decimal] = defaultdict(lambda: ZERO)
        marks: dict[str, Decimal] = {}
        open_cycles: dict[str, dict[str, Any]] = {}
        order_meta: dict[str, dict[str, Any]] = {}
        pending_entry: dict[str, str] = {}
        pending_exit: dict[str, str] = {}
        protections: dict[str, _Protection] = {}
        protection_count = 0
        candles_by_symbol: dict[str, list[Candle]] = defaultdict(list)
        window: dict[str, deque[StrategyCandle]] = defaultdict(
            lambda: deque(maxlen=request.context_history_bars)
        )
        replay_events = [
            e
            for e in self.feed.events(symbols=request.symbols)
            if e.available_at and request.window_start <= e.available_at <= request.window_end
        ]
        trigger_rows: set[tuple[str, str, datetime]] = set()
        state: dict[tuple[str, str], dict[str, Any]] = defaultdict(dict)
        for symbol in request.symbols:
            for strategy_id, initial in request.initial_strategy_state.items():
                state[(symbol, strategy_id)].update(dict(initial))
        vwap_session: dict[str, tuple[str, Decimal, Decimal, Decimal | None]] = {}
        entries_by_session: dict[tuple[str, str], int] = defaultdict(int)
        timeframe_checked = False

        async def order_is_open(order_id: str) -> bool:
            order = await self.execution.get_order(order_id)
            return order is not None and order.status not in TERMINAL_ORDER_STATUSES

        async def arm_protection(symbol: str, reference: Decimal) -> None:
            """Cover the held quantity with the configured stop (post-fill, GTC)."""
            nonlocal protection_count
            if stop_spec is None or quantities[symbol] <= ZERO:
                return
            cycle_id = str(open_cycles[symbol]["cycle_id"])
            current = protections.get(symbol)
            if current is not None and await order_is_open(current.order_id):
                if (await self.execution.get_order(current.order_id)).quantity != quantities[
                    symbol
                ]:
                    await self.execution.replace_order(
                        current.order_id, quantity=quantities[symbol]
                    )
                return
            entry_reference = current.entry_reference if current is not None else reference
            policy = protection_policy_for_entry(stop_spec, entry_reference)
            floor_stop = current.current_stop if current is not None else ZERO
            stepped = current.stepped if current is not None else None
            protection_count += 1
            common = dict(
                intent_id=f"{run_id}:{symbol}:protection:{protection_count}",
                client_order_id=f"{run_id[:20]}-protect-{symbol}-{protection_count}",
                account_scope_id=scope_id,
                cycle_id=cycle_id,
                symbol=symbol,
                side=OrderSide.SELL,
                purpose=OrderPurpose.PROTECTIVE_STOP,
                time_in_force=TimeInForce.GTC,
                created_at=self.clock.now(),
                quantity=quantities[symbol],
            )
            if policy.kind is ProtectionKind.BROKER_TRAILING_STOP:
                intent = OrderIntent(
                    order_type=OrderType.TRAILING_STOP,
                    trail_percent=policy.trail_percent,
                    trail_price=policy.trail_amount,
                    **common,
                )
                stop_now = ZERO
            else:
                if policy.kind is ProtectionKind.FIXED_STOP:
                    offset = (
                        policy.stop_offset_amount
                        if policy.stop_offset_amount is not None
                        else entry_reference * policy.stop_offset_percent / Decimal(100)
                    )
                else:
                    stepped = stepped or initial_state(
                        entry_reference,
                        SteppedTrailingPolicy(
                            DistanceKind.PERCENT,
                            policy.trail_percent,
                            DistanceKind.USD,
                            policy.step_size,
                            timedelta(0),
                            TimeInForce.GTC,
                            "backtest",
                        ),
                    )
                    offset = entry_reference - stepped.candidate_stop
                stop_now = max(_floor_stop(entry_reference - offset), floor_stop)
                if not ZERO < stop_now < entry_reference:
                    raise ValueError(
                        f"{symbol}: protective stop distance leaves no valid stop "
                        f"below the {entry_reference} entry reference"
                    )
                intent = OrderIntent(
                    order_type=OrderType.STOP, stop_price=stop_now, **common
                )
            order = await self.execution.submit_order(intent)
            order_meta[order.broker_order_id] = {
                "purpose": OrderPurpose.PROTECTIVE_STOP.value,
                "strategy": None,
                "protection_mode": str(spec_type(stop_spec)),
            }
            protections[symbol] = _Protection(
                order.broker_order_id, stop_spec, policy, entry_reference, stop_now, stepped
            )

        async def ratchet_stepped(symbol: str, high: Decimal) -> None:
            """Engine-managed stepped trailing: raise (never lower) the stop."""
            protection = protections.get(symbol)
            if (
                protection is None
                or protection.stepped is None
                or protection.policy.kind is not ProtectionKind.STEPPED_TRAILING_STOP
                or not await order_is_open(protection.order_id)
            ):
                return
            policy = protection.policy
            if high < protection.entry_reference + (policy.activation_gain or ZERO):
                return
            moved = advance(
                protection.stepped,
                high,
                SteppedTrailingPolicy(
                    DistanceKind.PERCENT,
                    policy.trail_percent,
                    DistanceKind.USD,
                    policy.step_size,
                    timedelta(0),
                    TimeInForce.GTC,
                    "backtest",
                ),
            )
            protection.stepped = moved
            candidate = _floor_stop(moved.candidate_stop)
            if candidate > protection.current_stop:
                await self.execution.replace_order(protection.order_id, stop_price=candidate)
                protection.current_stop = candidate

        def spec_type(spec: Mapping[str, Any]) -> str:
            return {
                "fixed": "FIXED",
                "native_trailing": "TRAILING_NATIVE",
                "stepped_trailing": "TRAILING_STEPPED",
            }[spec["type"]]

        async def submit(
            evaluation: StrategyEvaluation, context: MarketContext, symbol: str
        ) -> None:
            assert self.intent_factory is not None
            intent = self.intent_factory(evaluation, context, request)
            if intent.side is OrderSide.SELL:
                # Make way for the discretionary exit exactly as the engine does:
                # the stop is cancelled first and re-armed if the exit never fills.
                protection = protections.get(symbol)
                if protection is not None and await order_is_open(protection.order_id):
                    await self.execution.cancel_order(protection.order_id)
            intents.append(
                {
                    "symbol": symbol,
                    "strategy": evaluation.strategy_id,
                    "side": intent.side.value,
                    "purpose": intent.purpose.value,
                    "time": context.as_of,
                    "intent_id": getattr(intent, "intent_id", None),
                }
            )
            order = await self.execution.submit_order(intent)
            order_meta[order.broker_order_id] = {
                "purpose": intent.purpose.value,
                "strategy": evaluation.strategy_id,
                "signal_id": evaluation.signal_id,
            }
            if intent.side is OrderSide.BUY:
                pending_entry[symbol] = order.broker_order_id
            else:
                pending_exit[symbol] = order.broker_order_id

        for ev in replay_events:
            assert ev.available_at
            self.clock.advance_to(ev.available_at)
            outputs = self.execution.process_event(ev)
            for x in outputs:
                meta = order_meta.get(x.fill.broker_order_id, {})
                is_buy = x.side.value == "BUY"
                fill_row = {
                    "symbol": x.symbol,
                    "side": x.side.value,
                    "quantity": x.quantity,
                    "price": x.price,
                    "fee": x.fee,
                    "time": x.fill.filled_at,
                    "order_id": x.fill.broker_order_id,
                    "evidence": x.evidence,
                    "purpose": x.purpose.value,
                    "strategy": meta.get("strategy"),
                    "exit_reason": None
                    if is_buy
                    else (
                        ExitSource.PROTECTIVE_STOP.value
                        if x.purpose is OrderPurpose.PROTECTIVE_STOP
                        else ExitSource.STRATEGY_SELL.value
                    ),
                }
                fills.append(fill_row)
                cash += (
                    -(x.quantity * x.price + x.fee)
                    if is_buy
                    else x.quantity * x.price - x.fee
                )
                marks[x.symbol] = x.price
                if is_buy:
                    quantities[x.symbol] += x.quantity
                    costs[x.symbol] += x.quantity * x.price
                    if x.symbol not in open_cycles:
                        session_key = x.fill.filled_at.astimezone(NEW_YORK).date().isoformat()
                        entries_by_session[(x.symbol, session_key)] += 1
                        open_cycles[x.symbol] = {
                            "cycle_id": f"{run_id}:{x.symbol}:{len(cycles) + 1}",
                            "symbol": x.symbol,
                            "opened_at": x.fill.filled_at,
                            "closed_at": None,
                            "entry_quantity": ZERO,
                            "exit_quantity": ZERO,
                            "entry_strategy": meta.get("strategy"),
                            "exit_reason": None,
                            "exit_strategy": None,
                        }
                    open_cycles[x.symbol]["entry_quantity"] += x.quantity
                    await arm_protection(x.symbol, x.price)
                else:
                    average = costs[x.symbol] / quantities[x.symbol]
                    costs[x.symbol] -= average * x.quantity
                    quantities[x.symbol] -= x.quantity
                    if x.symbol in open_cycles:
                        cycle = open_cycles[x.symbol]
                        cycle["exit_quantity"] += x.quantity
                        cycle["exit_reason"] = fill_row["exit_reason"]
                        cycle["exit_strategy"] = meta.get("strategy")
                        if quantities[x.symbol] <= ZERO:
                            costs[x.symbol] = ZERO
                            protections.pop(x.symbol, None)
                            pending_exit.pop(x.symbol, None)
                            cycle = open_cycles.pop(x.symbol)
                            cycle["closed_at"] = x.fill.filled_at
                            cycles.append(cycle)
                equity_curve.append(
                    {
                        "time": x.fill.filled_at,
                        "cash": cash,
                        "equity": cash
                        + sum(
                            (qty * marks.get(sym, ZERO) for sym, qty in quantities.items()), ZERO
                        ),
                    }
                )
            if ev.kind != ReplayEvent.KIND_BAR or ev.candle is None:
                continue
            symbol = ev.symbol
            plan = plans.get(symbol)
            if plan is None:
                continue
            if not timeframe_checked:
                timeframe_checked = True
                for cls in {c for p in plans.values() for c in p.all}:
                    if cls.spec.trigger_timeframe.value != ev.candle.timeframe.value or any(
                        tf.value != ev.candle.timeframe.value
                        for tf in cls.spec.required_timeframes
                    ):
                        raise ValueError(
                            f"strategy {cls.spec.strategy_id} needs "
                            f"{cls.spec.trigger_timeframe.value} bars but the replay feed "
                            f"supplies {ev.candle.timeframe.value}; derived-timeframe "
                            "aggregation is not part of the historical replay"
                        )
            marks[symbol] = ev.candle.close
            equity_curve.append(
                {
                    "time": ev.available_at,
                    "cash": cash,
                    "equity": cash
                    + sum((qty * marks.get(sym, ZERO) for sym, qty in quantities.items()), ZERO),
                }
            )
            candles_by_symbol[symbol].append(ev.candle)
            candle = ev.candle
            window[symbol].append(
                StrategyCandle(
                    candle.symbol,
                    CandleTimeframe(candle.timeframe.value),
                    candle.start,
                    candle.end,
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume,
                    candle.provenance.feed,
                    candle.provenance.is_consolidated,
                    CandleOrigin.REPLAY,
                    PriceBasis.RAW,
                    CandleStatus.CLOSED,
                    candle.trade_count,
                )
            )
            session_date = candle.start.astimezone(NEW_YORK).date().isoformat()
            previous_session = vwap_session.get(symbol)
            if previous_session is None or previous_session[0] != session_date:
                weighted, volume, prior_vwap = ZERO, ZERO, None
            else:
                _, weighted, volume, prior_vwap = previous_session
            if candle.volume > ZERO:
                typical = (candle.high + candle.low + candle.close) / Decimal(3)
                weighted += typical * candle.volume
                volume += candle.volume
            latest_vwap = weighted / volume if volume > ZERO else None
            vwap_session[symbol] = (session_date, weighted, volume, latest_vwap)

            # Settle bookkeeping for orders that finished while this bar replayed.
            if symbol in pending_entry and not await order_is_open(pending_entry[symbol]):
                del pending_entry[symbol]
            if symbol in pending_exit and not await order_is_open(pending_exit[symbol]):
                del pending_exit[symbol]
                if quantities[symbol] > ZERO:
                    await arm_protection(symbol, marks[symbol])  # exit lapsed: re-protect
            await ratchet_stepped(symbol, candle.high)

            due = self.scheduler.process(
                CandleClosedEvent(
                    symbol,
                    CandleTimeframe(candle.timeframe.value),
                    candle.end,
                    snapshot_version=len(candles_by_symbol[symbol]),
                    due_at=ev.available_at,
                )
            )
            due_ids = {item.strategy_id for item in due if item.symbol == symbol}
            session_status = self.clock.session_status()
            age = ev.available_at - candle.end
            freshness = ContextFreshness(
                candle.end, max(age, timedelta(0)), timedelta(minutes=5), False, None
            )
            # One immutable context shared by every strategy of this symbol.
            context = MarketContext(
                symbol,
                ev.available_at,
                len(candles_by_symbol[symbol]),
                provenance.feed,
                provenance.is_consolidated,
                PriceBasis.RAW,
                session_status,
                ev.available_at.astimezone(NEW_YORK).date().isoformat(),
                freshness,
                {CandleTimeframe(candle.timeframe.value): tuple(window[symbol])},
                {"vwap": latest_vwap, "vwap_prev": prior_vwap},
            )

            def evaluate(cls: type) -> StrategyEvaluation | None:
                spec = cls.spec
                if spec.strategy_id not in due_ids:
                    return None
                if len(candles_by_symbol[symbol]) < max(1, spec.warmup_bars):
                    return None
                key = (symbol, spec.strategy_id, candle.start)
                if key in trigger_rows:
                    return None
                trigger_rows.add(key)
                evaluation: StrategyEvaluation = cls().evaluate(
                    context, plan.params[spec.strategy_id], state[(symbol, spec.strategy_id)]
                )
                state[(symbol, spec.strategy_id)].update(evaluation.state_updates)
                row = {
                    "symbol": symbol,
                    "strategy": spec.strategy_id,
                    "side": spec.side.value,
                    "version": spec.version,
                    "time": ev.available_at,
                    "bar_time": candle.start,
                    "signal_id": evaluation.signal_id,
                    "reason_codes": [x.value for x in evaluation.reason_codes],
                }
                evaluations.append(row)
                if evaluation.is_signal:
                    signals.append(row)
                    return evaluation
                return None

            if quantities[symbol] > ZERO:
                # SELL side: every configured SELL strategy sees the held position.
                winners = {
                    e.signal_id: (cls, e)
                    for cls in plan.sells
                    if (e := evaluate(cls)) is not None
                }
                if winners and self.intent_factory is not None:
                    cycle = open_cycles[symbol]
                    average = costs[symbol] / quantities[symbol]
                    position = Position(
                        scope_id,
                        symbol,
                        quantities[symbol],
                        average,
                        costs[symbol],
                        ev.available_at,
                        management=ManagementStatus.MANAGED,
                        cycle_id=str(cycle["cycle_id"]),
                    )
                    result = arbitrate_exit_decisions(
                        [
                            ExitCandidate(
                                signal_id=sid,
                                source=ExitSource.STRATEGY_SELL,
                                symbol=symbol,
                                strategy_id=cls.spec.strategy_id,
                                version=cls.spec.version,
                                position_cycle_id=str(cycle["cycle_id"]),
                                bar_time=e.bar_time,
                                requested_at=ev.available_at,
                            )
                            for sid, (cls, e) in winners.items()
                        ],
                        symbol=symbol,
                        position=position,
                        evaluation_time=ev.available_at,
                        minimum_hold=minimum_hold,
                        entry_filled_at=cycle["opened_at"],
                        pending_exit_source=(
                            ExitSource.STRATEGY_SELL if symbol in pending_exit else None
                        ),
                    )
                    decisions.append(
                        {"kind": "EXIT", "time": ev.available_at, **result.to_dict()}
                    )
                    if result.selected is not None:
                        await submit(winners[result.selected.signal_id][1], context, symbol)
            else:
                # BUY side: only a flat symbol with no entry working is eligible.
                candidates = {}
                for cls in plan.buys:
                    state[(symbol, cls.spec.strategy_id)]["eligibility"] = "WATCHING_BUY"
                    if (e := evaluate(cls)) is not None:
                        candidates[e.signal_id] = (cls, e)
                if candidates:
                    result = arbitrate_entry_decisions(
                        [
                            EntryCandidate(
                                sid, cls.spec.strategy_id, cls.spec.version, symbol, e.bar_time
                            )
                            for sid, (cls, e) in candidates.items()
                        ],
                        symbol=symbol,
                        evaluation_time=ev.available_at,
                        position_open=quantities[symbol] > ZERO,
                        entry_pending=symbol in pending_entry,
                    )
                    gate: str | None = None
                    if result.selected is not None:
                        cutoff_minutes = request.config.get("entry_cutoff_minutes")
                        max_cycles = request.config.get("max_entry_cycles_per_session")
                        session_key = ev.available_at.astimezone(NEW_YORK).date().isoformat()
                        if cutoff_minutes is not None and ev.available_at >= entry_cutoff(
                            session_for(ev.available_at.astimezone(NEW_YORK).date()),
                            int(cutoff_minutes),
                        ):
                            gate = "ENTRY_CUTOFF_REACHED"
                        elif max_cycles is not None and entries_by_session[
                            (symbol, session_key)
                        ] >= int(max_cycles):
                            gate = "MAX_ENTRY_CYCLES_REACHED"
                    decisions.append(
                        {
                            "kind": "ENTRY",
                            "time": ev.available_at,
                            "entry_gate": gate,
                            **result.to_dict(),
                        }
                    )
                    if (
                        result.selected is not None
                        and gate is None
                        and self.intent_factory is not None
                    ):
                        await submit(candidates[result.selected.signal_id][1], context, symbol)
        payload = {
            "evaluations": evaluations,
            "signals": signals,
            "intents": intents,
            "decisions": decisions,
            "fills": fills,
            "cycles": cycles + list(open_cycles.values()),
            "equity_curve": equity_curve,
        }
        result_hash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        result_path = root / f"{run_id}.results.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path = root / f"{run_id}.manifest.json"
        # Workers may finish duplicate job specs concurrently. Coordinate the
        # small immutable-artifact commit section; simulations remain parallel.
        import fcntl

        with (root / ".artifact-write.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if result_path.exists() or manifest_path.exists():
                    # Content-addressed retries reuse an identical completed result;
                    # a differing ledger or incomplete prior write remains a hard error.
                    if not result_path.is_file() or not manifest_path.is_file():
                        raise FileExistsError(
                            f"incomplete immutable backtest artifact for {run_id}"
                        )
                    prior_payload = json.loads(result_path.read_text(encoding="utf-8"))
                    prior_hash = hashlib.sha256(canonical_json(prior_payload).encode()).hexdigest()
                    prior_manifest = RunManifest.from_json(
                        manifest_path.read_text(encoding="utf-8")
                    )
                    if (
                        prior_hash != result_hash
                        or prior_manifest.content_hash != manifest.content_hash
                    ):
                        raise FileExistsError(f"immutable backtest artifact differs for {run_id}")
                else:
                    with result_path.open("x", encoding="utf-8", newline="\n") as f:
                        f.write(canonical_json(payload))
                        f.flush()
                    # Publish the manifest only after the immutable result ledger is complete.
                    write_manifest(manifest, root)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return BacktestResult(
            run_id,
            manifest,
            tuple(evaluations),
            tuple(signals),
            tuple(intents),
            tuple(fills),
            tuple(payload["cycles"]),
            tuple(equity_curve),
            result_hash,
            tuple(decisions),
        )
