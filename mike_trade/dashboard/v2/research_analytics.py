"""Read-only analytics derived from recorded backtest artifacts.

Every series here is reconstructed strictly from what a run recorded: the
mark-to-market ``equity_curve`` and the closed-cycle ledger rebuilt from fills.
Nothing is simulated or extrapolated. Equity-basis figures (which include the
unrealized P&L of open positions) and closed-trade realized figures are kept
separate and labelled, because they are not interchangeable.
"""

from __future__ import annotations

import statistics
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

ZERO = Decimal("0")
HUNDRED = Decimal("100")
ET = ZoneInfo("America/New_York")

METRIC_DEFINITIONS: dict[str, str] = {
    "net_pnl": (
        "Ending equity minus starting capital, from the recorded equity curve. "
        "Mark-to-market: includes unrealized P&L of any position still open at the end."
    ),
    "return_percent": "Net P&L divided by starting capital, in percent (equity basis).",
    "realized_pnl": (
        "Sum of P&L over closed trades, from entry/exit fill prices. Excludes open positions."
    ),
    "win_rate": (
        "Closed trades with positive realized P&L divided by closed trades with a known P&L."
    ),
    "profit_factor": (
        "Gross profit of winning trades divided by gross loss of losing trades. "
        "Unavailable when there are no losing trades."
    ),
    "max_drawdown": (
        "Largest peak-to-trough fall of the equity curve as a percent of the running peak. "
        "The peak starts at starting capital."
    ),
    "average_win": "Mean realized P&L of winning closed trades.",
    "average_loss": "Mean realized P&L of losing closed trades (negative).",
    "expectancy": "Mean realized P&L per closed trade (net of nothing but fill prices).",
    "payoff_ratio": "Average win divided by the absolute average loss.",
    "top3_profit_share": (
        "Share of gross profit contributed by the three largest winning trades. "
        "High values mean results depend on a few trades."
    ),
    "equity_curve": (
        "Account equity (cash plus marked positions) at each recorded bar. "
        "Downsampled for display; extremes within each bucket are preserved."
    ),
    "drawdown_curve": "Percent below the running equity peak at each point of the equity curve.",
    "cumulative_trade_pnl": (
        "Running total of realized P&L of closed trades, stepped at each exit. "
        "Not an account equity curve: it ignores open-position marks and idle time."
    ),
}


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _dec(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def _text(value: Decimal | float | None, places: int = 4) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(round(value, places))
    return str(round(value, places))


def downsample(
    points: Sequence[Mapping[str, Any]], max_points: int, value_key: str
) -> list[dict[str, Any]]:
    """Reduce a time-ordered series, keeping the first/last point and each bucket's extremes."""
    if max_points < 4 or len(points) <= max_points:
        return [dict(p) for p in points]
    buckets = max(1, (max_points - 2) // 2)
    size = len(points) / buckets
    keep: set[int] = {0, len(points) - 1}
    for b in range(buckets):
        lo = int(b * size)
        hi = max(lo + 1, int((b + 1) * size))
        window = range(lo, min(hi, len(points)))
        keep.add(min(window, key=lambda i: points[i][value_key]))
        keep.add(max(window, key=lambda i: points[i][value_key]))
    return [dict(points[i]) for i in sorted(keep)]


def equity_analysis(
    curve: Sequence[Mapping[str, Any]], starting_capital: Decimal | None, max_points: int
) -> dict[str, Any]:
    """Equity + drawdown series (full-resolution math, downsampled output) and drawdown detail."""
    series: list[dict[str, Any]] = []
    for point in curve:
        equity = _dec(point.get("equity"))
        at = point.get("time")
        if equity is not None and isinstance(at, str):
            series.append({"time": at, "equity": float(equity), "_d": equity})

    if not series:
        return {
            "equity": [],
            "total_points": 0,
            "drawdown": [],
            "detail": {"available": False},
            "peak_equity": None,
            "lowest_equity": None,
        }

    peak = starting_capital if starting_capital and starting_capital > ZERO else series[0]["_d"]
    peak_time = series[0]["time"]
    drawdown_series: list[dict[str, Any]] = []
    worst = ZERO
    worst_amount = ZERO
    worst_peak = peak
    worst_peak_time = peak_time
    worst_trough = peak
    worst_trough_time = peak_time
    for item in series:
        equity = item["_d"]
        if equity > peak:
            peak = equity
            peak_time = item["time"]
        depth = (peak - equity) / peak * HUNDRED if peak > ZERO else ZERO
        drawdown_series.append({"time": item["time"], "drawdown_percent": float(depth)})
        if depth > worst:
            worst = depth
            worst_amount = peak - equity
            worst_peak, worst_peak_time = peak, peak_time
            worst_trough, worst_trough_time = equity, item["time"]

    recovered_at = None
    if worst > ZERO:
        seen_trough = False
        for item in series:
            if item["time"] == worst_trough_time:
                seen_trough = True
            elif seen_trough and item["_d"] >= worst_peak:
                recovered_at = item["time"]
                break

    equities = [item["_d"] for item in series]
    detail = {
        "available": True,
        "max_drawdown_percent": _text(worst),
        "max_drawdown_amount": _text(worst_amount, 2),
        "peak_equity": _text(worst_peak, 2),
        "peak_time": worst_peak_time,
        "trough_equity": _text(worst_trough, 2),
        "trough_time": worst_trough_time,
        "recovered": recovered_at is not None,
        "recovered_at": recovered_at,
    }
    public = [{"time": s["time"], "equity": s["equity"]} for s in series]
    return {
        "equity": downsample(public, max_points, "equity"),
        "total_points": len(series),
        "drawdown": downsample(drawdown_series, max_points, "drawdown_percent"),
        "detail": detail,
        "peak_equity": _text(max(equities), 2),
        "lowest_equity": _text(min(equities), 2),
    }


def closed_trades(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in records:
        pnl = _dec(r.get("realized_pnl"))
        if r.get("position_status") == "CLOSED" and pnl is not None:
            out.append({**r, "_pnl": pnl, "_exit": _parse_time(r.get("exit_timestamp"))})
    return out


def trade_series(
    records: Iterable[Mapping[str, Any]], max_points: int = 3000
) -> tuple[list[dict[str, Any]], int]:
    """Cumulative realized P&L of closed trades ordered by exit time."""
    trades = [t for t in closed_trades(records) if t["_exit"] is not None]
    trades.sort(key=lambda t: (t["_exit"], t["trade_id"]))
    cumulative = ZERO
    rows: list[dict[str, Any]] = []
    for index, t in enumerate(trades, start=1):
        cumulative += t["_pnl"]
        rows.append({
            "trade_index": index,
            "time": t["exit_timestamp"],
            "trade_id": t["trade_id"],
            "symbol": t["symbol"],
            "trade_pnl": float(t["_pnl"]),
            "cumulative_pnl": float(cumulative),
        })
    total = len(rows)
    if total > max_points:
        rows = downsample(rows, max_points, "cumulative_pnl")
    return rows, total


def pnl_distribution(records: Iterable[Mapping[str, Any]], target_bins: int = 20) -> dict[str, Any]:
    pnls = [t["_pnl"] for t in closed_trades(records)]
    if not pnls:
        return {"available": False, "bins": []}
    values = [float(p) for p in pnls]
    low, high = min(values), max(values)
    summary = {
        "available": True,
        "count": len(values),
        "min": low,
        "max": high,
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "winners": sum(v > 0 for v in values),
        "losers": sum(v < 0 for v in values),
    }
    if high == low:
        summary["bins"] = [{"lower": low, "upper": high, "count": len(values)}]
        return summary
    width = (high - low) / target_bins
    # Align the grid to zero so no bin mixes winners and losers.
    start = (low // width) * width
    count = int((high - start) // width) + 1
    bins = [
        {"lower": start + i * width, "upper": start + (i + 1) * width, "count": 0}
        for i in range(count)
    ]
    for v in values:
        index = min(count - 1, int((v - start) // width))
        bins[index]["count"] += 1
    summary["bins"] = bins
    return summary


def _bucket(day: date, granularity: str) -> date:
    if granularity == "month":
        return day.replace(day=1)
    if granularity == "week":
        return day - timedelta(days=day.weekday())
    return day


def period_performance(
    curve: Sequence[Mapping[str, Any]],
    records: Iterable[Mapping[str, Any]],
    window_start: datetime | None,
    window_end: datetime | None,
    starting_capital: Decimal | None,
) -> dict[str, Any]:
    """Equity change and realized trade P&L bucketed over the evaluation window."""
    span_days = (window_end - window_start).days if window_start and window_end else None
    if span_days is None:
        times = [t for t in (_parse_time(p.get("time")) for p in [*curve[:1], *curve[-1:]]) if t]
        span_days = (times[-1] - times[0]).days if len(times) == 2 else 0
    granularity = "day" if span_days <= 31 else "week" if span_days <= 150 else "month"

    last_equity: OrderedDict[date, Decimal] = OrderedDict()
    for point in curve:
        at = _parse_time(point.get("time"))
        equity = _dec(point.get("equity"))
        if at is None or equity is None:
            continue
        last_equity[_bucket(at.astimezone(ET).date(), granularity)] = equity

    realized: dict[date, list[Decimal]] = {}
    for trade in closed_trades(records):
        if trade["_exit"] is not None:
            key = _bucket(trade["_exit"].astimezone(ET).date(), granularity)
            realized.setdefault(key, []).append(trade["_pnl"])

    keys = sorted(set(last_equity) | set(realized))
    previous = starting_capital
    rows: list[dict[str, Any]] = []
    for key in keys:
        end_equity = last_equity.get(key)
        pnls = realized.get(key, [])
        change = end_equity - previous if end_equity is not None and previous is not None else None
        rows.append({
            "period": key.isoformat(),
            "start_equity": _text(previous, 2),
            "end_equity": _text(end_equity, 2),
            "equity_change": _text(change, 2),
            "return_percent": _text(
                change / previous * HUNDRED if change is not None and previous else None
            ),
            "closed_trades": len(pnls),
            "winning_trades": sum(p > ZERO for p in pnls),
            "realized_pnl": _text(sum(pnls, ZERO), 2) if pnls else "0",
        })
        if end_equity is not None:
            previous = end_equity
    return {"granularity": granularity, "periods": rows}


def holding_seconds(records: Iterable[Mapping[str, Any]]) -> dict[str, float | None]:
    all_s: list[float] = []
    win_s: list[float] = []
    loss_s: list[float] = []
    for t in closed_trades(records):
        opened = _parse_time(t.get("entry_timestamp"))
        if opened is None or t["_exit"] is None:
            continue
        secs = (t["_exit"] - opened).total_seconds()
        all_s.append(secs)
        if t["_pnl"] > ZERO:
            win_s.append(secs)
        elif t["_pnl"] < ZERO:
            loss_s.append(secs)

    def mean(values: list[float]) -> float | None:
        return statistics.fmean(values) if values else None

    return {"all": mean(all_s), "winners": mean(win_s), "losers": mean(loss_s)}


def assumptions_from_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "fee_assumptions",
        "slippage_assumptions",
        "fill_assumptions",
        "ambiguity_assumptions",
        "seed",
        "code_revision",
        "data_provenance",
        "starting_capital",
        "holdout_access",
    )
    return {k: manifest[k] for k in keys if k in manifest}


def total_fees(result: Mapping[str, Any]) -> str | None:
    fees = [_dec(f.get("fee")) for f in result.get("fills", [])]
    known = [f for f in fees if f is not None]
    return str(sum(known, ZERO)) if known else None


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------

COMPARE_FIELDS: tuple[tuple[str, str], ...] = (
    ("symbols", "Symbols"),
    ("buy_strategies", "BUY strategies"),
    ("sell_strategies", "SELL strategies"),
    ("short_strategies", "SHORT strategies"),
    ("cover_strategies", "COVER strategies"),
    ("stop_policy", "Stop policy"),
    ("window_key", "Historical window"),
    ("period_role", "Period role"),
    ("dataset_id", "Dataset"),
    ("starting_capital", "Starting capital"),
    ("config_fingerprint", "Config fingerprint"),
)


def configuration_differences(runs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Fields that take different values across the compared runs."""
    out = []
    for field, label in COMPARE_FIELDS:
        values: dict[str, Any] = {}
        for run in runs:
            if field == "window_key":
                start, end = run.get("window_start"), run.get("window_end")
                value: Any = f"{str(start)[:10]}..{str(end)[:10]}" if start and end else None
            else:
                value = run.get(field)
            values[run["selection_id"]] = value
        distinct = {repr(v) for v in values.values()}
        if len(distinct) > 1:
            out.append({"field": field, "label": label, "values": values})
    return out


def period_overlap(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    starts = [_parse_time(r.get("window_start")) for r in runs]
    ends = [_parse_time(r.get("window_end")) for r in runs]
    if any(s is None for s in starts) or any(e is None for e in ends):
        return {"comparable": False, "reason": "A run has no recorded historical window."}
    latest_start, earliest_end = max(starts), min(ends)  # type: ignore[type-var]
    identical = len({(s, e) for s, e in zip(starts, ends, strict=True)}) == 1
    overlap_days = max(0, (earliest_end - latest_start).days)  # type: ignore[operator]
    return {
        "comparable": identical,
        "identical_windows": identical,
        "overlap_start": latest_start.isoformat() if overlap_days else None,  # type: ignore[union-attr]
        "overlap_end": earliest_end.isoformat() if overlap_days else None,  # type: ignore[union-attr]
        "overlap_days": overlap_days,
        "reason": None if identical else "Runs cover different historical windows.",
    }


def normalized_curve(
    curve: Sequence[Mapping[str, Any]], starting_capital: Decimal | None, max_points: int
) -> list[dict[str, Any]]:
    """Equity indexed to 100 at the starting capital (or first point)."""
    base = starting_capital
    if base is None or base <= ZERO:
        first = next((_dec(p.get("equity")) for p in curve if _dec(p.get("equity"))), None)
        base = first
    if base is None or base <= ZERO:
        return []
    points = []
    for p in curve:
        equity = _dec(p.get("equity"))
        if equity is not None and isinstance(p.get("time"), str):
            points.append({"time": p["time"], "index": float(equity / base * HUNDRED)})
    return downsample(points, max_points, "index")


LEADER_METRICS: tuple[tuple[str, str, bool], ...] = (
    # (metric key on the run, label, higher is better)
    ("net_pnl", "Net P&L", True),
    ("return_percent", "Return", True),
    ("win_rate", "Win rate", True),
    ("profit_factor", "Profit factor", True),
    ("max_drawdown", "Max drawdown", False),
    ("completed_trade_count", "Trade count", True),
)


def metric_leaders(runs: Sequence[Mapping[str, Any]]) -> dict[str, str | None]:
    leaders: dict[str, str | None] = {}
    for key, _, higher in LEADER_METRICS:
        scored = [
            (float(v), r["selection_id"])
            for r in runs
            if (v := r.get(key)) is not None
        ]
        if len(scored) < 2:
            leaders[key] = None
            continue
        best = max(scored) if higher else min(scored)
        # No leader when every run ties.
        leaders[key] = best[1] if len({s for s, _ in scored}) > 1 else None
    return leaders
