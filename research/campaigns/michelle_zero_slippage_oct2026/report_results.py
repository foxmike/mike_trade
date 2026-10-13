"""Concise report for the nine zero-slippage Michelle runs, with the 5 bps baseline alongside."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ZERO = Decimal(0)


def _source_helpers():
    """Reuse the original campaign's Michelle stop audit without modifying that campaign."""
    path = HERE.parent / "sip_annual_oct2026" / "summarize_results.py"
    spec = importlib.util.spec_from_file_location("sip_annual_summarize_results", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def fmt(value: Any, places: int = 2, pct: bool = False) -> str:
    number = dec(value)
    if number is None:
        return "N/A"
    return f"{number * 100:,.{places}f}%" if pct else f"{number:,.{places}f}"


def load_baseline(path: Path | None) -> dict[tuple[str, str], dict[str, Any]]:
    if path is None or not (path / "batch.json").is_file():
        return {}
    batch = json.loads((path / "batch.json").read_text())
    return {
        (row["symbol"], row["strategy"]): row
        for row in batch.get("jobs", [])
        if str(row.get("strategy", "")).startswith("michelle_")
    }


def summarize(batch, jobs, manifest, result_dir, baseline, helpers):
    expected = manifest["expected_execution"]
    by_index = {int(row["job_index"]): row for row in batch.get("jobs", [])}
    artifacts = {
        p.name.removesuffix(".results.json"): p
        for p in (result_dir / "executions").glob("*/*.results.json")
    }
    rows, problems = [], []
    for index, job in enumerate(jobs):
        result = by_index.get(index, {"status": "not_run"})
        row: dict[str, Any] = {
            "job_index": index, "symbol": job["symbol"], "configuration": job["strategy"],
            "dataset_id": job["dataset_id"], "status": result.get("status", "not_run"),
            "error": result.get("error"),
        }
        base = baseline.get((job["symbol"], job["strategy"]))
        row["baseline_5bps_status"] = None if base is None else base.get("status")
        if result.get("status") != "completed":
            problems.append(f"{job['symbol']}/{job['strategy']}: {result.get('error') or 'not run'}")
            rows.append(row)
            continue
        recorded = result.get("execution_assumptions") or {}
        row["effective_execution"] = {k: recorded.get(k) for k in expected} | {
            k: recorded.get(k) for k in ("bar_fill_policy", "ambiguity_policy")
        }
        mismatch = {k: recorded.get(k) for k, v in expected.items() if recorded.get(k) != v}
        row["execution_matches_expected"] = not mismatch
        if mismatch:
            problems.append(f"{job['symbol']}/{job['strategy']}: execution assumptions {mismatch}")
        capital = dec(job["starting_capital"])
        ledger_path = Path(result["trade_ledger_file"])
        if not ledger_path.is_file():
            ledger_path = result_dir / "trade_ledgers" / f"{result['run_id']}.json"
        trades = json.loads(ledger_path.read_text())["trades"] if ledger_path.is_file() else []
        net = sum((Decimal(str(t["pnl"])) for t in trades), ZERO) if trades else (
            dec(result["ending_equity"]) - capital
        )
        reasons = Counter(str(t.get("exit_reason") or "UNKNOWN") for t in trades) or Counter(
            result.get("exit_reasons") or {}
        )
        artifact_path = artifacts.get(result["run_id"])
        refused = 0
        stop_audit = None
        if artifact_path is not None:
            artifact = json.loads(artifact_path.read_text())
            refused = sum(
                e.get("event_type") == "ENTRY_REJECTED_PROTECTION_INVALID"
                for e in artifact.get("protection_events", [])
            )
            stop_audit = helpers.verify_michelle(artifact)
            if not stop_audit["passed"]:
                problems.append(f"{job['symbol']}/{job['strategy']}: stop audit {stop_audit['errors'][:3]}")
        row.update(
            trades=int(result["trades"]), wins=int(result["wins"]),
            win_rate=result.get("win_rate") if int(result["trades"]) else None,
            net_pnl=str(net), net_return=result.get("net_return"),
            profit_factor=result.get("profit_factor") if int(result["trades"]) else None,
            max_drawdown=result.get("max_drawdown"), exit_reasons=dict(sorted(reasons.items())),
            atomic_entry_rejections=refused, stop_audit=stop_audit,
        )
        if base is not None and base.get("status") == "completed":
            row["baseline_5bps"] = {
                "trades": base.get("trades"), "net_return": base.get("net_return"),
                "profit_factor": base.get("profit_factor"),
                "net_pnl": str(dec(base["ending_equity"]) - capital),
            }
        rows.append(row)
    return rows, problems


def markdown(rows, problems, batch, manifest) -> str:
    done = [r for r in rows if r["status"] == "completed"]
    lines = [
        "# Michelle zero-slippage research results", "",
        f"Experiments: {len(rows)}; completed: {batch.get('completed_count', 0)}; "
        f"failed: {batch.get('failed_count', 0)}; workers used: {batch.get('workers_used')}.", "",
        "Zero assumed slippage and fees is a research scenario, not a guarantee of live execution "
        "prices. Fills use the next-bar-open model; gap-through-stop exits and STOP_FIRST OHLC "
        "ambiguity are unchanged. Returns are fractions of the $10,000 starting capital.", "",
        "## Effective execution assumptions", "",
    ]
    effective = {json.dumps(r["effective_execution"], sort_keys=True) for r in done}
    expected = manifest["expected_execution"]
    lines.append(f"Required: `{json.dumps(expected, sort_keys=True)}`")
    lines.append("")
    if not done:
        lines.append("No completed run recorded assumptions.")
    for value in sorted(effective):
        count = sum(json.dumps(r["effective_execution"], sort_keys=True) == value for r in done)
        lines.append(f"- {count} runs recorded `{value}`")
    ok = all(r["execution_matches_expected"] for r in done)
    lines += ["", f"Assumption check: **{'PASS' if ok and done else 'FAIL'}**.", "",
              "## Results", "",
              "| Symbol | Configuration | Status | Trades | Win rate | Net P&L | Return | Profit factor | Max drawdown | Atomic ORB/VWAP entry rejections |",
              "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        if r["status"] != "completed":
            lines.append(f"| {r['symbol']} | {r['configuration']} | {r['status']} | N/A | N/A | N/A | N/A | N/A | N/A | N/A |")
            continue
        lines.append(
            f"| {r['symbol']} | {r['configuration']} | completed | {r['trades']} | "
            f"{fmt(r['win_rate'], pct=True)} | {fmt(r['net_pnl'])} | {fmt(r['net_return'], pct=True)} | "
            f"{fmt(r['profit_factor'])} | {fmt(r['max_drawdown'], pct=True)} | {r['atomic_entry_rejections']} |"
        )
    lines += ["", "## Exit-reason distribution", "",
              "| Symbol | Configuration | Exit reasons (count) |", "|---|---|---|"]
    for r in done:
        text = ", ".join(f"{k}: {v}" for k, v in r["exit_reasons"].items()) or "none"
        lines.append(f"| {r['symbol']} | {r['configuration']} | {text} |")
    if any("baseline_5bps" in r for r in done):
        lines += ["", "## Against the original 5 bps development run", "",
                  "The 5 bps source results are read-only and remain unchanged. Differences also "
                  "include the atomic ORB safety fix, so they are not a pure slippage effect.", "",
                  "| Symbol | Configuration | Trades (0 bps / 5 bps) | Net P&L (0 bps / 5 bps) | Profit factor (0 bps / 5 bps) |",
                  "|---|---|---:|---:|---:|"]
        for r in done:
            b = r.get("baseline_5bps")
            if b is None:
                lines.append(f"| {r['symbol']} | {r['configuration']} | {r['trades']} / failed or absent | "
                             f"{fmt(r['net_pnl'])} / N/A | {fmt(r['profit_factor'])} / N/A |")
            else:
                lines.append(f"| {r['symbol']} | {r['configuration']} | {r['trades']} / {b['trades']} | "
                             f"{fmt(r['net_pnl'])} / {fmt(b['net_pnl'])} | "
                             f"{fmt(r['profit_factor'])} / {fmt(b['profit_factor'])} |")
    lines += ["", "## Michelle protective-stop audit", ""]
    for r in done:
        audit = r.get("stop_audit")
        text = "N/A" if audit is None or not audit["verified"] else (
            f"{'PASS' if audit['passed'] else 'FAIL'} ({audit['cycles_checked']} cycles, "
            f"{audit['stop_moves_checked']} stop moves)")
        lines.append(f"- {r['symbol']} {r['configuration']}: {text}")
    if problems:
        lines += ["", "## Problems", ""] + [f"- {p}" for p in problems]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", required=True, type=Path)
    parser.add_argument("--experiments", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--baseline", type=Path, help="original 5 bps batch directory (read-only)")
    args = parser.parse_args()
    batch = json.loads(args.batch.read_text())
    jobs = json.loads(args.experiments.read_text())["experiments"]
    manifest = json.loads(args.manifest.read_text())
    rows, problems = summarize(batch, jobs, manifest, args.results, load_baseline(args.baseline),
                               _source_helpers())
    (args.results / "comparison.json").write_text(
        json.dumps({"rows": rows, "problems": problems}, indent=2, sort_keys=True) + "\n")
    flat = [{k: (json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v)
             for k, v in r.items() if k != "stop_audit"} for r in rows]
    keys = sorted({k for r in flat for k in r})
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
