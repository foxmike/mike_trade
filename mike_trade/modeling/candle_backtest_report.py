"""Consolidated per-configuration comparison of a finished model-directed campaign (read-only).

Reads the campaign report written by :func:`mike_trade.modeling.campaign.run_campaign`
(``report/trades.csv``, ``per_symbol.csv``, ``baseline_buy_and_hold.csv``) and the campaign's own
``campaign.yaml`` and produces, for every configuration x symbol run:

* trades, win rate, average and total net profit/loss, profit factor, maximum drawdown;
* average holding time and the exit mix (protective trailing stop / end-of-day / anything else);
* the actual buy and sell prices (full ledger in ``runs/<run>/trades.csv``);
* the slippage impact: the modeled adverse slippage per side recomputed from the recorded fill
  prices, the net result before it, and the backtest's own fill-versus-bar-open estimate;
* the buy-and-hold result over the same window for the same symbol.

Nothing is simulated here and no number is invented: every figure is derived from the recorded
fills.  A result is a historical measurement, not a claim about future profitability.

    python -m mike_trade.modeling.candle_backtest_report --campaign-dir DIR --out DIR
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

__all__ = ["build_comparison", "write_comparison", "main"]

_ZERO = Decimal(0)
_BPS = Decimal(10000)
_TRAILING = "PROTECTIVE_STOP"
_END_OF_DAY = "END_OF_DAY"


def _dec(value: object) -> Decimal:
    return Decimal(str(value))


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _run_label(configuration: str, symbol: str) -> str:
    return f"{symbol}_{configuration}"


def _stats(
    trades: Sequence[Mapping[str, str]], slippage_bps: Decimal, capital: Decimal
) -> dict[str, Any]:
    pnls = [_dec(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > _ZERO]
    losses = [p for p in pnls if p < _ZERO]
    gross_win, gross_loss = sum(wins, _ZERO), -sum(losses, _ZERO)
    reasons = Counter(t["exit_reason"] or "UNKNOWN" for t in trades)
    holds = [_dec(t["hold_seconds"]) for t in trades]
    rate = slippage_bps / _BPS
    modeled = _ZERO
    notional = _ZERO
    for t in trades:
        quantity = _dec(t["quantity"])
        entry, exit_ = _dec(t["entry_price"]), _dec(t["exit_price"])
        # fills are the touch price moved ``slippage_bps`` against the trader on each side
        modeled += quantity * (entry * rate / (1 + rate) + exit_ * rate / (1 - rate))
        notional += quantity * (entry + exit_)
    count = len(trades)
    net = sum(pnls, _ZERO)
    return {
        "trades": count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": None if not count else len(wins) / count,
        "average_pnl_per_trade": None if not count else net / count,
        "net_pnl": net,
        "profit_factor": None if gross_loss == _ZERO else gross_win / gross_loss,
        "gross_profit": gross_win,
        "gross_loss": gross_loss,
        "average_winner": None if not wins else gross_win / len(wins),
        "average_loser": None if not losses else -gross_loss / len(losses),
        "average_hold_minutes": None if not count else sum(holds, _ZERO) / count / 60,
        "trailing_stop_exits": reasons.get(_TRAILING, 0),
        "end_of_day_exits": reasons.get(_END_OF_DAY, 0),
        "other_exits": {k: v for k, v in reasons.items() if k not in {_TRAILING, _END_OF_DAY}},
        "average_entry_price": None if not count else sum((_dec(t["entry_price"]) for t in trades), _ZERO) / count,
        "average_exit_price": None if not count else sum((_dec(t["exit_price"]) for t in trades), _ZERO) / count,
        "modeled_slippage": modeled,
        "net_before_modeled_slippage": net + modeled,
        "slippage_bps_of_traded_notional": None if notional == _ZERO else modeled / notional * _BPS,
        "return_on_capital": net / capital,
    }


def build_comparison(campaign_dir: str | Path) -> dict[str, Any]:
    directory = Path(campaign_dir)
    campaign = yaml.safe_load((directory / "campaign.yaml").read_text(encoding="utf-8"))
    capital = _dec(campaign.get("starting_capital", "10000"))
    slippage_bps = _dec((campaign.get("execution") or {}).get("slippage_bps", "0"))
    report_dir = directory / "report"
    trades = _read(report_dir / "trades.csv")
    per_symbol = _read(report_dir / "per_symbol.csv")
    baseline = {row["symbol"]: row for row in _read(report_dir / "baseline_buy_and_hold.csv")}
    batch_failures = json.loads((report_dir / "report.json").read_text(encoding="utf-8")).get(
        "failures", []
    )
    by_run: dict[tuple[str, str], list[dict[str, str]]] = {}
    for trade in trades:
        by_run.setdefault((trade["configuration"], trade["symbol"]), []).append(trade)
    runs: list[dict[str, Any]] = []
    for row in per_symbol:
        key = (row["configuration"], row["symbol"])
        ledger = sorted(by_run.get(key, []), key=lambda t: t["entry_time"])
        stats = _stats(ledger, slippage_bps, capital)
        hold = baseline.get(row["symbol"])
        run = {
            "run": _run_label(*key),
            "configuration": row["configuration"],
            "symbol": row["symbol"],
            **stats,
            "max_drawdown": _dec(row["max_drawdown"]),
            "max_drawdown_fraction_of_capital": _dec(row["max_drawdown"]) / capital,
            "ending_equity": _dec(row["ending_equity"]),
            "backtest_fill_vs_bar_open_slippage": _dec(row["estimated_slippage"]),
            "buy_and_hold_return": None if hold is None else _dec(hold["return"]),
            "buy_and_hold_profit": None if hold is None else _dec(hold["profit_on_capital"]),
            "buy_and_hold_first_open": None if hold is None else _dec(hold["first_open"]),
            "buy_and_hold_last_close": None if hold is None else _dec(hold["last_close"]),
            "ledger": ledger,
        }
        run["net_minus_buy_and_hold"] = (
            None if hold is None else stats["net_pnl"] - _dec(hold["profit_on_capital"])
        )
        runs.append(run)
    runs.sort(key=lambda r: (r["symbol"], r["configuration"]))
    return {
        "campaign_id": campaign["campaign_id"],
        "window": campaign["window"],
        "starting_capital_per_run": capital,
        "slippage_bps_per_side": slippage_bps,
        "protective_stop": campaign.get("protective_stop"),
        "sell_strategies": sorted(
            {name for cfg in (campaign.get("configurations") or {}).values() for name in cfg.get("sell") or ()}
        ),
        "decision_review_configured": "decision_review" in campaign
        or any("decision_review" in cfg for cfg in (campaign.get("configurations") or {}).values()),
        "end_of_day_close": campaign.get("end_of_day_close"),
        "failures": batch_failures,
        "runs": runs,
    }


def _pct(value: Decimal | float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{float(value) * 100:.{digits}f}%"


def _num(value: Decimal | float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{float(value):,.{digits}f}"


def _markdown(data: Mapping[str, Any]) -> str:
    runs: Sequence[Mapping[str, Any]] = data["runs"]
    window = data["window"]
    out = [
        f"# {data['campaign_id']}: model-directed backtest of "
        f"{', '.join(sorted({r['symbol'] for r in runs}))}",
        "",
        f"Window {window['start']} → {window['end']} (the models' out-of-sample evaluation window). "
        f"Starting capital {_money(data['starting_capital_per_run'])} per run; "
        f"{data['slippage_bps_per_side']} bps adverse slippage per side; protective "
        f"{(data['protective_stop'] or {}).get('trail_percent')}% trailing stop; end-of-day "
        f"liquidation at {(data['end_of_day_close'] or {}).get('at')}; "
        f"{'SELL strategies: ' + ', '.join(data['sell_strategies']) if data['sell_strategies'] else 'no SELL strategy'}; "
        f"JEV review {'CONFIGURED' if data['decision_review_configured'] else 'off'}. "
        "Historical measurement only — not a forecast.",
        "",
    ]
    if data["failures"]:
        out += [f"**{len(data['failures'])} job(s) failed**: see `failures` in comparison.json.", ""]
    out += [
        "| Run | Trades | Win rate | Avg P/L | Net P/L | Profit factor | Max DD | Avg hold (min) "
        "| Trail stops | EOD exits | Buy & hold |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in runs:
        out.append(
            f"| {r['symbol']} · {r['configuration']} | {r['trades']} | {_pct(r['win_rate'], 1)} "
            f"| {_num(r['average_pnl_per_trade'])} | {_num(r['net_pnl'])} "
            f"| {_num(r['profit_factor'])} | {_num(r['max_drawdown'])} "
            f"| {_num(r['average_hold_minutes'], 1)} | {r['trailing_stop_exits']} "
            f"| {r['end_of_day_exits']} | {_num(r['buy_and_hold_profit'])} ({_pct(r['buy_and_hold_return'])}) |"
        )
    out += ["", "Money is in account currency on the per-run starting capital; Max DD is the "
            "largest peak-to-trough fall of the recorded equity curve.", "",
            "## Prices and slippage", "",
            "| Run | Avg buy | Avg sell | Modeled slippage | Net before slippage | Slippage (bps of traded notional) "
            "| Backtest fill-vs-open estimate |",
            "|---|---:|---:|---:|---:|---:|---:|"]
    for r in runs:
        out.append(
            f"| {r['symbol']} · {r['configuration']} | {_num(r['average_entry_price'], 4)} "
            f"| {_num(r['average_exit_price'], 4)} | {_num(r['modeled_slippage'])} "
            f"| {_num(r['net_before_modeled_slippage'])} | {_num(r['slippage_bps_of_traded_notional'])} "
            f"| {_num(r['backtest_fill_vs_bar_open_slippage'])} |"
        )
    out += [
        "",
        "Every fill is already moved against the trader (buys up, sells down), so the net figures "
        "above include slippage; *Net before slippage* adds the modeled amount back. The last "
        "column is the backtest's own `|fill − bar open| × quantity` estimate, which also "
        "contains genuine price movement within the fill minute, so it is not comparable to the "
        "modeled figure. Every individual buy and sell price is in `runs/<run>/trades.csv`.",
        "",
        "## Exit mix and buy-and-hold",
        "",
        "| Run | Trail stops | EOD | Other | Winners | Losers | Avg winner | Avg loser "
        "| Buy&hold first open → last close | Strategy − buy&hold |",
        "|---|---:|---:|---|---:|---:|---:|---:|---|---:|",
    ]
    for r in runs:
        out.append(
            f"| {r['symbol']} · {r['configuration']} | {r['trailing_stop_exits']} "
            f"| {r['end_of_day_exits']} | {json.dumps(r['other_exits']) if r['other_exits'] else '—'} "
            f"| {r['wins']} | {r['losses']} | {_num(r['average_winner'])} | {_num(r['average_loser'])} "
            f"| {_num(r['buy_and_hold_first_open'])} → {_num(r['buy_and_hold_last_close'])} "
            f"| {_num(r['net_minus_buy_and_hold'])} |"
        )
    out += [
        "",
        "Buy-and-hold buys the whole starting capital at the first regular-session open of the "
        "window and holds to the last close (no costs, stops or position limits). Each strategy "
        "run trades a fixed share count per entry from the same starting capital and holds no "
        "position overnight, so the two are reference points rather than like-for-like.",
        "",
    ]
    return "\n".join(out)


_CSV_FIELDS = (
    "run", "symbol", "configuration", "trades", "wins", "losses", "win_rate",
    "average_pnl_per_trade", "net_pnl", "profit_factor", "gross_profit", "gross_loss",
    "average_winner", "average_loser", "max_drawdown", "max_drawdown_fraction_of_capital",
    "average_hold_minutes", "trailing_stop_exits", "end_of_day_exits", "average_entry_price",
    "average_exit_price", "modeled_slippage", "net_before_modeled_slippage",
    "slippage_bps_of_traded_notional", "backtest_fill_vs_bar_open_slippage",
    "buy_and_hold_return", "buy_and_hold_profit", "net_minus_buy_and_hold", "ending_equity",
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def write_comparison(campaign_dir: str | Path, out_dir: str | Path) -> dict[str, Path]:
    data = build_comparison(campaign_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {"markdown": out / "comparison.md", "csv": out / "comparison.csv",
             "json": out / "comparison.json"}
    paths["markdown"].write_text(_markdown(data), encoding="utf-8")
    with paths["csv"].open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for run in data["runs"]:
            writer.writerow({k: ("" if run.get(k) is None else run.get(k)) for k in _CSV_FIELDS})
    summary = {**data, "runs": [{k: v for k, v in r.items() if k != "ledger"} for r in data["runs"]]}
    paths["json"].write_text(
        json.dumps(_jsonable(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for run in data["runs"]:
        run_dir = out / "runs" / run["run"]
        run_dir.mkdir(parents=True, exist_ok=True)
        ledger = run["ledger"]
        columns = list(ledger[0]) if ledger else ["configuration", "symbol"]
        with (run_dir / "trades.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(ledger)
        (run_dir / "summary.json").write_text(
            json.dumps(_jsonable({k: v for k, v in run.items() if k != "ledger"}), indent=2,
                       sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--campaign-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    paths = write_comparison(args.campaign_dir, args.out)
    print((paths["markdown"]).read_text(encoding="utf-8"))
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
