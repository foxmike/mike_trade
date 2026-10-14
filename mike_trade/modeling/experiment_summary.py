"""Plain-text outcome summary of one saved triple-barrier model (read-only).

Answers, from the stored evaluation alone:

1. how often the favorable (+) barrier was reached first, how often the adverse (-) barrier was, and
   how often neither was reached within the horizon - per data partition;
2. whether higher predicted P(favorable) identifies genuinely higher rates of favorable outcomes;
3. the outcome mix by decile of P(favorable).

Predictive metrics are label statistics, not trading results: nothing here is a profitability claim.

    python -m mike_trade.modeling.experiment_summary --model-id ID --version 1.0.0 [--root DIR]
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mike_trade.modeling.artifacts import FileModelRepository

__all__ = ["summarize_experiment", "main"]

_NAMES = {"1": "favorable (+ barrier first)", "-1": "adverse (- barrier first)", "0": "neither (timeout)"}


def _pct(part: int, total: int) -> str:
    return f"{100.0 * part / total:6.2f}%" if total else "     —"


def _f(value: Any, digits: int = 4) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def summarize_experiment(root: str | Path, model_id: str, version: str) -> str:
    repository = FileModelRepository(root)
    manifest, _artifact = repository.get(model_id, version)
    evaluation: Mapping[str, Any] = json.loads(repository.get_evaluation(model_id, version))
    counts = evaluation.get("dataset_counts", {})
    label = manifest.label_schema
    out: list[str] = [
        f"== {model_id}@{version}  ({manifest.algorithm}, {manifest.feature_schema.timeframe} candles "
        f"from {manifest.timeframe} {manifest.feed.upper()} data)",
        f"label {label.label_id}@{label.version}: +{label.parameters.get('positive_barrier')} before "
        f"-{label.parameters.get('negative_barrier')} within {label.horizon_bars} candles "
        f"({label.parameters.get('horizon_minutes', '?')} minutes); "
        f"same-candle ambiguity: {label.parameters.get('ambiguity_policy')}",
        f"features: {manifest.feature_schema.feature_id}@{manifest.feature_schema.version} "
        f"({len(manifest.selected_feature_names)} features)",
        "",
        "-- Outcome frequency by partition (rows are labelled signals, not independent trades)",
    ]
    classes = list(manifest.class_labels)
    header = f"{'partition':<16}{'rows':>10}  " + "  ".join(f"{_NAMES.get(c, c):>28}" for c in classes)
    out.append(header)
    for name, mix in (counts.get("target_counts") or {}).items():
        total = sum(mix.values())
        cells = "  ".join(f"{mix.get(c, 0):>15d} {_pct(mix.get(c, 0), total)}" for c in classes)
        out.append(f"{name:<16}{total:>10d}  {cells}")
    stats = counts.get("build_stats") or {}
    if stats:
        out += ["", f"candle aggregation: {json.dumps(stats.get('aggregation', {}), sort_keys=True)}",
                f"label resolution : {json.dumps(stats.get('labels', {}), sort_keys=True)}"]

    metrics = evaluation.get("metrics", {})
    baseline = evaluation.get("baseline", {})
    out += [
        "",
        "-- Held-out evaluation (predictive metrics only; NOT trading results)",
        f"rows {evaluation.get('sample_count')}  log_loss {_f(metrics.get('log_loss'))} "
        f"(class-frequency baseline {_f(baseline.get('log_loss'))}; "
        f"improvement {_f(baseline.get('model_log_loss_improvement'), 5)})",
        f"brier {_f(metrics.get('brier_score'))}  expected_calibration_error "
        f"{_f(metrics.get('expected_calibration_error'))}  accuracy {_f(metrics.get('accuracy'))} "
        "(accuracy alone says nothing about profitability)",
        f"confusion matrix rows=actual, cols=predicted, classes {classes}:",
    ]
    out += [f"  {row}" for row in evaluation.get("confusion_matrix", [])]
    per_class = metrics.get("per_class") or {}
    for c in classes:
        stat = per_class.get(c) or {}
        out.append(
            f"  class {c:>2}: precision {_f(stat.get('precision'))} recall {_f(stat.get('recall'))} "
            f"support {stat.get('support')}"
        )
    partitions = evaluation.get("partition_metrics") or {}
    if partitions:
        out += ["", "-- Final-model log loss / accuracy by partition (fit vs held-out)"]
        for name, values in partitions.items():
            out.append(
                f"  {name:<15} rows {values.get('sample_count'):>9}  log_loss {_f(values.get('log_loss'))}"
                f"  brier {_f(values.get('brier_score'))}  accuracy {_f(values.get('accuracy'))}"
            )
    dist = evaluation.get("prediction_distribution") or {}
    out += ["", f"prediction scores: {json.dumps(dist, sort_keys=True)}"]

    block = evaluation.get("favorable_deciles")
    if block:
        fav = block["favorable_class"]
        out += [
            "",
            f"-- Does a higher predicted P(favorable) identify more favorable outcomes? "
            f"(evaluation partition, base rate {_f(block['base_favorable_rate'])})",
            f"favorable-vs-rest ROC AUC {_f(block.get('favorable_vs_rest_roc_auc'))}; "
            f"correlation(decile, favorable rate) {_f(block.get('correlation_decile_vs_favorable_rate'))}; "
            f"top-decile lift {_f(block.get('top_decile_lift_vs_base_rate'), 2)}x",
            f"P(favorable) quantiles: {json.dumps(block.get('favorable_probability_quantiles', {}))}",
            f"decile 1 = lowest predicted P({fav}); decile {len(block['deciles'])} = highest",
            f"{'decile':>6}{'rows':>9}{'P range':>17}{'mean P':>8}"
            + "".join(f"{'rate ' + c:>9}" for c in classes)
            + f"{'lift':>7}{'barrier-implied mean ret (pre-cost)':>38}",
        ]
        for row in block["deciles"]:
            rates = "".join(f"{row['observed_rates'][c]:>9.4f}" for c in classes)
            out.append(
                f"{row['decile']:>6}{row['count']:>9}"
                f"{_f(row['min_probability'], 3) + '-' + _f(row['max_probability'], 3):>17}"
                f"{row['mean_probability']:>8.4f}{rates}"
                f"{_f(row['favorable_lift_vs_base_rate'], 2):>7}"
                f"{_f(row.get('barrier_implied_mean_return_before_costs'), 5):>38}"
            )
        out += ["", block["disclaimer"]]
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--root", default=os.environ.get("MIKE_TRADE_MODEL_DIR", "research/models"))
    args = parser.parse_args(argv)
    print(summarize_experiment(args.root, args.model_id, args.version))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
