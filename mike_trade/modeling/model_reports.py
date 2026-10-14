# ruff: noqa: E501  (Markdown table rows are intentionally one line each)
"""Operator-readable reports for one saved model (JSON, Markdown and CSV).

Every value is read from the immutable manifest, its checksummed evaluation sidecar, and (when
present) the walk-forward summary — nothing is estimated or filled in.  Where a value was not
recorded (for example observation counts of a model trained before they were recorded) the
report says so instead of inventing it.  Output is deterministic: no generation timestamps.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mike_trade.modeling.approvals import ApprovalRepository
from mike_trade.modeling.artifacts import FileModelRepository, ModelArtifactManifest

__all__ = [
    "build_model_report",
    "write_model_report",
    "render_markdown",
    "report_directory",
    "feature_importance_rows",
    "classify_window",
]

NOT_RECORDED = "not recorded"


def report_directory(model_root: str | Path, model_id: str, version: str) -> Path:
    return Path(model_root) / "reports" / model_id / version


def feature_importance_rows(manifest: ModelArtifactManifest) -> list[dict[str, Any]]:
    """Rank features by native gain; ``weight`` and optional permutation importance alongside."""
    table: dict[str, dict[str, float]] = {}
    for key, value in manifest.feature_importance.items():
        name, _, kind = key.rpartition(".")
        try:
            table.setdefault(name, {})[kind] = float(str(value))
        except ValueError:
            continue
    total = sum(item.get("gain", 0.0) for item in table.values()) or 1.0
    rows = [
        {
            "feature": name,
            "gain": item.get("gain", 0.0),
            "gain_share": item.get("gain", 0.0) / total,
            "weight": item.get("weight"),
            "permutation": item.get("permutation"),
        }
        for name, item in table.items()
    ]
    return sorted(rows, key=lambda row: (-float(row["gain"]), str(row["feature"])))


def classify_window(manifest: ModelArtifactManifest, start: Any, end: Any) -> str:
    """How a backtest window relates to the data the model used.

    ``in_sample`` overlaps training data; ``tuning_overlap`` overlaps early-stopping or
    calibration data; ``pre_training_period`` lies wholly before training began;
    ``out_of_sample_development_test`` touches none of the model's fit/tuning data (it may
    coincide with the model's evaluation partition, which only scored the finished model).
    ``holdout`` overlaps the reserved final holdout.
    """
    from mike_trade.modeling.splits import FINAL_HOLDOUT, TimeRange

    window = TimeRange(start, end)
    if window.overlaps(FINAL_HOLDOUT):
        return "holdout"
    if any(window.overlaps(item) for item in manifest.training_ranges):
        return "in_sample"
    tuning = manifest.validation_ranges[:-1]  # early stopping (+ calibration); last = evaluation
    if any(window.overlaps(item) for item in tuning):
        return "tuning_overlap"
    first = min(item.start for item in manifest.training_ranges)
    if window.end <= first:
        return "pre_training_period"
    return "out_of_sample_development_test"


def _rows(mapping: Mapping[str, Any] | None, key: str) -> list[dict[str, Any]]:
    return [{key: name, **dict(value)} for name, value in sorted((mapping or {}).items())]


def build_model_report(model_root: str | Path, model_id: str, version: str) -> dict[str, Any]:
    repository = FileModelRepository(model_root)
    manifest, _ = repository.get(model_id, version)  # checksum-verified
    evaluation: dict[str, Any] | None = None
    evaluation_sha: str | None = None
    if manifest.evaluation_sha256 is not None:
        payload = repository.get_evaluation(model_id, version)
        evaluation = json.loads(payload)
        evaluation_sha = hashlib.sha256(payload).hexdigest()
    walk_forward: dict[str, Any] | None = None
    for candidate in sorted(
        (Path(model_root) / "walk_forward").glob(f"*/{manifest.training_config_sha256}.json")
    ):
        walk_forward = json.loads(candidate.read_text(encoding="utf-8"))
        break
    counts = (evaluation or {}).get("dataset_counts")
    approvals = ApprovalRepository(model_root)
    highest = approvals.highest_valid_stage(manifest)
    ranges = {
        "training": [
            {"start": item.start.isoformat(), "end": item.end.isoformat()}
            for item in manifest.training_ranges
        ],
        "validation": [
            {"start": item.start.isoformat(), "end": item.end.isoformat()}
            for item in manifest.validation_ranges
        ],
    }
    sources = [
        {"symbol": item.symbol, "dataset_id": item.dataset_id, "sha256": item.sha256}
        for item in manifest.dataset_sources
    ]
    return {
        "schema": "mike_trade_model_report",
        "schema_version": 1,
        "identity": {
            "model_id": manifest.model_id,
            "model_version": manifest.model_version,
            "algorithm": manifest.algorithm,
            "adapter": f"{manifest.adapter_id}@{manifest.adapter_version}",
            "objective": manifest.learning_objective,
            "task": manifest.label_schema.task.value,
            "created_at": manifest.created_at.isoformat(),
            "artifact_sha256": manifest.artifact_sha256,
            "artifact_format": manifest.artifact_format,
            "artifact_size": manifest.artifact_size,
            "manifest_training_config_sha256": manifest.training_config_sha256,
            "evaluation_sha256": evaluation_sha,
            "software_versions": dict(manifest.software_versions),
            "random_seed": manifest.random_seed,
        },
        "data": {
            "feed": manifest.feed,
            "timeframe": manifest.timeframe,
            "adjustment": manifest.adjustment,
            "universe_id": manifest.universe_id,
            "symbols": len(sources),
            "datasets": sources,
            "ranges": ranges,
            "validation_method": manifest.validation_method,
            "holdout_evaluated": manifest.holdout_evaluated,
            "execution_assumptions": dict(manifest.execution_assumptions),
        },
        "features": {
            "schema": f"{manifest.feature_schema.feature_id}@{manifest.feature_schema.version}",
            "schema_sha256": manifest.feature_schema_sha256,
            "selected": list(manifest.selected_feature_names),
            "label_schema": f"{manifest.label_schema.label_id}@{manifest.label_schema.version}",
            "label_schema_sha256": manifest.label_schema_sha256,
            "label_parameters": dict(manifest.label_schema.parameters),
            "horizon_bars": manifest.label_schema.horizon_bars,
            "class_labels": list(manifest.class_labels),
        },
        "observations": counts if counts is not None else {"status": NOT_RECORDED},
        "hyperparameters": dict(manifest.hyperparameters),
        "calibration_method": dict(manifest.calibration),
        "evaluation": None
        if evaluation is None
        else {
            "sample_count": evaluation.get("sample_count"),
            "metrics": evaluation.get("metrics"),
            "baseline": evaluation.get("baseline"),
            "prediction_distribution": evaluation.get("prediction_distribution"),
            "confusion_matrix": evaluation.get("confusion_matrix"),
            "class_labels": evaluation.get("class_labels"),
            "calibration": evaluation.get("calibration"),
            "class_calibration": evaluation.get("class_calibration"),
            "favorable_deciles": evaluation.get("favorable_deciles"),
            "per_symbol": evaluation.get("per_symbol"),
            "per_period": evaluation.get("per_period"),
            "search_trials": evaluation.get("search_trials"),
            "disclaimer": evaluation.get("disclaimer"),
        },
        "feature_importance": feature_importance_rows(manifest),
        "feature_importance_disclaimer": (
            "Importance is predictive attribution on this data, not evidence of causation."
        ),
        "walk_forward": walk_forward,
        "approval": {"highest_valid_stage": highest},
        "disclaimer": (
            "Predictive metrics describe held-out labels. They are not trading results and do "
            "not establish profitability."
        ),
    }


def _csv(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(columns), lineterminator="\n", extrasaction="ignore"
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in columns})
    return buffer.getvalue()


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _prevalence_markdown(obs: Mapping[str, Any]) -> list[str]:
    """Class counts and prevalence per partition (candle experiments), plus build counters."""
    counts = obs.get("target_counts")
    if not counts:
        return []
    lines = ["", "### Class prevalence by partition", ""]
    labels = sorted({label for part in counts.values() for label in part})
    lines += [
        "| Partition | Rows | " + " | ".join(f"class {l} (n, %)" for l in labels) + " |",
        "|---|---:|" + "---:|" * len(labels),
    ]
    for name, part in counts.items():
        total = sum(part.values())
        cells = " | ".join(
            f"{part.get(l, 0)} ({100 * part.get(l, 0) / total:.2f}%)" if total else "—"
            for l in labels
        )
        lines.append(f"| {name} | {total} | {cells} |")
    stats = obs.get("build_stats") or {}
    for group in ("aggregation", "labels"):
        if stats.get(group):
            lines += ["", f"{group.capitalize()} counters: `{json.dumps(stats[group], sort_keys=True)}`"]
    return lines


def _decile_markdown(block: Mapping[str, Any]) -> list[str]:
    fav = block["favorable_class"]
    lines = [
        "",
        f"### Outcome mix by decile of predicted P(class {fav}) "
        f"(base rate {_fmt(block['base_favorable_rate'])}; "
        f"AUC {_fmt(block.get('favorable_vs_rest_roc_auc'))}; "
        f"decile-vs-rate correlation {_fmt(block.get('correlation_decile_vs_favorable_rate'))})",
        "",
        f"*{block['ordering']}. {block['disclaimer']}*",
        "",
    ]
    labels = list(block["deciles"][0]["observed_rates"]) if block["deciles"] else []
    lines += [
        "| Decile | Rows | P range | Mean P | "
        + " | ".join(f"rate {l}" for l in labels)
        + " | Lift | Barrier-implied mean return (pre-cost) |",
        "|---:|---:|---|---:|" + "---:|" * len(labels) + "---:|---:|",
    ]
    for row in block["deciles"]:
        rates = " | ".join(_fmt(row["observed_rates"][l]) for l in labels)
        lines.append(
            f"| {row['decile']} | {row['count']} | "
            f"{_fmt(row['min_probability'])}–{_fmt(row['max_probability'])} | "
            f"{_fmt(row['mean_probability'])} | {rates} | "
            f"{_fmt(row['favorable_lift_vs_base_rate'], 2)} | "
            f"{_fmt(row.get('barrier_implied_mean_return_before_costs'), 5)} |"
        )
    return lines


def render_markdown(report: Mapping[str, Any]) -> str:
    ident, data, feats = report["identity"], report["data"], report["features"]
    lines = [
        f"# Model report: {ident['model_id']}@{ident['model_version']}",
        "",
        f"*{report['disclaimer']}*",
        "",
        "## Identity",
        "",
        f"- Algorithm: **{ident['algorithm']}** ({ident['adapter']}, `{ident['artifact_format']}`)",
        f"- Objective: `{ident['objective']}` ({ident['task']})",
        f"- Artifact SHA-256: `{ident['artifact_sha256']}` ({ident['artifact_size']} bytes)",
        f"- Created: {ident['created_at']}; seed {ident['random_seed']}",
        f"- Software: {', '.join(f'{k} {v}' for k, v in sorted(ident['software_versions'].items()))}",
        f"- Highest valid approval: **{report['approval']['highest_valid_stage'] or 'none'}**",
        "",
        "## Data",
        "",
        f"- Feed: **{data['feed'].upper()}**, {data['timeframe']} bars, {data['adjustment']} prices",
        f"- Universe: `{data['universe_id']}` ({data['symbols']} pinned snapshots)",
        f"- Validation method: {data['validation_method']}",
        f"- Final holdout evaluated: **{str(data['holdout_evaluated']).lower()}**",
    ]
    for kind in ("training", "validation"):
        for item in data["ranges"][kind]:
            lines.append(f"- {kind.capitalize()} range: {item['start']} → {item['end']}")
    obs = report["observations"]
    lines += ["", "## Observations", ""]
    if obs.get("status") == NOT_RECORDED:
        lines.append("Observation counts were not recorded for this model.")
    else:
        lines += [
            f"- Fit (training) rows: **{obs['fit']}**",
            f"- Early-stopping rows: {obs['early_stopping']}",
            f"- Calibration rows: {obs['calibration']}",
            f"- Evaluation rows: {obs['evaluation']}",
            f"- Features: {obs['features']}",
            f"- Fit label mix: {json.dumps(obs.get('fit_target_counts', {}), sort_keys=True)}",
        ]
        lines += _prevalence_markdown(obs)
    lines += ["", "## Features and label", ""]
    lines += [
        f"- Feature schema: `{feats['schema']}` (`{feats['schema_sha256'][:16]}…`)",
        f"- Label: `{feats['label_schema']}`, horizon {feats['horizon_bars']} bars, "
        f"parameters {json.dumps(feats['label_parameters'], sort_keys=True)}",
        f"- Classes: {', '.join(feats['class_labels']) or 'regression'}",
        f"- Selected features ({len(feats['selected'])}): {', '.join(feats['selected'])}",
        "",
        "## Hyperparameters",
        "",
        f"`{json.dumps(report['hyperparameters'], sort_keys=True)}`",
        "",
        f"Probability calibration: `{json.dumps(report['calibration_method'], sort_keys=True)}`",
    ]
    evaluation = report["evaluation"]
    if evaluation:
        metrics = evaluation["metrics"] or {}
        scalar = {k: v for k, v in metrics.items() if isinstance(v, (int, float))}
        lines += ["", f"## Held-out predictive metrics ({evaluation['sample_count']} rows)", ""]
        lines += ["| Metric | Value |", "|---|---:|"]
        lines += [f"| {k} | {_fmt(v)} |" for k, v in sorted(scalar.items())]
        baseline = evaluation.get("baseline") or {}
        if baseline:
            lines += ["", "### Baseline", ""]
            lines += [
                f"- {k}: {_fmt(v) if not isinstance(v, dict) else json.dumps(v, sort_keys=True)}"
                for k, v in sorted(baseline.items())
            ]
        if evaluation.get("calibration"):
            lines += [
                "",
                "### Confidence calibration",
                "",
                "| Bin | Count | Mean confidence | Observed accuracy |",
                "|---|---:|---:|---:|",
            ]
            lines += [
                f"| {_fmt(r['lower'], 1)}–{_fmt(r['upper'], 1)} | {r['count']} | "
                f"{_fmt(r['mean_probability'])} | {_fmt(r['observed_accuracy'])} |"
                for r in evaluation["calibration"]
            ]
        for label, block in sorted((evaluation.get("class_calibration") or {}).items()):
            lines += [
                "",
                f"### Reliability of P(class {label}) — ECE {_fmt(block['expected_calibration_error'])}",
                "",
                "| Bin | Count | Mean predicted | Observed frequency |",
                "|---|---:|---:|---:|",
            ]
            lines += [
                f"| {_fmt(r['lower'], 1)}–{_fmt(r['upper'], 1)} | {r['count']} | "
                f"{_fmt(r['mean_predicted'])} | {_fmt(r['observed_frequency'])} |"
                for r in block["bins"]
            ]
        deciles = evaluation.get("favorable_deciles")
        if deciles:
            lines += _decile_markdown(deciles)
        per_period = evaluation.get("per_period") or {}
        if per_period:
            lines += [
                "",
                "### Per period",
                "",
                "| Period | Rows | Log loss | Accuracy |",
                "|---|---:|---:|---:|",
            ]
            lines += [
                f"| {k} | {v.get('sample_count')} | {_fmt(v.get('log_loss', v.get('rmse')))} | "
                f"{_fmt(v.get('accuracy', v.get('r2')))} |"
                for k, v in sorted(per_period.items())
            ]
        per_symbol = evaluation.get("per_symbol") or {}
        lines += [
            "",
            f"Per-symbol predictive metrics for {len(per_symbol)} symbols are in `per_symbol.csv`.",
        ]
    lines += [
        "",
        "## Feature importance (native gain)",
        "",
        "| Feature | Gain share | Weight | Permutation |",
        "|---|---:|---:|---:|",
    ]
    lines += [
        f"| {r['feature']} | {_fmt(r['gain_share'])} | {_fmt(r['weight'], 1)} | {_fmt(r['permutation'])} |"
        for r in report["feature_importance"]
    ]
    lines += ["", f"*{report['feature_importance_disclaimer']}*"]
    walk = report.get("walk_forward")
    if walk:
        lines += [
            "",
            f"## Walk-forward validation ({len(walk['folds'])} folds)",
            "",
            "| Fold | Validation | Fit rows | Eval rows | Log loss | Accuracy |",
            "|---|---|---:|---:|---:|---:|",
        ]
        for fold in walk["folds"]:
            ev = fold["ranges"]["evaluation"]
            lines.append(
                f"| {fold['fold']} | {ev['start'][:10]} → {ev['end'][:10]} | {fold['counts']['fit']} | "
                f"{fold['counts']['evaluation']} | {_fmt(fold['metrics'].get('log_loss'))} | "
                f"{_fmt(fold['metrics'].get('accuracy'))} |"
            )
        lines += ["", "| Metric | Mean | Min | Max | Stdev |", "|---|---:|---:|---:|---:|"]
        lines += [
            f"| {name} | {_fmt(s['mean'])} | {_fmt(s['min'])} | {_fmt(s['max'])} | {_fmt(s['stdev'])} |"
            for name, s in walk["summary"].items()
        ]
    else:
        lines += [
            "",
            "## Walk-forward validation",
            "",
            "No walk-forward summary exists for this configuration.",
        ]
    return "\n".join(lines) + "\n"


def write_model_report(
    model_root: str | Path,
    model_id: str,
    version: str,
    output_dir: str | Path | None = None,
) -> dict[str, Path]:
    """Write ``model_report.{json,md}`` plus CSV tables; return the written paths."""
    report = build_model_report(model_root, model_id, version)
    directory = Path(output_dir) if output_dir else report_directory(model_root, model_id, version)
    directory.mkdir(parents=True, exist_ok=True)
    evaluation = report["evaluation"] or {}
    tables: dict[str, str] = {
        "feature_importance.csv": _csv(
            report["feature_importance"], ["feature", "gain", "gain_share", "weight", "permutation"]
        ),
        "calibration.csv": _csv(
            evaluation.get("calibration") or [],
            ["lower", "upper", "count", "mean_probability", "observed_accuracy"],
        ),
        "class_calibration.csv": _csv(
            [
                {"class": label, **row}
                for label, block in sorted((evaluation.get("class_calibration") or {}).items())
                for row in block["bins"]
            ],
            ["class", "lower", "upper", "count", "mean_predicted", "observed_frequency"],
        ),
        "per_symbol.csv": _csv(
            _rows(evaluation.get("per_symbol"), "symbol"),
            ["symbol", "sample_count", "log_loss", "brier_score", "accuracy", "rmse", "mae", "r2"],
        ),
        "per_period.csv": _csv(
            _rows(evaluation.get("per_period"), "period"),
            ["period", "sample_count", "log_loss", "brier_score", "accuracy", "rmse", "mae", "r2"],
        ),
    }
    block = evaluation.get("favorable_deciles")
    if block and block["deciles"]:
        labels = list(block["deciles"][0]["observed_rates"])
        tables["favorable_deciles.csv"] = _csv(
            [
                {
                    "decile": row["decile"],
                    "count": row["count"],
                    "min_probability": row["min_probability"],
                    "max_probability": row["max_probability"],
                    "mean_probability": row["mean_probability"],
                    **{f"rate_{label}": row["observed_rates"][label] for label in labels},
                    "favorable_lift_vs_base_rate": row["favorable_lift_vs_base_rate"],
                    "barrier_implied_mean_return_before_costs": row.get(
                        "barrier_implied_mean_return_before_costs"
                    ),
                }
                for row in block["deciles"]
            ],
            [
                "decile", "count", "min_probability", "max_probability", "mean_probability",
                *[f"rate_{label}" for label in labels], "favorable_lift_vs_base_rate",
                "barrier_implied_mean_return_before_costs",
            ],
        )
    walk = report.get("walk_forward")
    if walk:
        tables["walk_forward_folds.csv"] = _csv(
            [
                {
                    "fold": f["fold"],
                    "validation_start": f["ranges"]["evaluation"]["start"],
                    "validation_end": f["ranges"]["evaluation"]["end"],
                    "fit_rows": f["counts"]["fit"],
                    "evaluation_rows": f["counts"]["evaluation"],
                    **{
                        k: f["metrics"].get(k)
                        for k in (
                            "log_loss",
                            "brier_score",
                            "accuracy",
                            "expected_calibration_error",
                            "rmse",
                        )
                    },
                }
                for f in walk["folds"]
            ],
            [
                "fold",
                "validation_start",
                "validation_end",
                "fit_rows",
                "evaluation_rows",
                "log_loss",
                "brier_score",
                "accuracy",
                "expected_calibration_error",
                "rmse",
            ],
        )
    paths = {"json": directory / "model_report.json", "markdown": directory / "model_report.md"}
    paths["json"].write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    paths["markdown"].write_text(render_markdown(report), encoding="utf-8")
    for name, text in tables.items():
        path = directory / name
        path.write_text(text, encoding="utf-8")
        paths[name] = path
    return paths
