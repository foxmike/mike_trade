"""Report for the nine stop-clarification runs, compared with the previous zero-slippage baseline.

OLD: reference-based $0.25 initial stop (ORB high / fill VWAP), 0.5% trail.
NEW: fill-based $0.25 initial stop, 0.2% trail, activation above the initial stop.
Both scenarios assume zero slippage and zero fees.

Entry rejections have no cycle (the fill never happened), so they carry no ``cycle_id``; they are
counted separately and validated, while every actual trading cycle is audited strictly.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from mike_trade.risk.fixed_stop import ALPACA_EQUITY_TICK_RULES

ZERO = Decimal(0)
OFFSET = Decimal("0.25")
TRAIL = Decimal("0.002")
REJECTION_EVENT = "ENTRY_REJECTED_PROTECTION_INVALID"
MICHELLE_BUYS = {"five_minute_orb_michelle_buy", "vwap_bounce_break_michelle_buy"}


def dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def fmt(value: Any, places: int = 2, pct: bool = False) -> str:
    number = dec(value)
    if number is None:
        return "N/A"
    return f"{number * 100:,.{places}f}%" if pct else f"{number:,.{places}f}"


def expected_initial(fill: Decimal) -> Decimal:
    """Independent re-derivation: fill - 0.25 rounded UP to the tick grid."""
    return ALPACA_EQUITY_TICK_RULES.ceil(fill - OFFSET)


def expected_candidate(highest: Decimal) -> Decimal:
    """0.2% below the highest traded price, rounded DOWN to the tick grid."""
    return ALPACA_EQUITY_TICK_RULES.floor(highest * (Decimal(1) - TRAIL))


def audit_artifact(artifact: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Count atomic entry rejections and strictly audit every actual Michelle cycle."""
    events: dict[str, list[dict[str, Any]]] = {}
    rejections, errors = 0, []
    for event in artifact.get("protection_events", []):
        cycle_id = event.get("cycle_id")
        if event.get("event_type") == REJECTION_EVENT and not cycle_id:
            entry, stop = dec(event.get("candidate_entry_price")), dec(event.get("initial_stop_price"))
            # Fill-derived: either no valid stop exists (None) or it is not below the fill.
            if event.get("reason") != "ENTRY_REJECTED_PROTECTIVE_STOP_NOT_BELOW_FILL" or entry is None or (
                stop is not None and stop < entry
            ):
                errors.append(f"malformed atomic rejection {event.get('order_id')}: {event}")
            rejections += 1
        elif not cycle_id:
            errors.append(f"protection event without cycle_id: {event.get('event_type')} {event}")
        else:
            events.setdefault(str(cycle_id), []).append(event)
    buy_fills: dict[str, list[dict[str, Any]]] = {}
    for fill in artifact.get("fills", []):
        if fill.get("side") == "BUY" and fill.get("cycle_id") is not None:
            buy_fills.setdefault(str(fill["cycle_id"]), []).append(fill)
    sell_quantity: Counter[str] = Counter()
    for fill in artifact.get("fills", []):
        if fill.get("side") == "SELL" and fill.get("cycle_id") is not None:
            sell_quantity[str(fill["cycle_id"])] += Decimal(str(fill["quantity"]))

    counts = {"cycles": 0, "initial_ok": 0, "moves": 0, "activation_ok": 0, "monotonic_ok": 0}
    for cycle in artifact.get("cycles", []):
        if cycle.get("entry_strategy") not in MICHELLE_BUYS:
            continue
        counts["cycles"] += 1
        cid = str(cycle["cycle_id"])
        if cycle.get("protection_mode") != "FIXED_THEN_TRAILING":
            errors.append(f"{cid}: wrong protection mode {cycle.get('protection_mode')}")
        fills = buy_fills.get(cid, [])
        if not fills:
            errors.append(f"{cid}: no recorded BUY fill")
            continue
        fill = Decimal(str(fills[0]["price"]))
        recorded_fill = dec(cycle.get("entry_fill_price"))
        if recorded_fill is not None and recorded_fill != fill:
            errors.append(f"{cid}: cycle fill {recorded_fill} != first BUY fill {fill}")
        want = expected_initial(fill)
        initial_events = [e for e in events.get(cid, []) if e.get("event_type") == "INITIAL_STOP_ESTABLISHED"]
        if len(initial_events) != 1:
            errors.append(f"{cid}: expected one initial stop event, found {len(initial_events)}")
            continue
        actual = dec(initial_events[0]["stop_price"])
        distance = fill - actual
        if actual != want or dec(cycle.get("initial_stop_price")) != want:
            errors.append(f"{cid}: initial stop {actual} != ceil_tick({fill} - 0.25) = {want}")
        elif not ZERO < distance <= OFFSET:
            errors.append(f"{cid}: initial distance {distance} outside (0, 0.25]")
        else:
            counts["initial_ok"] += 1
        last, cycle_ok_act, cycle_ok_mono = actual, True, True
        for event in events.get(cid, []):
            if event.get("event_type") != "STOP_MOVED":
                continue
            counts["moves"] += 1
            new = dec(event["new_stop_price"])
            if event.get("protection_mode") != "FIXED_THEN_TRAILING":
                errors.append(f"{cid}: stop moved under the wrong policy")
            if dec(event.get("previous_stop_price")) != last:
                errors.append(f"{cid}: previous stop {event.get('previous_stop_price')} != {last}")
                cycle_ok_mono = False
            if new <= last:
                errors.append(f"{cid}: stop did not rise ({last} -> {new})")
                cycle_ok_mono = False
            if new <= actual:
                errors.append(f"{cid}: trail moved to {new}, not strictly above initial stop {actual}")
                cycle_ok_act = False
            if new != new.quantize(Decimal("0.01")) and new >= 1:
                errors.append(f"{cid}: stop {new} is not on the tick grid")
            high = dec(event.get("highest_price"))
            if high is None:
                errors.append(f"{cid}: stop move has no recorded highest price to verify")
                cycle_ok_act = False
            elif new != expected_candidate(high):
                errors.append(
                    f"{cid}: stop {new} != floor_tick(0.998 x {high}) = {expected_candidate(high)}"
                )
                cycle_ok_act = False
            last = max(last, new)
        highest = dec(cycle.get("highest_trade_price"))
        final = dec(cycle.get("final_stop_price"))
        if final is not None and final != last:
            errors.append(f"{cid}: final stop {final} != last recorded stop {last}")
        if highest is not None and last > actual and last > expected_candidate(highest):
            errors.append(f"{cid}: stop {last} exceeds 0.2% below the highest price {highest}")
            cycle_ok_act = False
        counts["activation_ok"] += cycle_ok_act
        counts["monotonic_ok"] += cycle_ok_mono
        exited = sell_quantity.get(cid, ZERO)
        if cycle.get("closed_at") is not None and exited != Decimal(str(cycle["entry_quantity"])):
            errors.append(f"{cid}: exited {exited} of {cycle['entry_quantity']} shares")
    return rejections, {
        **counts, "errors": errors, "passed": not errors, "verified": counts["cycles"] > 0,
    }


def load_ledger(result: dict[str, Any], result_dir: Path) -> list[dict[str, Any]]:
    path = Path(result.get("trade_ledger_file") or "")
    if not path.is_file():
        path = result_dir / "trade_ledgers" / f"{result['run_id']}.json"
    return json.loads(path.read_text())["trades"] if path.is_file() else []


def metrics(result: dict[str, Any], capital: Decimal, result_dir: Path) -> dict[str, Any]:
    trades = load_ledger(result, result_dir)
    pnl = [Decimal(str(t["pnl"])) for t in trades]
    wins = sum(p > ZERO for p in pnl)
    reasons = Counter(str(t.get("exit_reason") or "UNKNOWN") for t in trades) or Counter(
        result.get("exit_reasons") or {}
    )
    count = int(result["trades"])
    return {
        "trades": count,
        "winning_trades": wins if trades else int(result.get("wins", 0)),
        "losing_trades": sum(p < ZERO for p in pnl) if trades else int(result.get("losses", 0)),
        "win_rate": result.get("win_rate") if count else None,
        "net_pnl": str(sum(pnl, ZERO)) if trades else str(dec(result["ending_equity"]) - capital),
        "net_return": result.get("net_return"),
        "profit_factor": result.get("profit_factor") if count else None,
        "max_drawdown": result.get("max_drawdown"),
        "largest_winner": str(max(pnl)) if pnl and max(pnl) > ZERO else None,
        "largest_loser": str(min(pnl)) if pnl and min(pnl) < ZERO else None,
        "protective_stop_exits": reasons.get("PROTECTIVE_STOP", 0),
        "eod_exits": reasons.get("END_OF_DAY", 0),
        "exit_reasons": dict(sorted(reasons.items())),
    }


def load_baseline(path: Path | None, jobs: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    if path is None or not (path / "batch.json").is_file():
        return {}
    batch = json.loads((path / "batch.json").read_text())
    out = {}
    for row in batch.get("jobs", []):
        job = jobs[int(row["job_index"])]
        if row.get("status") != "completed":
            out[(job["symbol"], job["strategy"])] = {"status": row.get("status")}
            continue
        out[(job["symbol"], job["strategy"])] = {
            "status": "completed", **metrics(row, Decimal(job["starting_capital"]), path)
        }
    return out


def summarize(batch, jobs, manifest, result_dir, baseline):
    expected = manifest["expected_execution"]
    by_index = {int(row["job_index"]): row for row in batch.get("jobs", [])}
    artifacts = {
        p.name.removesuffix(".results.json"): p
        for p in (result_dir / "executions").glob("*/*.results.json")
    }
    rows, problems = [], []
    for index, job in enumerate(jobs):
        result = by_index.get(index, {"status": "not_run"})
        label = f"{job['symbol']}/{job['strategy']}"
        row: dict[str, Any] = {
            "job_index": index, "symbol": job["symbol"], "configuration": job["strategy"],
            "dataset_id": job["dataset_id"], "status": result.get("status", "not_run"),
            "error": result.get("error"), "old": baseline.get((job["symbol"], job["strategy"])),
        }
        if result.get("status") != "completed":
            problems.append(f"{label}: {result.get('error') or 'not run'}")
            rows.append(row)
            continue
        recorded = result.get("execution_assumptions") or {}
        row["effective_execution"] = {k: recorded.get(k) for k in expected} | {
            k: recorded.get(k) for k in ("bar_fill_policy", "ambiguity_policy")
        }
        mismatch = {k: recorded.get(k) for k, v in expected.items() if recorded.get(k) != v}
        row["execution_matches_expected"] = not mismatch
        if mismatch:
            problems.append(f"{label}: execution assumptions {mismatch}")
        row.update(metrics(result, Decimal(job["starting_capital"]), result_dir))
        path = artifacts.get(result["run_id"])
        if path is None:
            problems.append(f"{label}: immutable replay artifact missing")
            row.update(atomic_entry_rejections=None, stop_audit=None)
        else:
            rejected, audit = audit_artifact(json.loads(path.read_text()))
            row.update(atomic_entry_rejections=rejected, stop_audit=audit)
            if not audit["passed"]:
                problems.append(f"{label}: stop audit FAILED: {audit['errors'][:5]}")
        rows.append(row)
    return rows, problems


def markdown(rows, problems, batch, manifest) -> str:
    done = [r for r in rows if r["status"] == "completed"]
    lines = [
        "# Michelle stop clarification: NEW vs OLD (both zero assumed costs)", "",
        f"Experiments: {len(rows)}; completed: {batch.get('completed_count', 0)}; "
        f"failed: {batch.get('failed_count', 0)}; workers used: {batch.get('workers_used')}.", "",
        f"- **OLD** ({manifest['scenarios']['OLD']})",
        f"- **NEW** ({manifest['scenarios']['NEW']})", "",
        "Zero assumed slippage and fees is a research scenario, not a guarantee of live execution "
        "prices; a protective stop is a level, not a guaranteed exit price (gaps fill at the gapped "
        "open). Sizing, entries, datasets and window are identical. Exits differ, so later trades and "
        "trade counts differ too. Returns are fractions of the $10,000 starting capital.", "",
        "## Effective execution assumptions (NEW run)", "",
        f"Required: `{json.dumps(manifest['expected_execution'], sort_keys=True)}`", "",
    ]
    for value in sorted({json.dumps(r["effective_execution"], sort_keys=True) for r in done}):
        count = sum(json.dumps(r["effective_execution"], sort_keys=True) == value for r in done)
        lines.append(f"- {count} runs recorded `{value}`")
    ok = bool(done) and all(r["execution_matches_expected"] for r in done)
    lines += ["", f"Assumption check: **{'PASS' if ok else 'FAIL'}**.", "", "## NEW results", "",
              "| Symbol | Configuration | Trades | Wins | Losses | Win rate | Net P&L | Return | Profit factor | Max DD | Largest win | Largest loss | Stop exits | EOD exits | Atomic rejections |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        if r["status"] != "completed":
            lines.append(f"| {r['symbol']} | {r['configuration']} | {r['status']} |" + " N/A |" * 12)
            continue
        lines.append(
            f"| {r['symbol']} | {r['configuration']} | {r['trades']} | {r['winning_trades']} | "
            f"{r['losing_trades']} | {fmt(r['win_rate'], pct=True)} | {fmt(r['net_pnl'])} | "
            f"{fmt(r['net_return'], pct=True)} | {fmt(r['profit_factor'])} | "
            f"{fmt(r['max_drawdown'], pct=True)} | {fmt(r['largest_winner'])} | "
            f"{fmt(r['largest_loser'])} | {r['protective_stop_exits']} | {r['eod_exits']} | "
            f"{r['atomic_entry_rejections'] if r['atomic_entry_rejections'] is not None else 'N/A'} |"
        )
    lines += ["", "## OLD vs NEW", "",
              "| Symbol | Configuration | Trades OLD / NEW | Win rate OLD / NEW | Net P&L OLD / NEW | Change | Profit factor OLD / NEW | Max DD OLD / NEW | Largest loss OLD / NEW |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    total_old = total_new = ZERO
    for r in done:
        o = r.get("old")
        if not o or o.get("status") != "completed":
            lines.append(f"| {r['symbol']} | {r['configuration']} | N/A / {r['trades']} | N/A | N/A / {fmt(r['net_pnl'])} | N/A | N/A | N/A | N/A |")
            continue
        change = dec(r["net_pnl"]) - dec(o["net_pnl"])
        total_old += dec(o["net_pnl"])
        total_new += dec(r["net_pnl"])
        lines.append(
            f"| {r['symbol']} | {r['configuration']} | {o['trades']} / {r['trades']} | "
            f"{fmt(o['win_rate'], pct=True)} / {fmt(r['win_rate'], pct=True)} | "
            f"{fmt(o['net_pnl'])} / {fmt(r['net_pnl'])} | {change:+,.2f} | "
            f"{fmt(o['profit_factor'])} / {fmt(r['profit_factor'])} | "
            f"{fmt(o['max_drawdown'], pct=True)} / {fmt(r['max_drawdown'], pct=True)} | "
            f"{fmt(o['largest_loser'])} / {fmt(r['largest_loser'])} |"
        )
    lines += ["", f"Sum of net P&L over comparable jobs: OLD {total_old:,.2f}, NEW {total_new:,.2f}.", "",
              "## Exit-reason distribution", "",
              "| Symbol | Configuration | NEW exits | OLD exits |", "|---|---|---|---|"]
    for r in done:
        o = r.get("old") or {}
        new_text = ", ".join(f"{k}: {v}" for k, v in r["exit_reasons"].items()) or "none"
        old_text = ", ".join(f"{k}: {v}" for k, v in (o.get("exit_reasons") or {}).items()) or "N/A"
        lines.append(f"| {r['symbol']} | {r['configuration']} | {new_text} | {old_text} |")
    lines += ["", "## Stop verification from recorded fills and protection events (NEW)", "",
              "Initial stop = ceil_tick(first BUY fill - 0.25); every move strictly above the initial "
              "stop, strictly rising, on the tick grid, and exactly floor_tick(0.998 x the recorded traded "
              "high that triggered it); the final stop equals the last move, never exceeds 0.2% "
              "below the highest price, and exits close the whole position.", "",
              "| Symbol | Configuration | Cycles | Initial stop correct | Stop moves | Activation correct | Monotonic correct | Result |",
              "|---|---|---:|---:|---:|---:|---:|---|"]
    for r in done:
        a = r.get("stop_audit")
        if a is None:
            lines.append(f"| {r['symbol']} | {r['configuration']} | N/A | N/A | N/A | N/A | N/A | NOT AUDITED |")
            continue
        verdict = "PASS" if a["passed"] and a["verified"] else ("NO CYCLES" if not a["verified"] else "FAIL")
        lines.append(f"| {r['symbol']} | {r['configuration']} | {a['cycles']} | {a['initial_ok']} | "
                     f"{a['moves']} | {a['activation_ok']} | {a['monotonic_ok']} | {verdict} |")
    failures = [r for r in done if r.get("stop_audit") and not r["stop_audit"]["passed"]]
    if failures:
        lines += ["", "### Stop-audit failures", ""]
        for r in failures:
            for error in r["stop_audit"]["errors"][:20]:
                lines.append(f"- {r['symbol']} {r['configuration']}: {error}")
    if problems:
        lines += ["", "## Problems", ""] + [f"- {p}" for p in problems]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", required=True, type=Path)
    parser.add_argument("--experiments", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--baseline", type=Path, help="previous zero-slippage run directory (read-only)")
    args = parser.parse_args()
    batch = json.loads(args.batch.read_text())
    jobs = json.loads(args.experiments.read_text())["experiments"]
    manifest = json.loads(args.manifest.read_text())
    rows, problems = summarize(batch, jobs, manifest, args.results, load_baseline(args.baseline, jobs))
    if args.baseline is not None and not any(r.get("old") for r in rows):
        problems.append(f"baseline {args.baseline} has no usable results")
    (args.results / "comparison.json").write_text(
        json.dumps({"rows": rows, "problems": problems}, indent=2, sort_keys=True) + "\n")
    flat = []
    for r in rows:
        item = {k: v for k, v in r.items() if k not in ("stop_audit", "old", "effective_execution")}
        item["exit_reasons"] = json.dumps(r.get("exit_reasons"), sort_keys=True)
        audit = r.get("stop_audit") or {}
        item.update({f"audit_{k}": audit.get(k) for k in ("cycles", "initial_ok", "moves", "activation_ok", "monotonic_ok", "passed")})
        old = r.get("old") or {}
        item.update({f"old_{k}": old.get(k) for k in ("status", "trades", "win_rate", "net_pnl", "profit_factor", "max_drawdown", "largest_winner", "largest_loser", "protective_stop_exits", "eod_exits")})
        flat.append(item)
    keys = sorted({k for item in flat for k in item})
    with (args.results / "comparison.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(flat)
    text = markdown(rows, problems, batch, manifest)
    (args.results / "comparison.md").write_text(text)
    print(text)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
