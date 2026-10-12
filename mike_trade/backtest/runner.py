"""Deterministic offline backtest orchestration and research metrics.

Run artifacts use immutable run-scoped JSON files because changing the existing
SQLite persistence schema would require a migration outside this task's scope.
All collaborators are injected; this module never constructs a broker client.
"""

from __future__ import annotations

import hashlib
import json
import random
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
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
    PositionDirection,
    TimeInForce,
)
from mike_trade.market_data.candles import (
    Candle as StrategyCandle,
)
from mike_trade.market_data.candles import (
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.market_data.session_candles import SessionCandleSource
from mike_trade.market_data.session_reference import SessionReferenceTracker
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
from mike_trade.strategies.indicators.core import rsi as calculate_rsi
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
#: Runs whose strategy collections include SHORT or COVER strategies are produced under
#: bidirectional lifecycle semantics ("3": adds the SHORT/COVER lifecycle, restricted short
#: collateral, direction-aware stops and borrow-cost accounting).  Long-only runs keep "2" so
#: their configuration hashes and artifacts are unchanged.
BIDIRECTIONAL_LIFECYCLE_SEMANTICS_VERSION = "3"
#: Regulation T style initial requirement: proceeds stay locked AND half the notional in
#: own cash is required (150% of the short's notional).
DEFAULT_SHORT_MARGIN_MULTIPLIER = Decimal("1.5")
_BPS = Decimal("10000")
_ENTRY_FILL_SIDES = frozenset({"BUY", "B", "SELL_SHORT"})

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
    #: Meaningful protection state changes only; no per-bar logging.
    protection_events: tuple[Mapping[str, Any], ...] = ()


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
    """Pair entries with the opposite-side exits FIFO, preserving costs and timestamps.

    ``BUY`` opens a long closed by ``SELL``; ``SELL_SHORT`` opens a short closed by
    ``BUY_TO_COVER``.  A short's gross P&L is ``(entry - exit) x quantity`` and any estimated
    borrow cost recorded on the cover fill is charged to the trade.
    """
    queues: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
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
        if side in ("BUY", "B", "SELL_SHORT"):
            direction = "SHORT" if side == "SELL_SHORT" else "LONG"
            queues[(symbol, direction)].append(
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
        if side not in ("SELL", "S", "BUY_TO_COVER"):
            continue
        direction = "SHORT" if side == "BUY_TO_COVER" else "LONG"
        queue = queues[(symbol, direction)]
        borrow = _decimal(fill.get("borrow_cost", "0"))
        remaining = qty
        while remaining > ZERO and queue:
            lot = queue[0]
            matched = min(remaining, lot["qty"])
            fraction = matched / qty
            entry_fee = (
                lot["fee"] * (matched / (matched + lot["qty"] - matched)) if lot["qty"] else ZERO
            )
            exit_fee = fee * fraction
            exit_borrow = borrow * fraction
            if direction == "LONG":
                gross = (price - lot["price"]) * matched
                mfe = (lot["high"] - lot["price"]) * matched
                mae = (lot["low"] - lot["price"]) * matched
            else:  # a short gains when the price falls
                gross = (lot["price"] - price) * matched
                mfe = (lot["price"] - lot["low"]) * matched
                mae = (lot["price"] - lot["high"]) * matched
            pnl = gross - entry_fee - exit_fee - exit_borrow
            trade = {
                "symbol": symbol,
                "entry_time": lot["time"],
                "exit_time": at,
                "entry_price": lot["price"],
                "exit_price": price,
                "quantity": matched,
                "gross_pnl": gross,
                "costs": entry_fee + exit_fee + exit_borrow,
                "pnl": pnl,
                "hold_duration_seconds": Decimal(str((at - lot["time"]).total_seconds())),
                "mfe": mfe,
                "mae": mae,
                "entry_strategy": lot.get("entry_strategy"),
                "exit_reason": fill.get("exit_reason"),
                "exit_strategy": fill.get("strategy"),
            }
            if direction == "SHORT":
                trade["direction"] = "SHORT"
                trade["borrow_cost"] = exit_borrow
            trades.append(trade)
            lot["qty"] -= matched
            remaining -= matched
            if lot["qty"] == ZERO:
                queue.pop(0)
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
            if t.get("direction") == "SHORT":
                t["mfe"] = max(ZERO, t["entry_price"] - low) * t["quantity"]
                t["mae"] = min(ZERO, t["entry_price"] - high) * t["quantity"]
            else:
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
            # signed exposure: BUY / BUY_TO_COVER add, SELL / SELL_SHORT subtract
            exposure[symbol] += (
                qty if str(f["side"]).upper() in ("BUY", "B", "BUY_TO_COVER") else -qty
            )
            peak_exposure = max(
                peak_exposure,
                sum((abs(q) * marks.get(s, ZERO) for s, q in exposure.items()), ZERO),
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
    shorts: tuple[type, ...] = ()
    covers: tuple[type, ...] = ()

    @property
    def all(self) -> tuple[type, ...]:
        return self.buys + self.sells + self.shorts + self.covers


@dataclass(slots=True)
class _Protection:
    """Backtest bookkeeping for one open cycle's protective stop."""

    order_id: str
    spec: Mapping[str, Any]
    policy: ProtectionPolicy
    entry_reference: Decimal
    current_stop: Decimal
    stepped: SteppedTrailingState | None = None
    direction: PositionDirection = PositionDirection.LONG


def _floor_stop(price: Decimal) -> Decimal:
    return ALPACA_EQUITY_TICK_RULES.floor(price)


def _ceil_stop(price: Decimal) -> Decimal:
    return ALPACA_EQUITY_TICK_RULES.ceil(price)


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
        session_candles: SessionCandleSource | None = None,
    ) -> None:
        self.registry, self.feed, self.clock, self.execution = registry, feed, clock, execution
        #: Optional completed D1/W1 history (point-in-time via ``available_at``). Without it a
        #: strategy that requires D1/W1 candles is refused and M1 behaviour is unchanged.
        self.session_candles = session_candles
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
                tuple(c for c in ordered if c.spec.side is StrategyRole.SHORT),
                tuple(c for c in ordered if c.spec.side is StrategyRole.COVER),
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
        supported_replay_timeframes = {CandleTimeframe.M1, CandleTimeframe.M5}
        for plan in plans.values():
            for cls in plan.all:
                required = {*cls.spec.required_timeframes, cls.spec.trigger_timeframe}
                needs_session = {tf for tf in required if tf.is_session_based}
                if needs_session and self.session_candles is None:
                    labels = sorted(tf.value for tf in needs_session)
                    raise ValueError(
                        f"strategy {cls.spec.strategy_id} requires completed {labels} candles; "
                        "supply a session_candles source (daily history dataset) to the runner"
                    )
                unsupported = required - supported_replay_timeframes - needs_session
                if unsupported:
                    labels = sorted(tf.value for tf in unsupported)
                    raise ValueError(
                        f"strategy {cls.spec.strategy_id} requires unsupported replay "
                        f"timeframes {labels}; local 1m replay derives completed 5m bars only"
                    )
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
                "lifecycle_semantics": (
                    BIDIRECTIONAL_LIFECYCLE_SEMANTICS_VERSION
                    if any(p.shorts or p.covers for p in plans.values())
                    else LIFECYCLE_SEMANTICS_VERSION
                ),
                "context_history_bars": request.context_history_bars,
                **(
                    {"session_candles": self.session_candles.fingerprint()}  # type: ignore[attr-defined]
                    if self.session_candles is not None
                    and hasattr(self.session_candles, "fingerprint")
                    else {}
                ),
                "strategy_collections": {
                    s: {
                        "buy": [c.spec.strategy_id for c in p.buys],
                        "sell": [c.spec.strategy_id for c in p.sells],
                        **(
                            {
                                "short": [c.spec.strategy_id for c in p.shorts],
                                "cover": [c.spec.strategy_id for c in p.covers],
                            }
                            if p.shorts or p.covers
                            else {}
                        ),
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
        execution_id = str(uuid.uuid4())
        started_at = datetime.now(UTC)
        manifest = RunManifest(
            run_id,
            started_at,
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
            execution_id=execution_id,
            started_at=started_at,
            run_purpose=(
                str(request.config.get("run_purpose") or request.config.get("purpose"))
                if request.config.get("run_purpose") or request.config.get("purpose")
                else None
            ),
            configuration=effective,
        )
        root = Path(request.result_directory)
        scope_id = str(request.config.get("research_scope_id", "backtest:research"))
        minimum_hold = timedelta(seconds=float(request.config.get("minimum_hold_seconds") or 0))
        eod_config = request.config.get("end_of_day_close") or {}
        if not isinstance(eod_config, Mapping):
            raise ValueError("end_of_day_close must be a mapping")
        eod_enabled = eod_config.get("enabled", False)
        if not isinstance(eod_enabled, bool):
            raise ValueError("end_of_day_close.enabled must be bool")
        eod_at = eod_config.get("at", "15:55")
        if not isinstance(eod_at, str):
            raise ValueError("end_of_day_close.at must be HH:MM")
        try:
            eod_hour, eod_minute = (int(part) for part in eod_at.split(":"))
            if (
                len(eod_at.split(":")) != 2
                or not (0 <= eod_hour < 16 and 0 <= eod_minute < 60)
            ):
                raise ValueError
        except ValueError as exc:
            raise ValueError("end_of_day_close.at must be HH:MM") from exc
        evaluations: list[Mapping[str, Any]] = []
        signals: list[Mapping[str, Any]] = []
        intents: list[Mapping[str, Any]] = []
        decisions: list[Mapping[str, Any]] = []
        protection_events: list[Mapping[str, Any]] = []
        fills: list[Mapping[str, Any]] = []
        cycles: list[dict[str, Any]] = []
        equity_curve: list[dict[str, Any]] = []
        cash = request.starting_capital
        quantities: dict[str, Decimal] = defaultdict(lambda: ZERO)  # position magnitude
        costs: dict[str, Decimal] = defaultdict(lambda: ZERO)
        directions: dict[str, PositionDirection] = {}  # direction of each open position
        marks: dict[str, Decimal] = {}
        # Short-selling controls (fail closed): shorting is opt-in per run, proceeds are
        # restricted collateral, and borrow cost is an explicit estimated assumption.
        short_authorized = bool(request.config.get("short_selling_enabled", False))
        margin_multiplier = Decimal(
            str(request.config.get("short_margin_multiplier", DEFAULT_SHORT_MARGIN_MULTIPLIER))
        )
        if margin_multiplier < Decimal(1):
            raise ValueError("short_margin_multiplier must be >= 1")
        borrow_bps_per_day = Decimal(str(request.config.get("short_borrow_bps_per_day", "0")))
        if borrow_bps_per_day < ZERO:
            raise ValueError("short_borrow_bps_per_day must be >= 0")
        max_gross_exposure = (
            Decimal(str(request.config["max_gross_exposure"]))
            if request.config.get("max_gross_exposure") is not None
            else None
        )
        has_short_strategies = any(p.shorts or p.covers for p in plans.values())

        def is_short(symbol: str) -> bool:
            return directions.get(symbol) is PositionDirection.SHORT

        def restricted_collateral() -> Decimal:
            """Cash locked behind open shorts: never available to buy or short anything."""
            return sum(
                (margin_multiplier * costs[s] for s in list(directions) if is_short(s)), ZERO
            )

        def portfolio_equity() -> Decimal:
            """Cash plus longs minus the market liability of shorts (marks, else cost)."""
            total = cash
            for sym, qty in quantities.items():
                if qty <= ZERO:
                    continue
                value = qty * marks.get(sym, ZERO)
                total += -value if is_short(sym) else value
            return total

        def equity_row(at: datetime) -> dict[str, Any]:
            row: dict[str, Any] = {"time": at, "cash": cash, "equity": portfolio_equity()}
            if has_short_strategies:
                collateral = restricted_collateral()
                row["restricted_collateral"] = collateral
                row["available_cash"] = cash - collateral
            return row

        def gross_exposure() -> Decimal:
            return sum(
                (q * marks.get(sym, ZERO) for sym, q in quantities.items() if q > ZERO), ZERO
            )

        def borrow_cost(symbol: str, quantity: Decimal, at: datetime) -> Decimal:
            """Estimated stock-borrow charge for covering ``quantity`` (an assumption, 0 default)."""
            cycle = open_cycles.get(symbol)
            if borrow_bps_per_day <= ZERO or not is_short(symbol) or cycle is None:
                return ZERO
            opened = cycle["opened_at"].astimezone(NEW_YORK).date()
            days = max((at.astimezone(NEW_YORK).date() - opened).days, 0)
            share = costs[symbol] * quantity / quantities[symbol]
            return share * borrow_bps_per_day * days / _BPS
        open_cycles: dict[str, dict[str, Any]] = {}
        order_meta: dict[str, dict[str, Any]] = {}
        pending_entry: dict[str, str] = {}
        pending_exit: dict[str, str] = {}
        protections: dict[str, _Protection] = {}
        eod_submitted: set[tuple[str, str]] = set()
        protection_count = 0
        candles_by_symbol: dict[str, list[Candle]] = defaultdict(list)
        strategy_candles: dict[str, dict[CandleTimeframe, deque[StrategyCandle]]] = defaultdict(
            lambda: {tf: deque(maxlen=request.context_history_bars) for tf in CandleTimeframe}
        )
        window: dict[str, deque[StrategyCandle]] = defaultdict(
            lambda: deque(maxlen=request.context_history_bars)
        )
        market_status_enabled = bool(request.config.get("market_status_enabled", False))
        backtest_monitor = request.config.get("market_status_monitor")
        ref_symbols: tuple[str, ...] = tuple(
            request.config.get("market_status_reference_symbols", ("SPY", "QQQ", "IWM"))
        )
        if market_status_enabled and backtest_monitor is None:
            from mike_trade.market_status.config import MarketStatusConfig
            from mike_trade.market_status.monitor import MarketStatusMonitor
            from mike_trade.market_status.policy import TradingAuthorizationPolicy

            ms_config = request.config.get("market_status_config") or MarketStatusConfig(
                symbols=ref_symbols
            )
            ms_policy = request.config.get("market_status_policy") or TradingAuthorizationPolicy()
            backtest_monitor = MarketStatusMonitor(
                config=ms_config,
                policy=ms_policy,
                clock=self.clock,
            )

        feed_symbols = request.symbols
        if backtest_monitor is not None:
            feed_symbols = tuple(dict.fromkeys((*request.symbols, *ref_symbols)))

        replay_events = [
            e
            for e in self.feed.events(symbols=feed_symbols)
            if e.available_at and request.window_start <= e.available_at <= request.window_end
        ]
        # Reference bars are fed to the monitor strictly by availability time, ahead of any
        # trading event at the same or a later time (point-in-time: nothing later is visible),
        # and the monitor is stepped once per distinct reference minute so the replayed state
        # sequence matches what a live feed produces.
        reference_events = [
            e
            for e in replay_events
            if backtest_monitor is not None
            and e.kind == ReplayEvent.KIND_BAR
            and e.candle is not None
            and e.symbol in ref_symbols
        ]
        reference_cursor = 0

        def feed_reference(upto: datetime) -> None:
            nonlocal reference_cursor
            while reference_cursor < len(reference_events):
                batch_time = reference_events[reference_cursor].available_at
                assert batch_time is not None
                if batch_time > upto:
                    return
                while (
                    reference_cursor < len(reference_events)
                    and reference_events[reference_cursor].available_at == batch_time
                ):
                    candle_ref = reference_events[reference_cursor].candle
                    assert candle_ref is not None and backtest_monitor is not None
                    backtest_monitor.add_bar(candle_ref)
                    reference_cursor += 1
                backtest_monitor.evaluate(as_of=batch_time)

        session_frames: dict[str, tuple[CandleTimeframe, ...]] = {
            symbol: tuple(
                sorted(
                    {
                        tf
                        for cls in plan.all
                        for tf in cls.spec.required_timeframes
                        if tf.is_session_based
                    },
                    key=lambda tf: tf.value,
                )
            )
            for symbol, plan in plans.items()
        }
        trigger_rows: set[tuple[str, str, datetime]] = set()
        state: dict[tuple[str, str], dict[str, Any]] = defaultdict(dict)
        for symbol in request.symbols:
            for strategy_id, initial in request.initial_strategy_state.items():
                state[(symbol, strategy_id)].update(dict(initial))
        vwap_session: dict[str, tuple[str, Decimal, Decimal, Decimal | None]] = {}
        session_references = SessionReferenceTracker()
        entries_by_session: dict[tuple[str, str], int] = defaultdict(int)
        rsi_values: dict[str, tuple[Decimal | None, Decimal | None]] = {}

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
            direction = directions.get(symbol, PositionDirection.LONG)
            long = direction is PositionDirection.LONG
            floor_stop = current.current_stop if current is not None else ZERO
            stepped = current.stepped if current is not None else None
            protection_count += 1
            common = dict(
                intent_id=f"{run_id}:{symbol}:protection:{protection_count}",
                client_order_id=f"{run_id[:20]}-protect-{symbol}-{protection_count}",
                account_scope_id=scope_id,
                cycle_id=cycle_id,
                symbol=symbol,
                side=OrderSide.SELL if long else OrderSide.BUY_TO_COVER,
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
                    raw_stop = entry_reference - offset if long else entry_reference + offset
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
                        side=direction.value,
                    )
                    raw_stop = stepped.candidate_stop
                if long:
                    stop_now = max(_floor_stop(raw_stop), floor_stop)
                    valid = ZERO < stop_now < entry_reference
                else:
                    # a short's protective stop sits ABOVE the market and only ever falls
                    stop_now = _ceil_stop(raw_stop)
                    if floor_stop > ZERO:
                        stop_now = min(stop_now, floor_stop)
                    valid = stop_now > entry_reference
                if not valid:
                    raise ValueError(
                        f"{symbol}: protective stop distance leaves no valid stop "
                        f"{'below' if long else 'above'} the {entry_reference} entry reference"
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
            recorded_stop = stop_now
            if policy.kind is ProtectionKind.BROKER_TRAILING_STOP:
                native_state = self.execution.protection_state(order.broker_order_id)
                if native_state is not None:
                    recorded_stop = native_state.stop_price
            protections[symbol] = _Protection(
                order.broker_order_id, stop_spec, policy, entry_reference, recorded_stop, stepped,
                direction,
            )
            open_cycles[symbol]["protection_mode"] = str(spec_type(stop_spec))
            if recorded_stop > ZERO:
                open_cycles[symbol].setdefault("initial_stop_price", recorded_stop)
                open_cycles[symbol]["final_stop_price"] = recorded_stop
            protection_events.append({
                "cycle_id": cycle_id, "symbol": symbol,
                "event_type": "INITIAL_STOP_ESTABLISHED", "time": self.clock.now(),
                "protection_mode": str(spec_type(stop_spec)),
                "order_id": order.broker_order_id,
                "stop_price": None if recorded_stop == ZERO else recorded_stop,
                "quantity": quantities[symbol],
            })

        async def ratchet_stepped(symbol: str, high: Decimal, low: Decimal) -> None:
            """Engine-managed stepped trailing: tighten (never loosen) the stop.

            A long's stop only rises with new highs; a short's stop only falls with new lows.
            """
            protection = protections.get(symbol)
            if (
                protection is None
                or protection.stepped is None
                or protection.policy.kind is not ProtectionKind.STEPPED_TRAILING_STOP
                or not await order_is_open(protection.order_id)
            ):
                return
            policy = protection.policy
            long = protection.direction is PositionDirection.LONG
            favorable = high if long else low
            activation = policy.activation_gain or ZERO
            if (
                favorable < protection.entry_reference + activation
                if long
                else favorable > protection.entry_reference - activation
            ):
                return
            moved = advance(
                protection.stepped,
                favorable,
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
            candidate = (
                _floor_stop(moved.candidate_stop) if long else _ceil_stop(moved.candidate_stop)
            )
            if (candidate > protection.current_stop) if long else (candidate < protection.current_stop):
                previous = protection.current_stop
                await self.execution.replace_order(protection.order_id, stop_price=candidate)
                protection.current_stop = candidate
                cycle_id = str(open_cycles[symbol]["cycle_id"])
                open_cycles[symbol]["final_stop_price"] = candidate
                protection_events.append({
                    "cycle_id": cycle_id, "symbol": symbol,
                    "event_type": "STOP_MOVED", "time": self.clock.now(),
                    "protection_mode": "TRAILING_STEPPED",
                    "order_id": protection.order_id,
                    "previous_stop_price": previous, "new_stop_price": candidate,
                    "stop_price": candidate,
                })

        async def record_native_stop(symbol: str) -> None:
            """Record broker-simulated native stop changes after a replay event."""
            protection = protections.get(symbol)
            if protection is None or protection.policy.kind is not ProtectionKind.BROKER_TRAILING_STOP:
                return
            state = self.execution.protection_state(protection.order_id)
            if state is None or (
                state.stop_price <= protection.current_stop
                if protection.direction is PositionDirection.LONG
                else state.stop_price >= protection.current_stop
            ):
                return
            previous = protection.current_stop
            protection.current_stop = state.stop_price
            cycle = open_cycles.get(symbol)
            if cycle is None:
                return
            cycle["final_stop_price"] = state.stop_price
            protection_events.append({
                "cycle_id": str(cycle["cycle_id"]), "symbol": symbol,
                "event_type": "STOP_MOVED", "time": self.clock.now(),
                "protection_mode": "TRAILING_NATIVE", "order_id": protection.order_id,
                "previous_stop_price": previous, "new_stop_price": state.stop_price,
                "stop_price": state.stop_price,
            })

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
            if intent.side.is_entry:
                # Direction-aware entry gates: shorting is opt-in, short-sale proceeds are
                # restricted collateral (only the own-cash margin part is purchasing power),
                # and an optional gross-exposure ceiling applies to long and short alike.
                reference = evaluation.reference_price or marks.get(symbol) or ZERO
                notional = (intent.quantity or ZERO) * reference
                refusal: str | None = None
                if intent.side is OrderSide.SELL_SHORT:
                    if not short_authorized:
                        refusal = "SHORT_SELLING_NOT_AUTHORIZED"
                    elif (margin_multiplier - 1) * notional > cash - restricted_collateral():
                        refusal = "INSUFFICIENT_SHORT_MARGIN"
                if (
                    refusal is None
                    and max_gross_exposure is not None
                    and gross_exposure() + notional > max_gross_exposure
                ):
                    refusal = "GROSS_EXPOSURE_LIMIT"
                if refusal is not None:
                    decisions.append({
                        "kind": "ENTRY_REFUSED", "time": context.as_of, "symbol": symbol,
                        "signal_id": evaluation.signal_id, "strategy_id": evaluation.strategy_id,
                        "side": intent.side.value, "reason": refusal,
                    })
                    return
            if intent.side.is_exit:
                # Make way for the discretionary exit exactly as the engine does:
                # the stop is cancelled first and re-armed if the exit never fills.
                protection = protections.get(symbol)
                if protection is not None and await order_is_open(protection.order_id):
                    await self.execution.cancel_order(protection.order_id)
            intents.append(
                {
                    "symbol": symbol,
                    "strategy": evaluation.strategy_id,
                    "strategy_version": evaluation.version,
                    "signal_id": evaluation.signal_id,
                    "signal_time": context.as_of,
                    "signal_bar_time": evaluation.bar_time,
                    "cycle_id": None,
                    "side": intent.side.value,
                    "purpose": intent.purpose.value,
                    "time": context.as_of,
                    "intent_id": getattr(intent, "intent_id", None),
                }
            )
            order = await self.execution.submit_order(intent)
            intents[-1]["order_id"] = order.broker_order_id
            order_meta[order.broker_order_id] = {
                "purpose": intent.purpose.value,
                "strategy": evaluation.strategy_id,
                "strategy_version": evaluation.version,
                "signal_id": evaluation.signal_id,
                "signal_time": context.as_of,
                "intent_id": getattr(intent, "intent_id", None),
            }
            if intent.side.is_entry:
                pending_entry[symbol] = order.broker_order_id
            else:
                pending_exit[symbol] = order.broker_order_id

        async def submit_eod_close(symbol: str, at: datetime) -> None:
            """Submit a regular-session DAY market close at the configured cutoff."""
            if not eod_enabled or quantities[symbol] <= ZERO:
                return
            session = session_for(at.astimezone(NEW_YORK).date())
            configured = datetime.combine(
                session.session_date,
                time(eod_hour, eod_minute),
                tzinfo=NEW_YORK,
            ).astimezone(UTC)
            # If the configured wall time is beyond an early close, keep its
            # minutes-before-16:00 interval relative to that session's close.
            if configured >= session.close_utc:
                minutes_before_close = 16 * 60 - (eod_hour * 60 + eod_minute)
                trigger = session.close_utc - timedelta(minutes=minutes_before_close)
            else:
                trigger = configured
            if not (trigger <= at < session.close_utc):
                return
            session_key = (symbol, session.session_date.isoformat())
            if session_key in eod_submitted:
                return
            direction = directions.get(symbol)
            if direction is None:
                return
            # A pending strategy exit is superseded by the mandatory EOD order.
            pending = pending_exit.pop(symbol, None)
            if pending is not None:
                await self.execution.cancel_order(pending)
            protection = protections.get(symbol)
            if protection is not None and await order_is_open(protection.order_id):
                await self.execution.cancel_order(protection.order_id)
            side = (
                OrderSide.SELL
                if direction is PositionDirection.LONG
                else OrderSide.BUY_TO_COVER
            )
            cycle = open_cycles[symbol]
            intent = OrderIntent(
                intent_id=f"eod-{symbol}-{session.session_date.isoformat()}",
                client_order_id=f"eod-{symbol}-{session.session_date.isoformat()}",
                account_scope_id=scope_id,
                cycle_id=str(cycle["cycle_id"]),
                symbol=symbol,
                side=side,
                purpose=OrderPurpose.EOD_CLOSE,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                created_at=at,
                quantity=quantities[symbol],
            )
            order = await self.execution.submit_order(intent)
            order_meta[order.broker_order_id] = {
                "purpose": OrderPurpose.EOD_CLOSE.value,
                "strategy": None,
                "exit_reason": "END_OF_DAY",
            }
            intents.append({
                "symbol": symbol,
                "strategy": None,
                "signal_id": None,
                "signal_time": at,
                "signal_bar_time": None,
                "cycle_id": str(cycle["cycle_id"]),
                "side": side.value,
                "purpose": OrderPurpose.EOD_CLOSE.value,
                "time": at,
                "intent_id": intent.intent_id,
                "order_id": order.broker_order_id,
            })
            pending_exit[symbol] = order.broker_order_id
            eod_submitted.add(session_key)

        for ev in replay_events:
            assert ev.available_at
            self.clock.advance_to(ev.available_at)
            if backtest_monitor is not None:
                feed_reference(ev.available_at)
            outputs = self.execution.process_event(ev)
            await record_native_stop(ev.symbol)
            for x in outputs:
                meta = order_meta.get(x.fill.broker_order_id, {})
                is_buy = x.side.is_entry  # opens a position (BUY or SELL_SHORT)
                direction = x.side.direction
                # Estimated borrow charge, charged only when covering a short.
                borrow = (
                    borrow_cost(x.symbol, x.quantity, x.fill.filled_at)
                    if not is_buy and direction is PositionDirection.SHORT
                    else ZERO
                )
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
                    "strategy_version": meta.get("strategy_version"),
                    "signal_id": meta.get("signal_id"),
                    "signal_time": meta.get("signal_time"),
                    "exit_reason": None
                    if is_buy
                    else (
                        str(meta["exit_reason"])
                        if meta.get("exit_reason") is not None
                        else (
                            ExitSource.PROTECTIVE_STOP.value
                            if x.purpose is OrderPurpose.PROTECTIVE_STOP
                            else (
                                ExitSource.STRATEGY_SELL.value
                                if direction is PositionDirection.LONG
                                else ExitSource.STRATEGY_COVER.value
                            )
                        )
                    ),
                }
                if direction is PositionDirection.SHORT:
                    # Long-only artifacts keep their exact shape; shorts add explicit fields.
                    fill_row["direction"] = direction.value
                    fill_row["borrow_cost"] = borrow
                fills.append(fill_row)
                # BUY pays; SELL_SHORT is credited (the proceeds are locked as collateral by
                # ``restricted_collateral``); SELL is credited; BUY_TO_COVER pays market + costs.
                if x.side is OrderSide.BUY:
                    cash -= x.quantity * x.price + x.fee
                elif x.side is OrderSide.BUY_TO_COVER:
                    cash -= x.quantity * x.price + x.fee + borrow
                else:
                    cash += x.quantity * x.price - x.fee
                marks[x.symbol] = x.price
                if is_buy:
                    if quantities[x.symbol] <= ZERO:
                        directions[x.symbol] = direction
                    quantities[x.symbol] += x.quantity
                    costs[x.symbol] += x.quantity * x.price
                    if x.symbol not in open_cycles:
                        session_key = x.fill.filled_at.astimezone(NEW_YORK).date().isoformat()
                        entries_by_session[(x.symbol, session_key)] += 1
                        open_cycles[x.symbol] = {
                            # Run identity is already carried by the artifact envelope. A
                            # run-local stable cycle id keeps economically identical runs
                            # comparable even when their config hashes differ.
                            "cycle_id": f"{x.symbol}:{len(cycles) + 1}",
                            "symbol": x.symbol,
                            "opened_at": x.fill.filled_at,
                            "closed_at": None,
                            "entry_quantity": ZERO,
                            "exit_quantity": ZERO,
                            "entry_strategy": meta.get("strategy"),
                            "entry_strategy_version": meta.get("strategy_version"),
                            "entry_signal_id": meta.get("signal_id"),
                            "entry_signal_timestamp": meta.get("signal_time"),
                            "entry_decision": "SELECTED",
                            "exit_reason": None,
                            "exit_strategy": None,
                            **(
                                {"direction": direction.value}
                                if direction is PositionDirection.SHORT
                                else {}
                            ),
                        }
                    fill_row["cycle_id"] = open_cycles[x.symbol]["cycle_id"]
                    for intent_row in intents:
                        if intent_row.get("intent_id") == meta.get("intent_id"):
                            intent_row["cycle_id"] = fill_row["cycle_id"]
                    open_cycles[x.symbol]["entry_quantity"] += x.quantity
                    await arm_protection(x.symbol, x.price)
                else:
                    average = costs[x.symbol] / quantities[x.symbol]
                    costs[x.symbol] -= average * x.quantity
                    quantities[x.symbol] -= x.quantity
                    if x.symbol in open_cycles:
                        cycle = open_cycles[x.symbol]
                        fill_row["cycle_id"] = cycle["cycle_id"]
                        for intent_row in intents:
                            if intent_row.get("intent_id") == meta.get("intent_id"):
                                intent_row["cycle_id"] = fill_row["cycle_id"]
                        cycle["exit_quantity"] += x.quantity
                        cycle["exit_reason"] = fill_row["exit_reason"]
                        cycle["exit_strategy"] = meta.get("strategy")
                        cycle["exit_strategy_version"] = meta.get("strategy_version")
                        cycle["exit_signal_id"] = meta.get("signal_id")
                        cycle["exit_signal_timestamp"] = meta.get("signal_time")
                        if quantities[x.symbol] <= ZERO:
                            costs[x.symbol] = ZERO
                            directions.pop(x.symbol, None)
                            protections.pop(x.symbol, None)
                            pending_exit.pop(x.symbol, None)
                            cycle = open_cycles.pop(x.symbol)
                            cycle["closed_at"] = x.fill.filled_at
                            cycles.append(cycle)
                equity_curve.append(equity_row(x.fill.filled_at))
            await submit_eod_close(ev.symbol, ev.available_at)
            if ev.kind != ReplayEvent.KIND_BAR or ev.candle is None:
                continue
            symbol = ev.symbol
            plan = plans.get(symbol)
            if plan is None:
                continue
            marks[symbol] = ev.candle.close
            equity_curve.append(equity_row(ev.available_at))
            candles_by_symbol[symbol].append(ev.candle)
            candle = ev.candle
            strategy_bar = StrategyCandle(
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
            window[symbol].append(strategy_bar)
            strategy_candles[symbol][CandleTimeframe.M1].append(strategy_bar)
            session_references.add_bar(strategy_bar)
            # Derive only fully completed, contiguous five-minute buckets aligned
            # to the regular-session open. The current 1m bar is available only
            # at its close, so no developing or future minute can enter the bucket.
            session = session_for(candle.start.astimezone(NEW_YORK).date())
            elapsed = int((candle.end - session.open_utc).total_seconds() // 60)
            m5_closed = elapsed > 0 and elapsed % 5 == 0
            if m5_closed:
                minute_history = list(strategy_candles[symbol][CandleTimeframe.M1])
                parts = minute_history[-5:]
                if len(parts) == 5 and all(
                    b.timeframe is CandleTimeframe.M1
                    and b.start == parts[0].start + timedelta(minutes=i)
                    and b.start.astimezone(NEW_YORK).date() == candle.start.astimezone(NEW_YORK).date()
                    for i, b in enumerate(parts)
                ):
                    strategy_candles[symbol][CandleTimeframe.M5].append(
                        StrategyCandle(
                            symbol, CandleTimeframe.M5, parts[0].start, parts[-1].end,
                            parts[0].open, max(b.high for b in parts), min(b.low for b in parts),
                            parts[-1].close, sum((b.volume for b in parts), ZERO),
                            parts[-1].feed_id, parts[-1].consolidated, CandleOrigin.REPLAY,
                            PriceBasis.RAW, CandleStatus.CLOSED,
                            None if any(b.trade_count is None for b in parts)
                            else sum((b.trade_count or 0 for b in parts), 0),
                        )
                    )
                    period = next((
                        int(plan.params[cls.spec.strategy_id].get("rsi_period", 14))
                        for cls in plan.sells if cls.spec.strategy_id == "rsi_reversal"
                    ), 14)
                    calc_values = calculate_rsi(
                        (b.close for b in strategy_candles[symbol][CandleTimeframe.M5]), period
                    )
                    rsi_values[symbol] = (
                        calc_values[-1] if calc_values else None,
                        calc_values[-2] if len(calc_values) >= 2 else None,
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
            await ratchet_stepped(symbol, candle.high, candle.low)

            due = self.scheduler.process(
                CandleClosedEvent(
                    symbol,
                    CandleTimeframe(candle.timeframe.value),
                    candle.end,
                    snapshot_version=len(candles_by_symbol[symbol]),
                    due_at=ev.available_at,
                )
            )
            if m5_closed and strategy_candles[symbol][CandleTimeframe.M5] and strategy_candles[symbol][CandleTimeframe.M5][-1].end == candle.end:
                due += self.scheduler.process(
                    CandleClosedEvent(
                        symbol, CandleTimeframe.M5, candle.end,
                        snapshot_version=len(strategy_candles[symbol][CandleTimeframe.M5]),
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
                session.session_id,
                freshness,
                {
                    **{tf: tuple(bars) for tf, bars in strategy_candles[symbol].items() if bars},
                    # completed D1/W1 history exactly as knowable at this event's availability
                    **{
                        tf: self.session_candles.get_completed_candles(
                            symbol, tf, None, as_of=ev.available_at
                        )
                        for tf in session_frames.get(symbol, ())
                        if self.session_candles is not None
                    },
                },
                {"vwap": latest_vwap, "vwap_prev": prior_vwap,
                 "rsi": rsi_values.get(symbol, (None, None))[0],
                 "rsi_prev": rsi_values.get(symbol, (None, None))[1]},
                session_reference=session_references.reference(symbol, ev.available_at),
            )

            def evaluate(cls: type, record: bool = True) -> StrategyEvaluation | None:
                spec = cls.spec
                if spec.strategy_id not in due_ids:
                    return None
                selected_history = strategy_candles[symbol].get(spec.trigger_timeframe, ())
                if len(selected_history) < max(
                    1, cls.required_bars(plan.params[spec.strategy_id])
                ):
                    return None
                if spec.trigger_timeframe.value != candle.timeframe.value and not (
                    spec.trigger_timeframe is CandleTimeframe.M5 and m5_closed
                    and selected_history and selected_history[-1].end == candle.end
                ):
                    return None
                trigger_bar = selected_history[-1]
                key = (symbol, spec.strategy_id, trigger_bar.start)
                if key in trigger_rows:
                    return None
                trigger_rows.add(key)
                evaluation: StrategyEvaluation = cls().evaluate(
                    context, plan.params[spec.strategy_id], state[(symbol, spec.strategy_id)]
                )
                state[(symbol, spec.strategy_id)].update(evaluation.state_updates)
                if not record:
                    return None
                context_bars = [
                    bar for history in strategy_candles[symbol].values() for bar in history
                ]
                context_max_bar_end = max((bar.end for bar in context_bars), default=None)
                row = {
                    "symbol": symbol,
                    "strategy": spec.strategy_id,
                    "side": spec.side.value,
                    "version": spec.version,
                    "time": ev.available_at,
                    "bar_time": trigger_bar.start,
                    "trigger_timeframe": spec.trigger_timeframe.value,
                    "trigger_bar_end": trigger_bar.end,
                    "trigger_history_count": len(selected_history),
                    "context_max_bar_end": context_max_bar_end,
                    "context_indicators": {
                        key: context.indicators.get(key)
                        for key in ("rsi", "rsi_prev")
                    },
                    "trigger_bar_ohlcv": {
                        "open": trigger_bar.open, "high": trigger_bar.high,
                        "low": trigger_bar.low, "close": trigger_bar.close,
                        "volume": trigger_bar.volume,
                    },
                    "signal_id": evaluation.signal_id,
                    "reference_price": evaluation.reference_price,
                    "reason_codes": [x.value for x in evaluation.reason_codes],
                }
                evaluations.append(row)
                if evaluation.is_signal:
                    signals.append(row)
                    return evaluation
                return None

            held_direction = directions.get(symbol) if quantities[symbol] > ZERO else None
            if held_direction is not None:
                # A position is held. Only the exit role of its own direction may act:
                # SELL closes a long, COVER closes a short.  Every other role keeps its
                # indicator state continuous (advance-only: ineligible roles can never
                # signal, and any signal they would raise is discarded).
                long_held = held_direction is PositionDirection.LONG
                exit_cls = plan.sells if long_held else plan.covers
                other_exit_cls = plan.covers if long_held else plan.sells
                exit_source = ExitSource.STRATEGY_SELL if long_held else ExitSource.STRATEGY_COVER
                for cls in (*plan.buys, *plan.shorts):
                    # Clearing the eligibility flag makes the gate block every entry signal.
                    state[(symbol, cls.spec.strategy_id)].pop("eligibility", None)
                    evaluate(cls, record=False)
                for cls in other_exit_cls:
                    evaluate(cls, record=False)
                winners = {
                    e.signal_id: (cls, e)
                    for cls in exit_cls
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
                        direction=held_direction,
                    )
                    result = arbitrate_exit_decisions(
                        [
                            ExitCandidate(
                                signal_id=sid,
                                source=exit_source,
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
                            exit_source if symbol in pending_exit else None
                        ),
                    )
                    decisions.append(
                        {"kind": "EXIT", "time": ev.available_at, **result.to_dict()}
                    )
                    if result.selected is not None:
                        await submit(winners[result.selected.signal_id][1], context, symbol)
            else:
                # Flat: SELL and COVER have nothing to close; keep their indicators advancing
                # and discard any signal.  BUY and (when authorized) SHORT compete for entry.
                for cls in (*plan.sells, *plan.covers):
                    evaluate(cls, record=False)
                candidates = {}
                for cls in plan.buys:
                    state[(symbol, cls.spec.strategy_id)]["eligibility"] = "WATCHING_BUY"
                    if (e := evaluate(cls)) is not None:
                        candidates[e.signal_id] = (cls, e)
                for cls in plan.shorts:
                    if short_authorized:
                        state[(symbol, cls.spec.strategy_id)]["eligibility"] = "WATCHING_SHORT"
                        if (e := evaluate(cls)) is not None:
                            candidates[e.signal_id] = (cls, e)
                    else:
                        # Not authorized: ineligible, advance-only (never a signal).
                        state[(symbol, cls.spec.strategy_id)].pop("eligibility", None)
                        evaluate(cls, record=False)
                market_blocked: dict[PositionDirection, str] = {}
                if candidates and backtest_monitor is not None:
                    # Market-state authorization is additional to every other entry gate and is
                    # applied per direction *before* arbitration: a restricted BUY must not
                    # suppress an authorized SHORT (nor the reverse).  Exits are never gated.
                    authorization = backtest_monitor.evaluate(
                        as_of=ev.available_at
                    ).authorization
                    for direction, allowed in (
                        (PositionDirection.LONG, authorization.buy_allowed),
                        (PositionDirection.SHORT, authorization.short_allowed),
                    ):
                        if not allowed:
                            market_blocked[direction] = (
                                authorization.reasons[0]
                                if authorization.reasons
                                else f"MARKET_STATE_{authorization.state.value}_"
                                f"{direction.value}_RESTRICTED"
                            )
                    blocked_candidates = {
                        sid: market_blocked[
                            PositionDirection.LONG
                            if cls.spec.side is StrategyRole.BUY
                            else PositionDirection.SHORT
                        ]
                        for sid, (cls, _e) in candidates.items()
                        if (
                            PositionDirection.LONG
                            if cls.spec.side is StrategyRole.BUY
                            else PositionDirection.SHORT
                        )
                        in market_blocked
                    }
                    if blocked_candidates:
                        for sid, reason in blocked_candidates.items():
                            cls_b, e_b = candidates.pop(sid)
                            decisions.append({
                                "kind": "ENTRY_REFUSED",
                                "time": ev.available_at,
                                "symbol": symbol,
                                "signal_id": sid,
                                "strategy_id": cls_b.spec.strategy_id,
                                "side": cls_b.spec.side.value,
                                "reason": reason,
                                "gate": "MARKET_STATUS",
                                "market_state": authorization.state.value,
                            })
                if candidates:
                    result = arbitrate_entry_decisions(
                        [
                            EntryCandidate(
                                sid, cls.spec.strategy_id, cls.spec.version, symbol, e.bar_time,
                                PositionDirection.LONG
                                if cls.spec.side is StrategyRole.BUY
                                else PositionDirection.SHORT,
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
            "schema_version": "2",
            "run_id": run_id,
            "execution_id": execution_id,
            "evaluations": evaluations,
            "signals": signals,
            "intents": intents,
            "decisions": decisions,
            "protection_events": protection_events,
            "fills": fills,
            "cycles": cycles + list(open_cycles.values()),
            "equity_curve": equity_curve,
        }
        manifest = replace(manifest, completed_at=datetime.now(UTC))
        result_hash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        artifact_root = root / "executions" / execution_id
        result_path = artifact_root / f"{run_id}.results.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path = artifact_root / f"{run_id}.manifest.json"
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
                    write_manifest(manifest, artifact_root)
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
            tuple(protection_events),
        )
