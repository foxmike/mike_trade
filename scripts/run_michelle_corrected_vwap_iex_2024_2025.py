#!/usr/bin/env python3
"""Run a pinned corrected-Michelle portfolio campaign offline."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import shutil
import sys
import threading
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from mike_trade.backtest.clock import ReplayClock
from mike_trade.backtest.execution import HistoricalExecutionSimulator
from mike_trade.backtest.feed import (
    DataProvenance,
    HistoricalReplayFeed,
    QuoteFidelity,
    ReplayEvent,
)
from mike_trade.backtest.manifest import PeriodRole
from mike_trade.backtest.runner import BacktestRequest, BacktestRunner, compute_metrics
from mike_trade.domain.market import Candle, FeedProvenance, Timeframe
from mike_trade.domain.strategy_types import StrategyRole
from mike_trade.domain.trading import (
    OrderIntent,
    OrderPurpose,
    OrderSide,
    OrderType,
    TimeInForce,
)
from mike_trade.research.commands import _research_registry, resolve_execution_assumptions
from mike_trade.research.datasets import HistoricalDatasetRepository
from mike_trade.risk.daily_loss import DEFAULT_DAILY_LOSS_LIMIT
from mike_trade.risk.michelle_corrected_stop import ACTIVATION_GAIN, INITIAL_STOP_OFFSET
from mike_trade.scheduling.calendar import NEW_YORK, session_for


DEFAULT_CONFIG = Path(
    "config/research_campaigns/michelle_corrected_vwap_iex_2024_2025_v1.json"
)
ZERO = Decimal("0")


def _moment(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware: {value}")
    return result.astimezone(UTC)


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(_json_value(value), indent=2, sort_keys=True) + "\n")


def _write_csv(path: Path, rows: list[Mapping[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _json_value(row.get(key)) for key in fields})


def _expected_sessions(start: datetime, end: datetime) -> set[str]:
    result: set[str] = set()
    current = start.astimezone(NEW_YORK).date()
    final = (end - timedelta(microseconds=1)).astimezone(NEW_YORK).date()
    while current <= final:
        try:
            session_for(current)
        except ValueError:
            pass
        else:
            session = session_for(current)
            if session.open_utc < end and session.close_utc > start:
                result.add(current.isoformat())
        current += timedelta(days=1)
    return result


def _drawdown(pnls: Iterable[Decimal], initial: Decimal = ZERO) -> Decimal:
    equity = peak = initial
    worst = ZERO
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def _profit_factor(trades: list[Mapping[str, Any]]) -> Decimal | None:
    wins = sum((Decimal(str(row["pnl"])) for row in trades if Decimal(str(row["pnl"])) > 0), ZERO)
    losses = -sum((Decimal(str(row["pnl"])) for row in trades if Decimal(str(row["pnl"])) < 0), ZERO)
    return None if losses == ZERO else wins / losses


def _duration(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _heartbeat(
    stop: threading.Event,
    seconds: int,
    started: float,
    clock: ReplayClock,
    session_dates: tuple[str, ...],
) -> None:
    while not stop.wait(seconds):
        elapsed = time.monotonic() - started
        replay_time = clock.now()
        completed = sum(
            session_for(date.fromisoformat(item)).close_utc <= replay_time
            for item in session_dates
        )
        total = len(session_dates)
        fraction = completed / total if total else 0.0
        eta = elapsed * (1.0 - fraction) / fraction if fraction > 0 else 0.0
        print(
            f"[replay] date={replay_time.astimezone(NEW_YORK).date()} "
            f"sessions={completed}/{total} progress={fraction:.1%} "
            f"elapsed={_duration(elapsed)} ETA={_duration(eta) if fraction > 0 else 'N/A'}",
            flush=True,
        )


def _configured_symbols(config: Mapping[str, Any]) -> tuple[str, ...]:
    configured = config.get("symbols")
    if configured is not None:
        symbols = tuple(str(symbol).upper() for symbol in configured)
    else:
        universe = json.loads(Path(str(config["universe_file"])).read_text())
        symbols = tuple(str(item["symbol"]).upper() for item in universe["symbols"])
    if len(symbols) != int(config["symbols_expected"]) or len(set(symbols)) != len(symbols):
        raise ValueError("symbol count/uniqueness does not match campaign configuration")
    return symbols


def _reference_symbols(config: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(symbol).upper() for symbol in config.get("reference_symbols", ()))


class _Tee:
    def __init__(self, *streams: Any) -> None:
        self.streams = streams

    def write(self, value: str) -> int:
        for stream in self.streams:
            stream.write(value)
            stream.flush()
        return len(value)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def _report(
    output: Path,
    config: Mapping[str, Any],
    result: Any,
    coverage: list[Mapping[str, Any]],
) -> None:
    starting = Decimal(str(config["portfolio"]["starting_capital"]))
    metrics = compute_metrics(result, starting_capital=starting, seed=int(config["runner"]["seed"]))
    trades = [dict(row) for row in metrics.trades]
    for trade in trades:
        trade["shares"] = trade.get("quantity")
    trades.sort(key=lambda row: (row["exit_time"], row["symbol"], row["entry_time"]))
    trade_fields = [
        "symbol", "entry_time", "entry_price", "exit_time", "exit_price", "shares", "quantity",
        "gross_pnl", "costs", "pnl", "exit_reason", "entry_strategy", "exit_strategy",
        "hold_duration_seconds",
    ]
    _write_json(output / "trades.json", trades)
    _write_csv(output / "trades.csv", trades, trade_fields)

    by_symbol: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for trade in trades:
        by_symbol[str(trade["symbol"])].append(trade)
    symbols = _configured_symbols(config)
    symbol_rows: list[dict[str, Any]] = []
    for symbol in symbols:
        rows = by_symbol[symbol]
        pnls = [Decimal(str(row["pnl"])) for row in rows]
        wins = sum(pnl > ZERO for pnl in pnls)
        losses = sum(pnl < ZERO for pnl in pnls)
        symbol_rows.append({
            "symbol": symbol,
            "trades": len(rows),
            "wins": wins,
            "losses": losses,
            "win_rate": ZERO if not rows else Decimal(wins) / len(rows),
            "net_profit": sum(pnls, ZERO),
            "profit_factor": _profit_factor(rows),
            "maximum_drawdown": _drawdown(pnls),
        })
    _write_json(output / "symbol_summary.json", symbol_rows)
    _write_csv(
        output / "symbol_summary.csv", symbol_rows,
        ["symbol", "trades", "wins", "losses", "win_rate", "net_profit", "profit_factor", "maximum_drawdown"],
    )

    daily: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for trade in trades:
        day = trade["exit_time"].astimezone(NEW_YORK).date().isoformat()
        daily[day] += Decimal(str(trade["pnl"]))
    daily_rows = [{"date": day, "net_pnl": pnl} for day, pnl in sorted(daily.items())]
    _write_json(output / "daily_pnl.json", daily_rows)
    _write_csv(output / "daily_pnl.csv", daily_rows, ["date", "net_pnl"])

    curve = sorted(result.equity_curve, key=lambda row: row["time"])
    _write_json(output / "equity_curve.json", curve)
    _write_csv(output / "equity_curve.csv", curve, ["time", "equity"])
    peak = starting
    maximum_drawdown = ZERO
    for point in curve:
        equity = Decimal(str(point["equity"]))
        peak = max(peak, equity)
        if peak > ZERO:
            maximum_drawdown = max(maximum_drawdown, (peak - equity) / peak)
    net_profit = sum((Decimal(str(row["pnl"])) for row in trades), ZERO)
    final_equity = Decimal(str(curve[-1]["equity"])) if curve else starting + net_profit
    wins = sum(Decimal(str(row["pnl"])) > ZERO for row in trades)
    portfolio = {
        "campaign_id": config["campaign_id"],
        "run_id": result.run_id,
        "initial_equity": starting,
        "final_equity": final_equity,
        "net_profit": net_profit,
        "total_return": (final_equity - starting) / starting,
        "total_trades": len(trades),
        "wins": wins,
        "losses": sum(Decimal(str(row["pnl"])) < ZERO for row in trades),
        "win_rate": ZERO if not trades else Decimal(wins) / len(trades),
        "profit_factor": _profit_factor(trades),
        "maximum_drawdown": maximum_drawdown,
        "daily_pnl_file": "daily_pnl.csv",
        "symbols_processed": sum(row["role"] == "traded" for row in coverage),
        "symbols_with_missing_sessions": sum(
            row["role"] == "traded" and bool(row["missing_regular_sessions"])
            for row in coverage
        ),
    }
    _write_json(output / "portfolio_summary.json", portfolio)
    _write_json(output / "data_coverage.json", coverage)

    table = "\n".join(
        f"| {row['symbol']} | {row['trades']} | {row['wins']} | {row['losses']} | "
        f"{row['win_rate']} | {row['net_profit']} | {row['profit_factor'] or 'N/A'} | {row['maximum_drawdown']} |"
        for row in symbol_rows
    )
    missing = [
        row for row in coverage
        if row["role"] == "traded" and row["missing_regular_sessions"]
    ]
    limitations = "\n".join(
        f"- {item}" for item in config.get("known_data_limitations", ())
    ) or "- None declared."
    report = f"""# Michelle corrected VWAP portfolio backtest

## Portfolio

| Initial equity | Final equity | Return | Trades | Win rate | Profit factor | Maximum drawdown |
|---:|---:|---:|---:|---:|---:|---:|
| {portfolio['initial_equity']} | {portfolio['final_equity']} | {portfolio['total_return']} | {portfolio['total_trades']} | {portfolio['win_rate']} | {portfolio['profit_factor'] or 'N/A'} | {portfolio['maximum_drawdown']} |

## Per symbol

| Symbol | Trades | Wins | Losses | Win rate | Net profit | Profit factor | Maximum realized drawdown ($) |
|---|---:|---:|---:|---:|---:|---:|---:|
{table}

## Processing coverage

- Symbols requested: {config['symbols_expected']}
- Traded symbols processed: {sum(row['role'] == 'traded' for row in coverage)}
- Market-context symbols loaded: {sum(row['role'] == 'market_context' for row in coverage)}
- Symbols with a regular session containing no IEX observations: {len(missing)}
- Exact missing dates, if any: `data_coverage.json`
- Actual first/last observations and pinned dataset IDs: `data_coverage.json`
- Immutable engine ledger and manifest: `executions/`
- Trade-level inspection and chart data remain available through the existing research dashboard.

## Known data and execution limitations

{limitations}
"""
    (output / "report.md").write_text(report, encoding="utf-8")


async def _run(config: Mapping[str, Any], output: Path) -> None:
    start = _moment(str(config["window"]["start"]))
    end = _moment(str(config["window"]["end"]))
    symbols = _configured_symbols(config)
    references = _reference_symbols(config)
    data_symbols = tuple(dict.fromkeys((*symbols, *references)))

    registry = _research_registry()
    buy_id = str(config["strategies"]["buy"])
    sell_id = str(config["strategies"]["sell"])
    if registry.get(buy_id).spec.side is not StrategyRole.BUY:
        raise ValueError(f"{buy_id} is not the registered BUY strategy")
    if registry.get(sell_id).spec.side is not StrategyRole.SELL:
        raise ValueError(f"{sell_id} is not the registered SELL strategy")
    if Decimal(str(config["risk"]["initial_stop_amount"])) != INITIAL_STOP_OFFSET:
        raise ValueError("campaign initial stop does not match the corrected strategy")
    if Decimal(str(config["risk"]["breakeven_trigger_amount"])) != ACTIVATION_GAIN:
        raise ValueError("campaign breakeven trigger does not match the corrected strategy")
    if Decimal(str(config["portfolio"]["account_daily_loss_limit"])) != DEFAULT_DAILY_LOSS_LIMIT:
        raise ValueError("campaign daily loss limit does not match the corrected strategy")

    repo = HistoricalDatasetRepository(str(config["data"]["root"]))
    snapshots = {snapshot.dataset_id: snapshot for snapshot in repo.snapshots()}
    pinned = config["data"]["dataset_ids"]
    events: list[ReplayEvent] = []
    coverage: list[dict[str, Any]] = []
    expected_sessions = _expected_sessions(start, end)
    provenance = FeedProvenance("replay", "IEX", "alpaca", False)
    for index, symbol in enumerate(data_symbols, 1):
        dataset_id = str(pinned[symbol])
        snapshot = snapshots.get(dataset_id)
        if snapshot is None or snapshot.symbol != symbol:
            raise ValueError(f"missing pinned dataset for {symbol}: {dataset_id}")
        if snapshot.feed != "iex" or snapshot.adjustment != "raw" or snapshot.timeframe != "1m":
            raise ValueError(f"pinned dataset has wrong provenance for {symbol}: {dataset_id}")
        bars = repo.available(symbol, start, end, feed="iex", dataset_id=dataset_id)
        if not bars:
            raise ValueError(f"pinned dataset has no observations for {symbol}")
        observed_sessions: set[str] = set()
        premarket_sessions: set[str] = set()
        regular_bars = 0
        premarket_bars = 0
        for bar in bars:
            try:
                session = session_for(bar.timestamp.astimezone(NEW_YORK).date())
            except ValueError:
                continue
            if session.open_utc <= bar.timestamp < session.close_utc:
                observed_sessions.add(session.session_date.isoformat())
                regular_bars += 1
            elif bar.timestamp < session.open_utc:
                premarket_sessions.add(session.session_date.isoformat())
                premarket_bars += 1
            events.append(ReplayEvent.bar(Candle(
                symbol, Timeframe.M1, bar.timestamp, bar.timestamp + timedelta(minutes=1),
                bar.open, bar.high, bar.low, bar.close, Decimal(bar.volume), provenance,
                bar.vwap, int(bar.trade_count) if bar.trade_count is not None else None,
            )))
        missing = sorted(expected_sessions - observed_sessions)
        coverage.append({
            "symbol": symbol, "role": "traded" if symbol in symbols else "market_context",
            "dataset_id": dataset_id, "observations": len(bars),
            "first_observation": bars[0].timestamp,
            "last_observation": bars[-1].timestamp,
            "premarket_observations": premarket_bars,
            "observed_premarket_sessions": len(premarket_sessions),
            "regular_session_observations": regular_bars,
            "observed_regular_sessions": len(observed_sessions),
            "expected_regular_sessions": len(expected_sessions),
            "missing_regular_sessions": missing,
        })
        print(f"[data {index:03d}/{len(data_symbols)}] {symbol} observations={len(bars)} missing_sessions={len(missing)}", flush=True)

    clock = ReplayClock(start)
    feed = HistoricalReplayFeed(
        events,
        DataProvenance("alpaca", "IEX", False, str(config["campaign_id"]), "RAW", False, QuoteFidelity.UNAVAILABLE),
        clock=clock,
        seed=int(config["runner"]["seed"]),
        max_events=int(config["runner"]["max_events"]),
    )
    assumptions = resolve_execution_assumptions(config["execution"])
    simulator = HistoricalExecutionSimulator(feed, clock, assumptions)
    sizing = config["position_sizing"]
    max_allocation = Decimal(str(sizing["maximum_capital_allocation"]))
    planned_loss = Decimal(str(sizing["planned_loss_budget"]))
    sizing_fraction = Decimal(str(sizing["sizing_distance_percent"])) / Decimal("100")

    def intent_factory(evaluation: Any, context: Any, request: Any) -> OrderIntent:
        entry = evaluation.side is StrategyRole.BUY
        if entry:
            reference = evaluation.reference_price
            if reference is None or reference <= ZERO:
                raise ValueError("entry signal has no valid reference price")
            quantity = min(
                max_allocation / reference,
                planned_loss / (reference * sizing_fraction),
            ).to_integral_value(rounding=ROUND_DOWN)
        else:
            quantity = simulator.held_quantity(evaluation.symbol)
        return OrderIntent(
            intent_id=str(evaluation.signal_id),
            client_order_id=f"research-{evaluation.signal_id}",
            account_scope_id="backtest:michelle-corrected-portfolio",
            cycle_id=f"research:{evaluation.symbol}",
            symbol=evaluation.symbol,
            side=OrderSide.BUY if entry else OrderSide.SELL,
            purpose=OrderPurpose.ENTRY if entry else OrderPurpose.STRATEGY_EXIT,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            created_at=context.as_of,
            quantity=quantity,
        )

    request = BacktestRequest(
        symbols,
        start,
        end,
        PeriodRole.RESEARCH,
        {
            "mode": "BACKTEST",
            "lifecycle": "collection",
            "run_purpose": config["campaign_id"],
            "dataset_ids": dict(pinned),
            "end_of_day_close": {"enabled": True, "at": "15:55"},
            "max_gross_exposure": str(config["portfolio"]["max_gross_exposure"]),
            "short_selling_enabled": False,
            "position_sizing": dict(sizing),
            "execution_model": "bar-only next-bar-open",
            "assumptions": dict(assumptions.as_mapping()),
        },
        int(config["runner"]["seed"]),
        Decimal(str(config["portfolio"]["starting_capital"])),
        (buy_id, sell_id),
        strategy_params={buy_id: {}, sell_id: {}},
        initial_strategy_state={buy_id: {"eligibility": "WATCHING_BUY"}},
        context_history_bars=int(config["runner"]["context_history_bars"]),
        result_directory=output,
    )
    runner = BacktestRunner(registry, feed, clock, simulator, intent_factory=intent_factory)
    started = time.monotonic()
    heartbeat_stop = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat,
        args=(
            heartbeat_stop,
            int(config["runner"]["heartbeat_seconds"]),
            started,
            clock,
            tuple(sorted(expected_sessions)),
        ),
        daemon=True,
    )
    heartbeat.start()
    try:
        result = await runner.run(request)
    finally:
        heartbeat_stop.set()
        heartbeat.join(timeout=1)
    print(f"[complete] run_id={result.run_id} elapsed_seconds={int(time.monotonic()-started)}", flush=True)
    _report(output, config, result, coverage)


def _preflight(config: Mapping[str, Any]) -> None:
    start = _moment(str(config["window"]["start"]))
    end = _moment(str(config["window"]["end"]))
    if start >= end or not bool(config["window"].get("end_exclusive")):
        raise ValueError("campaign requires a nonempty, end-exclusive window")
    symbols = _configured_symbols(config)
    references = _reference_symbols(config)
    if set(symbols) & set(references):
        raise ValueError("market-context symbols must not also be traded symbols")
    if config["data"]["provider"] != "alpaca" or config["data"]["feed"] != "iex":
        raise ValueError("campaign requires pinned Alpaca IEX data")
    if config["data"]["adjustment"] != "raw" or config["data"]["timeframe"] != "1m":
        raise ValueError("campaign requires raw one-minute data")

    registry = _research_registry()
    buy_id = str(config["strategies"]["buy"])
    sell_id = str(config["strategies"]["sell"])
    buy = registry.get(buy_id)
    sell = registry.get(sell_id)
    if buy.spec.side is not StrategyRole.BUY or sell.spec.side is not StrategyRole.SELL:
        raise ValueError("configured strategy roles are invalid")
    if not set(references).issuperset(buy.spec.reference_symbols):
        raise ValueError("campaign does not supply every BUY market-context symbol")
    if Decimal(str(config["risk"]["initial_stop_amount"])) != INITIAL_STOP_OFFSET:
        raise ValueError("campaign initial stop does not match the corrected strategy")
    if Decimal(str(config["risk"]["breakeven_trigger_amount"])) != ACTIVATION_GAIN:
        raise ValueError("campaign breakeven trigger does not match the corrected strategy")
    if Decimal(str(config["portfolio"]["account_daily_loss_limit"])) != DEFAULT_DAILY_LOSS_LIMIT:
        raise ValueError("campaign daily loss limit does not match the corrected strategy")

    repo = HistoricalDatasetRepository(str(config["data"]["root"]))
    snapshots = {snapshot.dataset_id: snapshot for snapshot in repo.snapshots()}
    pinned = config["data"]["dataset_ids"]
    for symbol in (*symbols, *references):
        dataset_id = str(pinned[symbol])
        snapshot = snapshots.get(dataset_id)
        if snapshot is None or snapshot.symbol != symbol:
            raise ValueError(f"missing pinned dataset for {symbol}: {dataset_id}")
        if snapshot.feed != "iex" or snapshot.adjustment != "raw" or snapshot.timeframe != "1m":
            raise ValueError(f"pinned dataset has wrong provenance for {symbol}: {dataset_id}")
        snapshot_start = _moment(str(snapshot.start))
        snapshot_end = _moment(str(snapshot.end))
        if snapshot_start > start or snapshot_end < end:
            raise ValueError(f"pinned dataset metadata does not span the window: {dataset_id}")
    assumptions = resolve_execution_assumptions(config["execution"])
    if Decimal(str(assumptions.slippage_bps)) != Decimal("5"):
        raise ValueError("campaign must retain 5 bps slippage")
    print(
        f"[preflight] ok campaign={config['campaign_id']} traded={','.join(symbols)} "
        f"context={','.join(references)} window={start.isoformat()}..{end.isoformat()} "
        f"buy={buy_id} sell={sell_id}",
        flush=True,
    )
    for warning in config.get("known_data_limitations", ()):
        print(f"[preflight warning] {warning}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="validate configuration, registrations, and pinned dataset metadata without replay",
    )
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.preflight:
        _preflight(config)
        return 0
    if args.output is None:
        parser.error("--output is required unless --preflight is used")
    args.output.mkdir(parents=True, exist_ok=False)
    log_stream = (args.output / "run.log").open("a", encoding="utf-8", buffering=1)
    sys.stdout = _Tee(sys.stdout, log_stream)
    sys.stderr = _Tee(sys.stderr, log_stream)
    shutil.copy2(args.config, args.output / "campaign_config.json")
    _write_json(args.output / "run_info.json", {
        "started_at": datetime.now(UTC), "pid": os.getpid(), "config": str(args.config),
    })
    print(f"[start] campaign={config['campaign_id']} output={args.output}", flush=True)
    try:
        asyncio.run(_run(config, args.output))
    except BaseException as exc:
        _write_json(args.output / "failure.json", {
            "failed_at": datetime.now(UTC), "exception_type": type(exc).__name__,
            "exception_message": str(exc),
        })
        raise
    _write_json(args.output / "success.json", {"completed_at": datetime.now(UTC)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
