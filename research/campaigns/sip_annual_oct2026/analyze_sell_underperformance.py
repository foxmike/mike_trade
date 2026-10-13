"""Analyze the campaign's matched 0/1/2/3 technical-SELL comparisons."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median

from mike_trade.scheduling.calendar import NEW_YORK, is_trading_day, session_for


ZERO = Decimal(0)
CAMPAIGN = Path(__file__).resolve().parent
RESULTS = Path(
    "research/results/sip_annual_oct2026/development-20261012T230002Z-981668"
)
FAMILIES = ("trend", "breakout", "vwap_mean", "price_action")


def moment(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def dec(value) -> Decimal:
    return Decimal(str(value))


def main() -> None:
    jobs = json.loads((CAMPAIGN / "experiments.json").read_text())["experiments"]
    batch = json.loads((RESULTS / "batch.json").read_text())
    completed = {int(row["job_index"]): row for row in batch["jobs"]
                 if row.get("status") == "completed"}
    window_start = moment(jobs[0]["start"])
    window_end = moment(jobs[0]["end"])
    first = window_start.astimezone(NEW_YORK).date()
    last = window_end.astimezone(NEW_YORK).date()
    session_count = 0
    day = first
    while day <= last:
        if is_trading_day(day):
            opened_at = session_for(day).open_utc
            session_count += int(window_start <= opened_at < window_end)
        day += timedelta(days=1)

    records = []
    exit_contribution = defaultdict(lambda: {
        "trades": 0, "realized_pnl": ZERO, "wins": 0, "losses": 0,
        "reentry_opportunities": 0, "same_day_reentries": 0,
        "reentries_within_5m": 0,
        "same_day_reentry_gap_seconds": ZERO,
    })
    for index, job in enumerate(jobs):
        name = job["strategy"]
        matched = next((family for family in FAMILIES if name.startswith(f"{family}_exit_")), None)
        if matched is None or index not in completed:
            continue
        sell_count = int(name.rsplit("_", 1)[1])
        result = completed[index]
        ledger_path = Path(result["trade_ledger_file"])
        if not ledger_path.exists():
            ledger_path = RESULTS / "trade_ledgers" / f"{result['run_id']}.json"
        trades = sorted(json.loads(ledger_path.read_text())["trades"],
                        key=lambda trade: moment(trade["entry_time"]))
        pnls = [dec(t["pnl"]) for t in trades]
        wins = [p for p in pnls if p > ZERO]
        losses = [p for p in pnls if p < ZERO]
        holds = [dec(t["hold_duration_seconds"]) for t in trades]
        gaps = []
        same_day_gaps = []
        same_day = 0
        within_5m = 0
        within_15m = 0
        for prior, following in zip(trades, trades[1:]):
            exited = moment(prior["exit_time"])
            entered = moment(following["entry_time"])
            gap = dec((entered - exited).total_seconds())
            if gap < ZERO:
                raise ValueError(f"overlapping trades in {job['symbol']} {name}")
            gaps.append(gap)
            same = exited.astimezone(NEW_YORK).date() == entered.astimezone(NEW_YORK).date()
            same_day += int(same)
            if same:
                same_day_gaps.append(gap)
            within_5m += int(same and gap <= 300)
            within_15m += int(same and gap <= 900)
            seller = prior.get("exit_strategy")
            if seller:
                exit_contribution[str(seller)]["reentry_opportunities"] += 1
                exit_contribution[str(seller)]["same_day_reentries"] += int(same)
                exit_contribution[str(seller)]["reentries_within_5m"] += int(same and gap <= 300)
                if same:
                    exit_contribution[str(seller)]["same_day_reentry_gap_seconds"] += gap
        for trade, pnl in zip(trades, pnls):
            seller = trade.get("exit_strategy")
            if seller:
                contribution = exit_contribution[str(seller)]
                contribution["trades"] += 1
                contribution["realized_pnl"] += pnl
                contribution["wins"] += int(pnl > ZERO)
                contribution["losses"] += int(pnl < ZERO)
        records.append({
            "symbol": job["symbol"], "family": matched, "sell_count": sell_count,
            "configuration": name, "trades": len(trades),
            "wins": len(wins), "losses": len(losses),
            "avg_hold_seconds": sum(holds, ZERO) / len(holds) if holds else None,
            "median_hold_seconds": dec(median(holds)) if holds else None,
            "avg_realized_pnl": sum(pnls, ZERO) / len(pnls) if pnls else None,
            "avg_win": sum(wins, ZERO) / len(wins) if wins else None,
            "avg_loss": sum(losses, ZERO) / len(losses) if losses else None,
            "gross_profit": sum(wins, ZERO),
            "gross_loss": -sum(losses, ZERO),
            "net_pnl": sum(pnls, ZERO),
            "estimated_slippage": dec(result["estimated_slippage"]),
            "trades_per_session": dec(len(trades)) / session_count,
            "same_day_reentries": same_day,
            "same_day_reentry_rate": (
                dec(same_day) / (len(trades) - 1) if len(trades) > 1 else None
            ),
            "reentries_within_5m": within_5m,
            "reentries_within_15m": within_15m,
            "avg_reentry_gap_seconds": sum(gaps, ZERO) / len(gaps) if gaps else None,
            "median_reentry_gap_seconds": dec(median(gaps)) if gaps else None,
            "reentry_gaps_seconds": gaps,
            "same_day_reentry_gaps_seconds": same_day_gaps,
            "exit_winners": Counter(
                str(t["exit_strategy"]) for t in trades if t.get("exit_strategy")
            ),
        })

    by_count = []
    for sell_count in range(4):
        selected = [r for r in records if r["sell_count"] == sell_count]
        total_trades = sum(r["trades"] for r in selected)
        all_gaps = [gap for row in selected for gap in row["reentry_gaps_seconds"]]
        same_day_gaps = [
            gap for row in selected for gap in row["same_day_reentry_gaps_seconds"]
        ]
        by_count.append({
            "sell_count": sell_count,
            "configurations": len(selected),
            "trades": total_trades,
            "avg_trades_per_configuration": dec(total_trades) / len(selected),
            "trades_per_session_per_configuration": (
                dec(total_trades) / (len(selected) * session_count)
            ),
            "avg_hold_seconds_trade_weighted": (
                sum((r["avg_hold_seconds"] * r["trades"] for r in selected), ZERO) / total_trades
            ),
            "avg_realized_pnl_per_trade": (
                sum((r["net_pnl"] for r in selected), ZERO) / total_trades
            ),
            "gross_profit": sum((r["gross_profit"] for r in selected), ZERO),
            "gross_loss": sum((r["gross_loss"] for r in selected), ZERO),
            "net_pnl": sum((r["net_pnl"] for r in selected), ZERO),
            "estimated_slippage": sum((r["estimated_slippage"] for r in selected), ZERO),
            "avg_exit_to_next_entry_seconds": (
                sum(all_gaps, ZERO) / len(all_gaps) if all_gaps else None
            ),
            "median_exit_to_next_entry_seconds": (
                dec(median(all_gaps)) if all_gaps else None
            ),
            "avg_same_day_exit_to_next_entry_seconds": (
                sum(same_day_gaps, ZERO) / len(same_day_gaps) if same_day_gaps else None
            ),
            "median_same_day_exit_to_next_entry_seconds": (
                dec(median(same_day_gaps)) if same_day_gaps else None
            ),
            "same_day_reentries": sum(r["same_day_reentries"] for r in selected),
            "reentries_within_5m": sum(r["reentries_within_5m"] for r in selected),
            "reentries_within_15m": sum(r["reentries_within_15m"] for r in selected),
        })
        # Recover exact weighted win/loss averages from the aggregate gross legs.
        count_wins = sum(r["wins"] for r in selected)
        count_losses = sum(r["losses"] for r in selected)
        by_count[-1]["avg_winning_trade"] = (
            by_count[-1]["gross_profit"] / count_wins if count_wins else None
        )
        by_count[-1]["avg_losing_trade"] = (
            -by_count[-1]["gross_loss"] / count_losses if count_losses else None
        )
        transitions = total_trades - len(selected)
        by_count[-1]["same_day_reentry_rate"] = (
            dec(by_count[-1]["same_day_reentries"]) / transitions if transitions else None
        )
        by_count[-1]["rapid_5m_share_of_same_day"] = (
            dec(by_count[-1]["reentries_within_5m"]) / by_count[-1]["same_day_reentries"]
            if by_count[-1]["same_day_reentries"] else None
        )

    paired_deltas = []
    for symbol in ("IBM", "NVDA", "TSLA"):
        for family in FAMILIES:
            base = next(r for r in records if r["symbol"] == symbol and r["family"] == family
                        and r["sell_count"] == 0)
            for count in (1, 2, 3):
                added = next(r for r in records if r["symbol"] == symbol
                             and r["family"] == family and r["sell_count"] == count)
                paired_deltas.append({
                    "symbol": symbol, "family": family, "sell_count": count,
                    "additional_trades": added["trades"] - base["trades"],
                    "net_pnl_change": added["net_pnl"] - base["net_pnl"],
                    "slippage_change": added["estimated_slippage"] - base["estimated_slippage"],
                    "holding_seconds_change": added["avg_hold_seconds"] - base["avg_hold_seconds"],
                })

    contribution = []
    for strategy, values in sorted(exit_contribution.items(),
                                   key=lambda item: item[1]["trades"], reverse=True):
        contribution.append({
            "strategy": strategy,
            **values,
            "avg_realized_pnl": values["realized_pnl"] / values["trades"],
            "win_rate": dec(values["wins"]) / values["trades"],
            "same_day_reentry_rate": (
                dec(values["same_day_reentries"]) / values["reentry_opportunities"]
                if values["reentry_opportunities"] else None
            ),
            "avg_same_day_reentry_gap_seconds": (
                values["same_day_reentry_gap_seconds"] / values["same_day_reentries"]
                if values["same_day_reentries"] else None
            ),
        })

    for record in records:
        del record["reentry_gaps_seconds"]
        del record["same_day_reentry_gaps_seconds"]

    def serial(value):
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, Counter):
            return dict(value)
        raise TypeError(type(value).__name__)

    output = {
        "development_sessions": session_count,
        "matched_configurations": len(records),
        "by_sell_count": by_count,
        "per_configuration": records,
        "paired_deltas": paired_deltas,
        "technical_sell_contribution": contribution,
    }
    target = CAMPAIGN / "technical_sell_analysis.json"
    target.write_text(json.dumps(output, indent=2, default=serial) + "\n")
    print(target)


if __name__ == "__main__":
    main()
