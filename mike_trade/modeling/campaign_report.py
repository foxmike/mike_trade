# ruff: noqa: E501  (Markdown table rows are intentionally one line each)
"""JSON, CSV and Markdown reports for a model-driven backtest campaign.

Every number is computed from recorded artifacts of the executed runs: the per-run trade ledgers
(entry/exit times and prices, fees, P&L), the batch job records (costs, slippage estimate, ending
equity), the runner's audit rows (model decisions), the pinned model manifests and their
evaluation sidecars, and the immutable bar snapshots (buy-and-hold baseline).  Nothing is
estimated beyond what the existing runner already labels as an estimate, and failed or skipped
jobs are listed rather than dropped.  Each configuration is an independent set of single-symbol
accounts, each starting with the campaign's capital.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mike_trade.modeling.campaign import CampaignPlan
from mike_trade.modeling.model_reports import NOT_RECORDED, feature_importance_rows

__all__ = ["build_campaign_report", "write_campaign_report", "render_campaign_markdown"]

_NY = ZoneInfo("America/New_York")
_ZERO = Decimal(0)
_CENT = Decimal("0.01")
_RATIO = Decimal("0.000001")
_TRADE_COLUMNS = (
    "configuration",
    "symbol",
    "entry_time",
    "entry_price",
    "exit_time",
    "exit_price",
    "quantity",
    "gross_pnl",
    "costs",
    "pnl",
    "hold_seconds",
    "entry_strategy",
    "exit_strategy",
    "exit_reason",
)


def _dec(value: object) -> Decimal:
    return Decimal(str(value))


def _money(value: Decimal) -> str:
    return str(value.quantize(_CENT))


def _ratio(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(_RATIO))


def _stats(trades: Sequence[Mapping[str, Any]], capital: Decimal) -> dict[str, Any]:
    """Trade statistics from ledger rows; ``capital`` is the total starting capital deployed."""
    pnls = [_dec(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > _ZERO]
    losses = [p for p in pnls if p < _ZERO]
    net = sum(pnls, _ZERO)
    gross_loss = -sum(losses, _ZERO)
    ordered = sorted(trades, key=lambda t: (str(t["exit_time"]), str(t["symbol"])))
    equity, peak = capital, capital
    drawdown_abs, drawdown_pct = _ZERO, _ZERO
    for trade in ordered:
        equity += _dec(trade["pnl"])
        peak = max(peak, equity)
        drawdown_abs = max(drawdown_abs, peak - equity)
        if peak > _ZERO:
            drawdown_pct = max(drawdown_pct, (peak - equity) / peak)
    count = len(pnls)
    return {
        "trades": count,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": count - len(wins) - len(losses),
        "win_rate": _ratio(Decimal(len(wins)) / count) if count else None,
        "net_profit": _money(net),
        "gross_profit": _money(sum(wins, _ZERO)),
        "gross_loss": _money(-gross_loss),
        "return_on_capital": _ratio(net / capital) if capital > _ZERO else None,
        "profit_factor": _ratio(sum(wins, _ZERO) / gross_loss) if gross_loss > _ZERO else None,
        "average_winner": _money(sum(wins, _ZERO) / len(wins)) if wins else None,
        "average_loser": _money(sum(losses, _ZERO) / len(losses)) if losses else None,
        "expectancy_per_trade": _money(net / count) if count else None,
        "max_drawdown": _money(drawdown_abs),
        "max_drawdown_fraction": _ratio(drawdown_pct),
        "trading_costs": _money(sum((_dec(t["costs"]) for t in trades), _ZERO)),
    }


def _load_ledger(path: str | Path) -> list[dict[str, Any]]:
    return list(json.loads(Path(path).read_text(encoding="utf-8"))["trades"])


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _audit_summary(
    results_file: Path,
) -> tuple[dict[str, dict[str, Any]], Counter[str]]:
    """Model decisions per strategy and refused-entry reasons from one run's audit artifact."""
    document = json.loads(results_file.read_text(encoding="utf-8"))
    refusals: Counter[str] = Counter(
        str(item.get("reason"))
        for item in document.get("decisions", ())
        if item.get("kind") == "ENTRY_REFUSED" and item.get("gate") == "MARKET_STATUS"
    )
    summary: dict[str, dict[str, Any]] = {}
    for row in document.get("evaluations", ()):
        audit = row.get("model_audit")
        if not audit:
            continue
        entry = summary.setdefault(
            row["strategy"], {"evaluated": 0, "signals": 0, "unavailable": 0, "reasons": Counter()}
        )
        reason = str(json.loads(audit).get("reason"))
        entry["evaluated"] += 1
        entry["signals"] += 1 if row.get("signal_id") else 0
        entry["unavailable"] += 1 if reason.startswith("PREDICTION_UNAVAILABLE") else 0
        entry["reasons"][reason] += 1
    return summary, refusals


def _buy_and_hold(plan: CampaignPlan) -> dict[str, Any]:
    from mike_trade.research.commands import _cached_snapshot_bars
    from mike_trade.scheduling.calendar import session_for

    spec = plan.spec
    capital = Decimal(spec.starting_capital)
    rows: list[dict[str, Any]] = []
    for symbol in plan.symbols:
        bars = _cached_snapshot_bars(
            str(spec.dataset_root.resolve()),
            symbol,
            spec.window.start.isoformat(),
            spec.window.end.isoformat(),
            plan.dataset_ids[symbol],
            feed=spec.feed,
        )
        regular = [
            bar
            for bar in bars
            if session_for(bar.timestamp.astimezone(_NY).date()).open_utc
            <= bar.timestamp
            < session_for(bar.timestamp.astimezone(_NY).date()).close_utc
        ]
        if not regular:
            continue
        first, last = regular[0], regular[-1]
        gain = (_dec(last.close) / _dec(first.open)) - 1
        rows.append(
            {
                "symbol": symbol,
                "first_open": str(first.open),
                "last_close": str(last.close),
                "return": _ratio(gain),
                "profit_on_capital": _money(capital * gain),
            }
        )
    mean_return = sum((Decimal(r["return"]) for r in rows), _ZERO) / len(rows) if rows else None
    return {
        "kind": "buy_and_hold_per_symbol",
        "description": (
            "Buy the first regular-session open of the window with the full starting capital and "
            "hold to the last close; no costs, stops, or position limits."
        ),
        "symbols": rows,
        "mean_return": _ratio(mean_return),
        "mean_profit_on_capital": None if mean_return is None else _money(capital * mean_return),
        "cash_baseline": "A strategy that never trades earns exactly 0.00.",
    }


def _model_section(plan: CampaignPlan) -> list[dict[str, Any]]:
    from mike_trade.modeling.artifacts import FileModelRepository

    repository = FileModelRepository(plan.spec.model_root)
    out: list[dict[str, Any]] = []
    for name, manifest in plan.manifests.items():
        pin = plan.spec.models[name]
        evaluation: dict[str, Any] | None = None
        if manifest.evaluation_sha256 is not None:
            evaluation = json.loads(
                repository.get_evaluation(manifest.model_id, manifest.model_version)
            )
        counts = (evaluation or {}).get("dataset_counts")
        metrics = (evaluation or {}).get("metrics") or {}
        baseline = (evaluation or {}).get("baseline") or {}
        out.append(
            {
                "name": name,
                "strategy": pin.strategy,
                "model_id": manifest.model_id,
                "model_version": manifest.model_version,
                "algorithm": manifest.algorithm,
                "objective": manifest.learning_objective,
                "artifact_sha256": manifest.artifact_sha256,
                "feed": manifest.feed,
                "universe_id": manifest.universe_id,
                "training_ranges": [
                    [r.start.isoformat(), r.end.isoformat()] for r in manifest.training_ranges
                ],
                "validation_ranges": [
                    [r.start.isoformat(), r.end.isoformat()] for r in manifest.validation_ranges
                ],
                "sample_role": plan.sample_roles[name],
                "training_observations": counts if counts is not None else NOT_RECORDED,
                "thresholds": dict(pin.parameters),
                "predictive_metrics": {
                    key: metrics.get(key)
                    for key in (
                        "log_loss",
                        "brier_score",
                        "accuracy",
                        "expected_calibration_error",
                        "roc_auc",
                        "rmse",
                        "mae",
                        "r2",
                    )
                    if metrics.get(key) is not None
                },
                "baseline": {
                    key: baseline.get(key)
                    for key in ("kind", "log_loss", "brier_score", "model_log_loss_improvement")
                    if baseline.get(key) is not None
                },
                "calibration_bins": (evaluation or {}).get("calibration"),
                "class_calibration": (evaluation or {}).get("class_calibration"),
                "top_features": feature_importance_rows(manifest)[:8],
            }
        )
    return out


def build_campaign_report(plan: CampaignPlan, batch: Mapping[str, Any]) -> dict[str, Any]:
    spec = plan.spec
    directory = spec.run_directory
    capital = Decimal(spec.starting_capital)
    by_job: dict[int, Mapping[str, Any]] = {int(j["job_index"]): j for j in batch["jobs"]}
    config_names = [c.name for c in spec.configurations]
    ledgers: dict[str, list[dict[str, Any]]] = {name: [] for name in config_names}
    symbol_rows: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    slippage: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    exit_reasons: dict[str, Counter[str]] = {name: Counter() for name in config_names}
    decisions: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in config_names}
    market_refusals: dict[str, Counter[str]] = {name: Counter() for name in config_names}
    runs: Counter[str] = Counter()
    for index, job in enumerate(plan.jobs):
        record = by_job.get(index)
        name = job.strategy
        if record is None or record.get("status") != "completed":
            failures.append(
                {
                    "job_index": index,
                    "job_id": (record or {}).get("job_id"),
                    "configuration": name,
                    "symbol": job.symbol,
                    "exception_type": (record or {}).get("exception_type")
                    or (record or {}).get("error_type"),
                    "error": (record or {}).get("error", "no result"),
                    "traceback": (record or {}).get("traceback"),
                    "timestamp": (record or {}).get("timestamp"),
                }
            )
            continue
        runs[name] += 1
        trades = _load_ledger(record["trade_ledger_file"])
        for trade in trades:
            ledgers[name].append({**trade, "configuration": name})
        slippage[name] += _dec(record.get("estimated_slippage", "0"))
        for reason, count in (record.get("exit_reasons") or {}).items():
            exit_reasons[name][str(reason)] += int(count)
        row = _stats(trades, capital)
        symbol_rows.append(
            {
                "configuration": name,
                "symbol": job.symbol,
                **row,
                "estimated_slippage": _money(_dec(record.get("estimated_slippage", "0"))),
                "ending_equity": _money(_dec(record["ending_equity"])),
                "run_max_drawdown_fraction": record.get("max_drawdown"),
                "run_id": record["run_id"],
            }
        )
        results_files = sorted(directory.glob(f"executions/*/{record['run_id']}.results.json"))
        entry: dict[str, Any] = {
            "configuration": name,
            "symbol": job.symbol,
            "run_id": record["run_id"],
            "results_hash": record.get("results_hash"),
            "dataset_id": record.get("dataset_id"),
            "trade_ledger_file": record["trade_ledger_file"],
            "results_file": None,
            "results_file_sha256": None,
        }
        if results_files:
            entry["results_file"] = str(results_files[-1])
            entry["results_file_sha256"] = _sha256_file(results_files[-1])
            if spec.market_status_enabled or any(
                s.endswith(("_probability_buy", "_probability_sell"))
                for s in job.buy_strategies + job.sell_strategies
            ):
                model_data, refused = _audit_summary(results_files[-1])
                market_refusals[name].update(refused)
                for strategy, data in model_data.items():
                    merged = decisions[name].setdefault(
                        strategy,
                        {"evaluated": 0, "signals": 0, "unavailable": 0, "reasons": Counter()},
                    )
                    for key in ("evaluated", "signals", "unavailable"):
                        merged[key] += data[key]
                    merged["reasons"].update(data["reasons"])
        audit.append(entry)

    configurations: list[dict[str, Any]] = []
    period_rows: list[dict[str, Any]] = []
    for configuration in spec.configurations:
        name = configuration.name
        trades = ledgers[name]
        total_capital = capital * runs[name]
        stats = _stats(trades, total_capital) if runs[name] else {}
        periods: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for trade in trades:
            exit_time = datetime.fromisoformat(str(trade["exit_time"]))
            periods[exit_time.astimezone(_NY).strftime("%Y-%m")].append(trade)
        for period, items in sorted(periods.items()):
            period_rows.append(
                {
                    "configuration": name,
                    "period": period,
                    **{
                        k: v
                        for k, v in _stats(items, total_capital).items()
                        if k not in {"max_drawdown", "max_drawdown_fraction"}
                    },
                }
            )
        configurations.append(
            {
                "name": name,
                "description": configuration.description,
                "buy": list(configuration.buy),
                "sell": list(configuration.sell),
                "buy_strategy_ids": list(
                    next(j for j in plan.jobs if j.strategy == name).buy_strategies
                ),
                "sell_strategy_ids": list(
                    next(j for j in plan.jobs if j.strategy == name).sell_strategies
                ),
                "symbol_runs": runs[name],
                "failed_runs": sum(1 for f in failures if f["configuration"] == name),
                "total_capital": _money(total_capital),
                **stats,
                "estimated_slippage": _money(slippage[name]),
                "exit_reasons": dict(sorted(exit_reasons[name].items())),
                "market_status_entry_refusals": dict(sorted(market_refusals[name].items())),
                "symbols_with_trades": len({t["symbol"] for t in trades}),
                "model_decisions": {
                    strategy: {
                        "evaluated_bars": data["evaluated"],
                        "signals": data["signals"],
                        "prediction_unavailable": data["unavailable"],
                        "reasons": dict(sorted(data["reasons"].items())),
                    }
                    for strategy, data in sorted(decisions[name].items())
                },
            }
        )
    warnings: list[str] = []
    for name, role in plan.sample_roles.items():
        if role != "out_of_sample_development_test":
            warnings.append(f"model {name}: window is {role}; results are not out-of-sample")
    if plan.skipped:
        warnings.append(
            f"{len(plan.skipped)} symbol(s) skipped for missing {spec.feed.upper()} data"
        )
    if failures:
        warnings.append(f"{len(failures)} job(s) failed; see failures")
    return {
        "schema": "mike_trade_model_backtest_report",
        "schema_version": 1,
        "campaign_id": spec.campaign_id,
        "description": spec.description,
        "config_sha256": spec.config_sha256,
        "feed": spec.feed,
        "window": {"start": spec.window.start.isoformat(), "end": spec.window.end.isoformat()},
        "holdout_evaluated": False,
        "universe_id": spec.universe_id,
        "symbols": list(plan.symbols),
        "skipped_symbols": dict(plan.skipped),
        "dataset_ids": dict(plan.dataset_ids),
        "reference_dataset_ids": dict(plan.reference_dataset_ids),
        "starting_capital_per_account": spec.starting_capital,
        "execution_assumptions": dict(spec.execution),
        "protective_stop": dict(spec.protective_stop),
        "end_of_day_close": spec.end_of_day_close,
        "market_status_enabled": spec.market_status_enabled,
        "accounting_note": (
            "Each (configuration, symbol) is an independent account with the stated starting "
            "capital; configuration totals sum those accounts. Drawdown is computed on the "
            "combined realized-P&L curve of the configuration, ordered by exit time."
        ),
        "models": _model_section(plan),
        "configurations": configurations,
        "per_symbol": symbol_rows,
        "per_period": period_rows,
        "trades": [
            {
                key: str(t.get(key, "")) if t.get(key) is not None else ""
                for key in _TRADE_COLUMNS
                if key != "hold_seconds"
            }
            | {"hold_seconds": str(t.get("hold_duration_seconds", ""))}
            for name in config_names
            for t in sorted(ledgers[name], key=lambda t: (str(t["entry_time"]), str(t["symbol"])))
        ],
        "baselines": _buy_and_hold(plan),
        "warnings": warnings,
        "failures": failures,
        "audit_artifacts": audit,
        "reproduce": (
            f"make model-backtest CAMPAIGN={spec.source} (same campaign bytes, same pinned "
            "snapshots and model artifacts, same seed)"
        ),
        "disclaimer": (
            "Historical simulation results with the stated execution assumptions. They are not "
            "a forecast and do not establish that any model or threshold is profitable."
        ),
    }


def _csv(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(columns), lineterminator="\n", extrasaction="ignore"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in columns})
    return buffer.getvalue()


_SUMMARY_COLUMNS = (
    "name",
    "symbol_runs",
    "trades",
    "wins",
    "losses",
    "breakeven",
    "win_rate",
    "net_profit",
    "return_on_capital",
    "profit_factor",
    "average_winner",
    "average_loser",
    "max_drawdown",
    "max_drawdown_fraction",
    "trading_costs",
    "estimated_slippage",
    "total_capital",
)
_SYMBOL_COLUMNS = (
    "configuration",
    "symbol",
    "trades",
    "wins",
    "losses",
    "breakeven",
    "win_rate",
    "net_profit",
    "return_on_capital",
    "profit_factor",
    "average_winner",
    "average_loser",
    "max_drawdown",
    "trading_costs",
    "estimated_slippage",
    "ending_equity",
    "run_id",
)
_PERIOD_COLUMNS = (
    "configuration",
    "period",
    "trades",
    "wins",
    "losses",
    "breakeven",
    "win_rate",
    "net_profit",
    "profit_factor",
    "trading_costs",
)


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join("—" if c is None else str(c) for c in row) + " |" for row in rows]
    return lines


def render_campaign_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        f"# Model backtest campaign: {report['campaign_id']}",
        "",
        f"*{report['disclaimer']}*",
        "",
        report["description"] or "",
        "",
        f"- Feed: **{report['feed'].upper()}**; window {report['window']['start']} → {report['window']['end']}",
        f"- Universe `{report['universe_id']}`: {len(report['symbols'])} symbols run, {len(report['skipped_symbols'])} skipped",
        f"- Starting capital per account: {report['starting_capital_per_account']}; protective stop `{json.dumps(report['protective_stop'], sort_keys=True)}`",
        f"- Execution assumptions: `{json.dumps(report['execution_assumptions'], sort_keys=True)}`",
        f"- Final holdout evaluated: **{str(report['holdout_evaluated']).lower()}**",
        f"- Campaign file SHA-256: `{report['config_sha256']}`",
        f"- {report['accounting_note']}",
    ]
    if report["warnings"]:
        lines += ["", "## Warnings", ""] + [f"- ⚠ {w}" for w in report["warnings"]]
    lines += ["", "## Configuration comparison", ""]
    lines += _md_table(
        [
            "Configuration",
            "Runs",
            "Trades",
            "W/L/BE",
            "Win rate",
            "Net profit",
            "Return",
            "Profit factor",
            "Avg win",
            "Avg loss",
            "Max DD",
            "Costs",
            "Slippage (est.)",
        ],
        [
            [
                c["name"],
                c["symbol_runs"],
                c.get("trades"),
                f"{c.get('wins')}/{c.get('losses')}/{c.get('breakeven')}",
                c.get("win_rate"),
                c.get("net_profit"),
                c.get("return_on_capital"),
                c.get("profit_factor"),
                c.get("average_winner"),
                c.get("average_loser"),
                c.get("max_drawdown"),
                c.get("trading_costs"),
                c.get("estimated_slippage"),
            ]
            for c in report["configurations"]
        ],
    )
    baselines = report["baselines"]
    lines += [
        "",
        "## Baselines",
        "",
        baselines["description"],
        "",
        f"- Buy-and-hold mean return per symbol: **{baselines['mean_return']}** (profit on capital {baselines['mean_profit_on_capital']})",
        f"- {baselines['cash_baseline']}",
    ]
    for c in report["configurations"]:
        lines += ["", f"## {c['name']}", ""]
        if c["description"]:
            lines += [c["description"], ""]
        lines += [
            f"- BUY: {', '.join(c['buy_strategy_ids'])}; SELL: {', '.join(c['sell_strategy_ids']) or 'none (protective stop only)'}",
            f"- Exit reasons: {json.dumps(c['exit_reasons'], sort_keys=True)}",
            f"- Symbols with trades: {c['symbols_with_trades']} of {c['symbol_runs']} runs",
            "- Entries refused by the market-status gate: "
            + (
                json.dumps(c["market_status_entry_refusals"], sort_keys=True)
                if c["market_status_entry_refusals"]
                else "none"
            ),
        ]
        for strategy, data in c["model_decisions"].items():
            lines.append(
                f"- Model `{strategy}`: {data['evaluated_bars']} audited bars, {data['signals']} signals, "
                f"{data['prediction_unavailable']} unavailable; reasons {json.dumps(data['reasons'], sort_keys=True)}"
            )
    symbol_rows = report["per_symbol"]
    lines += ["", "## Per-stock results", ""]
    if len(symbol_rows) <= 30:
        shown, note = symbol_rows, ""
    else:
        ranked = sorted(symbol_rows, key=lambda r: Decimal(r["net_profit"]))
        shown = ranked[:10] + ranked[-10:]
        note = f"Showing the 10 worst and 10 best of {len(symbol_rows)} account runs; the full table is `per_symbol.csv`."
    if note:
        lines += [note, ""]
    lines += _md_table(
        ["Configuration", "Symbol", "Trades", "Win rate", "Net profit", "Profit factor", "Costs"],
        [
            [
                r["configuration"],
                r["symbol"],
                r["trades"],
                r["win_rate"],
                r["net_profit"],
                r["profit_factor"],
                r["trading_costs"],
            ]
            for r in shown
        ],
    )
    lines += ["", "## Per-period results (by exit month, New York)", ""]
    lines += _md_table(
        ["Configuration", "Period", "Trades", "Win rate", "Net profit", "Profit factor"],
        [
            [
                r["configuration"],
                r["period"],
                r["trades"],
                r["win_rate"],
                r["net_profit"],
                r["profit_factor"],
            ]
            for r in report["per_period"]
        ],
    )
    lines += ["", "## Models", ""]
    for m in report["models"]:
        obs = m["training_observations"]
        fit_rows = obs.get("fit") if isinstance(obs, dict) else obs
        lines += [
            f"### {m['name']} — `{m['model_id']}@{m['model_version']}` ({m['algorithm']})",
            "",
            f"- Artifact `{m['artifact_sha256']}`; feed {m['feed'].upper()}; universe `{m['universe_id']}`",
            f"- Training range(s): {m['training_ranges']}; validation: {m['validation_ranges']}",
            f"- Training observations (fit rows): **{fit_rows}**",
            f"- Backtest window relation: **{m['sample_role']}**",
            f"- Predictive metrics (held-out labels): `{json.dumps(m['predictive_metrics'], sort_keys=True)}`; baseline `{json.dumps(m['baseline'], sort_keys=True)}`",
            f"- Top features by gain: {', '.join(f'{r["feature"]} ({r["gain_share"]:.2f})' for r in m['top_features'][:5])}",
            "",
        ]
    if report["failures"]:
        lines += ["## Failed jobs", ""] + [
            f"- {f['configuration']} {f['symbol']}: {f['error']}" for f in report["failures"]
        ]
    lines += [
        "",
        "## Reproducibility",
        "",
        f"- {report['reproduce']}",
        f"- {len(report['audit_artifacts'])} run audit files (with SHA-256) are listed in `report.json` under `audit_artifacts`; model predictions and decisions per bar are in each run's `model_audit` rows.",
        f"- Dataset snapshots pinned: {len(report['dataset_ids'])} primary, {len(report['reference_dataset_ids'])} reference.",
    ]
    return "\n".join(lines) + "\n"


def write_campaign_report(
    plan: CampaignPlan, batch: Mapping[str, Any], directory: str | Path
) -> dict[str, str]:
    report = build_campaign_report(plan, batch)
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "report.json": json.dumps(report, sort_keys=True, indent=2, default=str) + "\n",
        "report.md": render_campaign_markdown(report),
        "summary.csv": _csv(report["configurations"], _SUMMARY_COLUMNS),
        "per_symbol.csv": _csv(report["per_symbol"], _SYMBOL_COLUMNS),
        "per_period.csv": _csv(report["per_period"], _PERIOD_COLUMNS),
        "trades.csv": _csv(report["trades"], _TRADE_COLUMNS),
        "baseline_buy_and_hold.csv": _csv(
            report["baselines"]["symbols"],
            ["symbol", "first_open", "last_close", "return", "profit_on_capital"],
        ),
        "models.csv": _csv(
            [
                {
                    "name": m["name"],
                    "model": f"{m['model_id']}@{m['model_version']}",
                    "algorithm": m["algorithm"],
                    "artifact_sha256": m["artifact_sha256"],
                    "feed": m["feed"],
                    "sample_role": m["sample_role"],
                    "fit_rows": (m["training_observations"] or {}).get("fit")
                    if isinstance(m["training_observations"], dict)
                    else "",
                    **m["predictive_metrics"],
                }
                for m in report["models"]
            ],
            [
                "name",
                "model",
                "algorithm",
                "artifact_sha256",
                "feed",
                "sample_role",
                "fit_rows",
                "log_loss",
                "brier_score",
                "accuracy",
                "expected_calibration_error",
                "rmse",
            ],
        ),
    }
    paths: dict[str, str] = {}
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
        paths[name] = str(out / name)
    return paths
