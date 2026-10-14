"""Review outcome analysis: executed results versus counterfactual estimates, per variant.

Everything here reads completed run artifacts (``<run_id>.results.json`` and the
``<run_id>.reviews.jsonl`` sidecar); it runs nothing and calls no reviewer.

Two kinds of numbers are kept strictly apart:

* **Executed outcomes** are facts: a proposal was approved (or not reviewed), became an order and
  a cycle, and closed with a realized P&L computed from its fills.
* **Counterfactual estimates** describe what a *blocked* proposal might have done.  They need
  prices after the decision, so they are produced only here, after the fact, never fed back to
  the reviewer, never added to executed totals, and are absent (``None``) unless a caller
  supplies an estimator.  A rejection is not assumed to have helped or hurt.

Variants - baseline (no review), JEV on BUY only, SELL only, or both - are separate completed runs;
:func:`compare_variants` lines their executed results up against the baseline.  Removing a trade
changes capital, sequencing and later entries, so differences between variants are comparisons of
*runs*, not sums of individual trade outcomes.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from mike_trade.review.store import ReviewRecord, load_review_records

__all__ = [
    "cycle_outcomes",
    "review_rows",
    "summarize_run",
    "compare_variants",
    "path_counterfactual",
    "load_run",
    "format_comparison",
    "main",
]

_ZERO = Decimal(0)
Counterfactual = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]


def load_run(results_path: str | Path, reviews_path: str | Path | None = None) -> dict[str, Any]:
    """Read a run's immutable result ledger and (optionally) its full review records."""
    payload = json.loads(Path(results_path).read_text(encoding="utf-8"))
    records: list[ReviewRecord] = []
    sidecar = Path(reviews_path) if reviews_path else Path(
        str(results_path).replace(".results.json", ".reviews.jsonl")
    )
    if sidecar.is_file():
        records = load_review_records([sidecar])
    return {"payload": payload, "records": records}


def cycle_outcomes(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Realized P&L per closed cycle, from its fills (no costs assumed beyond recorded fees)."""
    spent: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    received: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    fees: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    quantity: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for fill in payload.get("fills", ()):
        cycle = fill.get("cycle_id")
        if cycle is None:
            continue
        value = Decimal(str(fill["price"])) * Decimal(str(fill["quantity"]))
        fees[cycle] += Decimal(str(fill.get("fee", 0)))
        if fill["purpose"] == "ENTRY":
            spent[cycle] += value
            quantity[cycle] += Decimal(str(fill["quantity"]))
        else:
            received[cycle] += value
    outcomes: dict[str, dict[str, Any]] = {}
    for cycle in payload.get("cycles", ()):
        cycle_id = str(cycle["cycle_id"])
        closed = cycle.get("closed_at") is not None
        entry = {
            "cycle_id": cycle_id,
            "symbol": cycle.get("symbol"),
            "entry_signal_id": cycle.get("entry_signal_id"),
            "exit_signal_id": cycle.get("exit_signal_id"),
            "entry_strategy": cycle.get("entry_strategy"),
            "exit_reason": cycle.get("exit_reason"),
            "opened_at": cycle.get("opened_at"),
            "closed_at": cycle.get("closed_at"),
            "closed": closed,
            "quantity": str(quantity[cycle_id]),
            "realized_pnl": None,
        }
        if closed:
            entry["realized_pnl"] = str(received[cycle_id] - spent[cycle_id] - fees[cycle_id])
        outcomes[cycle_id] = entry
    return outcomes


def review_rows(
    payload: Mapping[str, Any],
    records: Sequence[ReviewRecord] = (),
    *,
    counterfactual: Counterfactual | None = None,
) -> list[dict[str, Any]]:
    """One row per review: the proposal, JEV's decision, whether it executed, and its outcome.

    ``executed`` and ``actual_outcome`` come from the run's cycles.  ``counterfactual`` (only for
    proposals that did not execute) is whatever the supplied estimator returns, labelled as an
    estimate; without an estimator it stays ``None``.
    """
    full = {(r.review_id, r.source): r for r in records if r.source != "disposition"}
    by_review = {r.review_id: r for r in records if r.source == "live_inference"}
    summaries = payload.get("reviews", ())
    outcomes = cycle_outcomes(payload)
    by_entry = {o["entry_signal_id"]: o for o in outcomes.values() if o["entry_signal_id"]}
    by_exit = {o["exit_signal_id"]: o for o in outcomes.values() if o["exit_signal_id"]}
    rows: list[dict[str, Any]] = []
    for summary in summaries:
        record = by_review.get(summary["review_id"]) or next(
            (r for (rid, _src), r in full.items() if rid == summary["review_id"]), None
        )
        action = summary["action"]
        cycle = (by_entry if action == "BUY" else by_exit).get(summary["signal_id"])
        blocked = summary["effect"] in (
            "REJECTED_BLOCKED", "ERROR_REJECTED_BLOCKED", "STALE_BLOCKED"
        )
        row: dict[str, Any] = {
            "review_id": summary["review_id"],
            "as_of": summary["as_of"],
            "symbol": summary["symbol"],
            "action": action,
            "strategy_ids": summary["strategy_ids"],
            "selected_strategy_id": summary["selected_strategy_id"],
            "signal_id": summary["signal_id"],
            "decision": summary["decision"],
            "explanation": summary["explanation"],
            "observations": summary["observations"],
            "enforcement": summary["enforcement"],
            "effect": summary["effect"],
            "error": summary["error"],
            "executed": cycle is not None,
            "blocked_by_review": blocked,
            "actual_outcome": None
            if cycle is None
            else {"cycle_id": cycle["cycle_id"], "realized_pnl": cycle["realized_pnl"],
                  "exit_reason": cycle["exit_reason"], "closed": cycle["closed"]},
            "latency_ms": None if record is None else record.latency_ms,
            "review_cost": None if record is None or not record.usage
            else record.usage.get("cost"),
            "source": None if record is None else record.source,
            "counterfactual": None,
            "counterfactual_status": "not_applicable" if cycle is not None else "not_estimated",
        }
        if cycle is None and counterfactual is not None:
            estimate = counterfactual(row)
            if estimate is not None:
                row["counterfactual"] = {**estimate, "kind": "ESTIMATE_NOT_EXECUTED"}
                row["counterfactual_status"] = "estimated"
        rows.append(row)
    return rows


def path_counterfactual(
    action: str, reference_price: Decimal, quantity: Decimal, later_prices: Iterable[Decimal]
) -> dict[str, Any] | None:
    """Mark-to-market of a blocked proposal against the prices that followed it (analysis only).

    For a blocked BUY: P&L if entered at ``reference_price`` and marked at the last later price.
    For a blocked SELL: the P&L *avoided or forgone* by staying long to the last later price,
    relative to selling at ``reference_price``.  It ignores stops, fills and slippage, and the
    prices it uses did not exist at decision time - it is an estimate for reading outcomes, never
    an input to a decision.
    """
    prices = list(later_prices)
    if not prices:
        return None
    last = prices[-1]
    delta = (last - reference_price) * quantity
    return {
        "basis": "mark_to_last_price_after_decision",
        "pnl_if_proceeded": str(delta if action == "BUY" else -delta),
        "marked_at_price": str(last),
    }


def summarize_run(
    payload: Mapping[str, Any],
    records: Sequence[ReviewRecord] = (),
    *,
    counterfactual: Counterfactual | None = None,
) -> dict[str, Any]:
    outcomes = [o for o in cycle_outcomes(payload).values() if o["closed"]]
    pnls = [Decimal(o["realized_pnl"]) for o in outcomes if o["realized_pnl"] is not None]
    wins = [p for p in pnls if p > _ZERO]
    losses = [p for p in pnls if p < _ZERO]
    rows = review_rows(payload, records, counterfactual=counterfactual)
    paid = [r for r in records if r.source == "live_inference" and r.usage]
    cost = sum(
        (Decimal(str(r.usage["cost"])) for r in paid if r.usage and r.usage.get("cost")), _ZERO
    )
    latencies = [r.latency_ms for r in records if r.latency_ms is not None]
    by_action: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_action.setdefault(
            row["action"], {"reviews": 0, "approved": 0, "rejected": 0, "errors": 0,
                            "blocked": 0, "executed": 0}
        )
        bucket["reviews"] += 1
        bucket["approved"] += row["decision"] == "APPROVE"
        bucket["rejected"] += row["decision"] == "REJECT"
        bucket["errors"] += row["error"] is not None
        bucket["blocked"] += bool(row["blocked_by_review"])
        bucket["executed"] += bool(row["executed"])
    return {
        "executed": {
            "closed_trades": len(pnls),
            "net_pnl": str(sum(pnls, _ZERO)),
            "win_rate": None if not pnls else round(len(wins) / len(pnls), 6),
            "average_pnl": None if not pnls else str(sum(pnls, _ZERO) / len(pnls)),
            "profit_factor": None
            if not losses else str(sum(wins, _ZERO) / -sum(losses, _ZERO)),
        },
        "reviews": {
            "total": len(rows),
            "by_action": by_action,
            "blocked_proposals": sum(bool(r["blocked_by_review"]) for r in rows),
            "model_calls": sum(r.source == "live_inference" for r in records),
            "total_cost": str(cost),
            "mean_latency_ms": None if not latencies
            else round(sum(latencies) / len(latencies), 3),
        },
        "counterfactual": {
            "estimated_blocked_proposals": sum(
                r["counterfactual_status"] == "estimated" for r in rows
            ),
            "note": "estimates describe proposals that did not execute and are never added to "
            "the executed totals",
        },
        "point_in_time_valid": (payload.get("review_validity") or {}).get("point_in_time_valid"),
        "rows": rows,
    }


def compare_variants(runs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Line up summaries (``summarize_run`` output) against the ``baseline`` entry, if present."""
    baseline = runs.get("baseline")
    table: dict[str, Any] = {}
    for name, summary in runs.items():
        executed = summary["executed"]
        row: dict[str, Any] = {
            "closed_trades": executed["closed_trades"],
            "net_pnl": executed["net_pnl"],
            "win_rate": executed["win_rate"],
            "profit_factor": executed["profit_factor"],
            "reviews": summary["reviews"]["total"],
            "blocked_proposals": summary["reviews"]["blocked_proposals"],
            "review_cost": summary["reviews"]["total_cost"],
            "point_in_time_valid": summary["point_in_time_valid"],
        }
        if baseline is not None and name != "baseline":
            row["executed_net_pnl_vs_baseline"] = str(
                Decimal(executed["net_pnl"]) - Decimal(baseline["executed"]["net_pnl"])
            )
            row["executed_trades_vs_baseline"] = (
                executed["closed_trades"] - baseline["executed"]["closed_trades"]
            )
        table[name] = row
    return {
        "variants": table,
        "caveat": (
            "Executed results are separate runs; rejecting a proposal changes capital, "
            "sequencing and later entries, so differences are not the sum of individual "
            "trade outcomes. Counterfactual estimates (if any) are excluded from these totals."
        ),
    }


def format_comparison(comparison: Mapping[str, Any]) -> str:
    lines = [
        "| variant | closed trades | net P&L | win rate | PF | reviews | blocked | cost | "
        "Δ net vs baseline |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, row in comparison["variants"].items():
        lines.append(
            f"| {name} | {row['closed_trades']} | {row['net_pnl']} | {row['win_rate']} | "
            f"{row['profit_factor']} | {row['reviews']} | {row['blocked_proposals']} | "
            f"{row['review_cost']} | {row.get('executed_net_pnl_vs_baseline', '-')} |"
        )
    lines.append("")
    lines.append(comparison["caveat"])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mike_trade.review.analysis",
        description="Compare completed runs with and without decision review (reads artifacts only).",
    )
    parser.add_argument(
        "--run", action="append", required=True, metavar="NAME=RESULTS_JSON",
        help="a completed run, e.g. baseline=.../<run_id>.results.json (the sibling "
        "<run_id>.reviews.jsonl is read automatically)",
    )
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)
    summaries: dict[str, Any] = {}
    for item in args.run:
        name, _, path = item.partition("=")
        if not name or not path:
            parser.error(f"--run expects NAME=PATH; got {item!r}")
        run = load_run(path)
        summaries[name] = summarize_run(run["payload"], run["records"])
    comparison = compare_variants(summaries)
    print(json.dumps(comparison, indent=2, sort_keys=True) if args.json
          else format_comparison(comparison))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
