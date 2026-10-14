"""Campaign preflight: the EFFECTIVE settings a campaign will run with, checked before any job.

The values are read from the planned jobs through the same resolvers the runner uses
(``resolve_execution_assumptions``, ``normalize_stop_spec``, ``resolve_worker_count``), not from
the YAML text.  A campaign file may carry an ``expect`` mapping, and the command line may add
``--expect KEY=VALUE``; any expectation the effective settings do not meet fails the launch
before a single backtest is submitted.  Nothing here asks a human for anything.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mike_trade.modeling.campaign import CampaignPlan

__all__ = ["EXPECTATION_KEYS", "PreflightError", "preflight", "format_preflight", "enforce"]

EXPECTATION_KEYS = (
    "campaign_id",
    "feed",
    "slippage_bps",
    "trail_percent",
    "planned_jobs",
    "workers",
    "model_directed",
    "trading_instruments",
    "excluded_reference_instruments",
    "configurations",
    "evaluation_start",
    "evaluation_end",
)
_MODEL_STRATEGIES = {
    "xgboost_probability_buy",
    "xgboost_probability_sell",
    "lightgbm_probability_buy",
    "lightgbm_probability_sell",
}


class PreflightError(ValueError):
    """The effective campaign settings differ from what was required."""


def _unique(values: set[str], what: str) -> str:
    return next(iter(values)) if len(values) == 1 else f"MIXED({', '.join(sorted(values))}) {what}"


def preflight(plan: CampaignPlan, *, workers: str | int | None = None) -> dict[str, Any]:
    """Resolve what this plan will actually execute (no job is started)."""
    from mike_trade.modeling.campaign import CampaignError
    from mike_trade.research.commands import resolve_execution_assumptions, resolve_worker_count
    from mike_trade.risk.stop_config import normalize_stop_spec

    spec = plan.spec
    if not plan.jobs:
        raise CampaignError("the campaign expands to no jobs")
    slippage: set[str] = set()
    trail: set[str] = set()
    stops: set[str] = set()
    feeds: set[str] = set()
    modes: set[str] = set()
    reviewed: list[Mapping[str, Any]] = []
    for job in plan.jobs:
        if getattr(job, "decision_review", None):
            reviewed.append(job.decision_review)
        slippage.add(str(resolve_execution_assumptions(job.execution).slippage_bps.normalize()))
        stop = normalize_stop_spec(job.protective_stop or {})
        stops.add(str(stop["type"]))
        trail.add(str(stop["trail_percent"].normalize()) if "trail_percent" in stop else "n/a")
        feeds.add(str(job.feed))
        for strategy_id, values in (job.parameters or {}).items():
            if strategy_id in _MODEL_STRATEGIES:
                modes.add(str(values.get("decision_mode", "MODEL_DIRECTED")))
    requested = workers if workers is not None else spec.max_workers
    resolved = resolve_worker_count(
        requested, len(plan.jobs), memory_per_worker_bytes=spec.worker_memory_mb * 1024 * 1024
    )
    review_summary: dict[str, Any] = {}
    if reviewed:
        review_summary["decision_review"] = {
            "jobs_with_review": len(reviewed),
            "jobs_without_review": len(plan.jobs) - len(reviewed),
            "sources": sorted({str(r.get("source")) for r in reviewed}),
            "modes": sorted({str(r.get("mode", "advisory")) for r in reviewed}),
            "reviewers": sorted({str(r.get("reviewer", "jev")) for r in reviewed}),
            "note": (
                "reviews are produced while jobs run (live = OpenRouter calls, deduplicated by "
                "input hash; replay = recorded reviews, no calls); planning makes no model calls"
            ),
        }
    return {
        **review_summary,
        "campaign_id": spec.campaign_id,
        "config_file": str(spec.source),
        "config_sha256": spec.config_sha256,
        "trading_instruments": len(plan.symbols),
        "trading_instrument_symbols": list(plan.symbols),
        "excluded_reference_instruments": list(plan.excluded_reference_symbols),
        "configurations": [item.name for item in spec.configurations],
        "planned_jobs": len(plan.jobs),
        "feed": _unique(feeds, "feeds"),
        "evaluation_start": spec.window.start.isoformat(),
        "evaluation_end": spec.window.end.isoformat(),
        "slippage_bps": _unique(slippage, "slippage"),
        "slippage_basis": "per side (applied adversely to every fill)",
        "protective_stop_type": _unique(stops, "stops"),
        "trail_percent": _unique(trail, "trails"),
        "workers_requested": str(requested),
        "workers": resolved,
        "model_directed": bool(modes) and modes == {"MODEL_DIRECTED"},
        "model_decision_modes": sorted(modes),
        "output_directory": str(spec.run_directory),
    }


def format_preflight(report: Mapping[str, Any]) -> str:
    symbols = report["trading_instrument_symbols"]
    shown = ", ".join(symbols[:6]) + (f", … (+{len(symbols) - 6})" if len(symbols) > 6 else "")
    rows = (
        ("campaign", report["campaign_id"]),
        ("config file", report["config_file"]),
        ("config sha256", report["config_sha256"]),
        ("trading instruments", f"{report['trading_instruments']}  [{shown}]"),
        ("excluded reference", ", ".join(report["excluded_reference_instruments"]) or "none"),
        ("configurations", f"{len(report['configurations'])}  [{', '.join(report['configurations'])}]"),
        ("planned experiments", report["planned_jobs"]),
        ("feed", report["feed"]),
        ("evaluation window", f"{report['evaluation_start']} -> {report['evaluation_end']}"),
        ("slippage", f"{report['slippage_bps']} bps {report['slippage_basis']}"),
        ("protective stop", f"{report['protective_stop_type']} {report['trail_percent']}%"),
        ("workers", f"{report['workers']} (requested {report['workers_requested']})"),
        ("model directed", f"{str(report['model_directed']).lower()} {report['model_decision_modes']}"),
        *(
            (
                ("decision review", (
                    f"{r['jobs_with_review']} job(s) {r['modes']} source={r['sources']} "
                    f"reviewer={r['reviewers']}; {r['jobs_without_review']} baseline job(s)"
                )),
            )
            if (r := report.get("decision_review")) else ()
        ),
        ("output directory", report["output_directory"]),
    )
    return "campaign preflight (effective settings)\n" + "\n".join(
        f"  {name:<20} {value}" for name, value in rows
    )


def _canonical(key: str, value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(sorted(str(item).strip() for item in value))
    text = str(value).strip()
    if key in ("evaluation_start", "evaluation_end"):
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    if key == "model_directed":
        return text.lower()
    if key in ("slippage_bps", "trail_percent", "planned_jobs", "workers", "trading_instruments"):
        try:
            return format(Decimal(text).normalize(), "f")
        except InvalidOperation:
            return text
    if key == "excluded_reference_instruments":
        return ",".join(sorted(part.strip() for part in text.split(",") if part.strip()))
    return text


def enforce(report: Mapping[str, Any], expectations: Mapping[str, object]) -> None:
    """Raise :class:`PreflightError` unless every expectation matches the effective value."""
    unknown = sorted(set(expectations) - set(EXPECTATION_KEYS))
    if unknown:
        raise PreflightError(f"unknown expectation key(s) {unknown}; use {list(EXPECTATION_KEYS)}")
    key_alias = {"configurations": "configurations"}
    mismatches = []
    for key, wanted in expectations.items():
        actual = report[key_alias.get(key, key)]
        if key == "configurations" and not isinstance(wanted, (list, tuple)):
            actual = len(actual)  # an integer expectation counts the configurations
        try:
            same = _canonical(key, wanted) == _canonical(key, actual)
        except ValueError as exc:
            raise PreflightError(f"expectation {key}={wanted!r} is not valid: {exc}") from exc
        if not same:
            mismatches.append(f"{key}: required {wanted!r}, effective {actual!r}")
    if mismatches:
        raise PreflightError(
            "effective campaign settings differ from the requirements; nothing was started: "
            + "; ".join(mismatches)
        )


def dump(report: Mapping[str, Any]) -> str:
    return json.dumps(report, sort_keys=True, indent=2, default=str)
