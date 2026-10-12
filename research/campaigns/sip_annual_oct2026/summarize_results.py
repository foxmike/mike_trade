"""Summarize a completed or partially completed SIP campaign batch."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from mike_trade.risk.fixed_stop import ALPACA_EQUITY_TICK_RULES
from mike_trade.scheduling.calendar import NEW_YORK


ZERO = Decimal(0)
MICHELLE_REFS = {
    "five_minute_orb_michelle_buy": "entry_orb_high",
    "vwap_bounce_break_michelle_buy": "position_vwap_at_entry",
}


def dec(value: Any) -> Decimal:
    return Decimal(str(value))


def mean(values: list[Decimal]) -> str | None:
    return str(sum(values, ZERO) / len(values)) if values else None


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def verify_michelle(artifact: dict[str, Any]) -> dict[str, Any]:
    """Check fill-owned policy, initial stop, and monotonic recorded movement."""
    events = defaultdict(list)
    for event in artifact.get("protection_events", []):
        events[str(event["cycle_id"])].append(event)
    checked = 0
    moved = 0
    errors = []
    for cycle in artifact.get("cycles", []):
        buy = cycle.get("entry_strategy")
        if buy not in MICHELLE_REFS:
            continue
        checked += 1
        cid = str(cycle["cycle_id"])
        if cycle.get("protection_mode") != "FIXED_THEN_TRAILING":
            errors.append(f"{cid}: wrong protection mode {cycle.get('protection_mode')}")
        reference = cycle.get(MICHELLE_REFS[buy])
        initial = [e for e in events[cid] if e.get("event_type") == "INITIAL_STOP_ESTABLISHED"]
        if reference is None or not initial:
            errors.append(f"{cid}: missing fill reference or initial protective order")
            continue
        expected = ALPACA_EQUITY_TICK_RULES.floor(dec(reference) - Decimal("0.25"))
        actual = dec(initial[0]["stop_price"])
        if actual != expected:
            errors.append(f"{cid}: initial stop {actual} != {expected} from {buy}")
        if initial[0].get("protection_mode") != "FIXED_THEN_TRAILING":
            errors.append(f"{cid}: initial event used wrong policy")
        last = actual
        for event in events[cid]:
            if event.get("event_type") != "STOP_MOVED":
                continue
            moved += 1
            price = dec(event["new_stop_price"])
            if event.get("protection_mode") != "FIXED_THEN_TRAILING" or price < last:
                errors.append(f"{cid}: non-monotonic or wrong-policy stop movement")
            last = price
    return {"cycles_checked": checked, "stop_moves_checked": moved, "errors": errors,
            "verified": checked > 0, "passed": not errors}


def summarize(batch: dict[str, Any], jobs: list[dict[str, Any]], manifest: dict[str, Any],
              result_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_index = {int(row["job_index"]): row for row in batch.get("jobs", [])}
    artifact_paths = {path.name.removesuffix(".results.json"): path
                      for path in (result_dir / "executions").glob("*/*.results.json")}
    config_meta = {c["name"]: c for c in manifest["configurations"]}
    output = []
    all_contrib = {}
    michelle_audit = {}
    for index, job in enumerate(jobs):
        result = by_index.get(index, {"status": "not_run"})
        name = job["strategy"]
        meta = config_meta[name]
        row: dict[str, Any] = {
            "job_index": index, "symbol": job["symbol"], "configuration": name,
            "category": meta["category"], "buy_strategies": ",".join(job["buy_strategies"]),
            "sell_strategies": ",".join(job["sell_strategies"] or []),
            "protective_stop": "michelle_fixed_then_trailing" if meta["stop"] is None
                               else json.dumps(meta["stop"], sort_keys=True),
            "dataset_id": job["dataset_id"], "status": result.get("status", "not_run"),
            "error": result.get("error"), "completed_trades": None,
            "win_rate": None, "gross_profit": None, "gross_loss": None,
            "net_pnl": None, "total_return": None, "profit_factor": None,
            "max_drawdown": None, "avg_mfe": None, "avg_mae": None,
            "exit_reasons": None, "eod_exits": None, "protective_stop_exits": None,
            "technical_sell_exits": None, "avg_hold_seconds": None,
            "median_hold_seconds": None, "days_with_trades": None,
            "avg_trades_per_active_day": None, "buy_winner_counts": None,
            "sell_winner_counts": None,
        }
        if result.get("status") != "completed":
            output.append(row)
            continue
        ledger_path = Path(result["trade_ledger_file"])
        if not ledger_path.is_file():
            ledger_path = result_dir / "trade_ledgers" / f"{result['run_id']}.json"
        ledger = json.loads(ledger_path.read_text())
        if ledger["run_id"] != result["run_id"] or ledger["dataset_id"] != job["dataset_id"]:
            raise ValueError(f"job {index}: ledger identity mismatch")
        trades = ledger["trades"]
        if len(trades) != int(result["trades"]):
            raise ValueError(f"job {index}: ledger trade count mismatch")
        pnl = [dec(t["pnl"]) for t in trades]
        gross = [dec(t["gross_pnl"]) for t in trades]
        wins = sum(value > ZERO for value in pnl)
        gross_profit = sum((value for value in gross if value > ZERO), ZERO)
        gross_loss = -sum((value for value in gross if value < ZERO), ZERO)
        reasons = Counter(str(t.get("exit_reason") or "UNKNOWN") for t in trades)
        buys = Counter(str(t.get("entry_strategy") or "UNKNOWN") for t in trades)
        sells = Counter(str(t["exit_strategy"]) for t in trades if t.get("exit_strategy"))
        days = {parse_time(t["entry_time"]).astimezone(NEW_YORK).date().isoformat()
                for t in trades}
        buy_contrib = defaultdict(lambda: {"trades": 0, "net_pnl": ZERO})
        sell_contrib = defaultdict(lambda: {"trades": 0, "net_pnl": ZERO})
        for trade in trades:
            buyer = str(trade.get("entry_strategy") or "UNKNOWN")
            buy_contrib[buyer]["trades"] += 1
            buy_contrib[buyer]["net_pnl"] += dec(trade["pnl"])
            seller = trade.get("exit_strategy")
            if seller:
                sell_contrib[str(seller)]["trades"] += 1
                sell_contrib[str(seller)]["net_pnl"] += dec(trade["pnl"])
        all_contrib[f"{job['symbol']}:{name}"] = {
            side: {key: {"trades": value["trades"], "net_pnl": str(value["net_pnl"])}
                   for key, value in data.items()}
            for side, data in (("buy", buy_contrib), ("technical_sell", sell_contrib))
        }
        n = len(trades)
        row.update(
            status="no_trades" if n == 0 else "completed",
            completed_trades=n,
            win_rate=str(Decimal(wins) / n) if n else None,
            gross_profit=str(gross_profit), gross_loss=str(gross_loss),
            net_pnl=str(sum(pnl, ZERO)), total_return=result.get("net_return"),
            profit_factor=result.get("profit_factor") if n else None,
            max_drawdown=result.get("max_drawdown"),
            avg_mfe=mean([dec(t["mfe"]) for t in trades if t.get("mfe") is not None]),
            avg_mae=mean([dec(t["mae"]) for t in trades if t.get("mae") is not None]),
            exit_reasons=json.dumps(dict(sorted(reasons.items())), sort_keys=True),
            eod_exits=reasons.get("END_OF_DAY", 0),
            protective_stop_exits=reasons.get("PROTECTIVE_STOP", 0),
            technical_sell_exits=reasons.get("STRATEGY_SELL", 0),
            avg_hold_seconds=mean([dec(t["hold_duration_seconds"]) for t in trades]),
            median_hold_seconds=str(median(dec(t["hold_duration_seconds"]) for t in trades))
                                if n else None,
            days_with_trades=len(days),
            avg_trades_per_active_day=str(Decimal(n) / len(days)) if days else None,
            buy_winner_counts=json.dumps(dict(sorted(buys.items()))),
            sell_winner_counts=json.dumps(dict(sorted(sells.items()))),
        )
        if name.startswith("michelle_"):
            artifact_path = artifact_paths.get(result["run_id"])
            if artifact_path is None:
                raise ValueError(f"job {index}: immutable replay artifact missing")
            artifact = json.loads(artifact_path.read_text())
            if artifact.get("run_id") != result["run_id"]:
                raise ValueError(f"job {index}: immutable artifact run mismatch")
            michelle_audit[f"{job['symbol']}:{name}"] = verify_michelle(artifact)
        output.append(row)
    detail = {"strategy_contribution": all_contrib, "michelle_protection_audit": michelle_audit,
              "failed_jobs": [r for r in output if r["status"] in ("failed", "not_run")],
              "zero_trade_configurations": [f"{r['symbol']}:{r['configuration']}" for r in output
                                             if r["status"] == "no_trades"]}
    return output, detail


def markdown(rows: list[dict[str, Any]], detail: dict[str, Any], batch: dict[str, Any]) -> str:
    lines = ["# Annual SIP research results", "",
             f"Completed jobs: {batch.get('completed_count', 0)}; failed jobs: {batch.get('failed_count', 0)}; "
             f"zero-trade configurations: {len(detail['zero_trade_configurations'])}.", "",
             "Returns are fractions of $10,000 capital. P&L and MFE/MAE are USD; MAE is signed negative. "
             "N/A means unavailable or no completed trades. Rankings are descriptive, not holdout selections.", ""]
    def fmt(value, decimals=2):
        return "N/A" if value is None else f"{dec(value):,.{decimals}f}"
    for symbol in ("IBM", "NVDA", "TSLA"):
        subset = [r for r in rows if r["symbol"] == symbol]
        completed = [r for r in subset if r["status"] == "completed"]
        lines += [f"## {symbol}", "",
                  f"{len(completed)} trading configurations; "
                  f"{sum(r['status'] == 'no_trades' for r in subset)} with no trades; "
                  f"{sum(r['status'] in ('failed', 'not_run') for r in subset)} failed/not run.", "",
                  "### Highest development net P&L", "",
                  "| Configuration | Trades | Win rate | Net P&L | Return | Profit factor | Max drawdown |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for row in sorted(completed, key=lambda r: dec(r["net_pnl"]), reverse=True)[:10]:
            lines.append(f"| {row['configuration']} | {row['completed_trades']} | "
                         f"{fmt(dec(row['win_rate']) * 100)}% | {fmt(row['net_pnl'])} | "
                         f"{fmt(dec(row['total_return']) * 100)}% | {fmt(row['profit_factor'])} | "
                         f"{fmt(dec(row['max_drawdown']) * 100)}% |")
        lines += ["", "### Controlled technical SELL comparisons", "",
                  "| BUY family | SELL count | Trades | Net P&L | EOD | Stop | Technical SELL |",
                  "|---|---:|---:|---:|---:|---:|---:|"]
        for family in ("trend", "breakout", "vwap_mean", "price_action"):
            for n in range(4):
                row = next((r for r in subset if r["configuration"] == f"{family}_exit_{n}"), None)
                if row is None:
                    continue
                lines.append(f"| {family} | {n} | {row['completed_trades'] if row['completed_trades'] is not None else 'N/A'} | "
                             f"{fmt(row['net_pnl'])} | {row['eod_exits'] if row['eod_exits'] is not None else 'N/A'} | "
                             f"{row['protective_stop_exits'] if row['protective_stop_exits'] is not None else 'N/A'} | "
                             f"{row['technical_sell_exits'] if row['technical_sell_exits'] is not None else 'N/A'} |")
        lines += ["", "### Michelle and reference", "",
                  "| Configuration | Trades | Net P&L | ORB winners | VWAP winners | Protection check |",
                  "|---|---:|---:|---:|---:|---|"]
        for name in ("michelle_orb", "michelle_vwap", "michelle_combined", "vwap_reference"):
            row = next((r for r in subset if r["configuration"] == name), None)
            if row is None:
                continue
            buyers = json.loads(row["buy_winner_counts"] or "{}")
            check = detail["michelle_protection_audit"].get(f"{symbol}:{name}")
            check_text = ("N/A" if check is None or not check["verified"] else
                          f"{'PASS' if check['passed'] else 'FAIL'} ({check['cycles_checked']} cycles, {check['stop_moves_checked']} moves)")
            lines.append(f"| {name} | {row['completed_trades'] if row['completed_trades'] is not None else 'N/A'} | "
                         f"{fmt(row['net_pnl'])} | {buyers.get('five_minute_orb_michelle_buy', 0)} | "
                         f"{buyers.get('vwap_bounce_break_michelle_buy', 0)} | {check_text} |")
        lines.append("")
    lines += ["## Exceptions and files", "",
              f"Zero-trade configurations: {', '.join(detail['zero_trade_configurations']) or 'none'}.", "",
              "The complete batch table is in `summary.csv` and `summary.json`; per-strategy "
              "contributions and Michelle policy audit are in `summary.json`. Complete trade records "
              "and MFE/MAE are in `trade_ledgers/`; immutable execution details are in `executions/`."]
    if detail["failed_jobs"]:
        lines += ["", "Failed or absent jobs:", ""]
        for row in detail["failed_jobs"]:
            lines.append(f"- {row['symbol']} {row['configuration']}: {row['error'] or row['status']}")
    failed_stops = {name: value["errors"] for name, value in detail["michelle_protection_audit"].items()
                    if not value["passed"]}
    if failed_stops:
        lines += ["", "Michelle protection discrepancies:", ""]
        for name, errors in failed_stops.items():
            lines.append(f"- {name}: {'; '.join(errors)}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", required=True, type=Path)
    parser.add_argument("--experiments", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    args = parser.parse_args()
    batch = json.loads(args.batch.read_text())
    jobs = json.loads(args.experiments.read_text())["experiments"]
    manifest = json.loads(args.manifest.read_text())
    rows, detail = summarize(batch, jobs, manifest, args.results)
    args.results.mkdir(parents=True, exist_ok=True)
    with (args.results / "summary.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (args.results / "summary.json").write_text(
        json.dumps({"rows": rows, **detail}, indent=2, sort_keys=True) + "\n")
    (args.results / "summary.md").write_text(markdown(rows, detail, batch))
    bad_protection = sum(not value["passed"] for value in detail["michelle_protection_audit"].values())
    print(f"summary: {len(rows)} jobs, {len(detail['zero_trade_configurations'])} no-trade, "
          f"{len(detail['failed_jobs'])} failed/not-run, {bad_protection} Michelle protection failures")
    return 1 if bad_protection else 0


if __name__ == "__main__":
    raise SystemExit(main())
