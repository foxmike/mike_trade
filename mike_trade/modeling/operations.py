"""Operator services behind the ``model-*`` commands that are not training itself.

* ``model-prepare`` pins the exact data a training run will use and records what it would see.
* ``model-check`` is the offline pre-flight for a trading configuration: it performs the same
  verification the runtime performs at startup, plus the approval check, without starting anything.
* ``model-health`` reads the model status the engine publishes to the operational database.
* ``compare_reports`` lines up several model-backtest reports for the same question.

Nothing here trains inside the trading process, places an order, or modifies a configuration.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mike_trade.modeling.approvals import ApprovalRepository, required_stage_for_mode
from mike_trade.modeling.artifacts import FileModelRepository
from mike_trade.modeling.config import ModelExperimentConfig
from mike_trade.modeling.splits import ExperimentPurpose, authorize_range

__all__ = [
    "lock_path",
    "prepare_training_lock",
    "read_training_lock",
    "check_configuration",
    "read_model_health",
    "compare_reports",
]

#: Bytes per matrix cell on disk (float32) and a conservative multiple for the native library's
#: own copy (XGBoost DMatrix / LightGBM Dataset) while training.
_CELL_BYTES = 4
_TRAINING_COPY_FACTOR = 3


def lock_path(config: ModelExperimentConfig) -> Path:
    return (
        Path(config.model_root) / "prepared" / config.experiment_id / f"{config.config_sha256}.json"
    )


def prepare_training_lock(
    config: ModelExperimentConfig, *, dataset_ids: Mapping[str, str] | None = None
) -> tuple[Path, dict[str, Any]]:
    """Resolve and verify the training data, count what it yields, and write an immutable lock.

    The lock names every snapshot (id and SHA-256), the feed, the exact partitions, and the
    observation counts.  ``model-train --lock`` then uses exactly those snapshots, so data that
    arrives between preparation and training (for example a newer or SIP snapshot) cannot change
    the experiment.  Nothing is stored in the model repository.
    """
    from mike_trade.modeling.training import ModelTrainingService, _range_mapping, prepared_counts

    authorize_range(
        config.ranges.overall, ExperimentPurpose.DEVELOPMENT, experiment_hash=config.config_sha256
    )
    service = ModelTrainingService(config)
    with tempfile.TemporaryDirectory(prefix="mike-trade-prepare-") as directory:
        prepared = service.prepare(directory, dataset_ids=dataset_ids)
        counts = prepared_counts(prepared, config.class_labels)
    selection = prepared.selection
    cells = counts["fit"] * counts["features"]
    payload: dict[str, Any] = {
        "schema": "mike_trade_training_lock",
        "schema_version": 1,
        "experiment_id": config.experiment_id,
        "config_sha256": config.config_sha256,
        "algorithm": config.algorithm.value,
        "role": config.role.value,
        "model_id": config.model_id,
        "model_version": config.model_version,
        "feed": selection.feed,
        "timeframe": selection.timeframe,
        "adjustment": selection.adjustment,
        "universe_id": selection.universe_id,
        "ranges": _range_mapping(config.ranges),
        "dataset_sources": [
            {
                "symbol": item.symbol,
                "dataset_id": item.dataset_id,
                "sha256": item.sha256,
                "coverage_start": item.coverage_start.isoformat(),
                "coverage_end": item.coverage_end.isoformat(),
            }
            for item in selection.sources
        ],
        "counts": counts,
        "resources": {
            "fit_matrix_disk_bytes": cells * _CELL_BYTES,
            "estimated_training_memory_bytes": cells * _CELL_BYTES * _TRAINING_COPY_FACTOR,
            "threads": int(str(config.resources.get("threads", 4))),
            "device": str(config.resources.get("device", "cpu")),
            "note": (
                "Disk-backed matrices are memory-mapped; the estimate is the native library's "
                "working copy of the fit partition."
            ),
        },
        "holdout": "reserved final holdout excluded by range validation",
    }
    text = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    path = lock_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise FileExistsError(
                f"a different preparation already exists for this configuration: {path}"
            )
    else:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    return path, payload


def read_training_lock(path: str | Path, config: ModelExperimentConfig) -> dict[str, Any]:
    lock = json.loads(Path(path).read_text(encoding="utf-8"))
    if lock.get("schema") != "mike_trade_training_lock":
        raise ValueError(f"{path} is not a training lock")
    if lock["config_sha256"] != config.config_sha256:
        raise ValueError("the training lock was prepared from a different configuration file")
    return lock  # type: ignore[no-any-return]


def check_configuration(
    config_path: str | Path | None, *, mode: str | None = None
) -> dict[str, Any]:
    """Offline pre-flight of every model pin in a trading configuration."""
    from mike_trade.app import _collect_model_references, load_app_settings
    from mike_trade.modeling.inference import ModelLoadError
    from mike_trade.modeling.serving import build_inference_service
    from mike_trade.research.commands import _research_registry

    settings = load_app_settings(config_path)
    effective_mode = (mode or settings.mode.value).upper()
    references = _collect_model_references(settings, _research_registry())
    approvals = ApprovalRepository(settings.model_root)
    repository = FileModelRepository(settings.model_root)
    results: list[dict[str, Any]] = []
    for key, reference in sorted(references.items()):
        row: dict[str, Any] = {
            "model": key,
            "strategy_role": reference.role,
            "algorithm": reference.algorithm,
            "problems": [],
        }
        try:
            service = build_inference_service(
                settings.model_root, [reference], lambda: datetime.now(UTC)
            )
        except ModelLoadError as exc:
            row["problems"].append(f"{exc.code}: {exc}")
            row["status"] = "FAIL"
            results.append(row)
            continue
        manifest = repository.get(reference.model_id, reference.model_version)[0]
        row["artifact_sha256"] = manifest.artifact_sha256
        row["feed"] = manifest.feed
        row["reference_symbols"] = list(service.required_reference_symbols())
        if manifest.feed != settings.market_data_feed.strip().lower():
            row["problems"].append(
                f"MODEL_FEED_MISMATCH: model {manifest.feed} vs market_data.feed "
                f"{settings.market_data_feed}"
            )
        needed = required_stage_for_mode(effective_mode)
        highest = approvals.highest_valid_stage(manifest)
        row["highest_approval"] = highest
        row["required_approval"] = needed
        if needed is not None:
            try:
                approvals.require(manifest, effective_mode)
            except ValueError as exc:
                row["problems"].append(f"MODEL_NOT_APPROVED: {exc}")
        row["status"] = "FAIL" if row["problems"] else "OK"
        results.append(row)
    return {
        "config": str(config_path) if config_path else None,
        "mode": effective_mode,
        "model_root": str(settings.model_root),
        "market_data_feed": settings.market_data_feed,
        "models_configured": len(references),
        "ok": all(item["status"] == "OK" for item in results),
        "models": results,
    }


def read_model_health(database: str | Path) -> dict[str, Any]:
    """Model status published by the running engine (read-only SQLite access)."""
    path = Path(database)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    try:
        row = connection.execute(
            "SELECT status, heartbeat_at, details_json FROM service_health "
            "WHERE component = 'models' ORDER BY heartbeat_at DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return {"published": False, "reason": "the engine has not published model status"}
    details = json.loads(str(row[2]))
    summary = json.loads(str(details.get("summary_json", "{}")))
    return {
        "published": True,
        "component_status": row[0],
        "heartbeat_at": row[1],
        **summary,
    }


def compare_reports(paths: Sequence[str | Path]) -> dict[str, Any]:
    """Side-by-side configuration results from several model-backtest reports.

    Reports are comparable when they share window, feed, universe, symbols, capital, execution
    assumptions and protective stop; otherwise the figures are still listed but flagged.
    """
    loaded = []
    for path in paths:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        if report.get("schema") != "mike_trade_model_backtest_report":
            raise ValueError(f"{path} is not a model-backtest report")
        loaded.append((str(path), report))
    signatures = {
        json.dumps(
            [
                r["window"],
                r["feed"],
                r["universe_id"],
                r["symbols"],
                r["starting_capital_per_account"],
                r["execution_assumptions"],
                r["protective_stop"],
                r["end_of_day_close"],
            ],
            sort_keys=True,
        )
        for _, r in loaded
    }
    rows = []
    for path, report in loaded:
        models = {m["name"]: f"{m['model_id']}@{m['model_version']}" for m in report["models"]}
        for config in report["configurations"]:
            rows.append(
                {
                    "report": path,
                    "campaign_id": report["campaign_id"],
                    "configuration": config["name"],
                    "buy": config["buy"],
                    "sell": config["sell"],
                    "models": {k: v for k, v in models.items()},
                    **{
                        key: config.get(key)
                        for key in (
                            "trades",
                            "wins",
                            "losses",
                            "breakeven",
                            "win_rate",
                            "net_profit",
                            "return_on_capital",
                            "profit_factor",
                            "max_drawdown",
                            "max_drawdown_fraction",
                            "trading_costs",
                            "estimated_slippage",
                        )
                    },
                }
            )
    return {
        "all_comparable": len(signatures) == 1,
        "comparison_requirements": (
            "same window, feed, universe and symbols, starting capital, execution assumptions, "
            "protective stop and end-of-day policy"
        ),
        "warning": None
        if len(signatures) == 1
        else "The reports use different experiment inputs; do not rank these results.",
        "rows": rows,
        "disclaimer": "Historical simulation results; not a forecast of profitability.",
    }
