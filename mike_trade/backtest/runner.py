"""Deterministic offline backtest orchestration and research metrics.

Run artifacts use immutable run-scoped JSON files because changing the existing
SQLite persistence schema would require a migration outside this task's scope.
All collaborators are injected; this module never constructs a broker client.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
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
from mike_trade.domain.trading import OrderIntent, OrderPurpose, OrderSide, OrderType, TimeInForce
from mike_trade.market_data.candles import (
    Candle as StrategyCandle,
    CandleOrigin,
    CandleStatus,
    CandleTimeframe,
    PriceBasis,
)
from mike_trade.market_data.context import ContextFreshness, MarketContext
from mike_trade.scheduling.calendar import NEW_YORK, entry_cutoff, session_for
from mike_trade.scheduling.scheduler import CandleClosedEvent, StrategyScheduler
from mike_trade.scheduling.triggers import StrategyTrigger, TriggerKind
from mike_trade.strategies.base import StrategyEvaluation
from mike_trade.strategies.registry import StrategyRegistry

__all__ = [
    "BacktestRequest",
    "BacktestResult",
    "BacktestRunner",
    "BacktestMetrics",
    "compute_metrics",
]

ZERO = Decimal("0")


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


class BacktestRunner:
    """Run registered strategies over a supplied replay feed and simulator."""

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
        strategies = []
        for strategy_id in request.strategy_ids:
            cls = self.registry.get(strategy_id, request.strategy_versions.get(strategy_id))
            strategies.append(cls)
        for symbol in request.symbols:
            self.scheduler.watch(symbol)
            for cls in strategies:
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
        evaluations: list[Mapping[str, Any]] = []
        signals: list[Mapping[str, Any]] = []
        intents: list[Mapping[str, Any]] = []
        fills: list[Mapping[str, Any]] = []
        cycles: list[dict[str, Any]] = []
        equity_curve: list[dict[str, Any]] = []
        cash = request.starting_capital
        quantities: dict[str, Decimal] = defaultdict(lambda: ZERO)
        marks: dict[str, Decimal] = {}
        open_cycles: dict[str, dict[str, Any]] = {}
        candles_by_symbol: dict[str, list[Candle]] = defaultdict(list)
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
        params = {
            sid: self.registry.validate_parameters(
                sid, request.strategy_params.get(sid, {}), request.strategy_versions.get(sid)
            )
            for sid in request.strategy_ids
        }
        for ev in replay_events:
            assert ev.available_at
            self.clock.advance_to(ev.available_at)
            outputs = self.execution.process_event(ev)
            for x in outputs:
                fill_row = {
                    "symbol": x.symbol,
                    "side": x.side.value,
                    "quantity": x.quantity,
                    "price": x.price,
                    "fee": x.fee,
                    "time": x.fill.filled_at,
                    "order_id": x.fill.broker_order_id,
                    "evidence": x.evidence,
                }
                fills.append(fill_row)
                signed = x.quantity if x.side.value == "BUY" else -x.quantity
                cash += (
                    -(x.quantity * x.price + x.fee)
                    if x.side.value == "BUY"
                    else x.quantity * x.price - x.fee
                )
                quantities[x.symbol] += signed
                marks[x.symbol] = x.price
                if x.side.value == "BUY":
                    session_key = x.fill.filled_at.astimezone(NEW_YORK).date().isoformat()
                    entries_by_session[(x.symbol, session_key)] += 1
                    if x.symbol not in open_cycles:
                        open_cycles[x.symbol] = {
                            "cycle_id": f"{run_id}:{x.symbol}:{len(cycles) + 1}",
                            "symbol": x.symbol,
                            "opened_at": x.fill.filled_at,
                            "closed_at": None,
                            "entry_quantity": ZERO,
                            "exit_quantity": ZERO,
                        }
                    open_cycles[x.symbol]["entry_quantity"] += x.quantity
                    trail_percent = request.config.get("protective_trailing_percent")
                    if trail_percent is not None:
                        cycle_id = str(open_cycles[x.symbol]["cycle_id"])
                        stop_intent = OrderIntent(
                            intent_id=f"{run_id}:{x.symbol}:protection:{x.fill.filled_at.isoformat()}",
                            client_order_id=f"{run_id[:20]}-protect-{x.symbol}-{len(fills)}",
                            account_scope_id=str(
                                request.config.get("research_scope_id", "backtest:research")
                            ),
                            cycle_id=cycle_id,
                            symbol=x.symbol,
                            side=OrderSide.SELL,
                            purpose=OrderPurpose.PROTECTIVE_STOP,
                            order_type=OrderType.TRAILING_STOP,
                            time_in_force=TimeInForce.DAY,
                            created_at=self.clock.now(),
                            quantity=x.quantity,
                            trail_percent=Decimal(str(trail_percent)),
                        )
                        await self.execution.submit_order(stop_intent)
                elif x.symbol in open_cycles:
                    open_cycles[x.symbol]["exit_quantity"] += x.quantity
                    if quantities[x.symbol] <= ZERO:
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
            marks[ev.symbol] = ev.candle.close
            equity_curve.append(
                {
                    "time": ev.available_at,
                    "cash": cash,
                    "equity": cash
                    + sum((qty * marks.get(sym, ZERO) for sym, qty in quantities.items()), ZERO),
                }
            )
            candles_by_symbol[ev.symbol].append(ev.candle)
            session_date = ev.candle.start.astimezone(NEW_YORK).date().isoformat()
            previous_session = vwap_session.get(ev.symbol)
            if previous_session is None or previous_session[0] != session_date:
                weighted, volume, prior_vwap = ZERO, ZERO, None
            else:
                _, weighted, volume, prior_vwap = previous_session
            if ev.candle.volume > ZERO:
                typical = (ev.candle.high + ev.candle.low + ev.candle.close) / Decimal(3)
                weighted += typical * ev.candle.volume
                volume += ev.candle.volume
            latest_vwap = weighted / volume if volume > ZERO else None
            vwap_session[ev.symbol] = (session_date, weighted, volume, latest_vwap)
            due = self.scheduler.process(
                CandleClosedEvent(
                    ev.symbol,
                    CandleTimeframe(ev.candle.timeframe.value),
                    ev.candle.end,
                    snapshot_version=len(candles_by_symbol[ev.symbol]),
                    due_at=ev.available_at,
                )
            )
            due_ids = {(item.symbol, item.strategy_id) for item in due}
            # Registry class specs drive closed-bar trigger cadence and evaluation.
            for cls in strategies:
                spec = cls.spec
                if (ev.symbol, spec.strategy_id) not in due_ids:
                    continue
                # Exit legs are lifecycle-bound: evaluating them while flat can
                # generate an impossible SELL signal with no managed position.
                if spec.side.value == "SELL" and quantities[ev.symbol] <= ZERO:
                    continue
                if (
                    spec.side.value == "BUY"
                    and request.config.get("entry_cutoff_minutes") is not None
                ):
                    session = session_for(ev.available_at.astimezone(NEW_YORK).date())
                    cutoff = entry_cutoff(session, int(request.config["entry_cutoff_minutes"]))
                    if ev.available_at >= cutoff:
                        continue
                if (
                    spec.side.value == "BUY"
                    and request.config.get("max_entry_cycles_per_session") is not None
                ):
                    session_key = ev.available_at.astimezone(NEW_YORK).date().isoformat()
                    max_cycles = int(request.config["max_entry_cycles_per_session"])
                    if entries_by_session[(ev.symbol, session_key)] >= max_cycles:
                        continue
                if (
                    spec.side.value == "SELL"
                    and request.config.get("minimum_hold_seconds") is not None
                ):
                    opened = open_cycles.get(ev.symbol, {}).get("opened_at")
                    if opened is not None and (ev.available_at - opened).total_seconds() < float(
                        request.config["minimum_hold_seconds"]
                    ):
                        continue
                if ev.candle.timeframe.value != spec.trigger_timeframe.value or len(
                    candles_by_symbol[ev.symbol]
                ) < max(1, spec.warmup_bars):
                    continue
                key = (ev.symbol, spec.strategy_id, ev.candle.start)
                if key in trigger_rows:
                    continue
                trigger_rows.add(key)
                history = tuple(candles_by_symbol[ev.symbol][-max(spec.warmup_bars + 1, 1) :])
                spec_tf = CandleTimeframe(spec.trigger_timeframe.value)
                converted = tuple(
                    StrategyCandle(
                        c.symbol,
                        spec_tf,
                        c.start,
                        c.end,
                        c.open,
                        c.high,
                        c.low,
                        c.close,
                        c.volume,
                        c.provenance.feed,
                        c.provenance.is_consolidated,
                        CandleOrigin.REPLAY,
                        PriceBasis.RAW,
                        CandleStatus.CLOSED,
                        c.trade_count,
                    )
                    for c in history
                )
                session_status = self.clock.session_status()
                age = ev.available_at - ev.candle.end
                freshness = ContextFreshness(
                    ev.candle.end, max(age, timedelta(0)), timedelta(minutes=5), False, None
                )
                context = MarketContext(
                    ev.symbol,
                    ev.available_at,
                    len(candles_by_symbol[ev.symbol]),
                    provenance.feed,
                    provenance.is_consolidated,
                    PriceBasis.RAW,
                    session_status,
                    ev.available_at.astimezone(NEW_YORK).date().isoformat(),
                    freshness,
                    {spec_tf: converted},
                    {"vwap": latest_vwap, "vwap_prev": prior_vwap},
                )
                strategy = cls()
                evaluation: StrategyEvaluation = strategy.evaluate(
                    context, params[spec.strategy_id], state[(ev.symbol, spec.strategy_id)]
                )
                state[(ev.symbol, spec.strategy_id)].update(evaluation.state_updates)
                row = {
                    "symbol": ev.symbol,
                    "strategy": spec.strategy_id,
                    "version": spec.version,
                    "time": ev.available_at,
                    "bar_time": ev.candle.start,
                    "signal_id": evaluation.signal_id,
                    "reason_codes": [x.value for x in evaluation.reason_codes],
                }
                evaluations.append(row)
                if evaluation.is_signal:
                    signals.append(row)
                    if self.intent_factory is not None:
                        intent = self.intent_factory(evaluation, context, request)
                        intents.append(
                            {
                                "symbol": ev.symbol,
                                "strategy": spec.strategy_id,
                                "time": ev.available_at,
                                "intent_id": getattr(intent, "intent_id", None),
                            }
                        )
                        order = await self.execution.submit_order(intent)
                        self.execution.process_event(ev)
            # Other event kinds (quotes/splits/data-quality) are processed in chronology too.
        payload = {
            "evaluations": evaluations,
            "signals": signals,
            "intents": intents,
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
        )
