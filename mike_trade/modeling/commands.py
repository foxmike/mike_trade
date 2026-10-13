"""Explicit operator CLI for model training and immutable artifact inspection."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from mike_trade.modeling.artifacts import FileModelRepository
from mike_trade.modeling.config import load_experiment_config
from mike_trade.modeling.training import ModelTrainingService, evaluate_saved_model


def register(subparsers: Any) -> None:
    train = subparsers.add_parser("model-train", help="train and persist one configured model")
    train.add_argument("--config", required=True, help="versioned model YAML/JSON configuration")
    train.set_defaults(func=run_train)

    evaluate = subparsers.add_parser(
        "model-evaluate", help="re-evaluate a pinned model on its configured evaluation range"
    )
    evaluate.add_argument("--config", required=True)
    evaluate.set_defaults(func=run_evaluate)

    inspect = subparsers.add_parser("model-inspect", help="inspect manifest and evaluation")
    inspect.add_argument("--model-id", required=True)
    inspect.add_argument("--version", required=True)
    inspect.add_argument(
        "--root", default=os.environ.get("MIKE_TRADE_MODEL_DIR", "research/models")
    )
    inspect.set_defaults(func=run_inspect)

    listing = subparsers.add_parser("model-list", help="list immutable model versions")
    listing.add_argument(
        "--root", default=os.environ.get("MIKE_TRADE_MODEL_DIR", "research/models")
    )
    listing.set_defaults(func=run_list)

    compare = subparsers.add_parser("model-compare", help="compare stored predictive evaluations")
    compare.add_argument(
        "--models", required=True, help="comma-separated model_id@version references"
    )
    compare.add_argument(
        "--root", default=os.environ.get("MIKE_TRADE_MODEL_DIR", "research/models")
    )
    compare.set_defaults(func=run_compare)


def _print(value: object) -> None:
    print(json.dumps(value, sort_keys=True, indent=2, default=str))


def run_train(args: argparse.Namespace) -> int:
    config = load_experiment_config(args.config)
    outcome = ModelTrainingService(config).train()
    _print(
        {
            "model_id": outcome.stored.manifest.model_id,
            "model_version": outcome.stored.manifest.model_version,
            "algorithm": outcome.stored.manifest.algorithm,
            "artifact": str(outcome.stored.artifact_path),
            "manifest": str(outcome.stored.manifest_path),
            "evaluation": str(outcome.stored.evaluation_path),
            "metrics": outcome.evaluation.metrics,
            "search_trials": outcome.search_trials,
        }
    )
    return 0


def run_evaluate(args: argparse.Namespace) -> int:
    config = load_experiment_config(args.config)
    evaluation = evaluate_saved_model(config)
    _print(json.loads(evaluation.to_json_bytes()))
    return 0


def run_inspect(args: argparse.Namespace) -> int:
    repository = FileModelRepository(args.root)
    manifest, _ = repository.get(args.model_id, args.version)
    evaluation = (
        json.loads(repository.get_evaluation(args.model_id, args.version))
        if manifest.evaluation_sha256 is not None
        else None
    )
    _print({"manifest": manifest.to_mapping(), "evaluation": evaluation})
    return 0


def run_list(args: argparse.Namespace) -> int:
    manifests = FileModelRepository(args.root).list()
    _print(
        [
            {
                "model_id": item.model_id,
                "model_version": item.model_version,
                "algorithm": item.algorithm,
                "objective": item.learning_objective,
                "created_at": item.created_at,
                "feed": item.feed,
                "artifact_sha256": item.artifact_sha256,
            }
            for item in manifests
        ]
    )
    return 0


def run_compare(args: argparse.Namespace) -> int:
    repository = FileModelRepository(args.root)
    result = []
    signatures: list[tuple[object, ...]] = []
    for reference in str(args.models).split(","):
        try:
            model_id, version = reference.strip().split("@", 1)
        except ValueError:
            raise ValueError("model references must use model_id@version") from None
        manifest, _ = repository.get(model_id, version)
        evaluation = json.loads(repository.get_evaluation(model_id, version))
        signatures.append(
            (
                manifest.label_schema_sha256,
                manifest.feature_schema_sha256,
                manifest.selected_feature_names,
                manifest.feed,
                manifest.universe_id,
                tuple((item.dataset_id, item.sha256) for item in manifest.dataset_sources),
                tuple((item.start, item.end) for item in manifest.validation_ranges),
            )
        )
        result.append(
            {
                "model": reference.strip(),
                "algorithm": manifest.algorithm,
                "objective": manifest.learning_objective,
                "sample_count": evaluation.get("sample_count"),
                "metrics": evaluation.get("metrics"),
                "baseline": evaluation.get("baseline"),
                "disclaimer": evaluation.get("disclaimer"),
            }
        )
    comparable = bool(signatures) and all(item == signatures[0] for item in signatures)
    _print(
        {
            "all_comparable": comparable,
            "comparison_requirements": (
                "same label, feature schema/order, feed, universe, dataset snapshots, "
                "and validation ranges"
            ),
            "warning": (
                None
                if comparable
                else "Metrics are listed, but the pinned experiment inputs are not identical."
            ),
            "models": result,
        }
    )
    return 0
